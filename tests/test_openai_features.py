"""Tests for the OpenAI-SDK features added in Phase 18:
response_format, tool_choice, stop sequences, seed, sampling knobs, usage.
"""

from __future__ import annotations

from typing import Any

from evi.config import Config
from evi.llm.agent import Agent, Done, TextDelta, UsageStats


# ---- minimal capturing fake client --------------------------------------


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


class _Usage:
    def __init__(self, prompt=10, completion=20, total=30):
        self.prompt_tokens = prompt
        self.completion_tokens = completion
        self.total_tokens = total


class _CapturingCompletions:
    """Records each create() call. Yields a single text chunk + a usage chunk."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def create(self, **kwargs):
        self.calls.append(dict(kwargs))
        return iter([
            _Chunk(choices=[_Choice(content="ok", finish="stop")]),
            _Chunk(choices=[], usage=_Usage()),
        ])


def _make_agent(cfg: Config | None = None) -> tuple[Agent, _CapturingCompletions]:
    cc = _CapturingCompletions()
    client = type("C", (), {"chat": type("X", (), {"completions": cc})()})()
    return Agent(client=client, config=cfg or Config(), tools=[]), cc


# ---- response_format ----------------------------------------------------


def test_response_format_default_omitted() -> None:
    agent, cc = _make_agent()
    list(agent.chat("hi"))
    assert "response_format" not in cc.calls[0]


def test_response_format_json_mode_forwarded() -> None:
    agent, cc = _make_agent()
    list(agent.chat("hi", response_format={"type": "json_object"}))
    assert cc.calls[0]["response_format"] == {"type": "json_object"}


def test_response_format_structured_schema_forwarded() -> None:
    agent, cc = _make_agent()
    schema = {
        "type": "json_schema",
        "json_schema": {
            "name": "person",
            "schema": {
                "type": "object",
                "properties": {"name": {"type": "string"}},
                "required": ["name"],
            },
            "strict": True,
        },
    }
    list(agent.chat("hi", response_format=schema))
    assert cc.calls[0]["response_format"] == schema


# ---- tool_choice --------------------------------------------------------


def test_tool_choice_default_omitted() -> None:
    agent, cc = _make_agent()
    list(agent.chat("hi"))
    assert "tool_choice" not in cc.calls[0]


def test_tool_choice_required_forwarded() -> None:
    agent, cc = _make_agent()
    list(agent.chat("hi", tool_choice="required"))
    assert cc.calls[0]["tool_choice"] == "required"


def test_tool_choice_none_strips_tools() -> None:
    """tool_choice='none' should also remove the tools list (don't send schemas)."""
    agent, cc = _make_agent()
    list(agent.chat("hi", tool_choice="none"))
    assert cc.calls[0]["tools"] is None
    assert "tool_choice" not in cc.calls[0]  # we drop both


def test_tool_choice_specific_function() -> None:
    agent, cc = _make_agent()
    choice = {"type": "function", "function": {"name": "x"}}
    list(agent.chat("hi", tool_choice=choice))
    assert cc.calls[0]["tool_choice"] == choice


# ---- sampling knobs -----------------------------------------------------


def test_sampling_defaults_omitted() -> None:
    agent, cc = _make_agent()
    list(agent.chat("hi"))
    call = cc.calls[0]
    assert "top_p" not in call
    assert "presence_penalty" not in call
    assert "frequency_penalty" not in call
    assert "seed" not in call
    assert "stop" not in call


def test_top_p_forwarded_when_non_default() -> None:
    cfg = Config()
    cfg.llm.top_p = 0.85
    agent, cc = _make_agent(cfg)
    list(agent.chat("hi"))
    assert cc.calls[0]["top_p"] == 0.85


def test_penalties_forwarded() -> None:
    cfg = Config()
    cfg.llm.presence_penalty = 0.5
    cfg.llm.frequency_penalty = -0.3
    agent, cc = _make_agent(cfg)
    list(agent.chat("hi"))
    assert cc.calls[0]["presence_penalty"] == 0.5
    assert cc.calls[0]["frequency_penalty"] == -0.3


def test_seed_forwarded() -> None:
    cfg = Config()
    cfg.llm.seed = 42
    agent, cc = _make_agent(cfg)
    list(agent.chat("hi"))
    assert cc.calls[0]["seed"] == 42


def test_stop_sequences_forwarded() -> None:
    cfg = Config()
    cfg.llm.stop_sequences = ["</think>", "STOP"]
    agent, cc = _make_agent(cfg)
    list(agent.chat("hi"))
    assert cc.calls[0]["stop"] == ["</think>", "STOP"]


# ---- usage stats --------------------------------------------------------


def test_stream_options_always_requests_usage() -> None:
    agent, cc = _make_agent()
    list(agent.chat("hi"))
    assert cc.calls[0]["stream_options"] == {"include_usage": True}


def test_usage_stats_emitted() -> None:
    agent, _cc = _make_agent()
    events = list(agent.chat("hi"))
    usage = next(e for e in events if isinstance(e, UsageStats))
    assert usage.prompt_tokens == 10
    assert usage.completion_tokens == 20
    assert usage.total_tokens == 30


def test_usage_event_precedes_done() -> None:
    """UsageStats should appear before Done so consumers can finalize state."""
    agent, _cc = _make_agent()
    events = list(agent.chat("hi"))
    usage_i = next(i for i, e in enumerate(events) if isinstance(e, UsageStats))
    done_i = next(i for i, e in enumerate(events) if isinstance(e, Done))
    assert usage_i < done_i


def test_usage_absent_when_backend_doesnt_report() -> None:
    """No usage chunk → no UsageStats event. Done still fires."""
    class _NoUsageCompletions:
        def create(self, **_kwargs):
            return iter([_Chunk(choices=[_Choice(content="ok", finish="stop")])])

    client = type("C", (), {"chat": type("X", (), {"completions": _NoUsageCompletions()})()})()
    agent = Agent(client=client, config=Config(), tools=[])
    events = list(agent.chat("hi"))
    assert not any(isinstance(e, UsageStats) for e in events)
    assert any(isinstance(e, Done) for e in events)
    # Sanity: a TextDelta still came through.
    assert any(isinstance(e, TextDelta) for e in events)
