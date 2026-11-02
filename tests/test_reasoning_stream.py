"""The native reasoning channel in the agent loop: reasoning a server sends
beside `content` (`reasoning_content` / `reasoning`) is shown as thinking and
kept out of the answer, the history, the transcript and later requests."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from openai.types.chat import ChatCompletionChunk

from evi.config import Config
from evi.llm.agent import Agent, Done, Error, TextDelta, ThinkingDelta, ToolCall, ToolResult
from evi.tools.base import Tool


def _sdk_chunk(delta: dict, finish: str | None = None) -> ChatCompletionChunk:
    # A chunk exactly as the openai SDK builds it from a server's SSE line.
    return ChatCompletionChunk.model_validate({
        "id": "c", "object": "chat.completion.chunk", "created": 0, "model": "m",
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
    })


def _ns_chunk(finish: str | None = None, **delta) -> SimpleNamespace:
    # A duck-typed chunk, as evi.llm.cli_agent.delta_chunk builds them.
    fields = {"content": None, "tool_calls": None, **delta}
    return SimpleNamespace(choices=[SimpleNamespace(
        delta=SimpleNamespace(**fields), finish_reason=finish, logprobs=None)], usage=None)


def _thinking(events) -> str:
    return "".join(e.text for e in events if isinstance(e, ThinkingDelta))


def _text(events) -> str:
    return "".join(e.text for e in events if isinstance(e, TextDelta))


def _echo_tool() -> Tool:
    return Tool(
        name="echo", description="echo the message back",
        parameters={"type": "object", "properties": {"msg": {"type": "string"}},
                    "required": ["msg"]},
        func=lambda msg: msg,
    )


def test_reasoning_content_from_the_sdk_is_thinking_not_answer(fake_llm):
    client = fake_llm([[
        _sdk_chunk({"role": "assistant", "reasoning_content": "six "}),
        _sdk_chunk({"reasoning_content": "times seven"}),
        _sdk_chunk({"content": "It is "}),
        _sdk_chunk({"content": "42."}, finish="stop"),
    ]])
    agent = Agent(client=client, config=Config(), tools=[])
    events = list(agent.chat("what is 6*7?"))
    assert _thinking(events) == "six times seven"
    assert _text(events) == "It is 42."
    assert events[-1] == Done("stop")
    # The history keeps only the answer: no reasoning key, no reasoning text.
    assert agent.history[-1] == {"role": "assistant", "content": "It is 42."}


def test_reasoning_field_is_thinking_and_never_goes_back_to_the_server(fake_llm):
    # vLLM 0.16+ / Ollama /v1: `reasoning`, interleaved with a tool call.
    call = SimpleNamespace(index=0, id="call_0", type="function",
                           function=SimpleNamespace(name="echo", arguments='{"msg": "hi"}'))
    client = fake_llm([
        [_ns_chunk(reasoning="I should echo. "),
         _ns_chunk(tool_calls=[call]),
         _ns_chunk(reasoning="SECRET-REASONING-1", finish="tool_calls")],
        [_ns_chunk(reasoning="SECRET-REASONING-2"),
         _ns_chunk(content="done", finish="stop")],
        [_ns_chunk(content="ok", finish="stop")],
    ])
    agent = Agent(client=client, config=Config(), tools=[_echo_tool()],
                  unattended_policy="allow")
    logged: list[tuple] = []
    agent._log_to_transcript = lambda role, content, **kw: logged.append((role, content))
    events = list(agent.chat("say hi"))
    assert _thinking(events) == "I should echo. SECRET-REASONING-1SECRET-REASONING-2"
    assert _text(events) == "done"
    assert [e.name for e in events if isinstance(e, ToolCall)] == ["echo"]
    assert [e.output for e in events if isinstance(e, ToolResult)] == ["hi"]
    assert [m["role"] for m in agent.history] == ["system", "user", "assistant", "tool",
                                                   "assistant"]
    assert agent.history[2]["content"] == ""
    assert agent.history[-1] == {"role": "assistant", "content": "done"}
    assert "SECRET-REASONING" not in json.dumps(agent.history)
    assert all("SECRET-REASONING" not in c for _, c in logged)
    # The next turn's request carries the history without any reasoning.
    list(agent.chat("again"))
    sent = json.dumps(client.requests[-1]["messages"])
    assert "SECRET-REASONING" not in sent and "I should echo" not in sent
    assert "reasoning" not in {k for m in client.requests[-1]["messages"] for k in m}


def test_a_literal_think_tag_in_reasoning_does_not_leak_into_the_answer(fake_llm):
    # Reasoning is never fed to the tag parser: `</think>` in it is just text.
    client = fake_llm([[
        _ns_chunk(reasoning="the parser looks for </think> and then "),
        _ns_chunk(reasoning="<think> again"),
        _ns_chunk(content="Answer.", finish="stop"),
    ]])
    agent = Agent(client=client, config=Config(), tools=[])
    events = list(agent.chat("q"))
    assert _thinking(events) == "the parser looks for </think> and then <think> again"
    assert _text(events) == "Answer."
    assert agent.history[-1]["content"] == "Answer."


def test_a_reasoning_only_final_chunk_is_thinking_and_an_empty_answer(fake_llm):
    client = fake_llm([[
        _sdk_chunk({"reasoning": "ran out of tokens while"}),
        _sdk_chunk({"reasoning": " thinking"}, finish="length"),
    ]])
    agent = Agent(client=client, config=Config(), tools=[])
    events = list(agent.chat("q"))
    assert _thinking(events) == "ran out of tokens while thinking"
    assert _text(events) == ""
    assert agent.history[-1] == {"role": "assistant", "content": ""}


def test_both_names_in_one_delta_show_the_text_once(fake_llm):
    # OpenRouter sends `reasoning_content` as an alias of `reasoning`.
    client = fake_llm([[
        _sdk_chunk({"reasoning": "once", "reasoning_content": "once"}),
        _sdk_chunk({"content": "A"}, finish="stop"),
    ]])
    events = list(Agent(client=client, config=Config(), tools=[]).chat("q"))
    assert _thinking(events) == "once"


def test_reasoning_and_content_in_one_delta_are_both_kept_apart(fake_llm):
    # The content is longer than the tag parser's holdback, so its TextDelta
    # comes out of this delta (not the end-of-stream flush) and the order
    # check means something.
    client = fake_llm([[
        _sdk_chunk({"reasoning_content": "R", "content": "The answer is C."}),
        _sdk_chunk({"content": ""}, finish="stop"),
    ]])
    agent = Agent(client=client, config=Config(), tools=[])
    events = list(agent.chat("q"))
    shown = [e for e in events if isinstance(e, (TextDelta, ThinkingDelta))]
    assert isinstance(shown[0], ThinkingDelta) and isinstance(shown[1], TextDelta)
    assert _thinking(events) == "R" and _text(events) == "The answer is C."
    assert agent.history[-1]["content"] == "The answer is C."


def test_reasoning_beside_a_tool_call_in_one_sdk_delta_is_thinking(fake_llm):
    # Servers send the last reasoning piece with the first tool-call delta;
    # the SDK keeps `reasoning` in model_extra beside the declared tool_calls.
    client = fake_llm([
        [_sdk_chunk({"role": "assistant", "reasoning": "call echo", "tool_calls": [
            {"index": 0, "id": "call_0", "type": "function",
             "function": {"name": "echo", "arguments": '{"msg": "hi"}'}}]},
            finish="tool_calls")],
        [_sdk_chunk({"content": "done"}, finish="stop")],
    ])
    agent = Agent(client=client, config=Config(), tools=[_echo_tool()],
                  unattended_policy="allow")
    events = list(agent.chat("q"))
    assert _thinking(events) == "call echo"
    assert [e.output for e in events if isinstance(e, ToolResult)] == ["hi"]
    assert "call echo" not in json.dumps(agent.history)


def test_a_tool_call_written_as_text_after_reasoning_still_runs(fake_llm):
    client = fake_llm([
        [_ns_chunk(reasoning="I will echo"),
         _ns_chunk(content='{"name": "echo", "arguments": {"msg": "hi"}}', finish="stop")],
        [_ns_chunk(content="done", finish="stop")],
    ])
    agent = Agent(client=client, config=Config(), tools=[_echo_tool()],
                  unattended_policy="allow")
    events = list(agent.chat("q"))
    assert [e.output for e in events if isinstance(e, ToolResult)] == ["hi"]
    assert _thinking(events) == "I will echo"
    assert '"name"' not in _text(events) and _text(events) == "done"
    assert "I will echo" not in json.dumps(agent.history)


def test_a_stream_that_breaks_after_reasoning_only_is_an_error_not_a_reply(fake_llm):
    # Reasoning is not part of a reply: a stream cut off after it must not
    # invent an (empty) assistant message.
    def broken(**_kw):
        yield _ns_chunk(reasoning="half a thought")
        raise ConnectionError("connection dropped")

    client = fake_llm([])
    client.chat.completions.create = broken
    agent = Agent(client=client, config=Config(), tools=[])
    events = list(agent.chat("q"))
    assert _thinking(events) == "half a thought"
    assert any(isinstance(e, Error) for e in events)
    assert [m["role"] for m in agent.history] == ["system", "user"]


def test_think_tags_beside_native_reasoning_are_stripped_but_not_shown_twice(fake_llm):
    # llama.cpp --reasoning-format deepseek-legacy sends the thinking in
    # reasoning_content AND as <think> tags in content, a chunk at a time.
    client = fake_llm([[
        _ns_chunk(reasoning_content="six ", content="<think>six "),
        _ns_chunk(reasoning_content="sevens", content="sev"),
        _ns_chunk(content="ens</th"),
        _ns_chunk(content="ink>It is 42.", finish="stop"),
    ]])
    agent = Agent(client=client, config=Config(), tools=[])
    events = list(agent.chat("q"))
    assert _thinking(events) == "six sevens"
    assert _text(events) == "It is 42."
    assert agent.history[-1]["content"] == "It is 42."


def test_an_unclosed_think_tag_shows_only_what_native_reasoning_lacks(fake_llm):
    # The parser holds a tag's tail back and flushes it at the end; the part
    # repeating the native reasoning is dropped, the rest still shown.
    client = fake_llm([[
        _ns_chunk(reasoning="ponder", content="<think>ponder"),
        _ns_chunk(content=" more", finish="length"),
    ]])
    agent = Agent(client=client, config=Config(), tools=[])
    events = list(agent.chat("q"))
    assert _thinking(events) == "ponder more"
    assert _text(events) == ""


@pytest.mark.parametrize("content,text,tag_thinking", [
    # A literal tag in the answer (not a copy of the reasoning) is shown as
    # thinking, as without native reasoning, never dropped from view.
    (["Reasoning models wrap thoughts in <think> tags; ", "eVi hides them."],
     "Reasoning models wrap thoughts in ", " tags; eVi hides them."),
    (["Write <think>your plan</think> before the answer."],
     "Write  before the answer.", "your plan"),
])
def test_a_literal_think_tag_after_native_reasoning_is_still_shown(fake_llm, content, text,
                                                                   tag_thinking):
    client = fake_llm([[_ns_chunk(reasoning="about tags"),
                        *[_ns_chunk(content=c) for c in content],
                        _ns_chunk(finish="stop")]])
    events = list(Agent(client=client, config=Config(), tools=[]).chat("q"))
    assert _thinking(events) == "about tags" + tag_thinking
    assert _text(events) == text


def test_tag_text_held_back_when_native_reasoning_starts_is_still_shown(fake_llm):
    # The parser holds the tail of an open block ("fghijkl") until the next
    # chunk, which brings the first native piece: that tail is no copy of
    # it, so it is shown.
    client = fake_llm([[
        _ns_chunk(content="<think>abcdefghijkl"),
        _ns_chunk(reasoning_content="xyz", content="</think>Answer here"),
        _ns_chunk(finish="stop"),
    ]])
    events = list(Agent(client=client, config=Config(), tools=[]).chat("q"))
    assert _thinking(events) == "abcdexyzfghijkl"
    assert _text(events) == "Answer here"


def test_think_tags_before_any_native_reasoning_are_still_shown(fake_llm):
    # Tag text is dropped only where it repeats native reasoning already seen.
    client = fake_llm([[
        _ns_chunk(content="<think>tagged</think>"),
        _ns_chunk(reasoning="native"),
        _ns_chunk(content="visible", finish="stop"),
    ]])
    agent = Agent(client=client, config=Config(), tools=[])
    events = list(agent.chat("q"))
    assert _thinking(events) == "taggednative"
    assert _text(events) == "visible"


def test_each_reply_decides_on_its_own_whether_tags_are_shown(fake_llm):
    # A tool round: the first reply has native reasoning, the next one only
    # <think> tags, which are shown (the dedup is per streamed reply).
    call = SimpleNamespace(index=0, id="call_0", type="function",
                           function=SimpleNamespace(name="echo", arguments='{"msg": "hi"}'))
    client = fake_llm([
        [_ns_chunk(reasoning="call echo"), _ns_chunk(tool_calls=[call], finish="tool_calls")],
        [_ns_chunk(content="<think>it echoed</think>done", finish="stop")],
    ])
    agent = Agent(client=client, config=Config(), tools=[_echo_tool()],
                  unattended_policy="allow")
    events = list(agent.chat("q"))
    assert _thinking(events) == "call echoit echoed"
    assert _text(events) == "done"


def test_a_held_json_answer_after_reasoning_is_still_shown_once(fake_llm):
    # A reply leading with `{` is held to the end (it may be a tool call as
    # text); reasoning before it streams as thinking meanwhile.
    client = fake_llm([[
        _ns_chunk(reasoning="the user wants JSON"),
        _ns_chunk(content='{"a": 1}', finish="stop"),
    ]])
    agent = Agent(client=client, config=Config(), tools=[])
    events = list(agent.chat("give json"))
    assert _thinking(events) == "the user wants JSON"
    assert _text(events) == '{"a": 1}'
    assert agent.history[-1]["content"] == '{"a": 1}'


def test_a_stream_without_reasoning_fields_is_unchanged(fake_llm):
    client = fake_llm([fake_llm.text("plain")])
    agent = Agent(client=client, config=Config(), tools=[])
    events = list(agent.chat("q"))
    assert _thinking(events) == "" and _text(events) == "plain"


def test_a_real_openai_client_over_sse_carries_the_reasoning():
    # End to end through the SDK's own SSE parsing (no fake chunk objects): a
    # vLLM-style stream with `reasoning`, then an older-style one with
    # `reasoning_content`.
    import httpx
    import openai

    def sse(*deltas):
        lines = []
        for i, d in enumerate(deltas):
            finish = "stop" if i == len(deltas) - 1 else None
            lines.append("data: " + json.dumps({
                "id": "c", "object": "chat.completion.chunk", "created": 0, "model": "m",
                "choices": [{"index": 0, "delta": d, "finish_reason": finish}]}))
        return ("\n\n".join(lines) + "\n\ndata: [DONE]\n\n").encode()

    bodies = [sse({"role": "assistant", "reasoning": "vllm "}, {"reasoning": "thinks"},
                  {"content": "V"}),
              sse({"reasoning_content": "lmstudio thinks"}, {"content": "L"})]

    def handler(request):
        return httpx.Response(200, content=bodies.pop(0),
                              headers={"content-type": "text/event-stream"})

    client = openai.OpenAI(base_url="http://vllm.invalid/v1", api_key="x",
                           http_client=httpx.Client(transport=httpx.MockTransport(handler)))
    agent = Agent(client=client, config=Config(), tools=[])
    first = list(agent.chat("q1"))
    assert _thinking(first) == "vllm thinks" and _text(first) == "V"
    second = list(agent.chat("q2"))
    assert _thinking(second) == "lmstudio thinks" and _text(second) == "L"
    assert [m["content"] for m in agent.history if m["role"] == "assistant"] == ["V", "L"]
