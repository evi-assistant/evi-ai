"""Tools go to the model under provider-safe names with normalized schemas
(evi.toolschema), and the names the model uses map back to the real tools at
dispatch (Agent._wire_tool_schemas, Agent._real_tool_name)."""

from __future__ import annotations

import json
import re

from evi.config import Config
from evi.llm.agent import Agent, ToolCall, ToolResult
from evi.tools.base import Tool
from tests.conftest import llm_call, llm_text

_SAFE = re.compile(r"[a-zA-Z0-9_-]{1,64}")
_PYDANTIC = {"$defs": {"Q": {"type": "object", "properties": {"text": {"type": "string"}},
                             "required": ["text"]}},
             "type": "object", "properties": {"query": {"$ref": "#/$defs/Q"},
                                              "limit": {"anyOf": [{"type": "integer"},
                                                                  {"type": "null"}]}}}


def _tool(name: str, parameters: dict | None = None, out: str | None = None) -> Tool:
    return Tool(name=name, description=f"the {name} tool",
                parameters=parameters or {"type": "object", "properties": {}},
                func=lambda **_k: out or f"ran {name}", category="mcp")


def _agent(client, *tools: Tool, **kw) -> Agent:
    kw.setdefault("unattended_policy", "allow")
    return Agent(client=client, config=Config(), transcripts=None, tools=list(tools), **kw)


def _sent(client, i: int = 0) -> dict[str, dict]:
    return {t["function"]["name"]: t["function"]
            for t in client.chat.completions.requests[i]["tools"]}


def test_an_mcp_tool_goes_under_a_safe_name_and_runs_when_called_by_it(fake_llm):
    client = fake_llm([llm_call(("github__search", {})), llm_text("done")])
    agent = _agent(client, _tool("github.search"))
    evs = list(agent.chat("go", max_turns=3))
    assert set(_sent(client)) == {"github__search"}
    assert all(_SAFE.fullmatch(n) for r in client.chat.completions.requests for n in _sent(client))
    # The call ran the real tool, shown and checked under its own name.
    [call] = [e for e in evs if isinstance(e, ToolCall)]
    [result] = [e for e in evs if isinstance(e, ToolResult)]
    assert call.name == "github.search" and result.output == "ran github.search"
    # The history keeps the name the model used, as the model saw it.
    assert agent.history[2]["tool_calls"][0]["function"]["name"] == "github__search"


def test_the_permission_prompt_and_policy_see_the_real_name(fake_llm):
    asked: list[str] = []
    agent = _agent(fake_llm([llm_call(("github__search", {})), llm_text("ok")]),
                   _tool("github.search"), unattended_policy="deny")
    agent.auto_approve_categories = set()
    agent.permission_callback = lambda name, args, cat: asked.append(name) or True
    list(agent.chat("go", max_turns=3))
    assert asked == ["github.search"]


def test_schemas_go_normalized_and_byte_identical_every_turn(fake_llm):
    client = fake_llm([llm_call(("db__find", {"query": {"text": "x"}})), llm_text("done")])
    tool = _tool("db.find", _PYDANTIC)
    list(_agent(client, tool).chat("go", max_turns=3))
    params = _sent(client)["db__find"]["parameters"]
    assert "$ref" not in json.dumps(params) and "$defs" not in params
    assert params["properties"]["limit"] == {"type": "integer"}
    assert client.chat.completions.requests[0]["tools"] == client.chat.completions.requests[1]["tools"]
    assert "$defs" in tool.parameters  # the tool's own schema is untouched


def test_names_that_would_collide_stay_distinct_and_each_routes_right(fake_llm):
    client = fake_llm([llm_call(("a__b", {})), llm_text("done")])
    agent = _agent(client, _tool("a.b"), _tool("a__b"))
    evs = list(agent.chat("go", max_turns=3))
    sent = set(_sent(client))
    assert len(sent) == 2 and "a__b" in sent and all(_SAFE.fullmatch(n) for n in sent)
    assert [e.output for e in evs if isinstance(e, ToolResult)] == ["ran a__b"]


def test_an_unknown_name_is_answered_with_the_names_the_model_can_call(fake_llm):
    agent = _agent(fake_llm([llm_call(("github_search", {})), llm_text("ok")]),
                   _tool("github.search"))
    [result] = [e for e in agent.chat("go", max_turns=3) if isinstance(e, ToolResult)]
    assert "Did you mean: github__search" in result.output


def test_a_call_written_as_text_with_the_wire_name_runs(fake_llm):
    text = '{"name": "github__search", "arguments": {}}'
    agent = _agent(fake_llm([llm_text(text), llm_text("done")]), _tool("github.search"))
    results = [e.output for e in agent.chat("go", max_turns=3) if isinstance(e, ToolResult)]
    assert results == ["ran github.search"]
