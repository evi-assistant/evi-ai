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
