"""Active-skill tool scoping — restrict the toolset while a skill is in use.

A skill's SKILL.md frontmatter may declare which tools it's allowed to use:

    ---
    name: safe-reader
    description: read-only investigation
    allowed-tools: read_file, find_files, search_files
    ---

or which to forbid (`disallowed-tools: write_file, run_command`). When the model
invokes such a skill, the remaining tool rounds of that turn are scoped
accordingly — the model never sees out-of-scope tools, and a stray call to one
is refused. Mirrors Claude Code's skill allowed/disallowed-tools.

Scope is a ContextVar set by ``invoke_skill`` and read by the agent loop. The
agent clears it at the start of each turn, so a skill's scope applies for the
turn it's used (its instructions live on in history regardless).
"""

from __future__ import annotations

import contextvars

# (allowed | None, disallowed). allowed=None → no allow-list (only deny applies).
_SCOPE: contextvars.ContextVar[tuple[frozenset[str] | None, frozenset[str]] | None] = (
    contextvars.ContextVar("evi_skill_scope", default=None)
)


def activate(allowed: frozenset[str] | None, disallowed: frozenset[str]) -> None:
    _SCOPE.set((allowed, disallowed))


def clear() -> None:
    _SCOPE.set(None)


def active() -> bool:
    return _SCOPE.get() is not None


def allows(tool_name: str) -> bool:
    """Whether `tool_name` is permitted under the active skill scope (True when
    no scope is set)."""
    sc = _SCOPE.get()
    if sc is None:
        return True
    allowed, disallowed = sc
    if tool_name in disallowed:
        return False
    if allowed is not None and tool_name not in allowed:
        return False
    return True


def filter_tools(tools):
    """Keep only in-scope tools from an iterable of Tool objects."""
    return [t for t in tools if allows(t.name)]
