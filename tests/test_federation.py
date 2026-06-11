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


# ---- managing peers.json ---------------------------------------------------


def test_save_add_remove_roundtrip(tmp_path):
    p = tmp_path / "peers.json"
    assert federation.add_peer(Peer(name="gpu", url="http://h:8473", token="t"), p)
    assert federation.add_peer(Peer(name="cpu", url="http://h2:8473"), p)
    names = [x.name for x in federation.load_peers(p)]
    assert names == ["gpu", "cpu"]
    # duplicate name: rejected without overwrite, replaced with it
    assert not federation.add_peer(Peer(name="gpu", url="http://new:8473"), p)
    assert federation.add_peer(Peer(name="gpu", url="http://new:8473"), p, overwrite=True)
    assert federation.get_peer("gpu", federation.load_peers(p)).url == "http://new:8473"
    assert federation.remove_peer("cpu", p)
    assert not federation.remove_peer("ghost", p)
    assert [x.name for x in federation.load_peers(p)] == ["gpu"]


# ---- discovery (probe / check / scan) --------------------------------------


def _serve_health(payload: dict):
    """A persistent local HTTP server answering GET /api/health with payload.
    Returns (server, port); call .shutdown() + .server_close() when done."""
    body = json.dumps(payload).encode()

    class H(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            if self.path == "/api/health":
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(body)
            else:
                self.send_response(404)
                self.end_headers()

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, srv.server_address[1]


def test_probe_evi_fingerprint():
    srv, port = _serve_health({"ok": True, "version": "0.31.0", "model": "qwen2.5:14b"})
    try:
        info = federation.probe_evi("127.0.0.1", port, timeout=5)
    finally:
        srv.shutdown()
        srv.server_close()
    assert info == {
        "host": "127.0.0.1",
        "url": f"http://127.0.0.1:{port}",
        "version": "0.31.0",
        "model": "qwen2.5:14b",
    }


def test_probe_evi_rejects_non_evi_server():
    # answers HTTP but without the eVi health shape
    srv, port = _serve_health({"status": "up"})
    try:
        assert federation.probe_evi("127.0.0.1", port, timeout=5) is None
    finally:
        srv.shutdown()
        srv.server_close()


def test_probe_evi_closed_port_is_none():
    assert federation.probe_evi("127.0.0.1", 1, timeout=0.5) is None


def test_check_peer_reachable_and_not():
    srv, port = _serve_health({"ok": True, "version": "1.0", "model": "m"})
    try:
        up = federation.check_peer(Peer(name="a", url=f"http://127.0.0.1:{port}"), timeout=5)
    finally:
        srv.shutdown()
        srv.server_close()
    assert up == {"reachable": True, "version": "1.0", "model": "m"}
    down = federation.check_peer(Peer(name="b", url="http://127.0.0.1:1"), timeout=0.5)
    assert down["reachable"] is False


def test_scan_network_finds_instance_on_host_list():
    srv, port = _serve_health({"ok": True, "version": "1.0", "model": "m"})
    try:
        found = federation.scan_network(port, hosts=["127.0.0.1"], timeout=5)
    finally:
        srv.shutdown()
        srv.server_close()
    assert len(found) == 1 and found[0]["host"] == "127.0.0.1"


def test_scan_network_empty_hosts():
    assert federation.scan_network(8473, hosts=[]) == []
