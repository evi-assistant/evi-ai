"""Subagent runner — spin up a scoped `Agent` for delegated work and drain it.

A subagent shares the same `Agent` class as the main loop but is built with
a focused system prompt and a restricted tool list. Callers hand in a `task`
string, we run the agent to completion, and return the concatenated final
assistant text. Used by `evi.tools.subagent` to back `delegate_explore`,
`delegate_plan`, …
"""

from __future__ import annotations

from typing import Iterable

from evi.config import Config
from evi.llm.agent import Agent, Done, Error, TextDelta, ToolResult
from evi.llm.client import make_client
from evi.tools.base import REGISTRY, Tool


def _tools_in_categories(categories: Iterable[str]) -> list[Tool]:
    allowed = set(categories)
    return [t for t in REGISTRY.values() if t.category in allowed]


def run_subagent(
    *,
    system_prompt: str,
    task: str,
    tool_categories: Iterable[str] = (),
    max_turns: int = 6,
) -> str:
    """Run a one-shot scoped Agent and return its final assistant text.

    Pulls the same LLM client/config the parent uses; respects the parent's
    tool *category* filter but ignores per-tool toggles, so a subagent can
    use read-only filesystem tools even if `fs` is otherwise on.
    """
    config = Config.load()
    client = make_client(config.llm)
    tools = _tools_in_categories(tool_categories)
    agent = Agent(
        client=client,
        config=config,
        tools=tools,
        system_prompt=system_prompt,
    )

    text_parts: list[str] = []
    tool_trace: list[str] = []
    error: str | None = None

    for event in agent.chat(task, max_turns=max_turns):
        if isinstance(event, TextDelta):
            text_parts.append(event.text)
        elif isinstance(event, ToolResult):
            # Keep a short trace so the caller can see what the sub-agent did.
            preview = event.output[:200].replace("\n", " ")
            tool_trace.append(f"{event.name}: {preview}")
        elif isinstance(event, Error):
            error = event.message
            break
        elif isinstance(event, Done):
            break

    if error:
        return f"ERROR: subagent failed: {error}"

    result = "".join(text_parts).strip()
    if not result:
        result = "(subagent produced no text)"
    if tool_trace:
        trace = "\n".join(f"  - {t}" for t in tool_trace)
        result = f"{result}\n\n[trace]\n{trace}"
    return result


# Pre-baked subagent personalities. New ones can be added without changing
# the tool-layer dispatch — see evi/tools/subagent.py.
SUBAGENT_PROFILES: dict[str, dict[str, object]] = {
    "explore": {
        "system_prompt": (
            "You are an Explore subagent. Your job is to investigate a "
            "codebase or filesystem and report findings concisely. You may "
            "use read-only filesystem tools. Do not modify anything. End "
            "with a short bulleted summary of what you found."
        ),
        "tool_categories": ("fs",),
    },
    "plan": {
        "system_prompt": (
            "You are a Plan subagent. Given a task, produce a step-by-step "
            "implementation plan as a numbered list. Identify critical files, "
            "trade-offs, and risks. Do not write code. Do not call tools."
        ),
        "tool_categories": (),
    },
}
