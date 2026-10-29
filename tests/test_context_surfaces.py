"""/context in the CLI and the web context route show the tool-schema tokens
(context_report's schemas bucket and schema_groups)."""

from __future__ import annotations

import io

from fastapi.testclient import TestClient
from rich.console import Console

import evi.apps.cli.main as cli_main
import evi.tools.fs  # noqa: F401  registers read_file
from evi.commands import CommandStore
from evi.config import Config
from evi.llm.agent import Agent
from evi.tools.base import REGISTRY, Tool
from tests._live_web import web_app
from tests.conftest import llm_text


def _mcp(name: str) -> Tool:
    """An MCP-like tool: MCP tools are named "<server>.<tool>"."""
    return Tool(name=name, description="d" * 400, parameters={"type": "object", "properties": {}},
                func=lambda **k: "", category="mcp")


def _context_text(monkeypatch, tmp_path, tools) -> str:
    out = io.StringIO()
    monkeypatch.setattr(cli_main, "console", Console(file=out, width=120))
    agent = Agent(client=None, config=Config(), transcripts=None, tools=tools)
    cli_main._handle_context(agent, "", CommandStore(root=tmp_path))
    return out.getvalue()


def test_cli_context_shows_the_schemas_and_their_groups(monkeypatch, tmp_path):
    text = _context_text(monkeypatch, tmp_path, [REGISTRY["read_file"], _mcp("github.search")])
    assert "tool schemas " in text
    [groups] = [line for line in text.splitlines() if "tool schemas by group:" in line]
    assert "mcp:github" in groups and "fs " in groups


def test_cli_context_without_tools_has_an_empty_schemas_row_and_no_groups(monkeypatch, tmp_path):
    text = _context_text(monkeypatch, tmp_path, [])
    [row] = [line for line in text.splitlines() if "tool schemas" in line]
    assert row.rstrip().endswith("0 (0%)")
    assert "by group" not in text


def test_the_web_context_route_reports_the_schemas(monkeypatch, tmp_path, fake_llm):
    agent = Agent(client=fake_llm([llm_text("hi")]), config=Config(), transcripts=None,
                  tools=[REGISTRY["read_file"]])
    c = TestClient(web_app(monkeypatch, tmp_path, agent))
    c.post("/api/chat", json={"session_id": "s1", "message": "go"})
    rep = c.get("/api/session/s1/context").json()
    assert rep["buckets"]["schemas"] > 0 and "fs" in rep["schema_groups"]
    # and the system prompt's parts, which account for the whole first message
    assert {"date", "capabilities"} <= set(rep["system_groups"])
    assert "other" not in rep["system_groups"]


class _Memory:
    def format_for_prompt(self) -> str:
        return "## Memory\n" + "- a remembered fact\n" * 100


def test_the_prompt_is_its_labelled_parts_joined():
    agent = Agent(client=None, config=Config(), transcripts=None, tools=[])
    agent.memory = _Memory()
    parts = agent.system_prompt_parts()
    assert agent._compose_system_prompt() == "\n\n".join(text for _, text in parts)
    assert [label for label, _ in parts][:1] == ["base prompt"]
    assert ("memory", _Memory().format_for_prompt()) in parts


def test_cli_context_shows_the_system_prompt_by_part(monkeypatch, tmp_path):
    out = io.StringIO()
    monkeypatch.setattr(cli_main, "console", Console(file=out, width=200))
    agent = Agent(client=None, config=Config(), transcripts=None, tools=[])
    agent.memory = _Memory()
    agent.history[0] = {"role": "system", "content": agent._compose_system_prompt()}
    cli_main._handle_context(agent, "", CommandStore(root=tmp_path))
    [line] = [x for x in out.getvalue().splitlines() if "system prompt by part:" in x]
    # memory (~480 tokens) is the largest part, so it comes first
    assert line.split("by part:")[1].strip().startswith("memory ")
    assert "other" not in line
