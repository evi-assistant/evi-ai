"""Tests for the opt-in Responses API path (Phase 55): chat<->responses
conversion and the stream adapter that re-emits Responses events as
Chat-Completion-shaped chunks. No live endpoint — synthetic events modeled on
the SDK's `.type` discriminators."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from evi.llm import responses as R  # noqa: E402


def ev(type_, **kw):
    return SimpleNamespace(type=type_, **kw)


# --- request conversion --------------------------------------------------


def test_as_text_handles_str_list_none():
    assert R._as_text("hi") == "hi"
    assert R._as_text([{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]) == "ab"
    assert R._as_text(None) == ""


def test_to_responses_tools_flattens_function():
    chat = [{"type": "function", "function": {"name": "f", "description": "d", "parameters": {"type": "object"}}}]
    assert R.to_responses_tools(chat) == [
        {"type": "function", "name": "f", "description": "d", "parameters": {"type": "object"}}
    ]
    assert R.to_responses_tools(None) == []


def test_to_responses_input_text_passthrough():
    msgs = [{"role": "system", "content": "sys"}, {"role": "user", "content": "hi"}]
    assert R.to_responses_input(msgs) == [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "hi"},
    ]


def test_to_responses_input_tool_calls_and_results():
    msgs = [
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "call_1", "function": {"name": "git_status", "arguments": "{}"}},
        ]},
        {"role": "tool", "tool_call_id": "call_1", "content": "clean"},
    ]
    out = R.to_responses_input(msgs)
    assert out[0] == {"type": "function_call", "call_id": "call_1", "name": "git_status", "arguments": "{}"}
    assert out[1] == {"type": "function_call_output", "call_id": "call_1", "output": "clean"}


# --- stream adaptation ---------------------------------------------------


def test_adapt_text_then_completed_stop():
    events = [
        ev("response.output_text.delta", delta="Hel"),
        ev("response.output_text.delta", delta="lo"),
        ev("response.completed", response=SimpleNamespace(usage=SimpleNamespace(input_tokens=3, output_tokens=2, total_tokens=5))),
    ]
    chunks = list(R.adapt_responses_stream(events))
    assert chunks[0].choices[0].delta.content == "Hel"
    assert chunks[1].choices[0].delta.content == "lo"
    # finish chunk (stop) then usage chunk
    assert chunks[2].choices[0].finish_reason == "stop"
    assert chunks[3].choices == []
    assert chunks[3].usage.prompt_tokens == 3
    assert chunks[3].usage.completion_tokens == 2
    assert chunks[3].usage.total_tokens == 5


def test_adapt_function_call_stream():
    events = [
        ev("response.output_item.added", output_index=0,
           item=SimpleNamespace(type="function_call", call_id="call_9", name="git_log")),
        ev("response.function_call_arguments.delta", output_index=0, delta='{"n":'),
        ev("response.function_call_arguments.delta", output_index=0, delta='3}'),
        ev("response.completed", response=SimpleNamespace(usage=None)),
    ]
    chunks = list(R.adapt_responses_stream(events))
    added = chunks[0].choices[0].delta.tool_calls[0]
    assert added.index == 0 and added.id == "call_9" and added.function.name == "git_log"
    assert chunks[1].choices[0].delta.tool_calls[0].function.arguments == '{"n":'
    assert chunks[2].choices[0].delta.tool_calls[0].function.arguments == "3}"
    # function calls present -> finish_reason tool_calls; usage None -> no usage chunk
    assert chunks[3].choices[0].finish_reason == "tool_calls"
    assert all(getattr(c, "usage", None) is None for c in chunks)


def test_adapt_failed_event_finishes():
    chunks = list(R.adapt_responses_stream([ev("response.failed")]))
    assert chunks[0].choices[0].finish_reason == "stop"


# --- end-to-end wrapper (mocked client) ---------------------------------


class _FakeResponses:
    def __init__(self):
        self.kwargs = None

    def create(self, **kw):
        self.kwargs = kw
        return iter([
            ev("response.output_text.delta", delta="hi"),
            ev("response.completed", response=SimpleNamespace(usage=None)),
        ])


class _FakeClient:
    def __init__(self):
        self.responses = _FakeResponses()


def test_stream_chat_via_responses_maps_kwargs_and_ignores_chat_only():
    client = _FakeClient()
    chunks = list(R.stream_chat_via_responses(
        client,
        model="gpt-x",
        messages=[{"role": "user", "content": "hi"}],
        tools=[{"type": "function", "function": {"name": "f", "parameters": {}}}],
        temperature=0.5,
        max_tokens=100,
        # chat-only kwargs that must be tolerated/ignored:
        stream=True, tool_choice="auto", stream_options={"include_usage": True},
        top_p=0.9,
    ))
    kw = client.responses.kwargs
    assert kw["model"] == "gpt-x"
    assert kw["input"] == [{"role": "user", "content": "hi"}]
    assert kw["tools"][0]["name"] == "f"
    assert kw["max_output_tokens"] == 100
    assert kw["stream"] is True
    assert chunks[0].choices[0].delta.content == "hi"


def test_builtin_tool_spec():
    assert R.builtin_tool_spec("web_search") == {"type": "web_search"}
    ci = R.builtin_tool_spec("code_interpreter")
    assert ci["type"] == "code_interpreter" and ci["container"] == {"type": "auto"}


def test_stream_includes_builtin_tools():
    client = _FakeClient()
    list(R.stream_chat_via_responses(
        client,
        model="gpt-x",
        messages=[{"role": "user", "content": "hi"}],
        tools=[{"type": "function", "function": {"name": "f", "parameters": {}}}],
        builtin_tools=["web_search", "code_interpreter"],
    ))
    types = [t.get("type") for t in client.responses.kwargs["tools"]]
    assert "function" in types and "web_search" in types and "code_interpreter" in types


def test_builtin_tools_only_no_function_tools():
    client = _FakeClient()
    list(R.stream_chat_via_responses(
        client, model="gpt-x", messages=[{"role": "user", "content": "hi"}],
        builtin_tools=["web_search"],
    ))
    assert client.responses.kwargs["tools"] == [{"type": "web_search"}]
