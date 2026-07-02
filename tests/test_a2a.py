"""Tests for the A2A (Agent2Agent) adapter — card, JSON-RPC dispatch, client."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from evi import a2a
from evi.config import Config


def setup_function(_):
    a2a.reset_tasks()


# ---- Agent Card ----------------------------------------------------------


def test_build_agent_card_shape():
    cfg = Config()
    cfg.llm.model = "qwen2.5-vl-7b"
    cfg.federation.a2a = True
    card = a2a.build_agent_card(cfg, url="http://host:8473/")
    assert card["protocolVersion"] == a2a.A2A_PROTOCOL_VERSION
    assert card["url"] == "http://host:8473/a2a"  # base + /a2a, trailing slash trimmed
    assert card["preferredTransport"] == "JSONRPC"
    assert card["capabilities"] == {"streaming": False, "pushNotifications": False}
    assert card["defaultInputModes"] == ["text/plain"]
    assert isinstance(card["skills"], list) and card["skills"][0]["id"] == "assistant"
    assert card["securitySchemes"]["bearer"] == {"type": "http", "scheme": "bearer"}
    xevi = card["x-evi"]
    assert xevi["model"] == "qwen2.5-vl-7b"
    assert isinstance(xevi["capabilities"], dict)
    assert xevi["serve"] is True


# ---- JSON-RPC dispatch ---------------------------------------------------


def _msg(text):
    return {"role": "user", "messageId": "m1", "parts": [{"kind": "text", "text": text}]}


def _send(text, runner):
    body = {"jsonrpc": "2.0", "id": "1", "method": "message/send",
            "params": {"message": _msg(text)}}
    return a2a.handle_rpc(body, runner)


def test_message_send_completed_and_tasks_get():
    resp = _send("do a thing", lambda t: (f"did: {t}", ""))
    assert resp["jsonrpc"] == "2.0" and resp["id"] == "1"
    task = resp["result"]
    assert task["kind"] == "task"
    assert task["status"]["state"] == "completed"
    assert task["artifacts"][0]["parts"][0]["text"] == "did: do a thing"
    tid = task["id"]
    got = a2a.handle_rpc(
        {"jsonrpc": "2.0", "id": "2", "method": "tasks/get", "params": {"id": tid}},
        lambda t: ("", ""),
    )
    assert got["result"]["id"] == tid


def test_message_send_failed():
    task = _send("x", lambda t: ("", "model down"))["result"]
    assert task["status"]["state"] == "failed"
    assert "model down" in task["status"]["message"]["parts"][0]["text"]


def test_message_send_runner_raises_becomes_failed_task():
    def boom(_):
        raise RuntimeError("kaboom")

    task = _send("x", boom)["result"]
    assert task["status"]["state"] == "failed"
    assert "kaboom" in task["status"]["message"]["parts"][0]["text"]


def test_message_send_no_text_is_invalid_params():
    body = {"jsonrpc": "2.0", "id": "1", "method": "message/send",
            "params": {"message": {"role": "user", "parts": []}}}
    resp = a2a.handle_rpc(body, lambda t: ("x", ""))
    assert resp["error"]["code"] == -32602


def test_unknown_method():
    resp = a2a.handle_rpc(
        {"jsonrpc": "2.0", "id": "1", "method": "frobnicate"}, lambda t: ("", "")
    )
    assert resp["error"]["code"] == -32601


def test_invalid_jsonrpc_request():
    resp = a2a.handle_rpc({"method": "message/send"}, lambda t: ("", ""))
    assert resp["error"]["code"] == -32600


def test_tasks_get_not_found():
    resp = a2a.handle_rpc(
        {"jsonrpc": "2.0", "id": "1", "method": "tasks/get", "params": {"id": "nope"}},
        lambda t: ("", ""),
    )
    assert resp["error"]["code"] == -32001


def test_tasks_cancel_terminal_is_not_cancelable():
    tid = _send("x", lambda t: ("done", ""))["result"]["id"]
    resp = a2a.handle_rpc(
        {"jsonrpc": "2.0", "id": "2", "method": "tasks/cancel", "params": {"id": tid}},
        lambda t: ("", ""),
    )
    assert resp["error"]["code"] == -32002  # already completed → not cancelable


def test_message_text_handles_kind_and_type_and_bare():
    assert a2a.message_text({"parts": [{"kind": "text", "text": "a"}]}) == "a"
    assert a2a.message_text({"parts": [{"type": "text", "text": "b"}]}) == "b"
    assert a2a.message_text({"parts": [{"text": "c"}]}) == "c"
    assert a2a.message_text({"parts": []}) == ""


# ---- client (eVi calling an external A2A agent) --------------------------


def _serve_once(handler_cls):
    srv = HTTPServer(("127.0.0.1", 0), handler_cls)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def test_client_send_roundtrip():
    class H(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            self.rfile.read(int(self.headers.get("Content-Length", 0) or 0))
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            task = {
                "id": "t1", "kind": "task", "status": {"state": "completed"},
                "artifacts": [{"artifactId": "a1", "parts": [{"kind": "text", "text": "remote answer"}]}],
            }
            self.wfile.write(json.dumps({"jsonrpc": "2.0", "id": "1", "result": task}).encode())

        def log_message(self, *a):
            pass

    srv = _serve_once(H)
    try:
        out = a2a.client_send(f"http://127.0.0.1:{srv.server_address[1]}/a2a", "hi", timeout=5)
    finally:
        srv.shutdown()
        srv.server_close()
    assert out == "remote answer"


def test_client_send_error_response_raises():
    class H(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            self.rfile.read(int(self.headers.get("Content-Length", 0) or 0))
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(
                json.dumps({"jsonrpc": "2.0", "id": "1",
                            "error": {"code": -32603, "message": "boom"}}).encode()
            )

        def log_message(self, *a):
            pass

    srv = _serve_once(H)
    try:
        with pytest.raises(a2a.A2AError):
            a2a.client_send(f"http://127.0.0.1:{srv.server_address[1]}/a2a", "hi", timeout=5)
    finally:
        srv.shutdown()
        srv.server_close()


def test_client_send_unreachable_raises():
    with pytest.raises(a2a.A2AError):
        a2a.client_send("http://127.0.0.1:1/a2a", "hi", timeout=0.5)


# ---- POST /a2a endpoint (gated + non-interactive) ------------------------


def _a2a_client(monkeypatch, tmp_path, *, enabled: bool):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    import evi.apps.web.server as server_mod
    import evi.config as config_mod
    from evi.llm.agent import Done, TextDelta

    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(
        f"[federation]\na2a = {'true' if enabled else 'false'}\n", encoding="utf-8"
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
            yield TextDelta(f"echo:{msg}")
            yield Done(reason="stop")

    import evi.sdk.builder as builder_mod
    monkeypatch.setattr(builder_mod, "build_agent", lambda *_, **__: _FakeAgent())
    monkeypatch.setattr(server_mod, "make_client", lambda *_: None)
    monkeypatch.setattr(server_mod, "IMAGE_DIR", tmp_path)
    return TestClient(server_mod.create_app())


def _rpc(text):
    return {"jsonrpc": "2.0", "id": "1", "method": "message/send",
            "params": {"message": _msg(text)}}


def test_a2a_endpoint_disabled_is_403(monkeypatch, tmp_path):
    client = _a2a_client(monkeypatch, tmp_path, enabled=False)
    assert client.post("/a2a", json=_rpc("hi")).status_code == 403


def test_a2a_endpoint_runs_when_enabled(monkeypatch, tmp_path):
    client = _a2a_client(monkeypatch, tmp_path, enabled=True)
    r = client.post("/a2a", json=_rpc("hi"))
    assert r.status_code == 200
    task = r.json()["result"]
    assert task["status"]["state"] == "completed"
    assert "echo:hi" in task["artifacts"][0]["parts"][0]["text"]
