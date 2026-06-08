"""Tests for the session browser (list / find / history_from_transcript)."""

from __future__ import annotations

import json
import time
from pathlib import Path

from evi.sessions import (
    find_session,
    history_from_transcript,
    list_sessions,
    most_recent_session_id,
)


def _write_session(root: Path, day: str, session: str, entries: list[dict]) -> Path:
    d = root / day
    d.mkdir(parents=True, exist_ok=True)
    f = d / f"{session}.jsonl"
    with f.open("w", encoding="utf-8") as fh:
        for e in entries:
            fh.write(json.dumps(e) + "\n")
    return f


def test_list_sessions_empty(tmp_path: Path) -> None:
    assert list_sessions(root=tmp_path) == []


def test_list_sessions_summarises(tmp_path: Path) -> None:
    now = time.time()
    _write_session(tmp_path, "2026-05-20", "abc", [
        {"role": "user", "content": "Hello world", "ts": now - 100},
        {"role": "assistant", "content": "Hi!", "ts": now - 99},
    ])
    _write_session(tmp_path, "2026-05-21", "def", [
        {"role": "user", "content": "Another session", "ts": now - 10},
    ])

    items = list_sessions(root=tmp_path)
    # Newest day first.
    assert [s.session_id for s in items] == ["def", "abc"]
    assert items[0].first_user_message == "Another session"
    assert items[1].message_count == 2


def test_list_sessions_strips_goal_prefix(tmp_path: Path) -> None:
    """User messages with [ongoing goal: …] prefix should surface the actual ask."""
    now = time.time()
    content = "[ongoing goal: build evi]\n\nactually look at file X please"
    _write_session(tmp_path, "2026-05-22", "xyz", [
        {"role": "user", "content": content, "ts": now},
    ])
    items = list_sessions(root=tmp_path)
    assert items[0].first_user_message.startswith("actually look at")


def test_list_sessions_skips_malformed(tmp_path: Path) -> None:
    d = tmp_path / "2026-05-22"
    d.mkdir()
    (d / "broken.jsonl").write_text("{garbage\n", encoding="utf-8")
    # No structured entries → no SessionInfo emitted.
    assert list_sessions(root=tmp_path) == []


def test_find_session(tmp_path: Path) -> None:
    f = _write_session(tmp_path, "2026-05-22", "needle", [
        {"role": "user", "content": "x", "ts": 0}
    ])
    assert find_session("needle", root=tmp_path) == f
    assert find_session("missing", root=tmp_path) is None


def test_history_from_transcript_preserves_roles(tmp_path: Path) -> None:
    f = _write_session(tmp_path, "2026-05-22", "abc", [
        {"role": "user", "content": "hi", "ts": 0},
        {"role": "assistant", "content": "hello!", "ts": 1},
        {"role": "tool", "content": "ran", "tool_name": "read_file", "ts": 2},
        {"role": "assistant", "content": "done", "ts": 3},
        {"role": "system", "content": "boring", "ts": 4},  # skipped
    ])
    history = history_from_transcript(f)
    assert [m["role"] for m in history] == ["user", "assistant", "tool", "assistant"]
    tool_msg = next(m for m in history if m["role"] == "tool")
    assert tool_msg["name"] == "read_file"
    assert tool_msg["tool_call_id"].startswith("resumed_")


def test_history_preserves_tool_calls(tmp_path: Path) -> None:
    tcs = [{"id": "c1", "function": {"name": "x", "arguments": "{}"}}]
    f = _write_session(tmp_path, "2026-05-22", "abc", [
        {"role": "assistant", "content": "", "tool_calls": tcs, "ts": 0},
    ])
    history = history_from_transcript(f)
    assert history[0]["tool_calls"] == tcs


def test_most_recent_session_by_timestamp(tmp_path: Path) -> None:
    now = time.time()
    # "aaa" is lexically first but older; "bbb" is newer — most_recent must
    # pick by timestamp, not filename order.
    _write_session(tmp_path, "2026-05-20", "bbb", [
        {"role": "user", "content": "new", "ts": now},
    ])
    _write_session(tmp_path, "2026-05-19", "aaa", [
        {"role": "user", "content": "old", "ts": now - 9999},
    ])
    assert most_recent_session_id(root=tmp_path) == "bbb"


def test_most_recent_session_empty(tmp_path: Path) -> None:
    assert most_recent_session_id(root=tmp_path) is None
