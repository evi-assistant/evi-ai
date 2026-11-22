"""Tool search at scale.

When an agent has many tools (built-ins + lots of MCP servers), sending every
tool schema on every turn bloats the context. Instead, defer the long tail
behind a single ``search_tools`` meta-tool: the model searches by capability,
the matching tools are added to the agent's live toolset, and they become
callable on the next round (with the normal per-category permission gating —
``search_tools`` only *exposes* tools, it never executes them).

This mirrors Claude Code's deferred-tool / ToolSearch pattern. The core
:func:`rank_tools` is pure and unit-testable; :func:`make_search_tools` binds it
to a specific agent + catalog.
"""

from __future__ import annotations

import json
import re
from typing import Any

from evi.tools.base import Tool

# Categories kept always-loaded even in deferred mode, so the agent has basic
# capability without searching first. The long tail (MCP, niche tools) defers.
CORE_CATEGORIES: frozenset[str] = frozenset({"fs", "memory"})

_WORD_RE = re.compile(r"[a-z0-9]+")


def _tokens(text: str) -> set[str]:
    return set(_WORD_RE.findall((text or "").lower()))


def rank_tools(catalog: list[Tool], query: str, limit: int = 8) -> list[Tool]:
    """Rank ``catalog`` tools by relevance to ``query`` (name weighted over
    description), returning up to ``limit`` with a positive score. Pure."""
    q = (query or "").lower().strip()
    qtokens = _tokens(q)
    scored: list[tuple[int, str, Tool]] = []
    for t in catalog:
        name_l = t.name.lower()
        desc_l = (t.description or "").lower()
        name_tokens = _tokens(name_l)
        desc_tokens = _tokens(desc_l)
        score = 0
        for w in qtokens:
            if w in name_tokens:
                score += 3
            elif w in desc_tokens:
                score += 1
        # Whole-query substring boosts (handles dotted MCP names like "git.log").
        if q and q in name_l:
            score += 4
        elif q and q in desc_l:
            score += 1
        if score > 0:
            scored.append((score, t.name, t))
    scored.sort(key=lambda s: (-s[0], s[1]))
    return [t for _, _, t in scored[: max(1, limit)]]


def make_search_tools(agent: Any, catalog: dict[str, Tool]) -> Tool:
    """Build a ``search_tools`` Tool bound to ``agent`` over a deferred
    ``catalog`` ({name: Tool}). Calling it ranks the catalog, ADDS the matches to
    ``agent.tools`` (so they're callable next round, still permission-gated), and
    returns a JSON directory of what it surfaced."""

    def search_tools(query: str, limit: int = 8) -> str:
        matches = rank_tools(list(catalog.values()), query, limit)
        if not matches:
            return json.dumps({
                "added": [],
                "note": f"no deferred tools matched {query!r}; "
                        f"{len(catalog)} tools available — try other keywords",
            })
        added = []
        for t in matches:
            agent.tools[t.name] = t  # surfaced -> callable on the next round
            added.append({"name": t.name, "description": t.description, "category": t.category})
        return json.dumps({
            "added": added,
            "note": f"{len(added)} tool(s) now available — call them directly.",
        })

    return Tool(
        name="search_tools",
        description=(
            "Search for additional tools by capability when you don't already "
            "have one for the task. Returns matching tools AND makes them "
            "available to call on your next step. Use specific keywords "
            "(e.g. 'git commit', 'sqlite query', 'send slack message')."
        ),
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "What capability you need."},
                "limit": {"type": "integer", "description": "Max tools to surface.", "default": 8},
            },
            "required": ["query"],
        },
        func=search_tools,
        category="meta",
    )


# --- tool_search = "auto" --------------------------------------------------
#
# Decided per request (Agent._wire_tool_schemas), so it holds whatever set the
# tools (a mode preset, a workflow step) and follows the window the server
# reports: when the tool schemas would take more than AUTO_SHARE_PCT of the
# window, only AUTO_CORE_CATEGORIES, the tools the model already looked up and
# a `search_tools` tool are sent. Every tool stays in agent.tools, so a call
# to one is gated as ever; the deferred ones just aren't described each turn.

TOOL_SEARCH_MODES = ("auto", "on", "off")
AUTO_SHARE_PCT = 20
# What a coding turn can't do without: files, the shell, running code, git,
# and memory (kept loaded in "on" mode too).
AUTO_CORE_CATEGORIES: frozenset[str] = CORE_CATEGORIES | {"shell", "code", "git", "meta"}
AUTO_SEARCH_NAME = "search_tools"


def tool_search_mode(value: Any) -> str:
    """`[tools] tool_search` as one of TOOL_SEARCH_MODES: true / "on" →
    "on" (defer past `tool_search_threshold` tools), false / "off" → "off",
    anything else (the default) "auto"."""
    if value is True:
        return "on"
    if value is False:
        return "off"
    text = str(value or "").strip().lower()
    if text in ("on", "true", "yes", "always"):
        return "on"
    if text in ("off", "false", "no", "never"):
        return "off"
    return "auto"


def crowds(schema_tokens: int, window: int) -> bool:
    """Tool schemas of `schema_tokens` take more than AUTO_SHARE_PCT of a
    `window`-token context (an unknown window, 0, never crowds)."""
    return window > 0 and schema_tokens * 100 > window * AUTO_SHARE_PCT


def deferrable(tool: Tool, surfaced: set[str]) -> bool:
    """Whether "auto" leaves `tool`'s schema out: not a core category, not
    looked up yet, not the search tool itself."""
    return (tool.category not in AUTO_CORE_CATEGORIES and tool.name not in surfaced
            and not getattr(tool, "auto_search", False))


def make_auto_search(agent: Any) -> Tool:
    """The `search_tools` of "auto" mode: ranks the agent's deferred tools
    (`deferrable`) and marks the matches as looked up (`agent._surfaced_tools`),
    so their schemas go with the next request."""

    def search_tools(query: str, limit: int = 8) -> str:
        surfaced: set[str] = agent.__dict__.setdefault("_surfaced_tools", set())
        catalog = [t for t in agent.tools.values() if deferrable(t, surfaced)]
        matches = rank_tools(catalog, query, limit)
        if not matches:
            return json.dumps({
                "added": [],
                "note": f"no deferred tools matched {query!r}; "
                        f"{len(catalog)} tools available — try other keywords",
            })
        for t in matches:
            surfaced.add(t.name)
        return json.dumps({
            "added": [{"name": t.name, "description": t.description, "category": t.category}
                      for t in matches],
            "note": f"{len(matches)} tool(s) now available — call them directly.",
        })

    tool = make_search_tools(agent, {})
    tool = Tool(name=AUTO_SEARCH_NAME, description=tool.description, parameters=tool.parameters,
                func=search_tools, category="meta")
    tool.auto_search = True  # type: ignore[attr-defined]
    return tool


def apply_tool_search(agent: Any, tools: list[Tool], *, threshold: int) -> bool:
    """If ``tools`` exceeds ``threshold``, reconfigure ``agent`` for deferred tool
    search: keep CORE_CATEGORIES loaded, move the rest into a catalog behind a
    bound ``search_tools`` tool. Returns True if deferral was applied.

    Mutates ``agent.tools`` in place (the agent is already constructed). Safe to
    call with a small toolset — it just no-ops and returns False.
    """
    if len(tools) <= max(1, threshold):
        return False
    core = {t.name: t for t in tools if t.category in CORE_CATEGORIES}
    catalog = {t.name: t for t in tools if t.category not in CORE_CATEGORIES}
    if not catalog:
        return False
    agent.tools = core
    st = make_search_tools(agent, catalog)
    agent.tools[st.name] = st
    return True
