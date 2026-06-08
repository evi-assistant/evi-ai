"""The web/desktop chat must survive a restart — a session that only exists on
disk (transcript) is revived by /api/session/{id}/history instead of 404'ing
into a blank chat. (Bug: desktop reopened to an empty chat.)"""

from __future__ import annotations

import json

from fastapi.testclient import TestClient


def _write_transcript(tdir, session, entries):
    day = tdir / "2026-06-08"
    day.mkdir(parents=True, exist_ok=True)
    (day / f"{session}.jsonl").write_text(
        "".join(json.dumps(e) + "\n" for e in entries), encoding="utf-8"
    )


def test_history_revives_from_transcript(monkeypatch, tmp_path):
    tdir = tmp_path / "transcripts"
    _write_transcript(tdir, "s1", [
        {"session": "s1", "ts": 1, "role": "user", "content": "hello there"},
        {"session": "s1", "ts": 2, "role": "assistant", "content": "hi back"},
    ])
    # TranscriptStore() + find_session() read their module-level TRANSCRIPTS_DIR.
    monkeypatch.setattr("evi.transcripts.TRANSCRIPTS_DIR", tdir)
    monkeypatch.setattr("evi.sessions.TRANSCRIPTS_DIR", tdir)

    from evi.apps.web.server import create_app

    client = TestClient(create_app())
    msgs = client.get("/api/session/s1/history").json()["messages"]
    contents = [m["content"] for m in msgs]
    # the user + assistant turns are restored from disk
    assert "hello there" in contents and "hi back" in contents
    assert any(m["role"] == "user" for m in msgs)
    assert any(m["role"] == "assistant" for m in msgs)


def test_brand_new_session_has_no_chat(monkeypatch, tmp_path):
    monkeypatch.setattr("evi.transcripts.TRANSCRIPTS_DIR", tmp_path / "t")
    monkeypatch.setattr("evi.sessions.TRANSCRIPTS_DIR", tmp_path / "t")
    from evi.apps.web.server import create_app

    client = TestClient(create_app())
    msgs = client.get("/api/session/never-seen/history").json()["messages"]
    # no user/assistant turns (only the internal system prompt, if any)
    assert not any(m["role"] in ("user", "assistant") for m in msgs)
