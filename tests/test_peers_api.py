"""Web API for the Peers panel — list+status, add, remove, LAN scan."""

from __future__ import annotations

import json

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

import evi.apps.web.server as server_mod  # noqa: E402


@pytest.fixture
def peers_path(monkeypatch, tmp_path):
    p = tmp_path / "peers.json"
    monkeypatch.setattr("evi.federation.PEERS_PATH", p)
    return p


@pytest.fixture
def client(monkeypatch, peers_path, tmp_path):
    monkeypatch.setattr(server_mod, "make_client", lambda *_: None)
    monkeypatch.setattr(server_mod, "IMAGE_DIR", tmp_path)
    return TestClient(server_mod.create_app())


def test_list_empty(client):
    d = client.get("/api/peers").json()
    assert d["peers"] == [] and isinstance(d["serving"], bool)


def test_serve_toggle_persists(client, monkeypatch, tmp_path):
    # Keep cfg.save() inside tmp (the endpoint does Config.load() + cfg.save()).
    monkeypatch.setattr("evi.config.HOME", tmp_path)
    monkeypatch.setattr("evi.config.CONFIG_PATH", tmp_path / "config.toml")
    assert client.get("/api/peers").json()["serving"] is False
    r = client.post("/api/peers/serve", json={"enabled": True})
    assert r.status_code == 200 and r.json()["serving"] is True
    # round-trips through the on-disk config (no hand-editing config.toml)
    from evi.config import Config
    assert Config.load().federation.serve is True
    assert client.get("/api/peers").json()["serving"] is True
    # and back off
    assert client.post("/api/peers/serve", json={"enabled": False}).json()["serving"] is False


def test_bind_lan_toggle_persists(client, monkeypatch, tmp_path):
    monkeypatch.setattr("evi.config.HOME", tmp_path)
    monkeypatch.setattr("evi.config.CONFIG_PATH", tmp_path / "config.toml")
    assert client.get("/api/peers").json()["bind_lan"] is False
    r = client.post("/api/peers/bind-lan", json={"enabled": True})
    assert r.status_code == 200 and r.json()["bind_lan"] is True
    from evi.config import Config
    assert Config.load().federation.bind_lan is True
    assert client.get("/api/peers").json()["bind_lan"] is True


def test_add_list_remove(client, peers_path, monkeypatch):
    # avoid real network probes in status rows
    monkeypatch.setattr(
        "evi.federation.check_peer",
        lambda p, timeout=2.0: {"reachable": True, "version": "1.0", "model": "m"},
    )
    r = client.post("/api/peers", json={"name": "gpu", "url": "http://h:8473/",
                                        "token": "secret"})
    assert r.status_code == 200
    saved = json.loads(peers_path.read_text(encoding="utf-8"))
    assert saved[0]["name"] == "gpu" and saved[0]["url"] == "http://h:8473"

    d = client.get("/api/peers").json()
    p = d["peers"][0]
    assert p["name"] == "gpu" and p["has_token"] is True
    assert p["reachable"] is True and p["version"] == "1.0"
    assert "token" not in p  # the secret itself is never echoed back

    assert client.post("/api/peers/remove", json={"name": "gpu"}).status_code == 200
    assert client.get("/api/peers").json()["peers"] == []


def test_add_requires_name_and_url(client):
    assert client.post("/api/peers", json={"name": "x"}).status_code == 400
    assert client.post("/api/peers", json={"url": "http://h"}).status_code == 400


def test_remove_unknown_404(client):
    assert client.post("/api/peers/remove", json={"name": "ghost"}).status_code == 404


def test_scan_marks_configured(client, monkeypatch):
    client.post("/api/peers", json={"name": "gpu", "url": "http://10.0.0.5:8473"})
    monkeypatch.setattr(
        "evi.federation.scan_network",
        lambda port, hosts=None: [
            {"host": "10.0.0.5", "url": "http://10.0.0.5:8473", "version": "1.0", "model": "m"},
            {"host": "10.0.0.9", "url": "http://10.0.0.9:8473", "version": "1.0", "model": "m"},
        ],
    )
    d = client.post("/api/peers/scan", json={}).json()
    by = {f["host"]: f for f in d["found"]}
    assert by["10.0.0.5"]["configured"] is True
    assert by["10.0.0.9"]["configured"] is False
    assert d["port"] == 8473


def test_scan_validates_hosts(client):
    r = client.post("/api/peers/scan", json={"hosts": "not-a-list"})
    assert r.status_code == 400
