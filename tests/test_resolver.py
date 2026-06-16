"""Tests for tool-search-at-scale (evi.tools.resolver)."""

from __future__ import annotations

import json

from evi.tools.base import Tool
from evi.tools.resolver import (
    CORE_CATEGORIES,
    apply_tool_search,
    make_search_tools,
    rank_tools,
)


def _tool(name: str, desc: str, category: str = "misc") -> Tool:
    return Tool(name=name, description=desc, parameters={"type": "object", "properties": {}},
                func=lambda: "", category=category)


CATALOG = [
    _tool("git.commit", "Create a git commit with a message", "mcp"),
    _tool("git.log", "Show git commit history", "mcp"),
    _tool("sqlite.query", "Run a read-only SQL query against a database", "sqlite"),
    _tool("send_slack", "Post a message to a Slack channel", "mcp"),
    _tool("weather", "Look up the current weather for a city", "mcp"),
]


# ---- rank_tools (pure) -----------------------------------------------------


def test_rank_prioritises_name_matches():
    out = rank_tools(CATALOG, "commit", limit=3)
    assert out and out[0].name == "git.commit"


def test_rank_matches_description_words():
    out = rank_tools(CATALOG, "sql database", limit=3)
    assert out[0].name == "sqlite.query"


def test_rank_dotted_substring_boost():
    out = rank_tools(CATALOG, "git.log", limit=2)
    assert out[0].name == "git.log"


def test_rank_no_match_is_empty():
    assert rank_tools(CATALOG, "zzzzz nonsense", limit=5) == []


def test_rank_respects_limit():
    out = rank_tools(CATALOG, "git message channel query weather", limit=2)
    assert len(out) == 2


# ---- make_search_tools surfaces into the agent -----------------------------


class _FakeAgent:
    def __init__(self) -> None:
        self.tools: dict[str, Tool] = {}


def test_search_tools_adds_matches_to_agent():
    agent = _FakeAgent()
    catalog = {t.name: t for t in CATALOG}
    st = make_search_tools(agent, catalog)
    assert st.name == "search_tools" and st.category == "meta"

    out = json.loads(st.call(json.dumps({"query": "git commit"})))
    added = {a["name"] for a in out["added"]}
    assert "git.commit" in added
    # surfaced tools are now live on the agent (callable next round)
    assert "git.commit" in agent.tools


def test_search_tools_no_match_adds_nothing():
    agent = _FakeAgent()
    st = make_search_tools(agent, {t.name: t for t in CATALOG})
    out = json.loads(st.call(json.dumps({"query": "quantum teleporter"})))
    assert out["added"] == []
    assert agent.tools == {}


# ---- apply_tool_search wiring ----------------------------------------------


def test_apply_tool_search_defers_long_tail():
    agent = _FakeAgent()
    core = [_tool("read_file", "read", "fs"), _tool("remember", "save", "memory")]
    tail = [_tool(f"mcp.t{i}", f"tool {i}", "mcp") for i in range(40)]
    applied = apply_tool_search(agent, core + tail, threshold=30)
    assert applied is True
    # core categories stay loaded; the long tail is behind search_tools
    assert "read_file" in agent.tools and "remember" in agent.tools
    assert "search_tools" in agent.tools
    assert "mcp.t0" not in agent.tools
    assert all(c in CORE_CATEGORIES for c in ("fs", "memory"))


def test_apply_tool_search_noop_below_threshold():
    agent = _FakeAgent()
    agent.tools = {"x": _tool("x", "y")}
    applied = apply_tool_search(agent, [_tool("a", "b"), _tool("c", "d")], threshold=30)
    assert applied is False
    assert agent.tools == {"x": agent.tools["x"]}  # untouched


def test_build_agent_enables_tool_search(tmp_path, monkeypatch):
    monkeypatch.setattr("evi.config.HOME", tmp_path)
    monkeypatch.setattr("evi.config.CONFIG_PATH", tmp_path / "config.toml")
    from evi.config import Config
    from evi.sdk.builder import build_agent

    cfg = Config.load()
    cfg.tools.tool_search = True
    cfg.tools.tool_search_threshold = 3
    # explicit tools so we exceed the threshold deterministically
    many = [_tool(f"mcp.t{i}", f"tool {i}", "mcp") for i in range(6)]
    agent = build_agent(config=cfg, tools=many, enable_project=False,
                        enable_hooks=False, enable_guardrails=False)
    assert "search_tools" in agent.tools
    assert "mcp.t0" not in agent.tools  # deferred
