"""Tests for the context-window breakdown helper (Phase 88)."""

from __future__ import annotations

from evi.context_report import BUCKETS, context_breakdown


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
