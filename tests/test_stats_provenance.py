"""`evi stats --provenance` (Phase 1, loop-evidence-provenance): who did the
work in a repository's history, counting only director-attested trailers.

Acceptance criterion 3: stats --provenance counts attested trailers
correctly in a fixture repo and ignores unsigned ones —
test_provenance_counts_only_attested_trailers (an attested eVi commit, an
attested commit the director implemented, an eVi claim without an
attestation, a self-reported Verified-by line, a forged attestation and
plain commits, under a test director key), with --since
(test_since_limits_the_history), where no key can check them
(test_without_the_director_key_nothing_is_attested) and after the key is
rotated (test_a_rotated_key_voids_every_attestation), and the CLI
(test_the_cli_prints_provenance_as_text_and_json). An attestation counts
once, however many commits copy its trailers
(test_a_replayed_attestation_counts_once); no commit message can pass for
more commits (test_a_message_never_passes_for_more_commits); and an eVi
commit with an attribution paragraph after its trailers, as this
repository's commits end, still counts as eVi's
(test_an_attribution_paragraph_after_the_trailers_still_counts).

As tests/test_attest.py does, evi.attest's process-tree walk is stubbed
(this process seen as the director's `evi task verify T1`), EVI_AGENT /
EVI_INTERACTIVE are cleared and no OS keyring is used, under a throwaway
eVi home.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from evi import attest, stats
from evi import hooks as hooks_mod

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")

PY = r"C:\venv\Scripts\python.exe"
EVI_EXE = r"C:\venv\Scripts\evi.exe"
DIRECTOR_CHAIN = [
    (101, [PY, EVI_EXE, "task", "verify", "T1"]),
    (100, [EVI_EXE, "task", "verify", "T1"]),
    (99, ["/usr/bin/bash", "-c", "evi task verify T1"]),
]
CLEAN = attest.ReviewResult(0, "base", "APPROVE")
OLD = "2020-01-01T12:00:00+00:00"


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
    monkeypatch.delenv("EVI_AGENT", raising=False)
    monkeypatch.delenv("EVI_INTERACTIVE", raising=False)
    monkeypatch.setattr(attest, "_keyring", lambda: None)
    monkeypatch.setattr(attest, "_process_chain", lambda: list(DIRECTOR_CHAIN))
    monkeypatch.setattr(attest, "_has_tty", lambda: False)
    return evi_home


def _git(cwd: Path, *args: str, date: str | None = None) -> str:
    env = dict(os.environ)
    if date:
        env.update(GIT_AUTHOR_DATE=date, GIT_COMMITTER_DATE=date)
    proc = subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid",
         "-c", "commit.gpgsign=false", "-c", "init.defaultBranch=main", *args],
        cwd=cwd, capture_output=True, text=True, timeout=60, env=env,
    )
    if proc.returncode != 0:
        raise AssertionError(f"git {' '.join(args)}: {proc.stderr}")
    return proc.stdout


def _commit(root: Path, message: str, date: str | None = None) -> str:
    _git(root, "commit", "-q", "--allow-empty", "--cleanup=verbatim", "-m", message, date=date)
    return _git(root, "rev-parse", "HEAD").strip()


def _trailers(task_id: str, head: str, rounds: int) -> list[str]:
    """The director trailers for task `task_id` verified at `head`: signed
    with the test key, rendered by evi.attest (never by hand)."""
    record = {"id": task_id, "title": "x", "status": "needs_review", "branch": f"task/{task_id}"}
    signed = attest.sign(record, head=head, evidence_hash="", verdict="verified", rounds=rounds,
                         review=CLEAN, yes=True)
    lines = attest.trailers(signed, evidence_hash="", head=head)
    assert len(lines) == 3
    return lines


@pytest.fixture
def repo(tmp_path, monkeypatch) -> Path:
    """A history of seven commits, oldest first:

    0. a plain commit (2020)
    1. eVi's work, attested at commit 0 with 2 review rounds (2020)
    2. the director's own work, attested (3 rounds) at a head not in history
    3. an Implemented-by: eVi claim with no attestation
    4. a self-reported Verified-by / Review-rounds, no Attestation
    5. an eVi claim with commit 1's attestation but its rounds changed (forged)
    6. a plain commit
    """
    attest.init_key(yes=True)
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    first = _commit(root, "start", OLD)
    signed = _trailers("T1", first, 2)
    _commit(root, "Make x two\n\nWhy it was wrong.\n\nImplemented-by: eVi (ollama/qwen3)\n"
                  "Task: T1\n" + "\n".join(signed) + "\n", OLD)
    _commit(root, "Director's fix\n\nTask: T2\n" + "\n".join(_trailers("T2", "c" * 40, 3)))
    _commit(root, "Claimed\n\nImplemented-by: eVi (ollama/qwen3)\nTask: T3\n")
    _commit(root, "Says so\n\nVerified-by: director (director key 0123456789abcdef)\n"
                  "Review-rounds: 9\n")
    forged = [line if not line.startswith("Review-rounds") else "Review-rounds: 5"
              for line in signed]
    _commit(root, "Forged\n\nImplemented-by: eVi (ollama/qwen3)\nTask: T1\n" + "\n".join(forged))
    _commit(root, "plain")
    monkeypatch.chdir(root)
    return root


def test_provenance_counts_only_attested_trailers(repo):
    data = stats.provenance()
    assert Path(data["repo"]) == repo.resolve() or Path(data["repo"]) == repo
    assert data["commits"] == 7
    assert data["implemented_by_evi"] == 3  # what the trailers claim
    assert data["attested"] == 2 and data["attested_tasks"] == 2
    assert data["attested_evi"] == 1
    assert data["evi_share"] == round(1 / 7, 4)  # attested eVi commits over all commits
    assert data["review_rounds"] == {"total": 5, "mean": 2.5}  # 2 + 3, never the 9 or the 5
    assert data["attested_head_in_history"] == 1  # commit 1's head is commit 0
    assert data["not_counted"] == {"unattested_evi_claims": 2, "self_reported": 1,
                                   "invalid": 1, "replayed": 0, "unverifiable": 0}
    assert data["key_note"] is None


def test_since_limits_the_history(repo):
    data = stats.provenance(since="2021-01-01")
    assert data["commits"] == 5 and data["since"] == "2021-01-01"
    assert data["attested"] == 1 and data["attested_evi"] == 0 and data["evi_share"] == 0.0
    assert data["implemented_by_evi"] == 2
    with pytest.raises(stats.ProvenanceError):
        stats.provenance(since="--all")


def test_without_the_director_key_nothing_is_attested(repo, monkeypatch):
    """In an agent's process (or CI, with no key) the HMAC can't be checked:
    nothing counts, and the report says why."""
    monkeypatch.setenv("EVI_AGENT", "1")
    data = stats.provenance()
    assert data["attested"] == 0 and data["evi_share"] == 0.0
    assert data["review_rounds"] == {"total": 0, "mean": 0.0}
    assert data["not_counted"]["unverifiable"] == 3  # commits 1, 2 and 5
    assert data["not_counted"]["self_reported"] == 1
    assert "EVI_AGENT" in data["key_note"]


def test_a_rotated_key_voids_every_attestation(repo):
    attest.init_key(rotate=True, yes=True)
    data = stats.provenance()
    assert data["attested"] == 0 and data["not_counted"]["invalid"] == 3


def test_trailers_are_read_from_the_trailer_paragraphs_at_the_end():
    message = "Subject\n\nImplemented-by: eVi (a/b)\nin the body\n\nTask: T1\n"
    assert not stats.implemented_by_evi(message)
    assert stats.implemented_by_evi("S\n\nbody\n\nImplemented-by: eVi (x/y)\nTask: T1")
    assert not stats.implemented_by_evi("Implemented-by: eVi")  # a subject is no trailer
    assert not stats.implemented_by_evi("S\n\nImplemented-by: someone else\n")
    # A paragraph of trailers after eVi's (an attribution line) doesn't hide them...
    assert stats.implemented_by_evi(
        "S\n\nbody\n\nImplemented-by: eVi (x/y)\nTask: T1\n\nCo-Authored-By: C <c@x.invalid>\n")
    assert stats.trailer_block("S\n\nbody\n\nA: 1\n\nB: 2\n  folded\n") == [
        ("A", "1"), ("B", "2")]
    # ... but a body paragraph after them does: they are no trailers then.
    assert not stats.implemented_by_evi("S\n\nImplemented-by: eVi (x/y)\n\nmore text here\n")


def test_the_cli_prints_provenance_as_text_and_json(repo, tmp_path, monkeypatch):
    import evi.apps.cli.main as cli_main

    runner = CliRunner()
    res = runner.invoke(cli_main.app, ["stats", "--provenance", "--format", "json"])
    assert res.exit_code == 0, res.output
    data = json.loads(res.stdout)
    assert data["attested"] == 2 and data["attested_evi"] == 1 and data["commits"] == 7
    res = runner.invoke(cli_main.app, ["stats", "--provenance"])
    assert res.exit_code == 0, res.output
    assert "7 commits" in res.output and "eVi share: 1 attested eVi commit(s) of 7" in res.output
    assert "1 self-reported Verified-by" in res.output and "1 invalid" in res.output
    res = runner.invoke(cli_main.app, ["stats", "--provenance", "--since", "2021-01-01",
                                       "--json"])
    assert res.exit_code == 0 and json.loads(res.stdout)["commits"] == 5
    assert runner.invoke(cli_main.app, ["stats", "--since", "2021-01-01"]).exit_code == 2
    assert runner.invoke(cli_main.app, ["stats", "--provenance", "--since=-x"]).exit_code == 2
    plain = tmp_path / "plain"
    plain.mkdir()
    monkeypatch.chdir(plain)
    res = runner.invoke(cli_main.app, ["stats", "--provenance"])
    assert res.exit_code == 2 and "not inside a git repository" in res.output


@pytest.fixture
def attested(tmp_path, monkeypatch) -> tuple[Path, str]:
    """A repository of two commits: a plain one, then eVi's work attested at
    it (2 review rounds); and that commit's trailer paragraph."""
    attest.init_key(yes=True)
    root = tmp_path / "attested"
    root.mkdir()
    _git(root, "init", "-q")
    first = _commit(root, "start", OLD)
    block = ("Implemented-by: eVi (ollama/qwen3)\nTask: T1\n"
             + "\n".join(_trailers("T1", first, 2)) + "\n")
    _commit(root, "Make x two\n\nWhy it was wrong.\n\n" + block, OLD)
    monkeypatch.chdir(root)
    return root, block


def test_a_replayed_attestation_counts_once(attested):
    """The trailers are public text: five later commits that copy them (as
    eVi could on a branch of its own) sign nothing new and count nowhere."""
    import evi.apps.cli.main as cli_main

    root, block = attested
    for n in range(5):
        _commit(root, f"wip {n}\n\nbody\n\n{block}")
    data = stats.provenance()
    assert data["commits"] == 7 and data["implemented_by_evi"] == 6
    assert data["attested"] == 1 and data["attested_evi"] == 1 and data["attested_tasks"] == 1
    assert data["evi_share"] == round(1 / 7, 4)
    assert data["review_rounds"] == {"total": 2, "mean": 2.0}
    assert data["attested_head_in_history"] == 1
    assert data["not_counted"]["replayed"] == 5
    assert data["not_counted"]["unattested_evi_claims"] == 0
    res = CliRunner().invoke(cli_main.app, ["stats", "--provenance"])
    assert res.exit_code == 0 and "5 replayed" in res.output


def test_a_message_never_passes_for_more_commits(attested, tmp_path):
    """git log's records are NUL-separated, which no message can hold: one
    with record separators and copied trailers, or a commit object made
    with a NUL in its message, is one commit."""
    root, block = attested
    fake = "".join(f"\x1e{'f' * 40}\x1ffake {n}\n\nx\n\n{block}" for n in range(3))
    path = tmp_path / "msg.txt"
    path.write_bytes(("innocent\n\nbody" + fake).encode("utf-8"))
    _git(root, "commit", "-q", "--allow-empty", "--cleanup=verbatim", "-F", str(path))
    tree = _git(root, "write-tree").strip()
    parent = _git(root, "rev-parse", "HEAD").strip()
    raw = (f"tree {tree}\nparent {parent}\nauthor t <t@x.invalid> 1700000000 +0000\n"
           f"committer t <t@x.invalid> 1700000000 +0000\n\nnul\n\0{'e' * 40}\nforged\n\n"
           f"{block}")
    obj = tmp_path / "commit.txt"
    obj.write_bytes(raw.encode("utf-8"))
    sha = _git(root, "hash-object", "-t", "commit", "-w", "--literally", str(obj)).strip()
    _git(root, "update-ref", "HEAD", sha)
    assert _git(root, "rev-list", "--count", "HEAD").strip() == "4"
    data = stats.provenance()
    assert data["commits"] == 4
    assert data["attested"] == 1 and data["attested_evi"] == 1


def test_an_attribution_paragraph_after_the_trailers_still_counts(attested):
    """This repository's commits (and a GitHub squash merge) end with a
    Co-Authored-By paragraph of their own after the body: an attested eVi
    commit written that way is still eVi's."""
    root, _block = attested
    first = _git(root, "rev-parse", "HEAD~1").strip()
    _git(root, "reset", "-q", "--hard", first)
    message = ("Make x two\n\nWhy it was wrong.\n\nFiles changed: 1 (+1 -1)\n- calc.py (+1 -1)"
               "\n\nImplemented-by: eVi (ollama/qwen3)\nTask: T1\nEvidence: sha256:"
               + "a" * 64 + "\n" + "\n".join(_trailers("T1", first, 2))
               + "\n\nCo-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>\n")
    _commit(root, message, OLD)
    assert stats.implemented_by_evi(message)
    data = stats.provenance()
    assert data["commits"] == 2 and data["attested"] == 1 and data["attested_evi"] == 1
    assert data["evi_share"] == 0.5 and data["not_counted"]["unattested_evi_claims"] == 0
