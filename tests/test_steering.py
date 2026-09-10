"""Non-interrupting steering — a follow-up sent while a turn is running.

The property under test: such a message is applied at the NEXT step (once the
current tool round finishes), not by interrupting work in flight and not by
being dropped. The "dropped" half matters most — refusing the message outright
would be better than silently losing it, so the unapplied path is asserted too.
"""

from __future__ import annotations

import threading
import types

from evi.config import Config
from evi.llm.agent import Agent, Done, Steering
from evi.tools.base import Tool

# --- a stub streaming client -------------------------------------------------


class _Delta:
    def __init__(self, content=None, tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls
        self.reasoning_content = None


class _Choice:
    def __init__(self, delta, finish=None):
        self.delta = delta
        self.finish_reason = finish
        self.logprobs = None


class _Chunk:
    def __init__(self, delta, finish=None):
        self.choices = [_Choice(delta, finish)]
        self.usage = None
        self.model = "stub"


class _ToolCallDelta:
    def __init__(self):
        self.index = 0
        self.id = "call_1"
        self.type = "function"
        self.function = types.SimpleNamespace(name="probe_tool", arguments="{}")


def _make_agent(*, tool_call_first: bool, on_tool=None, seen: list | None = None) -> Agent:
    """An Agent whose stub model optionally makes one tool call, then answers.

    `seen` collects the user messages visible on each request, which is how we
    prove *when* a steered message became visible to the model.
    """
    turns = {"n": 0}

    def create(**kw):
        n = turns["n"]
        turns["n"] += 1
        if seen is not None:
            seen.append([m["content"] for m in kw["messages"] if m.get("role") == "user"])
        if n == 0 and tool_call_first:
            return iter([_Chunk(_Delta(tool_calls=[_ToolCallDelta()])),
                         _Chunk(_Delta(), "tool_calls")])
        return iter([_Chunk(_Delta(content="done")), _Chunk(_Delta(), "stop")])

    client = types.SimpleNamespace(
        chat=types.SimpleNamespace(completions=types.SimpleNamespace(create=create))
    )
    tool = Tool(
        name="probe_tool", description="probe",
        parameters={"type": "object", "properties": {}},
        func=(on_tool or (lambda **kw: "ok")), category="fs",
    )
    agent = Agent(client=client, config=Config(), tools=[tool], transcripts=None)
    agent.enable_auto_all()
    return agent


def _drain(agent: Agent, message: str = "original request") -> list:
    out = []
    for ev in agent.chat(message, max_turns=4):
        out.append(ev)
        if isinstance(ev, Done):
            break
    return out


# --- the queue ---------------------------------------------------------------


def test_steer_queues_and_drains():
    a = _make_agent(tool_call_first=False)
    assert a.steer("   ") is False        # empty is refused so the caller can fall back
    assert a.steer("one") is True
    a.steer("two")
    assert a.pending_steering() == 2
    assert a.take_pending_steering() == ["one", "two"]
    assert a.pending_steering() == 0


def test_steer_is_thread_safe():
    """steer() is called from the web request thread while chat() runs on a
    worker, so the queue has to survive concurrent writers."""
    a = _make_agent(tool_call_first=False)
    threads = [threading.Thread(target=lambda i=i: a.steer(f"m{i}")) for i in range(40)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(a.take_pending_steering()) == 40


# --- applied at the next step ------------------------------------------------


def test_follow_up_lands_at_the_next_step_not_mid_tool():
    """Steering DURING the tool round reaches the model on the next request.

    The tool itself does the steering, which makes this deterministic — no
    timing, no racing a background thread.
    """
    seen: list[list[str]] = []
    holder: dict = {}

    def on_tool(**kw):
        holder["agent"].steer("ACTUALLY use TypeScript")
        return "tool-ok"

    a = _make_agent(tool_call_first=True, on_tool=on_tool, seen=seen)
    holder["agent"] = a
    events = _drain(a)

    # First request saw only the original; the second saw both, in order — so it
    # was applied BETWEEN steps rather than injected into work in flight.
    assert seen[0] == ["original request"]
    assert seen[1] == ["original request", "ACTUALLY use TypeScript"]

    steers = [e for e in events if isinstance(e, Steering)]
    assert [(e.text, e.applied) for e in steers] == [("ACTUALLY use TypeScript", True)]
    assert a.pending_steering() == 0


def test_follow_up_queued_before_the_turn_is_applied_immediately():
    seen: list[list[str]] = []
    a = _make_agent(tool_call_first=False, seen=seen)
    a.steer("extra context")
    _drain(a)
    assert seen[0] == ["original request", "extra context"]


def test_unapplied_steering_is_handed_back_not_dropped():
    """A turn can end before there is another step. Anything that never landed
    must still be retrievable — the caller re-sends it as a normal message."""
    a = _make_agent(tool_call_first=False)      # no tool call -> a single step
    events = _drain(a)
    a.steer("too late")                          # arrives after the loop ended
    assert [e for e in events if isinstance(e, Steering)] == []
    assert a.take_pending_steering() == ["too late"]


# --- the endpoint ------------------------------------------------------------


def _client():
    from fastapi.testclient import TestClient

    from evi.apps.web.server import create_app

    return TestClient(create_app())


def test_steer_endpoint_rejects_empty():
    assert _client().post("/api/session/s1/steer", json={"text": "  "}).status_code == 400


def test_steer_endpoint_reports_not_busy():
    """An idle session has nothing to steer — say so, so the UI sends the
    message normally instead of queueing it onto a turn that never runs."""
    r = _client().post("/api/session/s1/steer", json={"text": "hi"}).json()
    assert r["queued"] is False
    assert r["reason"] == "not busy"
