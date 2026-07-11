"""claude_agent backend + shim: message translation, OpenAI-shaped streaming,
tool-call interception, backend registration. Uses a FAKE Agent SDK so these run
without the `claude` CLI or claude-agent-sdk installed (as in CI)."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from evi.llm import claude_agent as ca
from evi.llm.claude_agent import translate_messages


# --- translate_messages (pure, no SDK) ---------------------------------------


def test_translate_system_joined_and_text_blocks():
    sys, msgs = translate_messages([
        {"role": "system", "content": "A"},
        {"role": "system", "content": "B"},
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "yo"},
    ])
    assert sys == "A\n\nB"
    assert msgs[0]["message"] == {"role": "user", "content": [{"type": "text", "text": "hi"}]}
    assert msgs[1]["message"]["role"] == "assistant"
    # content is always a LIST of blocks (the CLI scans it with .some(...))
    assert isinstance(msgs[1]["message"]["content"], list)


def test_translate_tool_history_rendered_as_text():
    sys, msgs = translate_messages([
        {"role": "user", "content": "q"},
        {"role": "assistant", "content": None,
         "tool_calls": [{"id": "t1", "type": "function",
                         "function": {"name": "add", "arguments": '{"a": 2}'}}]},
        {"role": "tool", "tool_call_id": "t1", "content": "5"},
    ])
    asst = next(m for m in msgs if m["message"]["role"] == "assistant")
    assert "[called add(" in asst["message"]["content"][0]["text"]
    user_text = " ".join(
        b["text"] for m in msgs if m["message"]["role"] == "user"
        for b in m["message"]["content"]
    )
    # tool result names the tool via the tool_call_id -> name map
    assert "[tool add returned: 5]" in user_text


def test_translate_coalesces_consecutive_same_role():
    # a tool result (user) then a real user message must merge into one turn
    sys, msgs = translate_messages([
        {"role": "assistant", "content": None,
         "tool_calls": [{"id": "t1", "function": {"name": "x", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "t1", "content": "ok"},
        {"role": "user", "content": "and now?"},
    ])
    user_turns = [m for m in msgs if m["message"]["role"] == "user"]
    assert len(user_turns) == 1  # coalesced
    assert len(user_turns[0]["message"]["content"]) == 2  # result block + question block


def test_translate_list_content_flattened():
    sys, msgs = translate_messages([
        {"role": "user", "content": [{"type": "text", "text": "hello"}, {"type": "text", "text": "world"}]},
    ])
    assert msgs[0]["message"]["content"][0]["text"] == "hello\nworld"


# --- backend registration + model list (no SDK) ------------------------------


def test_backend_registered_in_factory():
    from evi.backends.claude_agent import ClaudeAgentBackend
    from evi.backends.factory import KNOWN_BACKENDS, default_base_url

    assert KNOWN_BACKENDS["claude_agent"] is ClaudeAgentBackend
    assert default_base_url("claude_agent") == ""  # no HTTP endpoint


def test_get_backend_returns_claude_agent():
    from evi.backends import get_backend

    settings = SimpleNamespace(backend="claude_agent", base_url="", api_key="",
                               request_timeout=120.0)
    assert type(get_backend(settings)).__name__ == "ClaudeAgentBackend"


def test_backend_list_models_are_aliases():
    from evi.backends.claude_agent import ClaudeAgentBackend

    b = ClaudeAgentBackend()
    models = b.list_models()
    assert [m.id for m in models] == ["opus", "sonnet", "haiku"]
    assert all(m.backend == "claude_agent" and m.family == "claude" for m in models)
    assert b.supports_pull() is False


def test_unavailable_raises_when_sdk_missing(monkeypatch):
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *a, **k):
        if name == "claude_agent_sdk":
            raise ImportError("not installed")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    with pytest.raises(ca.ClaudeAgentUnavailable):
        ca._import_sdk()


# --- the shim's create() with a FAKE Agent SDK -------------------------------


class _FakeTextBlock:
    def __init__(self, text):
        self.text = text
        self.type = "text"


class _FakeThinkingBlock:
    def __init__(self, thinking):
        self.thinking = thinking


class _FakeAssistantMessage:
    def __init__(self, content):
        self.content = content


class _FakeResultMessage:
    def __init__(self, usage=None, is_error=False, result=""):
        self.usage = usage
        self.is_error = is_error
        self.result = result


class _FakeDeny:
    def __init__(self, message="", interrupt=False):
        self.behavior = "deny"
        self.message = message
        self.interrupt = interrupt


def _fake_sdk(query_fn):
    """Assemble a stand-in `claude_agent_sdk` module namespace."""
    return SimpleNamespace(
        AssistantMessage=_FakeAssistantMessage,
        TextBlock=_FakeTextBlock,
        ThinkingBlock=_FakeThinkingBlock,
        ResultMessage=_FakeResultMessage,
        PermissionResultDeny=_FakeDeny,
        ClaudeAgentOptions=lambda **kw: SimpleNamespace(**kw),
        query=query_fn,
        tool=lambda name, desc, schema: (lambda fn: SimpleNamespace(name=name)),
        create_sdk_mcp_server=lambda name, version, tools: SimpleNamespace(name=name, tools=tools),
    )


def _client_with(monkeypatch, query_fn):
    monkeypatch.setattr(ca, "_import_sdk", lambda: _fake_sdk(query_fn))
    return ca.ClaudeAgentClient()


def test_create_stream_text_shape(monkeypatch):
    async def q(*, prompt, options):
        # drain the prompt iterable (the shim feeds history through it)
        async for _ in prompt:
            pass
        yield _FakeAssistantMessage([_FakeThinkingBlock("reasoning"), _FakeTextBlock("hello there")])
        yield _FakeResultMessage(usage={"input_tokens": 7, "output_tokens": 3}, is_error=False)

    client = _client_with(monkeypatch, q)
    chunks = list(client.chat.completions.create(
        model="haiku",
        messages=[{"role": "system", "content": "s"}, {"role": "user", "content": "hi"}],
        stream=True,
    ))
    content = "".join(
        c.choices[0].delta.content for c in chunks
        if c.choices and c.choices[0].delta and c.choices[0].delta.content
    )
    assert "<think>reasoning</think>" in content  # thinking wrapped for the ThinkParser
    assert "hello there" in content
    finishes = [c.choices[0].finish_reason for c in chunks if c.choices]
    assert "stop" in finishes
    usage = next(c.usage for c in chunks if getattr(c, "usage", None) is not None)
    assert usage.prompt_tokens == 7 and usage.completion_tokens == 3 and usage.total_tokens == 10


def test_create_stream_tool_interception(monkeypatch):
    async def q(*, prompt, options):
        async for _ in prompt:
            pass
        # Simulate Claude requesting the tool: the SDK consults can_use_tool,
        # which captures + denies + interrupts (raises), as the real CLI does.
        ctx = SimpleNamespace(tool_use_id="toolu_abc")
        await options.can_use_tool("mcp__evi__add", {"a": 1, "b": 2}, ctx)
        raise RuntimeError("interrupted by deny")
        yield  # pragma: no cover - unreachable, makes this an async generator

    client = _client_with(monkeypatch, q)
    tools = [{"type": "function", "function": {
        "name": "add", "description": "add", "parameters": {"type": "object", "properties": {}}}}]
    chunks = list(client.chat.completions.create(
        model="haiku", messages=[{"role": "user", "content": "add 1 and 2"}],
        tools=tools, stream=True,
    ))
    calls = [tc for c in chunks if c.choices and c.choices[0].delta and c.choices[0].delta.tool_calls
             for tc in c.choices[0].delta.tool_calls]
    assert len(calls) == 1
    assert calls[0].function.name == "add"  # unwrapped from mcp__evi__add
    assert calls[0].id == "toolu_abc"
    assert json.loads(calls[0].function.arguments) == {"a": 1, "b": 2}
    finishes = [c.choices[0].finish_reason for c in chunks if c.choices]
    assert "tool_calls" in finishes


def test_create_non_stream_returns_choices(monkeypatch):
    async def q(*, prompt, options):
        async for _ in prompt:
            pass
        yield _FakeAssistantMessage([_FakeTextBlock("the answer")])
        yield _FakeResultMessage(usage={"input_tokens": 2, "output_tokens": 1})

    client = _client_with(monkeypatch, q)
    resp = client.chat.completions.create(
        model="haiku", messages=[{"role": "user", "content": "q"}], stream=False,
    )
    assert resp.choices[0].message.content == "the answer"
    assert resp.choices[0].message.role == "assistant"
    assert resp.usage.prompt_tokens == 2


def test_create_error_result_propagates(monkeypatch):
    async def q(*, prompt, options):
        async for _ in prompt:
            pass
        yield _FakeResultMessage(is_error=True, result="boom")

    client = _client_with(monkeypatch, q)
    with pytest.raises(RuntimeError):
        list(client.chat.completions.create(
            model="haiku", messages=[{"role": "user", "content": "q"}], stream=True,
        ))
