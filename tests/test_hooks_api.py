"""Web API for the hooks editor — GET summary + POST validate/save."""

from __future__ import annotations

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

import evi.apps.web.server as server_mod  # noqa: E402


@pytest.fixture
def hooks_path(monkeypatch, tmp_path):
    p = tmp_path / "hooks.toml"
    monkeypatch.setattr("evi.hooks.HOOKS_CONFIG_PATH", p)
    monkeypatch.setattr("evi.plugins.plugin_dirs", lambda root=None: [])
    return p


@pytest.fixture
def client(monkeypatch, hooks_path, tmp_path):
    monkeypatch.setattr(server_mod, "make_client", lambda *_: None)
    monkeypatch.setattr(server_mod, "IMAGE_DIR", tmp_path)
    return TestClient(server_mod.create_app())


def test_get_empty(client):
    d = client.get("/api/hooks").json()
    assert d["raw"] == "" and d["hooks"] == []
    assert "before_tool_call" in d["events"]


def test_post_saves_and_get_reflects(client):
    toml = (
        '[[before_tool_call]]\nname = "audit"\nmatch = "*"\n'
        'command = ["echo", "hi"]\n'
        '[[stop]]\nname = "done"\nurl = "https://example.com/hook"\n'
    )
    r = client.post("/api/hooks", json={"raw": toml})
    assert r.status_code == 200 and r.json()["count"] == 2
    d = client.get("/api/hooks").json()
    assert "audit" in d["raw"]
    by = {h["name"]: h for h in d["hooks"]}
    assert by["audit"]["event"] == "before_tool_call" and by["audit"]["kind"] == "command"
    assert by["done"]["event"] == "stop" and by["done"]["kind"] == "url"


def test_post_invalid_toml_400(client):
    assert client.post("/api/hooks", json={"raw": "this = = bad"}).status_code == 400


def test_post_typoed_event_400(client, hooks_path):
    r = client.post("/api/hooks", json={
        "raw": '[[before_toolcall]]\nname = "x"\ncommand = ["echo"]\n'})
    assert r.status_code == 400 and "before_toolcall" in r.json()["detail"]
    assert not hooks_path.exists()  # rejected input is never written


def test_post_requires_raw(client):
    assert client.post("/api/hooks", json={}).status_code == 400
