"""Tests for exposing Evi's tools as an MCP server (Phase 53).

Asserts only on tool categories that don't need optional extras (`git`,
`memory`) so the suite passes in CI, which doesn't install every extra.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from evi.mcp import publish  # noqa: E402
from evi.tools.base import Tool  # noqa: E402


def _fake_tool(name="echo"):
    return Tool(
        name=name,
        description="echo back",
        parameters={"type": "object", "properties": {"x": {"type": "string"}}, "required": ["x"]},
        func=lambda x: f"got {x}",
        category="test",
    )


# --- tool selection ------------------------------------------------------


def test_selected_tools_includes_always_available_categories():
    git = publish.selected_tools(("git",))
    mem = publish.selected_tools(("memory",))
    assert git and all(t.category == "git" for t in git)
    assert mem and all(t.category == "memory" for t in mem)


def test_selected_tools_unknown_category_is_empty():
    assert publish.selected_tools(("definitely-not-a-category",)) == []


def test_selected_tools_sorted_by_name():
    tools = publish.selected_tools(("git", "memory"))
    names = [t.name for t in tools]
    assert names == sorted(names)


def test_default_categories_are_reasonable():
    assert "memory" in publish.DEFAULT_CATEGORIES
    assert "git" in publish.DEFAULT_CATEGORIES


# --- spec mapping --------------------------------------------------------


def test_mcp_tool_specs_shape():
    specs = publish.mcp_tool_specs([_fake_tool("a")])
    assert specs == [{
        "name": "a",
        "description": "echo back",
        "inputSchema": {"type": "object", "properties": {"x": {"type": "string"}}, "required": ["x"]},
    }]


def test_specs_construct_valid_mcp_tools():
    mcp_types = pytest.importorskip("mcp.types")
    spec = publish.mcp_tool_specs([_fake_tool("b")])[0]
    mt = mcp_types.Tool(**spec)  # must not raise — the inputSchema is reused verbatim
    assert mt.name == "b"
    assert mt.inputSchema["type"] == "object"


# --- dispatch ------------------------------------------------------------


def test_dispatch_invokes_known_tool():
    by_name = {"echo": _fake_tool("echo")}
    assert publish.dispatch(by_name, "echo", {"x": "hi"}) == "got hi"


def test_dispatch_unknown_tool_returns_error():
    out = publish.dispatch({}, "nope", {})
    assert out.startswith("ERROR: unknown tool")


def test_dispatch_tolerates_none_arguments():
    # The fake tool requires `x`; missing args surface as a tool ERROR, not a crash.
    out = publish.dispatch({"echo": _fake_tool("echo")}, "echo", None)
    assert out.startswith("ERROR")


# --- server construction (smoke) ----------------------------------------


def test_build_server_smoke():
    pytest.importorskip("mcp")
    server = publish.build_server(("git",))
    assert server is not None
    assert getattr(server, "name", "evi") == "evi"
