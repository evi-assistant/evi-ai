"""Web API for the MCP panel — list servers, add, remove, toggle."""

from __future__ import annotations

import json

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

import evi.apps.web.server as server_mod  # noqa: E402


@pytest.fixture
def mcp_path(monkeypatch, tmp_path):
    p = tmp_path / "mcp.json"
    monkeypatch.setattr("evi.mcp.servers.MCP_CONFIG_PATH", p)
    # keep real installed plugins out of the merged listing
    monkeypatch.setattr("evi.plugins.plugin_dirs", lambda root=None: [])
    return p


@pytest.fixture
def client(monkeypatch, mcp_path, tmp_path):
    monkeypatch.setattr(server_mod, "make_client", lambda *_: None)
    monkeypatch.setattr(server_mod, "IMAGE_DIR", tmp_path)
    return TestClient(server_mod.create_app())


def test_list_empty(client):
    d = client.get("/api/mcp").json()
    assert d["servers"] == [] and isinstance(d["enabled"], bool)


def test_add_list_remove(client, mcp_path):
    r = client.post("/api/mcp", json={
        "name": "filesystem", "command": "npx",
        "args": "-y @modelcontextprotocol/server-filesystem C:/Users",
        "env": {"API_KEY": "secret"},
    })
    assert r.status_code == 200
    saved = json.loads(mcp_path.read_text(encoding="utf-8"))
    assert saved[0]["name"] == "filesystem"
    assert saved[0]["args"] == ["-y", "@modelcontextprotocol/server-filesystem", "C:/Users"]
    assert saved[0]["env"] == {"API_KEY": "secret"}

    d = client.get("/api/mcp").json()
    s = d["servers"][0]
    assert s["name"] == "filesystem" and s["on"] is True and s["plugin"] is False
    assert s["env_keys"] == ["API_KEY"]
    assert "secret" not in json.dumps(d)  # env VALUES never echoed

    assert client.post("/api/mcp/remove", json={"name": "filesystem"}).status_code == 200
    assert client.get("/api/mcp").json()["servers"] == []


def test_add_args_as_list(client):
    client.post("/api/mcp", json={"name": "git", "command": "uvx",
                                  "args": ["mcp-server-git", "--repository", "."]})
    s = client.get("/api/mcp").json()["servers"][0]
    assert s["args"] == ["mcp-server-git", "--repository", "."]


def test_toggle(client):
    client.post("/api/mcp", json={"name": "fs", "command": "npx"})
    assert client.post("/api/mcp/toggle", json={"name": "fs", "on": False}).status_code == 200
    assert client.get("/api/mcp").json()["servers"][0]["on"] is False
    assert client.post("/api/mcp/toggle", json={"name": "fs", "on": True}).status_code == 200
    assert client.get("/api/mcp").json()["servers"][0]["on"] is True
    assert client.post("/api/mcp/toggle", json={"name": "ghost"}).status_code == 404


def test_add_validation(client):
    assert client.post("/api/mcp", json={"name": "x"}).status_code == 400  # no command
    assert client.post("/api/mcp", json={"command": "npx"}).status_code == 400  # no name
    r = client.post("/api/mcp", json={"name": "a:b", "command": "npx"})
    assert r.status_code == 400  # ':' reserved for plugin namespacing
    r = client.post("/api/mcp", json={"name": "x", "command": "npx", "args": 7})
    assert r.status_code == 400


def test_remove_plugin_server_rejected(client):
    r = client.post("/api/mcp/remove", json={"name": "someplugin:tool"})
    assert r.status_code == 400
    assert client.post("/api/mcp/remove", json={"name": "ghost"}).status_code == 404
