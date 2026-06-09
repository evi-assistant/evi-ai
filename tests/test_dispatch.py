"""Tests for the agent dispatch view (Phase 85)."""

from __future__ import annotations

from typing import Iterator

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

import evi.apps.web.server as server_mod  # noqa: E402
import evi.config as config_mod  # noqa: E402
from evi.config import Config  # noqa: E402
from evi.llm.agent import Done, Event, TextDelta  # noqa: E402


class _FakeAgent:
    def __init__(self, *_, **__) -> None:
        self.config = Config()
        self.tools: dict = {}
        self.goal = None
        self.plan_mode_once = False
        self.history: list[dict] = [{"role": "system", "content": "sys"}]
        self.pending: dict = {}

    def chat(self, *_a, **_k) -> Iterator[Event]:
        yield TextDelta("ok")
        yield Done(reason="stop")

    def enable_auto_all(self):
        pass

    def token_usage(self):
        return (120, 8000)


@pytest.fixture
def client(monkeypatch, tmp_path) -> TestClient:
    # Isolate config + the workflows dir into tmp.
    monkeypatch.setattr(config_mod, "HOME", tmp_path)
    monkeypatch.setattr(config_mod, "CONFIG_PATH", tmp_path / "config.toml")
    monkeypatch.setattr(server_mod, "Agent", _FakeAgent)
    monkeypatch.setattr(server_mod, "make_client", lambda *_: None)
    monkeypatch.setattr(server_mod, "get_enabled_tools", lambda _: [])
    monkeypatch.setattr(server_mod, "IMAGE_DIR", tmp_path)
    return TestClient(server_mod.create_app())


def _write_workflow(tmp_path, name="wf"):
    d = tmp_path / "workflows"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{name}.toml").write_text(
        'name = "wf"\ndescription = "demo"\n'
        '[[steps]]\nid = "a"\nprompt = "hi"\n'
        '[[steps]]\nid = "b"\nprompt = "after {a}"\n',
        encoding="utf-8",
    )


def test_dispatch_lists_sessions_and_workflows(client, tmp_path):
    _write_workflow(tmp_path)
    # Open a session so it shows up.
    client.post("/api/chat", json={"session_id": "d1", "message": "/help"})

    snap = client.get("/api/dispatch").json()
    ids = [s["id"] for s in snap["sessions"]]
    assert "d1" in ids
    s = next(s for s in snap["sessions"] if s["id"] == "d1")
    assert s["ceiling"] == 8000 and "mode" in s
    assert any(w["name"] == "wf" and w["steps"] == 2 for w in snap["workflows"])


def test_dispatch_run_workflow(client, tmp_path):
    _write_workflow(tmp_path)
    r = client.post("/api/dispatch/workflow/wf", json={})
    assert r.status_code == 200
    out = r.json()["outputs"]
    # Each step runs through the fake agent → "ok".
    assert out == {"a": "ok", "b": "ok"}


def test_dispatch_run_unknown_workflow(client):
    assert client.post("/api/dispatch/workflow/nope", json={}).status_code == 404
