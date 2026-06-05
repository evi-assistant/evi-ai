"""Tests for the optional web bearer-token auth middleware.

We stub the Agent (same trick as test_web.py) so no LM Studio is required,
then monkeypatch `Config.load` to return a Config with `web.auth_token`
set or unset. The full HTTP path through the middleware runs against
FastAPI's TestClient.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterator

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("sse_starlette")

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
        self.auto_all = False
        self.auto_approve_categories: set[str] = set()
        self.permission_callback = None

    def chat(self, message: str, images=None, **_) -> Iterator[Event]:
        yield TextDelta(text="ok")
        yield Done(reason="stop")

    def reset(self) -> None:
        pass


def _client_with_token(monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
                       token: str) -> TestClient:
    """Build a TestClient where every Config.load() returns a Config with
    `web.auth_token` patched to `token`."""
    monkeypatch.setattr(server_mod, "Agent", _FakeAgent)
    monkeypatch.setattr(server_mod, "make_client", lambda *_: None)
    monkeypatch.setattr(server_mod, "get_enabled_tools", lambda _: [])
    monkeypatch.setattr(server_mod, "IMAGE_DIR", tmp_path)

    original_load = Config.load

    def fake_load(cls=Config):
        cfg = original_load()
        cfg.web.auth_token = token
        return cfg

    # Both server_mod.Config and the live class are referenced; patch both.
    monkeypatch.setattr(server_mod.Config, "load", classmethod(lambda cls: fake_load()))

    app = server_mod.create_app()
    return TestClient(app)


# ---- disabled (default) ----------------------------------------------------


def test_auth_disabled_allows_everything(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    client = _client_with_token(monkeypatch, tmp_path, token="")
    assert client.get("/api/health").status_code == 200
    r = client.post("/api/chat", json={"session_id": "s", "message": "hi"})
    assert r.status_code == 200


# ---- enabled --------------------------------------------------------------


def test_auth_enabled_blocks_chat_without_token(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    client = _client_with_token(monkeypatch, tmp_path, token="secret123")
    r = client.post("/api/chat", json={"session_id": "s", "message": "hi"})
    assert r.status_code == 401
    assert r.json() == {"error": "unauthorized"}


def test_auth_enabled_blocks_wrong_token(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    client = _client_with_token(monkeypatch, tmp_path, token="secret123")
    r = client.post(
        "/api/chat",
        json={"session_id": "s", "message": "hi"},
        headers={"Authorization": "Bearer wrong"},
    )
    assert r.status_code == 401


def test_auth_enabled_accepts_correct_header(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    client = _client_with_token(monkeypatch, tmp_path, token="secret123")
    r = client.post(
        "/api/chat",
        json={"session_id": "s", "message": "hi"},
        headers={"Authorization": "Bearer secret123"},
    )
    assert r.status_code == 200


def test_auth_enabled_accepts_query_token(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    client = _client_with_token(monkeypatch, tmp_path, token="secret123")
    r = client.post(
        "/api/chat?token=secret123",
        json={"session_id": "s", "message": "hi"},
    )
    assert r.status_code == 200


def test_health_is_public(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    client = _client_with_token(monkeypatch, tmp_path, token="secret123")
    r = client.get("/api/health")
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_auth_check_endpoint(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    client = _client_with_token(monkeypatch, tmp_path, token="secret123")

    # Without a token -> required=true, ok=false
    r = client.get("/api/auth/check")
    assert r.status_code == 200
    assert r.json() == {"ok": False, "required": True}

    # With the right token -> ok=true
    r = client.get(
        "/api/auth/check", headers={"Authorization": "Bearer secret123"}
    )
    assert r.json() == {"ok": True, "required": True}


def test_auth_check_when_disabled(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    client = _client_with_token(monkeypatch, tmp_path, token="")
    r = client.get("/api/auth/check")
    assert r.json() == {"ok": True, "required": False}


def test_images_path_is_public(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    img = tmp_path / "abc.png"
    img.write_bytes(b"\x89PNG-DATA")
    client = _client_with_token(monkeypatch, tmp_path, token="secret123")
    r = client.get("/images/abc.png")
    # Capability-URL style: filename is random hex so we trust the path.
    assert r.status_code == 200
    assert r.content == b"\x89PNG-DATA"


def test_index_html_is_public(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    client = _client_with_token(monkeypatch, tmp_path, token="secret123")
    r = client.get("/")
    assert r.status_code == 200
    # The login overlay needs to render before any token is set.
    assert "auth-overlay" in r.text


def test_constant_time_compare_rejects_long_token(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A wrong token of different length still 401s (no leak via timing
    error). This mostly exercises the secrets.compare_digest path."""
    client = _client_with_token(monkeypatch, tmp_path, token="short")
    r = client.post(
        "/api/chat",
        json={"session_id": "s", "message": "hi"},
        headers={"Authorization": "Bearer " + "x" * 200},
    )
    assert r.status_code == 401
