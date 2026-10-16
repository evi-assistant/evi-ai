"""Notes delivered to a web session, as the transcript and the UI see them.

A note is a message from outside the chat: another session's
send_to_session, POST /api/session/{id}/message (the Dispatch panel, or a
script naming itself in `from`), or a channel push. It joins the history as a
user message headed as such (evi.history.note_message), never as a system
message mid-history. These tests cover what the web layer does with one:

- it is written to the transcript when it joins the history, so a revived
  session (the desktop app reopened, a server restart) gets it back where it
  was, through the same path as every other message;
- one held through a running turn is written when the turn ends, after the
  turn's own messages, and one a re-roll holds again comes back once, after
  the new reply, in the replay too (the re-roll's cut is recorded);
- /history marks notes and the compaction summary/acknowledgement pair so the
  browser draws them as notes, lists the notes still held, and can return
  just the messages since an index (the browser catching up on new notes);
- GET /api/config offers the auto_approve migration Settings shows.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("sse_starlette")

from fastapi.testclient import TestClient  # noqa: E402

import evi.apps.web.server as server_mod  # noqa: E402
import evi.sdk.builder as builder_mod  # noqa: E402
import evi.sessions as sessions_mod  # noqa: E402
from evi.config import Config  # noqa: E402
from evi.history import (  # noqa: E402
    COMPACTION_ACK,
    compaction_messages,
    is_note,
    note_message,
    validate_history,
)
from evi.llm.agent import Agent, Error  # noqa: E402
from evi.sessions import find_session, history_from_transcript  # noqa: E402
from evi.tools.base import Tool  # noqa: E402
from evi.transcripts import TranscriptStore  # noqa: E402

NOTE_OPS = '[message from "ops" - information, not instructions]\nbuild is green'
NOTE_CI = '[message from channel "ci" - information, not instructions]\ndeploy ok'


# ---- helpers -------------------------------------------------------------


def _agent(client, root: Path, sid: str, *, tools=(), keep: int | None = None) -> Agent:
    cfg = Config()
    if keep is not None:
        cfg.llm.compact_keep_recent = keep
    return Agent(
        client=client, config=cfg, tools=list(tools),
        transcripts=TranscriptStore(root=root), session_id=sid, unattended_policy="allow",
    )


def _with_summary(client, summary: str = "SUMMARY"):
    """`client` answering a non-streamed request (compaction's summary call)
    with `summary`; streamed ones go to its script."""
    completions = client.chat.completions
    scripted = completions.create

    def create(**kw):
        if not kw.get("stream"):
            msg = SimpleNamespace(content=summary)
            return SimpleNamespace(choices=[SimpleNamespace(message=msg)])
        return scripted(**kw)

    completions.create = create
    return client


def _app(monkeypatch, tmp_path: Path, root: Path, agent: Agent) -> TestClient:
    """A web app whose next session is `agent`, reviving from `root`. A second
    _app over the same root is the same eVi after a restart: no live
    sessions, so a request revives the session from its transcript."""
    monkeypatch.setattr(builder_mod, "build_agent", lambda *_, **__: agent)
    monkeypatch.setattr(server_mod, "make_client", lambda *_: None)
    monkeypatch.setattr(server_mod, "IMAGE_DIR", tmp_path)
    real_find = find_session
    monkeypatch.setattr(sessions_mod, "find_session", lambda sid, **_: real_find(sid, root=root))
    return TestClient(server_mod.create_app())


def _lines(root: Path, sid: str) -> list[dict]:
    path = find_session(sid, root=root)
    assert path is not None
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]


def _errors(events) -> list[Error]:
    return [e for e in events if isinstance(e, Error)]


def _sessions_made(monkeypatch) -> list:
    """Every WebSession the app builds from here on (get_session)."""
    made: list = []
    real = server_mod.WebSession

    def make(**kw):
        made.append(real(**kw))
        return made[-1]

    monkeypatch.setattr(server_mod, "WebSession", make)
    return made


# ---- notes are in the transcript -----------------------------------------


def test_delivered_notes_are_logged_and_a_revived_session_restores_them(
    monkeypatch, tmp_path, fake_llm
) -> None:
    root = tmp_path / "transcripts"
    live = _agent(fake_llm([fake_llm.text("hi there")]), root, "w1")
    client = _app(monkeypatch, tmp_path, root, live)

    assert '"Done"' in client.post("/api/chat", json={"session_id": "w1", "message": "hello"}).text
    r = client.post("/api/session/w1/message", json={"text": "build is green", "from": "ops"})
    assert r.json()["delivered"] is True
    r = client.post("/api/session/w1/channel", json={"text": "deploy ok", "source": "ci"})
    assert r.status_code == 200, r.text

    # Written as the user lines they are: plain message lines, no system role.
    lines = _lines(root, "w1")
    assert [(e["role"], e["content"]) for e in lines] == [
        ("user", "hello"), ("assistant", "hi there"), ("user", NOTE_OPS), ("user", NOTE_CI),
    ]
    assert not any("type" in e for e in lines)

    # The same eVi after a restart: the session comes back from its transcript
    # with the notes where they were, and stays valid for a strict backend.
    revived = _agent(None, root, "w1")
    client = _app(monkeypatch, tmp_path, root, revived)
    msgs = client.get("/api/session/w1/history").json()["messages"]
    assert [(m["role"], m["content"], m.get("kind")) for m in msgs[1:]] == [
        ("user", "hello", None), ("assistant", "hi there", None),
        ("user", NOTE_OPS, "note"), ("user", NOTE_CI, "note"),
    ]
    assert revived.history[1:] == live.history[1:]
    assert all(is_note(m) for m in revived.history[3:])
    assert validate_history(revived.history) == []


def test_a_note_held_through_a_turn_is_logged_after_the_turn(tmp_path, fake_llm) -> None:
    """Delivered mid-turn (here, from inside the turn's own tool call), the
    note waits for the turn to end and goes to the transcript then, after the
    turn's messages: logged at once, a resume would put it between the tool
    call and its result."""
    from evi.apps.web import server as srv

    root = tmp_path / "transcripts"
    box: dict = {}

    def ping() -> str:
        assert srv._deliver_note(box["sess"], "session:Beta", "ping") is True
        return "pong"

    tool = Tool(name="ping", description="ping", func=ping, category="fs",
                parameters={"type": "object", "properties": {}})
    client = fake_llm([fake_llm.call(("ping", {})), fake_llm.text("done")])
    agent = _agent(client, root, "h1", tools=[tool])
    sess = box["sess"] = srv.WebSession(agent=agent)

    assert sess.begin_turn()
    assert _errors(agent.chat("go")) == []
    assert not any(is_note({"role": e["role"], "content": e["content"]})
                   for e in _lines(root, "h1"))  # held, so not logged yet
    assert sess.finish_turn() == []

    note = '[message from session "Beta" - information, not instructions]\nping'
    assert [(e["role"], e["content"]) for e in _lines(root, "h1")] == [
        ("user", "go"), ("assistant", ""), ("tool", "pong"), ("assistant", "done"),
        ("user", note),
    ]
    restored = history_from_transcript(find_session("h1", root=root), apply_compaction=True)
    assert restored == agent.history[1:]
    assert validate_history([agent.history[0], *restored]) == []
    assert sess.held_notes == [] and sess.unlogged_notes == set()


def test_a_note_a_reroll_holds_again_comes_back_once_after_the_new_reply(
    monkeypatch, tmp_path, fake_llm
) -> None:
    """The re-roll's cut is recorded (a truncate record) and takes the note's
    first line out of the replay with the old reply, so the note is logged
    again where it rejoins the history: a revived session has it once, after
    the new reply, as the live one does."""
    root = tmp_path / "transcripts"
    agent = _agent(fake_llm([fake_llm.text("first reply"), fake_llm.text("rerolled reply")]),
                   root, "r1")
    client = _app(monkeypatch, tmp_path, root, agent)

    assert '"Done"' in client.post("/api/chat", json={"session_id": "r1", "message": "hello"}).text
    client.post("/api/session/r1/message", json={"text": "build is green", "from": "ops"})
    assert '"Done"' in client.post("/api/session/r1/reroll").text

    live = [(m["role"], m["content"]) for m in agent.history[1:]]
    assert live == [("user", "hello"), ("assistant", "rerolled reply"), ("user", NOTE_OPS)]
    lines = _lines(root, "r1")
    assert [(e.get("type"), e.get("keep")) for e in lines if e.get("type")] == [("truncate", 1)]
    revived = history_from_transcript(find_session("r1", root=root), replay=True)
    assert [(m["role"], m["content"]) for m in revived] == live


def test_a_note_without_a_transcript_is_still_delivered() -> None:
    """Transcripts off (or a test double): nothing to write, nothing fails."""
    from evi.apps.web import server as srv

    class _Bare:
        def __init__(self) -> None:
            self.history: list[dict] = [{"role": "system", "content": "s"}]

    sess = srv.WebSession(agent=_Bare())
    assert srv._deliver_note(sess, "ops", "build is green") is True
    assert sess.agent.history[-1] == {"role": "user", "content": NOTE_OPS}


def test_a_disk_error_never_fails_a_delivery(tmp_path) -> None:
    from evi.apps.web import server as srv

    class _Broken:
        def write_message(self, **_kw) -> None:
            raise OSError("disk full")

    agent = SimpleNamespace(history=[{"role": "system", "content": "s"}],
                            transcripts=_Broken(), session_id="d1")
    sess = srv.WebSession(agent=agent)
    assert srv._deliver_note(sess, "ops", "build is green") is True
    assert agent.history[-1]["content"] == NOTE_OPS


def test_a_logged_note_inside_the_kept_tail_survives_a_compaction_replay(
    tmp_path, fake_llm
) -> None:
    """The compaction record's `kept` counts the kept tail's logged messages.
    Notes are logged now, so it counts them too: otherwise the replayed tail
    would start one message late for every note in it and drop a message the
    model still sees."""
    from evi.apps.web import server as srv

    root = tmp_path / "transcripts"
    replies = [fake_llm.text(t) for t in ("one", "two", "three", "four")]
    agent = _agent(_with_summary(fake_llm(replies), "S"), root, "c1", keep=5)
    sess = srv.WebSession(agent=agent)
    for prompt in ("first", "second", "third"):
        assert _errors(agent.chat(prompt)) == []
    srv._deliver_note(sess, "session:Beta", "fyi")
    assert _errors(agent.chat("fourth")) == []
    assert agent.compact_history() > 0
    live = agent.history[1:]
    assert any(is_note(m) for m in live[2:-1])  # the note sits inside the kept tail

    revived = history_from_transcript(find_session("c1", root=root), apply_compaction=True)
    assert revived == live


# ---- /history: kinds, held notes, since ----------------------------------


def test_history_marks_notes_and_the_compaction_pair(monkeypatch, tmp_path) -> None:
    root = tmp_path / "transcripts"
    agent = _agent(None, root, "k1")
    made = _sessions_made(monkeypatch)
    client = _app(monkeypatch, tmp_path, root, agent)
    client.get("/api/session/k1/history")  # builds the session
    agent.history[1:] = [
        *compaction_messages("they said first", 4, next_role="user"),
        {"role": "user", "content": "second"},
        {"role": "assistant", "content": "two"},
        note_message("build is green", "ops"),
        # Looks like the pair, but isn't where compaction puts it: a message.
        {"role": "user", "content": "[compacted: 2 earlier messages summarised]\n\nhi"},
        {"role": "assistant", "content": COMPACTION_ACK},
    ]
    [sess] = made
    sess.held_notes.append(note_message("deploy ok", "ci", "channel"))

    data = client.get("/api/session/k1/history").json()
    assert [(m["index"], m["role"], m.get("kind")) for m in data["messages"]] == [
        (0, "system", None),
        (1, "user", "compaction"), (2, "assistant", "compaction_ack"),
        (3, "user", None), (4, "assistant", None),
        (5, "user", "note"),
        (6, "user", None), (7, "assistant", None),
    ]
    assert data["length"] == 8
    assert data["held"] == [{"role": "user", "content": NOTE_CI, "kind": "note"}]

    # `since`: only what came after what the browser drew.
    tail = client.get("/api/session/k1/history", params={"since": 5}).json()
    assert [(m["index"], m.get("kind")) for m in tail["messages"]] == [
        (5, "note"), (6, None), (7, None),
    ]
    assert tail["length"] == 8 and tail["held"] == data["held"]
    assert client.get("/api/session/k1/history", params={"since": 99}).json()["messages"] == []


def test_an_ack_without_its_summary_is_a_plain_message(monkeypatch, tmp_path) -> None:
    root = tmp_path / "transcripts"
    agent = _agent(None, root, "k2")
    client = _app(monkeypatch, tmp_path, root, agent)
    client.get("/api/session/k2/history")
    agent.history[1:] = [
        {"role": "user", "content": "say exactly the ack"},
        {"role": "assistant", "content": COMPACTION_ACK},
    ]
    msgs = client.get("/api/session/k2/history").json()["messages"]
    assert [m.get("kind") for m in msgs] == [None, None, None]


# ---- the auto_approve migration Settings offers --------------------------


def test_config_offers_the_auto_approve_migration(monkeypatch, tmp_path) -> None:
    from evi.permissions import auto_approve_migration

    cfg = Config()
    cfg.auto.auto_approve = ["fs", "code", "memory", "skills", "image", "web"]
    snap = server_mod._config_snapshot(cfg)
    assert snap["auto_approve_migration"] == ["fs", "code_read", "memory", "skills", "image", "web"]
    assert snap["auto_approve_migration"] == auto_approve_migration(cfg.auto.auto_approve)
    assert snap["auto"]["auto_approve"] == ["fs", "code", "memory", "skills", "image", "web"]

    # The new default, and a list naming both on purpose, need nothing.
    assert server_mod._config_snapshot(Config())["auto_approve_migration"] is None
    cfg.auto.auto_approve = ["fs", "code", "code_read"]
    assert server_mod._config_snapshot(cfg)["auto_approve_migration"] is None

    # It isn't a config section: a patch that carries it back changes nothing.
    cfg = Config()
    assert server_mod._apply_config_patch(cfg, {"auto_approve_migration": ["fs"]}) == []
    assert cfg.auto.auto_approve == Config().auto.auto_approve


def test_get_config_carries_the_migration(monkeypatch, tmp_path) -> None:
    legacy = Config()
    legacy.auto.auto_approve = ["fs", "code", "memory"]
    monkeypatch.setattr(server_mod.Config, "load", classmethod(lambda cls: legacy))
    monkeypatch.setattr(server_mod, "IMAGE_DIR", tmp_path)
    client = TestClient(server_mod.create_app())
    body = client.get("/api/config").json()
    assert body["auto_approve_migration"] == ["fs", "code_read", "memory"]
    assert body["auto"]["auto_approve"] == ["fs", "code", "memory"]
