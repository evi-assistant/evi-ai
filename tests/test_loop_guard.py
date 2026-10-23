"""The repeat guard: a call that keeps failing the same way ends the turn as
loop_detected (evi.llm.agent._RepeatGuard)."""

from __future__ import annotations

import evi.tools.fs  # noqa: F401  registers read_file, write_file
from evi.config import Config
from evi.events import STOP_REASONS
from evi.headless import EXIT_CODES
from evi.llm.agent import LOOP_NOTE, LOOP_REPEATS, Agent, Done, ToolResult, _RepeatGuard
from evi.tools.base import REGISTRY
from tests.conftest import llm_call, llm_text


def _agent(client) -> Agent:
    return Agent(client=client, config=Config(), transcripts=None, unattended_policy="allow",
                 tools=[REGISTRY["read_file"], REGISTRY["write_file"]])


def _run(agent: Agent, max_turns: int = 10) -> tuple[list[str], str]:
    events = list(agent.chat("go", max_turns=max_turns))
    outputs = [e.output for e in events if isinstance(e, ToolResult)]
    done = next(e for e in events if isinstance(e, Done))
    return outputs, done.reason


def test_the_same_failing_call_ends_the_turn_as_loop_detected(fake_llm, tmp_path):
    missing = {"path": str(tmp_path / "missing.txt")}
    client = fake_llm([llm_call(("read_file", missing)) for _ in range(LOOP_REPEATS)]
                      + [llm_text("never reached")])
    outputs, reason = _run(_agent(client))

    assert reason == "loop_detected" and "loop_detected" in STOP_REASONS
    assert EXIT_CODES["loop_detected"] == 3
    assert len(outputs) == LOOP_REPEATS
    assert all(o.startswith("ERROR: not a file") for o in outputs)
    assert LOOP_NOTE in outputs[-1] and not any(LOOP_NOTE in o for o in outputs[:-1])
    assert len(client.requests) == LOOP_REPEATS  # no further round-trip


def test_a_write_in_between_starts_the_count_again(fake_llm, tmp_path):
    missing = {"path": str(tmp_path / "missing.txt")}
    note = {"path": str(tmp_path / "note.txt"), "content": "x\n"}
    client = fake_llm([llm_call(("read_file", missing)), llm_call(("read_file", missing)),
                       llm_call(("write_file", note)),
                       llm_call(("read_file", missing)), llm_call(("read_file", missing)),
                       llm_text("done")])
    outputs, reason = _run(_agent(client))

    assert reason != "loop_detected" and not any(LOOP_NOTE in o for o in outputs)
    assert (tmp_path / "note.txt").read_text(encoding="utf-8") == "x\n"


def test_what_counts_as_the_same_failure():
    guard = _RepeatGuard()
    # A timing differs from run to run; the failure is the same.
    assert not guard.note("run_command", '{"command": "pytest"}', "exit 1\nE x\n1 failed in 2.07s")
    assert not guard.note("run_command", '{"command": "pytest"}', "exit 1\nE x\n1 failed in 3.1s")
    # Other arguments, or another way of failing, are counted on their own.
    assert not guard.note("run_command", '{"command": "pytest -x"}', "exit 1\nE x")
    assert not guard.note("run_command", '{"command": "pytest"}', "exit 2\nusage: pytest")
    assert guard.note("run_command", '{"command": "pytest"}', "exit 1\nE x\n1 failed in 9.9s")
    # Success is not a failure; a denial is.
    guard = _RepeatGuard()
    assert not any(guard.note("run_command", "{}", "exit 0\nok") for _ in range(5))
    assert [guard.note("web_fetch", "{}", "PERMISSION DENIED: no") for _ in range(3)] == [
        False, False, True]
