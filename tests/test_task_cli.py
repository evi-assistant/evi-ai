"""`evi task` (Phase 1, loop-task-queue): filing, claiming and running tasks.

Acceptance criteria covered here, each with its test:

1. The director creates a task, and `evi task start T1 --backend <local>`
   runs it in a worktree under the implementer policy, leaving it
   implemented with a report and a branch —
   test_start_runs_the_task_in_its_worktree_and_leaves_it_implemented
   (on the shared fake-LLM client: the worktree and branch task/T1 are
   made, the verify gate and then eVi itself run the task's required check
   there, the report is written under eVi's home, the main checkout is
   untouched and nothing is committed).
2. A start never yields a verified status: it stops at implemented, and a
   failing required check, whether the verify gate or eVi's own run of it
   catches it, returns the task to open with the attempt counted —
   test_a_failing_required_check_returns_the_task_to_open,
   test_evis_own_run_of_the_required_checks_is_ground_truth,
   test_a_later_attempt_reuses_the_worktree_and_counts_again.
4. An expired lease can be reclaimed — test_an_expired_lease_is_reclaimed_from_the_cli
   (and test_start_refuses_someone_elses_live_claim_unless_forced), also
   after a start died with its worktree locked —
   test_start_takes_over_the_worktree_a_dead_start_left_locked.

Also: a start refuses an untrusted repository and a delegate CLI-agent
backend (a [models] one too, whatever allow_cli_agent_unattended says),
putting the task back as it was; a hostile or broken answer and a finish
that can't be written never leave the run unrecorded behind a traceback;
show, list, a start's lines and error lines print no control characters; a write the queue can't make is
an error line; the other commands (new, list, show, claim, release, submit,
reopen) and their exit codes.

Real git repositories live under tmp_path, and so do eVi's home, config and
trust list (never ~/.evi, never the real repository's .evi/tasks).
"""

from __future__ import annotations

import datetime as dt
import json
import os
import shlex
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

import jsonschema  # not skipped when missing: CI has it (the mcp extra)
import pytest
from typer.testing import CliRunner

import evi.tools.fs  # noqa: F401  registers write_file
import evi.tools.shell  # noqa: F401  registers run_command
from evi import events, tasks, trust
from evi import hooks as hooks_mod
from evi import worktree as wt
from evi.config import Config
from evi.llm.agent import Agent
from evi.tools.base import REGISTRY
from tests.conftest import llm_call, llm_text

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")

ROOT = Path(__file__).resolve().parents[1]
PY = Path(sys.executable).as_posix()
# The task's required check: calc.py says x = 2, and the check runs as an
# agent's process (EVI_AGENT=1, evi/proc.py).
CHECK_SCRIPT = """\
import os, pathlib, sys
text = pathlib.Path("calc.py").read_text(encoding="utf-8")
agent = os.environ.get("EVI_" + "AGENT")
print(f"calc.py={text!r} EVI_AGENT={agent}")
sys.exit(0 if text == "x = 2\\n" and agent == "1" else 1)
"""
CHECK = f"{shlex.quote(PY)} check_calc.py"


def _git(cwd: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid",
         "-c", "commit.gpgsign=false", "-c", "init.defaultBranch=main", *args],
        cwd=cwd, capture_output=True, text=True, timeout=60,
    )
    if proc.returncode != 0:
        raise AssertionError(f"git {' '.join(args)}: {proc.stderr}")
    return proc.stdout


@pytest.fixture
def home(monkeypatch, tmp_path):
    """eVi's home, config, transcripts, backends and trust list under
    tmp_path, no safe mode, no agent markers in this process."""
    import evi.config as config_mod
    import evi.sessions as sessions_mod

    evi_home = tmp_path / "evi-home"
    evi_home.mkdir()
    monkeypatch.delenv("EVI_SAFE_MODE", raising=False)
    monkeypatch.delenv("EVI_AGENT", raising=False)
    monkeypatch.delenv("EVI_INTERACTIVE", raising=False)
    monkeypatch.setenv("EVI_HOME", str(evi_home))
    monkeypatch.setattr(config_mod, "HOME", evi_home)
    monkeypatch.setattr(config_mod, "CONFIG_PATH", evi_home / "config.toml")
    monkeypatch.setattr(config_mod, "AGENTS_CONFIG_PATH", evi_home / "agents.toml")
    monkeypatch.setattr(config_mod, "ensure_dirs", lambda: None)
    monkeypatch.setattr(hooks_mod, "HOOKS_CONFIG_PATH", evi_home / "hooks.toml")
    monkeypatch.setattr("evi.guardrails.GUARDRAILS_PATH", evi_home / "guardrails.toml")
    monkeypatch.setattr("evi.plugins.plugin_dirs", lambda root=None: [])
    monkeypatch.setattr(sessions_mod, "TRANSCRIPTS_DIR", evi_home / "transcripts")
    monkeypatch.setattr(sessions_mod, "HANDOFF_DIR", evi_home / "handoff")
    monkeypatch.setattr("evi.backends.registry.BACKENDS_PATH", evi_home / "backends.json")
    (evi_home / "config.toml").write_text(
        "[tools]\nmemory = false\nskills = false\ntranscripts = false\n", encoding="utf-8")
    return evi_home


@pytest.fixture
def repo(home, tmp_path, monkeypatch) -> Path:
    """A git repository on main with calc.py, the check script and eVi's own
    .gitignore; trusted (`evi trust`, on the throwaway trust list); the
    process's folder."""
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "core.autocrlf", "false")
    (root / "calc.py").write_text("x = 1\n", encoding="utf-8")
    (root / "check_calc.py").write_text(CHECK_SCRIPT, encoding="utf-8")
    (root / "EVI.md").write_text("# Project\nthe implementer contract\n", encoding="utf-8")
    shutil.copyfile(ROOT / ".gitignore", root / ".gitignore")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "start")
    trust.grant(root)
    monkeypatch.chdir(root)
    return root.resolve()


def _cli(*args: str):
    import evi.apps.cli.main as cli_main

    return CliRunner().invoke(cli_main.app, list(args))


def _agent(client) -> Agent:
    return Agent(client=client, config=Config.load(),
                 tools=[REGISTRY["write_file"], REGISTRY["run_command"]], transcripts=None)


def _use_agents(monkeypatch, *agents: Agent) -> list[dict]:
    """`_build_agent` hands out `agents` in order; returns the calls' kwargs."""
    import evi.apps.cli.main as cli_main

    queue = list(agents)
    calls: list[dict] = []

    def build(*_a, **kw):
        calls.append({**kw, "cwd": os.getcwd()})
        return queue.pop(0)

    monkeypatch.setattr(cli_main, "_build_agent", build)
    return calls


def _new(*extra: str) -> None:
    res = _cli("task", "new", "--title", "Make x two", "--why", "the answer is two",
               "--acceptance", "calc.py sets x = 2", "--scope", "calc.py", "--check", CHECK,
               *extra)
    assert res.exit_code == 0, res.output


def _record(repo: Path, task_id: str = "T1") -> dict:
    return tomllib.loads((repo / ".evi" / "tasks" / f"{task_id}.toml").read_text(encoding="utf-8"))


def _head(cwd: Path, ref: str = "HEAD") -> str:
    return _git(cwd, "rev-parse", ref).strip()


# ---- filing and handling tasks -----------------------------------------------------


def test_new_list_show_claim_release_submit_reopen(repo):
    _new("--implementer", "evi-local")
    res = _cli("task", "new", "--title", "Second one", "--id", "login-fix")
    assert res.exit_code == 0 and "login-fix" in res.output
    record = _record(repo)
    assert record["status"] == "open" and record["files_in_scope"] == ["calc.py"]
    assert record["required_checks"] == [CHECK] and record["implementer"] == "evi-local"

    listed = json.loads(_cli("task", "list", "--format", "json").stdout)
    assert [(r["id"], r["label"]) for r in listed] == [("T1", "open"), ("login-fix", "open")]
    text = _cli("task", "list").output
    assert "T1" in text and "Make x two" in text and "login-fix" in text

    shown = json.loads(_cli("task", "show", "T1", "--format", "json").stdout)
    assert shown["label"] == "open" and shown["task"]["title"] == "Make x two"
    assert Path(shown["path"]) == repo / ".evi" / "tasks" / "T1.toml"
    text = _cli("task", "show", "T1").output
    for bit in ("Make x two", "the answer is two", "calc.py sets x = 2", "check_calc.py"):
        assert bit in text, text

    res = _cli("task", "claim", "T1", "--holder", "alice", "--ttl", "30")
    assert res.exit_code == 0 and "alice" in res.output
    assert _record(repo)["lease"]["holder"] == "alice"
    res = _cli("task", "claim", "T1", "--holder", "bob")
    assert res.exit_code == 2 and "claimed by alice" in res.output
    res = _cli("task", "release", "T1", "--holder", "bob")
    assert res.exit_code == 2
    assert _cli("task", "release", "T1", "--holder", "alice").exit_code == 0
    assert _record(repo)["status"] == "open"

    res = _cli("task", "submit", "T1")
    assert res.exit_code == 2 and "only an implemented task" in res.output
    res = _cli("task", "reopen", "T1")
    assert res.exit_code == 2
    res = _cli("task", "verify", "T1", "--yes")  # pytest is not director tooling
    assert res.exit_code == 2 and "refusing to verify" in res.output
    assert _record(repo)["status"] == "open"


def test_submit_and_reopen_an_implemented_task(repo):
    _new()
    q = tasks.TaskQueue(repo)
    q.update("T1", lambda r: tasks.finish(tasks.claim(r, holder="me"), holder="me",
                                          implemented=True, stop_reason="done", exit_code=0))
    assert _cli("task", "submit", "T1").exit_code == 0
    assert _record(repo)["status"] == "needs_review"
    res = _cli("task", "reopen", "T1", "--reason", "needs a test")
    assert res.exit_code == 0
    record = _record(repo)
    assert record["status"] == "open" and record["reviewer_notes"][-1]["note"] == "needs a test"


def test_new_from_a_file_and_bad_input(repo, tmp_path):
    spec = tmp_path / "spec.toml"
    spec.write_text('id = "from-file"\ntitle = "From a file"\nacceptance = ["one"]\n'
                    'status = "verified"\nattempts = 9\n[attestation]\nv = 1\n', encoding="utf-8")
    assert _cli("task", "new", "--from", str(spec)).exit_code == 0
    record = _record(repo, "from-file")
    assert record["title"] == "From a file" and record["acceptance"] == ["one"]
    # run bookkeeping and a verdict don't come along: a new task starts open
    assert record["status"] == "open" and record["attempts"] == 0 and "attestation" not in record
    for args, msg in ((["--title", ""], "needs a title"),
                      (["--title", "x", "--id", "bad id"], "not a task id"),
                      (["--title", "x", "--scope", "../out.py"], "files_in_scope"),
                      (["--title", "x", "--id", "FROM-FILE"], "already exists")):
        res = _cli("task", "new", *args)
        assert res.exit_code == 2 and msg in res.output, res.output
    assert _cli("task", "show", "T9").exit_code == 2
    assert _cli("task", "show", "../x").exit_code == 2
    # A required_checks string would be one check per character.
    spec.write_text('title = "t"\nrequired_checks = "pytest"\n', encoding="utf-8")
    res = _cli("task", "new", "--from", str(spec))
    assert res.exit_code == 2 and "required_checks must be a list" in res.output, res.output
    assert tasks.TaskQueue(repo).ids() == ["from-file"]


def test_a_write_the_queue_cant_make_is_an_error_line_not_a_traceback(repo, monkeypatch):
    """os.replace refused for good (on Windows: a reader that keeps the task
    file open): exit 2 with a message, the task as it was."""
    _new()
    before = _record(repo)

    def refused(src, dst):
        raise PermissionError(13, "Access is denied", str(dst))

    monkeypatch.setattr(tasks, "_SHARING_DELAYS", (0,))
    monkeypatch.setattr(tasks.os, "replace", refused)
    res = _cli("task", "claim", "T1", "--holder", "alice")
    monkeypatch.undo()
    assert res.exit_code == 2 and isinstance(res.exception, SystemExit), res.output
    assert "can't write" in res.output and "Access is denied" in res.output
    assert _record(repo) == before


def test_show_and_list_never_print_control_characters_from_a_task_file(repo):
    """The task file is tracked and its last result is the model's: neither
    may move the cursor or repaint the director's terminal."""
    q = tasks.TaskQueue(repo)
    q.create(tasks.new_task("T1", title="evil \x1b[2J\x1b]8;;http://x\x1b\\title",
                            why="line one\nline two \x1b[1A\r"))
    q.update("T1", lambda r: tasks.finish(
        tasks.claim(r, holder="me"), holder="me", implemented=False, stop_reason="error",
        exit_code=1, summary="done \x1b[1A\x1b[2Kstatus: verified\x9b"))
    for args in (("task", "show", "T1"), ("task", "list")):
        res = _cli(*args)
        assert res.exit_code == 0, res.output
        assert not any(c in res.output for c in "\x1b\r\x9b"), repr(res.output)
    shown = _cli("task", "show", "T1").output
    assert "evil [2J]8;;http://x" in shown and "line two [1A" in shown
    assert "status: verified" in shown  # the text stays, only the controls go


def test_error_lines_never_print_control_characters_from_a_task_file(repo):
    """An error quoting the tracked task file (a lease holder, a branch) is
    printed without its control characters, as show and list are."""
    q = tasks.TaskQueue(repo)
    q.create(tasks.new_task("T1", title="t"))
    q.update("T1", lambda r: tasks.claim(r, holder="evil\x1b[2J\x1b]8;;http://x\x1b\\holder"))
    res = _cli("task", "claim", "T1", "--holder", "bob")
    assert res.exit_code == 2 and "claimed by evil[2J]8;;http://x\\holder" in res.output
    assert "\x1b" not in res.output
    q.create(tasks.new_task("T2", title="t"))
    q.update("T2", lambda r: {**r, "status": "needs_review", "branch": "task/\x1b[1AT2",
                              "base": "abc"})
    res = _cli("task", "verify", "T2", "--yes")
    assert res.exit_code == 2 and "\x1b" not in res.output


def test_start_never_prints_control_characters_from_a_task_files_checks(repo, monkeypatch,
                                                                        fake_llm):
    """A start's lines quote the task's required checks (the verify gate's
    and eVi's own run's): printed without the control characters a tracked
    task file may hold, as show and list print them."""
    from evi.config import _to_toml

    _new()
    q = tasks.TaskQueue(repo)
    body = q.read("T1")
    body["required_checks"] = [[PY, "-c", "pass", "\x1b]0;pwned\x07\x1b[2Jtail"]]
    q.path("T1").write_text(_to_toml(body), encoding="utf-8")
    _use_agents(monkeypatch, _agent(fake_llm([
        llm_call(("write_file", {"path": "calc.py", "content": "x = 2\n"})),
        llm_text("Done: calc.x is 2."),
    ])))
    res = _cli("task", "start", "T1")
    assert res.exit_code == 0, res.output
    assert "required check " in res.stderr and "]0;pwned[2Jtail: passed" in res.stderr
    assert "(round 1) passed" in res.stderr  # the verify gate's line
    assert "\x1b" not in res.output and "\x07" not in res.output


def test_an_expired_lease_is_reclaimed_from_the_cli(repo, monkeypatch):
    _new()
    assert _cli("task", "claim", "T1", "--holder", "alice", "--ttl", "10").exit_code == 0
    assert _cli("task", "claim", "T1", "--holder", "bob").exit_code == 2
    later = tasks.now_utc() + dt.timedelta(minutes=11)
    monkeypatch.setattr(tasks, "now_utc", lambda: later)
    res = _cli("task", "claim", "T1", "--holder", "bob")
    assert res.exit_code == 0, res.output
    assert _record(repo)["lease"]["holder"] == "bob"


def test_outside_a_repository_there_is_no_queue(home, tmp_path, monkeypatch):
    folder = tmp_path / "plain"
    folder.mkdir()
    monkeypatch.chdir(folder)
    res = _cli("task", "list")
    assert res.exit_code == 2 and "git repository" in res.output


# ---- evi task start ------------------------------------------------------------------


def test_start_runs_the_task_in_its_worktree_and_leaves_it_implemented(repo, monkeypatch,
                                                                         fake_llm):
    from evi.backends.registry import BackendEntry, add_backend

    add_backend(BackendEntry(name="local", kind="ollama", base_url="http://127.0.0.1:9/v1"))
    _new()
    base = _head(repo)
    client = fake_llm([
        llm_call(("write_file", {"path": "calc.py", "content": "x = 2\n"})),
        llm_text("Done: calc.py sets x = 2; the required check passes."),
    ])
    agent = _agent(client)
    calls = _use_agents(monkeypatch, agent)

    res = _cli("task", "start", "T1", "--backend", "local")

    assert res.exit_code == 0, res.output
    path = repo / ".worktrees" / "task__T1"
    assert calls and Path(calls[0]["cwd"]).resolve() == path.resolve()  # built in the worktree
    assert calls[0]["backend"].name == "local"  # on the saved local backend
    assert Path(os.getcwd()).resolve() == repo  # and the folder put back
    # The task: implemented, with its branch, worktree, base, attempt and result.
    record = _record(repo)
    assert record["status"] == "implemented" and record["lease"] == {}
    assert record["branch"] == "task/T1" and Path(record["worktree"]) == path
    assert record["base"] == base and record["attempts"] == 1 and record["backend"] == "local"
    last = record["last_result"]
    assert last["stop_reason"] == "done" and last["exit_code"] == 0
    assert "calc.py sets x = 2" in last["summary"]
    # Under the implementer policy, with the verify gate running the check.
    assert agent.policy is not None and agent.policy.name == "implementer"
    assert agent.policy.root == path.resolve() and agent.policy.trusted
    assert agent.permission_callback is None
    # The report: under eVi's home, a valid run report with a task section.
    report_file = Path(last["report"])
    assert report_file == tasks.report_path(repo, "T1", 1) and report_file.is_file()
    assert str(report_file).startswith(str(home_of(monkeypatch)))
    report = json.loads(report_file.read_text(encoding="utf-8"))
    jsonschema.Draft202012Validator(events.report_schema()).validate(report)
    assert report["policy"] == "implementer" and report["stop_reason"] == "done"
    assert [c["exit"] for c in report["checks"]] == [0]  # the verify gate's run
    assert report["checks"][0]["source"] == "config"
    task = report["task"]
    assert task["outcome"] == "implemented" and task["attempt"] == 1 and task["base"] == base
    assert [c["exit"] for c in task["required_checks"]] == [0]  # eVi's own run
    assert "EVI_AGENT=1" in task["required_checks"][0]["tail"]
    assert report["worktree"]["branch"] == "task/T1"
    assert report["worktree"]["diffstat"] == {"files": 1, "insertions": 1, "deletions": 1}
    # The work is in the worktree, uncommitted; nothing touched main.
    assert (path / "calc.py").read_text(encoding="utf-8") == "x = 2\n"
    assert (repo / "calc.py").read_text(encoding="utf-8") == "x = 1\n"
    assert _head(repo) == base and _head(repo, "task/T1") == base
    assert _git(repo, "branch", "--show-current").strip() == "main"
    status = _git(repo, "status", "--porcelain", "--untracked-files=all")
    assert status.splitlines() == ["?? .evi/tasks/T1.toml"]
    assert not {e.path: e for e in wt.list_worktrees(repo)}[path].locked  # released
    # The process hosted an agent: it is marked (evi/proc.py).
    assert os.environ.get("EVI_AGENT") == "1"
    # Text output: the hand-off on stdout, the outcome on stderr.
    assert "Done: calc.py sets x = 2" in res.stdout
    assert "implemented (attempt 1)" in res.stderr and "report:" in res.stderr


def home_of(monkeypatch) -> Path:
    import evi.config as config_mod

    return Path(config_mod.HOME)


def test_a_failing_required_check_returns_the_task_to_open(repo, monkeypatch, fake_llm):
    _new()
    client = fake_llm([
        llm_call(("write_file", {"path": "calc.py", "content": "x = 3\n"})),
        llm_text("done"),
        llm_text("done, really"),
    ])
    _use_agents(monkeypatch, _agent(client))
    res = _cli("task", "start", "T1")
    assert res.exit_code == 5, res.output  # checks_failed, as `evi run`
    record = _record(repo)
    assert record["status"] == "open" and record["attempts"] == 1 and record["lease"] == {}
    assert record["last_result"]["stop_reason"] == "checks_failed"
    assert record["last_result"]["exit_code"] == 5
    report = json.loads(Path(record["last_result"]["report"]).read_text(encoding="utf-8"))
    assert report["stop_reason"] == "checks_failed" and report["task"]["outcome"] == "open"
    assert "back to open after attempt 1" in res.stderr


def test_evis_own_run_of_the_required_checks_is_ground_truth(repo, monkeypatch, fake_llm):
    """The model changed nothing (so the verify gate ran nothing) and says it
    is done: eVi's own run of the required check fails, so the task is open."""
    _new()
    _use_agents(monkeypatch, _agent(fake_llm([llm_text("All done, trust me.")])))
    res = _cli("task", "start", "T1")
    assert res.exit_code == 5, res.output
    record = _record(repo)
    assert record["status"] == "open" and record["attempts"] == 1
    assert record["last_result"]["stop_reason"] == "checks_failed"
    assert record["last_result"]["summary"].startswith("required check failed: ")
    report = json.loads(Path(record["last_result"]["report"]).read_text(encoding="utf-8"))
    assert report["stop_reason"] == "done" and report["checks"] == []  # the run's own view
    assert report["task"]["stop_reason"] == "checks_failed"
    assert [c["exit"] for c in report["task"]["required_checks"]] == [1]
    assert "required check" in res.stderr and "exit 1" in res.stderr


def test_a_later_attempt_reuses_the_worktree_and_counts_again(repo, monkeypatch, fake_llm):
    _new()
    base = _head(repo)
    first = _agent(fake_llm([llm_text("nothing to do")]))
    second = _agent(fake_llm([
        llm_call(("write_file", {"path": "calc.py", "content": "x = 2\n"})), llm_text("fixed"),
    ]))
    _use_agents(monkeypatch, first, second)
    assert _cli("task", "start", "T1").exit_code == 5
    _git(repo, "commit", "-q", "--allow-empty", "-m", "main moved on")
    res = _cli("task", "start", "T1")
    assert res.exit_code == 0, res.output
    record = _record(repo)
    assert record["status"] == "implemented" and record["attempts"] == 2
    assert record["base"] == base  # the branch's fork point, not where main is now
    assert Path(record["last_result"]["report"]) == tasks.report_path(repo, "T1", 2)
    assert tasks.report_path(repo, "T1", 1).is_file()
    # an implemented task isn't started again
    res = _cli("task", "start", "T1")
    assert res.exit_code == 2 and "only an open task" in res.output


def test_start_refuses_an_untrusted_repository(repo, monkeypatch, fake_llm):
    _new()
    trust.revoke(repo)
    calls = _use_agents(monkeypatch, _agent(fake_llm([])))
    res = _cli("task", "start", "T1")
    assert res.exit_code == 2 and "evi trust" in res.output
    assert calls == [] and _record(repo)["status"] == "open"
    assert not (repo / ".worktrees").exists()


def test_start_refuses_a_delegate_backend_and_puts_the_task_back(repo, monkeypatch, fake_llm):
    _new()
    before = _record(repo)
    agent = _agent(fake_llm([]))
    agent.config.llm.backend = "codex"
    _use_agents(monkeypatch, agent)
    res = _cli("task", "start", "T1")
    assert res.exit_code == 2 and "outside the implementer policy" in res.output
    assert _record(repo) == before  # as it was: no claim, no attempt
    path = repo / ".worktrees" / "task__T1"
    assert not {e.path: e for e in wt.list_worktrees(repo)}[path].locked


def test_start_refuses_a_delegate_backend_named_on_the_command_line(repo, monkeypatch,
                                                                    fake_llm):
    from evi.backends.registry import BackendEntry, add_backend

    add_backend(BackendEntry(name="cx", kind="codex"))
    _new()
    before = _record(repo)
    calls = _use_agents(monkeypatch, _agent(fake_llm([])))
    res = _cli("task", "start", "T1", "--backend", "cx")
    assert res.exit_code == 2 and "codex" in res.output
    assert calls == [] and _record(repo) == before


def test_start_refuses_a_delegate_models_backend_even_when_unattended_use_is_allowed(
        repo, monkeypatch, fake_llm):
    """allow_cli_agent_unattended lets runs without a policy use a CLI agent;
    a task runs under the implementer policy, which no CLI agent keeps to,
    so a [models] *_backend naming one is refused all the same."""
    _new()
    before = _record(repo)
    agent = _agent(fake_llm([]))
    agent.config.llm.backend = "ollama"
    agent.config.llm.allow_cli_agent_unattended = True
    agent.config.models.vision_backend = "codex"
    _use_agents(monkeypatch, agent)
    res = _cli("task", "start", "T1")
    assert res.exit_code == 2, res.output
    assert "[models] vision_backend = 'codex'" in res.output
    assert "outside the implementer policy" in res.output
    assert _record(repo) == before and agent.policy is None


def test_a_broken_or_hostile_answer_is_recorded_safely(repo, monkeypatch, fake_llm):
    """A lone surrogate (a broken emoji) and terminal escapes in the model's
    answer: the start finishes and records the task (no crash after the run
    leaving it claimed), the summary is printable, the report is JSON."""
    _new()
    answer = "Done \ud83d \x1b[1A\x1b[2K\rstatus: verified\nmore"
    _use_agents(monkeypatch, _agent(fake_llm([
        llm_call(("write_file", {"path": "calc.py", "content": "x = 2\n"})), llm_text(answer),
    ])))
    res = _cli("task", "start", "T1")
    assert res.exit_code == 0, res.output
    assert isinstance(res.exception, SystemExit) or res.exception is None
    raw = (repo / ".evi" / "tasks" / "T1.toml").read_bytes().decode("utf-8")
    record = tomllib.loads(raw)
    assert record["status"] == "implemented" and record["lease"] == {}
    assert record["last_result"]["summary"] == "Done \\ud83d [1A[2Kstatus: verified"
    report = json.loads(Path(record["last_result"]["report"]).read_text(encoding="utf-8"))
    assert report["text"] == answer  # the escape reads back as the same string
    assert "Done \\ud83d" in res.stdout  # the hand-off, its surrogate escaped


def test_a_finish_that_cant_be_written_is_reported_not_lost(repo, monkeypatch, fake_llm):
    """The run is over but its outcome can't be written (on Windows: a reader
    holding the task file the whole time): no traceback, the exit code says
    so, and the note says how to put the task back."""
    _new()
    _use_agents(monkeypatch, _agent(fake_llm([
        llm_call(("write_file", {"path": "calc.py", "content": "x = 2\n"})), llm_text("fixed"),
    ])))
    failing: list[int] = []
    real_replace, real_finish = os.replace, tasks.finish

    def replace(src, dst):
        if failing:
            raise PermissionError(13, "Access is denied", str(dst))
        return real_replace(src, dst)

    def finish(*a, **kw):
        failing.append(1)
        return real_finish(*a, **kw)

    monkeypatch.setattr(tasks, "_SHARING_DELAYS", (0,))
    monkeypatch.setattr(tasks.os, "replace", replace)
    monkeypatch.setattr(tasks, "finish", finish)
    res = _cli("task", "start", "T1")
    assert res.exit_code == 1 and isinstance(res.exception, SystemExit), res.output
    assert "the outcome was not recorded" in res.stderr and "Access is denied" in res.stderr
    assert "`evi task release T1`" in res.stderr
    assert tasks.report_path(repo, "T1", 1).is_file()  # the run's report is there
    assert _record(repo)["status"] == "claimed"
    failing.clear()
    assert _cli("task", "release", "T1").exit_code == 0
    assert _record(repo)["status"] == "open"


def _dead_pid() -> int:
    import psutil

    pid = 4_000_000
    while psutil.pid_exists(pid):
        pid += 4
    return pid


def test_start_takes_over_the_worktree_a_dead_start_left_locked(repo, monkeypatch, fake_llm):
    """A start killed mid-run leaves its claim (until the lease runs out) and
    its worktree locked under its pid. Once the claim is reclaimable the next
    start takes the worktree over too; a live run's lock still refuses, the
    task put back as it was."""
    _new()
    assert _cli("task", "claim", "T1", "--holder", "crashed@here", "--ttl", "1").exit_code == 0
    iso = wt.open_isolated("task/T1", start=repo)  # never released: the start died
    host = wt._host()

    def lock_as(pid: int) -> None:
        _git(repo, "worktree", "unlock", str(iso.path))
        _git(repo, "worktree", "lock", "--reason", f"eVi isolated run (pid {pid} on {host})",
             str(iso.path))

    later = tasks.now_utc() + dt.timedelta(minutes=5)
    monkeypatch.setattr(tasks, "now_utc", lambda: later)
    sleeper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        lock_as(sleeper.pid)  # a run that is still going
        calls = _use_agents(monkeypatch, _agent(fake_llm([])))
        before = _record(repo)
        res = _cli("task", "start", "T1")
        assert res.exit_code == 2 and "can't be locked" in res.output, res.output
        assert calls == [] and _record(repo) == before
    finally:
        sleeper.kill()
        sleeper.wait()
    lock_as(_dead_pid())
    _use_agents(monkeypatch, _agent(fake_llm([
        llm_call(("write_file", {"path": "calc.py", "content": "x = 2\n"})), llm_text("fixed"),
    ])))
    res = _cli("task", "start", "T1")
    assert res.exit_code == 0, res.output
    record = _record(repo)
    assert record["status"] == "implemented" and Path(record["worktree"]) == iso.path
    assert not {e.path: e for e in wt.list_worktrees(repo)}[iso.path].locked


def test_start_refuses_someone_elses_live_claim_unless_forced(repo, monkeypatch, fake_llm):
    _new()
    assert _cli("task", "claim", "T1", "--holder", "someone-else").exit_code == 0
    _use_agents(monkeypatch, _agent(fake_llm([
        llm_call(("write_file", {"path": "calc.py", "content": "x = 2\n"})), llm_text("ok"),
    ])))
    res = _cli("task", "start", "T1")
    assert res.exit_code == 2 and "someone-else" in res.output
    assert not (repo / ".worktrees").exists()
    res = _cli("task", "start", "T1", "--force")
    assert res.exit_code == 0, res.output
    assert _record(repo)["status"] == "implemented"


def test_start_with_a_failing_build_is_a_failed_attempt(repo, monkeypatch):
    import evi.apps.cli.main as cli_main

    _new()

    def broken(*_a, **_k):
        raise RuntimeError("no backend answers")

    monkeypatch.setattr(cli_main, "_build_agent", broken)
    res = _cli("task", "start", "T1")
    assert res.exit_code == 1, res.output
    record = _record(repo)
    assert record["status"] == "open" and record["attempts"] == 1
    assert record["last_result"]["stop_reason"] == "error"
    assert "no backend answers" in record["last_result"]["summary"]


def test_start_refuses_an_unknown_backend(repo):
    _new()
    res = _cli("task", "start", "T1", "--backend", "nope")
    assert res.exit_code == 2 and "no saved backend" in res.output
    assert _record(repo)["status"] == "open"
