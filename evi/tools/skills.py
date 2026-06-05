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
        "then follow the instructions returned."
    ),
    category="skills",
)
def invoke_skill(name: str) -> str:
    try:
        return _store.read(name)
    except KeyError:
        return f"ERROR: no skill named '{name}'"
