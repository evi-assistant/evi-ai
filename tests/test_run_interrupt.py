"""`evi run` on Ctrl+C / SIGINT (loop-agent-cancel: "On SIGINT, `evi run`
cancels and emits its result").

It used to die with a KeyboardInterrupt traceback and no result. Now the
first SIGINT cancels the turn (Agent.cancel: its commands' process trees are
killed, unanswered calls get "(cancelled)" results) and the run reports it:
stop_reason "interrupted" in the json, exit code 130 in either format, the
text that streamed before it on stdout. A second SIGINT stops waiting for the
turn to wind down and still reports. headless.run_headless carries the stop
reason for every caller (`stop_reason` in the json).

SIGINT is sent to this very process (signal.raise_signal from the tool's
thread, delivered on the main thread where the handler is installed), as a
terminal's Ctrl+C would.
"""

from __future__ import annotations

import json
import signal
import threading

import pytest

from evi.config import Config
from evi.headless import HeadlessResult, run_headless, to_json
from evi.history import validate_history
from evi.llm.agent import Agent, Done, TextDelta
from evi.tools.base import Tool, ToolContext
from tests._live_web import Scripted
from tests.conftest import _text_chunk, llm_call, llm_text


@pytest.fixture(autouse=True)
def main_thread_only():
    if threading.current_thread() is not threading.main_thread():
        pytest.skip("Python runs signal handlers on the main thread only")
    before = signal.getsignal(signal.SIGINT)
    yield
    assert signal.getsignal(signal.SIGINT) is before  # the run's handler is gone


def _press_ctrl_c() -> None:
    """SIGINT to this process, only while a run's handler is installed (the
    default one would raise KeyboardInterrupt into pytest itself)."""
    import evi.headless as headless

    handler = signal.getsignal(signal.SIGINT)
    assert isinstance(getattr(handler, "__self__", None), headless._SigintCancels), handler
    signal.raise_signal(signal.SIGINT)


def _slow_agent(presses: int = 1, *, honour_cancel: bool = True):
    """A turn that says something, then runs a tool that presses Ctrl+C
    `presses` times and waits for the cancel (or ignores it)."""
    release = threading.Event()
    log: list[str] = []

    def slow(ctx: ToolContext | None = None) -> str:
        _press_ctrl_c()
        if honour_cancel and ctx.cancel_token.wait(20):
            log.append("cancelled")
            return "stopped early"
        for _ in range(presses - 1):
            ctx.cancel_token.wait(5)
            _press_ctrl_c()
        release.wait(20)
        return "ran to the end"

    tool = Tool(name="slow", description="slow", func=slow, category="fs",
                parameters={"type": "object", "properties": {}})
    client = Scripted([
        [_text_chunk("working on it "), *llm_call(("slow", {}))],
        llm_text("never asked"),
    ])
    agent = Agent(client=client, config=Config(), tools=[tool], transcripts=None,
                  unattended_policy="allow")
    return agent, release, log


def test_the_result_carries_the_turns_stop_reason() -> None:
    class _Fake:
        def chat(self, prompt, max_turns=12):
            yield TextDelta("hi")
            yield Done("max_turns_reached")

    res = run_headless(_Fake(), "x")
    assert res.stop_reason == "max_turns_reached" and not res.interrupted
    assert json.loads(to_json(res))["stop_reason"] == "max_turns_reached"


def test_sigint_cancels_the_turn_and_the_result_says_interrupted() -> None:
    agent, _release, log = _slow_agent()
    res = run_headless(agent, "go", cancel_on_sigint=True)
    assert res.stop_reason == "interrupted" and res.interrupted
    assert log == ["cancelled"]
    assert res.tools == [{"name": "slow", "output": "stopped early"}]
    assert res.text.startswith("working on it")
    assert res.error is None
    assert len(agent.client.requests) == 1  # the model is not asked again
    assert validate_history(agent.history) == []
    assert json.loads(to_json(res))["stop_reason"] == "interrupted"


def test_a_second_sigint_stops_waiting_and_still_returns_a_result() -> None:
    agent, release, _log = _slow_agent(presses=2, honour_cancel=False)
    try:
        res = run_headless(agent, "go", cancel_on_sigint=True)
    finally:
        release.set()
    assert res.stop_reason == "interrupted"
    assert res.text.startswith("working on it")


def test_without_cancel_on_sigint_ctrl_c_is_left_alone() -> None:
    """Other callers (a batch, the web's channel run) keep Ctrl+C as it was:
    no handler is installed."""
    seen: list = []

    class _Fake:
        def chat(self, prompt, max_turns=12):
            seen.append(signal.getsignal(signal.SIGINT))
            yield Done("stop")

    before = signal.getsignal(signal.SIGINT)
    run_headless(_Fake(), "x")
    assert seen == [before]


# ---- the `evi run` command ----------------------------------------------------------


def _invoke(monkeypatch, agent_or_error, *args: str):
    from typer.testing import CliRunner

    import evi.apps.cli.main as cli_main

    def build(*_a, **_k):
        if isinstance(agent_or_error, BaseException):
            raise agent_or_error
        return agent_or_error

    monkeypatch.setattr(cli_main, "_build_agent", build)
    monkeypatch.setattr(cli_main.Config, "load", classmethod(lambda cls: Config()))
    return CliRunner().invoke(cli_main.app, ["run", "go", *args])


def test_evi_run_json_on_sigint_reports_interrupted_and_exits_130(monkeypatch) -> None:
    agent, _release, _log = _slow_agent()
    r = _invoke(monkeypatch, agent, "--format", "json")
    assert r.exit_code == 130, r.output
    assert "Traceback" not in r.output
    out = json.loads(r.stdout.strip().splitlines()[-1])
    assert out["stop_reason"] == "interrupted"
    assert out["tools"] == [{"name": "slow", "output": "stopped early"}]
    assert out["text"].startswith("working on it") and out["error"] is None


def test_evi_run_text_on_sigint_prints_what_streamed_and_exits_130(monkeypatch) -> None:
    agent, _release, _log = _slow_agent()
    r = _invoke(monkeypatch, agent)
    assert r.exit_code == 130
    assert r.stdout.strip().startswith("working on it")
    assert "interrupted" in r.stderr and "Traceback" not in r.stderr


def test_evi_run_interrupted_before_the_turn_starts_still_reports(monkeypatch) -> None:
    """Ctrl+C while the agent is built (MCP servers starting, say)."""
    r = _invoke(monkeypatch, KeyboardInterrupt(), "--format", "json")
    assert r.exit_code == 130
    assert json.loads(r.stdout.strip()) == {
        "text": "", "tools": [], "usage": None, "error": None, "stop_reason": "interrupted"}


def test_evi_run_that_finishes_reports_its_stop_reason(monkeypatch) -> None:
    agent = Agent(client=Scripted([llm_text("all done")]), config=Config(), tools=[],
                  transcripts=None)
    r = _invoke(monkeypatch, agent, "--format", "json")
    assert r.exit_code == 0
    out = json.loads(r.stdout.strip())
    assert out["stop_reason"] == "stop" and out["text"] == "all done"
    assert HeadlessResult().stop_reason is None  # a result nobody filled in says nothing
