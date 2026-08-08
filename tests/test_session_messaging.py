"""Intra-instance session messaging (Ph 95) + federation LAN fail-safe.

Tests the pure server helpers directly (no server spin-up): message delivery
into a session's history/inbox, target resolution, the injected tools, and the
federation guard that refuses remote delegation on an open (tokenless) server.
"""

from __future__ import annotations

import json

import pytest

from evi.apps.web import server as srv


class _FakeAgent:
    def __init__(self) -> None:
        self.history = [{"role": "system", "content": "sys"}]


class _FakeSess:
    def __init__(self, title: str = "") -> None:
        self.agent = _FakeAgent()
        self.channel_log: list = []
        self.title = title
        self.busy = False
        self.mode = "chat"


# ---- delivery ------------------------------------------------------------

def test_deliver_note_appends_history_and_inbox() -> None:
    s = _FakeSess()
    assert srv._deliver_note(s, "session:alice", "hello") is True
    assert s.channel_log == [{"source": "session:alice", "text": "hello", "ran": False}]
    assert s.agent.history[-1] == {
        "role": "system", "content": '📨 Message from session "alice": hello'
    }


def test_deliver_note_dedupes_identical_repeat() -> None:
    s = _FakeSess()
    assert srv._deliver_note(s, "you", "x") is True
    assert srv._deliver_note(s, "you", "x") is False  # identical repeat dropped
    assert len(s.channel_log) == 1
    assert srv._deliver_note(s, "you", "y") is True   # different text delivers
    assert len(s.channel_log) == 2


def test_deliver_note_caps_log(monkeypatch) -> None:
    monkeypatch.setattr(srv, "_MSG_LOG_CAP", 5)
    s = _FakeSess()
    for i in range(20):
        srv._deliver_note(s, "you", f"m{i}")
    assert len(s.channel_log) == 5
    assert s.channel_log[-1]["text"] == "m19"


# ---- target resolution ---------------------------------------------------

def test_resolve_target_by_id_title_ambiguity_and_self() -> None:
    a, b, c = _FakeSess("Alpha"), _FakeSess("Beta"), _FakeSess("Beta")
    bucket = {"id_a": a, "id_b": b, "id_c": c}
    assert srv._resolve_target(bucket, "self", "id_a") == ("id_a", a)      # by id
    assert srv._resolve_target(bucket, "self", "alpha") == ("id_a", a)     # by title (ci)
    amb = srv._resolve_target(bucket, "self", "beta")                      # ambiguous
    assert isinstance(amb, list) and len(amb) == 2
    assert srv._resolve_target(bucket, "self", "nope") is None             # no match
    assert srv._resolve_target({"self": a}, "self", "self") is None        # never self


# ---- injected tools ------------------------------------------------------

def test_session_tools_list_excludes_self_and_send_delivers() -> None:
    a, b = _FakeSess("Alpha"), _FakeSess("Beta")
    bucket = {"id_a": a, "id_b": b}
    tools = {t.name: t for t in srv._make_session_tools(bucket, "id_a")}

    listed = json.loads(tools["list_sessions"].func())
    assert [x["name"] for x in listed] == ["Beta"]  # excludes self

    res = tools["send_to_session"].func(target="Beta", text="ping")
    assert "delivered" in res and "Beta" in res
    assert b.agent.history[-1]["content"] == '📨 Message from session "Alpha": ping'

    assert "ERROR" in tools["send_to_session"].func(target="ghost", text="x")  # unknown
    assert "ERROR" in tools["send_to_session"].func(target="Beta", text="  ")  # empty


def test_session_tools_are_auto_approvable_category() -> None:
    assert all(t.category == "session" for t in srv._make_session_tools({}, "self"))


# ---- federation LAN fail-safe -------------------------------------------

class _FakeReq:
    def __init__(self, host: str) -> None:
        self.client = type("C", (), {"host": host})()


class _FakeCfg:
    def __init__(self, token: str) -> None:
        self.web = type("W", (), {"auth_token": token})()


def test_is_loopback_host() -> None:
    for h in ("127.0.0.1", "::1", "localhost", "", "127.5.5.5"):
        assert srv._is_loopback_host(h)
    assert not srv._is_loopback_host("192.168.1.5")


def test_federation_guard_allows_loopback_without_token() -> None:
    srv._federation_lan_guard(_FakeReq("127.0.0.1"), _FakeCfg(""))  # must not raise


def test_federation_guard_blocks_remote_without_token() -> None:
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as ei:
        srv._federation_lan_guard(_FakeReq("192.168.1.5"), _FakeCfg(""))
    assert ei.value.status_code == 403


def test_federation_guard_allows_remote_with_token() -> None:
    srv._federation_lan_guard(_FakeReq("192.168.1.5"), _FakeCfg("secret"))  # must not raise
