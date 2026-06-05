"""Memory tools — let the model save and retrieve persistent notes.

These wrap `evi.memory.MemoryStore` so the Agent loop can read/write user
context (preferences, project facts, contact details, …) that survives
across sessions.
"""

from __future__ import annotations

import json

from evi.memory import MemoryStore
from evi.tools.base import tool


_store = MemoryStore()


@tool(
    description=(
        "Save a piece of information to long-term memory so it persists across "
        "sessions. `name` is a short identifier (letters, digits, dash, "
        "underscore). `content` is markdown — overwrites any existing entry "
        "with the same name."
    ),
    category="memory",
)
def remember(name: str, content: str) -> str:
    path = _store.write(name, content)
    return f"saved memory '{name}' to {path}"


@tool(
    description="Retrieve the full contents of a stored memory by name.",
    category="memory",
)
def recall(name: str) -> str:
    try:
        return _store.read(name)
    except KeyError:
        return f"ERROR: no memory named '{name}'"


@tool(
    description="Delete a stored memory by name. Returns whether it existed.",
    category="memory",
)
def forget(name: str) -> str:
    removed = _store.delete(name)
    return "deleted" if removed else f"no memory named '{name}'"


@tool(
    description=(
        "List all stored memories as JSON: [{name, summary}, …]. The summary "
        "is the first non-empty line of each memory file."
    ),
    category="memory",
)
def list_memories() -> str:
    entries = _store.list()
    return json.dumps([{"name": e.name, "summary": e.summary} for e in entries])
