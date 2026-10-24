"""A turn's last step runs with no tools and asks for a hand-off: when its
step budget (max_turns) is spent, and when the loop guard stops it. The turn
still ends max_turns_reached / loop_detected, now with the model's report."""

from __future__ import annotations

import evi.tools.fs  # noqa: F401  registers read_file and edit_file
from evi.config import Config
from evi.history import is_note
from evi.llm.agent import BUDGET_NOTE, Agent, CheckResult, Done, TextDelta, ToolResult
from evi.tools.base import REGISTRY
from tests.conftest import llm_call, llm_text


def _agent(client, work) -> Agent:
    agent = Agent(client=client, config=Config(), transcripts=None, unattended_policy="allow",
                  tools=[REGISTRY[n] for n in ("read_file", "edit_file")])
    agent.cwd = str(work)
    return agent


def _read(path: str = "f.txt"):
    return llm_call(("read_file", {"path": path}))


def test_the_last_step_has_no_tools_and_its_hand_off_ends_the_turn(fake_llm, tmp_path):
    (tmp_path / "f.txt").write_text("x\n", encoding="utf-8")
    client = fake_llm([_read(), _read(), llm_text("Done: read f.txt. Left: nothing. Next: none.")])
    agent = _agent(client, tmp_path)
    events = list(agent.chat("go", max_turns=3))
    assert events[-1] == Done("max_turns_reached")
    assert "".join(e.text for e in events if isinstance(e, TextDelta)).startswith("Done: read")
    requests = client.chat.completions.requests
    assert [bool(r.get("tools")) for r in requests] == [True, True, False]
    # The note that asked for it is in the history, framed, before the hand-off.
    note = agent.history[-2]
    assert is_note(note) and BUDGET_NOTE in note["content"]
    assert agent.history[-1] == {"role": "assistant",
                                 "content": "Done: read f.txt. Left: nothing. Next: none."}


def test_a_turn_that_ends_early_never_sees_the_budget_note(fake_llm, tmp_path):
    (tmp_path / "f.txt").write_text("x\n", encoding="utf-8")
    agent = _agent(fake_llm([_read(), llm_text("all done")]), tmp_path)
    events = list(agent.chat("go", max_turns=3))
    assert events[-1] == Done("stop")
    assert not any(is_note(m) for m in agent.history)


def test_one_step_turns_and_text_calls_on_the_last_step(fake_llm, tmp_path):
    # max_turns=1 has no step to spare: the single request keeps its tools.
    (tmp_path / "f.txt").write_text("x\n", encoding="utf-8")
    client = fake_llm([_read()])
    events = list(_agent(client, tmp_path).chat("go", max_turns=1))
    assert events[-1] == Done("max_turns_reached")
    assert client.chat.completions.requests[0].get("tools")
    # A hand-off written as JSON on the last step stays text: nothing runs.
    client = fake_llm([_read(), llm_text('{"name": "read_file", "arguments": {"path": "f.txt"}}')])
    events = list(_agent(client, tmp_path).chat("go", max_turns=2))
    assert events[-1] == Done("max_turns_reached")
    assert len([e for e in events if isinstance(e, ToolResult)]) == 1


def test_the_loop_guard_lets_the_model_report_before_the_turn_ends(fake_llm, tmp_path):
    (tmp_path / "f.txt").write_text("a = 1\n", encoding="utf-8")
    miss = llm_call(("edit_file", {"path": "f.txt", "old_string": "zzz", "new_string": "y"}))
    client = fake_llm([miss, miss, miss, llm_text("Tried: edit_file thrice. Blocked: no 'zzz'.")])
    events = list(_agent(client, tmp_path).chat("go", max_turns=10))
    assert events[-1] == Done("loop_detected")
    assert "".join(e.text for e in events if isinstance(e, TextDelta)).startswith("Tried:")
    requests = client.chat.completions.requests
    assert len(requests) == 4 and not requests[-1].get("tools")


def test_a_loop_on_the_last_step_ends_without_another_request(fake_llm, tmp_path):
    (tmp_path / "f.txt").write_text("a = 1\n", encoding="utf-8")
    miss = llm_call(("edit_file", {"path": "f.txt", "old_string": "zzz", "new_string": "y"}))
    client = fake_llm([miss, miss, miss])
    events = list(_agent(client, tmp_path).chat("go", max_turns=3))
    assert events[-1] == Done("loop_detected")
    assert len(client.chat.completions.requests) == 3


def test_the_hand_off_step_runs_no_checks(fake_llm, tmp_path, monkeypatch):
    # Out of steps is not done: a passing suite on the hand-off mustn't make it "stop".
    import evi.llm.agent as agent_mod

    ran: list[str] = []

    def gate(self, state):
        ran.append("gate")
        return None
        yield  # a generator, as the real gate is

    monkeypatch.setattr(agent_mod.Agent, "_verify_gate", gate)
    (tmp_path / "f.txt").write_text("x\n", encoding="utf-8")
    events = list(_agent(fake_llm([_read(), llm_text("hand-off")]), tmp_path)
                  .chat("go", max_turns=2))
    assert events[-1] == Done("max_turns_reached") and ran == []
    assert not any(isinstance(e, CheckResult) for e in events)
