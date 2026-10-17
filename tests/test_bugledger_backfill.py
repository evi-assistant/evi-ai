"""The repository's bug-fix ledger is tracked and backfilled (Phase 1,
loop-task-queue): .evi/bug-ledger.jsonl holds the adversarial-review findings
fixed in 1.0.50, and search_fixes / evi.bugledger.search find them.

The 1.0.50 release commit (86a6fc7) says "17 confirmed, 4 refuted" but
documents ten of the fixes; those ten are what the ledger records (in
evi.bugledger's format: symptom, cause, fix, ts). The file is read here,
never written.
"""

from __future__ import annotations

import json
from pathlib import Path

from evi import bugledger, workdir

ROOT = Path(__file__).resolve().parents[1]
LEDGER = ROOT / ".evi" / "bug-ledger.jsonl"
DOCUMENTED = 10  # the fixes the 1.0.50 commit message lists


def _lines() -> list[str]:
    return LEDGER.read_bytes().decode("utf-8").split("\n")


def test_the_ledger_is_in_bugledgers_format():
    raw = LEDGER.read_bytes()
    assert b"\r" not in raw and raw.endswith(b"\n")  # LF, as the repository keeps text
    rows = [json.loads(line) for line in _lines() if line]
    assert rows and all(set(r) == {"symptom", "cause", "fix", "ts"} for r in rows)
    entries = bugledger.all_entries(root=ROOT)
    assert len(entries) == len(rows)  # every line is an entry the reader takes


def test_the_1_0_50_findings_are_backfilled():
    found = bugledger.search("1.0.50 adversarial review", root=ROOT, limit=50)
    assert len(found) == DOCUMENTED
    for entry in found:
        assert entry.symptom and entry.cause and entry.fix
        assert "86a6fc7" in entry.fix and entry.ts.startswith("2026-08-25")


def test_search_fixes_finds_them_by_what_went_wrong():
    """The agent's search_fixes tool, in a session whose folder is the repo."""
    from evi.tools import bugledger as tool

    token = workdir.set_cwd(str(ROOT))
    try:
        for query, expected in (
            ("openDiagnostics", "window.eviUI"),
            ("asset_size_label", "successful lookups"),
            ("request-ordering", "superseded response"),
            ("starter prompt", "actually renders"),
            ("server_path", "is_file()"),
        ):
            hits = json.loads(tool.search_fixes(query, limit=5))
            assert any(expected in h["fix"] for h in hits), (query, hits)
        everything = json.loads(tool.search_fixes("adversarial review", limit=25))
        assert len(everything) >= DOCUMENTED
    finally:
        workdir.reset(token)


def test_record_writes_lf_lines(tmp_path):
    bugledger.record("crash", "a cause", "the fix", root=tmp_path)
    bugledger.record("another", "", "fixed too", root=tmp_path)
    raw = (tmp_path / ".evi" / "bug-ledger.jsonl").read_bytes()
    assert b"\r" not in raw and raw.count(b"\n") == 2
    assert [e.symptom for e in bugledger.search("", root=tmp_path)] == ["another", "crash"]
