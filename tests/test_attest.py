"""Director attestation (Phase 1, loop-director-attestation).

Only the director marks a task verified or rejected: `evi.attest` signs a
verdict with a key kept in the OS keyring (or a user-only file under eVi's
home), over the task record, the branch head, the evidence hash, the verdict
and the review rounds, and refuses to sign from an agent's process. These
tests cover:

- the key: created once, user-only and binary on disk, keyring preferred, a
  test home never touching the real home's keyring entry; rotation voids
  every old attestation;
- sign / verify: a TOML round trip stays valid; editing the record, its
  evidence or the attestation afterwards, replaying it on another task, a
  guessed key or a malformed table all read as unverified, and a
  ``status = "verified"`` nobody signed (or one that only looks like it)
  reads ``unverified (unsigned)``; a moved branch head reads stale;
- commit-body trailers come only from a valid "verified" attestation at the
  branch's current head, and their signed Attestation line verifies (for
  ``stats --provenance``) without the task or its evidence;
- who may sign: never with EVI_AGENT / EVI_INTERACTIVE set, never from a
  process that isn't ``evi task verify|reject`` / ``evi attest``, never under
  an eVi process, and only with a terminal confirmation or ``--yes``;
- the review gate: a verify with blocking ``evi review --multi --exit-code``
  findings fails unless an override reason is given, which is signed;
- an agent in yolo with run_command, read_file and write_file (and a user
  approving every prompt) can't produce a task that shows as verified.

Every test runs under a throwaway eVi home and a fake keyring (never the OS
one), with the process checks stubbed unless a test is about them.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

import evi.tools.code  # noqa: F401  registers run_python
import evi.tools.fs  # noqa: F401  registers read_file / write_file
import evi.tools.shell  # noqa: F401  registers run_command
from evi import attest
from evi import config as config_mod
from evi import hooks as hooks_mod
from evi.config import Config, _to_toml
from evi.llm.agent import Agent, ToolResult, is_permission_denial
from evi.tools.base import REGISTRY

_REAL_PROCESS_CHAIN = attest._process_chain
HEAD = "a" * 40  # the branch head `_sign` signs at
PY = r"C:\venv\Scripts\python.exe"
EVI_EXE = r"C:\venv\Scripts\evi.exe"
# This process as `evi task verify T1` run by the director from Claude Code's
# shell, as Windows shows it: the interpreter, under the venv's python.exe
# redirector (same command line), under the console-script launcher, under bash.
DIRECTOR_CHAIN = [
    (101, [PY, EVI_EXE, "task", "verify", "T1"]),
    (100, [PY, EVI_EXE, "task", "verify", "T1"]),
    (99, [EVI_EXE, "task", "verify", "T1"]),
    (98, ["/usr/bin/bash", "-c", "cd /p/evi && evi task verify T1"]),
    (97, [r"C:\Users\me\AppData\Local\claude\claude.exe"]),
]


class FakeKeyring:
    """An in-memory stand-in for the `keyring` module."""

    def __init__(self, fail_set: bool = False) -> None:
        self.store: dict[tuple[str, str], str] = {}
        self.fail_set = fail_set

    def get_password(self, service: str, user: str) -> str | None:
        return self.store.get((service, user))

    def set_password(self, service: str, user: str, value: str) -> None:
        if self.fail_set:
            raise RuntimeError("no keyring backend")
        self.store[(service, user)] = value


@pytest.fixture(autouse=True)
def home(monkeypatch, tmp_path) -> Path:
    """eVi's home under tmp_path, no keyring, no agent markers, and this
    process seen as the director's `evi task verify T1` without a terminal."""
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
    return evi_home


def _task(**extra) -> dict:
    return {"id": "T1", "title": "Fix the thing", "status": "review", "branch": "evi/T1",
            "criteria": ["a test fails before", "it passes after"], **extra}


CLEAN = attest.ReviewResult(0, "main", "## Correctness\n\nNothing in this lens.\n\nAPPROVE")
BLOCKING = attest.ReviewResult(1, "main", "## Correctness\n\nevi/x.py:12 off by one")


def _sign(record=None, **kw) -> dict:
    args = dict(head=HEAD, evidence_hash="", verdict="verified", rounds=2, review=CLEAN,
                yes=True)
    args.update(kw)
    return attest.sign(record or _task(), **args)


def _round_trip(record: dict) -> dict:
    """Write `record` as TOML and read it back, as the task queue will."""
    return tomllib.loads(_to_toml(record))


# ---- the key ---------------------------------------------------------------


def test_init_key_writes_a_user_only_binary_file(home):
    kid = attest.init_key(yes=True)
    path = home / "director" / "director.key"
    data = path.read_bytes()
    assert data.startswith(b"\xff") and len(data) == len(attest._FILE_MAGIC) + 32
    with pytest.raises(UnicodeDecodeError):  # text tools (search_files, grep) skip it
        path.read_text(encoding="utf-8")
    assert attest.key_id() == kid and attest.key_location() == str(path)
    assert not list(path.parent.glob("*.tmp"))
    if os.name != "nt":
        assert path.stat().st_mode & 0o777 == 0o600
        assert path.parent.stat().st_mode & 0o777 == 0o700


def test_init_key_refuses_to_replace_a_key_unless_rotating():
    kid = attest.init_key(yes=True)
    with pytest.raises(attest.AttestError, match="already exists"):
        attest.init_key(yes=True)
    assert attest.key_id() == kid
    new = attest.init_key(rotate=True, yes=True)
    assert new != kid and attest.key_id() == new


def test_the_keyring_is_preferred_and_a_test_home_has_its_own_entry(monkeypatch, home):
    kr = FakeKeyring()
    monkeypatch.setattr(attest, "_keyring", lambda: kr)
    kid = attest.init_key(yes=True)
    assert not (home / "director" / "director.key").exists()
    [(service, user)] = kr.store
    # not the real ~/.evi's entry, which a test must never read or replace
    assert service == "evi-director" and user.startswith("attestation-key@")
    assert attest.key_id() == kid and "keyring" in attest.key_location()
    signed = _sign()
    assert attest.verify(signed, evidence_hash="").ok


def test_a_failing_keyring_falls_back_to_the_file(monkeypatch, home):
    monkeypatch.setattr(attest, "_keyring", lambda: FakeKeyring(fail_set=True))
    kid = attest.init_key(yes=True)
    assert (home / "director" / "director.key").is_file() and attest.key_id() == kid


def test_without_a_key_nothing_signs(home):
    with pytest.raises(attest.AttestError, match="no director key"):
        _sign()
    assert attest.key_id() is None and attest.key_location() == ""
    assert not (home / "director").exists()


def test_rotating_the_key_voids_old_attestations():
    attest.init_key(yes=True)
    signed = _sign()
    attest.init_key(rotate=True, yes=True)
    check = attest.verify(signed, evidence_hash="")
    assert check.state == attest.INVALID and "another director key" in check.reason


# ---- sign / verify -----------------------------------------------------------


def test_a_signed_task_survives_the_toml_round_trip():
    attest.init_key(yes=True)
    signed = _round_trip(_sign(rounds=3))
    assert signed["status"] == "verified" and signed["attestation"]["task"] == "T1"
    check = attest.verify(signed, evidence_hash="", head="a" * 40)
    assert check.ok and check.verdict == "verified" and check.rounds == 3
    assert attest.status_label(signed, evidence_hash="", head=HEAD) == "verified"


def test_a_verified_status_nobody_signed_reads_unverified():
    attest.init_key(yes=True)
    forged = _task(status="verified")
    assert attest.status_label(forged, evidence_hash="", head=HEAD) == "unverified (unsigned)"
    assert attest.trailers(forged, evidence_hash="", head=HEAD) == []
    # a status that claims nothing is shown as it is
    assert attest.status_label(_task(), evidence_hash="", head=HEAD) == "review"


@pytest.mark.parametrize("status", [
    "VERIFIED", "Verified", " verified ", "verified​", "ver­ified", "veri fied",
    "ｖｅｒｉｆｉｅｄ",  # full-width
    "vеrified",  # a Cyrillic e
    "verifed", "verif1ed", "verified-by-director", "verified.", "unverified",
    "Rejected", "rejected⁠", "REJECTED!",
])
def test_a_status_that_reads_as_a_verdict_needs_a_signature(status):
    # what an agent's write_file could put in the TOML: each prints like (or
    # close to) "verified" / "rejected", so it counts only with a signature
    attest.init_key(yes=True)
    forged = _round_trip(_task(status=status))
    assert forged["status"] == status and attest.claims_verdict(status)
    assert attest.status_label(forged, evidence_hash="", head=HEAD) == "unverified (unsigned)"
    assert attest.trailers(forged, evidence_hash="", head=HEAD) == []


@pytest.mark.parametrize("status,label", [
    ("open", "open"), ("needs_review", "needs_review"), ("In-Progress", "In-Progress"),
    ("", ""), (7, "7"),
    ("進行中", "unverified (unrecognised status '\\u9032\\u884c\\u4e2d')"),
    ("done (director)", "unverified (unrecognised status 'done (director)')"),
    ("x" * 70, f"unverified (unrecognised status '{'x' * 64}'...)"),
])
def test_only_a_plain_ascii_status_is_shown_as_it_is(status, label):
    assert not attest.claims_verdict(status)
    assert attest.status_label(_task(status=status), evidence_hash="", head=HEAD) == label


def test_status_label_and_trailers_need_the_current_head():
    attest.init_key(yes=True)
    signed = _round_trip(_sign())
    with pytest.raises(TypeError):
        attest.status_label(signed, evidence_hash="")  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        attest.trailers(signed, evidence_hash="")  # type: ignore[call-arg]
    for head in (None, "", "  "):
        with pytest.raises(attest.AttestError, match="current head"):
            attest.trailers(signed, evidence_hash="", head=head)  # type: ignore[arg-type]
    # commit-body as the contract spells it, after the agent kept committing
    assert attest.trailers(signed, evidence_hash="", head="b" * 40) == []
    assert attest.trailers(signed, evidence_hash="", head=HEAD)
    # head=None: the task's branch is gone (merged and deleted)
    assert attest.status_label(signed, evidence_hash="", head=None) == "verified"


def test_an_older_verified_task_restored_from_git_history_reads_stale():
    attest.init_key(yes=True)
    committed = _to_toml(_sign(head=HEAD))  # verified at HEAD, then reopened...
    restored = tomllib.loads(committed)  # ...reworked to b..., and the old file put back
    label = attest.status_label(restored, evidence_hash="", head="b" * 40)
    assert label.startswith("unverified (stale: signed at aaaaaaaaaaaa")
    assert attest.trailers(restored, evidence_hash="", head="b" * 40) == []


def test_a_reopened_task_drops_its_attestation():
    attest.init_key(yes=True)
    signed = _round_trip(_sign())
    reopened = dict(signed, status="claimed", attempts=2)
    assert attest.status_label(reopened, evidence_hash="", head=HEAD) == (
        "claimed (an earlier attestation no longer applies)"
    )
    assert attest.trailers(reopened, evidence_hash="", head=HEAD) == []
    clean = attest.drop_attestation(reopened)
    assert "attestation" not in clean and "attestation" in reopened
    assert attest.status_label(clean, evidence_hash="", head=HEAD) == "claimed"


def test_no_evidence_never_hashes_the_current_folder(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "a.txt").write_text("x", encoding="utf-8")
    assert attest.evidence_hash("") == "" == attest.evidence_hash("   ")
    (tmp_path / "b.txt").write_text("y", encoding="utf-8")
    assert attest.evidence_hash("") == ""
    assert attest.evidence_hash(".").startswith("sha256:")  # a folder named so still is


@pytest.mark.parametrize("edit", [
    lambda r: r.__setitem__("title", "Something else"),
    lambda r: r.__setitem__("status", "verified "),
    lambda r: r["criteria"].append("one more"),
    lambda r: r.__setitem__("notes", "added after signing"),
    lambda r: r.pop("branch"),
])
def test_editing_the_task_after_signing_invalidates_it(edit):
    attest.init_key(yes=True)
    signed = _round_trip(_sign())
    edit(signed)
    check = attest.verify(signed, evidence_hash="")
    assert check.state == attest.INVALID and "changed after it was signed" in check.reason
    label = attest.status_label(signed, evidence_hash="", head=HEAD)
    assert label.startswith("unverified (invalid signature")
    assert attest.trailers(signed, evidence_hash="", head=HEAD) == []


@pytest.mark.parametrize("field,value", [
    ("rounds", 9), ("verdict", "rejected"), ("signer", "eVi"), ("head", "b" * 40),
    ("evidence", "sha256:" + "0" * 64), ("signed_at", "2020-01-01T00:00:00Z"),
    ("review", {"base": "main", "exit_code": 0, "report_sha256": "0" * 64}),
    ("sig", "0" * 64),
])
def test_editing_the_attestation_invalidates_it(field, value):
    attest.init_key(yes=True)
    signed = _round_trip(_sign())
    signed["attestation"][field] = value
    check = attest.verify(signed, evidence_hash="")
    assert check.state == attest.INVALID and not check.ok


@pytest.mark.parametrize("block", [
    "verified", {"v": 1}, {"v": 2}, {"v": True},
    {"v": 1, "task": "T1", "digest": 1, "head": "h", "evidence": "", "verdict": "verified",
     "rounds": 1, "signer": "d", "signed_at": "t", "key": "k", "sig": "0" * 64},
])
def test_a_malformed_attestation_is_invalid_not_an_error(block):
    attest.init_key(yes=True)
    record = _task(status="verified", attestation=block)
    check = attest.verify(record, evidence_hash="")
    assert check.state == attest.INVALID
    assert attest.status_label(record, evidence_hash="", head=HEAD).startswith("unverified")


def test_extra_attestation_fields_are_refused():
    attest.init_key(yes=True)
    signed = _sign()
    signed["attestation"]["trusted"] = True
    assert attest.verify(signed, evidence_hash="").state == attest.INVALID


def test_an_attestation_copied_to_another_task_is_invalid():
    attest.init_key(yes=True)
    signed = _sign()
    other = _task(id="T2")
    other["status"] = "verified"
    other["attestation"] = signed["attestation"]
    check = attest.verify(other, evidence_hash="")
    assert check.state == attest.INVALID and "task 'T1'" in check.reason


def test_a_guessed_key_does_not_verify():
    kid = attest.init_key(yes=True)
    body = dict(_task(), status="verified")
    block = {"v": 1, "task": "T1", "digest": attest.task_digest(body), "head": "a" * 40,
             "evidence": "", "verdict": "verified", "rounds": 1, "review": {},
             "signer": "director", "signed_at": "2026-10-16T00:00:00Z", "key": kid}
    block["sig"] = attest._mac(os.urandom(32), block)
    body["attestation"] = block
    check = attest.verify(body, evidence_hash="")
    assert check.state == attest.INVALID and "signature does not match" in check.reason


def test_changed_evidence_invalidates_the_attestation(tmp_path):
    attest.init_key(yes=True)
    bundle = tmp_path / "evidence"
    bundle.mkdir()
    (bundle / "pytest.txt").write_text("12 passed\n", encoding="utf-8")
    (bundle / "diff.patch").write_text("+x\n", encoding="utf-8")
    ev = attest.evidence_hash(bundle)
    signed = _round_trip(_sign(evidence_hash=ev))
    assert attest.verify(signed, evidence_hash=attest.evidence_hash(bundle)).ok

    (bundle / "pytest.txt").write_text("12 passed, 1 failed\n", encoding="utf-8")
    check = attest.verify(signed, evidence_hash=attest.evidence_hash(bundle))
    assert check.state == attest.INVALID and "evidence changed" in check.reason
    assert attest.status_label(
        signed, evidence_hash=attest.evidence_hash(bundle), head=HEAD
    ).startswith("unverified (invalid signature")
    # deleting the bundle is a change too
    assert not attest.verify(signed, evidence_hash=attest.evidence_hash(tmp_path / "gone")).ok


def test_evidence_hash_covers_names_contents_and_files(tmp_path):
    assert attest.evidence_hash(None) == "" == attest.evidence_hash(tmp_path / "missing")
    bundle = tmp_path / "b"
    (bundle / "sub").mkdir(parents=True)
    (bundle / "a.txt").write_bytes(b"one")
    (bundle / "sub" / "b.txt").write_bytes(b"two")
    first = attest.evidence_hash(bundle)
    assert first.startswith("sha256:") and first == attest.evidence_hash(bundle)
    (bundle / "sub" / "b.txt").rename(bundle / "sub" / "c.txt")
    assert attest.evidence_hash(bundle) != first
    (bundle / "sub" / "c.txt").rename(bundle / "sub" / "b.txt")
    assert attest.evidence_hash(bundle) == first
    (bundle / "new.txt").write_bytes(b"")
    assert attest.evidence_hash(bundle) != first
    single = attest.evidence_hash(bundle / "a.txt")
    assert single.startswith("sha256:") and single != first


def test_a_moved_branch_head_is_stale():
    attest.init_key(yes=True)
    signed = _sign(head="a" * 40)
    check = attest.verify(signed, evidence_hash="", head="b" * 40)
    assert check.state == attest.STALE and not check.ok
    assert attest.status_label(signed, evidence_hash="", head="b" * 40).startswith(
        "unverified (stale"
    )
    assert attest.trailers(signed, evidence_hash="", head="b" * 40) == []


def test_without_the_key_nothing_reads_as_verified(monkeypatch, home):
    attest.init_key(yes=True)
    signed = _sign()
    (home / "director" / "director.key").unlink()
    assert attest.verify(signed, evidence_hash="").state == attest.NO_KEY
    label = attest.status_label(signed, evidence_hash="", head=HEAD)
    assert label.startswith("unverified (no director key")
    assert attest.trailers(signed, evidence_hash="", head=HEAD) == []


@pytest.mark.parametrize("kw,match", [
    ({"verdict": "approved"}, "unknown verdict"),
    ({"rounds": -1}, "whole number"),
    ({"rounds": True}, "whole number"),
    ({"head": ""}, "branch head is empty"),
    ({"signer": "me\nVerified-by: someone else"}, "control character"),
    ({"evidence_hash": "sha256:x\ny"}, "single-line"),
])
def test_sign_rejects_bad_input(kw, match):
    attest.init_key(yes=True)
    with pytest.raises(attest.AttestError, match=match):
        _sign(**kw)


def test_a_rejection_is_signed_too_and_gives_no_trailers():
    attest.init_key(yes=True)
    signed = _round_trip(_sign(verdict="rejected", review=None, rounds=1))
    assert signed["status"] == "rejected"
    assert attest.verify(signed, evidence_hash="").verdict == "rejected"
    assert attest.status_label(signed, evidence_hash="", head=HEAD) == "rejected"
    assert attest.trailers(signed, evidence_hash="", head=HEAD) == []


# ---- commit-body trailers ------------------------------------------------------


def test_trailers_come_only_from_a_valid_verified_attestation():
    attest.init_key(yes=True)
    signed = _round_trip(_sign(rounds=4, signer="Dustin (director)"))
    kid = attest.key_id()
    lines = attest.trailers(signed, evidence_hash="", head=HEAD)
    assert lines[:2] == [
        f"Verified-by: Dustin (director) (director key {kid})",
        "Review-rounds: 4",
    ]
    assert lines[2].startswith(f"Attestation: v1 task=T1 head={HEAD} key={kid} sig=")
    assert len(lines) == 3
    # self-reported: no attestation, or one the record no longer matches
    assert attest.trailers(_task(status="verified", rounds=4), evidence_hash="",
                           head=HEAD) == []
    signed["title"] = "edited"
    assert attest.trailers(signed, evidence_hash="", head=HEAD) == []


def _commit_message(lines: list[str]) -> str:
    return "feat: fix the thing\n\nWhat changed and why.\n\nTask: T1\n" + "\n".join(lines) + "\n"


def test_signed_trailers_verify_without_the_task_or_its_evidence(tmp_path):
    # what `evi stats --provenance` checks, long after the bundle expired
    attest.init_key(yes=True)
    bundle = tmp_path / "evidence"
    bundle.mkdir()
    (bundle / "pytest.txt").write_text("12 passed\n", encoding="utf-8")
    ev = attest.evidence_hash(bundle)
    lines = attest.trailers(_sign(evidence_hash=ev, rounds=3, signer="Dustin (director)"),
                            evidence_hash=ev, head=HEAD)
    (bundle / "pytest.txt").unlink()
    bundle.rmdir()
    check = attest.verify_trailers(_commit_message(lines))
    assert check.ok and check.task == "T1" and check.head == HEAD and check.rounds == 3
    assert check.signer == "Dustin (director)" and check.key == attest.key_id()
    # a task id that needs quoting in a trailer survives it
    odd = _sign(_task(id="loop/director attestation ä"))
    check = attest.verify_trailers(_commit_message(attest.trailers(odd, evidence_hash="",
                                                                   head=HEAD)))
    assert check.ok and check.task == "loop/director attestation ä"


@pytest.mark.parametrize("edit", [
    lambda ls: [ls[0].replace("Dustin", "eVi"), ls[1], ls[2]],
    lambda ls: [ls[0], "Review-rounds: 9", ls[2]],
    lambda ls: [ls[0], ls[1], ls[2].replace("task=T1", "task=T2")],
    lambda ls: [ls[0], ls[1], ls[2].replace(f"head={HEAD}", "head=" + "b" * 40)],
    lambda ls: [ls[0], ls[1], ls[2][:-1] + ("0" if ls[2][-1] != "0" else "1")],
    lambda ls: [ls[0], ls[1], ls[2].replace("v1 ", "v2 ")],
    lambda ls: [*ls, ls[0]],  # a second Verified-by
    lambda ls: [ls[0], ls[2]],  # no Review-rounds
    lambda ls: [ls[0], "Review-rounds: two", ls[2]],
])
def test_edited_trailers_are_invalid(edit):
    attest.init_key(yes=True)
    lines = attest.trailers(_sign(signer="Dustin (director)"), evidence_hash="", head=HEAD)
    check = attest.verify_trailers(_commit_message(edit(lines)))
    assert check.state == attest.INVALID and not check.ok


def test_self_reported_or_forged_trailers_do_not_count():
    kid = attest.init_key(yes=True)
    assert attest.verify_trailers("feat: x\n").state == attest.UNSIGNED
    # a Verified-by line with the (public) key id and nothing signed
    bare = [f"Verified-by: director (director key {kid})", "Review-rounds: 2"]
    assert attest.verify_trailers(_commit_message(bare)).state == attest.UNSIGNED
    # signed with a made-up key
    sig = attest._trailer_mac(os.urandom(32), "T1", HEAD, 2, "director", kid)
    forged = [*bare, f"Attestation: v1 task=T1 head={HEAD} key={kid} sig={sig}"]
    assert attest.verify_trailers(_commit_message(forged)).state == attest.INVALID
    # signed under another key (rotated, or another machine's)
    lines = attest.trailers(_sign(), evidence_hash="", head=HEAD)
    attest.init_key(rotate=True, yes=True)
    check = attest.verify_trailers(_commit_message(lines))
    assert check.state == attest.INVALID and "another director key" in check.reason


def test_trailers_are_not_checked_where_the_key_is_not_read(monkeypatch):
    attest.init_key(yes=True)
    lines = attest.trailers(_sign(), evidence_hash="", head=HEAD)
    monkeypatch.setenv("EVI_AGENT", "1")
    check = attest.verify_trailers(_commit_message(lines))
    assert check.state == attest.NO_KEY and not check.ok and check.task == "T1"


def test_a_verifier_reads_the_key_once_for_a_whole_list(monkeypatch):
    # `evi task list` over the whole queue: one key read (and process-tree
    # walk), not one per task
    attest.init_key(yes=True)
    records = [_round_trip(_sign(_task(id=f"T{i}"))) for i in range(5)] + [_task(status="open")]
    calls: list[int] = []
    real = attest._key_or_reason
    monkeypatch.setattr(attest, "_key_or_reason", lambda: calls.append(1) or real())
    verifier = attest.Verifier()
    labels = [verifier.status_label(r, evidence_hash="", head=HEAD) for r in records]
    assert labels == ["verified"] * 5 + ["open"]
    assert all(verifier.trailers(r, evidence_hash="", head=HEAD) for r in records[:5])
    assert len(calls) == 1
    # a status that claims nothing needs no key at all
    calls.clear()
    assert attest.status_label(_task(status="open"), evidence_hash="", head=HEAD) == "open"
    assert calls == []


# ---- the review gate -----------------------------------------------------------


def test_verify_fails_on_blocking_review_findings_unless_a_reason_is_recorded():
    attest.init_key(yes=True)
    with pytest.raises(attest.ReviewBlocked, match="blocking findings"):
        _sign(review=BLOCKING)
    with pytest.raises(attest.ReviewBlocked):
        _sign(review=BLOCKING, override_reason="   ")
    signed = _round_trip(_sign(review=BLOCKING, override_reason="false positive: x.py:12 is a test"))
    gate = signed["attestation"]["review"]
    assert gate["exit_code"] == 1 and gate["base"] == "main"
    assert gate["override"] == "false positive: x.py:12 is a test"
    assert attest.verify(signed, evidence_hash="").ok
    # the recorded reason is signed: editing it voids the attestation
    signed["attestation"]["review"]["override"] = "approved by the director"
    assert attest.verify(signed, evidence_hash="").state == attest.INVALID


def test_a_verify_needs_the_review_gate_result():
    attest.init_key(yes=True)
    with pytest.raises(attest.AttestError, match="review gate"):
        _sign(review=None)


def _git(repo: Path, *args: str) -> str:
    env = {**os.environ, "GIT_AUTHOR_NAME": "T", "GIT_AUTHOR_EMAIL": "t@x",
           "GIT_COMMITTER_NAME": "T", "GIT_COMMITTER_EMAIL": "t@x"}
    return subprocess.run(["git", *args], cwd=repo, env=env, check=True, capture_output=True,
                          text=True).stdout


def _have_git() -> bool:
    try:
        subprocess.run(["git", "--version"], capture_output=True, check=True, timeout=10)
        return True
    except (OSError, subprocess.SubprocessError):
        return False


@pytest.fixture
def repo(tmp_path) -> Path:
    if not _have_git():
        pytest.skip("git not installed")
    r = tmp_path / "repo"
    r.mkdir()
    _git(r, "-c", "init.defaultBranch=main", "init")
    (r / "x.py").write_text("a = 1\n", encoding="utf-8")
    _git(r, "add", "x.py")
    _git(r, "commit", "-m", "init")
    _git(r, "checkout", "-b", "evi/T1")
    (r / "x.py").write_text("a = 2\n", encoding="utf-8")
    _git(r, "commit", "-am", "change")
    return r


def test_review_gate_reviews_the_branch_diff_like_the_cli(repo):
    seen: list[str] = []

    def blocking(diff, *, context=""):
        seen.append(diff)
        return "## Correctness\n\nx.py:1 the new value is wrong"

    res = attest.review_gate("main", cwd=repo, reviewer=blocking)
    assert res.blocking and res.exit_code == 1 and res.base == "main"
    assert "+a = 2" in seen[0] and "-a = 1" in seen[0]
    clean = attest.review_gate("main", cwd=repo, reviewer=lambda d, context="": "fine. APPROVE")
    assert not clean.blocking
    # no changes: a clean gate, without asking a reviewer
    _git(repo, "checkout", "main")
    assert attest.review_gate("main", cwd=repo, reviewer=lambda *a, **k: 1 / 0).exit_code == 0


def test_branch_head_reads_the_commit(repo):
    head = attest.branch_head(cwd=repo)
    assert head == _git(repo, "rev-parse", "HEAD").strip() and len(head) == 40
    with pytest.raises(attest.AttestError):
        attest.branch_head("no-such-branch", cwd=repo)
    with pytest.raises(attest.AttestError):
        attest.branch_head("--output=x", cwd=repo)


# ---- who may sign --------------------------------------------------------------


@pytest.mark.parametrize("var,value", [("EVI_AGENT", "1"), ("EVI_AGENT", ""),
                                       ("EVI_AGENT", "0"), ("EVI_INTERACTIVE", "1")])
def test_an_agent_environment_refuses_even_with_yes(monkeypatch, home, var, value):
    attest.init_key(yes=True)
    monkeypatch.setenv(var, value)
    with pytest.raises(attest.AgentRefused, match=var):
        _sign(yes=True)
    with pytest.raises(attest.AgentRefused):
        attest.init_key(rotate=True, yes=True)


def test_without_yes_a_terminal_confirmation_is_needed(monkeypatch):
    attest.init_key(yes=True)
    with pytest.raises(attest.NotConfirmed, match="no terminal"):
        _sign(yes=False)
    monkeypatch.setattr(attest, "_has_tty", lambda: True)
    asked: list[str] = []
    with pytest.raises(attest.NotConfirmed, match="not confirmed"):
        _sign(yes=False, confirm=lambda q: asked.append(q) or False)
    assert "T1" in asked[0] and "verified" in asked[0]

    def eof(_q):
        raise EOFError

    with pytest.raises(attest.NotConfirmed):
        _sign(yes=False, confirm=eof)
    assert attest.verify(_sign(yes=False, confirm=lambda q: True), evidence_hash="").ok


_PYTEST = [(10, [sys.executable, "-m", "pytest", "tests/test_attest.py"]), (9, ["bash"])]


@pytest.mark.parametrize("chain,refused", [
    (DIRECTOR_CHAIN, None),
    # `uv run evi task verify T1` and `python -m evi task reject T1` from a shell
    ([(5, ["python3", "/w/.venv/bin/evi", "task", "verify", "T1"]),
      (4, ["uv", "run", "evi", "task", "verify", "T1"]), (3, ["zsh"])], None),
    ([(5, ["python", "-m", "evi", "--profile", "work", "task", "reject", "T1"]), (4, ["pwsh"])],
     None),
    # not director tooling: run_python, python -c, a test runner
    (_PYTEST, "only director tooling signs"),
    ([(5, ["python", "-c", "from evi import attest"]), (4, ["bash"])], "only director tooling"),
    ([(5, [PY, EVI_EXE, "chat"]), (4, ["bash"])], "only director tooling"),
    # an agent's shell running `evi task verify T1`, under `evi chat`
    ([*DIRECTOR_CHAIN[:2], (98, ["cmd.exe", "/c", "evi task verify T1"]),
      (97, [PY, EVI_EXE, "chat"]), (96, ["pwsh"])], "pid 97"),
    # ...under the desktop app's server, or `python -m evi serve`
    ([*DIRECTOR_CHAIN[:2], (98, ["/bin/sh", "-c", "evi task verify T1"]),
      (97, ["py", "-3.13", "-m", "uvicorn", "evi.apps.web.server:app", "--port", "8765"])],
     "pid 97"),
    ([*DIRECTOR_CHAIN[:2], (98, ["/bin/sh"]), (97, ["python", "-m", "evi", "serve"])], "pid 97"),
    # ...under the packaged desktop app's frozen sidecar server, or the app itself
    ([*DIRECTOR_CHAIN[:2], (98, ["cmd.exe", "/c", "evi task verify T1 --yes"]),
      (97, [r"C:\Program Files\eVi\evi-server\evi-server.exe", "--host", "127.0.0.1",
            "--port", "8765"]),
      (96, [r"C:\Program Files\eVi\evi-desktop.exe"])], "pid 97"),
    ([*DIRECTOR_CHAIN[:2], (98, ["/bin/sh", "-c", "evi task verify T1"]),
      (97, ["/opt/eVi/EVI-DESKTOP"])], "pid 97"),
    # a command line evi_args can't place is never taken for a launcher
    ([(5, ["sudo", "-u", "me", "evi", "task", "verify", "T1"]),
      (4, ["py", "-m", "uvicorn", "evi.apps.web.server:app"])], "pid 4"),
    # an unreadable command line can't be told apart from an agent's
    ([(5, [])], "can't be read"),
    ([], "can't be read"),
])
def test_agent_context_reads_the_process_chain(monkeypatch, chain, refused):
    monkeypatch.setattr(attest, "_process_chain", lambda: list(chain))
    why = attest.agent_context()
    if refused is None:
        assert why is None
    else:
        assert why is not None and refused in why


@pytest.mark.parametrize("how", ["env", "ancestor"])
def test_a_process_an_agent_may_have_started_never_reads_the_key(monkeypatch, how):
    kid = attest.init_key(yes=True)
    signed = _sign()
    assert attest.key_id() == kid
    if how == "env":
        monkeypatch.setenv("EVI_AGENT", "1")
    else:  # `python -c "from evi import attest; ..."` run by `evi chat`'s agent
        monkeypatch.setattr(attest, "_process_chain", lambda: [
            (5, ["python", "-c", "from evi import attest"]), (4, ["/bin/sh", "-c", "python -c x"]),
            (3, [PY, EVI_EXE, "chat"]),
        ])
    assert attest.key_id() is None and attest.key_location() == ""
    check = attest.verify(signed, evidence_hash="")
    assert check.state == attest.NO_KEY and "isn't read here" in check.reason
    label = attest.status_label(signed, evidence_hash="", head=HEAD)
    assert label.startswith("unverified (no director key")
    assert attest.trailers(signed, evidence_hash="", head=HEAD) == []


def test_a_child_process_with_evi_agent_set_gets_no_key(home):
    # What eVi's spawners are to set for an agent's processes: a real child
    # python with EVI_AGENT=1 can't read the key through eVi's own API.
    kid = attest.init_key(yes=True)
    code = ("import sys; sys.modules['keyring'] = None\n"  # never the OS keyring
            "from evi import attest; print('key id:', attest.key_id())")
    env = {**os.environ, "EVI_HOME": str(home), "EVI_AGENT": "1", "PYTHONUTF8": "1"}

    def child() -> str:
        out = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True,
                             text=True, timeout=120, cwd=str(home))
        assert out.returncode == 0, out.stderr
        return out.stdout.strip()

    assert child() == "key id: None"
    # the control: without EVI_AGENT the same child reads it, unless this test
    # run itself sits under an eVi process (then its ancestors refuse it too)
    if attest._agent_parent({}, _REAL_PROCESS_CHAIN()) is None:
        del env["EVI_AGENT"]
        assert child() == f"key id: {kid}"


def test_the_real_process_chain_starts_with_this_process(monkeypatch):
    monkeypatch.setattr(attest, "_process_chain", _REAL_PROCESS_CHAIN)
    chain = attest._process_chain()
    assert chain and chain[0][0] == os.getpid() and chain[0][1]
    # pytest is not director tooling, so signing from here is refused
    assert "only director tooling" in (attest.agent_context(env={}) or "")


# ---- an agent in yolo can't forge a verified task --------------------------------


def _results(events) -> list[ToolResult]:
    return [e for e in events if isinstance(e, ToolResult)]


def _forged_record(kid: str) -> dict:
    """What a capable agent would write: status verified and a well-formed
    attestation naming the real (public) key id, signed with a key it made up."""
    body = dict(_task(), status="verified")
    block = {"v": 1, "task": "T1", "digest": attest.task_digest(body), "head": "c" * 40,
             "evidence": "", "verdict": "verified", "rounds": 2, "review": {},
             "signer": "director", "signed_at": "2026-10-16T00:00:00Z", "key": kid}
    block["sig"] = attest._mac(os.urandom(32), block)
    return {**body, "attestation": block}


# Signing from an agent's own process by patching this module: drop
# EVI_AGENT, stub the process-tree check, sign (what code an agent runs could
# do; the guard refuses it as run_command / run_python code).
_PATCH = ("import os; os.environ.pop('EVI_AGENT', None); from evi import attest as a; "
          "a._process_chain = lambda: [(1, ['evi', 'task', 'verify', 'T1'])]; "
          "print(a.sign({'id': 'T1'}, head='c' * 40, evidence_hash='', verdict='verified', "
          "rounds=1, review=a.ReviewResult(0, 'main'), yes=True))")


def test_a_yolo_agent_cannot_produce_a_verified_task(home, tmp_path, fake_llm):
    kid = attest.init_key(yes=True)
    proj = tmp_path / "proj"
    proj.mkdir()
    key_file = str(attest.key_path())
    toml_text = _to_toml(_forged_record(kid))
    client = fake_llm([
        fake_llm.call(
            ("run_command", {"command": "EVI_DIRECTOR=1 evi task verify T1 --yes"}),
            ("run_command", {"command": "set EVI_AGENT= && evi task verify T1 --yes > marker.txt"}),
            ("run_command", {"command": "python -m evi task verify T1 --yes"}),
            ("run_command", {"command": f'type "{key_file}" > stolen.txt'}),
            ("run_command", {"command": f'python -c "{_PATCH}" > signed.txt'}),
        ),
        fake_llm.call(
            ("read_file", {"path": key_file}),
            ("write_file", {"path": key_file, "content": "my own key"}),
            ("run_python", {"code": _PATCH}),
        ),
        fake_llm.call(("write_file", {"path": ".evi/tasks/T1.toml", "content": toml_text})),
        fake_llm.text("T1 is verified."),
    ])
    cfg = Config()
    cfg.auto.mode = "yolo"
    prompts: list[str] = []
    agent = Agent(client=client, config=cfg,
                  tools=[REGISTRY[n] for n in ("run_command", "read_file", "write_file",
                                               "run_python")],
                  permission_callback=lambda name, args, cat: prompts.append(name) or True)
    agent.cwd = str(proj)

    results = _results(agent.chat("verify T1 yourself"))

    assert len(results) == 9
    for res in results[:8]:  # refused outright: the user was never asked
        assert is_permission_denial(res.output), res.output
    assert "director" in results[0].output and "director" in results[3].output
    assert "evi.attest" in results[4].output  # run_python's denial has no guard reason
    assert prompts == ["write_file"]  # only the task file, which the user approved
    for left in ("marker.txt", "stolen.txt", "signed.txt"):
        assert not (proj / left).exists(), left
    assert attest.key_id() == kid  # the key is untouched

    written = tomllib.loads((proj / ".evi" / "tasks" / "T1.toml").read_text(encoding="utf-8"))
    assert written["status"] == "verified"  # the agent wrote what it liked...
    label = attest.status_label(written, evidence_hash="", head=HEAD)
    assert label.startswith("unverified (invalid signature")  # ...it can't sign it
    assert attest.trailers(written, evidence_hash="", head=HEAD) == []
    unsigned = {k: v for k, v in written.items() if k != "attestation"}
    assert attest.status_label(unsigned, evidence_hash="", head=HEAD) == "unverified (unsigned)"


def test_no_config_widening_lets_an_agent_run_director_tooling(home, tmp_path):
    # `/auto on`, the guard switched off, its rule ids disabled, an allow glob
    # and a specific allow rule: the call is still denied (no prompt either).
    proj = tmp_path / "proj"
    proj.mkdir()
    cfg = Config()
    cfg.auto.mode = "yolo"
    cfg.auto.block_destructive = False
    cfg.auto.destructive_disable_rules = ["director-tooling", "director-env"]
    cfg.auto.destructive_allow = ["evi task verify*"]
    cfg.auto.rules = ["allow run_command evi*", "allow shell evi task*"]
    asked: list[str] = []
    agent = Agent(client=object(), config=cfg, tools=[],
                  permission_callback=lambda *a: asked.append(a[0]) or True)
    agent.cwd = str(proj)
    agent.enable_auto_all()
    run = REGISTRY["run_command"]
    for line in ("evi task verify T1 --yes", "EVI_DIRECTOR=1 evi task reject T1",
                 "evi attest init --yes"):
        assert agent._permission_decision(run, json.dumps({"command": line})) == "deny", line
    assert agent._permission_decision(run, json.dumps({"command": "evi task show T1"})) == "allow"
    assert asked == []


def test_redaction_never_queries_the_keyring_without_a_director_key(monkeypatch, tmp_path):
    """Redaction asks evi.attest for the director key on its first use. With no
    key file and no keyring marker it must not touch the OS keyring at all
    (on macOS that query can raise a Keychain prompt for every eVi user)."""
    import evi.attest as attest_mod
    from evi import redact

    monkeypatch.setattr(attest_mod, "_home", lambda: tmp_path)
    calls: list[str] = []

    class _SpyKeyring:
        def get_password(self, *a):
            calls.append("get")
            return None

    monkeypatch.setattr(attest_mod, "_keyring", lambda: _SpyKeyring())
    assert not attest_mod.key_may_exist()
    assert redact._director_key() is None
    assert calls == []

    # Once init_key has put a key in the keyring, the marker makes it readable.
    attest_mod.keyring_marker().parent.mkdir(parents=True)
    attest_mod.keyring_marker().write_text("abc\n", encoding="utf-8")
    assert attest_mod.key_may_exist()
