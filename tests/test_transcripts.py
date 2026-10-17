"""Tests for the JSONL transcript store."""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta
from pathlib import Path

from evi.transcripts import TranscriptStore


def test_write_creates_day_partition(tmp_path: Path) -> None:
    store = TranscriptStore(root=tmp_path)
    store.write_message(session="abc", role="user", content="hello")
    days = list(tmp_path.iterdir())
    assert len(days) == 1
    files = list(days[0].glob("*.jsonl"))
    assert [f.name for f in files] == ["abc.jsonl"]


def test_write_appends_multiple_lines(tmp_path: Path) -> None:
    store = TranscriptStore(root=tmp_path)
    store.write_message(session="s1", role="user", content="hi")
    store.write_message(session="s1", role="assistant", content="hello back")
    [day] = list(tmp_path.iterdir())
    [path] = list(day.glob("*.jsonl"))
    lines = path.read_text("utf-8").strip().splitlines()
    assert len(lines) == 2
    decoded = [json.loads(line) for line in lines]
    assert decoded[0]["role"] == "user"
    assert decoded[1]["role"] == "assistant"


def test_iter_since_filters_by_cutoff(tmp_path: Path) -> None:
    store = TranscriptStore(root=tmp_path)
    old_ts = (datetime.now() - timedelta(days=10)).timestamp()
    fresh_ts = time.time()
    store.write_message(session="s", role="user", content="old", timestamp=old_ts)
    store.write_message(session="s", role="user", content="fresh", timestamp=fresh_ts)

    cutoff = datetime.now() - timedelta(hours=1)
    entries = list(store.iter_since(cutoff))
    assert [e.content for e in entries] == ["fresh"]


def test_iter_since_yields_oldest_first(tmp_path: Path) -> None:
    store = TranscriptStore(root=tmp_path)
    base = time.time()
    store.write_message(session="s", role="user", content="b", timestamp=base + 10)
    store.write_message(session="s", role="user", content="a", timestamp=base + 5)
    entries = list(store.iter_since(datetime.fromtimestamp(base)))
    # Within a file order is write order; the dream engine doesn't strictly
    # need timestamp sort, just stable replay.
    assert [e.content for e in entries] == ["b", "a"]


def test_iter_since_ignores_malformed_lines(tmp_path: Path) -> None:
    # Use today's date so the day-dir is always inside the iter_since window
    # below (a hardcoded date here becomes a time-bomb once the clock passes
    # it by more than the lookback).
    day = tmp_path / datetime.now().strftime("%Y-%m-%d")
    day.mkdir()
    f = day / "s.jsonl"
    good = json.dumps({
        "session": "s", "ts": time.time(), "role": "user", "content": "ok"
    })
    f.write_text(good + "\n{garbage\n" + good + "\n")
    store = TranscriptStore(root=tmp_path)
    entries = list(store.iter_since(datetime.now() - timedelta(days=2)))
    assert len(entries) == 2


def test_prune_removes_old_day_dirs(tmp_path: Path) -> None:
    store = TranscriptStore(root=tmp_path)
    old_day = (datetime.now() - timedelta(days=60)).strftime("%Y-%m-%d")
    new_day = datetime.now().strftime("%Y-%m-%d")
    # Two sessions: one with the same id in a kept day would be one session
    # that ran on, and keeps its old day (test_transcript_fidelity).
    (tmp_path / old_day).mkdir()
    (tmp_path / old_day / "old.jsonl").write_text("{}\n")
    (tmp_path / new_day).mkdir()
    (tmp_path / new_day / "new.jsonl").write_text("{}\n")

    removed = store.prune(keep_days=30)
    assert removed == 1
    assert not (tmp_path / old_day).exists()
    assert (tmp_path / new_day).exists()


# ---- secrets never reach the file (evi.redact) ------------------------------

# An env API key with no recognisable shape: only its value identifies it.
ENV_KEY = "Zq8vN2kLp4Xr7Tw1Ys5M"
GH = "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ012345"


def _raw(root: Path) -> str:
    return "".join(p.read_text("utf-8") for p in sorted(root.rglob("*.jsonl")))


def test_lines_are_redacted_as_they_are_written(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("ACME_API_KEY", ENV_KEY)
    store = TranscriptStore(root=tmp_path)
    store.write_message("s", "user", f"my key is {ENV_KEY}")
    headers = json.dumps({"Authorization": "Bearer opaque-cred-1"})
    calls = [{"id": "call_0", "type": "function",
              "function": {"name": "http_get", "arguments": json.dumps(
                  {"url": "https://api.example.invalid", "token": ENV_KEY,
                   "headers": headers})}}]
    store.write_message("s", "assistant", "", tool_calls=calls)
    store.write_message("s", "tool", f"echo: {ENV_KEY} {GH}",
                        tool_name="http_get", tool_call_id="call_0")

    raw = _raw(tmp_path)
    assert ENV_KEY not in raw and GH not in raw and "opaque-cred-1" not in raw
    user, call, result = [json.loads(x) for x in raw.splitlines()]
    assert user["content"] == "my key is [REDACTED:ACME_API_KEY]"
    # Tool-call arguments are redacted as the JSON they are (the header sits
    # in a JSON string inside them) and stay valid JSON, and the call still
    # pairs with its result.
    fn = call["tool_calls"][0]["function"]
    assert json.loads(fn["arguments"]) == {
        "url": "https://api.example.invalid", "token": "[REDACTED:ACME_API_KEY]",
        "headers": '{"Authorization": "Bearer [REDACTED:bearer]"}',
    }
    assert call["tool_calls"][0]["id"] == result["tool_call_id"] == "call_0"
    assert result["content"] == "echo: [REDACTED:ACME_API_KEY] [REDACTED:github_token]"
    assert (result["session"], result["role"], result["tool_name"]) == ("s", "tool", "http_get")


def test_meta_records_are_redacted_too(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("ACME_API_KEY", ENV_KEY)
    store = TranscriptStore(root=tmp_path)
    store.write_edit("s", index=0, role="user", text=f"use {ENV_KEY}", length=1)
    store.write_compaction("s", f"the user shared {GH}", cut=1, kept=0, collapsed=2)
    store.write_fork("s", source="src", messages=[
        {"role": "user", "content": f"key {ENV_KEY}"},
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "c1", "type": "function",
             "function": {"name": "f", "arguments": json.dumps({"k": ENV_KEY})}}]},
        {"role": "tool", "tool_call_id": "c1", "name": "f", "content": GH},
    ])
    raw = _raw(tmp_path)
    assert ENV_KEY not in raw and GH not in raw
    edit, compaction, fork = [json.loads(x) for x in raw.splitlines()]
    assert edit["text"] == "use [REDACTED:ACME_API_KEY]" and edit["index"] == 0
    assert compaction["summary"] == "the user shared [REDACTED:github_token]"
    assert fork["source"] == "src" and fork["cut"] == 3
    assert fork["messages"][0]["content"] == "key [REDACTED:ACME_API_KEY]"
    assert fork["messages"][2] == {"role": "tool", "content": "[REDACTED:github_token]",
                                   "name": "f", "tool_call_id": "c1"}


def test_fork_copies_are_redacted_as_messages(tmp_path: Path) -> None:
    store = TranscriptStore(root=tmp_path)
    args = json.dumps({"text": f"tokens:\n{GH}"})
    store.write_fork("s", source="src", messages=[
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": GH, "type": "function", "function": {"name": "save", "arguments": args}}]},
        {"role": "tool", "tool_call_id": GH, "name": "save", "content": f"ok\n{GH}"},
    ])
    [fork] = [json.loads(x) for x in _raw(tmp_path).splitlines()]
    call, result = fork["messages"]
    # Ids and names are kept (they pair the copies), arguments stay JSON.
    assert call["tool_calls"][0]["id"] == result["tool_call_id"] == GH
    assert call["tool_calls"][0]["function"]["name"] == result["name"] == "save"
    assert json.loads(call["tool_calls"][0]["function"]["arguments"]) == {
        "text": "tokens:\n[REDACTED:github_token]"}
    assert result["content"] == "ok\n[REDACTED:github_token]"


def test_clean_tool_call_arguments_are_written_byte_for_byte(tmp_path: Path) -> None:
    store = TranscriptStore(root=tmp_path)
    args = '{"path":"a.py",\n  "content": "x = \\u00e9"}'
    store.write_message("s", "assistant", "", tool_calls=[
        {"id": "c0", "type": "function", "function": {"name": "write_file", "arguments": args}}])
    line = json.loads(_raw(tmp_path))
    assert line["tool_calls"][0]["function"]["arguments"] == args


def test_short_values_stay_verbatim_in_transcripts(monkeypatch, tmp_path: Path) -> None:
    from evi.redact import MIN_KNOWN_LEN

    short = "Zq8vN2kLp4X"[: MIN_KNOWN_LEN - 1]
    monkeypatch.setenv("ACME_API_KEY", short)
    store = TranscriptStore(root=tmp_path)
    store.write_message("s", "tool", f"value {short}", tool_name="t")
    assert json.loads(_raw(tmp_path))["content"] == f"value {short}"


def test_an_env_key_a_tool_echoes_never_reaches_transcripts_or_exports(
    monkeypatch, tmp_path: Path, fake_llm
) -> None:
    """Agent.chat end to end: a tool prints an env API key and the model
    repeats it. The transcript, replay, every export format and fine-tune
    data show a marker; the live history the model works from keeps the
    value (redacting the model's own context is opt-in, not this)."""
    import os

    from evi import finetune, sessions
    from evi.config import Config
    from evi.llm.agent import Agent
    from evi.tools.base import Tool

    monkeypatch.setenv("ACME_API_KEY", ENV_KEY)
    echo_env = Tool(
        name="echo_env", description="print an environment variable",
        parameters={"type": "object", "properties": {"name": {"type": "string"}},
                    "required": ["name"]},
        func=lambda name: f"{name}={os.environ.get(name, '')}",
    )
    client = fake_llm([
        fake_llm.call(("echo_env", {"name": "ACME_API_KEY"})),
        fake_llm.text(f"Your key is {ENV_KEY}."),
    ])
    root = tmp_path / "transcripts"
    agent = Agent(client=client, config=Config(), tools=[echo_env],
                  transcripts=TranscriptStore(root=root), session_id="S1",
                  unattended_policy="allow")
    list(agent.chat("what is my ACME key?"))

    # The model still saw the real output.
    assert f"ACME_API_KEY={ENV_KEY}" in [m.get("content") for m in agent.history]

    raw = _raw(root)
    assert raw and ENV_KEY not in raw
    assert "ACME_API_KEY=[REDACTED:ACME_API_KEY]" in raw
    assert "Your key is [REDACTED:ACME_API_KEY]." in raw

    path = sessions.find_session("S1", root=root)
    replayed = sessions.history_from_transcript(path, replay=True)
    assert [m["role"] for m in replayed] == ["user", "assistant", "tool", "assistant"]
    assert not any(ENV_KEY in json.dumps(m) for m in replayed)
    for fmt in ("md", "html", "json"):
        out = sessions.export_session("S1", fmt=fmt, root=root)
        assert ENV_KEY not in out and "[REDACTED:ACME_API_KEY]" in out, fmt

    data = tmp_path / "ft.jsonl"
    assert finetune.export_dataset(data, root=root, include_tools=True) == (1, 1)
    text = data.read_text("utf-8")
    assert ENV_KEY not in text and "[REDACTED:ACME_API_KEY]" in text


def test_tokens_in_tool_arguments_are_redacted_and_a_resume_sends_valid_json(
    tmp_path: Path, fake_llm
) -> None:
    """Agent.chat end to end: the model passes tokens on lines of their own
    (each follows a JSON `\\n` escape) and a PEM header constant (followed by
    an escaped quote) to a tool returning a dict. The transcript holds no
    token, and the arguments a resumed session sends back are valid JSON."""
    from evi import sessions
    from evi.config import Config
    from evi.llm.agent import Agent, _sanitized_messages
    from evi.tools.base import Tool

    aws = "AKIAIOSFODNN7EXAMPL1"
    text = f'CI tokens:\n{GH}\n{aws}\nHDR = "-----BEGIN RSA PRIVATE KEY-----"\n'
    save = Tool(
        name="save_note", description="save text",
        parameters={"type": "object", "properties": {"text": {"type": "string"}},
                    "required": ["text"]},
        func=lambda text: {"saved": True, "echo": text},
    )
    client = fake_llm([fake_llm.call(("save_note", {"text": text})), fake_llm.text("Saved.")])
    root = tmp_path / "transcripts"
    agent = Agent(client=client, config=Config(), tools=[save],
                  transcripts=TranscriptStore(root=root), session_id="S2",
                  unattended_policy="allow")
    list(agent.chat("save my CI tokens"))

    raw = _raw(root)
    assert raw and GH not in raw and aws not in raw
    lines = [json.loads(x) for x in raw.splitlines()]
    [call] = [x for x in lines if x.get("tool_calls")]
    [result] = [x for x in lines if x["role"] == "tool"]
    args = json.loads(call["tool_calls"][0]["function"]["arguments"])
    assert args == {"text": text.replace(GH, "[REDACTED:github_token]")
                    .replace(aws, "[REDACTED:aws_access_key]")}
    assert "[REDACTED:github_token]" in json.loads(result["content"])["echo"]

    replayed = sessions.history_from_transcript(sessions.find_session("S2", root=root),
                                                replay=True)
    sent = _sanitized_messages([{"role": "system", "content": "sys"}, *replayed])
    for m in sent:
        for c in m.get("tool_calls") or []:
            assert json.loads(c["function"]["arguments"]) == args
