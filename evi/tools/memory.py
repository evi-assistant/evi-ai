"""Memory tools — let the model save and retrieve persistent notes.

These wrap `evi.memory.MemoryStore` so the Agent loop can read/write user
context (preferences, project facts, contact details, …) that survives
across sessions.

Each call works on the calling session's store (`_store_for`): in multi-user
web mode that is the login's own ``HOME/users/<name>/memory``, the same store
its system-prompt index lists, never the shared one or another user's.
"""

from __future__ import annotations

import json
from pathlib import Path

from evi.memory import MemoryStore
from evi.tools.base import ToolContext, tool


# The shared default store (~/.evi/memory): single-user sessions, and calls
# made outside an agent (`evi mcp serve`, direct calls).
_store = MemoryStore()


def _store_for(ctx: ToolContext | None) -> MemoryStore:
    """The store a call works on: the session user's (``user_root/memory``,
    multi-user web mode), else the calling agent's own (the one its prompt
    index lists), else the shared default."""
    if ctx is None:
        return _store
    mem = getattr(ctx.agent, "memory", None)
    if ctx.user_root is not None:
        root = Path(ctx.user_root) / "memory"
        if isinstance(mem, MemoryStore) and mem.root == root:
            return mem
        return MemoryStore(root=root)
    if isinstance(mem, MemoryStore):
        return mem
    return _store


@tool(
    description=(
        "Save a piece of information to long-term memory so it persists across "
        "sessions. `name` is a short identifier (letters, digits, dash, "
        "underscore). `content` is markdown — overwrites any existing entry "
        "with the same name. `tags` is an optional comma-separated list "
        "(e.g. \"work, project-x\") for grouping + retrieval via recall_by_tag; "
        "leave it empty to keep any existing tags."
    ),
    category="memory",
)
def remember(name: str, content: str, tags: str = "", ctx: ToolContext | None = None) -> str:
    store = _store_for(ctx)
    tag_list = [t.strip() for t in tags.split(",") if t.strip()] if tags.strip() else None
    path = store.write(name, content, tags=tag_list)
    saved = store.tags_of(name)
    note = f" [tags: {', '.join(saved)}]" if saved else ""
    return f"saved memory '{name}' to {path}{note}"


@tool(
    description="Retrieve the full contents of a stored memory by name.",
    category="memory",
)
def recall(name: str, ctx: ToolContext | None = None) -> str:
    try:
        return _store_for(ctx).read(name)
    except KeyError:
        return f"ERROR: no memory named '{name}'"


@tool(
    description="Delete a stored memory by name. Returns whether it existed.",
    category="memory",
)
def forget(name: str, ctx: ToolContext | None = None) -> str:
    removed = _store_for(ctx).delete(name)
    return "deleted" if removed else f"no memory named '{name}'"


@tool(
    description=(
        "List all stored memories as JSON: [{name, summary, tags}, …]. The "
        "summary is the first non-empty line of each memory file."
    ),
    category="memory",
)
def list_memories(ctx: ToolContext | None = None) -> str:
    entries = _store_for(ctx).list()
    return json.dumps(
        [{"name": e.name, "summary": e.summary, "tags": list(e.tags)} for e in entries]
    )


@tool(
    description=(
        "Find stored memories carrying a given tag (case-insensitive). Returns "
        "JSON: [{name, summary, tags}, …]. Pair with the `tags` arg of remember."
    ),
    category="memory",
)
def recall_by_tag(tag: str, ctx: ToolContext | None = None) -> str:
    entries = _store_for(ctx).by_tag(tag)
    return json.dumps(
        [{"name": e.name, "summary": e.summary, "tags": list(e.tags)} for e in entries]
    )
