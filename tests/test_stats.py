"""Tests for local usage analytics (evi stats)."""

from __future__ import annotations

import json

from evi import stats


def _write_session(root, day, sid, entries):
    d = root / day
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{sid}.jsonl"
    with p.open("w", encoding="utf-8") as f:
        for i, e in enumerate(entries):
            f.write(json.dumps({"session": sid, "ts": float(i + 1), **e}) + "\n")


def test_empty(tmp_path):
    data = stats.compute_stats(root=tmp_path / "transcripts")
    assert data["sessions"] == 0 and data["messages"] == 0


def test_aggregates(tmp_path):
    root = tmp_path / "transcripts"
    _write_session(root, "2026-06-01", "s1", [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello"},
        {"role": "tool", "content": "result", "tool_name": "read_file"},
        {"role": "tool", "content": "r2", "tool_name": "read_file"},
        {"role": "tool", "content": "r3", "tool_name": "web_search"},
    ])
    _write_session(root, "2026-06-02", "s2", [
        {"role": "user", "content": "q"},
        {"role": "assistant", "content": "a"},
    ])
    data = stats.compute_stats(root=root)
    assert data["sessions"] == 2
    assert data["messages"] == 7
    assert data["roles"]["tool"] == 3 and data["roles"]["user"] == 2
    assert data["tools"]["read_file"] == 2 and data["tools"]["web_search"] == 1
    # most-used tool first
    assert list(data["tools"])[0] == "read_file"
    # per-category attribution: read_file -> fs (x2), web_search -> web (x1)
    cats = data["tool_categories"]
    assert cats.get("fs") == 2 and cats.get("web") == 1
    assert set(data["busiest_days"]) == {"2026-06-01", "2026-06-02"}
    assert data["approx_tokens"] >= 0
    assert data["first_ts"] == 1.0
