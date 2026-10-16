"""evi.sdk re-exports ToolContext, so an SDK tool author declares a `ctx`
parameter without importing evi.tools.base (the evi.sdk docstring's example).
How the agent fills it in is tests/test_tool_context.py."""

from __future__ import annotations

import json

import evi.sdk as sdk
from evi.sdk import REGISTRY, ToolContext, tool
from evi.tools import base


def test_tool_context_is_part_of_the_sdk():
    assert ToolContext is base.ToolContext
    assert "ToolContext" in sdk.__all__


def test_the_docstring_example_gets_the_call_context(tmp_path):
    name = "sdk_where_probe"
    try:
        @tool(name=name, category="custom", description="Where this session works")
        def where(ctx: ToolContext | None = None) -> str:
            return ctx.cwd if ctx and ctx.cwd else "."

        t = REGISTRY[name]
        assert t.takes_ctx
        assert "ctx" not in json.dumps(t.openai_schema())
        assert t.call({}, ToolContext(session_id="s1", cwd=str(tmp_path))) == str(tmp_path)
        assert t.call({}) == "."  # outside an agent: no context
    finally:
        REGISTRY.pop(name, None)
