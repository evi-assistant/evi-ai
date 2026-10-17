"""`evi task verify|reject` and `evi attest` (Phase 1, loop-task-queue on
loop-director-attestation).

Acceptance criterion 2: no tool, env flag or file edit available to an
agent yields a signed "verified" status. Each with its test:

- the director's `evi task verify T1` signs through evi.attest (review gate,
  branch head, evidence hash) and the task then shows as verified; a
  rejection is signed too, and a reopen drops the attestation —
  test_verify_signs_the_task_and_it_shows_verified,
  test_reject_signs_the_reason_and_reopen_drops_the_attestation;
- the same command refuses, writing nothing, under EVI_AGENT (or
  EVI_INTERACTIVE), under an eVi process, or from anything that isn't
  director tooling — test_verify_and_reject_refuse_where_an_agent_may_have_started_them;
- an agent's shell can't run it: the implementer policy and the normal
  permission engine (yolo, every prompt approved) both deny
  `evi task verify T1` — test_an_agent_cannot_run_director_tooling_under_either_engine;
- a hand-edited (or forged, or edited-after-signing) verified TOML reads
  unverified in `evi task show` and `list` —
  test_a_hand_edited_verified_task_reads_unverified;
- verify needs the work committed on the task branch and a review gate that
  doesn't block (unless the director records why) —
  test_verify_needs_committed_work_and_a_clean_review;
- `evi attest init` / `status` manage the key and never print it —
  test_attest_init_and_status;
- director tooling never marks its own process EVI_AGENT;
- what the (tracked) task file names is never taken as it is: a tag named
  like the task branch never answers for it —
  test_a_tag_named_like_the_task_branch_never_answers_for_it; verify reviews
  the worktree git has on the branch —
  test_verify_reviews_the_worktree_git_has_not_the_one_the_file_names; its
  base must be an ancestor of the branch's head, and not the head —
  test_verify_never_trusts_the_base_a_task_file_names; and an evidence path
  is read only for a signed task, and only when it is a bundle eVi wrote,
  unchanged since it was collected —
  test_list_and_show_never_follow_an_evidence_path_from_a_task_file,
  test_the_evidence_bundle_is_signed_and_checked,
  test_verify_refuses_a_bundle_changed_since_it_was_collected.

As tests/test_attest.py does, every test stubs evi.attest's process-tree
walk (this process seen as the director's `evi task verify T1`), clears
EVI_AGENT / EVI_INTERACTIVE and uses no OS keyring, under a throwaway eVi
home; the review gate is stubbed (no model).
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tomllib
from pathlib import Path

import pytest
from typer.testing import CliRunner

import evi.tools.shell  # noqa: F401  registers run_command
from evi import attest, tasks, trust
from evi import hooks as hooks_mod
from evi import worktree as wt
from evi.config import Config, _to_toml
from evi.llm.agent import Agent, ToolResult, is_permission_denial
from evi.policy import implementer_policy
from evi.tools.base import REGISTRY
from tests.conftest import llm_call, llm_text

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")

PY = r"C:\venv\Scripts\python.exe"
EVI_EXE = r"C:\venv\Scripts\evi.exe"
# This process as the director's `evi task verify T1` from their own shell.
DIRECTOR_CHAIN = [
    (101, [PY, EVI_EXE, "task", "verify", "T1"]),
    (100, [EVI_EXE, "task", "verify", "T1"]),
    (99, ["/usr/bin/bash", "-c", "evi task verify T1"]),
]
CLEAN = attest.ReviewResult(0, "base", "## Correctness\n\nNothing found.\n\nAPPROVE")
BLOCKING = attest.ReviewResult(1, "base", "## Correctness\n\ncalc.py:1 wrong value")


def _git(cwd: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid",
         "-c", "commit.gpgsign=false", "-c", "init.defaultBranch=main", *args],
        cwd=cwd, capture_output=True, text=True, timeout=60,
    )
    if proc.returncode != 0:
        raise AssertionError(f"git {' '.join(args)}: {proc.stderr}")
    return proc.stdout


@pytest.fixture(autouse=True)
def home(monkeypatch, tmp_path) -> Path:
    import evi.config as config_mod

    evi_home = tmp_path / "evi-home"
    evi_home.mkdir()
    monkeypatch.setenv("EVI_HOME", str(evi_home))
    monkeypatch.setattr(config_mod, "HOME", evi_home)
    monkeypatch.setattr(config_mod, "CONFIG_PATH", evi_home / "config.toml")
    monkeypatch.setattr(config_mod, "ensure_dirs", lambda: None)
    monkeypatch.setattr(hooks_mod, "HOOKS_CONFIG_PATH", evi_home / "hooks.toml")
    monkeypatch.setattr("evi.guardrails.GUARDRAILS_PATH", evi_home / "guardrails.toml")
    monkeypatch.setattr("evi.plugins.plugin_dirs", lambda root=None: [])
    monkeypatch.delenv("EVI_AGENT", raising=False)
    monkeypatch.delenv("EVI_INTERACTIVE", raising=False)
    monkeypatch.delenv("EVI_SAFE_MODE", raising=False)
    monkeypatch.setattr(attest, "_keyring", lambda: None)
    monkeypatch.setattr(attest, "_process_chain", lambda: list(DIRECTOR_CHAIN))
    monkeypatch.setattr(attest, "_has_tty", lambda: False)
    (evi_home / "config.toml").write_text(
        "[tools]\nmemory = false\nskills = false\ntranscripts = false\n", encoding="utf-8")
    return evi_home


class _Gates(list):
    """The stubbed review gate's calls, (base, cwd) each; it answers
    `results[0]` when set, else a clean review."""

    def __init__(self) -> None:
        super().__init__()
        self.results: list[attest.ReviewResult] = []


@pytest.fixture
def gates(monkeypatch) -> _Gates:
    calls = _Gates()

    def review_gate(base, *, cwd=None, reviewer=None):
        calls.append((base, Path(cwd)))
        result = calls.results[0] if calls.results else CLEAN
        return attest.ReviewResult(result.exit_code, base, result.report)

    monkeypatch.setattr(attest, "review_gate", review_gate)
    return calls


@pytest.fixture
def task(home, tmp_path, monkeypatch) -> Path:
    """A trusted repository whose task T1 was started (worktree and branch
    task/T1 from main), its work committed on the branch, and submitted:
    needs_review. The process's folder is the main checkout."""
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "core.autocrlf", "false")
    (root / "calc.py").write_text("x = 1\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "start")
    root = root.resolve()
    trust.grant(root)
    q = tasks.TaskQueue(root)
    q.create(tasks.new_task("T1", title="Make x two", acceptance=["x is 2"],
                            files_in_scope=["calc.py"]))
    q.update("T1", lambda r: tasks.claim(r, holder="evi"))
    iso = wt.open_isolated("task/T1", start=root)
    (iso.path / "calc.py").write_text("x = 2\n", encoding="utf-8")
    _git(iso.path, "commit", "-q", "-am", "make x two")  # what the director commits
    wt.release(iso)
    q.update("T1", lambda r: tasks.finish(
        r, holder="evi", implemented=True, stop_reason="done", exit_code=0,
        fields={"worktree": str(iso.path), "branch": iso.branch, "base": iso.base}))
    q.update("T1", tasks.submit)
    monkeypatch.chdir(root)
    return root


def _cli(*args: str):
    import evi.apps.cli.main as cli_main

    return CliRunner().invoke(cli_main.app, list(args))


def _file(root: Path) -> Path:
    return root / ".evi" / "tasks" / "T1.toml"


def _record(root: Path) -> dict:
    return tomllib.loads(_file(root).read_text(encoding="utf-8"))


def _label(root: Path) -> str:
    res = _cli("task", "show", "T1", "--format", "json")
    assert res.exit_code == 0, res.output
    return json.loads(res.stdout)["label"]


# ---- signing -------------------------------------------------------------------------


def test_verify_signs_the_task_and_it_shows_verified(task, gates):
    kid = attest.init_key(yes=True)
    record = _record(task)
    res = _cli("task", "verify", "T1", "--yes", "--rounds", "2")
    assert res.exit_code == 0, res.output
    signed = _record(task)
    head = _git(task, "rev-parse", "task/T1").strip()
    assert signed["status"] == "verified"
    block = signed["attestation"]
    assert block["verdict"] == "verified" and block["head"] == head and block["key"] == kid
    assert block["rounds"] == 2 and block["evidence"] == "" and block["review"]["exit_code"] == 0
    # The review gate reviewed the branch against its base, in its worktree.
    assert gates == [(record["base"], Path(record["worktree"]))]
    assert attest.verify(signed, evidence_hash="", head=head).ok
    assert _label(task) == "verified"
    listed = json.loads(_cli("task", "list", "--format", "json").stdout)
    assert listed[0]["label"] == "verified"
    assert f"verified at {head[:12]}" in res.output
    # Director tooling never marks its own process an agent's.
    import os

    assert "EVI_AGENT" not in os.environ
    # A verified task changes only by reopening, and the branch moving on
    # makes the attestation stale.
    assert _cli("task", "verify", "T1", "--yes").exit_code == 2
    folder = Path(signed["worktree"])
    _git(folder, "commit", "-q", "--allow-empty", "-m", "later")
    assert _label(task).startswith("unverified (stale")


def test_a_tag_named_like_the_task_branch_never_answers_for_it(task, gates):
    """git rev-parse reads `task/T1` as the tag refs/tags/task/T1 before the
    branch. verify signs the branch's head, and once the branch moves on
    the attestation is stale, whatever tag says otherwise."""
    attest.init_key(yes=True)
    record = _record(task)
    # `git tag task/T1 <sha>` (a lightweight tag) at the base, before signing
    _git(task, "update-ref", "refs/tags/task/T1", record["base"])
    res = _cli("task", "verify", "T1", "--yes")
    assert res.exit_code == 0, res.output
    head = _git(task, "rev-parse", "refs/heads/task/T1").strip()
    assert head != record["base"] and _record(task)["attestation"]["head"] == head
    assert _label(task) == "verified"
    # The tag moved to the signed head, then an unreviewed commit on the branch.
    _git(task, "update-ref", "refs/tags/task/T1", head)
    _git(Path(record["worktree"]), "commit", "-q", "--allow-empty", "-m", "unreviewed change")
    assert _label(task).startswith("unverified (stale")
    listed = json.loads(_cli("task", "list", "--format", "json").stdout)
    assert listed[0]["label"].startswith("unverified (stale")


def test_verify_reviews_the_worktree_git_has_not_the_one_the_file_names(task, gates, tmp_path):
    """The task file is tracked: its worktree path may point anywhere (a UNC
    share). verify reviews the worktree git has on the task branch."""
    attest.init_key(yes=True)
    record = _record(task)
    elsewhere = tmp_path / "not-the-worktree"
    tasks.TaskQueue(task).update("T1", lambda r: {**r, "worktree": str(elsewhere)})
    res = _cli("task", "verify", "T1", "--yes")
    assert res.exit_code == 0, res.output
    assert [Path(cwd).resolve() for _, cwd in gates] == [Path(record["worktree"]).resolve()]
    assert not elsewhere.exists()
    # With the branch checked out nowhere, there is nothing to review.
    _cli("task", "reopen", "T1")
    q = tasks.TaskQueue(task)
    q.update("T1", lambda r: tasks.submit(tasks.finish(
        tasks.claim(r, holder="evi"), holder="evi", implemented=True, stop_reason="done",
        exit_code=0)))
    _git(task, "worktree", "remove", "--force", record["worktree"])
    res = _cli("task", "verify", "T1", "--yes")
    assert res.exit_code == 2 and "no worktree has task/T1 checked out" in res.output
    assert len(gates) == 1


def test_list_and_show_never_follow_an_evidence_path_from_a_task_file(task, gates, monkeypatch,
                                                                      tmp_path):
    """Nothing is read at a task's evidence path unless the task carries an
    attestation, and then only inside the repository or the task's folder
    under eVi's home: never a UNC path (stat'ing one makes Windows log on to
    that host), never a folder elsewhere (hashed on every list)."""
    kid = attest.init_key(yes=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "f.txt").write_text("x\n", encoding="utf-8")
    hashed: list[str] = []
    real = attest.evidence_hash

    def spy(path):  # never touches a path outside the repository, even unfixed
        hashed.append(str(path))
        return real(path) if str(path).startswith(str(task)) else ""

    monkeypatch.setattr(attest, "evidence_hash", spy)
    q = tasks.TaskQueue(task)
    unc = "//attacker.invalid/share/bundle"
    for task_id, evidence in (("T9", unc), ("T10", str(outside))):
        q.create(tasks.new_task(task_id, title="filed by someone"))
        q.update(task_id, lambda r, e=evidence: {**r, "evidence": e})
    res = _cli("task", "list", "--format", "json")
    assert res.exit_code == 0, res.output
    assert {r["id"]: r["label"] for r in json.loads(res.stdout)}["T9"] == "open"
    for task_id in ("T9", "T10"):
        assert _cli("task", "show", task_id, "--format", "json").exit_code == 0
        assert _cli("task", "show", task_id).exit_code == 0
    assert hashed == []
    # A "verified" one with an attestation and a UNC evidence path: refused
    # unread, and it reads unverified.
    body = {**_record(task), "id": "T11", "status": "verified", "evidence": unc}
    block = {"v": 1, "task": "T11", "digest": attest.task_digest(body), "head": "c" * 40,
             "evidence": "", "verdict": "verified", "rounds": 1, "review": {},
             "signer": "director", "signed_at": "2026-10-17T00:00:00Z", "key": kid}
    block["sig"] = attest._mac(b"\x03" * 32, block)
    q.folder.joinpath("T11.toml").write_text(_to_toml({**body, "attestation": block}),
                                             encoding="utf-8")
    shown = json.loads(_cli("task", "show", "T11", "--format", "json").stdout)
    assert shown["label"].startswith("unverified (") and hashed == []
    # verify refuses to sign over one, writing nothing and reviewing nothing.
    q.update("T1", lambda r: {**r, "evidence": str(outside)})
    before = _file(task).read_bytes()
    res = _cli("task", "verify", "T1", "--yes")
    assert res.exit_code == 2 and "not read" in res.output
    assert _file(task).read_bytes() == before and gates == [] and hashed == []


def test_the_evidence_bundle_is_signed_and_checked(task, gates):
    """`evi evidence collect T1` records the bundle (under eVi's home) and
    its summary; verify signs the bundle's hash, and a bundle changed after
    signing reads unverified."""
    from evi import evidence

    attest.init_key(yes=True)
    res = _cli("evidence", "collect", "T1")
    assert res.exit_code == 0, res.output
    record = _record(task)
    bundle = Path(record["evidence"])
    assert bundle == evidence.task_bundle_path(task, "T1", 1) and evidence.is_bundle(bundle)
    assert record["evidence_summary"]["hash"] == attest.evidence_hash(bundle) != ""
    assert record["evidence_summary"]["files"] == 1  # calc.py, committed on the branch
    res = _cli("task", "verify", "T1", "--yes")
    assert res.exit_code == 0, res.output
    assert _record(task)["attestation"]["evidence"] == attest.evidence_hash(bundle)
    assert _label(task) == "verified"
    (bundle / "diff.patch").write_text("nothing changed, honest\n", encoding="utf-8")
    assert _label(task) == "unverified (invalid signature: the evidence changed after it was " \
                           "signed)"
    # A verified task's bundle is not collected again (that would void it).
    res = _cli("evidence", "collect", "T1")
    assert res.exit_code == 2 and "verified" in res.output


def test_verify_refuses_a_bundle_changed_since_it_was_collected(task, gates):
    attest.init_key(yes=True)
    assert _cli("evidence", "collect", "T1").exit_code == 0
    bundle = Path(_record(task)["evidence"])
    (bundle / "diffstat.txt").write_text(" 0 files changed\n", encoding="utf-8")
    before = _file(task).read_bytes()
    res = _cli("task", "verify", "T1", "--yes")
    assert res.exit_code == 2 and "changed since it was collected" in res.output
    assert _file(task).read_bytes() == before and gates == []


@pytest.mark.parametrize("how", ["head", "not an ancestor", "no commit"])
def test_verify_never_trusts_the_base_a_task_file_names(task, gates, how):
    """The base comes from the tracked task file: it must be an ancestor of
    the branch's head and differ from it, or the review gate would review
    an empty (or the wrong) diff and pass. Refused with exit 2, unsigned."""
    attest.init_key(yes=True)
    record = _record(task)
    head = _git(task, "rev-parse", "task/T1").strip()
    if how == "head":
        forged = head
    elif how == "not an ancestor":  # a commit off main the branch never had
        _git(task, "commit", "-q", "--allow-empty", "-m", "elsewhere")
        forged = _git(task, "rev-parse", "HEAD").strip()
    else:
        forged = "0" * 40
    tasks.TaskQueue(task).update("T1", lambda r: {**r, "base": forged})
    before = _file(task).read_bytes()
    res = _cli("task", "verify", "T1", "--yes")
    assert res.exit_code == 2, res.output
    assert {"head": "no work to review", "not an ancestor": "is not an ancestor of task/T1",
            "no commit": "names no commit"}[how] in res.output
    assert _file(task).read_bytes() == before and gates == []
    assert record["base"] != forged


def test_reject_signs_the_reason_and_reopen_drops_the_attestation(task):
    attest.init_key(yes=True)
    res = _cli("task", "reject", "T1", "--yes")
    assert res.exit_code == 2  # --reason is required
    res = _cli("task", "reject", "T1", "--reason", "no test for it", "--yes")
    assert res.exit_code == 0, res.output
    record = _record(task)
    assert record["status"] == "rejected" and record["attestation"]["verdict"] == "rejected"
    assert record["reviewer_notes"][-1]["note"] == "no test for it"  # signed with the record
    assert _label(task) == "rejected"
    res = _cli("task", "reopen", "T1", "--reason", "rework it")
    assert res.exit_code == 0, res.output
    record = _record(task)
    assert record["status"] == "open" and "attestation" not in record
    assert _label(task) == "open"


@pytest.mark.parametrize("how", ["EVI_AGENT", "EVI_INTERACTIVE", "evi chat above",
                                 "not director tooling"])
def test_verify_and_reject_refuse_where_an_agent_may_have_started_them(task, gates,
                                                                         monkeypatch, how):
    attest.init_key(yes=True)
    before = _file(task).read_bytes()
    if how == "EVI_AGENT":
        monkeypatch.setenv("EVI_AGENT", "1")
    elif how == "EVI_INTERACTIVE":
        monkeypatch.setenv("EVI_INTERACTIVE", "1")
    elif how == "evi chat above":  # an agent's shell running it under `evi chat`
        monkeypatch.setattr(attest, "_process_chain", lambda: [
            *DIRECTOR_CHAIN[:2], (98, ["cmd.exe", "/c", "evi task verify T1 --yes"]),
            (97, [PY, EVI_EXE, "chat"])])
    else:  # `python -c "..."` calling the CLI in-process
        monkeypatch.setattr(attest, "_process_chain", lambda: [
            (5, ["python", "-c", "from evi.apps.cli.main import app; app()"]), (4, ["bash"])])
    for args in (["task", "verify", "T1", "--yes"],
                 ["task", "reject", "T1", "--reason", "x", "--yes"]):
        res = _cli(*args)
        assert res.exit_code == 2, res.output
        assert "refusing to" in res.output and "Only the director" in res.output
    assert _file(task).read_bytes() == before  # nothing written
    assert gates == []  # not even the review ran


def test_verify_needs_committed_work_and_a_clean_review(task, gates):
    attest.init_key(yes=True)
    record = _record(task)
    folder = Path(record["worktree"])
    (folder / "calc.py").write_text("x = 22\n", encoding="utf-8")
    res = _cli("task", "verify", "T1", "--yes")
    assert res.exit_code == 2 and "uncommitted changes" in res.output
    _git(folder, "checkout", "-q", "--", "calc.py")
    # A blocking review refuses, unless the director records why.
    gates.results.append(BLOCKING)
    res = _cli("task", "verify", "T1", "--yes")
    assert res.exit_code == 1 and "blocking findings" in res.output
    assert _record(task)["status"] == "needs_review"
    res = _cli("task", "verify", "T1", "--yes", "--reason", "the finding is a false alarm")
    assert res.exit_code == 0, res.output
    signed = _record(task)
    assert signed["attestation"]["review"]["override"] == "the finding is a false alarm"
    assert signed["reviewer_notes"][-1]["verdict"] == "verified"


def test_verify_refuses_without_a_key_or_a_confirmation(task, gates, monkeypatch):
    res = _cli("task", "verify", "T1", "--yes")
    assert res.exit_code == 1 and "no director key" in res.output
    attest.init_key(yes=True)
    res = _cli("task", "verify", "T1")  # no --yes and no terminal
    assert res.exit_code == 1 and "no terminal" in res.output
    assert _record(task)["status"] == "needs_review"


def test_a_task_not_in_review_is_not_signed(task, gates):
    attest.init_key(yes=True)
    tasks.TaskQueue(task).update("T1", lambda r: tasks.reopen(r))
    res = _cli("task", "verify", "T1", "--yes")
    assert res.exit_code == 2 and "needs_review" in res.output
    assert gates == []


# ---- what an agent can't do -------------------------------------------------------


def test_a_hand_edited_verified_task_reads_unverified(task):
    kid = attest.init_key(yes=True)
    path = _file(task)
    record = _record(task)
    # status = "verified" written by hand: nobody signed it.
    path.write_text(_to_toml({**record, "status": "verified"}), encoding="utf-8")
    assert _label(task) == "unverified (unsigned)"
    text = _cli("task", "list").output
    assert "unverified (unsigned)" in text
    # A forged attestation naming the real key id, signed with a made-up key.
    body = {**record, "status": "verified"}
    head = _git(task, "rev-parse", "task/T1").strip()
    block = {"v": 1, "task": "T1", "digest": attest.task_digest(body), "head": head,
             "evidence": "", "verdict": "verified", "rounds": 1, "review": {},
             "signer": "director", "signed_at": "2026-10-17T00:00:00Z", "key": kid}
    block["sig"] = attest._mac(b"\x01" * 32, block)
    path.write_text(_to_toml({**body, "attestation": block}), encoding="utf-8")
    assert _label(task).startswith("unverified (invalid signature")


def test_list_reads_the_key_once_for_the_whole_queue(task, monkeypatch):
    """`evi task list` checks every task with one evi.attest.Verifier."""
    kid = attest.init_key(yes=True)
    record = _record(task)
    q = tasks.TaskQueue(task)
    for task_id in ("T2", "T3"):
        body = {**record, "id": task_id, "status": "verified"}
        block = {"v": 1, "task": task_id, "digest": attest.task_digest(body), "head": "c" * 40,
                 "evidence": "", "verdict": "verified", "rounds": 1, "review": {},
                 "signer": "director", "signed_at": "2026-10-17T00:00:00Z", "key": kid}
        block["sig"] = attest._mac(b"\x02" * 32, block)
        q.folder.joinpath(f"{task_id}.toml").write_text(
            _to_toml({**body, "attestation": block}), encoding="utf-8")
    reads: list[int] = []
    real = attest._key_or_reason
    monkeypatch.setattr(attest, "_key_or_reason", lambda: reads.append(1) or real())
    res = _cli("task", "list", "--format", "json")
    assert res.exit_code == 0, res.output
    labels = {r["id"]: r["label"] for r in json.loads(res.stdout)}
    assert labels["T1"] == "needs_review"
    assert labels["T2"].startswith("unverified (invalid signature")
    assert labels["T3"].startswith("unverified (invalid signature")
    assert len(reads) == 1


def test_editing_a_signed_task_by_hand_voids_it(task, gates):
    attest.init_key(yes=True)
    assert _cli("task", "verify", "T1", "--yes").exit_code == 0
    signed = _record(task)
    _file(task).write_text(_to_toml({**signed, "title": "Something else"}), encoding="utf-8")
    assert _label(task) == "unverified (invalid signature: the task record changed after it " \
                           "was signed)"


def test_an_agent_cannot_run_director_tooling_under_either_engine(task, fake_llm):
    """An agent's run_command `evi task verify T1 --yes`: denied by the
    implementer policy, and by the permission engine in yolo with every
    prompt approved. The task stays in needs_review."""
    attest.init_key(yes=True)
    before = _file(task).read_bytes()
    lines = ("evi task verify T1 --yes", "python -m evi task reject T1 --reason x --yes",
             "evi attest init --rotate --yes")
    folder = Path(_record(task)["worktree"])

    def run(agent: Agent) -> list[ToolResult]:
        return [e for e in agent.chat("verify it yourself") if isinstance(e, ToolResult)]

    # 1. under the implementer policy (what `evi task start` runs)
    agent = Agent(client=fake_llm([llm_call(*[("run_command", {"command": c}) for c in lines]),
                                   llm_text("verified")]),
                  config=Config.load(), tools=[REGISTRY["run_command"]], transcripts=None)
    agent.cwd = str(folder)
    agent.policy = implementer_policy(folder)
    results = run(agent)
    assert len(results) == 3 and all(is_permission_denial(r.output) for r in results)
    # 2. under the permission engine, yolo, a user approving every prompt
    cfg = Config.load()
    cfg.auto.mode = "yolo"
    asked: list[str] = []
    agent = Agent(client=fake_llm([llm_call(*[("run_command", {"command": c}) for c in lines]),
                                   llm_text("verified")]),
                  config=cfg, tools=[REGISTRY["run_command"]], transcripts=None,
                  permission_callback=lambda *a: asked.append(a[0]) or True)
    agent.cwd = str(task)
    agent.enable_auto_all()
    results = run(agent)
    assert len(results) == 3 and all(is_permission_denial(r.output) for r in results)
    assert all("director" in r.output for r in results) and asked == []
    assert _file(task).read_bytes() == before


# ---- evi attest ------------------------------------------------------------------------


def test_attest_init_and_status(home, monkeypatch):
    res = _cli("attest", "status")
    assert res.exit_code == 1 and "no director key" in res.output
    monkeypatch.setattr(attest, "_process_chain", lambda: [
        (5, [PY, EVI_EXE, "attest", "init", "--yes"]), (4, ["bash"])])
    res = _cli("attest", "init", "--yes")
    assert res.exit_code == 0, res.output
    kid = attest.key_id()
    assert kid and kid in res.output
    res = _cli("attest", "init", "--yes")
    assert res.exit_code == 1 and "already exists" in res.output
    res = _cli("attest", "status")
    assert res.exit_code == 0 and kid in res.output and str(attest.key_path()) in res.output
    key = attest._file_get()
    assert key is not None and key.hex() not in res.output and key.hex().upper() not in res.output
    # From an agent's process: no key read, nothing created.
    monkeypatch.setenv("EVI_AGENT", "1")
    res = _cli("attest", "status")
    assert res.exit_code == 1 and "isn't read here" in res.output and kid not in res.output
    res = _cli("attest", "init", "--rotate", "--yes")
    assert res.exit_code == 2 and "EVI_AGENT" in res.output
    monkeypatch.delenv("EVI_AGENT")
    assert attest.key_id() == kid  # untouched
