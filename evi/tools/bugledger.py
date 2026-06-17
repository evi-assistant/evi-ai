"""Bug-fix ledger tools — record fixes and look them up before retrying.

Wraps :mod:`evi.bugledger` so the agent can persist "symptom → cause → fix"
notes per project and, crucially, *search* them before re-attempting a repair —
avoiding re-deriving (or re-breaking) something already solved.
"""

from __future__ import annotations

import json

from evi import bugledger
from evi.tools.base import tool


@tool(
    description=(
        "Record a resolved bug in the project's fix ledger so the knowledge "
        "persists across sessions. `symptom` = how it manifested, `cause` = the "
        "root cause you found, `fix` = what actually fixed it. Call this after "
        "confirming a fix works."
    ),
    category="memory",
)
def record_fix(symptom: str, cause: str, fix: str) -> str:
    try:
        path = bugledger.record(symptom, cause, fix)
    except ValueError as exc:
        return f"ERROR: {exc}"
    return f"recorded fix to {path}"


@tool(
    description=(
        "Search the project's bug-fix ledger for past fixes before attempting a "
        "repair. Returns up to `limit` matching entries (symptom/cause/fix) as "
        "JSON, newest first. Empty query returns the most recent fixes. Check "
        "this when a bug looks familiar — it may already be solved."
    ),
    category="memory",
)
def search_fixes(query: str = "", limit: int = 5) -> str:
    limit = max(1, min(int(limit), 25))
    hits = bugledger.search(query, limit=limit)
    if not hits:
        return "no matching fixes in the ledger"
    return json.dumps([
        {"symptom": e.symptom, "cause": e.cause, "fix": e.fix, "ts": e.ts}
        for e in hits
    ])
