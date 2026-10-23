"""The agent loop repairs each call once, before anything checks or runs it
(Agent._prepare_call -> Tool.prepare): the history, the permission prompt,
the policy and the tool all see the repaired arguments."""

from __future__ import annotations

import json

import evi.tools.fs  # noqa: F401  registers read_file and write_file
from evi.config import Config
from evi.llm.agent import Agent, ToolResult
from evi.policy import implementer_policy
from evi.tools.base import REGISTRY
from tests.conftest import llm_call, llm_text


def _agent(client, work, **kw) -> Agent:
    agent = Agent(client=client, config=Config(), transcripts=None,
                  tools=[REGISTRY[n] for n in ("read_file", "write_file")], **kw)
    agent.cwd = str(work)
    return agent


def _results(agent: Agent) -> list[str]:
    return [e.output for e in agent.chat("go", max_turns=4) if isinstance(e, ToolResult)]


def _sent_arguments(agent: Agent) -> list[str]:
    return [c["function"]["arguments"] for m in agent.history if m.get("role") == "assistant"
            for c in m.get("tool_calls") or []]


def test_a_number_sent_as_text_is_coerced_and_the_result_says_so(fake_llm, tmp_path):
    (tmp_path / "f.txt").write_text("one\ntwo\nthree\nfour\n", encoding="utf-8")
    agent = _agent(fake_llm([
        llm_call(("read_file", {"path": "f.txt", "offset": "3", "limit": "1"})), llm_text("ok"),
    ]), tmp_path, unattended_policy="allow")
    [out] = _results(agent)
    assert "three" in out and "two" not in out and "four" not in out
    assert out.endswith('(eVi repaired this call: offset: "3" -> 3; limit: "1" -> 1.)')
    # The history holds the call as it ran.
    assert json.loads(_sent_arguments(agent)[0]) == {"path": "f.txt", "offset": 3, "limit": 1}


def test_a_call_that_needs_no_repair_keeps_its_exact_text(fake_llm, tmp_path):
    (tmp_path / "f.txt").write_text("x\n", encoding="utf-8")
    raw = '{"path":   "f.txt"}'
    agent = _agent(fake_llm([llm_call(("read_file", raw)), llm_text("ok")]), tmp_path,
                   unattended_policy="allow")
    [out] = _results(agent)
    assert "repaired" not in out
    assert _sent_arguments(agent) == [raw]  # byte-stable for the prompt cache


def test_the_permission_prompt_is_asked_about_the_repaired_arguments(fake_llm, tmp_path):
    asked: list[dict] = []

    def approve(name, args, category):
        asked.append(json.loads(args))
        return True

    agent = _agent(fake_llm([
        llm_call(("write_file", {"file_path": "new.txt", "content": "hi\n"})), llm_text("ok"),
    ]), tmp_path)
    agent.auto_approve_categories = set()  # so the write is asked about
    agent.permission_callback = approve
    [out] = _results(agent)
    assert asked == [{"path": "new.txt", "content": "hi\n"}]
    assert out.startswith("wrote") and "file_path -> path" in out
    assert (tmp_path / "new.txt").read_text(encoding="utf-8") == "hi\n"


def test_the_policy_judges_the_path_a_malformed_call_names(fake_llm, tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    agent = _agent(fake_llm([
        llm_call(("write_file", "{'path': '../outside.txt', 'content': 'x'}")), llm_text("ok"),
    ]), work)
    agent.policy = implementer_policy(work)
    [out] = _results(agent)
    assert not (tmp_path / "outside.txt").exists()
    # Refused by the policy for its path, not for its quoting.
    assert "not a JSON object" not in out and "JSONDecodeError" not in out
    assert "outside.txt" in out


def test_a_call_that_cant_be_used_is_answered_with_how_to_call_the_tool(fake_llm, tmp_path):
    agent = _agent(fake_llm([
        llm_call(("write_file", {"path": "a.txt"})),
        llm_call(("write_file", '{"path": "b.txt", "content": "half')),
        llm_text("ok"),
    ]), tmp_path, unattended_policy="allow")
    missing, cut = _results(agent)
    assert missing.startswith("ERROR: write_file needs content. Call it as write_file(")
    # Cut off mid-content: never written as if whole, and kept as valid JSON.
    assert cut.startswith("ERROR: the arguments to write_file are not a JSON object")
    assert not (tmp_path / "a.txt").exists() and not (tmp_path / "b.txt").exists()
    assert _sent_arguments(agent) == ['{"path": "a.txt"}', "{}"]


def test_a_cut_off_read_is_closed_and_runs(fake_llm, tmp_path):
    (tmp_path / "f.txt").write_text("hello\n", encoding="utf-8")
    agent = _agent(fake_llm([llm_call(("read_file", '{"path": "f.txt"')), llm_text("ok")]),
                   tmp_path, unattended_policy="allow")
    [out] = _results(agent)
    assert "hello" in out and "closed arguments that were cut off" in out


def test_a_tagged_text_call_gets_the_types_its_tool_takes(fake_llm, tmp_path):
    # Qwen3-Coder's tags carry every value as text; prepare converts offset to 5.
    (tmp_path / "f.txt").write_text("".join(f"line {i}\n" for i in range(1, 8)), encoding="utf-8")
    text = ("<tool_call>\n<function=read_file>\n<parameter=path>\nf.txt\n</parameter>\n"
            "<parameter=offset>\n5\n</parameter>\n</function>\n</tool_call>")
    agent = _agent(fake_llm([llm_text(text), llm_text("ok")]), tmp_path,
                   unattended_policy="allow")
    [out] = _results(agent)
    assert "line 5" in out and "line 4" not in out
    assert 'offset: "5" -> 5' in out
