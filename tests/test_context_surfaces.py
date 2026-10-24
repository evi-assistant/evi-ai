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
