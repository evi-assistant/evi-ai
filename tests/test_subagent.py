"""Tests for the subagent runner and the delegate_* tools.

We don't hit the real LLM — instead we monkeypatch `Agent` inside
`evi.llm.subagent` with a fake that yields a scripted event stream so we can
verify event drain, tool tracing, and error propagation.
"""

from __future__ import annotations

import json
from typing import Iterator

import pytest

import evi.llm.subagent as subagent_mod
from evi.llm.agent import Done, Error, Event, TextDelta, ToolResult
from evi.tools.base import REGISTRY
import evi.tools.subagent  # noqa: F401  register the delegate_* tools


class _FakeAgent:
    last_init_kwargs: dict = {}

    def __init__(self, **kwargs):
        _FakeAgent.last_init_kwargs = kwargs

    def chat(self, task: str, max_turns: int = 6) -> Iterator[Event]:
        yield TextDelta("hello ")
        yield TextDelta("world")
        yield ToolResult(name="read_file", output="x" * 500)
        yield Done(reason="stop")


class _ErroringAgent:
    def __init__(self, **kwargs):
        pass

    def chat(self, task: str, max_turns: int = 6) -> Iterator[Event]:
        yield Error("model unreachable")


@pytest.fixture
def stub_llm(monkeypatch: pytest.MonkeyPatch) -> None:
    # We don't need a real OpenAI client or config — short-circuit both.
    monkeypatch.setattr(subagent_mod, "make_client", lambda *_: None)


def test_run_subagent_concatenates_text(stub_llm, monkeypatch) -> None:
    monkeypatch.setattr(subagent_mod, "Agent", _FakeAgent)
    out = subagent_mod.run_subagent(
        system_prompt="you are a test",
        task="do the thing",
        tool_categories=("fs",),
    )
    assert out.startswith("hello world")
    # The trace section truncates long outputs to 200 chars.
    assert "[trace]" in out
    assert "read_file:" in out


def test_run_subagent_surfaces_error(stub_llm, monkeypatch) -> None:
    monkeypatch.setattr(subagent_mod, "Agent", _ErroringAgent)
    out = subagent_mod.run_subagent(system_prompt="t", task="x")
    assert out.startswith("ERROR: subagent failed:")
    assert "model unreachable" in out


def test_delegate_explore_uses_fs_only(stub_llm, monkeypatch) -> None:
    monkeypatch.setattr(subagent_mod, "Agent", _FakeAgent)
    REGISTRY["delegate_explore"].call(json.dumps({"task": "find foo"}))
    kwargs = _FakeAgent.last_init_kwargs
    cats = {t.category for t in kwargs["tools"]}
    # Even if other tools are registered (memory, code, etc.) the explore
    # subagent must only see fs tools.
    assert cats.issubset({"fs"})


def test_delegate_plan_has_no_tools(stub_llm, monkeypatch) -> None:
    monkeypatch.setattr(subagent_mod, "Agent", _FakeAgent)
    REGISTRY["delegate_plan"].call(json.dumps({"task": "design X"}))
    kwargs = _FakeAgent.last_init_kwargs
    assert kwargs["tools"] == []
