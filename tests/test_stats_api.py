"""Web API for the usage/stats panel — GET /api/stats (mirrors `evi stats`)."""

from __future__ import annotations

import json

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

import evi.apps.web.server as server_mod  # noqa: E402


def _seed(transcripts, day, sid, entries):
    d = transcripts / day
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{sid}.jsonl").write_text(
        "\n".join(json.dumps(e) for e in entries) + "\n", encoding="utf-8"
    )


@pytest.fixture
def transcripts(monkeypatch, tmp_path):
    t = tmp_path / "transcripts"
    t.mkdir()
    monkeypatch.setattr("evi.sessions.TRANSCRIPTS_DIR", t)
    return t


@pytest.fixture
def client(monkeypatch, transcripts, tmp_path):
    monkeypatch.setattr(server_mod, "make_client", lambda *_: None)
    monkeypatch.setattr(server_mod, "IMAGE_DIR", tmp_path)
    return TestClient(server_mod.create_app())


def test_stats_empty(client):
    d = client.get("/api/stats").json()
    assert d["sessions"] == 0 and d["messages"] == 0
    assert d["roles"] == {} and d["tools"] == {}


def test_stats_aggregates(client, transcripts):
    _seed(transcripts, "2026-06-01", "s1", [
        {"role": "user", "content": "hello there", "ts": 1000.0},
        {"role": "assistant", "content": "hi back", "ts": 1001.0},
        {"role": "tool", "tool_name": "read_file", "content": "x", "ts": 1002.0},
        {"role": "tool", "tool_name": "read_file", "content": "y", "ts": 1003.0},
    ])
    _seed(transcripts, "2026-06-02", "s2", [
        {"role": "user", "content": "again", "ts": 2000.0},
        {"role": "assistant", "content": "ok", "ts": 2001.0},
    ])
    d = client.get("/api/stats").json()
    assert d["sessions"] == 2
    assert d["messages"] == 6
    assert d["roles"] == {"user": 2, "assistant": 2, "tool": 2}
    assert d["tools"] == {"read_file": 2}
    assert set(d["busiest_days"]) == {"2026-06-01", "2026-06-02"}
    assert d["first_ts"] == 1000.0 and d["last_ts"] == 2001.0
    assert d["approx_tokens"] >= 0


def test_stats_days_filter(client, transcripts):
    _seed(transcripts, "2026-06-01", "s1", [{"role": "user", "content": "a", "ts": 1.0}])
    _seed(transcripts, "2026-06-02", "s2", [{"role": "user", "content": "b", "ts": 2.0}])
    # days=1 walks only the newest calendar day directory
    assert client.get("/api/stats?days=1").json()["sessions"] == 1
    assert client.get("/api/stats").json()["sessions"] == 2
