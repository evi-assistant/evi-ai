"""A shell line that reads a protected secret asks.

`cat .env` used to run unasked under an `allow run_command *` rule or with
"shell" auto-approved, while `read_file .env` asked. Now a reading command
(`cat`, `Get-Content`, `grep`, `cp`, `base64` ...) or an input redirect
naming a protected secret asks, an allow rule doesn't clear it, a deny rule
denies it, and an unattended run refuses it.
"""

from __future__ import annotations

import json

import pytest

import evi.tools.shell  # noqa: F401  registers run_command
from evi.config import Config
from evi.llm.agent import Agent, ToolResult
from evi.permissions import SECRET_PROTECTED_PATHS, decide, shell_protected_read
from evi.tools.base import REGISTRY

PATS = list(SECRET_PROTECTED_PATHS)


def _d(line: str, *, dialect: str = "bash", mode: str = "ask", rules=(),
       auto=("shell",)) -> str:
    t = REGISTRY["run_command"]
    a = json.dumps({"command": line})
    return decide(mode, list(auto), list(rules), "run_command", t.category, a,
                  protected_paths=PATS, workdir=".", dialect=dialect,
                  call_category=t.category_for(a))


@pytest.mark.parametrize("line", [
    "cat .env && ls",
    "head -n 5 config/.env",
    "grep KEY .env",
    "cp ~/.ssh/id_ed25519 /tmp/k",
    "base64 server.pem",
    "sort < .env",
    "cd sub && cat .env",
])
def test_reading_a_secret_asks_over_an_allow_rule(line):
    assert _d(line, rules=["allow run_command *"]) == "ask"
    assert _d(line) == "ask"  # "shell" auto-approved


def test_powershell_get_content_asks():
    assert _d("Get-Content ~/.ssh/id_ed25519", dialect="pwsh",
              rules=["allow run_command *"]) == "ask"
    assert _d("gc -Path:.env", dialect="pwsh") == "ask"


def test_a_deny_rule_still_denies():
    assert _d("cat .env", rules=["deny run_command cat*"]) == "deny"


@pytest.mark.parametrize("line", ["grep -r TODO .", "cat README.md", "ls .env", "echo .env"])
def test_lines_that_read_no_secret_are_unchanged(line):
    assert _d(line, rules=["allow run_command *"]) == "allow"


def test_yolo_is_unchanged():
    # Like read_file .env: yolo still runs it (with a UI to ask, it is the
    # user's chosen mode); unattended runs refuse it below.
    assert _d("cat .env", mode="yolo") == "allow"


def test_an_expansion_is_not_guessed():
    assert shell_protected_read("run_command", "shell", {"command": "cat $F"}, PATS) is None


def test_removing_the_pattern_turns_it_off():
    pats = [p for p in PATS if p not in (".env", "*.env", ".env.*")]
    assert shell_protected_read("run_command", "shell", {"command": "cat .env"}, pats) is None


def test_unattended_run_refuses_and_says_why(tmp_path, fake_llm):
    cfg = Config()
    cfg.tools.shell = True
    cfg.tools.memory = cfg.tools.skills = cfg.tools.transcripts = False
    cfg.auto.rules = ["allow run_command *"]
    (tmp_path / ".env").write_text("SECRET=1\n", encoding="utf-8")
    client = fake_llm([fake_llm.call(("run_command", {"command": "cat .env"})),
                       fake_llm.text("done")])
    agent = Agent(client=client, config=cfg, tools=[REGISTRY["run_command"]])
    agent.cwd = str(tmp_path)
    [res] = [e for e in agent.chat("go") if isinstance(e, ToolResult)]
    assert "reads the protected path '.env'" in res.output
    assert "SECRET=1" not in res.output
