"""Tests for headless single-shot runs (Phase 65)."""

from __future__ import annotations

import json

from evi.headless import HeadlessResult, run_headless, to_json
from evi.llm.agent import Done, Error, TextDelta, ToolResult, UsageStats


class _FakeAgent:
    def __init__(self, events):
        self._events = events

    def chat(self, prompt, max_turns=12):
        yield from self._events


def test_text_collected():
    a = _FakeAgent([TextDelta("hello "), TextDelta("world"), Done("stop")])
    res = run_headless(a, "hi")
    assert res.text == "hello world"
    assert res.error is None


def test_tools_and_usage():
    a = _FakeAgent([
        ToolResult("read_file", "data"),
        UsageStats(10, 5, 15),
        TextDelta("done"),
        Done("stop"),
    ])
    res = run_headless(a, "x")
    assert res.tools[0]["name"] == "read_file"
    assert res.usage["total"] == 15
    assert res.text == "done"


def test_error_captured():
    a = _FakeAgent([Error("boom")])
    res = run_headless(a, "x")
    assert res.error == "boom"


def test_json_envelope():
    call = {"name": "t", "args": "{}", "status": "ok", "duration_ms": 1, "output": "o"}
    res = HeadlessResult(text="hi", tool_calls=[call], usage={"total": 3}, stop_reason="done")
    d = json.loads(to_json(res))
    assert d["schema_version"] == 1
    assert (d["text"], d["tool_calls"], d["usage"], d["error"], d["stop_reason"]) == (
        "hi", [call], {"total": 3}, None, "done")
    assert res.tools == [{"name": "t", "output": "o"}]  # the pre-report view, for SDK callers


def test_tool_output_truncated():
    a = _FakeAgent([ToolResult("big", "x" * 5000), Done("stop")])
    res = run_headless(a, "x")
    assert len(res.tools[0]["output"]) == 2000
