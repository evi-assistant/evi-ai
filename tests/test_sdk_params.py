"""Tests for Phase 35: remaining OpenAI SDK pass-through params.

parallel_tool_calls, max_completion_tokens, logit_bias, and n-best-of
(`Agent.complete_variants`).
"""

from __future__ import annotations

from typing import Any

from evi.config import Config
from evi.llm.agent import Agent


# ---- shared fakes -------------------------------------------------------


class _Delta:
    def __init__(self, content="", tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls


class _Choice:
    def __init__(self, content="", finish="stop"):
        self.delta = _Delta(content=content)
        self.finish_reason = finish


class _Chunk:
    def __init__(self, choices=None, usage=None):
        self.choices = choices or []
        self.usage = usage


class _CapturingCompletions:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def create(self, **kwargs):
        self.calls.append(dict(kwargs))
        if not kwargs.get("stream", False):
            # Non-streaming (complete_variants) — return a plain response.
            return self._nonstream(kwargs.get("n", 1))
        return iter([_Chunk(choices=[_Choice(content="ok", finish="stop")])])

    @staticmethod
    def _nonstream(n: int):
        class _Msg:
            def __init__(self, c):
                self.content = c

        class _Ch:
            def __init__(self, c):
                self.message = _Msg(c)

        class _Resp:
            def __init__(self, n):
                self.choices = [_Ch(f"variant {i+1}") for i in range(n)]

        return _Resp(n)


def _make_agent(cfg: Config | None = None, tools=None) -> tuple[Agent, _CapturingCompletions]:
    cc = _CapturingCompletions()
    client = type("C", (), {"chat": type("X", (), {"completions": cc})()})()
    return Agent(client=client, config=cfg or Config(), tools=tools or []), cc


# A tool so requests actually carry a `tools` array.
class _NoopTool:
    name = "noop"
    description = "no-op"
    category = "fs"

    def openai_schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {"type": "object", "properties": {}},
            },
        }

    def call(self, args_json: str) -> str:
        return "ok"


# ---- parallel_tool_calls ------------------------------------------------


def test_parallel_tool_calls_default_omitted() -> None:
    """Default True = don't send the flag at all."""
    agent, cc = _make_agent(tools=[_NoopTool()])
    list(agent.chat("hi"))
    assert "parallel_tool_calls" not in cc.calls[0]


def test_parallel_tool_calls_false_from_config_forwarded() -> None:
    cfg = Config()
    cfg.llm.parallel_tool_calls = False
    agent, cc = _make_agent(cfg, tools=[_NoopTool()])
    list(agent.chat("hi"))
    assert cc.calls[0]["parallel_tool_calls"] is False


def test_parallel_tool_calls_false_dropped_when_no_tools() -> None:
    """No tools in the request → the flag is meaningless, don't send it."""
    cfg = Config()
    cfg.llm.parallel_tool_calls = False
    agent, cc = _make_agent(cfg, tools=[])  # no tools
    list(agent.chat("hi"))
    assert "parallel_tool_calls" not in cc.calls[0]


def test_parallel_tool_calls_per_turn_override_wins() -> None:
    """Per-turn False beats config True."""
    agent, cc = _make_agent(tools=[_NoopTool()])  # config default True
    list(agent.chat("hi", parallel_tool_calls=False))
    assert cc.calls[0]["parallel_tool_calls"] is False


# ---- max_completion_tokens ---------------------------------------------


def test_max_tokens_sent_by_default() -> None:
    agent, cc = _make_agent()
    list(agent.chat("hi"))
    assert "max_tokens" in cc.calls[0]
    assert "max_completion_tokens" not in cc.calls[0]


def test_max_completion_tokens_replaces_max_tokens() -> None:
    cfg = Config()
    cfg.llm.max_completion_tokens = 2048
    agent, cc = _make_agent(cfg)
    list(agent.chat("hi"))
    assert cc.calls[0]["max_completion_tokens"] == 2048
    assert "max_tokens" not in cc.calls[0]


# ---- logit_bias ---------------------------------------------------------


def test_logit_bias_default_omitted() -> None:
    agent, cc = _make_agent()
    list(agent.chat("hi"))
    assert "logit_bias" not in cc.calls[0]


def test_logit_bias_from_config_json_parsed_and_clamped() -> None:
    cfg = Config()
    cfg.llm.logit_bias = '{"123": -100, "456": 999, "789": -999}'
    agent, cc = _make_agent(cfg)
    list(agent.chat("hi"))
    bias = cc.calls[0]["logit_bias"]
    assert bias["123"] == -100.0
    assert bias["456"] == 100.0   # clamped from 999
    assert bias["789"] == -100.0  # clamped from -999


def test_logit_bias_invalid_json_ignored() -> None:
    cfg = Config()
    cfg.llm.logit_bias = "not json {{"
    agent, cc = _make_agent(cfg)
    list(agent.chat("hi"))
    assert "logit_bias" not in cc.calls[0]


def test_logit_bias_per_turn_override() -> None:
    agent, cc = _make_agent()
    list(agent.chat("hi", logit_bias={"42": 10}))
    assert cc.calls[0]["logit_bias"] == {"42": 10}


def test_config_logit_bias_helper_empty() -> None:
    agent, _cc = _make_agent()
    assert agent._config_logit_bias() is None


# ---- n-best-of: complete_variants --------------------------------------


def test_complete_variants_returns_list() -> None:
    agent, cc = _make_agent()
    out = agent.complete_variants("commit message", n=3)
    assert out == ["variant 1", "variant 2", "variant 3"]
    # Non-streaming call with n forwarded.
    call = cc.calls[0]
    assert call["n"] == 3
    assert call["stream"] is False


def test_complete_variants_does_not_touch_history() -> None:
    agent, _cc = _make_agent()
    before = list(agent.history)
    agent.complete_variants("x", n=2)
    assert agent.history == before  # stateless


def test_complete_variants_clamps_n_minimum() -> None:
    agent, cc = _make_agent()
    agent.complete_variants("x", n=0)
    assert cc.calls[0]["n"] == 1


def test_complete_variants_uses_max_completion_tokens_when_set() -> None:
    cfg = Config()
    cfg.llm.max_completion_tokens = 512
    agent, cc = _make_agent(cfg)
    agent.complete_variants("x", n=1)
    assert cc.calls[0]["max_completion_tokens"] == 512
    assert "max_tokens" not in cc.calls[0]


# ---- web ChatRequest ----------------------------------------------------


def test_chat_request_accepts_new_fields() -> None:
    import pytest

    pytest.importorskip("fastapi")
    from evi.apps.web.server import ChatRequest

    req = ChatRequest(
        session_id="s",
        message="hi",
        parallel_tool_calls=False,
        logit_bias={"1": -5},
    )
    assert req.parallel_tool_calls is False
    assert req.logit_bias == {"1": -5}


def test_chat_request_new_fields_optional() -> None:
    import pytest

    pytest.importorskip("fastapi")
    from evi.apps.web.server import ChatRequest

    req = ChatRequest(session_id="s", message="hi")
    assert req.parallel_tool_calls is None
    assert req.logit_bias is None
