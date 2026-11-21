"""Per-tool MCP policy: always_deny, always_ask and trusted readOnlyHint.

A server entry in ~/.evi/mcp.json may list tools to always deny (refused in
every mode, yolo included) or always ask (asks in every mode, refused
unattended), and may trust its tools' readOnlyHint annotations (a read-only,
non-destructive tool then runs unasked). A plugin's mcp.json can tighten but
never trust hints.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from evi.config import Config
from evi.llm.agent import Agent, ToolResult
from evi.mcp import servers as S
from evi.mcp.manager import MCPManager
from evi.mcp.servers import MCPServer
from evi.permissions import decide
from evi.tools.base import Tool


def _server(**kw) -> MCPServer:
    return MCPServer(name="gh", command="x", **kw)


@pytest.mark.parametrize(("kw", "tool", "ann", "rule"), [
    ({}, "list_issues", None, ""),
    ({"always_deny": ["delete_*"]}, "Delete_Repo", None, "deny"),
    ({"always_ask": ["create_issue"]}, "create_issue", None, "ask"),
    ({"always_deny": ["x"], "always_ask": ["x"]}, "x", None, "deny"),
    ({"read_only_hints": True}, "list_issues", {"readOnlyHint": True}, "read_only"),
    ({"read_only_hints": True}, "list_issues",
     SimpleNamespace(read_only_hint=True, destructive_hint=None), "read_only"),
    ({"read_only_hints": True}, "wipe", {"readOnlyHint": True, "destructiveHint": True}, ""),
    ({"read_only_hints": True}, "list_issues", None, ""),
    ({}, "list_issues", {"readOnlyHint": True}, ""),  # hints not trusted
    ({"read_only_hints": True, "always_ask": ["list_*"]}, "list_issues",
     {"readOnlyHint": True}, "ask"),
])
def test_rule_for(kw, tool, ann, rule):
    assert _server(**kw).rule_for(tool, ann) == rule


def _d(rule: str, mode: str = "ask", **kw) -> str:
    return decide(mode, ["fs"], kw.pop("rules", []), "gh.t", "mcp", "{}", tool_rule=rule, **kw)


def test_decide():
    assert _d("") == "ask"
    assert _d("deny", "yolo") == "deny"
    assert _d("deny", rules=["allow gh.t"]) == "deny"
    assert _d("ask", "yolo") == "ask"
    assert _d("ask", rules=["allow gh.t"]) == "ask"
    assert _d("ask", rules=["deny gh.t"]) == "deny"
    assert _d("ask", "plan") == "deny"
    assert _d("read_only") == "allow"
    assert _d("read_only", rules=["deny gh.t"]) == "deny"
    assert _d("read_only", "plan") == "deny"


def test_parse_and_save_round_trip(tmp_path):
    p = tmp_path / "mcp.json"
    p.write_text(json.dumps([{"name": "gh", "command": "x", "always_ask": ["a", 3, ""],
                              "always_deny": ["d"], "read_only_hints": True}]),
                 encoding="utf-8")
    [s] = S.user_servers(p)
    assert (s.always_ask, s.always_deny, s.read_only_hints) == (["a"], ["d"], True)
    assert S.set_enabled("gh", False, p)  # rewrites the file
    [s] = S.user_servers(p)
    assert (s.always_ask, s.always_deny, s.read_only_hints, s.enabled) == (["a"], ["d"], True, False)


def test_a_plugin_file_cannot_trust_hints(tmp_path):
    p = tmp_path / "mcp.json"
    p.write_text(json.dumps([{"name": "gh", "command": "x", "always_deny": ["d"],
                              "read_only_hints": True}]), encoding="utf-8")
    [s] = S._parse_server_file(p, name_prefix="plug:")
    assert s.always_deny == ["d"] and s.read_only_hints is False


def test_read_only_hints_must_be_literally_true(tmp_path):
    p = tmp_path / "mcp.json"
    p.write_text(json.dumps([{"name": "gh", "command": "x", "read_only_hints": "yes"}]),
                 encoding="utf-8")
    assert S.user_servers(p)[0].read_only_hints is False


def test_the_wrapped_tool_carries_the_rule():
    mgr = MCPManager([])
    mcp_tool = SimpleNamespace(name="delete_repo", description="", inputSchema=None,
                               annotations=None)
    tool = mgr._wrap_tool("gh", None, mcp_tool, _server(always_deny=["delete_*"]))
    assert tool.name == "gh.delete_repo" and tool.mcp_rule == "deny"


def _mcp_tool(name: str, rule: str, ran: list) -> Tool:
    def call(**kw):
        ran.append(name)
        return "ok"

    return Tool(name=name, description="", parameters={"type": "object", "properties": {}},
                func=call, category="mcp", trust="untrusted", mcp_rule=rule)


def _agent(fake_llm, tools, *, ask=None) -> Agent:
    cfg = Config()
    cfg.tools.memory = cfg.tools.skills = cfg.tools.transcripts = False
    client = fake_llm([fake_llm.call(*[(t.name, {}) for t in tools]), fake_llm.text("done")])
    return Agent(client=client, config=cfg, tools=tools, permission_callback=ask)


def test_agent_always_deny_in_yolo(fake_llm):
    ran: list = []
    agent = _agent(fake_llm, [_mcp_tool("gh.delete_repo", "deny", ran)])
    agent.enable_auto_all()
    [res] = [e for e in agent.chat("go") if isinstance(e, ToolResult)]
    assert "always_deny" in res.output and not ran


def test_agent_always_ask_asks_in_yolo(fake_llm):
    ran: list = []
    asked: list = []

    def ask(name, args, category):
        asked.append(name)
        return False

    agent = _agent(fake_llm, [_mcp_tool("gh.create_issue", "ask", ran)], ask=ask)
    agent.enable_auto_all()
    list(agent.chat("go"))
    assert asked == ["gh.create_issue"] and not ran


def test_agent_always_ask_is_refused_unattended(fake_llm):
    ran: list = []
    agent = _agent(fake_llm, [_mcp_tool("gh.create_issue", "ask", ran)])
    agent.config.auto.unattended_policy = "allow"
    list(agent.chat("go"))
    assert not ran


def test_agent_read_only_runs_unasked(fake_llm):
    ran: list = []
    asked: list = []
    agent = _agent(fake_llm, [_mcp_tool("gh.list_issues", "read_only", ran)],
                   ask=lambda *a: asked.append(a) or False)
    list(agent.chat("go"))
    assert ran == ["gh.list_issues"] and not asked
