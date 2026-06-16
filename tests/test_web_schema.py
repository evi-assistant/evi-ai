"""Web structured outputs — /api/chat forwards a schema as response_format."""

from __future__ import annotations

from typing import Iterator

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

import evi.apps.web.server as server_mod  # noqa: E402
from evi.config import Config  # noqa: E402
from evi.llm.agent import Done, Event, TextDelta  # noqa: E402

_captured: dict = {}


class _FakeAgent:
    def __init__(self, *_, **__) -> None:
        self.config = Config()
        self.tools: dict = {}
        self.goal = None
        self.plan_mode_once = False
        self.history: list[dict] = [{"role": "system", "content": "s"}]
        self.pending: dict = {}

    def chat(self, message, **kw) -> Iterator[Event]:
        _captured.clear()
        _captured.update(kw)
        yield TextDelta('{"ok": true}')
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


def test_chat_forwards_schema_as_response_format(client):
    r = client.post(
        "/api/chat",
        json={"session_id": "s1", "message": "extract", "output_schema": {"type": "object"}},
    )
    assert r.status_code == 200
    r.read()  # drain the SSE stream so the worker runs
    rf = _captured.get("response_format")
    assert rf and rf["type"] == "json_schema"
    assert rf["json_schema"]["schema"] == {"type": "object"}


def test_chat_bad_schema_string_is_400(client):
    r = client.post(
        "/api/chat",
        json={"session_id": "s2", "message": "x", "output_schema": "{not valid json"},
    )
    assert r.status_code == 400


def test_chat_without_schema_has_no_response_format(client):
    r = client.post("/api/chat", json={"session_id": "s3", "message": "hi"})
    r.read()
    assert _captured.get("response_format") is None
