"""Skill tools — the model decides when to load an instruction packet."""

from __future__ import annotations

import json

from evi import skillscope
from evi.skills import SkillStore
from evi.skillscope import ScopedOutput
from evi.tools.base import tool


_store = SkillStore()


@tool(
    description=(
        "List installed skills as JSON: [{name, description}, …]. Skills are "
        "named markdown instruction packets installed under "
        "~/.evi/skills/<name>/SKILL.md."
    ),
    category="skills",
)
def list_skills() -> str:
    entries = _store.list()
    return json.dumps([{"name": e.name, "description": e.description} for e in entries])


@tool(
    description=(
        "Load the full instructions for a named skill. Call this when the "
        "skill's index entry suggests it would help with the current task; "
        "then follow the instructions returned. If the skill bundles companion "
        "files, their absolute paths are listed at the end — read them with the "
        "file tools when the instructions refer to them."
    ),
    category="skills",
)
def invoke_skill(name: str) -> str | ScopedOutput:
    try:
        body, _skill_dir, resources = _store.load(name)
    except KeyError:
        return f"ERROR: no skill named '{name}'"
    # Scope the toolset for the rest of this turn if the skill declares it. This
    # runs on a tool thread, so the scope rides back on the result and the agent
    # loop applies it (evi/skillscope.py).
    scope = _skill_scope(name)
    scope_note = "\n\n" + skillscope.describe(scope) if scope is not None else ""
    if not resources:
        return _scoped(body + scope_note, scope)
    shown = resources[:50]
    lines = [body, "", "---", "Bundled files for this skill (read with the file "
             "tools if the instructions reference them):"]
    lines += [f"- {p}" for p in shown]
    if len(resources) > len(shown):
        lines.append(f"- … and {len(resources) - len(shown)} more")
    return _scoped("\n".join(lines) + scope_note, scope)


def _scoped(text: str, scope: skillscope.Scope | None) -> str | ScopedOutput:
    return text if scope is None else ScopedOutput(text=text, scope=scope)


def _skill_scope(name: str) -> skillscope.Scope | None:
    """The tool scope skill `name` declares (Claude Code tool names mapped to
    eVi's), or None when it declares none or can't be read."""
    try:
        allowed, disallowed = _store.tool_scope(name)
    except Exception:  # noqa: BLE001 — scoping must never break skill loading
        return None
    names = None if allowed is None else skillscope.normalize_names(allowed)
    if allowed and not names:
        names = None  # entries, but none that could name a tool: not understood
    denied = skillscope.normalize_names(disallowed)
    if names is None and not denied:
        return None
    return (names, denied)


def _peek_scope(args_json: str) -> skillscope.Scope | None:
    """The scope an `invoke_skill` call will set, read from its arguments
    without loading the skill, so the loop can scope the rest of its batch."""
    args = json.loads(args_json) if args_json else {}
    name = args.get("name") if isinstance(args, dict) else None
    return _skill_scope(name) if isinstance(name, str) and name else None


skillscope.register_peek(invoke_skill, _peek_scope)
