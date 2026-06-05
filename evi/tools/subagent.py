"""Delegate tools — hand a task to a scoped sub-Agent and return its report.

Each `delegate_*` tool corresponds to one pre-baked profile in
`evi.llm.subagent.SUBAGENT_PROFILES`. The sub-agent gets its own LLM
conversation; its final assistant text becomes the tool result.
"""

from __future__ import annotations

from evi.llm.subagent import SUBAGENT_PROFILES, run_subagent
from evi.tools.base import tool


@tool(
    description=(
        "Delegate a read-only investigation to an Explore subagent. Use this "
        "to answer questions like 'where is X defined' or 'what files "
        "reference Y' without filling the main conversation with file dumps. "
        "Returns the subagent's findings as text."
    ),
    category="subagent",
)
def delegate_explore(task: str) -> str:
    profile = SUBAGENT_PROFILES["explore"]
    return run_subagent(
        system_prompt=str(profile["system_prompt"]),
        task=task,
        tool_categories=profile["tool_categories"],  # type: ignore[arg-type]
    )


@tool(
    description=(
        "Delegate planning to a Plan subagent. Use this for designing an "
        "implementation strategy before writing code. Returns a numbered "
        "plan with files-to-touch and trade-offs."
    ),
    category="subagent",
)
def delegate_plan(task: str) -> str:
    profile = SUBAGENT_PROFILES["plan"]
    return run_subagent(
        system_prompt=str(profile["system_prompt"]),
        task=task,
        tool_categories=profile["tool_categories"],  # type: ignore[arg-type]
    )
