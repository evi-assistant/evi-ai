"""`[tools] tool_search = "auto"` (tools-tool-search-auto): when the tool
schemas would take more than a fifth of the context window, a request sends
only the core categories' tools, the tools the model already looked up and a
`search_tools` tool; every tool stays callable (and permission-gated)."""

from __future__ import annotations

import json

import pytest

from evi.config import Config
from evi.llm.agent import Agent
from evi.tools import register_builtin_tools
from evi.tools.base import Tool, get_enabled_tools
from evi.tools.resolver import (
    AUTO_CORE_CATEGORIES,
    AUTO_SEARCH_NAME,
    crowds,
    tool_search_mode,
)
from tests.conftest import llm_call, llm_text


def _tool(name: str, desc: str, category: str) -> Tool:
    return Tool(name=name, description=desc, parameters={"type": "object", "properties": {}},
                func=lambda: f"{name} ran", category=category)


def _all_tools() -> list[Tool]:
    register_builtin_tools()
    tools = get_enabled_tools({c: True for c in (
        "fs", "shell", "code", "git", "memory", "web", "pdf", "image", "calendar", "index")})
    return tools + [_tool("weather.lookup", "Look up the current weather for a city", "mcp")]


def _agent(client=None, *, window: int, mode="auto", tools=None) -> Agent:
    cfg = Config()
    cfg.llm.context_size = window
    cfg.tools.tool_search = mode
    return Agent(client=client, config=cfg, tools=tools if tools is not None else _all_tools(),
                 transcripts=None, unattended_policy="allow")


def _names(schemas) -> set[str]:
    return {s["function"]["name"] for s in schemas}


@pytest.mark.parametrize("value, want", [
    (True, "on"), (False, "off"), ("auto", "auto"), (None, "auto"), ("", "auto"),
    ("ON", "on"), ("true", "on"), ("off", "off"), ("never", "off"), ("bogus", "auto"),
])
def test_tool_search_mode_parses_the_setting(value, want):
    assert tool_search_mode(value) == want


def test_crowds_past_a_fifth_of_the_window_and_never_without_one():
    assert crowds(2001, 10000) and not crowds(2000, 10000)
    assert not crowds(10**6, 0)


def test_auto_is_the_default():
    assert tool_search_mode(Config().tools.tool_search) == "auto"


def test_a_roomy_window_sends_every_schema():
    agent = _agent(window=131072)
    sent = _names(agent._wire_tool_schemas())
    assert AUTO_SEARCH_NAME not in sent
    assert "weather__lookup" in sent and len(sent) == len(agent.tools)


def test_a_small_window_defers_all_but_the_core_tools():
    agent = _agent(window=8192)
    sent = agent._wire_tool_schemas()
    names = _names(sent)
    assert AUTO_SEARCH_NAME in names and "weather__lookup" not in names
    core = {t.name for t in agent.tools.values() if t.category in AUTO_CORE_CATEGORIES}
    assert core <= names
    assert len(json.dumps(sent)) < len(json.dumps(_agent(window=131072)._wire_tool_schemas()))


def test_off_and_on_send_what_they_did_before():
    tools = _all_tools()
    off = _agent(window=8192, mode=False, tools=tools)
    assert _names(off._wire_tool_schemas()) == {
        n.replace(".", "__") for n in off.tools}
    assert AUTO_SEARCH_NAME not in off.tools


def test_search_tools_surfaces_a_deferred_tool_for_the_next_request(fake_llm):
    client = fake_llm([
        llm_call((AUTO_SEARCH_NAME, {"query": "weather city"})),
        llm_call(("weather__lookup", {})),
        llm_text("sunny"),
    ])
    agent = _agent(client, window=8192)
    list(agent.chat("what's the weather?"))
    first, second, _ = [_names(r.get("tools") or []) for r in client.requests]
    assert "weather__lookup" not in first and AUTO_SEARCH_NAME in first
    assert "weather__lookup" in second
    outputs = [m.get("content") for m in agent.history if m.get("role") == "tool"]
    assert any("weather.lookup" in (o or "") for o in outputs)
    assert any("weather.lookup ran" in (o or "") for o in outputs)


def test_a_deferred_tool_called_by_name_still_runs(fake_llm):
    client = fake_llm([llm_call(("weather__lookup", {})), llm_text("done")])
    agent = _agent(client, window=8192)
    list(agent.chat("go"))
    outputs = [m.get("content") for m in agent.history if m.get("role") == "tool"]
    assert any("weather.lookup ran" in (o or "") for o in outputs)


def test_web_settings_store_the_mode_whatever_the_old_type(monkeypatch):
    from evi.apps.web import server

    cfg = Config()
    cfg.tools.tool_search = True
    server._apply_config_patch(cfg, {"tools": {"tool_search": "auto"}})
    assert cfg.tools.tool_search == "auto"
    server._apply_config_patch(cfg, {"tools": {"tool_search": "off"}})
    assert cfg.tools.tool_search == "off"
