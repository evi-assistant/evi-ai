"""Compaction hardening (ctx-compaction-hardening): a summary reply that is
empty, a refusal, missing its headings or too short is asked for once more,
then the history is left as it was; and a claim that a check passed, with
no command output in the stretch to show it, is marked UNVERIFIED."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from evi import summary_check
from evi.config import Config
from evi.llm.agent import SUMMARY_SECTIONS, Agent

pytestmark = pytest.mark.usefixtures("summary_checks")


def good(done: str = "- read config.py", *, extra: str = "") -> str:
    body = {
        "Objective": "- make the parser accept tabs",
        "Decisions": "- keep the old API",
        "Done": done,
        "In progress": "- the tokenizer change",
        "Files": "- evi/parse.py, tests/test_parse.py",
        "Blockers": "- none",
        "Next step": "- run the parser tests" + extra,
    }
    return "\n".join(f"## {k}\n{v}" for k, v in body.items())


class _Summarizer:
    def __init__(self, *replies: str) -> None:
        self.replies, self.prompts = list(replies), []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    def create(self, **kw):
        self.prompts.append(kw["messages"][1]["content"])
        text = self.replies.pop(0) if self.replies else ""
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=text))])


def _agent(client, tool_output: tuple[str, str] | None = None) -> Agent:
    agent = Agent(client=client, config=Config(), tools=[], transcripts=None)
    for i in range(6):
        agent.history.append({"role": "user", "content": f"user {i}"})
        if i == 1 and tool_output is not None:
            name, out = tool_output
            call = {"id": "c1", "type": "function",
                    "function": {"name": name, "arguments": "{}"}}
            agent.history += [
                {"role": "assistant", "content": "", "tool_calls": [call]},
                {"role": "tool", "tool_call_id": "c1", "name": name, "content": out},
            ]
        agent.history.append({"role": "assistant", "content": f"reply {i}"})
    return agent


def _summary(agent: Agent) -> str:
    return agent.history[1]["content"].partition("\n\n")[2]


# ---- evi.summary_check.problem --------------------------------------------------


@pytest.mark.parametrize("reply, why", [
    ("", "empty"),
    ("   ", "empty"),
    ("I'm sorry, but I can't help with summarising this conversation.", "a refusal"),
    ("As an AI, I cannot do that.", "a refusal"),
    ("The user asked for tabs and the assistant edited parse.py.", "missing its headings"),
    ("## Objective\ntabs\n## Done\nx\n## Files\ny\n## Next step\nz", "too short"),
])
def test_a_bad_reply_says_why(reply, why):
    assert summary_check.problem(reply, "short talk", SUMMARY_SECTIONS) == why


def test_a_good_reply_and_one_with_headings_in_another_style_pass():
    assert summary_check.problem(good(), "short talk", SUMMARY_SECTIONS) == ""
    bold = good().replace("## ", "**").replace("\n-", "**\n-")
    assert summary_check.problem(bold, "short talk", SUMMARY_SECTIONS) == ""


def test_a_long_stretch_needs_a_longer_summary():
    terse = "\n".join(f"## {s}\nnone" for s in SUMMARY_SECTIONS) + "\nwords " * 12
    assert summary_check.problem(terse, "a few words", SUMMARY_SECTIONS) == ""
    assert summary_check.problem(terse, "word " * 2000, SUMMARY_SECTIONS) == "too short"


def test_an_apology_inside_a_real_summary_is_no_refusal():
    reply = "Sorry for the length.\n" + good()
    assert summary_check.problem(reply, "talk", SUMMARY_SECTIONS) == ""


# ---- the retry ----------------------------------------------------------------------


def test_a_refusal_is_asked_for_once_more_and_the_retry_is_used():
    client = _Summarizer("I'm sorry, I can't help with that.", good())
    agent = _agent(client)
    assert agent.compact_history(keep_recent=2) > 0
    assert len(client.prompts) == 2
    assert client.prompts[1].startswith("Your last reply could not be used as the summary: "
                                        "it was a refusal.")
    assert client.prompts[1].endswith(client.prompts[0])
    assert _summary(agent) == good()


def test_two_bad_replies_leave_the_history_as_it_was():
    client = _Summarizer("ok", "I cannot summarise this.")
    agent = _agent(client)
    before = [dict(m) for m in agent.history]
    assert agent.compact_history(keep_recent=2) == 0
    assert agent.history == before and len(client.prompts) == 2


def test_a_good_first_reply_is_one_request():
    client = _Summarizer(good())
    assert _agent(client).compact_history(keep_recent=2) > 0
    assert len(client.prompts) == 1


def test_the_summary_prompt_asks_for_unverified_marks():
    client = _Summarizer(good())
    _agent(client).compact_history(keep_recent=2)
    assert "UNVERIFIED:" in client.prompts[0]


# ---- UNVERIFIED -------------------------------------------------------------------------


def test_a_pass_claim_without_command_output_is_marked():
    client = _Summarizer(good("- fixed the tokenizer\n- all tests pass now"))
    agent = _agent(client)
    agent.compact_history(keep_recent=2)
    text = _summary(agent)
    assert "- UNVERIFIED: all tests pass now" in text
    assert "- fixed the tokenizer" in text  # not a check claim: left to the model


def test_command_output_that_shows_a_pass_keeps_the_claim_as_it_is():
    client = _Summarizer(good("- all tests pass now"))
    agent = _agent(client, ("run_command", "....\n12 passed in 0.31s"))
    agent.compact_history(keep_recent=2)
    assert "- all tests pass now" in _summary(agent)
    assert "UNVERIFIED" not in _summary(agent)


def test_a_files_text_is_no_evidence():
    client = _Summarizer(good("- the tests pass"))
    agent = _agent(client, ("read_file", "# notes: 12 passed in 0.31s"))
    agent.compact_history(keep_recent=2)
    assert "- UNVERIFIED: the tests pass" in _summary(agent)


def test_a_line_the_model_marked_or_the_earlier_summary_held_is_left_alone():
    earlier = good("- ruff is clean")
    text = summary_check.mark_unverified(
        "- ruff is clean\n- UNVERIFIED: the build passes\nThe lint passed.", False,
        earlier.splitlines())
    assert text == "- ruff is clean\n- UNVERIFIED: the build passes\nUNVERIFIED: The lint passed."


@pytest.mark.parametrize("output", [
    "12 passed, 1 skipped", "All checks passed!", "Ran 3 tests in 0.01s\n\nOK",
    "    Finished dev [unoptimized] target(s)", "exit code: 0",
])
def test_what_counts_as_a_pass(output):
    assert summary_check.has_evidence([{"role": "tool", "name": "run_command", "content": output}])


@pytest.mark.parametrize("output", ["1 failed", "ok, done", "FAILED tests/x.py", "success"])
def test_what_does_not(output):
    assert not summary_check.has_evidence(
        [{"role": "tool", "name": "run_command", "content": output}])
