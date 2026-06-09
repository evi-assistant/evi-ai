"""Tests for the /recent REPL builtin (lighter/later item)."""

from __future__ import annotations

from pathlib import Path

import evi.sessions as sessions
from evi.apps.cli import main
from evi.sessions import SessionInfo


def _info(sid):
    return SessionInfo(
        session_id=sid,
        day="2026-06-09",
        path=Path("x"),
        message_count=3,
        first_user_message="hello there",
        started_at=1.0,
        ended_at=2.0,
    )


def test_recent_lists_and_passes_limit(monkeypatch):
    captured = {}

    def fake_list(*, days=7, limit=20, root=None):
        captured["limit"] = limit
        return [_info("abcd1234ef")]

    monkeypatch.setattr(sessions, "list_sessions", fake_list)
    res = main._handle_recent(None, "5", None)
    assert res == "continue"
    assert captured["limit"] == 5


def test_recent_default_limit(monkeypatch):
    captured = {}

    def fake_list(*, days=7, limit=20, root=None):
        captured["limit"] = limit
        return []

    monkeypatch.setattr(sessions, "list_sessions", fake_list)
    assert main._handle_recent(None, "", None) == "continue"
    assert captured["limit"] == 8  # default


def test_recent_bad_arg_falls_back(monkeypatch):
    captured = {}

    def fake_list(*, days=7, limit=20, root=None):
        captured["limit"] = limit
        return []

    monkeypatch.setattr(sessions, "list_sessions", fake_list)
    assert main._handle_recent(None, "notanumber", None) == "continue"
    assert captured["limit"] == 8


def test_recent_registered_as_builtin():
    assert main._BUILTINS.get("recent") is main._handle_recent
