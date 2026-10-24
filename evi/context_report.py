"""Context-window breakdown (Phase 88).

The REPL chip and status line show how *full* the context is (`pct`). This
goes one level deeper: where the tokens actually went — system prompt, your
messages, the assistant's replies, and tool traffic (tool calls + results).

Token counts are the same ~4-chars/token estimate the agent uses for its
chip (see `evi.llm.agent._approx_tokens`), summed per category. Good enough
for "what's eating my context"; not a substitute for the real tokenizer.
"""

from __future__ import annotations

import json
from typing import Any, Iterable

# Display order of the buckets. "schemas" is the tool schemas that go with
# every request (counted when context_breakdown is given the tools).
BUCKETS = ("system", "schemas", "user", "assistant", "tools")


def _text_chars(m: dict[str, Any]) -> int:
    content = m.get("content")
    if isinstance(content, str):
        return len(content)
    if isinstance(content, list):
        return sum(
            len(p.get("text") or "")
            for p in content
            if isinstance(p, dict) and p.get("type") == "text"
        )
    return 0


def _toolcall_chars(m: dict[str, Any]) -> int:
    total = 0
    for tc in m.get("tool_calls") or []:
        fn = tc.get("function", {})
        total += len(fn.get("name", "")) + len(fn.get("arguments", ""))
    return total


def tool_schema_tokens(tools: Iterable[Any]) -> dict[str, int]:
    """Estimated tokens of the tool schemas that go with every request, per
    group: a built-in tool's category ("fs", "code", ...), or "mcp:<server>"
    for an MCP tool (named "<server>.<tool>"). Largest group first; the same
    ~4 chars/token estimate as the buckets."""
    chars: dict[str, int] = {}
    for tool in tools:
        try:
            size = len(json.dumps(tool.openai_schema(), ensure_ascii=False))
        except (AttributeError, TypeError, ValueError):
            continue  # not a tool, or a schema that can't be sent anyway
        name = str(getattr(tool, "name", ""))
        category = str(getattr(tool, "category", "") or "other")
        group = f"mcp:{name.split('.', 1)[0]}" if category == "mcp" and "." in name else category
        chars[group] = chars.get(group, 0) + size
    return dict(sorted(((g, c // 4) for g, c in chars.items()), key=lambda kv: (-kv[1], kv[0])))


def context_breakdown(
    history: list[dict[str, Any]], ceiling: int, tools: Iterable[Any] = ()
) -> dict[str, Any]:
    """Categorise `history` tokens into system/user/assistant/tools buckets.

    The schemas of `tools` (the session's tools) go into the schemas bucket.
    Returns a dict with `buckets` (token count per category), `used` (their
    sum), `ceiling`, `pct`, `messages`, `schema_groups`, and per-bucket
    `pct_of_used`.
    """
    chars = {b: 0 for b in BUCKETS}
    for m in history:
        role = m.get("role")
        if role == "system":
            chars["system"] += _text_chars(m)
        elif role == "user":
            chars["user"] += _text_chars(m)
        elif role == "assistant":
            chars["assistant"] += _text_chars(m)
            chars["tools"] += _toolcall_chars(m)  # the model's function calls
        elif role == "tool":
            chars["tools"] += _text_chars(m)  # tool results returned to the model

    buckets = {b: chars[b] // 4 for b in BUCKETS}
    schema_groups = tool_schema_tokens(tools)
    buckets["schemas"] = sum(schema_groups.values())
    used = sum(buckets.values())
    ceiling = int(ceiling or 0)
    return {
        "buckets": buckets,
        "used": used,
        "ceiling": ceiling,
        "pct": (used * 100 // ceiling) if ceiling else 0,
        "messages": len(history),
        "schema_groups": schema_groups,
        "pct_of_used": {
            b: (buckets[b] * 100 // used) if used else 0 for b in BUCKETS
        },
    }
