"""Bug-fix ledger — a searchable record of past fixes, per project.

When an agent re-attempts a repair it often re-discovers the same root cause from
scratch (or worse, re-introduces a fix that already failed). The ledger is a tiny
append-only log of "symptom → root cause → fix" the agent can search BEFORE
trying again, so hard-won debugging knowledge persists across sessions.

Stored as JSONL at ``<root>/.evi/bug-ledger.jsonl`` (per-project: bugs are
project-specific). Backs the ``record_fix`` / ``search_fixes`` tools.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

LEDGER_REL = Path(".evi") / "bug-ledger.jsonl"


@dataclass
class FixEntry:
    symptom: str
    cause: str
    fix: str
    ts: str = ""


def _root(root: str | Path | None) -> Path:
    if root is not None:
        return Path(root)
    # Honour the per-session working folder when set, else cwd.
    try:
        from evi import workdir

        return Path(workdir.get_cwd())
    except Exception:
        return Path.cwd()


def ledger_path(root: str | Path | None = None) -> Path:
    return _root(root) / LEDGER_REL


def record(symptom: str, cause: str, fix: str, *, root: str | Path | None = None) -> Path:
    """Append one fix to the ledger and return its path. Raises ValueError if
    symptom or fix is empty (a useless entry)."""
    if not symptom.strip() or not fix.strip():
        raise ValueError("record needs a non-empty symptom and fix")
    entry = FixEntry(
        symptom=symptom.strip(), cause=cause.strip(), fix=fix.strip(),
        ts=datetime.now().isoformat(timespec="seconds"),
    )
    p = ledger_path(root)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(asdict(entry), ensure_ascii=False) + "\n")
    return p


def all_entries(root: str | Path | None = None) -> list[FixEntry]:
    """Every recorded fix, newest last. Skips malformed lines."""
    p = ledger_path(root)
    if not p.is_file():
        return []
    out: list[FixEntry] = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(d, dict) and d.get("symptom") and d.get("fix"):
            out.append(FixEntry(
                symptom=str(d.get("symptom", "")), cause=str(d.get("cause", "")),
                fix=str(d.get("fix", "")), ts=str(d.get("ts", "")),
            ))
    return out


def search(query: str, *, root: str | Path | None = None, limit: int = 5) -> list[FixEntry]:
    """Case-insensitive substring match over symptom/cause/fix, newest first.
    Empty query returns the most recent entries."""
    entries = list(reversed(all_entries(root)))
    q = query.strip().lower()
    if not q:
        return entries[:limit]
    hits = [e for e in entries if q in f"{e.symptom} {e.cause} {e.fix}".lower()]
    return hits[:limit]
