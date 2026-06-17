"""Tests for channels — push-into-session (Phase 83)."""

from __future__ import annotations

from typing import Iterator

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

import evi.apps.web.server as server_mod  # noqa: E402
from evi.config import Config  # noqa: E402
from evi.llm.agent import Done, Event, TextDelta  # noqa: E402


class _FakeAgent:
    def __init__(self, *_, **__) -> None:
        self.config = Config()
        self.tools: dict = {}
        self.goal = None
        self.plan_mode_once = False
        self.history: list[dict] = [{"role": "system", "content": "sys"}]

    def chat(self, *_a, **_k) -> Iterator[Event]:
        yield TextDelta("ok")
        yield Done(reason="stop")

    def token_usage(self):
        return (0, 0)


@pytest.fixture
def client(monkeypatch, tmp_path) -> TestClient:
    import evi.sdk.builder as builder_mod
    monkeypatch.setattr(builder_mod, "build_agent", lambda *_, **__: _FakeAgent())
    monkeypatch.setattr(server_mod, "make_client", lambda *_: None)
    monkeypatch.setattr(server_mod, "IMAGE_DIR", tmp_path)
    return TestClient(server_mod.create_app())


def test_push_channel_injects_system_note(client: TestClient) -> None:
    r = client.post(
        "/api/session/c1/channel", json={"text": "build failed", "source": "ci"}
    )
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] and body["source"] == "ci" and body["pending"] == 1

    # The note is now in the agent's history for the next turn.
    hist = client.get("/api/session/c1/history").json()["messages"]
    assert any(
        m["role"] == "system" and m["content"] == "[channel:ci] build failed"
        for m in hist
    )


def test_push_channel_run_drives_live_turn(client: TestClient) -> None:
    # run=true pushes into the LIVE session: the agent acts immediately and the
    # reply comes back (the fake agent streams "ok").
    r = client.post(
        "/api/session/cr/channel",
        json={"text": "deploy finished", "source": "ci", "run": True},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["ran"] is True and body["reply"] == "ok"
    log = client.get("/api/session/cr/channel").json()["messages"]
    assert log and log[-1]["ran"] is True


def test_push_channel_requires_text(client: TestClient) -> None:
    r = client.post("/api/session/c2/channel", json={"text": "  "})
    assert r.status_code == 400


def test_channel_log_lists_messages(client: TestClient) -> None:
    client.post("/api/session/c3/channel", json={"text": "one"})
    client.post("/api/session/c3/channel", json={"text": "two", "source": "alerts"})
    msgs = client.get("/api/session/c3/channel").json()["messages"]
    assert [m["text"] for m in msgs] == ["one", "two"]
    assert msgs[1]["source"] == "alerts"
    # default source when omitted
    assert msgs[0]["source"] == "channel"


def test_channel_log_empty_for_unknown_session(client: TestClient) -> None:
    assert client.get("/api/session/nope/channel").json()["messages"] == []
