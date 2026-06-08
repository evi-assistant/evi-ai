"""Fine-tune dataset export from transcripts (Phase 90).

Turns your stored sessions (``~/.evi/transcripts/``) into a JSONL dataset in the
standard chat fine-tune format — one conversation per line:

    {"messages": [{"role": "user", ...}, {"role": "assistant", ...}, ...]}

This is the *curation + export* half; training stays off-device and optional
(feed the JSONL to OpenAI/Together/Axolotl/etc., or to a local LoRA run). Tool
calls are dropped by default (many fine-tune APIs reject the ``tool`` role);
pass ``include_tools`` to keep them for tool-use fine-tuning.

Everything is a pure transform over reconstructed history, so it's testable
without a model and never touches the network.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from evi.sessions import history_from_transcript, list_sessions


def _text(m: dict[str, Any]) -> str:
    """Flatten a message's content to plain text (ignores image parts)."""
    c = m.get("content")
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        return "".join(
            p.get("text", "")
            for p in c
            if isinstance(p, dict) and p.get("type") == "text"
        )
    return ""


def history_to_example(
    history: list[dict[str, Any]],
    *,
    system: str | None = None,
    include_tools: bool = False,
) -> dict[str, Any] | None:
    """Convert one reconstructed history into a fine-tune example.

    Returns ``{"messages": [...]}`` or None when the conversation has no usable
    user→assistant exchange (so empty/degenerate sessions are skipped).
    """
    msgs: list[dict[str, Any]] = []
    if system:
        msgs.append({"role": "system", "content": system})

    n_user = n_asst = 0
    for m in history:
        role = m.get("role")
        if role == "user":
            t = _text(m).strip()
            if not t:
                continue
            msgs.append({"role": "user", "content": t})
            n_user += 1
        elif role == "assistant":
            t = _text(m).strip()
            tcs = m.get("tool_calls")
            if include_tools and tcs:
                msgs.append({"role": "assistant", "content": t, "tool_calls": tcs})
                n_asst += 1
            elif t:
                msgs.append({"role": "assistant", "content": t})
                n_asst += 1
            # else: a pure tool-call turn with no text — dropped when not including tools
        elif role == "tool" and include_tools:
            msgs.append(
                {
                    "role": "tool",
                    "name": m.get("name", ""),
                    "tool_call_id": m.get("tool_call_id", ""),
                    "content": _text(m),
                }
            )

    if n_user == 0 or n_asst == 0:
        return None
    return {"messages": msgs}


def export_dataset(
    out_path: str | Path,
    *,
    root: Path | None = None,
    sessions: list[str] | None = None,
    days: int | None = None,
    limit: int = 10000,
    min_user_turns: int = 1,
    system: str | None = None,
    include_tools: bool = False,
) -> tuple[int, int]:
    """Write a JSONL fine-tune dataset from transcripts.

    Returns ``(examples_written, sessions_considered)``.
    """
    infos = list_sessions(root=root, days=days, limit=limit)
    if sessions:
        want = set(sessions)
        infos = [i for i in infos if i.session_id in want]

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    with out_path.open("w", encoding="utf-8") as f:
        for info in infos:
            ex = history_to_example(
                history_from_transcript(info.path),
                system=system,
                include_tools=include_tools,
            )
            if ex is None:
                continue
            n_user = sum(1 for m in ex["messages"] if m["role"] == "user")
            if n_user < min_user_turns:
                continue
            f.write(json.dumps(ex, ensure_ascii=False) + "\n")
            written += 1
    return written, len(infos)
