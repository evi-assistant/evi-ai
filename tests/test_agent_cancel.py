"""Cancelling a turn: Agent.cancel, the process-tree-safe runner, REPL Ctrl+C.

There was no way to stop a running turn. Ctrl+C in the REPL raised
KeyboardInterrupt through the agent loop and killed the session; a
run_command timeout killed only the shell (on Windows it then waited for
pipes a grandchild still held, so the turn could hang for good, and on POSIX
the grandchild was orphaned).

- `Agent.cancel(reason)` ends the turn with Done("interrupted") within about
  a second, wherever it is: a reply streaming (its text kept, marked as cut
  off), a request still waiting for its first token, a permission prompt, a
  tool round (running calls see ToolContext.cancel_token; a call still
  running after CANCEL_GRACE is left behind, and keeps seeing its cancel; a
  call still queued never starts). Every call left without a result is
  answered "(cancelled)", so the history stays valid, and the stop hook
  fires. A turn starts when chat() is called, so a cancel sent before it is
  iterated still ends it.
- `evi.proc.run` runs a command as the root of a Job Object / process group
  and kills the whole tree at its timeout, on a cancel or an `until` match,
  returning the output so far, and when eVi exits. run_command, run_python
  and monitor use it; unattended runs (no callback, a remote trigger, an
  unattended entry point) don't get *_KEY / *_TOKEN / *_SECRET variables.
- In the REPL the first Ctrl+C cancels the turn and returns to the prompt
  with the session intact (a permission question open on any thread is
  answered no, and none is asked after it); a second within 2 s quits.

Agent-level tests drive Agent.chat against scripted clients built from the
shared fake_llm chunks (tests/conftest.py).
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import psutil
import pytest

import evi.llm.agent as agent_mod
import evi.tools.code  # noqa: F401  registers run_python
import evi.tools.monitor  # noqa: F401  registers monitor
from evi import proc
from evi.config import Config
from evi.history import INTERRUPTED, validate_history
from evi.llm.agent import CANCELLED, Agent, Done, TextDelta, ToolProgress, ToolResult
from evi.tools.base import REGISTRY, Tool, ToolContext, bind_context, unbind_context
from evi.tools.shell import run_command
from tests.conftest import _text_chunk, llm_call, llm_text


# ---- helpers -------------------------------------------------------------


class _Client:
    """An OpenAI-shaped client whose create() serves `turns` in order: a list
    of chunks, a stream object, or a callable(**kwargs) returning either."""

    def __init__(self, turns: list) -> None:
        self.turns = list(turns)
        self.requests: list[dict] = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kwargs: Any):
        self.requests.append(kwargs)
        turn = self.turns.pop(0)
        if callable(turn):
            return turn(**kwargs)
        return iter(turn) if isinstance(turn, list) else turn


class _SlowStream:
    """A reply that streams `first`, then keeps the connection open: a chunk
    with no text every `every` seconds, or nothing at all (every=None), until
    it is closed."""

    def __init__(self, first: list, every: float | None = None) -> None:
        self.first = first
        self.every = every
        self.closed = threading.Event()

    def __iter__(self):
        yield from self.first
        while not self.closed.wait(self.every or 30):
            yield _text_chunk("")

    def close(self) -> None:
        self.closed.set()


def _tool(name: str, func, *, category: str = "fs", long: bool = False) -> Tool:
    return Tool(name=name, description=name, func=func, category=category, long=long,
                parameters={"type": "object", "properties": {}})


def _agent(client, tools=(), **kw) -> Agent:
    kw.setdefault("unattended_policy", "allow")  # no permission UI: let tools run
    return Agent(client=client, config=Config(), tools=list(tools), **kw)


def _stops(agent: Agent) -> list[str]:
    """Record the reasons the `stop` lifecycle hook fires with."""
    seen: list[str] = []
    fire = agent._fire_lifecycle

    def record(event: str, payload: str = "") -> bool:
        if event == "stop":
            seen.append(payload)
        return fire(event, payload)

    agent._fire_lifecycle = record  # type: ignore[method-assign]
    return seen


def _results(events) -> list[ToolResult]:
    return [e for e in events if isinstance(e, ToolResult)]


def _alive(pid: int) -> bool:
    try:
        return psutil.Process(pid).status() != psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return False


def _gone_within(pids: list[int], seconds: float) -> bool:
    deadline = time.monotonic() + seconds
    while any(_alive(p) for p in pids):
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.05)
    return True


def _kill(pids: list[int]) -> None:
    for pid in pids:
        try:
            psutil.Process(pid).kill()
        except psutil.Error:
            pass


def _read_pids(path: Path, timeout: float = 30.0) -> list[int]:
    """The pids a _TREE script wrote, once it has written them."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            pids = [int(x) for x in path.read_text(encoding="utf-8").split()]
            if len(pids) == 2:
                return pids
        except (OSError, ValueError):
            pass
        time.sleep(0.05)
    raise AssertionError(f"{path} never got its pids")


# A command with a grandchild: it starts a `holder` that inherits its stdout
# and stderr pipes (as a background job or a build daemon does), says so,
# writes "<own pid> <holder pid>" to argv[1], then sleeps (argv[2] == "stay")
# or exits.
_TREE = r'''
import os, subprocess, sys, time
holder = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"],
                          stdout=sys.stdout, stderr=sys.stderr)
print("started", holder.pid, flush=True)
with open(sys.argv[1] + ".tmp", "w") as f:
    f.write(f"{os.getpid()} {holder.pid}")
os.replace(sys.argv[1] + ".tmp", sys.argv[1])
if sys.argv[2] == "stay":
    time.sleep(60)
'''


def _tree_script(tmp_path: Path) -> tuple[Path, Path]:
    script = tmp_path / "tree.py"
    script.write_text(_TREE, encoding="utf-8")
    return script, tmp_path / "tree.pids"


def _cancel_when(path: Path, cancel) -> dict:
    """Call `cancel()` on a thread once `path` holds the _TREE pids; returns a
    dict that gets the pids and the moment of the cancel."""
    out: dict = {}

    def watch() -> None:
        out["pids"] = _read_pids(path)
        out["at"] = time.monotonic()
        cancel()

    threading.Thread(target=watch, daemon=True).start()
    return out


# ---- the agent loop ---------------------------------------------------------


def test_cancel_mid_stream_ends_the_turn_within_a_second_and_keeps_the_text():
    stream = _SlowStream([_text_chunk("Hello, "), _text_chunk("wor")], every=0.05)
    client = _Client([stream])
    agent = _agent(client)
    stops = _stops(agent)
    cancelled_at: list[float] = []

    def cancel() -> None:
        cancelled_at.append(time.monotonic())
        agent.cancel("the user stopped it")

    # The stream is still open (sending empty chunks) when the cancel lands.
    threading.Timer(0.3, cancel).start()
    events = list(agent.chat("hi"))
    done_at = time.monotonic()

    assert events[-1] == Done("interrupted")
    assert done_at - cancelled_at[0] < 1.0
    assert "".join(e.text for e in events if isinstance(e, TextDelta)) == "Hello, wor"
    last = agent.history[-1]
    assert last["role"] == "assistant" and "tool_calls" not in last
    assert last["content"] == (
        f"Hello, wor\n\n{INTERRUPTED} The reply was cut off here: the user stopped it."
    )
    assert validate_history(agent.history) == []
    assert stops == ["interrupted"]
    assert stream.closed.wait(2)  # the request itself was ended too


def test_cancel_while_the_model_has_sent_nothing_yet():
    # A local backend still busy with a long prompt: create() hasn't returned.
    release = threading.Event()

    def busy(**_kw):
        release.wait(30)
        return iter(llm_text("too late"))

    client = _Client([busy, llm_text("fine")])
    agent = _agent(client)
    try:
        threading.Timer(0.3, agent.cancel).start()
        t0 = time.monotonic()
        events = list(agent.chat("a long prompt"))
        assert time.monotonic() - t0 < 0.3 + 1.0
    finally:
        release.set()
    assert events[-1] == Done("interrupted")
    # No reply arrived, so no assistant message is made up.
    assert [m["role"] for m in agent.history] == ["system", "user"]
    assert validate_history(agent.history) == []
    # The next turn starts uncancelled and works.
    assert list(agent.chat("again"))[-1] == Done("stop")
    assert agent.history[-1] == {"role": "assistant", "content": "fine"}


def test_cancel_with_the_stream_silent_ends_the_turn_within_a_second():
    stream = _SlowStream([_text_chunk("thinking about it")], every=None)
    agent = _agent(_Client([stream]))
    try:
        threading.Timer(0.3, agent.cancel).start()
        t0 = time.monotonic()
        events = list(agent.chat("hi"))
        assert time.monotonic() - t0 < 0.3 + 1.0
    finally:
        stream.close()
    assert events[-1] == Done("interrupted")
    assert agent.history[-1]["content"].startswith("thinking about it\n\n(interrupted)")
    assert validate_history(agent.history) == []


def test_cancel_mid_run_command_kills_the_tree_and_keeps_its_partial_output(tmp_path):
    script, pidfile = _tree_script(tmp_path)
    command = f'"{sys.executable}" "{script}" "{pidfile}" stay'
    client = _Client([llm_call(("run_command", {"command": command, "timeout": 60})),
                      llm_text("never asked")])
    agent = _agent(client, [REGISTRY["run_command"]])
    stops = _stops(agent)
    seen = _cancel_when(pidfile, lambda: agent.cancel("the user pressed Ctrl+C"))
    try:
        events = list(agent.chat("run it"))
        done_at = time.monotonic()
        pids = seen["pids"]
        no_survivor = _gone_within(pids, 2.0)
    finally:
        _kill(seen.get("pids", []))

    assert events[-1] == Done("interrupted")
    assert done_at - seen["at"] < 5.0  # not the command's 60 s
    assert no_survivor, "a process of the cancelled command's tree survived"
    [res] = _results(events)
    assert res.output.startswith("(cancelled)") and "started" in res.output
    assert agent.history[-1] == {"role": "tool", "tool_call_id": "call_0",
                                 "name": "run_command", "content": res.output}
    assert validate_history(agent.history) == []
    assert len(client.requests) == 1  # the model isn't asked again
    assert stops == ["interrupted"]


def test_cancel_at_a_permission_prompt_runs_and_asks_nothing_more():
    ran: list = []
    asked: list = []
    shell_like = _tool("act", lambda: ran.append(1) or "acted", category="shell")
    client = _Client([llm_call(("act", {}), ("act", {})), llm_text("never asked")])

    def ask(name: str, args: str, category: str) -> bool:
        asked.append(name)
        agent.cancel("stopped at the prompt")
        return True  # even an approval doesn't run anything now

    agent = _agent(client, [shell_like], permission_callback=ask)
    events = list(agent.chat("go"))

    assert events[-1] == Done("interrupted")
    assert asked == ["act"]  # the second call is not asked about
    assert ran == []
    results = _results(events)
    assert len(results) == 2
    assert all(r.output.startswith(f"{CANCELLED} Not run: the turn was cancelled "
                                   "(stopped at the prompt)") for r in results)
    assert validate_history(agent.history) == []
    assert len(client.requests) == 1


def test_cancel_at_a_batch_permission_prompt_answers_the_calls_as_cancelled():
    ran: list = []
    act = _tool("act", lambda: ran.append(1) or "acted", category="shell")
    client = _Client([llm_call(("act", {}), ("act", {})), llm_text("never asked")])

    def ask_once(calls: list) -> list[bool]:
        agent.cancel("stopped at the prompt")
        return [True] * len(calls)

    agent = _agent(client, [act], permission_batch_callback=ask_once)
    events = list(agent.chat("go"))

    assert events[-1] == Done("interrupted") and ran == []
    assert [r.output.startswith(f"{CANCELLED} Not run") for r in _results(events)] == [True] * 2
    assert validate_history(agent.history) == []


def test_cancel_during_a_tool_round_stops_before_the_next_request():
    def finishes(ctx: ToolContext | None = None) -> str:
        agent.cancel()  # e.g. the user's Ctrl+C lands just as it finishes
        return "finished anyway"

    client = _Client([llm_call(("step", {})), llm_text("never asked")])
    agent = _agent(client, [_tool("step", finishes)])
    events = list(agent.chat("go"))

    assert events[-1] == Done("interrupted")
    [res] = _results(events)
    assert res.output == "finished anyway"  # a result it has is kept
    assert len(client.requests) == 1
    assert validate_history(agent.history) == []


def test_calls_that_ignore_the_cancel_are_left_behind_after_the_grace(monkeypatch):
    monkeypatch.setattr(agent_mod, "CANCEL_GRACE", 0.3)
    release = threading.Event()
    started: list = []
    lock = threading.Lock()

    def stuck() -> str:
        with lock:
            started.append(1)
        release.wait(30)  # never looks at the cancel token
        return "too late"

    # Five calls, four workers: the fifth is still queued at the cancel.
    client = _Client([llm_call(*[("stuck", {})] * 5), llm_text("never asked")])
    agent = _agent(client, [_tool("stuck", stuck)])

    def cancel_once_four_run() -> None:
        while len(started) < 4:
            time.sleep(0.01)
        agent.cancel("the user pressed Ctrl+C")

    threading.Thread(target=cancel_once_four_run, daemon=True).start()
    try:
        t0 = time.monotonic()
        events = list(agent.chat("go"))
        elapsed = time.monotonic() - t0
    finally:
        release.set()

    assert events[-1] == Done("interrupted")
    assert elapsed < 5.0
    outputs = [r.output for r in _results(events)]
    running = (f"{CANCELLED} The turn was cancelled (the user pressed Ctrl+C) while this "
               "call was running.")
    assert [o.startswith(running) for o in outputs] == [True] * 4 + [False]
    assert outputs[4].startswith(f"{CANCELLED} Not run")
    assert validate_history(agent.history) == []
    time.sleep(0.2)
    assert len(started) == 4  # the queued call never started


def test_a_call_still_queued_at_the_cancel_never_starts():
    # Four calls stop as soon as the turn is cancelled (as run_command does),
    # which frees their workers during the grace. The fifth, still waiting for
    # a worker then, must not start: it could write a file or run a command.
    started: list[str] = []
    lock = threading.Lock()

    def stops(ctx: ToolContext | None = None) -> str:
        with lock:
            started.append("stops")
        ctx.cancel_token.wait(30)
        return "stopped"

    def writes() -> str:
        with lock:
            started.append("writes")
        return "wrote the file"

    client = _Client([llm_call(*[("stops", {})] * 4, ("writes", {})), llm_text("never asked")])
    agent = _agent(client, [_tool("stops", stops), _tool("writes", writes)])

    def cancel_once_four_run() -> None:
        while len(started) < 4:
            time.sleep(0.01)
        agent.cancel("the user pressed Ctrl+C")

    threading.Thread(target=cancel_once_four_run, daemon=True).start()
    events = list(agent.chat("go"))
    time.sleep(0.3)

    assert events[-1] == Done("interrupted")
    assert started == ["stops"] * 4  # the queued call never started
    outputs = [r.output for r in _results(events)]
    assert outputs[:4] == ["stopped"] * 4  # finished calls keep their output
    assert outputs[4].startswith(f"{CANCELLED} Not run: the turn was cancelled")
    assert validate_history(agent.history) == []


def test_a_call_a_worker_picks_up_after_the_cancel_never_starts():
    # The loop drops queued calls when it sees the cancel; one a worker took
    # in the moment before that is stopped at its start.
    ran: list = []
    agent = _agent(_Client([]), [_tool("writes", lambda: ran.append(1) or "wrote")])
    token = threading.Event()
    token.set()
    out = agent._start_call(token, agent.tools["writes"], "writes", "{}", None)
    assert out is agent_mod._NOT_STARTED and ran == []
    live = threading.Event()
    assert agent._start_call(live, agent.tools["writes"], "writes", "{}", None).text == "wrote"


def test_a_cancel_between_turns_does_not_touch_the_next_one():
    agent = _agent(_Client([llm_text("hello")]))
    agent.cancel("pressed while idle")
    events = list(agent.chat("hi"))
    assert events[-1] == Done("stop")
    assert agent.history[-1] == {"role": "assistant", "content": "hello"}


def test_a_cancel_sent_before_the_turn_runs_still_ends_it():
    # The web claims a turn and a worker iterates it later: a stop pressed in
    # between must not be lost. The turn starts when chat() is called.
    client = _Client([llm_text("the whole answer"), llm_text("a re-roll")])
    agent = _agent(client)
    stops = _stops(agent)
    turn = agent.chat("hi")
    assert agent.cancel("stopped right away")
    events = list(turn)

    assert events[-1] == Done("interrupted")
    assert not any(isinstance(e, TextDelta) for e in events)
    assert client.requests == []  # the model is never asked
    assert [m["role"] for m in agent.history] == ["system", "user"]
    assert validate_history(agent.history) == []
    assert stops == ["interrupted"]
    # A re-roll too.
    reroll = agent.continue_chat()
    agent.cancel()
    assert list(reroll)[-1] == Done("interrupted") and client.requests == []


def test_a_call_left_behind_keeps_its_cancel_while_the_next_turn_runs(monkeypatch):
    # A call deaf to the cancel (blocked in I/O, a custom tool that checks only
    # at the end) is left behind after the grace. The next turn starting at
    # once (a steer, the goal loop) must not make it think it can go on.
    monkeypatch.setattr(agent_mod, "CANCEL_GRACE", 0.3)
    release = threading.Event()
    later: list[bool] = []
    turn_two: list[bool] = []

    def deaf(ctx: ToolContext | None = None) -> str:
        release.wait(30)
        later.append(ctx.cancel_token.is_set())
        return "done late"

    def probe(ctx: ToolContext | None = None) -> str:
        turn_two.append(ctx.cancel_token.is_set())
        return "fine"

    client = _Client([llm_call(("deaf", {})), llm_call(("probe", {})), llm_text("turn two")])
    agent = _agent(client, [_tool("deaf", deaf), _tool("probe", probe)])
    threading.Timer(0.2, agent.cancel).start()
    try:
        assert list(agent.chat("one"))[-1] == Done("interrupted")
        assert list(agent.chat("two"))[-1] == Done("stop")
    finally:
        release.set()
    deadline = time.monotonic() + 5
    while not later and time.monotonic() < deadline:
        time.sleep(0.02)
    assert later == [True]  # still cancelled for the call turn one left behind
    assert turn_two == [False]  # and the next turn's calls start uncancelled


def test_an_agent_started_inside_a_tool_call_stops_with_that_turn():
    # A subagent (or a workflow step) runs its turn inside its parent's tool
    # call; cancelling the parent's turn stops it, and its own tools see that.
    parent_token = threading.Event()
    stream = _SlowStream([_text_chunk("sub ")], every=None)
    token = bind_context(ToolContext(cancel_token=parent_token))
    try:
        child = _agent(_Client([stream]))
        threading.Timer(0.3, parent_token.set).start()
        t0 = time.monotonic()
        events = list(child.chat("subtask"))
        elapsed = time.monotonic() - t0
    finally:
        unbind_context(token)
        stream.close()
    assert events[-1] == Done("interrupted")
    assert elapsed < 0.3 + 1.0
    assert child.cancel_token.is_set()
    assert validate_history(child.history) == []


def test_closing_the_turn_mid_tools_cancels_the_running_calls():
    seen: list = []

    def waits(ctx: ToolContext | None = None) -> str:
        seen.append(ctx.cancel_token.wait(10))
        return "x"

    agent = _agent(_Client([llm_call(("waits", {})), llm_text("never asked")]),
                   [_tool("waits", waits, long=True)])
    gen = agent.chat("go")
    for ev in gen:
        if isinstance(ev, ToolProgress):
            break
    gen.close()  # e.g. the REPL quitting on a second Ctrl+C
    assert seen == [True]


# ---- evi.proc.run -----------------------------------------------------------


def test_run_cancel_kills_the_whole_tree_and_returns_partial_output(tmp_path):
    script, pidfile = _tree_script(tmp_path)
    token = threading.Event()
    seen = _cancel_when(pidfile, token.set)
    try:
        res = proc.run([sys.executable, str(script), str(pidfile), "stay"], timeout=60,
                       cancel_token=token)
        returned_at = time.monotonic()
        no_survivor = _gone_within(seen["pids"], 2.0)
    finally:
        _kill(seen.get("pids", []))
    assert res.cancelled and res.killed and not res.timed_out
    assert returned_at - seen["at"] < 5.0
    assert res.stdout.startswith("started ")
    assert no_survivor


def test_run_returns_at_its_timeout_while_a_grandchild_holds_the_pipe(tmp_path):
    # subprocess.run(timeout=) on Windows kills the child, then waits on the
    # pipes the grandchild still holds: forever.
    script, pidfile = _tree_script(tmp_path)
    t0 = time.monotonic()
    try:
        res = proc.run([sys.executable, str(script), str(pidfile), "stay"], timeout=3)
        elapsed = time.monotonic() - t0
        pids = _read_pids(pidfile, 1)
        no_survivor = _gone_within(pids, 2.0)
    finally:
        if pidfile.exists():
            _kill(_read_pids(pidfile, 1))
    assert res.timed_out and res.killed
    assert 3 <= elapsed < 3 + 5
    assert res.stdout.startswith("started ")
    assert no_survivor


def test_run_returns_soon_after_the_child_exits_and_leaves_its_background_job(tmp_path):
    script, pidfile = _tree_script(tmp_path)
    t0 = time.monotonic()
    try:
        res = proc.run([sys.executable, str(script), str(pidfile), "exit"], timeout=60,
                       exit_grace=0.5)
        elapsed = time.monotonic() - t0
        child, holder = _read_pids(pidfile, 1)
        holder_alive = _alive(holder)
    finally:
        if pidfile.exists():
            _kill(_read_pids(pidfile, 1))
    assert res.detached and not res.killed and res.returncode == 0
    assert elapsed < 15  # a short grace after the child exits, not the 60 s timeout
    assert res.stdout.startswith("started ")
    assert holder_alive  # what it started in the background keeps running


def test_run_until_stops_the_tree_at_the_line():
    code = "import time; print('booting'); print('READY on 8000', flush=True); time.sleep(60)"
    t0 = time.monotonic()
    res = proc.run([sys.executable, "-c", code], timeout=60,
                   until=lambda line: line.startswith("READY"))
    assert res.matched and res.killed and not res.timed_out
    assert res.stdout == "booting\nREADY on 8000\n"
    assert time.monotonic() - t0 < 15


def test_run_reads_both_streams_and_the_exit_code():
    code = "import sys; print('out'); print('err', file=sys.stderr); sys.exit(3)"
    res = proc.run([sys.executable, "-c", code], timeout=60)
    assert (res.returncode, res.stdout, res.stderr) == (3, "out\n", "err\n")
    assert not (res.killed or res.detached or res.timed_out or res.cancelled)
    merged = proc.run([sys.executable, "-u", "-c", code], timeout=60, merge_stderr=True)
    assert merged.stdout in ("out\nerr\n", "err\nout\n") and merged.stderr == ""


def test_run_gives_the_child_no_terminal_input_unless_given_some():
    code = "import sys; print(ascii(sys.stdin.buffer.read().decode('utf-8')))"
    assert proc.run([sys.executable, "-c", code], timeout=60).stdout == "''\n"
    fed = proc.run([sys.executable, "-c", code], timeout=60, input="héllo")
    assert fed.stdout == "'h\\xe9llo'\n"


def test_scrubbed_env_drops_keys_tokens_and_secrets():
    env = {"OPENAI_API_KEY": "a", "GH_TOKEN": "b", "client_secret": "c", "PATH": "p",
           "HOME": "h", "KEYBOARD": "k", "TOKENIZERS_PARALLELISM": "t"}
    assert proc.scrubbed_env(env) == {"PATH": "p", "HOME": "h", "KEYBOARD": "k",
                                      "TOKENIZERS_PARALLELISM": "t"}


def test_runs_unattended_means_an_agent_nobody_can_ask():
    assert not proc.runs_unattended(None)
    assert not proc.runs_unattended(ToolContext())
    assert proc.runs_unattended(ToolContext(agent=SimpleNamespace()))
    asks = SimpleNamespace(permission_callback=lambda *a: True)
    assert not proc.runs_unattended(ToolContext(agent=asks))
    batch = SimpleNamespace(permission_batch_callback=lambda calls: [True] * len(calls))
    assert not proc.runs_unattended(ToolContext(agent=batch))


def test_runs_unattended_covers_remote_triggers_and_unattended_entry_points():
    from evi.llm.agent import remote_unattended
    from evi.llm.cli_agent import unattended

    # A channel push / federation / A2A run: a remote trigger that keeps a
    # callback refusing every call, so the agent still has one.
    agent = _agent(_Client([]), permission_callback=lambda *a: True)
    deny = lambda *_a: False  # noqa: E731
    with remote_unattended(agent, deny):
        assert agent.permission_callback is deny
        assert proc.runs_unattended(ToolContext(agent=agent))
    assert not proc.runs_unattended(ToolContext(agent=agent))  # put back as it was
    # Inside an unattended entry point (a web workflow, a channel), callback or not.
    asks = SimpleNamespace(permission_callback=lambda *a: True)
    with unattended("channel"):
        assert proc.runs_unattended(ToolContext(agent=asks))
        assert not proc.runs_unattended(None)  # still: no agent, nothing to judge


def test_unattended_entry_points_reach_the_tools_the_turn_runs():
    from evi.llm.cli_agent import unattended

    seen: list[bool] = []

    def probe(ctx: ToolContext | None = None) -> str:
        seen.append(proc.runs_unattended(ctx))
        return "ok"

    client = _Client([llm_call(("probe", {})), llm_text("done"),
                      llm_call(("probe", {})), llm_text("done")])
    agent = _agent(client, [_tool("probe", probe)], permission_callback=lambda *a: True)
    list(agent.chat("attended"))
    with unattended("channel"):
        list(agent.chat("from a channel"))
    assert seen == [False, True]  # the marker reaches the tool's pool thread


# Python waits for every non-daemon thread before it exits, a tool call's pool
# thread blocked in proc.run among them; the command, in its own process
# group, gets no Ctrl+C of its own.
_EXITS = r'''
import os, sys, time
from concurrent.futures import ThreadPoolExecutor
from evi import proc
script, pidfile = sys.argv[1], sys.argv[2]
pool = ThreadPoolExecutor(1)  # as Agent._run_calls runs a tool call
pool.submit(proc.run, [sys.executable, script, pidfile, "stay"], timeout=60)
while not os.path.exists(pidfile):
    time.sleep(0.05)
raise KeyboardInterrupt  # Ctrl+C at `evi serve` or the scheduler: eVi exits
'''


def test_a_command_still_running_when_evi_exits_is_stopped_with_it(tmp_path):
    script, pidfile = _tree_script(tmp_path)
    exits = tmp_path / "exits.py"
    exits.write_text(_EXITS, encoding="utf-8")
    env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1]))
    t0 = time.monotonic()
    try:
        subprocess.run([sys.executable, str(exits), str(script), str(pidfile)], env=env,
                       stdin=subprocess.DEVNULL, capture_output=True, timeout=40)
        elapsed = time.monotonic() - t0
        no_survivor = _gone_within(_read_pids(pidfile, 1), 2.0)
    finally:
        if pidfile.exists():
            _kill(_read_pids(pidfile, 1))
    assert elapsed < 20  # not the command's 60 s
    assert no_survivor, "a process of the command's tree outlived eVi"


# ---- the tools --------------------------------------------------------------


def test_run_command_timeout_kills_the_tree_and_keeps_partial_output(tmp_path):
    script, pidfile = _tree_script(tmp_path)
    try:
        out = run_command(f'"{sys.executable}" "{script}" "{pidfile}" stay', timeout=2)
        pids = _read_pids(pidfile, 1)
        no_survivor = _gone_within(pids, 2.0)
    finally:
        if pidfile.exists():
            _kill(_read_pids(pidfile, 1))
    assert out.startswith("ERROR: command timed out after 2s; its process tree was killed")
    assert "started" in out
    assert no_survivor


def test_run_command_says_when_it_left_a_background_job_running(tmp_path):
    script, pidfile = _tree_script(tmp_path)
    try:
        out = run_command(f'"{sys.executable}" "{script}" "{pidfile}" exit', timeout=60)
    finally:
        if pidfile.exists():
            _kill(_read_pids(pidfile, 1))
    assert out.startswith("exit 0\nstarted")
    assert "left a process running in the background" in out


def test_run_command_scrubs_secrets_only_for_an_unattended_or_sandboxed_run(monkeypatch):
    monkeypatch.setenv("EVI_TEST_API_KEY", "sk-test")
    monkeypatch.setenv("EVI_TEST_PLAIN", "visible")
    code = "import os; print(os.environ.get('EVI_TEST_API_KEY'), os.environ.get('EVI_TEST_PLAIN'))"
    command = f'"{sys.executable}" -c "{code}"'
    asks = ToolContext(agent=SimpleNamespace(permission_callback=lambda *a: True))
    nobody = ToolContext(agent=SimpleNamespace(permission_callback=None))
    assert run_command(command, timeout=60) == "exit 0\nsk-test visible"
    assert run_command(command, timeout=60, ctx=asks) == "exit 0\nsk-test visible"
    assert run_command(command, timeout=60, ctx=nobody) == "exit 0\nNone visible"


def test_run_command_scrubs_secrets_for_a_channel_push(monkeypatch):
    # The channel push shape (server.py): remote_unattended with the web's
    # deny-everything callback, inside cli_agent.unattended("channel").
    from evi.llm.agent import remote_unattended
    from evi.llm.cli_agent import unattended

    monkeypatch.setenv("EVI_TEST_API_KEY", "sk-test")
    monkeypatch.setenv("EVI_TEST_PLAIN", "visible")
    code = "import os; print(os.environ.get('EVI_TEST_API_KEY'), os.environ.get('EVI_TEST_PLAIN'))"
    command = f'"{sys.executable}" -c "{code}"'
    agent = _agent(_Client([]), permission_callback=lambda *a: True)
    with remote_unattended(agent, lambda *_a: False):
        out = run_command(command, timeout=60, ctx=ToolContext(agent=agent))
    assert out == "exit 0\nNone visible"
    attended = SimpleNamespace(permission_callback=lambda *a: True)
    with unattended("channel"):
        out = run_command(command, timeout=60, ctx=ToolContext(agent=attended))
    assert out == "exit 0\nNone visible"


def test_run_python_stops_when_the_turn_is_cancelled():
    token = threading.Event()
    threading.Timer(0.5, token.set).start()
    code = "import time\nprint('begun', flush=True)\ntime.sleep(30)\n"
    t0 = time.monotonic()
    out = REGISTRY["run_python"].call(json.dumps({"code": code}),
                                      ToolContext(cancel_token=token))
    assert time.monotonic() - t0 < 6
    assert out.startswith("(cancelled)") and "begun" in out


def test_monitor_command_stops_when_the_turn_is_cancelled():
    cfg = Config()
    cfg.tools.shell = True
    token = threading.Event()
    threading.Timer(0.5, token.set).start()
    target = f'"{sys.executable}" -u -c "import time; print(1); time.sleep(30)"'
    t0 = time.monotonic()
    out = REGISTRY["monitor"].call(
        json.dumps({"target": target, "kind": "command", "timeout": 60}),
        ToolContext(agent=SimpleNamespace(config=cfg), cancel_token=token),
    )
    assert time.monotonic() - t0 < 6
    assert out.endswith("[cancelled: the turn was cancelled, so the command was stopped]")


def test_monitor_tail_stops_when_the_turn_is_cancelled(tmp_path):
    log = tmp_path / "app.log"
    log.write_text("", encoding="utf-8")
    token = threading.Event()
    threading.Timer(0.3, token.set).start()
    t0 = time.monotonic()
    out = REGISTRY["monitor"].call(json.dumps({"target": str(log), "timeout": 60}),
                                   ToolContext(cancel_token=token))
    assert time.monotonic() - t0 < 3
    assert out == "(no new lines)\n[cancelled]"


# ---- the REPL ---------------------------------------------------------------


def _repl(monkeypatch, tmp_path: Path, typed: list[str]):
    """evi.apps.cli.main, with the REPL reading `typed` (then EOF), and the
    list of the lines it has read so far."""
    import evi.apps.cli.main as cli_main
    import evi.repl_input as repl_input
    from evi.commands import CommandStore

    if threading.current_thread() is not threading.main_thread():
        pytest.skip("Python runs signal handlers on the main thread only")
    lines = iter(typed)
    reads: list[str] = []

    class _Input:
        def __init__(self, agent) -> None:
            pass

        def read(self, prompt: str) -> str:
            try:
                line = next(lines)
            except StopIteration:
                raise EOFError
            reads.append(line)
            return line

    monkeypatch.setattr(repl_input, "ReplInput", _Input)
    monkeypatch.setattr(
        cli_main, "CommandStore", lambda *a, **k: CommandStore(root=tmp_path / "commands")
    )
    monkeypatch.setattr(cli_main.atexit, "register", lambda *a, **k: None)
    monkeypatch.setenv("EVI_INTERACTIVE", "0")  # restored after; the REPL sets it
    return cli_main, reads


def _press_ctrl_c() -> bool:
    """Send this process SIGINT as Ctrl+C does, but only while the REPL's turn
    handler is installed: the default one would raise KeyboardInterrupt into
    pytest itself."""
    import evi.apps.cli.main as cli_main

    handler = signal.getsignal(signal.SIGINT)
    if not isinstance(getattr(handler, "__self__", None), cli_main._ReplCtrlC):
        return False
    signal.raise_signal(signal.SIGINT)
    return True


def _run(cli_main, agent: Agent) -> None:
    try:
        cli_main._run_repl(agent)
    except KeyboardInterrupt:
        pytest.fail("Ctrl+C escaped the REPL")


def test_repl_ctrl_c_mid_tool_returns_to_the_prompt_and_the_next_prompt_works(
    monkeypatch, tmp_path, capsys
):
    cli_main, reads = _repl(monkeypatch, tmp_path, ["run the slow thing", "and now?"])
    pressed: list[bool] = []

    def slow(ctx: ToolContext | None = None) -> str:
        pressed.append(_press_ctrl_c())
        stopped = ctx.cancel_token.wait(10)
        return "stopped early" if stopped else "ran to the end"

    client = _Client([llm_call(("slow", {})), llm_text("the second answer")])
    agent = _agent(client, [_tool("slow", slow)])
    before = signal.getsignal(signal.SIGINT)

    _run(cli_main, agent)

    assert pressed == [True]
    assert signal.getsignal(signal.SIGINT) is before  # the turn's handler is gone
    assert reads == ["run the slow thing", "and now?"]
    assert [m["role"] for m in agent.history] == [
        "system", "user", "assistant", "tool", "user", "assistant"]
    assert agent.history[3]["content"] == "stopped early"
    assert agent.history[-1] == {"role": "assistant", "content": "the second answer"}
    assert validate_history(agent.history) == []
    out = capsys.readouterr().out
    assert "interrupted" in out and "the second answer" in out


def test_repl_second_ctrl_c_within_two_seconds_quits(monkeypatch, tmp_path, capsys):
    cli_main, reads = _repl(monkeypatch, tmp_path, ["run the slow thing", "never read"])
    pressed: list[bool] = []

    def slow(ctx: ToolContext | None = None) -> str:
        pressed.append(_press_ctrl_c())
        ctx.cancel_token.wait(10)  # the first press cancelled the turn
        pressed.append(_press_ctrl_c())
        time.sleep(1.0)  # still running when the second press lands
        return "x"

    agent = _agent(_Client([llm_call(("slow", {}))]), [_tool("slow", slow)])
    before = signal.getsignal(signal.SIGINT)

    _run(cli_main, agent)  # returns: quit, not crashed

    assert pressed == [True, True]
    assert reads == ["run the slow thing"]  # it quit, prompting no more
    assert signal.getsignal(signal.SIGINT) is before
    assert "bye." in capsys.readouterr().out


def test_repl_ctrl_c_at_a_permission_question_answers_no_and_cancels(
    monkeypatch, tmp_path, capsys
):
    cli_main, reads = _repl(monkeypatch, tmp_path, ["do it", "thanks"])
    asked: list[str] = []

    def typed_answer(prompt: str = "") -> str:
        asked.append(prompt)
        _press_ctrl_c()  # raises out of here when the REPL handles Ctrl+C
        return "y"

    monkeypatch.setattr(cli_main.console, "input", typed_answer)
    ran: list = []
    client = _Client([llm_call(("act", {})), llm_text("ok, not done")])
    agent = _agent(client, [_tool("act", lambda: ran.append(1) or "acted", category="shell")],
                   permission_callback=cli_main._cli_permission_prompt)

    _run(cli_main, agent)

    assert len(asked) == 1 and ran == []
    assert reads == ["do it", "thanks"]
    assert agent.history[3]["content"].startswith(f"{CANCELLED} Not run")
    assert agent.history[-1] == {"role": "assistant", "content": "ok, not done"}
    assert validate_history(agent.history) == []
    assert cli_main._PROMPT_OPEN == [False]
    assert "interrupted" in capsys.readouterr().out


def test_repl_ctrl_c_at_a_subagents_permission_question_answers_no_and_cancels(
    monkeypatch, tmp_path, capsys
):
    # A subagent asks through its parent's callback (_cli_permission_prompt),
    # on the parent's tool thread, where the Ctrl+C handler can't raise.
    import evi.llm.subagent as subagent

    cli_main, reads = _repl(monkeypatch, tmp_path, ["delegate it", "and now?"])
    still_typing = threading.Event()
    asked: list[bool] = []

    def typed_answer(prompt: str = "") -> str:
        asked.append(threading.current_thread() is threading.main_thread())
        _press_ctrl_c()
        still_typing.wait(10)  # the user hasn't answered when Ctrl+C lands
        return "y"

    monkeypatch.setattr(cli_main.console, "input", typed_answer)
    ran: list = []
    monkeypatch.setitem(REGISTRY, "act_for_parent",
                        _tool("act_for_parent", lambda: ran.append(1) or "acted",
                              category="shell"))
    child_client = _Client([llm_call(("act_for_parent", {})), llm_text("never asked")])
    monkeypatch.setattr(subagent, "make_client", lambda *_a, **_k: child_client)

    def delegate() -> str:
        return subagent.run_subagent(system_prompt="sub", task="act",
                                     tools=["act_for_parent"])

    client = _Client([llm_call(("delegate", {})), llm_text("the second answer")])
    agent = _agent(client, [_tool("delegate", delegate)],
                   permission_callback=cli_main._cli_permission_prompt)
    try:
        _run(cli_main, agent)
    finally:
        still_typing.set()

    assert asked == [False]  # asked once, on the tool thread
    assert ran == []
    assert reads == ["delegate it", "and now?"]
    # The question ended at the cancel, so the subagent stopped and handed
    # back its own result in time (not "left behind").
    result = agent.history[3]["content"]
    assert not result.startswith(CANCELLED) and "act_for_parent: (cancelled) Not run" in result
    assert len(child_client.requests) == 1  # the subagent wasn't asked again
    assert agent.history[-1] == {"role": "assistant", "content": "the second answer"}
    assert validate_history(agent.history) == []
    assert cli_main._PROMPT_OPEN == [False]
    assert "interrupted" in capsys.readouterr().out


@pytest.mark.parametrize("calls_per_child", [1, 2], ids=["one-question", "batch-question"])
def test_repl_ctrl_c_asks_a_subagent_waiting_its_turn_to_ask_nothing(
    monkeypatch, tmp_path, capsys, calls_per_child
):
    # Two subagents of one batch both need to ask; their questions go one at a
    # time (subagent._one_at_a_time). Ctrl+C lands at the first while the
    # second waits to ask: the second must not open a question (and a console
    # read the Ctrl+C can no longer end) after the cancel.
    import evi.llm.subagent as subagent

    cli_main, reads = _repl(monkeypatch, tmp_path, ["delegate both", "and now?"])
    still_typing = threading.Event()
    both_waiting = threading.Event()
    arrived: list[int] = []
    asked: list[str] = []

    real_one_at_a_time = subagent._one_at_a_time

    def counted(ask, parent, child):
        serialized = real_one_at_a_time(ask, parent, child)
        if serialized is None:
            return None

        def arrive(*args):
            arrived.append(1)  # past the subagent's own checks, about to ask
            if len(arrived) >= 2:
                both_waiting.set()
            return serialized(*args)

        return arrive

    monkeypatch.setattr(subagent, "_one_at_a_time", counted)

    def typed_answer(prompt: str = "") -> str:
        asked.append(prompt)
        if len(asked) == 1:
            assert both_waiting.wait(10), "the second subagent never got to ask"
            time.sleep(0.05)  # it is now blocked behind this question
            _press_ctrl_c()
        still_typing.wait(10)  # the user hasn't answered when Ctrl+C lands
        return "y"

    monkeypatch.setattr(cli_main.console, "input", typed_answer)
    ran: list = []
    monkeypatch.setitem(REGISTRY, "act_for_parent",
                        _tool("act_for_parent", lambda: ran.append(1) or "acted",
                              category="shell"))
    child_calls = [("act_for_parent", {})] * calls_per_child
    monkeypatch.setattr(subagent, "make_client", lambda *_a, **_k: _Client(
        [llm_call(*child_calls), llm_text("never asked")]))

    def delegate() -> str:
        return subagent.run_subagent(system_prompt="sub", task="act",
                                     tools=["act_for_parent"])

    client = _Client([llm_call(("delegate", {}), ("delegate", {})),
                      llm_text("the second answer")])
    agent = _agent(client, [_tool("delegate", delegate)],
                   permission_callback=cli_main._cli_permission_prompt,
                   permission_batch_callback=cli_main._cli_permission_prompt_batch)
    try:
        _run(cli_main, agent)
        out = capsys.readouterr().out
    finally:
        still_typing.set()

    assert len(arrived) == 2
    assert len(asked) == 1  # only the question open at the Ctrl+C was read
    assert out.count("permission:") == 1  # the second was never even shown
    assert ran == []
    assert reads == ["delegate both", "and now?"]
    results = [m["content"] for m in agent.history if m["role"] == "tool"]
    assert len(results) == 2 and all(CANCELLED in r for r in results)
    assert agent.history[-1] == {"role": "assistant", "content": "the second answer"}
    assert validate_history(agent.history) == []
    assert "interrupted" in out


def test_repl_ctrl_c_just_before_its_own_question_opens_reads_nothing(
    monkeypatch, tmp_path, capsys
):
    # Ctrl+C lands after the agent's last check but before the REPL's own
    # question reads (here: while its panel prints, with no question open
    # yet to raise into). The question is answered no without a read.
    cli_main, reads = _repl(monkeypatch, tmp_path, ["do it", "thanks"])
    asked: list[str] = []
    pressed: list[bool] = []
    real_print = cli_main.console.print

    def printing(*args, **kwargs):
        real_print(*args, **kwargs)
        if not pressed and args and "permission:" in str(args[0]):
            pressed.append(_press_ctrl_c())

    def typed_answer(prompt: str = "") -> str:
        asked.append(prompt)
        return "y"

    monkeypatch.setattr(cli_main.console, "print", printing)
    monkeypatch.setattr(cli_main.console, "input", typed_answer)
    ran: list = []
    client = _Client([llm_call(("act", {})), llm_text("ok, not done")])
    agent = _agent(client, [_tool("act", lambda: ran.append(1) or "acted", category="shell")],
                   permission_callback=cli_main._cli_permission_prompt)

    _run(cli_main, agent)

    assert pressed == [True]
    assert asked == [] and ran == []
    assert reads == ["do it", "thanks"]
    assert agent.history[3]["content"].startswith(f"{CANCELLED} Not run")
    assert agent.history[-1] == {"role": "assistant", "content": "ok, not done"}
    assert validate_history(agent.history) == []
    assert cli_main._PROMPT_OPEN == [False] and cli_main._REPL_TURN == [None]
    assert "interrupted" in capsys.readouterr().out


def test_repl_survives_a_question_interrupt_that_escapes_the_turn(monkeypatch, tmp_path, capsys):
    # Belt and braces: should Ctrl+C meant for a question get raised where no
    # question is open (here: a tool thread marks one open on the REPL's
    # thread), the REPL still goes back to its prompt.
    cli_main, reads = _repl(monkeypatch, tmp_path, ["go", "and now?"])

    def marks_a_question_open(ctx: ToolContext | None = None) -> str:
        cli_main._PROMPT_OPEN[0] = True
        _press_ctrl_c()
        ctx.cancel_token.wait(10)
        time.sleep(0.3)  # the REPL's thread raises at its next check
        return "x"

    client = _Client([llm_call(("mark", {})), llm_text("the second answer")])
    agent = _agent(client, [_tool("mark", marks_a_question_open)])
    try:
        _run(cli_main, agent)
    finally:
        cli_main._PROMPT_OPEN[0] = False

    assert reads == ["go", "and now?"]
    assert agent.history[-1] == {"role": "assistant", "content": "the second answer"}
    assert "interrupted" in capsys.readouterr().out
