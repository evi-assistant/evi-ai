"""Headless (non-interactive) single-shot runs.

Backs `evi run "<prompt>"` — drain one agent turn-loop to completion and return
a structured result, for scripts / CI / cron. The CLI builds the agent (and
sets a non-interactive permission policy); this module just drains + formats, so
it's testable with a fake agent.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from evi.llm.agent import Done, Error, TextDelta, ToolResult, UsageStats


@dataclass
class HeadlessResult:
    text: str = ""
    tools: list[dict] = field(default_factory=list)
    usage: dict | None = None
    error: str | None = None


def run_headless(
    agent, prompt: str, max_turns: int = 12, response_format: dict | None = None
) -> HeadlessResult:
    """Run one prompt through `agent` to completion and collect the result.

    `response_format` (e.g. a json_schema wrapper) is forwarded to the backend
    for Structured Outputs when the agent supports it.
    """
    parts: list[str] = []
    res = HeadlessResult()
    chat_kwargs = {"max_turns": max_turns}
    if response_format is not None:
        chat_kwargs["response_format"] = response_format
    for event in agent.chat(prompt, **chat_kwargs):
        if isinstance(event, TextDelta):
            parts.append(event.text)
        elif isinstance(event, ToolResult):
            res.tools.append({"name": event.name, "output": event.output[:2000]})
        elif isinstance(event, UsageStats):
            res.usage = {
                "prompt": event.prompt_tokens,
                "completion": event.completion_tokens,
                "total": event.total_tokens,
            }
        elif isinstance(event, Error):
            res.error = event.message
        elif isinstance(event, Done):
            break
    res.text = "".join(parts).strip()
    return res


def to_json(res: HeadlessResult) -> str:
    return json.dumps(
        {"text": res.text, "tools": res.tools, "usage": res.usage, "error": res.error}
    )
