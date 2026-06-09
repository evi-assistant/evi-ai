"""Delegate tools — hand a task to a scoped sub-Agent and return its report.

Each `delegate_*` tool corresponds to one pre-baked profile in
`evi.llm.subagent.SUBAGENT_PROFILES`. The sub-agent gets its own LLM
conversation; its final assistant text becomes the tool result.
"""

from __future__ import annotations

from evi.llm.subagent import (
    SUBAGENT_PROFILES,
    get_profile,
    run_subagent,
    run_subagents_parallel,
)
from evi.tools.base import tool

_MAX_PARALLEL = 6


@tool(
    description=(
        "Delegate a task to a named subagent profile. Built-in profiles: "
        "'explore' (read-only investigation), 'plan' (planning). Plugins add "
        "more, named '<plugin>:<name>'. Pass the profile name + the task; "
        "returns the subagent's report. Run `evi agents` to see options."
    ),
    category="subagent",
)
def delegate(profile: str, task: str) -> str:
    p = get_profile(str(profile).strip())
    if p is None:
        return (
            f"ERROR: unknown subagent profile {profile!r}. "
            "Run `evi agents` to list available profiles."
        )
    return run_subagent(
        system_prompt=str(p["system_prompt"]),
        task=task,
        tool_categories=p.get("tool_categories", ()),  # type: ignore[arg-type]
    )


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


@tool(
    description=(
        "Research several sub-questions in PARALLEL. Pass a list of focused, "
        "independent sub-questions; each runs at the same time as its own "
        "read-only Explore subagent, and their findings are combined into one "
        f"report. Best for broad investigations you can split up (max "
        f"{_MAX_PARALLEL} at once). Synthesize the combined findings yourself."
    ),
    category="subagent",
    long=True,
)
def parallel_research(tasks: list[str]) -> str:
    cleaned = [str(t).strip() for t in (tasks or []) if str(t).strip()][:_MAX_PARALLEL]
    if not cleaned:
        return "ERROR: provide at least one sub-question in `tasks`."
    profile = SUBAGENT_PROFILES["explore"]
    results = run_subagents_parallel(
        cleaned,
        system_prompt=str(profile["system_prompt"]),
        tool_categories=profile["tool_categories"],  # type: ignore[arg-type]
    )
    blocks = [f"### {i + 1}. {task}\n{findings}" for i, (task, findings) in enumerate(results)]
    return "## Parallel research findings\n\n" + "\n\n".join(blocks)
