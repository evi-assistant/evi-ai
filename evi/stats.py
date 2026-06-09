"""Local usage analytics — computed from transcripts, printed locally.

Reads ``~/.evi/transcripts/`` and aggregates: session/message counts, a
role breakdown, the most-used tools, the busiest days, and a rough token volume
(chars/4 — not a tokenizer). Everything stays on disk; this is deliberately the
*local* counterpart to a cloud analytics dashboard (which eVi does not do).
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any


def _read_entries(path: Path) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict):
                out.append(obj)
    except OSError:
        pass
    return out


def compute_stats(*, root: Path | None = None, days: int | None = None) -> dict[str, Any]:
    """Aggregate transcript stats. Returns a dict (printed by `evi stats`)."""
    from evi.sessions import list_sessions

    infos = list_sessions(root=root, days=days, limit=100_000)
    roles: Counter[str] = Counter()
    tools: Counter[str] = Counter()
    per_day: Counter[str] = Counter()
    total_msgs = 0
    char_total = 0
    first_ts: float | None = None
    last_ts: float | None = None

    for info in infos:
        per_day[info.day] += 1
        for e in _read_entries(info.path):
            role = str(e.get("role", "?"))
            roles[role] += 1
            total_msgs += 1
            content = e.get("content")
            if isinstance(content, str):
                char_total += len(content)
            ts = e.get("ts")
            if isinstance(ts, (int, float)):
                first_ts = ts if first_ts is None else min(first_ts, ts)
                last_ts = ts if last_ts is None else max(last_ts, ts)
            # Count actual tool executions (tool-result entries carry tool_name).
            if role == "tool" and e.get("tool_name"):
                tools[str(e["tool_name"])] += 1

    return {
        "sessions": len(infos),
        "messages": total_msgs,
        "roles": dict(roles),
        "tools": dict(tools.most_common()),
        "busiest_days": dict(per_day.most_common(7)),
        "approx_tokens": char_total // 4,
        "first_ts": first_ts,
        "last_ts": last_ts,
    }
