"""Tests for multi-user web auth (opt-in)."""

from __future__ import annotations

import json

import pytest

from evi import users
from evi.users import User


def _write_users(path, items):
    path.write_text(json.dumps(items), encoding="utf-8")


def test_load_users(tmp_path):
    p = tmp_path / "users.json"
    _write_users(p, [{"name": "alice", "token": "a"}, {"name": "bad"}])
    out = users.load_users(p)
    assert [u.name for u in out] == ["alice"]


def test_load_missing(tmp_path):
    assert users.load_users(tmp_path / "none.json") == []


def test_authenticate():
    us = [User("alice", "a-token"), User("bob", "b-token")]
    assert users.authenticate("b-token", us).name == "bob"
    assert users.authenticate("nope", us) is None
    assert users.authenticate("", us) is None


def test_add_remove_roundtrip(tmp_path):
    p = tmp_path / "users.json"
    u = users.add_user("alice", p)
    assert u.token and len(u.token) > 10
    assert [x.name for x in users.load_users(p)] == ["alice"]
    # re-issue replaces (no dup), new token
    u2 = users.add_user("alice", p)
    assert u2.token != u.token
    assert len(users.load_users(p)) == 1
    assert users.remove_user("alice", p) is True
    assert users.load_users(p) == []
    assert users.remove_user("alice", p) is False


# ---- middleware integration ---------------------------------------------


def _client(monkeypatch, tmp_path, *, multi_user, users_list=None):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    import evi.apps.web.server as server_mod
    import evi.config as config_mod

    cfg = tmp_path / "config.toml"
    cfg.write_text(f"[web]\nmulti_user = {'true' if multi_user else 'false'}\n", encoding="utf-8")
    monkeypatch.setattr(config_mod, "CONFIG_PATH", cfg)
    monkeypatch.setattr(config_mod, "HOME", tmp_path)
    # users.py binds USERS_PATH at import — patch the bound name it actually reads.
    monkeypatch.setattr("evi.users.USERS_PATH", tmp_path / "users.json")
    if users_list:
        _write_users(tmp_path / "users.json", users_list)
    monkeypatch.setattr(server_mod, "IMAGE_DIR", tmp_path)
    return TestClient(server_mod.create_app(), raise_server_exceptions=False)


def test_multi_user_token_authenticates(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path, multi_user=True,
                     users_list=[{"name": "alice", "token": "alice-tok"}])
    # health is public
    assert client.get("/api/health").status_code == 200
    # a gated endpoint requires a valid user token
    assert client.get("/api/whoami").status_code == 401
    r = client.get("/api/whoami", headers={"Authorization": "Bearer alice-tok"})
    assert r.status_code == 200 and r.json()["user"] == "alice"
    assert client.get("/api/whoami", headers={"Authorization": "Bearer wrong"}).status_code == 401


def test_multi_user_off_is_open(monkeypatch, tmp_path):
    # No auth_token + multi_user off => open access (single-user default).
    client = _client(monkeypatch, tmp_path, multi_user=False)
    assert client.get("/api/whoami").status_code == 200


def _isolation_client(monkeypatch, tmp_path):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    import evi.apps.web.server as server_mod
    import evi.config as config_mod
    from evi.config import Config
    from evi.llm.agent import Done, TextDelta

    cfg = tmp_path / "config.toml"
    cfg.write_text("[web]\nmulti_user = true\n", encoding="utf-8")
    monkeypatch.setattr(config_mod, "CONFIG_PATH", cfg)
    monkeypatch.setattr(config_mod, "HOME", tmp_path)
    monkeypatch.setattr("evi.users.USERS_PATH", tmp_path / "users.json")
    _write_users(tmp_path / "users.json",
                 [{"name": "alice", "token": "A"}, {"name": "bob", "token": "B"}])

    class _FakeAgent:
        def __init__(self, *_, **__):
            self.config = Config()
            self.tools: dict = {}
            self.history: list[dict] = [{"role": "system", "content": "s"}]
            self.pending: dict = {}

        def chat(self, *a, **k):
            yield TextDelta("ok")
            yield Done(reason="stop")

        def token_usage(self):
            return (0, 0)

    import evi.sdk.builder as builder_mod
    monkeypatch.setattr(builder_mod, "build_agent", lambda *_, **__: _FakeAgent())
    monkeypatch.setattr(server_mod, "make_client", lambda *_: None)
    monkeypatch.setattr(server_mod, "IMAGE_DIR", tmp_path)
    return TestClient(server_mod.create_app(), raise_server_exceptions=False)


def test_sessions_isolated_per_user(monkeypatch, tmp_path):
    """The crux: same session id, different users -> fully separate sessions."""
    client = _isolation_client(monkeypatch, tmp_path)
    A = {"Authorization": "Bearer A"}
    B = {"Authorization": "Bearer B"}

    # Both push to a session called "s1" (creates it in each user's bucket).
    client.post("/api/session/s1/channel", json={"text": "secret-A"}, headers=A)
    client.post("/api/session/s1/channel", json={"text": "hello-B"}, headers=B)

    amsgs = client.get("/api/session/s1/channel", headers=A).json()["messages"]
    bmsgs = client.get("/api/session/s1/channel", headers=B).json()["messages"]
    # Each user sees ONLY their own message — no cross-user leak.
    assert [m["text"] for m in amsgs] == ["secret-A"]
    assert [m["text"] for m in bmsgs] == ["hello-B"]

    # The dispatch view is scoped per user too.
    da = client.get("/api/dispatch", headers=A).json()
    db = client.get("/api/dispatch", headers=B).json()
    assert [s["id"] for s in da["sessions"]] == ["s1"]
    assert [s["id"] for s in db["sessions"]] == ["s1"]
    assert client.get("/api/whoami", headers=A).json()["user"] == "alice"
