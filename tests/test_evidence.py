"""Evidence bundles (Phase 1, loop-evidence-provenance): evi/evidence.py,
`evi run --evidence`, `evi evidence collect|prune` and the bundle `evi task
start` / `submit` record.

Acceptance criteria, each with its test:

1. A task run produces an evidence dir whose report.json validates and whose
   junit counts match pytest —
   test_a_task_run_leaves_a_bundle_whose_report_validates_and_junit_matches_pytest
   (`evi task start` on the shared fake-LLM client, a pytest required check)
   and test_junit_totals_match_what_pytest_printed (passed, failed, skipped
   and errored tests).
4. A secret planted in a transcript fixture is absent from the excerpt —
   test_a_secret_planted_in_the_transcript_is_absent_from_the_bundle (a token
   shape and a known value, in a message and in tool-call arguments; nor in
   the diff or a check's output).

Also: the bundle is written atomically (never into a folder that holds
something else, never half, an older bundle replaced whole), the checks run
only in a trusted folder, `evi run --evidence DIR` writes one whatever the
run's stop_reason (and a bad DIR is a usage error before anything runs),
`evi task submit` collects again and records the summary, `evi evidence
collect` for a plain run, and `evi evidence prune` keeps what a task names.

Review round 1: a secret with XML-special characters is absent from the
junit report too; eVi's --junitxml goes before a ``--``; a task run that
stopped before its checks gets none run, and a Ctrl+C stops eVi's own run
of them; --out / --evidence never write under the evidence root nor over
the bundle another task names; a registered folder reached through a link
is not read; a huge keep_days keeps everything instead of crashing.

Real git repositories live under tmp_path, and so do eVi's home, config,
transcripts and trust list (never ~/.evi).
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tomllib
import xml.etree.ElementTree as ET
from pathlib import Path
from xml.sax.saxutils import escape

import jsonschema  # not skipped when missing: CI has it (the mcp extra)
import pytest
from typer.testing import CliRunner

import evi.tools.fs  # noqa: F401  registers write_file
from evi import attest, events, evidence, tasks, trust
from evi import hooks as hooks_mod
from evi.config import Config
from evi.headless import HeadlessResult
from evi.llm.agent import Agent
from evi.tools.base import REGISTRY
from tests.conftest import llm_call, llm_text

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")

PY = Path(sys.executable).as_posix()
PYTEST = [PY, "-m", "pytest", "-q", "-p", "no:cacheprovider", "test_calc.py"]
# What pytest runs in the task's repository: two pass once calc.py says
# x = 2, one is skipped.
TESTS = """\
import pytest
import calc


def test_x_is_two():
    assert calc.x == 2


def test_y_is_three():
    assert 1 + 2 == 3


@pytest.mark.skip(reason="not today")
def test_skipped():
    pass
"""
# A token shape the redactor knows, and a value only this machine has.
TOKEN = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"
KNOWN = "acme-live-7f3c9a1e5b2d4f60"


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
def home(monkeypatch, tmp_path) -> Path:
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
    monkeypatch.setattr(config_mod, "TRANSCRIPTS_DIR", evi_home / "transcripts")
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
    """A trusted git repository on main with calc.py (x = 1) and its tests;
    the process's folder."""
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "core.autocrlf", "false")
    (root / "calc.py").write_text("x = 1\n", encoding="utf-8")
    (root / "test_calc.py").write_text(TESTS, encoding="utf-8")
    (root / ".gitignore").write_text(".evi/*\n!.evi/tasks/\n.worktrees/\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "start")
    trust.grant(root)
    monkeypatch.chdir(root)
    return root.resolve()


def _cli(*args: str):
    import evi.apps.cli.main as cli_main

    return CliRunner().invoke(cli_main.app, list(args))


def _use_agents(monkeypatch, *agents: Agent) -> None:
    import evi.apps.cli.main as cli_main

    queue = list(agents)
    monkeypatch.setattr(cli_main, "_build_agent", lambda *a, **k: queue.pop(0))


def _agent(client) -> Agent:
    return Agent(client=client, config=Config.load(), tools=[REGISTRY["write_file"]],
                 transcripts=None)


def _record(repo: Path, task_id: str = "T1") -> dict:
    return tomllib.loads((repo / ".evi" / "tasks" / f"{task_id}.toml").read_text(encoding="utf-8"))


def _validate(report: dict) -> None:
    jsonschema.Draft202012Validator(evidence.SCHEMA).validate(report)


def _printed_counts(output: str) -> dict[str, int]:
    """What pytest's closing line says, read here on its own (not with
    evi.evidence's parser): {passed, failed, skipped, error}."""
    line = [ln for ln in output.splitlines() if re.search(r"\bin [\d.]+s\b", ln)][-1]
    counts = {"passed": 0, "failed": 0, "skipped": 0, "error": 0}
    for n, word in re.findall(r"(\d+) (passed|failed|skipped|errors?)", line):
        counts["error" if word.startswith("error") else word] += int(n)
    return counts


def _junit_file(path: Path) -> dict[str, int]:
    suite = ET.parse(path).getroot()
    suite = suite if suite.tag == "testsuite" else suite.find("testsuite")
    return {k: int(suite.get(k)) for k in ("tests", "failures", "errors", "skipped")}


def _every_byte(folder: Path) -> bytes:
    return b"".join(p.read_bytes() for p in sorted(folder.rglob("*")) if p.is_file())


# ---- acceptance 1: a task run's bundle ------------------------------------------------


def test_a_task_run_leaves_a_bundle_whose_report_validates_and_junit_matches_pytest(
        repo, home, monkeypatch, fake_llm):
    res = _cli("task", "new", "--title", "Make x two", "--why", "the answer is two",
               "--acceptance", "calc.x == 2", "--scope", "calc.py",
               "--check", shlex.join(PYTEST))
    assert res.exit_code == 0, res.output
    _use_agents(monkeypatch, _agent(fake_llm([
        llm_call(("write_file", {"path": "calc.py", "content": "x = 2\n"})),
        llm_text("Done: calc.x is 2."),
    ])))
    res = _cli("task", "start", "T1")
    assert res.exit_code == 0, res.output
    record = _record(repo)
    assert record["status"] == "implemented"
    bundle = Path(record["evidence"])
    # Under eVi's home, per repository, task and attempt; the repo has no bundle.
    assert bundle == evidence.task_bundle_path(repo, "T1", 1) == (
        home / "evidence" / evidence.repo_key(repo) / "T1" / "1")
    assert evidence.is_bundle(bundle)
    assert _git(repo, "status", "--porcelain", "--untracked-files=all").splitlines() == [
        "?? .evi/tasks/T1.toml"]
    report = json.loads((bundle / "report.json").read_text(encoding="utf-8"))
    _validate(report)
    jsonschema.Draft202012Validator(events.report_schema()).validate(report["run"])
    assert report["run"]["stop_reason"] == "done" and report["run"]["task"]["id"] == "T1"
    assert report["task"] == {"id": "T1", "title": "Make x two", "attempt": 1,
                              "branch": "task/T1", "status": "claimed"}
    env = report["environment"]
    assert env["policy"] == "implementer" and env["trusted"] is True
    assert env["branch"] == "task/T1" and env["base"] == record["base"]
    assert env["python"] and env["platform"] and env["evi_version"]
    # The diff: calc.py against the task's base, uncommitted in the worktree.
    assert report["diff"]["files"] == 1 and report["diff"]["paths"] == [
        {"path": "calc.py", "added": 1, "deleted": 1}]
    patch = (bundle / "diff.patch").read_text(encoding="utf-8")
    assert "-x = 1" in patch and "+x = 2" in patch
    assert "calc.py | +1 -1" in (bundle / "diffstat.txt").read_text(encoding="utf-8")
    # eVi's own run of the required pytest check, with the junit report it asked for.
    [check] = report["checks"]
    assert check["ok"] and check["exit"] == 0 and check["source"] == "required"
    assert check["cmd"] and "--junitxml" not in check["cmd"]
    output = (bundle / check["output"]).read_text(encoding="utf-8")
    junit_path = bundle / check["junit_file"]
    printed = _printed_counts(output)
    assert printed == {"passed": 2, "failed": 0, "skipped": 1, "error": 0}
    in_file = _junit_file(junit_path)
    assert in_file == {"tests": 3, "failures": 0, "errors": 0, "skipped": 1}
    assert check["junit"] == {**in_file, "passed": 2} == report["junit"]
    assert check["reported"]["passed"] == printed["passed"]
    assert sorted(report["files"]) == sorted(
        p.relative_to(bundle).as_posix() for p in bundle.rglob("*") if p.is_file())
    # The task records the bundle's path and summary (the bundle stays out of git).
    assert record["evidence_summary"] == {
        "hash": attest.evidence_hash(bundle), "files": 1, "insertions": 1, "deletions": 1,
        "checks_passed": 1, "checks_failed": 0,
        "junit": {"tests": 3, "failures": 0, "errors": 0, "skipped": 1},
    }
    assert "evidence:" in res.stderr
    shown = _cli("task", "show", "T1").output
    assert "evidence summary" in shown and "junit 3 tests" in shown


def test_a_bundle_that_cant_be_collected_never_loses_the_run(repo, monkeypatch, fake_llm):
    """The run is recorded all the same, the note says why, and an earlier
    attempt's bundle is no longer named as this attempt's evidence."""
    res = _cli("task", "new", "--title", "Make x two", "--scope", "calc.py",
               "--check", shlex.join(PYTEST))
    assert res.exit_code == 0, res.output
    earlier = evidence.task_bundle_path(repo, "T1", 1)
    earlier.mkdir(parents=True)
    (earlier / "report.json").write_text("{}\n", encoding="utf-8")
    tasks.TaskQueue(repo).update("T1", lambda r: {
        **r, "attempts": 1, "evidence": str(earlier), "evidence_summary": {"hash": "x"}})

    def broken(*_a, **_k):
        raise RuntimeError("disk full")

    monkeypatch.setattr(evidence, "collect_task", broken)
    _use_agents(monkeypatch, _agent(fake_llm([
        llm_call(("write_file", {"path": "calc.py", "content": "x = 2\n"})), llm_text("done"),
    ])))
    res = _cli("task", "start", "T1")
    assert res.exit_code == 0, res.output
    record = _record(repo)
    assert record["status"] == "implemented" and record["attempts"] == 2
    assert record["evidence"] == "" and record["evidence_summary"] == {}
    assert "evidence was not collected: RuntimeError: disk full" in res.stderr


def test_junit_totals_match_what_pytest_printed(repo):
    (repo / "test_mixed.py").write_text(
        "import pytest\n\n\n@pytest.fixture\ndef broken():\n    raise RuntimeError('setup')\n\n\n"
        "def test_a():\n    pass\n\n\ndef test_b():\n    pass\n\n\ndef test_c():\n"
        "    assert False\n\n\n@pytest.mark.skip\ndef test_d():\n    pass\n\n\n"
        "def test_e(broken):\n    pass\n", encoding="utf-8")
    base = _git(repo, "rev-parse", "HEAD").strip()
    out = evidence.task_bundle_path(repo, "T7", 1)
    bundle = evidence.collect(out, folder=repo, base=base, repo=repo, checks=[
        [PY, "-m", "pytest", "-q", "-p", "no:cacheprovider", "test_mixed.py"]])
    _validate(bundle.report)
    [check] = bundle.report["checks"]
    assert check["exit"] == 1 and not check["ok"]
    printed = _printed_counts((bundle.path / check["output"]).read_text(encoding="utf-8"))
    assert printed == {"passed": 2, "failed": 1, "skipped": 1, "error": 1}
    assert check["junit"] == {"tests": 5, "failures": printed["failed"],
                              "errors": printed["error"], "skipped": printed["skipped"],
                              "passed": printed["passed"]}
    assert _junit_file(bundle.path / check["junit_file"]) == {
        "tests": 5, "failures": 1, "errors": 1, "skipped": 1}
    assert bundle.summary["checks_failed"] == 1 and bundle.summary["junit"]["errors"] == 1
    # The untracked test file is in the patch as a new file.
    assert bundle.report["diff"]["untracked"] == 1
    assert "+++ b/test_mixed.py" in (bundle.path / "diff.patch").read_text(encoding="utf-8")


# ---- acceptance 4: nothing secret in a bundle ----------------------------------------


def test_a_secret_planted_in_the_transcript_is_absent_from_the_bundle(repo, home, monkeypatch):
    monkeypatch.setenv("ACME_SERVICE_TOKEN", KNOWN)
    from evi import redact

    redact.refresh()
    day = home / "transcripts" / "2026-10-17"
    day.mkdir(parents=True)
    transcript = day / "s1.jsonl"
    lines = [{"session": "s1", "ts": float(n), "role": "user", "content": f"filler {n}"}
             for n in range(100)]
    lines += [
        {"session": "s1", "ts": 101.0, "role": "user",
         "content": f"deploy with {TOKEN} and {KNOWN} please"},
        {"session": "s1", "ts": 102.0, "role": "assistant", "content": "on it",
         "tool_calls": [{"id": "c1", "type": "function", "function": {
             "name": "run_command", "arguments": json.dumps({"command": f"deploy --key {KNOWN}"})}}]},
        {"session": "s1", "ts": 103.0, "role": "tool", "tool_name": "run_command",
         "tool_call_id": "c1", "content": f"auth ok for {TOKEN}"},
        {"session": "s1", "ts": 104.0, "type": "compaction", "role": "system", "content": "",
         "summary": "meta"},
    ]
    transcript.write_text("".join(json.dumps(x) + "\n" for x in lines), encoding="utf-8")
    # ... and in the work and in a check's output too.
    (repo / "calc.py").write_text(f"x = 2\nKEY = '{KNOWN}'\n", encoding="utf-8")
    run = HeadlessResult(stop_reason="done", text=f"used {TOKEN}", transcript_path=str(transcript),
                         max_turns=3).to_dict()
    gate_check = {"name": "ruff", "cmd": "ruff check calc.py", "exit": 1, "duration_ms": 5,
                  "tail": f"calc.py:2 found {KNOWN}", "round": 1, "source": "pyproject"}
    bundle = evidence.collect(
        evidence.run_bundle_path(repo), folder=repo, base="HEAD", run_report=run, repo=repo,
        checks=[[PY, "-c", f"print('token {TOKEN}')"]], check_records=[gate_check])
    own, gate = bundle.report["checks"]
    assert own["source"] == "required" and own["ok"] and own["junit"] is None
    assert gate["source"] == "run" and gate["exit"] == 1 and not gate["ok"]
    assert "found [REDACTED:" in (bundle.path / gate["output"]).read_text(encoding="utf-8")
    excerpt = (bundle.path / "transcript.md").read_text(encoding="utf-8")
    assert "deploy with [REDACTED:" in excerpt and "auth ok for [REDACTED:" in excerpt
    assert "filler 99" in excerpt and "filler 10\n" not in excerpt  # the end, bounded
    assert "tool call run_command" in excerpt
    everything = _every_byte(bundle.path)
    for secret in (TOKEN, KNOWN):
        assert secret.encode() not in everything
        assert secret[:16].encode() not in everything  # no prefix of it either
    report = bundle.report
    _validate(report)
    assert report["transcript"]["messages"] == 103 and report["transcript"]["shown"] == (
        evidence.TRANSCRIPT_MESSAGES)
    assert report["transcript"]["session_id"] == "s1"
    assert sum(report["redactions"].values()) >= 5


def test_a_transcript_outside_evis_home_is_not_read(repo, tmp_path):
    elsewhere = tmp_path / "notes.jsonl"
    elsewhere.write_text(json.dumps({"role": "user", "content": "private"}) + "\n",
                         encoding="utf-8")
    assert evidence.transcript_excerpt(elsewhere) is None
    run = HeadlessResult(stop_reason="done", transcript_path=str(elsewhere)).to_dict()
    bundle = evidence.collect(evidence.run_bundle_path(repo), folder=repo, base="HEAD",
                              run_report=run)
    assert bundle.report["transcript"] is None and not (bundle.path / "transcript.md").exists()


# ---- writing ---------------------------------------------------------------------------


def test_a_bundle_is_written_whole_or_not_at_all(repo, monkeypatch, tmp_path):
    # Never into a folder that holds something else, nor a file.
    busy = tmp_path / "busy"
    busy.mkdir()
    (busy / "mine.txt").write_text("keep\n", encoding="utf-8")
    for target in (busy, busy / "mine.txt"):
        with pytest.raises(evidence.EvidenceError):
            evidence.collect(target, folder=repo, base="HEAD")
    assert sorted(p.name for p in busy.iterdir()) == ["mine.txt"]
    # An empty folder takes one; a second collect replaces it whole.
    out = tmp_path / "bundle"
    out.mkdir()
    first = evidence.collect(out, folder=repo, base="HEAD")
    (out / "stale.txt").write_text("from before\n", encoding="utf-8")
    (repo / "calc.py").write_text("x = 3\n", encoding="utf-8")
    second = evidence.collect(out, folder=repo, base="HEAD")
    assert not (out / "stale.txt").exists() and first.hash != second.hash
    assert second.report["diff"]["files"] == 1
    # A failure while writing leaves the older bundle as it was, and no
    # half-written folder beside it.
    before = _every_byte(out)

    def broken(path, data):
        raise OSError("disk full")

    monkeypatch.setattr(evidence, "_write_json", broken)
    with pytest.raises(OSError, match="disk full"):
        evidence.collect(out, folder=repo, base="HEAD")
    assert _every_byte(out) == before
    assert sorted(p.name for p in tmp_path.iterdir() if p.name.startswith(".")) == []


def test_checks_run_only_in_a_trusted_folder(repo, tmp_path):
    marker = tmp_path / "ran.txt"
    trust.revoke(repo)
    bundle = evidence.collect(evidence.run_bundle_path(repo), folder=repo, base="HEAD",
                              checks=[[PY, "-c", f"open({str(marker)!r}, 'w').write('x')"]])
    assert not marker.exists()
    assert bundle.report["checks"] == [] and "not a trusted folder" in (
        bundle.report["checks_skipped"])
    assert bundle.summary["checks_skipped"]
    _validate(bundle.report)


def test_a_work_tree_that_isnt_the_repositorys_own_is_not_diffed(repo, tmp_path):
    """A repository whose config points git at another folder (core.worktree)
    would list every file there: its diff is refused, with a note."""
    other = tmp_path / "home-like"
    other.mkdir()
    (other / "secret.txt").write_text("private\n", encoding="utf-8")
    _git(repo, "config", "core.worktree", str(other))
    info = evidence.collect_diff(repo, "HEAD")
    assert info.paths == [] and info.patch == "" and "not a git work tree of its own" in info.note


# ---- evi run --evidence -----------------------------------------------------------------


def test_evi_run_writes_a_bundle_whatever_the_stop_reason(repo, monkeypatch, fake_llm, tmp_path):
    import evi.apps.cli.main as cli_main

    out = tmp_path / "run-evidence"
    agent = _agent(fake_llm([
        llm_call(("write_file", {"path": "notes.txt", "content": "hello\n"})),
        llm_text("wrote notes.txt"),
    ]))
    monkeypatch.setattr(cli_main, "_build_agent", lambda *a, **k: agent)
    res = CliRunner().invoke(cli_main.app, ["run", "-y", "--format", "json", "--evidence",
                                            str(out), "write notes"])
    assert res.exit_code == 0, res.output
    run = json.loads(res.stdout)
    report = json.loads((out / "report.json").read_text(encoding="utf-8"))
    _validate(report)
    assert report["run"]["stop_reason"] == "done" and report["run"]["text"] == run["text"]
    assert report["task"] is None and report["diff"]["paths"] == [
        {"path": "notes.txt", "added": 1, "deleted": 0}]
    assert f"evidence: {out}" in res.stderr
    # Recorded, so a task may name it as its evidence.
    assert str(out) in evidence.registered()
    assert tasks.evidence_path({"id": "T1", "evidence": str(out)}, repo) == str(out)
    # A run that errors still leaves one (here over the older bundle).
    monkeypatch.setattr(cli_main, "_build_agent",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no backend")))
    res = CliRunner().invoke(cli_main.app, ["run", "--evidence", str(out), "again"])
    assert res.exit_code == 1, res.output
    report = json.loads((out / "report.json").read_text(encoding="utf-8"))
    assert report["run"]["stop_reason"] == "error" and "no backend" in report["run"]["error"]


def test_evi_run_refuses_an_evidence_folder_that_holds_something_else(repo, monkeypatch,
                                                                      tmp_path):
    import evi.apps.cli.main as cli_main

    busy = tmp_path / "busy"
    busy.mkdir()
    (busy / "keep.txt").write_text("mine\n", encoding="utf-8")
    built: list[int] = []
    monkeypatch.setattr(cli_main, "_build_agent", lambda *a, **k: built.append(1))
    res = CliRunner().invoke(cli_main.app, ["run", "--evidence", str(busy), "hi"])
    assert res.exit_code == 2 and "--evidence" in res.output and built == []
    assert [p.name for p in busy.iterdir()] == ["keep.txt"]


# ---- evi task submit and evi evidence collect ------------------------------------------


def _started(repo: Path) -> None:
    """Task T1 started once (its worktree and branch), its work committed
    there, and implemented."""
    from evi import worktree as wt

    q = tasks.TaskQueue(repo)
    q.create(tasks.new_task("T1", title="Make x two", acceptance=["calc.x == 2"],
                            files_in_scope=["calc.py"], required_checks=[PYTEST]))
    q.update("T1", lambda r: tasks.claim(r, holder="evi"))
    iso = wt.open_isolated("task/T1", start=repo)
    (iso.path / "calc.py").write_text("x = 2\n", encoding="utf-8")
    _git(iso.path, "commit", "-q", "-am", "make x two")
    wt.release(iso)
    q.update("T1", lambda r: tasks.finish(
        r, holder="evi", implemented=True, stop_reason="done", exit_code=0,
        fields={"worktree": str(iso.path), "branch": iso.branch, "base": iso.base,
                "backend": "ollama", "model": "qwen3"}))


def test_submit_collects_the_evidence_again_and_records_its_summary(repo):
    _started(repo)
    res = _cli("task", "submit", "T1")
    assert res.exit_code == 0, res.output
    record = _record(repo)
    assert record["status"] == "needs_review"
    bundle = Path(record["evidence"])
    assert bundle == evidence.task_bundle_path(repo, "T1", 1)
    summary = record["evidence_summary"]
    assert summary["hash"] == attest.evidence_hash(bundle)
    assert summary["files"] == 1 and summary["checks_passed"] == 1
    assert summary["junit"] == {"tests": 3, "failures": 0, "errors": 0, "skipped": 1}
    assert "evidence:" in res.stderr


def test_submit_without_a_worktree_keeps_the_task_and_says_why(repo):
    _started(repo)
    _git(repo, "worktree", "remove", "--force", _record(repo)["worktree"])
    res = _cli("task", "submit", "T1")
    assert res.exit_code == 0, res.output
    assert "not collected again" in res.stderr and _record(repo)["status"] == "needs_review"
    assert not _record(repo).get("evidence")


def test_collect_for_a_task_and_for_a_plain_run(repo, tmp_path):
    _started(repo)
    res = _cli("evidence", "collect", "T1", "--format", "json")
    assert res.exit_code == 0, res.output
    shown = json.loads(res.stdout)
    assert Path(shown["path"]) == evidence.task_bundle_path(repo, "T1", 1)
    assert _record(repo)["evidence_summary"] == shown["summary"]
    # A plain run: a work tree and its base, a check of your own.
    base = _git(repo, "rev-parse", "HEAD").strip()
    (repo / "calc.py").write_text("x = 2\n", encoding="utf-8")
    res = _cli("evidence", "collect", "--worktree", str(repo), "--base", base,
               "--check", shlex.join(PYTEST), "--format", "json")
    assert res.exit_code == 0, res.output
    shown = json.loads(res.stdout)
    path = Path(shown["path"])
    assert path.parent == evidence.repo_folder(repo) / evidence.RUNS
    assert shown["summary"]["junit"]["tests"] == 3
    report = json.loads((path / "report.json").read_text(encoding="utf-8"))
    assert {p["path"] for p in report["diff"]["paths"]} == {"calc.py", ".evi/tasks/T1.toml"}
    # Usage: a task id or --worktree, not both, not neither; a claimed task is
    # its start's to collect.
    assert _cli("evidence", "collect").exit_code == 2
    assert _cli("evidence", "collect", "T1", "--worktree", str(repo)).exit_code == 2
    assert _cli("evidence", "collect", "--worktree", str(repo)).exit_code == 2  # no --base
    tasks.TaskQueue(repo).update("T1", lambda r: tasks.claim(tasks.reopen(r), holder="x"))
    res = _cli("evidence", "collect", "T1")
    assert res.exit_code == 2 and "claimed" in res.output


# ---- retention ---------------------------------------------------------------------------


def _bundle_at(path: Path, created: str) -> Path:
    path.mkdir(parents=True)
    (path / "report.json").write_text(json.dumps({"created": created}), encoding="utf-8")
    return path


def test_prune_keeps_what_a_task_names_and_what_is_recent(repo, home):
    old, new = "2020-01-01T00:00:00Z", evidence._stamp(evidence.now_utc())
    named = _bundle_at(evidence.task_bundle_path(repo, "T1", 1), old)
    superseded = _bundle_at(evidence.task_bundle_path(repo, "T1", 2), old)
    recent = _bundle_at(evidence.task_bundle_path(repo, "T2", 1), new)
    plain = _bundle_at(evidence.run_bundle_path(repo), old)
    unreadable = _bundle_at(evidence.task_bundle_path(repo, "T5", 1), old)
    leftover = evidence.task_bundle_path(repo, "T1", 1).with_name(".3.tmp-0a1b2c3d")
    leftover.mkdir()
    os.utime(leftover, (0, 0))
    other_repo = _bundle_at(evidence.task_bundle_path(repo.parent / "elsewhere", "T1", 1), old)
    q = tasks.TaskQueue(repo)
    q.create(tasks.new_task("T1", title="one"))
    q.update("T1", lambda r: {**r, "evidence": str(named)})
    q.folder.joinpath("T5.toml").write_text("this is not toml [", encoding="utf-8")

    res = _cli("evidence", "prune", "--dry-run")
    assert res.exit_code == 0, res.output
    assert "would remove" in res.output and all(
        p.exists() for p in (named, superseded, recent, plain, unreadable, leftover))
    res = _cli("evidence", "prune")
    assert res.exit_code == 0, res.output
    assert named.exists() and recent.exists() and unreadable.exists()
    assert not superseded.exists() and not plain.exists() and not leftover.exists()
    assert other_repo.exists()  # another repository's bundles are not this one's to prune
    assert "3 bundle(s) removed" in res.output
    # keep_days comes from [evidence]; --keep-days 0 keeps only what a task names.
    assert evidence.keep_days(Config.load()) == 30
    res = _cli("evidence", "prune", "--keep-days", "0")
    assert res.exit_code == 0 and not recent.exists() and named.exists()


def test_a_task_naming_the_repository_never_makes_list_hash_it(repo, home, monkeypatch):
    """A forged task file naming "." (or .git) as its evidence, with an
    attestation so list and show look at it: nothing is hashed."""
    hashed: list[str] = []
    monkeypatch.setattr(attest, "evidence_hash", lambda p: hashed.append(str(p)) or "")
    q = tasks.TaskQueue(repo)
    for task_id, path in (("T1", "."), ("T2", ".git"), ("T3", str(repo))):
        q.create(tasks.new_task(task_id, title="forged"))
        body = {**q.read(task_id), "status": "verified", "evidence": path}
        body["attestation"] = {"v": 1, "task": task_id, "digest": "x", "head": "h",
                               "evidence": "", "verdict": "verified", "rounds": 1, "review": {},
                               "signer": "d", "signed_at": "2026-10-17T00:00:00Z",
                               "key": "0" * 16, "sig": "0" * 64}
        from evi.config import _to_toml

        q.folder.joinpath(f"{task_id}.toml").write_text(_to_toml(body), encoding="utf-8")
    res = _cli("task", "list", "--format", "json")
    assert res.exit_code == 0, res.output
    assert all(r["label"].startswith("unverified") for r in json.loads(res.stdout))
    assert _cli("task", "show", "T1").exit_code == 0
    assert hashed == []


# ---- review round 1 -----------------------------------------------------------------------


def test_a_secret_with_xml_special_characters_is_absent_from_the_junit_report(repo, monkeypatch):
    """pytest writes captured output and failure messages XML-escaped, so a
    known value with & < > " in it reads differently in the junit report:
    the report is redacted as the XML it is."""
    secret = 'Tr0ub4dor&3<horse>"battery-staple'
    monkeypatch.setenv("ACME_DB_PASSWORD", secret)
    from evi import redact

    redact.refresh()
    # Printed (captured output) and the failure's message (an attribute).
    (repo / "test_db.py").write_text(
        "import os\n\n\ndef test_db():\n    pw = os.environ['ACME_DB_PASSWORD']\n"
        "    print('connecting with', pw)\n    assert False, pw\n", encoding="utf-8")
    bundle = evidence.collect(evidence.run_bundle_path(repo), folder=repo, base="HEAD", repo=repo,
                              checks=[[PY, "-m", "pytest", "-q", "-p", "no:cacheprovider",
                                       "test_db.py"]])
    [check] = bundle.report["checks"]
    assert check["exit"] == 1 and check["junit"] == {
        "tests": 1, "failures": 1, "errors": 0, "skipped": 0, "passed": 0}
    junit_path = bundle.path / check["junit_file"]
    junit = junit_path.read_text(encoding="utf-8")
    assert "[REDACTED:ACME_DB_PASSWORD]" in junit and "connecting with" in junit
    assert _junit_file(junit_path) == {"tests": 1, "failures": 1, "errors": 0, "skipped": 0}
    everything = _every_byte(bundle.path)
    for form in (secret, escape(secret), escape(secret, {'"': "&quot;"}), "Tr0ub4dor&amp;3"):
        assert form.encode() not in everything, form
    _validate(bundle.report)


def test_a_junit_report_that_cant_be_redacted_is_not_kept(tmp_path):
    from evi.redact import Redactor

    broken = tmp_path / "01-python.junit.xml"
    broken.write_text("<testsuites><testsuite tests='1'>secret &bogus; <", encoding="utf-8")
    assert evidence._redact_junit(broken, Redactor()) is None and not broken.exists()


def test_the_junit_flag_goes_before_a_double_dash(repo):
    """Past a ``--`` pytest reads every word as a path: eVi's --junitxml goes
    before it, else last, after the check's own flags."""
    j = "out.xml"
    assert evidence.junit_argv([PY, "-m", "pytest", "-q", "--", "t.py"], j) == [
        PY, "-m", "pytest", "-q", f"--junitxml={j}", "--", "t.py"]
    assert evidence.junit_argv(["pytest", "--junitxml=mine.xml", "-q"], j) == [
        "pytest", "--junitxml=mine.xml", "-q", f"--junitxml={j}"]
    assert evidence.junit_argv(["uv", "run", "pytest", "--", "a"], j)[3] == f"--junitxml={j}"
    (repo / "calc.py").write_text("x = 2\n", encoding="utf-8")
    argv = [PY, "-m", "pytest", "-q", "-p", "no:cacheprovider", "--", "test_calc.py"]
    bundle = evidence.collect(evidence.run_bundle_path(repo), folder=repo, base="HEAD",
                              repo=repo, checks=[argv])
    [check] = bundle.report["checks"]
    assert check["ok"] and check["junit"]["tests"] == 3 and check["junit"]["passed"] == 2


@pytest.mark.parametrize("stop, raised", [("error", RuntimeError("backend unreachable")),
                                          ("interrupted", KeyboardInterrupt())])
def test_a_run_that_stopped_before_its_checks_gets_none_run(repo, monkeypatch, tmp_path,
                                                            stop, raised):
    """An errored or interrupted start never got to its checks: the bundle
    runs none either (a test suite's worth of time, and a passing check for
    a run that did nothing), and says why."""
    import evi.apps.cli.main as cli_main

    marker = tmp_path / "ran.txt"
    check = [PY, "-c", f"open({str(marker)!r}, 'w').write('x')"]
    res = _cli("task", "new", "--title", "Make x two", "--scope", "calc.py",
               "--check", shlex.join(check))
    assert res.exit_code == 0, res.output

    def build(*_a, **_k):
        raise raised

    monkeypatch.setattr(cli_main, "_build_agent", build)
    res = _cli("task", "start", "T1")
    assert res.exit_code == (1 if stop == "error" else 130), res.output
    assert not marker.exists()
    record = _record(repo)
    assert record["last_result"]["stop_reason"] == stop and record["status"] == "open"
    report = json.loads((Path(record["evidence"]) / "report.json").read_text(encoding="utf-8"))
    _validate(report)
    assert report["checks"] == [] and f"the run stopped ({stop})" in report["checks_skipped"]
    assert record["evidence_summary"]["checks_passed"] == 0
    assert record["evidence_summary"]["checks_skipped"] == report["checks_skipped"]


def test_a_cancel_stops_evis_own_run_of_the_checks(repo, tmp_path):
    import threading
    import time

    marker = tmp_path / "second.txt"
    began = tmp_path / "first-started.txt"
    token = threading.Event()

    # Cancel once the first check is running (a fixed timer raced collect's own
    # git and transcript steps on a loaded machine: the token could be set
    # before any check started, which is a different case, tested below).
    def cancel_when_running():
        deadline = time.monotonic() + 120
        while not began.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        token.set()

    threading.Thread(target=cancel_when_running, daemon=True).start()
    first_check = (f"import pathlib, time; pathlib.Path({str(began)!r}).write_text('on'); "
                   "time.sleep(60)")
    bundle = evidence.collect(
        evidence.run_bundle_path(repo), folder=repo, base="HEAD", repo=repo, cancel_token=token,
        checks=[[PY, "-c", first_check],
                [PY, "-c", f"open({str(marker)!r}, 'w').write('x')"]])
    assert began.exists()
    [first] = bundle.report["checks"]
    assert first["note"] == "cancelled" and not first["ok"] and not marker.exists()
    assert first["duration_ms"] < 30000  # stopped, not run out
    assert bundle.report["checks_skipped"] == "cancelled: eVi stopped running the checks after 1 of 2"
    _validate(bundle.report)


def test_a_cancel_before_the_checks_runs_none_of_them(repo, tmp_path):
    import threading

    marker = tmp_path / "ran.txt"
    token = threading.Event()
    token.set()
    bundle = evidence.collect(
        evidence.run_bundle_path(repo), folder=repo, base="HEAD", repo=repo, cancel_token=token,
        checks=[[PY, "-c", f"open({str(marker)!r}, 'w').write('x')"]])
    assert bundle.report["checks"] == [] and not marker.exists()
    assert bundle.report["checks_skipped"] == "cancelled: eVi stopped running the checks after 0 of 1"
    _validate(bundle.report)


def test_ctrl_c_during_a_starts_evidence_stops_its_checks(repo, monkeypatch, fake_llm):
    """`evi task start` collects the evidence with Ctrl+C wired to a cancel:
    a first press stops the check running, a second stops the collection
    (the run is recorded all the same), and Ctrl+C is as it was after."""
    import signal

    res = _cli("task", "new", "--title", "Make x two", "--scope", "calc.py",
               "--check", shlex.join(PYTEST))
    assert res.exit_code == 0, res.output
    seen: dict = {}

    def collect_task(root, record, **kw):
        handler = signal.getsignal(signal.SIGINT)
        handler(signal.SIGINT, None)
        seen["first"] = kw["cancel_token"].is_set()
        seen["run_stop"] = kw["run_stop"]
        handler(signal.SIGINT, None)  # the second press

    monkeypatch.setattr(evidence, "collect_task", collect_task)
    before = signal.getsignal(signal.SIGINT)
    _use_agents(monkeypatch, _agent(fake_llm([
        llm_call(("write_file", {"path": "calc.py", "content": "x = 2\n"})), llm_text("done"),
    ])))
    res = _cli("task", "start", "T1")
    assert res.exit_code == 0, res.output
    assert seen == {"first": True, "run_stop": "done"}
    assert "evidence was not collected: RuntimeError: interrupted" in res.stderr
    assert _record(repo)["status"] == "implemented"
    assert signal.getsignal(signal.SIGINT) is before


def test_out_and_evidence_never_write_over_evis_or_a_tasks_bundle(repo, home, monkeypatch,
                                                                   tmp_path):
    import evi.apps.cli.main as cli_main

    _started(repo)
    assert _cli("evidence", "collect", "T1").exit_code == 0
    named = Path(_record(repo)["evidence"])
    digest = attest.evidence_hash(named)
    # Nothing under the evidence root: eVi files its own bundles there.
    for target in (named, home / "evidence" / "mine"):
        res = _cli("evidence", "collect", "--worktree", str(repo), "--base", "HEAD",
                   "--out", str(target))
        assert res.exit_code == 2 and "where eVi files its own bundles" in res.output
        res = _cli("evidence", "collect", "T1", "--out", str(target))
        assert res.exit_code == 2 and "where eVi files its own bundles" in res.output
    assert not (home / "evidence" / "mine").exists()
    assert attest.evidence_hash(named) == digest and _record(repo)["evidence"] == str(named)
    # A folder of your own the task then names: only the task collects it again.
    mine = tmp_path / "mine"
    res = _cli("evidence", "collect", "T1", "--out", str(mine))
    assert res.exit_code == 0, res.output
    assert _record(repo)["evidence"] == str(mine)
    assert tasks.evidence_path(_record(repo), repo) == str(mine)
    digest = attest.evidence_hash(mine)
    res = _cli("evidence", "collect", "--worktree", str(repo), "--base", "HEAD",
               "--out", str(mine))
    assert res.exit_code == 2 and "task T1's evidence bundle" in res.output
    built: list[int] = []
    monkeypatch.setattr(cli_main, "_build_agent", lambda *a, **k: built.append(1))
    res = _cli("run", "--evidence", str(mine), "hi")
    assert res.exit_code == 2 and "task T1's evidence bundle" in res.output and built == []
    assert attest.evidence_hash(mine) == digest
    res = _cli("evidence", "collect", "T1", "--out", str(mine))
    assert res.exit_code == 0, res.output


def _link(link: Path, target: Path) -> None:
    if sys.platform == "win32":
        import _winapi

        _winapi.CreateJunction(str(target), str(link))
    else:
        try:
            link.symlink_to(target, target_is_directory=True)
        except OSError as exc:
            pytest.skip(f"can't make a link here: {exc}")


def test_a_registered_folder_reached_through_a_link_is_not_read(repo, tmp_path):
    out = tmp_path / "a" / "b"
    evidence.collect(out, folder=repo, base="HEAD")
    assert tasks.evidence_path({"id": "T1", "evidence": str(out)}, repo) == str(out)
    # The folder on the way swapped for a link to another "bundle".
    shutil.rmtree(tmp_path / "a")
    elsewhere = tmp_path / "c" / "b"
    elsewhere.mkdir(parents=True)
    (elsewhere / "report.json").write_text("{}", encoding="utf-8")
    _link(tmp_path / "a", tmp_path / "c")
    assert evidence.is_bundle(out)
    with pytest.raises(tasks.TaskError):
        tasks.evidence_path({"id": "T1", "evidence": str(out)}, repo)
    # A folder written through a link is recorded as where it really is.
    real = tmp_path / "real"
    real.mkdir()
    _link(tmp_path / "via", real)
    bundle = evidence.collect(tmp_path / "via" / "b", folder=repo, base="HEAD")
    assert bundle.path == Path(os.path.realpath(real)) / "b"
    assert str(bundle.path) in evidence.registered()
    assert tasks.evidence_path({"id": "T1", "evidence": str(bundle.path)}, repo) == str(
        bundle.path)


def test_a_huge_keep_days_keeps_every_bundle(repo, home):
    old = _bundle_at(evidence.task_bundle_path(repo, "T9", 1), "2020-01-01T00:00:00Z")
    assert evidence.prune(repo, days=1_000_000_000).removed == [] and old.exists()
    res = _cli("evidence", "prune", "--keep-days", "999999999")
    assert res.exit_code == 0, res.output
    assert old.exists() and f"keep_days {evidence.MAX_KEEP_DAYS}" in res.output
    (home / "config.toml").write_text(
        "[tools]\nmemory = false\nskills = false\ntranscripts = false\n"
        "[evidence]\nkeep_days = 1000000\n", encoding="utf-8")
    assert evidence.keep_days(Config.load()) == evidence.MAX_KEEP_DAYS
    res = _cli("evidence", "prune")
    assert res.exit_code == 0 and old.exists(), res.output
