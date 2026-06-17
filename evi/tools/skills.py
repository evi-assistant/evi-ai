"""Skill tools — the model decides when to load an instruction packet."""

from __future__ import annotations

import json

from evi.skills import SkillStore
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
def invoke_skill(name: str) -> str:
    try:
        body, _skill_dir, resources = _store.load(name)
    except KeyError:
        return f"ERROR: no skill named '{name}'"
    # Scope the toolset for the rest of this turn if the skill declares it.
    scope_note = ""
    try:
        from evi import skillscope

        allowed, disallowed = _store.tool_scope(name)
        if allowed is not None or disallowed:
            skillscope.activate(allowed, disallowed)
            if allowed is not None:
                scope_note = "\n\n[tools scoped to: " + (", ".join(sorted(allowed)) or "none") + "]"
            elif disallowed:
                scope_note = "\n\n[tools disallowed: " + ", ".join(sorted(disallowed)) + "]"
    except Exception:  # noqa: BLE001 — scoping must never break skill loading
        pass
    if not resources:
        return body + scope_note
    shown = resources[:50]
    lines = [body, "", "---", "Bundled files for this skill (read with the file "
             "tools if the instructions reference them):"]
    lines += [f"- {p}" for p in shown]
    if len(resources) > len(shown):
        lines.append(f"- … and {len(resources) - len(shown)} more")
    return "\n".join(lines) + scope_note
