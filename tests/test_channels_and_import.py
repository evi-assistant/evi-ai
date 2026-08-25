"""Inbound channels (Telegram) + importing other CLIs' sessions.

The channel tests are mostly about ONE property: an unknown sender must never
reach the agent. That's the difference between a convenience feature and remote
code execution on the user's machine, so it's asserted directly rather than
inferred from the pairing helpers behaving.
"""

from __future__ import annotations

import json

import pytest

from evi.channels import pairing, telegram
from evi.channels import telegram_api as api
from evi.config import Config

@pytest.fixture(autouse=True)
def _isolated_pairing_store(tmp_path, monkeypatch):
    """Each test gets its own approval store.

    Without this the tests share ~/.evi/channels/telegram.json, so one test's
    leftover pending request is another's `pending_requests()[0]` — which is
    exactly how a test can approve the wrong sender and still look green.
    """
    monkeypatch.setattr(pairing, "HOME", tmp_path)


# --------------------------------------------------------------- pairing gate


def test_unpaired_sender_never_reaches_the_agent(monkeypatch):
    """The load-bearing security property of the whole feature."""

    def explode(*a, **k):
        raise AssertionError("an agent turn ran for an unpaired sender")

    monkeypatch.setattr(telegram, "_reply_for", explode)
    msg = {"user_id": 999, "chat_id": 1, "name": "stranger", "text": "run rm -rf /"}
    reply = telegram.handle_message(msg, Config())

    assert "pairing code" in reply.lower()
    assert not pairing.is_paired("telegram", 999)
    # Repeating must stay gated (and must not mint a second code).
    assert "pairing code" in telegram.handle_message(msg, Config()).lower()
    assert len(pairing.pending_requests("telegram")) == 1


def test_approval_opens_the_gate_and_revoke_closes_it(monkeypatch):
    monkeypatch.setattr(telegram, "_reply_for", lambda text, m, c: f"answered:{text}")
    msg = {"user_id": 7, "chat_id": 1, "name": "dk", "text": "hello"}

    telegram.handle_message(msg, Config())                      # -> pairing code
    code = pairing.pending_requests("telegram")[0]["code"]
    assert pairing.approve("telegram", code.lower()) is not None  # case-insensitive
    assert telegram.handle_message(msg, Config()) == "answered:hello"

    assert pairing.revoke("telegram", 7) is True
    assert "pairing code" in telegram.handle_message(msg, Config()).lower()


def test_expired_pairing_requests_are_pruned(monkeypatch):
    pairing.request_pairing("telegram", 55, "old")
    monkeypatch.setattr(pairing, "PENDING_TTL", -1.0)          # everything is stale
    assert pairing.pending_requests("telegram") == []
    assert pairing.approve("telegram", "ANYCOD") is None


def test_approved_senders_get_a_narrow_toolset_not_the_users(monkeypatch):
    """`build_agent(tool_categories=...)` must be passed explicitly, so enabling
    shell locally can never widen what a paired stranger can do."""
    seen: dict = {}

    def fake_build_agent(**kw):
        seen.update(kw)

        class _A:
            permission_callback = None
            permission_batch_callback = None

        return _A()

    monkeypatch.setattr("evi.sdk.build_agent", fake_build_agent)
    monkeypatch.setattr("evi.sdk.run_headless",
                        lambda a, t, max_turns=6: type("R", (), {"text": "ok", "error": ""})())

    cfg = Config()
    cfg.tools.shell = True          # the local user is permissive...
    telegram._reply_for("hi", {"user_id": 1}, cfg)

    assert seen["tool_categories"] == ["memory", "skills"]      # ...the bot is not
    assert "shell" not in seen["tool_categories"]
    assert seen["enable_project"] is False and seen["enable_hooks"] is False


def test_channel_is_off_without_enable_and_token():
    cfg = Config()
    assert telegram.start_if_configured(cfg) is None            # both missing
    cfg.channels.telegram_enabled = True
    assert telegram.start_if_configured(cfg) is None            # token still missing


# ------------------------------------------------------------------ bot API


@pytest.mark.parametrize(
    ("update", "expected"),
    [
        ({"update_id": 1, "message": {"text": "hi", "chat": {"id": 5},
                                      "from": {"id": 9, "username": "dk"}}}, "dk"),
        ({"update_id": 2, "message": {"text": "hi", "chat": {"id": 5},
                                      "from": {"id": 9, "first_name": "A", "last_name": "B"}}}, "A B"),
    ],
)
def test_parse_message_extracts_sender(update, expected):
    assert api.parse_message(update)["name"] == expected


def test_parse_message_ignores_non_text():
    assert api.parse_message({"update_id": 3, "message": {"photo": [], "chat": {"id": 1}}}) is None
    assert api.parse_message({"update_id": 4}) is None


def test_config_channels_round_trips():
    cfg = Config()
    cfg.channels.telegram_enabled = True
    cfg.channels.telegram_token = "tok"
    cfg.save()
    # The [desktop] bug: a section absent from Config.load() is silently write-only.
    back = Config.load()
    assert back.channels.telegram_enabled is True
    assert back.channels.telegram_token == "tok"


# --------------------------------------------------------------- session import


def _write(p, records):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("\n".join(json.dumps(r) for r in records), encoding="utf-8")


def test_parses_claude_code_sessions(tmp_path):
    from evi import sessionimport as si

    f = tmp_path / "proj" / "abc.jsonl"
    _write(f, [
        {"type": "queue-operation", "sessionId": "x"},                 # bookkeeping
        {"type": "user", "timestamp": "2026-08-01T10:00:00Z",
         "message": {"role": "user", "content": "hello"}},
        {"type": "assistant", "timestamp": "2026-08-01T10:00:05Z",
         "message": {"role": "assistant", "content": [
             {"type": "thinking", "thinking": "hmm"},                  # dropped
             {"type": "text", "text": "hi there"},
             {"type": "tool_use", "name": "read_file"}]}},
    ])
    s = si.parse_claude_session(f)
    assert s and s.turns == 1 and len(s.messages) == 2
    assert s.messages[1]["content"] == "hi there"        # thinking is not transcript
    assert s.messages[1]["tools"] == ["read_file"]
    assert s.label == "proj"


def test_parses_codex_sessions(tmp_path):
    from evi import sessionimport as si

    f = tmp_path / "rollout-x.jsonl"
    _write(f, [
        {"type": "session_meta", "timestamp": "2026-08-01T10:00:00Z", "payload": {"cwd": "/w"}},
        {"type": "response_item", "timestamp": "2026-08-01T10:00:01Z",
         "payload": {"type": "message", "role": "developer",
                     "content": [{"type": "input_text", "text": "SYSTEM"}]}},
        {"type": "response_item", "timestamp": "2026-08-01T10:00:02Z",
         "payload": {"type": "message", "role": "user",
                     "content": [{"type": "input_text", "text": "ping"}]}},
        {"type": "response_item", "timestamp": "2026-08-01T10:00:03Z",
         "payload": {"type": "message", "role": "assistant",
                     "content": [{"type": "output_text", "text": "pong"}]}},
    ])
    s = si.parse_codex_session(f)
    assert s and [m["role"] for m in s.messages] == ["user", "assistant"]
    assert all("SYSTEM" not in m["content"] for m in s.messages)   # developer dropped
    assert s.label == "/w"


def test_import_never_writes_tool_calls(tmp_path):
    """An assistant message carrying tool_calls MUST be followed by matching tool
    results or the API rejects the history. The source logs don't give us those,
    so an import that emitted tool_calls would make the session unresumable."""
    import datetime as dt

    from evi import sessionimport as si
    from evi.transcripts import TranscriptStore

    sess = si.ImportedSession(
        source="claude-code", session_id="s1", path=tmp_path / "s1.jsonl", started=1_760_000_000.0,
        messages=[
            {"role": "user", "content": "do it", "ts": 1_760_000_000.0, "tools": []},
            {"role": "assistant", "content": "done", "ts": 1_760_000_001.0,
             "tools": ["read_file", "read_file", "shell"]},
        ],
    )
    store = TranscriptStore(root=tmp_path / "t")
    sid = si.import_session(sess, store=store)
    entries = [e for e in store.iter_since(dt.datetime(2000, 1, 1)) if e.session == sid]

    assert len(entries) == 2
    assert all(e.tool_calls is None for e in entries)
    # Tools are recorded as text instead — de-duplicated, order preserved.
    assert "[used: read_file, shell]" in entries[1].content

    # And the result must rebuild into a valid history.
    from evi.sessions import history_from_transcript

    day = dt.datetime.fromtimestamp(entries[0].timestamp).strftime("%Y-%m-%d")
    hist = history_from_transcript(store.root / day / f"{sid}.jsonl")
    assert [m["role"] for m in hist] == ["user", "assistant"]
    assert not any("tool_calls" in m for m in hist)


def test_discover_tolerates_missing_roots(monkeypatch, tmp_path):
    from evi import sessionimport as si

    monkeypatch.setattr(si, "CLAUDE_ROOT", tmp_path / "nope")
    monkeypatch.setattr(si, "CODEX_ROOT", tmp_path / "also-nope")
    assert si.discover() == []


def test_read_jsonl_survives_a_torn_final_line(tmp_path):
    from evi import sessionimport as si

    f = tmp_path / "p" / "torn.jsonl"
    f.parent.mkdir(parents=True)
    f.write_text(
        json.dumps({"type": "user", "message": {"role": "user", "content": "kept"}})
        + '\n{"type": "assistant", "message"',       # crashed mid-write
        encoding="utf-8",
    )
    s = si.parse_claude_session(f)
    assert s and len(s.messages) == 1 and s.messages[0]["content"] == "kept"
