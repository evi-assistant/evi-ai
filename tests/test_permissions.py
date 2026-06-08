"""Tests for the permission policy (Phase 66)."""

from __future__ import annotations

from evi.config import AutoSettings, Config
from evi.llm.agent import Agent
from evi.permissions import decide
from evi.tools.base import Tool


# --- pure policy --------------------------------------------------------


def test_modes():
    assert decide("yolo", [], [], "run_shell", "shell", "{}") == "allow"
    assert decide("plan", ["fs"], [], "write_file", "fs", "{}") == "deny"
    assert decide("accept_edits", [], [], "write_file", "fs", "{}") == "allow"
    assert decide("accept_edits", [], [], "run_shell", "shell", "{}") == "ask"


def test_auto_approve_category():
    assert decide("ask", ["fs"], [], "write_file", "fs", "{}") == "allow"
    assert decide("ask", [], [], "write_file", "fs", "{}") == "ask"


def test_rule_deny_with_arg_glob():
    rules = ["deny write_file *.env"]
    assert decide("ask", ["fs"], rules, "write_file", "fs",
                  '{"path": "/x/.env", "content": "y"}') == "deny"
    # a non-matching path still uses the category allow
    assert decide("ask", ["fs"], rules, "write_file", "fs",
                  '{"path": "/x/readme.md"}') == "allow"


def test_rule_allow_overrides_ask():
    assert decide("ask", [], ["allow web_search"], "web_search", "web", "{}") == "allow"


def test_rule_first_match_wins():
    rules = ["deny run_shell rm*", "allow run_shell *"]
    assert decide("ask", [], rules, "run_shell", "shell", '{"command": "rm -rf /"}') == "deny"
    assert decide("ask", [], rules, "run_shell", "shell", '{"command": "ls"}') == "allow"


def test_tool_glob():
    assert decide("ask", [], ["deny delegate_*"], "delegate_explore", "subagent", "{}") == "deny"


# --- agent integration --------------------------------------------------


def _agent(auto: AutoSettings) -> Agent:
    cfg = Config()
    cfg.auto = auto
    return Agent(client=object(), config=cfg, tools=[])


def _tool(name="run_shell", category="shell") -> Tool:
    return Tool(name=name, description="", parameters={"type": "object", "properties": {}},
                func=lambda: "x", category=category)


def test_agent_permission_decision():
    assert _agent(AutoSettings(mode="yolo"))._permission_decision(_tool(), "{}") == "allow"
    assert _agent(AutoSettings(mode="plan"))._permission_decision(_tool(), "{}") == "deny"
    a = _agent(AutoSettings(mode="ask", rules=["deny run_shell rm*"]))
    assert a._permission_decision(_tool(), '{"command": "rm x"}') == "deny"
    assert a._permission_decision(_tool(), '{"command": "ls"}') == "ask"


def test_agent_auto_all_overrides_plan():
    a = _agent(AutoSettings(mode="plan"))
    a.enable_auto_all()
    assert a._permission_decision(_tool(), "{}") == "allow"
