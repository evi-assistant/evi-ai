"""POST /api/session/{id}/cancel: the web side of Agent.cancel (loop-agent-cancel,
acceptance criterion 5: "POST cancel frees a busy web session").

- A cancel ends the running turn with Done("interrupted") on its own stream
  (a reply being streamed keeps its text; a running tool sees the cancel; a
  permission question waiting on the browser is denied) and answers once the
  session is free, so the next message starts a turn at once. The history
  stays valid.
- A cancel that lands after the session is claimed but before the worker has
  started the turn still ends it (the turn starts in the request,
  WebSession.start_turn); no question is posted once the turn is cancelled.
- Auth like the other session routes: only the caller's own live sessions
  (a multi-user login can't stop the owner's), and a browser's request needs
  X-Evi-Client.

Turns run against a real Agent whose model is scripted (tests/_live_web.py),
on a real uvicorn server where a request has to land mid-stream.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("sse_starlette")

import httpx  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import evi.apps.web.server as server_mod  # noqa: E402
from evi.config import Config  # noqa: E402
from evi.history import INTERRUPTED, validate_history  # noqa: E402
from evi.llm.agent import CANCELLED, Agent  # noqa: E402
from evi.tools.base import Tool, ToolContext, bind_context, unbind_context  # noqa: E402
from tests._live_web import (  # noqa: E402
    UI, Scripted, SlowStream, read_until, serving, sse_events, web_app,
)
from tests.conftest import _text_chunk, llm_call, llm_text  # noqa: E402


def _tool(name: str, func, category: str = "fs") -> Tool:
    return Tool(name=name, description=name, func=func, category=category,
                parameters={"type": "object", "properties": {}})


def _agent(turns: list, tools=(), *, auto: bool = True) -> Agent:
    agent = Agent(client=Scripted(turns), config=Config(), tools=list(tools), transcripts=None)
    if auto:
        agent.enable_auto_all()
    return agent


def _busy(c: httpx.Client, sid: str) -> bool:
    snap = c.get("/api/dispatch").json()
    return next(s for s in snap["sessions"] if s["id"] == sid)["busy"]


def test_cancel_mid_stream_frees_the_session_at_once_and_the_next_message_works(
    monkeypatch, tmp_path: Path
) -> None:
    stream = SlowStream([_text_chunk("partial answer ")])
    agent = _agent([stream, llm_text("the second answer")])
    with serving(web_app(monkeypatch, tmp_path, agent)) as base, \
            httpx.Client(base_url=base, timeout=20) as c, \
            httpx.Client(base_url=base, timeout=20) as c2:
        with c.stream("POST", "/api/chat", json={"session_id": "s1", "message": "go"}) as first:
            lines = first.iter_lines()
            assert read_until(lines, "TextDelta")["text"].startswith("partial")
            assert _busy(c2, "s1") is True

            started = time.monotonic()
            r = c2.post("/api/session/s1/cancel", headers=UI)
            took = time.monotonic() - started
            assert r.status_code == 200
            assert r.json() == {"cancelled": True, "busy": False}
            assert took < 1.5, took  # the agent stops within CANCEL_POLL of the cancel

            seen: list[dict] = []
            done = read_until(lines, "Done", seen)
            assert done["reason"] == "interrupted"
            assert all(e["kind"] != "Error" for e in seen), seen

        assert stream.closed.wait(5)  # the abandoned stream was closed
        assert validate_history(agent.history) == []
        assert [m["role"] for m in agent.history] == ["system", "user", "assistant"]
        assert agent.history[-1]["content"].startswith("partial answer")
        assert INTERRUPTED in agent.history[-1]["content"]  # marked as cut off

        # Free: the next message is a normal turn, not a 409.
        events = sse_events(c2.post("/api/chat", json={"session_id": "s1",
                                                       "message": "again"}).text)
        assert [e["kind"] for e in events][-1] == "Done"
        assert events[-1]["reason"] == "stop"
        assert "the second answer" in "".join(e.get("text", "") for e in events)
        assert validate_history(agent.history) == []


def test_cancel_mid_tool_stops_the_call_and_frees_the_session(monkeypatch, tmp_path) -> None:
    started = threading.Event()

    def slow(ctx: ToolContext | None = None) -> str:
        started.set()
        return "stopped early" if ctx.cancel_token.wait(20) else "ran to the end"

    agent = _agent([llm_call(("slow", {}))], [_tool("slow", slow)])
    with serving(web_app(monkeypatch, tmp_path, agent)) as base, \
            httpx.Client(base_url=base, timeout=30) as c, \
            httpx.Client(base_url=base, timeout=30) as c2:
        with c.stream("POST", "/api/chat", json={"session_id": "s2", "message": "go"}) as first:
            lines = first.iter_lines()
            read_until(lines, "ToolCall")
            assert started.wait(10)
            r = c2.post("/api/session/s2/cancel", headers=UI)
            assert r.json() == {"cancelled": True, "busy": False}
            result = read_until(lines, "ToolResult")
            assert result["output"] == "stopped early"  # finished within the grace
            assert read_until(lines, "Done")["reason"] == "interrupted"
    assert len(agent.client.requests) == 1  # the model is not asked again
    assert validate_history(agent.history) == []


def test_cancel_denies_the_question_the_turn_waits_on(monkeypatch, tmp_path) -> None:
    ran: list[bool] = []
    agent = _agent([llm_call(("fetch_it", {}))],
                   [_tool("fetch_it", lambda: ran.append(True) or "ran", category="network")],
                   auto=False)
    with serving(web_app(monkeypatch, tmp_path, agent)) as base, \
            httpx.Client(base_url=base, timeout=30) as c, \
            httpx.Client(base_url=base, timeout=30) as c2:
        with c.stream("POST", "/api/chat", json={"session_id": "s3", "message": "go"}) as first:
            lines = first.iter_lines()
            req = read_until(lines, "PermissionRequest")
            assert req["tool_name"] == "fetch_it"
            started = time.monotonic()
            r = c2.post("/api/session/s3/cancel", headers=UI)
            # Not left waiting out the 300 s decision deadline.
            assert r.json() == {"cancelled": True, "busy": False}
            assert time.monotonic() - started < 3
            result = read_until(lines, "ToolResult")
            assert result["output"].startswith(CANCELLED), result
            assert read_until(lines, "Done")["reason"] == "interrupted"
        late = c2.post("/api/decide", json={"session_id": "s3",
                                            "decision_id": req["decision_id"], "approved": True})
        assert late.json() == {"ok": False, "reason": "no such decision"}
    assert ran == []
    assert validate_history(agent.history) == []


def test_cancel_when_no_turn_runs_says_so(monkeypatch, tmp_path) -> None:
    agent = _agent([llm_text("hi")])
    c = TestClient(web_app(monkeypatch, tmp_path, agent))
    assert c.post("/api/chat", json={"session_id": "idle", "message": "hello"}).status_code == 200
    r = c.post("/api/session/idle/cancel", headers=UI)
    assert r.json() == {"cancelled": False, "reason": "not busy", "busy": False}
    # ...and it cancelled nothing for later: the next turn runs.
    agent.client.turns.append(llm_text("still here"))
    events = sse_events(c.post("/api/chat", json={"session_id": "idle", "message": "x"}).text)
    assert (events[-1]["kind"], events[-1]["reason"]) == ("Done", "stop")


def test_cancel_of_a_session_that_is_not_live_is_404(monkeypatch, tmp_path) -> None:
    c = TestClient(web_app(monkeypatch, tmp_path, _agent([])))
    assert c.post("/api/session/nobody/cancel", headers=UI).status_code == 404


def test_a_cancel_before_the_worker_starts_the_turn_still_ends_it() -> None:
    """The web claims the session (begin_turn), then starts the turn
    (start_turn) and hands its events to a worker: a Stop pressed in between,
    or before the worker gets going, ends that turn at its first step."""
    agent = _agent([llm_text("never asked for")])
    sess = server_mod.WebSession(agent=agent)
    assert sess.begin_turn()
    assert sess.cancel_turn("stopped early") is True
    events = list(sess.start_turn(lambda: agent.chat("hi")))
    assert events[-1].reason == "interrupted"
    assert agent.client.requests == []  # the model was never called
    assert sess.finish_turn() == []
    # The next claim starts clean: no cancel carried over.
    assert sess.begin_turn()
    agent.client.turns.append(llm_text("fine"))
    assert list(sess.start_turn(lambda: agent.chat("again")))[-1].reason == "stop"


def test_a_turn_that_fails_to_start_reports_it_on_the_stream(monkeypatch, tmp_path) -> None:
    agent = _agent([])

    def broken(*_a, **_k):
        raise RuntimeError("no model")

    agent.chat = broken  # type: ignore[method-assign]
    c = TestClient(web_app(monkeypatch, tmp_path, agent))
    events = sse_events(c.post("/api/chat", json={"session_id": "b", "message": "x"}).text)
    assert [(e["kind"], e["message"], e["seq"]) for e in events] == [
        ("Error", "RuntimeError: no model", 1)]
    snap = next(s for s in c.get("/api/dispatch").json()["sessions"] if s["id"] == "b")
    assert snap["busy"] is False


def _callback(sess: server_mod.WebSession, enqueued: list):
    return server_mod._make_permission_callback({"q": sess}, "q", enqueued.append, 30.0)


def test_no_question_is_posted_once_the_turn_is_cancelled() -> None:
    """A second subagent that waited its turn to ask, after the turn was
    cancelled, is answered no without a PermissionRequest (the question
    would wait on a browser that has stopped reading)."""
    agent = _agent([])
    sess = server_mod.WebSession(agent=agent)
    enqueued: list[dict] = []
    agent._begin_turn()
    agent.cancel("stopped")
    assert _callback(sess, enqueued)("fetch_it", "{}", "network") is False
    assert enqueued == [] and sess.pending == {}


def test_no_question_is_posted_for_a_cancelled_parent_call() -> None:
    """A subagent asks on its parent's tool thread: the parent call's cancel
    token (or one further up) is what says the turn is over."""
    agent = _agent([])
    agent._begin_turn()  # the session's own turn is not cancelled
    sess = server_mod.WebSession(agent=agent)
    enqueued: list[dict] = []
    parent_token = threading.Event()
    outer = ToolContext(cancel_token=parent_token)
    inner = ToolContext(cancel_token=threading.Event(), parent=outer)
    token = bind_context(inner)
    try:
        parent_token.set()
        assert _callback(sess, enqueued)("fetch_it", "{}", "network") is False
    finally:
        unbind_context(token)
    assert enqueued == []


# ---- auth: like the other session routes ------------------------------------


def test_a_browser_cancel_needs_the_client_header(monkeypatch, tmp_path) -> None:
    from tests.test_web_hardening import LOCAL, _make_app

    c = TestClient(_make_app(monkeypatch, tmp_path), base_url=LOCAL)
    r = c.post("/api/session/s1/cancel", headers={"Origin": LOCAL})
    assert r.status_code == 403 and "X-Evi-Client" in r.json()["detail"]
    assert c.post("/api/session/s1/cancel", headers={"Origin": LOCAL, **UI}).status_code == 404


def test_a_user_login_cannot_cancel_the_owners_session(monkeypatch, tmp_path) -> None:
    from tests.test_web_hardening import LOCAL, _make_app

    app = _make_app(monkeypatch, tmp_path, token="owner-tok", multi_user=True,
                    users=[{"name": "alice", "token": "alice-tok"}])
    c = TestClient(app, base_url=LOCAL)
    owner = {"Authorization": "Bearer owner-tok"}
    alice = {"Authorization": "Bearer alice-tok"}
    assert c.post("/api/chat", json={"session_id": "owner-s", "message": "no-tool"},
                  headers=owner).status_code == 200
    assert c.post("/api/session/owner-s/cancel", headers=alice).status_code == 404
    assert c.post("/api/session/owner-s/cancel", headers=owner).json()["cancelled"] is False
