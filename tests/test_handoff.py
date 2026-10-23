"""Tests for cross-device session handoff (Phase 87)."""

from __future__ import annotations

import json
from pathlib import Path

from evi import sessions


def _write_session(root, day, sid, n_msgs=2):
    d = root / day
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{sid}.jsonl"
    with p.open("w", encoding="utf-8") as f:
        for i in range(n_msgs):
            role = "user" if i % 2 == 0 else "assistant"
            f.write(json.dumps({"session": sid, "ts": float(i + 1), "role": role, "content": "x"}) + "\n")
    return p


def test_handoff_info_for_known_session(tmp_path):
    root = tmp_path / "transcripts"
    _write_session(root, "2026-06-01", "s1", n_msgs=3)
    info = sessions.handoff_info("s1", base_url="http://host:8000/", root=root)
    assert info is not None
    assert info["session_id"] == "s1"
    assert info["day"] == "2026-06-01"
    assert info["messages"] == 3
    assert info["resume_cmd"] == "evi sessions resume s1"
    assert info["resume_url"] == "http://host:8000/?session=s1"


def test_handoff_info_unknown_session(tmp_path):
    assert sessions.handoff_info("nope", root=tmp_path / "transcripts") is None


def test_handoff_url_without_base(tmp_path):
    root = tmp_path / "transcripts"
    _write_session(root, "2026-06-02", "s2")
    info = sessions.handoff_info("s2", root=root)
    assert info["resume_url"] == "/?session=s2"


# ---- web endpoint --------------------------------------------------------


def test_handoff_endpoint(monkeypatch, tmp_path):
    import pytest

    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    tdir = tmp_path / "transcripts"
    hdir = tmp_path / "handoff"
    hdir.mkdir()
    _write_session(tdir, "2026-06-08", "h1", n_msgs=4)
    monkeypatch.setattr("evi.transcripts.TRANSCRIPTS_DIR", tdir)
    monkeypatch.setattr("evi.sessions.TRANSCRIPTS_DIR", tdir)
    monkeypatch.setattr("evi.sessions.HANDOFF_DIR", hdir)

    from evi.apps.web.server import create_app

    client = TestClient(create_app())
    r = client.post("/api/session/h1/handoff")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] and body["messages"] == 4
    assert body["resume_url"].endswith("/?session=h1")
    assert body["resume_cmd"] == "evi sessions resume h1"
    assert body["staged"] is not None
    assert Path(body["staged"]).name == "h1.jsonl"
    assert (hdir / "h1.jsonl").exists()
    assert (hdir / "h1.jsonl").read_text().count("\n") == 4

    # Unknown / unpersisted session → 404.
    assert client.post("/api/session/missing/handoff").status_code == 404
    assert not (hdir / "missing.jsonl").exists()

    # OSError during staging → 500.
    def disk_full(_sid):
        raise OSError("disk full")

    monkeypatch.setattr("evi.sessions.stage_handoff", disk_full)
    r = client.post("/api/session/h1/handoff")
    assert r.status_code == 500
    assert "could not copy the session for sync" in r.json()["detail"]
    assert "disk full" not in r.json()["detail"]  # the reason stays in the server log


def test_handoff_multi_user(monkeypatch, tmp_path):
    import pytest
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    from tests.test_web_hardening import _make_app

    hdir = tmp_path / "handoff"
    monkeypatch.setattr("evi.sessions.HANDOFF_DIR", hdir)
    app = _make_app(monkeypatch, tmp_path, token="own", multi_user=True,
                    users=[{"name": "alice", "token": "al"}])
    client = TestClient(app)

    # Alice's session h2
    alice_root = tmp_path / "home" / "users" / "alice" / "transcripts"
    _write_session(alice_root, "2026-06-09", "h2", n_msgs=5)

    # Handoff as Alice
    r = client.post("/api/session/h2/handoff", headers={"Authorization": "Bearer al"})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] and body["messages"] == 5
    assert body["staged"] is None

    # A login's session is not copied into the owner's sync folder.
    assert not hdir.exists() or not any(hdir.iterdir())
