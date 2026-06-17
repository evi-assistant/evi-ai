"""Session reflection — distill durable lessons from a conversation into memory.

eVi's memory is model-decided: the agent calls `remember` when it notices
something worth keeping. Reflection is the complementary pass — after a session,
look back over the whole conversation and extract the *corrections* and
*durable preferences* the user expressed that the agent may not have saved in the
moment ("no, always use X", "we don't do Y here"). Those become memories so the
next session starts smarter.

The LLM call is injected (`run_one(prompt) -> text`, like `evals.make_runners`)
so this module stays model-agnostic and unit-testable. Off by default — invoked
explicitly via `evi reflect` (or wired into a `session_end` hook).
"""

from __future__ import annotations

import json
import re

_PROMPT = """\
Review this conversation between a user and an AI assistant. Extract ONLY durable,
reusable facts the user expressed that should persist to future sessions —
especially CORRECTIONS ("no, do X instead"), standing PREFERENCES ("always …",
"never …"), and project conventions. Ignore one-off task details, transient
context, and anything already obvious.

Return a JSON array (and nothing else) of objects:
  [{"name": "kebab-case-slug", "content": "the durable fact, imperative", "tags": "comma,tags"}]
Return [] if there is nothing worth keeping.

CONVERSATION:
__CONVO__
"""

_MAX_CONVO_CHARS = 12_000


def _render_convo(messages: list[dict]) -> str:
    lines: list[str] = []
    for m in messages:
        role = m.get("role", "")
        content = m.get("content", "")
        if not isinstance(content, str) or not content.strip():
            continue
        if role not in ("user", "assistant"):
            continue
        lines.append(f"{role.upper()}: {content.strip()}")
    convo = "\n\n".join(lines)
    if len(convo) > _MAX_CONVO_CHARS:
        convo = convo[-_MAX_CONVO_CHARS:]  # keep the most recent exchanges
    return convo


def _slugify(name: str) -> str:
    """Coerce a model-supplied name into a valid memory slug (so a non-slug
    name normalises instead of being silently dropped by MemoryStore)."""
    s = re.sub(r"[^a-z0-9_-]+", "-", name.strip().lower()).strip("-")
    return s[:64]


def _extract_json_array(text: str) -> list[dict]:
    """Pull the first valid JSON array of objects out of a model reply.

    A greedy ``\\[.*\\]`` breaks when prose around the array contains stray
    brackets ("see [docs]: [{…}]"), so instead try to decode an array at each
    ``[`` and return the first that parses to a list — tolerant of fences/prose."""
    if not text:
        return []
    cleaned = text.replace("```json", "").replace("```", "")
    decoder = json.JSONDecoder()
    for i, ch in enumerate(cleaned):
        if ch != "[":
            continue
        try:
            data, _end = decoder.raw_decode(cleaned[i:])
        except json.JSONDecodeError:
            continue
        if isinstance(data, list):
            return [d for d in data if isinstance(d, dict)]
    return []


def reflect(messages: list[dict], *, run_one, store=None) -> list[str]:
    """Reflect over `messages`, writing durable lessons to memory.

    `run_one(prompt) -> str` performs the LLM call. Returns the list of memory
    names written. Never raises — a flaky model / bad JSON yields []."""
    convo = _render_convo(messages)
    if not convo:
        return []
    try:
        reply = run_one(_PROMPT.replace("__CONVO__", convo))
    except Exception:
        return []
    items = _extract_json_array(reply or "")
    if not items:
        return []

    if store is None:
        from evi.memory import MemoryStore

        store = MemoryStore()

    def _exists(n: str) -> bool:
        try:
            store.read(n)
            return True
        except Exception:
            return False

    written: list[str] = []
    seen: set[str] = set()
    for item in items:
        name = _slugify(str(item.get("name") or ""))
        content = str(item.get("content") or "").strip()
        if not name or not content:
            continue
        raw_tags = str(item.get("tags") or "")
        tags = [t.strip() for t in raw_tags.split(",") if t.strip()]
        if "reflected" not in tags:
            tags.append("reflected")
        # Don't clobber a hand-authored memory (reflection is automatic): if the
        # name already exists on disk (and we didn't create it this run), write
        # under a "-reflected" variant so the original survives.
        target = name
        if target not in seen and _exists(target):
            target = f"{name}-reflected"[:64]
        while target in seen:  # in-batch dedupe — two items, same slug
            target = f"{target}-2"[:64]
        try:
            store.write(target, content, tags=tags)
            written.append(target)
            seen.add(target)
        except Exception:
            continue
    return written
