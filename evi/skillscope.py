"""Active-skill tool scoping — restrict the toolset while a skill is in use.

A skill's SKILL.md frontmatter may declare which tools it's allowed to use:

    ---
    name: safe-reader
    description: read-only investigation
    allowed-tools: read_file, find_files, search_files
    ---

or which to forbid (`disallowed-tools: write_file, run_command`). When the model
invokes such a skill, the rest of that turn is scoped accordingly: the skill's
result tells the model which tools it may use, and a call to any other is
refused at dispatch. That includes the other calls the model made in the same
batch as `invoke_skill`, which run alongside it (the loop reads the scope a
call will set with :func:`peek` before running the batch). The tool list sent
to the model stays the same (a list that changed mid-turn would make local
backends re-process the whole prompt). Mirrors Claude Code's skill
allowed/disallowed-tools; Claude Code tool names (`Read`, `Bash(git:*)`, …)
are mapped to eVi's (:func:`normalize_names`).

The scope is per-agent state (``Agent.active_skill_scope``), not a ContextVar:
tool bodies run on pool threads, and nothing a tool writes into its own context
ever reaches the agent loop. ``invoke_skill`` returns the scope on its result
(:class:`ScopedOutput`) and the loop applies it once the tool round is in. The
agent clears it at the start of each turn, so a skill's scope applies for the
turn it's used (its instructions live on in history regardless). A second
scoped skill in the same turn can only narrow the scope, never widen it.

A subagent spawned while a scope is active runs under it too: the agent binds
its scope around each tool call (:func:`bind`, flowing down only, like
``evi.hooks.bind``) and ``run_subagent`` makes it the subagent's
``inherited_skill_scope``, a floor its own turns never clear.
"""

from __future__ import annotations

import contextvars
import re
from dataclasses import dataclass
from typing import Callable, Iterable

from evi.citations import ToolOutput

# (allowed | None, disallowed). allowed=None → no allow-list (only deny applies).
Scope = tuple[frozenset[str] | None, frozenset[str]]


@dataclass
class ScopedOutput(ToolOutput):
    """A tool result that also asks the agent to scope the rest of the turn."""

    scope: Scope | None = None


def scope_of(output: object) -> Scope | None:
    """The scope a tool result carries, or None (any other result)."""
    return output.scope if isinstance(output, ScopedOutput) else None


def narrow(current: Scope | None, new: Scope | None) -> Scope | None:
    """Combine an active scope with a newly requested one. Allow-lists
    intersect and deny-lists add up, so a later skill can't re-enable a tool an
    earlier one withheld: widening takes a new turn."""
    if new is None:
        return current
    if current is None:
        return new
    allowed_a, denied_a = current
    allowed_b, denied_b = new
    if allowed_a is None:
        allowed = allowed_b
    elif allowed_b is None:
        allowed = allowed_a
    else:
        allowed = allowed_a & allowed_b
    return (allowed, denied_a | denied_b)


def allows(tool_name: str, scope: Scope | None) -> bool:
    """Whether `tool_name` is permitted under `scope` (True when no scope)."""
    if scope is None:
        return True
    allowed, disallowed = scope
    if tool_name in disallowed:
        return False
    if allowed is not None and tool_name not in allowed:
        return False
    return True


def describe(scope: Scope) -> str:
    """The note appended to the skill body so the model knows its toolset."""
    allowed, disallowed = scope
    if allowed is not None:
        return "[tools scoped to: " + (", ".join(sorted(allowed)) or "none") + "]"
    return "[tools disallowed: " + ", ".join(sorted(disallowed)) + "]"


# --- Claude Code tool names --------------------------------------------------
#
# Imported Claude skills name Claude Code's tools. Each maps to the eVi tools
# doing the same job; a `Bash(git status:*)` pattern maps to the whole tool
# (eVi can't scope a tool to some of its arguments).

CLAUDE_TOOLS: dict[str, tuple[str, ...]] = {
    "Read": ("read_file",),
    "Write": ("write_file",),
    "Edit": ("edit_file", "apply_patch"),
    "MultiEdit": ("edit_file", "apply_patch"),
    "Bash": ("run_command",),
    "Glob": ("find_files",),
    "Grep": ("search_files",),
    "LS": ("list_dir",),
    "WebFetch": ("web_fetch",),
    "WebSearch": ("web_search",),
    "Task": ("delegate", "delegate_explore", "delegate_plan", "parallel_research"),
    "Agent": ("delegate", "delegate_explore", "delegate_plan", "parallel_research"),
    "Skill": ("invoke_skill",),
}

_TOOL_NAME = re.compile(r"[A-Za-z0-9_.\-]+")


def normalize_names(names: Iterable[str]) -> frozenset[str]:
    """Frontmatter tool names as eVi tool names: Claude Code's are mapped
    (`Read` → read_file, `Bash(git:*)` → run_command) and eVi's kept. A
    fragment that can't be a tool name (the `status:*)` left when the parser
    split `Bash(git status:*)` on its space) is dropped."""
    out: set[str] = set()
    for raw in names:
        base = str(raw).split("(", 1)[0].strip()
        if not _TOOL_NAME.fullmatch(base):
            continue
        out.update(CLAUDE_TOOLS.get(base, (base,)))
    return frozenset(out)


def fit(scope: Scope | None, known_tools: Iterable[str]) -> Scope | None:
    """A skill's scope as eVi understands it, given every tool name it knows.
    An allow-list that names none of them (a Claude skill listing only
    Claude-only tools such as TodoWrite, say) is not understood and is ignored
    rather than locking every tool out. One naming a known tool is kept even
    if this agent lacks that tool, and one written empty on purpose still
    allows nothing. The deny-list always applies."""
    if scope is None:
        return None
    allowed, denied = scope
    if allowed and not (allowed & set(known_tools)):
        allowed = None
    if allowed is None and not denied:
        return None
    return (allowed, denied)


# --- scoping the rest of a batch ---------------------------------------------
#
# The model can call invoke_skill and other tools in one batch; they all run at
# once, so the skill's result comes too late to stop the others. A tool whose
# result scopes the turn registers a function that reads the scope a call will
# set from its arguments, without running it (tools/skills.py registers
# invoke_skill's), and the loop checks the rest of the batch against it.

_PEEKERS: dict[Callable[..., object], Callable[[str], Scope | None]] = {}


def register_peek(func: Callable[..., object], peek_fn: Callable[[str], Scope | None]) -> None:
    """Register `peek_fn(args_json)` for the tool whose function is `func`."""
    _PEEKERS[func] = peek_fn


def peek(tool: object, args_json: str) -> Scope | None:
    """The scope a call to `tool` with `args_json` will set, or None."""
    fn = _PEEKERS.get(getattr(tool, "func", None))  # type: ignore[arg-type]
    if fn is None:
        return None
    try:
        return fn(args_json)
    except Exception:  # noqa: BLE001 — a peek must never break dispatch
        return None


# --- handing the scope down to subagents --------------------------------------
#
# Set by the agent around each tool call (on that call's own copied context,
# from its own scope), so a tool that spawns a subagent hands the scope on. It
# only ever flows down: nothing reads it back into the agent loop.

_INHERITED: contextvars.ContextVar[Scope | None] = contextvars.ContextVar(
    "evi_inherited_skill_scope", default=None
)


def bind(scope: Scope | None) -> contextvars.Token:
    """Make `scope` the one a subagent spawned here inherits. Returns a token
    for :func:`unbind`."""
    return _INHERITED.set(scope)


def unbind(token: contextvars.Token | None) -> None:
    if token is not None:
        try:
            _INHERITED.reset(token)
        except (ValueError, LookupError):
            pass


def inherited() -> Scope | None:
    """The scope of the agent whose tool is running here, or None."""
    return _INHERITED.get()
