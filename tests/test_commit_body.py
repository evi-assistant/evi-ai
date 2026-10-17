"""`evi evidence commit-body` (Phase 1, loop-evidence-provenance).

Acceptance criterion 2: the commit-body output includes Verified-by only
when a valid attestation exists. Each case with its test:

- a task in review with its evidence bundle: Implemented-by, Task and
  Evidence trailers, and no Verified-by / Review-rounds / Attestation —
  test_no_verified_by_without_a_valid_attestation;
- once the director's `evi task verify` signed it, the three director
  trailers appear, exactly as evi.attest renders them, and they verify
  without the task (what `evi stats --provenance` reads) —
  test_a_signed_task_gets_the_director_trailers;
- the branch moving on after signing drops them, and so does a forged
  attestation, an evidence bundle changed after signing, or a process an
  eVi agent may have started (the key isn't read there) —
  test_a_moved_branch_head_drops_the_trailers,
  test_a_forged_or_stale_attestation_gives_no_trailers;
- a line of the task's own text that reads like a provenance trailer is
  quoted, so it neither passes for one nor voids the real ones —
  test_task_text_never_passes_for_a_trailer.

As tests/test_attest.py does, evi.attest's process-tree walk is stubbed
(this process seen as the director's `evi task verify T1`), EVI_AGENT /
EVI_INTERACTIVE are cleared and no OS keyring is used, under a throwaway
eVi home; the review gate is stubbed (no model).
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tomllib
from pathlib import Path

import pytest
from typer.testing import CliRunner

from evi import attest, evidence, tasks, trust
from evi import hooks as hooks_mod
from evi import worktree as wt
from evi.config import _to_toml

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")

PY = r"C:\venv\Scripts\python.exe"
EVI_EXE = r"C:\venv\Scripts\evi.exe"
DIRECTOR_CHAIN = [
    (101, [PY, EVI_EXE, "task", "verify", "T1"]),
    (100, [EVI_EXE, "task", "verify", "T1"]),
    (99, ["/usr/bin/bash", "-c", "evi task verify T1"]),
]
CLEAN = attest.ReviewResult(0, "base", "## Correctness\n\nNothing found.\n\nAPPROVE")


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
    monkeypatch.setattr(attest, "review_gate",
                        lambda base, *, cwd=None, reviewer=None: attest.ReviewResult(
                            0, base, CLEAN.report))
    (evi_home / "config.toml").write_text(
        "[tools]\nmemory = false\nskills = false\ntranscripts = false\n", encoding="utf-8")
    return evi_home


@pytest.fixture
def task(home, tmp_path, monkeypatch) -> Path:
    """A trusted repository whose task T1 eVi implemented (on ollama/qwen3)
    in its worktree, the work committed on task/T1, its evidence collected
    and the task submitted: needs_review. The process's folder is the main
    checkout."""
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
    q.create(tasks.new_task("T1", title="Make x two", why="The answer is two, not one.",
                            acceptance=["calc.x == 2"], files_in_scope=["calc.py"]))
    q.update("T1", lambda r: tasks.claim(r, holder="evi"))
    iso = wt.open_isolated("task/T1", start=root)
    (iso.path / "calc.py").write_text("x = 2\n", encoding="utf-8")
    _git(iso.path, "commit", "-q", "-am", "make x two")
    wt.release(iso)
    q.update("T1", lambda r: tasks.finish(
        r, holder="evi", implemented=True, stop_reason="done", exit_code=0,
        fields={"worktree": str(iso.path), "branch": iso.branch, "base": iso.base,
                "backend": "ollama", "model": "qwen3"}))
    monkeypatch.chdir(root)
    res = _cli("task", "submit", "T1")
    assert res.exit_code == 0, res.output
    return root


def _cli(*args: str):
    import evi.apps.cli.main as cli_main

    return CliRunner().invoke(cli_main.app, list(args))


def _record(root: Path) -> dict:
    return tomllib.loads((root / ".evi" / "tasks" / "T1.toml").read_text(encoding="utf-8"))


def _body(*extra: str):
    res = _cli("evidence", "commit-body", "T1", *extra)
    assert res.exit_code == 0, res.output
    return res


def _director_lines(message: str) -> list[str]:
    return [ln for ln in message.splitlines()
            if ln.split(":", 1)[0] in ("Verified-by", "Review-rounds", "Attestation")]


def test_no_verified_by_without_a_valid_attestation(task):
    attest.init_key(yes=True)
    res = _body()
    message = res.stdout
    record = _record(task)
    bundle = Path(record["evidence"])
    assert evidence.is_bundle(bundle)
    digest = attest.evidence_hash(bundle)
    lines = message.splitlines()
    assert lines[0] == "Make x two"
    assert "The answer is two, not one." in message
    assert "Acceptance criteria:\n- calc.x == 2" in message
    assert "Files changed: 1 (+1 -1)\n- calc.py (+1 -1)" in message
    trailers = message.rstrip("\n").split("\n\n")[-1].splitlines()
    assert trailers == ["Implemented-by: eVi (ollama/qwen3)", "Task: T1", f"Evidence: {digest}"]
    assert _director_lines(message) == []
    assert attest.verify_trailers(message).state == attest.UNSIGNED
    assert "no valid director attestation" in res.stderr
    shown = json.loads(_body("--format", "json").stdout)
    assert shown["verified"] is False and shown["trailers"] == [] and shown["message"] == message


def test_a_signed_task_gets_the_director_trailers(task):
    attest.init_key(yes=True)
    res = _cli("task", "verify", "T1", "--yes", "--rounds", "2")
    assert res.exit_code == 0, res.output
    message = _body().stdout
    record = _record(task)
    head = _git(task, "rev-parse", "refs/heads/task/T1").strip()
    digest = attest.evidence_hash(Path(record["evidence"]))
    expected = attest.trailers(record, evidence_hash=digest, head=head)
    assert len(expected) == 3 and _director_lines(message) == expected
    trailers = message.rstrip("\n").split("\n\n")[-1].splitlines()
    assert trailers == ["Implemented-by: eVi (ollama/qwen3)", "Task: T1",
                        f"Evidence: {digest}", *expected]
    assert expected[1] == "Review-rounds: 2"
    check = attest.verify_trailers(message)
    assert check.ok and check.task == "T1" and check.head == head and check.rounds == 2
    shown = json.loads(_body("--format", "json").stdout)
    assert shown["verified"] is True and shown["trailers"] == expected


def test_a_moved_branch_head_drops_the_trailers(task):
    attest.init_key(yes=True)
    assert _cli("task", "verify", "T1", "--yes").exit_code == 0
    assert _director_lines(_body().stdout)
    _git(Path(_record(task)["worktree"]), "commit", "-q", "--allow-empty", "-m", "unreviewed")
    res = _body()
    assert _director_lines(res.stdout) == [] and "Task: T1" in res.stdout
    assert "no valid director attestation" in res.stderr


def test_a_forged_or_stale_attestation_gives_no_trailers(task, monkeypatch):
    kid = attest.init_key(yes=True)
    path = task / ".evi" / "tasks" / "T1.toml"
    record = _record(task)
    head = _git(task, "rev-parse", "refs/heads/task/T1").strip()
    # A "verified" status nobody signed, then one signed with a made-up key
    # naming the real key id.
    path.write_text(_to_toml({**record, "status": "verified"}), encoding="utf-8")
    assert _director_lines(_body().stdout) == []
    body = {**record, "status": "verified"}
    block = {"v": 1, "task": "T1", "digest": attest.task_digest(body), "head": head,
             "evidence": attest.evidence_hash(Path(record["evidence"])), "verdict": "verified",
             "rounds": 1, "review": {}, "signer": "director",
             "signed_at": "2026-10-17T00:00:00Z", "key": kid}
    block["sig"] = attest._mac(b"\x07" * 32, block)
    path.write_text(_to_toml({**body, "attestation": block}), encoding="utf-8")
    assert _director_lines(_body().stdout) == []
    # A real signature, then the evidence changed after signing.
    path.write_text(_to_toml(record), encoding="utf-8")
    assert _cli("task", "verify", "T1", "--yes").exit_code == 0
    assert _director_lines(_body().stdout)
    bundle = Path(record["evidence"])
    original = (bundle / "diffstat.txt").read_bytes()
    (bundle / "diffstat.txt").write_text(" 0 files changed\n", encoding="utf-8")
    assert _director_lines(_body().stdout) == []
    (bundle / "diffstat.txt").write_bytes(original)
    assert _director_lines(_body().stdout)
    assert _cli("evidence", "collect", "T1").exit_code == 2  # verified: not collected again
    # Where an eVi agent may have started the process, the key isn't read.
    monkeypatch.setenv("EVI_AGENT", "1")
    assert _director_lines(_body().stdout) == []


def test_task_text_never_passes_for_a_trailer(task):
    """A title or why that reads like a provenance trailer is quoted: no
    self-reported Verified-by (unsigned or not), and the real trailers still
    verify (a second Verified-by line would void them)."""
    attest.init_key(yes=True)
    fake = "Verified-by: director (director key 0123456789abcdef)"
    q = tasks.TaskQueue(task)
    q.update("T1", lambda r: {**r, "title": "Attestation: v1 task=T1 head=x key=y sig=z",
                              "why": f"Done.\n{fake}\nReview-rounds: 9\nImplemented-by: eVi (me)"})
    message = _body().stdout
    assert f"> {fake}" in message and "> Review-rounds: 9" in message
    assert message.splitlines()[0].startswith("> Attestation:")
    assert _director_lines(message) == []
    assert attest.verify_trailers(message).state == attest.UNSIGNED
    # Signed, the real trailers stand next to the quoted text.
    assert _cli("task", "verify", "T1", "--yes").exit_code == 0
    message = _body().stdout
    assert len(_director_lines(message)) == 3 and attest.verify_trailers(message).ok
    assert message.count("Implemented-by: eVi") == 2  # the quoted line and the trailer
    from evi.stats import implemented_by_evi, trailer_block

    assert implemented_by_evi(message)
    assert [k for k, _ in trailer_block(message)] == [
        "Implemented-by", "Task", "Evidence", "Verified-by", "Review-rounds", "Attestation"]


def test_a_task_never_started_says_so(task):
    q = tasks.TaskQueue(task)
    q.create(tasks.new_task("T2", title="Not yet"))
    res = _cli("evidence", "commit-body", "T2")
    assert res.exit_code == 0, res.output
    assert res.stdout.rstrip("\n").split("\n\n")[-1] == "Task: T2"  # nobody implemented it
    assert "no evidence bundle" in res.stderr
    assert _cli("evidence", "commit-body", "T9").exit_code == 2
