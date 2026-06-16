"""Tests for the permission policy (Phase 66)."""

from __future__ import annotations

import json

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


def test_hard_deny_beats_everything():
    # hard_deny is evaluated before yolo AND before allow rules — unoverridable.
    assert decide("yolo", ["fs"], ["allow run_shell *"], "run_shell", "shell",
                  '{"command": "rm -rf /"}', hard_deny=["run_shell rm*"]) == "deny"
    # a leading "deny" in the entry is accepted too
    assert decide("ask", [], [], "write_file", "fs", '{"path": "/x/secrets"}',
                  hard_deny=["deny write_file *secrets*"]) == "deny"
    # non-matching hard_deny doesn't block
    assert decide("yolo", [], [], "read_file", "fs", "{}", hard_deny=["run_shell *"]) == "allow"


def test_protected_paths_force_ask():
    pp = [".env", "*.pem", ".gitconfig"]
    # accept_edits would allow a write, but a protected path forces a prompt
    assert decide("accept_edits", [], [], "write_file", "fs",
                  '{"path": "/proj/.env"}', protected_paths=pp) == "ask"
    assert decide("accept_edits", [], [], "write_file", "fs",
                  '{"path": "/proj/key.pem"}', protected_paths=pp) == "ask"
    # an auto-approved category is overridden too
    assert decide("ask", ["fs"], [], "write_file", "fs",
                  '{"path": "/home/u/.gitconfig"}', protected_paths=pp) == "ask"
    # a normal file under accept_edits is still allowed
    assert decide("accept_edits", [], [], "write_file", "fs",
                  '{"path": "/proj/main.py"}', protected_paths=pp) == "allow"
    # an EXPLICIT allow rule still wins (honour user intent over protection)
    assert decide("ask", ["fs"], ["allow write_file *.env"], "write_file", "fs",
                  '{"path": "/proj/.env"}', protected_paths=pp) == "allow"


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


# --- trusted dirs + domains (Phase 77) ----------------------------------


def test_trusted_dir(tmp_path):
    inside = tmp_path / "proj" / "src"
    inside.mkdir(parents=True)
    trusted = [str(tmp_path / "proj")]
    args_in = json.dumps({"path": str(inside / "f.py"), "content": "x"})
    args_out = json.dumps({"path": str(tmp_path / "other" / "f.py")})
    assert decide("ask", [], [], "write_file", "fs", args_in, trusted_dirs=trusted) == "allow"
    assert decide("ask", [], [], "write_file", "fs", args_out, trusted_dirs=trusted) == "ask"


def test_trusted_domain():
    trusted = ["docs.python.org"]
    fetch = json.dumps({"url": "https://docs.python.org/3/library/os.html"})
    other = json.dumps({"url": "https://evil.example.com/x"})
    assert decide("ask", [], [], "web_fetch", "web", fetch, trusted_domains=trusted) == "allow"
    assert decide("ask", [], [], "web_fetch", "web", other, trusted_domains=trusted) == "ask"
    # subdomain matches
    sub = json.dumps({"url": "https://api.docs.python.org/x"})
    assert decide("ask", [], [], "web_fetch", "web", sub, trusted_domains=trusted) == "allow"


def test_deny_rule_beats_trusted_dir(tmp_path):
    trusted = [str(tmp_path)]
    args = json.dumps({"path": str(tmp_path / "secret.env")})
    # explicit deny rule wins over the trusted dir
    assert decide("ask", [], ["deny write_file *.env"], "write_file", "fs", args,
                  trusted_dirs=trusted) == "deny"


def test_agent_uses_trusted_dirs(tmp_path):
    a = _agent(AutoSettings(mode="ask", trusted_dirs=[str(tmp_path)]))
    args = json.dumps({"path": str(tmp_path / "x.py"), "content": "y"})
    tool = _tool(name="write_file", category="fs")
    assert a._permission_decision(tool, args) == "allow"
