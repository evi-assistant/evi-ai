"""eVi Agent SDK — the curated, supported public surface for building agents.

eVi is a library first and a CLI/app second: the same primitives the CLI and the
web app use are importable here under one stable namespace. Prefer
``from evi.sdk import ...`` over reaching into ``evi.llm`` / ``evi.tools`` /
``evi.mcp`` internals — those module layouts may move; this re-export will not.

Quick start::

    from evi.sdk import build_agent, run_headless

    agent = build_agent()                 # batteries: tools + memory + skills + hooks
    print(run_headless(agent, "Summarise the README").text)

Streaming a turn yourself::

    from evi.sdk import build_agent, TextDelta, ToolCall, Done

    agent = build_agent()
    for ev in agent.chat("List the Python files here"):
        if isinstance(ev, TextDelta):
            print(ev.text, end="")
        elif isinstance(ev, ToolCall):
            print(f"\\n[tool] {ev.name}")
        elif isinstance(ev, Done):
            break

Defining a custom tool::

    from evi.sdk import tool, build_agent

    @tool(category="custom", description="Add two integers")
    def add(a: int, b: int) -> int:
        return a + b

    agent = build_agent(tools=[add])

See ``docs/sdk.md`` and ``examples/python/`` for the full guide.
"""

from __future__ import annotations

# --- core agent + streaming event types ---------------------------------
from evi.llm.agent import (
    DEFAULT_SYSTEM_PROMPT,
    Agent,
    BatchPermissionCallback,
    Done,
    Error,
    Guardrail,
    LogProbs,
    PermissionCallback,
    RouteInfo,
    TextDelta,
    ThinkingDelta,
    ToolCall,
    ToolProgress,
    ToolResult,
    UsageStats,
)
from evi.llm.client import make_client

# --- convenience constructor --------------------------------------------
from evi.sdk.builder import build_agent

# --- tools ---------------------------------------------------------------
from evi.citations import ToolOutput
from evi.tools import (
    REGISTRY,
    Tool,
    get_enabled_tools,
    register_builtin_tools,
    tool,
)

# --- subagents -----------------------------------------------------------
from evi.llm.subagent import (
    SUBAGENT_PROFILES,
    all_profiles,
    get_profile,
    run_subagent,
    run_subagents_parallel,
)

# --- skills / commands / hooks ------------------------------------------
from evi.commands import CommandStore, SlashCommandEntry
from evi.hooks import Hook, HookRegistry, HookResult, load_hooks
from evi.skills import SkillEntry, SkillStore, import_skill

# --- MCP -----------------------------------------------------------------
from evi.mcp.manager import MCPManager

# --- config --------------------------------------------------------------
from evi.config import Config

# --- structured output ---------------------------------------------------
from evi.structured import SchemaError, as_response_format, load_schema

# --- headless / batch ----------------------------------------------------
from evi.headless import HeadlessResult, run_headless, to_json

# --- sessions / checkpoints ---------------------------------------------
from evi.checkpoints import list_checkpoints, record_before_write, rewind
from evi.sessions import (
    SessionInfo,
    export_markdown,
    find_session,
    history_from_transcript,
    list_sessions,
    most_recent_session_id,
)

# --- orchestration: workflows + ultracode -------------------------------
from evi.ultracode import (
    UltraConfig,
    UltraResult,
    UltraStage,
    make_runner,
    run_ultracode,
)
from evi.workflows import Workflow, fan_out, run_workflow

# --- telemetry -----------------------------------------------------------
from evi import otel

from evi import __version__

__all__ = [
    "__version__",
    # core
    "Agent",
    "build_agent",
    "make_client",
    "Config",
    "DEFAULT_SYSTEM_PROMPT",
    "PermissionCallback",
    "BatchPermissionCallback",
    # events
    "TextDelta",
    "ThinkingDelta",
    "ToolCall",
    "ToolResult",
    "ToolProgress",
    "UsageStats",
    "LogProbs",
    "Guardrail",
    "RouteInfo",
    "Done",
    "Error",
    # tools
    "tool",
    "Tool",
    "ToolOutput",
    "REGISTRY",
    "get_enabled_tools",
    "register_builtin_tools",
    # subagents
    "run_subagent",
    "run_subagents_parallel",
    "SUBAGENT_PROFILES",
    "all_profiles",
    "get_profile",
    # skills / commands / hooks
    "SkillStore",
    "SkillEntry",
    "import_skill",
    "CommandStore",
    "SlashCommandEntry",
    "HookRegistry",
    "Hook",
    "HookResult",
    "load_hooks",
    # mcp
    "MCPManager",
    # structured
    "load_schema",
    "as_response_format",
    "SchemaError",
    # headless
    "run_headless",
    "HeadlessResult",
    "to_json",
    # sessions / checkpoints
    "list_sessions",
    "SessionInfo",
    "find_session",
    "history_from_transcript",
    "export_markdown",
    "most_recent_session_id",
    "record_before_write",
    "list_checkpoints",
    "rewind",
    # orchestration
    "run_ultracode",
    "make_runner",
    "UltraConfig",
    "UltraResult",
    "UltraStage",
    "run_workflow",
    "Workflow",
    "fan_out",
    # telemetry
    "otel",
]
