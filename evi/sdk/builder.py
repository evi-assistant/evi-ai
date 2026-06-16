"""``build_agent`` — the convenience constructor for the eVi Agent SDK.

This is the generic Agent-assembly seam shared by the CLI, the web dispatcher,
and external SDK users. It wires a :class:`~evi.llm.agent.Agent` from a
:class:`~evi.config.Config` with sensible defaults (built-in tools selected by
config toggles or an explicit category list, plus memory / skills / project
context / hooks / guardrails), and leaves the runtime-specific concerns —
interactive permission prompts, MCP-server spawning, ``_AUTO_STATE`` bookkeeping
— to the caller via parameters.

The CLI's ``_build_agent`` delegates here (it adds only ``_ensure_mcp`` + the CLI
permission callbacks + auto-state registration), so there is exactly one place
that knows how to assemble an Agent.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import asdict
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only
    from pathlib import Path

    from openai import OpenAI

    from evi.config import Config
    from evi.llm.agent import (
        BatchPermissionCallback,
        PermissionCallback,
    )
    from evi.tools.base import Tool
    from evi.transcripts import TranscriptStore


def _resolve_tools(items: Iterable[Any]) -> "list[Tool]":
    """Normalise a mixed list into Tool objects. Accepts Tool instances (used
    as-is) and ``@tool``-decorated functions (resolved from REGISTRY by name —
    the decorator returns the function but registers the Tool). Anything else is
    a clear error rather than a confusing AttributeError deeper in the agent."""
    from evi.tools.base import REGISTRY, Tool

    out: list[Tool] = []
    for item in items:
        if isinstance(item, Tool):
            out.append(item)
            continue
        nm = getattr(item, "__name__", None)
        if nm and nm in REGISTRY:
            out.append(REGISTRY[nm])
            continue
        raise TypeError(
            f"build_agent(tools=...) expects Tool instances or @tool-decorated "
            f"functions; got {item!r}. Decorate it with @evi.sdk.tool first."
        )
    return out


def build_agent(
    *,
    config: "Config | None" = None,
    system_prompt: str | None = None,
    model: str | None = None,
    client: "OpenAI | None" = None,
    tools: "list[Tool] | None" = None,
    tool_categories: Iterable[str] | None = None,
    enable_memory: bool | None = None,
    enable_skills: bool | None = None,
    enable_project: bool = True,
    enable_hooks: bool = True,
    enable_guardrails: bool = True,
    memory_root: "Path | None" = None,
    permission_callback: "PermissionCallback | None" = None,
    permission_batch_callback: "BatchPermissionCallback | None" = None,
    transcripts: "TranscriptStore | None" = None,
    session_id: str | None = None,
):
    """Assemble an :class:`~evi.llm.agent.Agent` from config with batteries.

    Parameters (all keyword-only):
      config: an :class:`~evi.config.Config`; defaults to ``Config.load()``.
      system_prompt: override the agent's base system prompt (defaults to eVi's).
      model: override the model id for this agent (same backend/endpoint, a
        different model) — e.g. route an ultracode stage to a cheaper model.
      client: a pre-built OpenAI-compatible client; defaults to
        ``make_client(config.llm)``.
      tools: an explicit tool list (skips registry selection entirely).
      tool_categories: select built-in tools by category (e.g. ``["fs", "code"]``)
        instead of the config's ``[tools]`` toggles. Ignored if ``tools`` is given.
      enable_memory / enable_skills: ``None`` follows the config toggle; ``True``/
        ``False`` forces it on/off.
      enable_project / enable_hooks / enable_guardrails: load project context /
        hooks / guardrails from disk (default ``True``).
      memory_root: directory for the :class:`~evi.memory.MemoryStore` when memory
        is enabled (e.g. a per-user data dir in multi-user web mode). ``None``
        uses the shared default location — identical to ``MemoryStore()``.
      permission_callback / permission_batch_callback: tool-permission prompts;
        ``None`` means non-interactive (the caller decides via auto-approve).
      transcripts: a :class:`~evi.transcripts.TranscriptStore` to log turns to.
      session_id: reuse a specific session id (default: a fresh one).

    Returns the constructed Agent. Does **not** register the agent anywhere or
    spawn MCP servers — those are runtime concerns for the caller.
    """
    from dataclasses import replace as _replace

    from evi.config import Config, ensure_dirs
    from evi.llm.agent import Agent
    from evi.llm.client import make_client
    from evi.tools import get_enabled_tools, register_builtin_tools

    ensure_dirs()
    config = config or Config.load()
    # Optional per-build model override (same endpoint, different model id) — used
    # e.g. by ultracode to route a stage to a cheaper/fast model.
    if model:
        config = _replace(config, llm=_replace(config.llm, model=model))
    client = client or make_client(config.llm)
    toggles = asdict(config.tools)

    if tools is None:
        register_builtin_tools()  # ensure REGISTRY is populated before selection
        if tool_categories is not None:
            tools = get_enabled_tools({c: True for c in tool_categories})
        else:
            tools = get_enabled_tools(toggles)
    else:
        tools = _resolve_tools(tools)

    want_memory = toggles.get("memory", False) if enable_memory is None else enable_memory
    want_skills = toggles.get("skills", False) if enable_skills is None else enable_skills

    memory = None
    if want_memory:
        from evi.memory import MemoryStore

        # ``root=None`` resolves to the shared default dir (== MemoryStore()),
        # so callers can inject a per-user root without changing single-user behaviour.
        memory = MemoryStore(root=memory_root)
    skills = None
    if want_skills:
        from evi.skills import SkillStore

        skills = SkillStore()
    project = None
    if enable_project:
        from evi.project import load_project_context

        project = load_project_context()
    hooks = None
    if enable_hooks:
        from evi.hooks import load_hooks

        hooks = load_hooks()
    guardrails = None
    if enable_guardrails:
        from evi.guardrails import Guardrails

        loaded = Guardrails.load()
        guardrails = loaded if loaded.enabled else None

    agent = Agent(
        client=client,
        config=config,
        tools=tools,
        memory=memory,
        skills=skills,
        project=project,
        hooks=hooks,
        permission_callback=permission_callback,
        permission_batch_callback=permission_batch_callback,
        transcripts=transcripts,
        session_id=session_id,
        guardrails=guardrails,
        **({"system_prompt": system_prompt} if system_prompt is not None else {}),
    )
    # Tool-search-at-scale (opt-in): with many tools, defer the long tail behind
    # a `search_tools` meta-tool so per-turn context stays small.
    if getattr(config.tools, "tool_search", False):
        from evi.tools.resolver import apply_tool_search

        apply_tool_search(
            agent, tools, threshold=getattr(config.tools, "tool_search_threshold", 30)
        )
    return agent
