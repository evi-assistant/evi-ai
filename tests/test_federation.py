"""Tests for federation — eVi↔eVi delegation."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from evi import federation
from evi.federation import Peer


def _write_peers(path, peers):
    path.write_text(json.dumps(peers), encoding="utf-8")


def test_load_peers(tmp_path):
    p = tmp_path / "peers.json"
    _write_peers(p, [
        {"name": "gpu", "url": "http://host:8473/", "token": "t"},
        {"name": "bad-no-url"},
    ])
    peers = federation.load_peers(p)
    assert [x.name for x in peers] == ["gpu"]
    assert peers[0].url == "http://host:8473" and peers[0].token == "t"


def test_load_peers_missing(tmp_path):
    assert federation.load_peers(tmp_path / "nope.json") == []


def test_get_peer():
    peers = [Peer(name="GPU", url="u")]
    assert federation.get_peer("gpu", peers).url == "u"
    assert federation.get_peer("missing", peers) is None


def _serve(handler_cls):
    srv = HTTPServer(("127.0.0.1", 0), handler_cls)
    threading.Thread(target=srv.handle_request, daemon=True).start()
    return srv


def test_delegate_returns_text():
    received = {}

    class H(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            n = int(self.headers.get("Content-Length", 0))
            received.update(json.loads(self.rfile.read(n) or b"{}"))
            received["auth"] = self.headers.get("Authorization")
            self.send_response(200)
            self.end_headers()
            self.wfile.write(json.dumps({"text": "the answer"}).encode())

        def log_message(self, *a):
            pass

    srv = _serve(H)
    peer = Peer(name="p", url=f"http://127.0.0.1:{srv.server_address[1]}", token="secret")
    out = federation.delegate(peer, "do a thing", timeout=5)
    srv.server_close()
    assert out == "the answer"
    assert received["task"] == "do a thing"
    assert received["auth"] == "Bearer secret"


def test_delegate_peer_error_passes_through():
    class H(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            self.send_response(200)
            self.end_headers()
            self.wfile.write(json.dumps({"error": "model down"}).encode())

        def log_message(self, *a):
            pass

    srv = _serve(H)
    peer = Peer(name="p", url=f"http://127.0.0.1:{srv.server_address[1]}")
    out = federation.delegate(peer, "x", timeout=5)
    srv.server_close()
    assert out.startswith("ERROR:") and "model down" in out


def test_delegate_unreachable_raises():
    # Nothing listening on this port.
    peer = Peer(name="p", url="http://127.0.0.1:1")
    with pytest.raises(federation.FederationError):
        federation.delegate(peer, "x", timeout=2)


# ---- /api/federate endpoint ---------------------------------------------


def _fed_client(monkeypatch, tmp_path, *, serve: bool):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    import evi.apps.web.server as server_mod
    import evi.config as config_mod
    from evi.config import Config
    from evi.llm.agent import Done, TextDelta

    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(
        f"[federation]\nserve = {'true' if serve else 'false'}\n", encoding="utf-8"
    )
    monkeypatch.setattr(config_mod, "CONFIG_PATH", cfg_path)
    monkeypatch.setattr(config_mod, "HOME", tmp_path)

    class _FakeAgent:
        def __init__(self, *_, **__):
            self.config = Config()
            self.tools: dict = {}
            self.permission_callback = None
            self.permission_batch_callback = None

        def chat(self, msg, **kw):
            yield TextDelta("delegated answer")
            yield Done(reason="stop")

    monkeypatch.setattr(server_mod, "Agent", _FakeAgent)
    monkeypatch.setattr(server_mod, "make_client", lambda *_: None)
    monkeypatch.setattr(server_mod, "get_enabled_tools", lambda _: [])
    monkeypatch.setattr(server_mod, "IMAGE_DIR", tmp_path)
    return TestClient(server_mod.create_app())


def test_federate_disabled_is_403(monkeypatch, tmp_path):
    client = _fed_client(monkeypatch, tmp_path, serve=False)
    assert client.post("/api/federate", json={"task": "x"}).status_code == 403


def test_federate_runs_when_enabled(monkeypatch, tmp_path):
    client = _fed_client(monkeypatch, tmp_path, serve=True)
    r = client.post("/api/federate", json={"task": "do it"})
    assert r.status_code == 200 and r.json()["text"] == "delegated answer"
    assert client.post("/api/federate", json={"task": "  "}).status_code == 400
