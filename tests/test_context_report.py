"""Tests for the context-window breakdown helper (Phase 88)."""

from __future__ import annotations

import json
from evi.context_report import BUCKETS, context_breakdown
import evi.tools.fs  # noqa: F401  registers read_file and write_file
from evi.tools.base import REGISTRY, Tool


def test_empty_history():
    rep = context_breakdown([], ceiling=0)
    assert rep["used"] == 0 and rep["pct"] == 0 and rep["messages"] == 0
    assert all(rep["buckets"][b] == 0 for b in BUCKETS)


def test_categorises_by_role():
    history = [
        {"role": "system", "content": "x" * 40},  # 40 chars -> 10 tok
        {"role": "user", "content": "y" * 80},  # 80 -> 20 tok
        {
            "role": "assistant",
            "content": "z" * 40,  # 40 -> 10 tok
            "tool_calls": [{"function": {"name": "fs", "arguments": '{"a":1}'}}],  # 9 chars
        },
        {"role": "tool", "content": "r" * 120},  # 120 chars
    ]
    rep = context_breakdown(history, ceiling=1000)
    assert rep["buckets"]["system"] == 10
    assert rep["buckets"]["user"] == 20
    assert rep["buckets"]["assistant"] == 10
    # tools = assistant tool_calls (9 chars) + tool result (120) = 129 -> 32 tok
    assert rep["buckets"]["tools"] == 32
    assert rep["used"] == 72
    assert rep["pct"] == 7  # 72 of 1000
    assert rep["messages"] == 4
    # shares sum to ~100 (integer rounding may drop a point or two)
    assert sum(rep["pct_of_used"].values()) <= 100


def test_no_ceiling_no_zero_division():
    rep = context_breakdown([{"role": "user", "content": "hello"}], ceiling=0)
    assert rep["ceiling"] == 0 and rep["pct"] == 0 and rep["used"] >= 1


def test_multipart_text_counts_only_text():
    history = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "a" * 40},
                {"type": "image_url", "image_url": {"url": "data:" + "Z" * 9999}},
            ],
        }
    ]
    rep = context_breakdown(history, ceiling=0)
    assert rep["buckets"]["user"] == 10  # image data URL ignored



def _mcp(name: str, size: int) -> Tool:
    """An MCP-like tool ("<server>.<tool>") whose description is `size` chars."""
    return Tool(name=name, description="d" * size, parameters={"type": "object", "properties": {}},
                func=lambda **k: "", category="mcp")


def test_tool_schema_tokens():
    from evi.context_report import tool_schema_tokens
    # AC1: Built-in tools
    rf, wf = REGISTRY["read_file"], REGISTRY["write_file"]
    expected = (len(json.dumps(rf.openai_schema())) + len(json.dumps(wf.openai_schema()))) // 4
    assert tool_schema_tokens([rf, wf]) == {"fs": expected}

    # AC2: MCP tools grouping and sorting
    mcp_tools = [_mcp("github.search", 400), _mcp("github.issue", 400), _mcp("notes.add", 100)]
    res = tool_schema_tokens(mcp_tools)
    assert list(res.keys()) == ["mcp:github", "mcp:notes"]
    assert res["mcp:github"] > res["mcp:notes"]

    # AC3: Non-tool objects skipped
    assert tool_schema_tokens([rf, object()]) == {"fs": len(json.dumps(rf.openai_schema())) // 4}


def test_context_breakdown_with_tools():
    # AC4: Tools impact on breakdown
    history = [{"role": "user", "content": "a" * 40}]
    rf = REGISTRY["read_file"]

    # With tools
    rep_with = context_breakdown(history, 1000, tools=[rf])
    schema_toks = rep_with["schema_groups"]["fs"]
    assert rep_with["buckets"]["schemas"] == schema_toks
    assert rep_with["buckets"]["schemas"] > 0
    assert rep_with["used"] == (10 + schema_toks)

    # Without tools
    rep_without = context_breakdown(history, 1000)
    assert rep_without["buckets"]["schemas"] == 0
    assert rep_without["schema_groups"] == {}
    assert rep_without["used"] == 10
