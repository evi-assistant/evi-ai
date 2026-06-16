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


# --- parallel multi-agent research (Phase 61) ---------------------------


def _echo(*, system_prompt, task, tool_categories=(), max_turns=6):
    return f"found:{task}"


def test_run_subagents_parallel_preserves_order(monkeypatch) -> None:
    monkeypatch.setattr(subagent_mod, "run_subagent", _echo)
    res = subagent_mod.run_subagents_parallel(["a", "b", "c"], system_prompt="x")
    assert res == [("a", "found:a"), ("b", "found:b"), ("c", "found:c")]


def test_run_subagents_parallel_isolates_errors(monkeypatch) -> None:
    def flaky(*, system_prompt, task, tool_categories=(), max_turns=6):
        if task == "boom":
            raise RuntimeError("nope")
        return f"ok:{task}"

    monkeypatch.setattr(subagent_mod, "run_subagent", flaky)
    res = dict(subagent_mod.run_subagents_parallel(["a", "boom", "c"], system_prompt="x"))
    assert res["a"] == "ok:a" and res["c"] == "ok:c"
    assert res["boom"].startswith("ERROR:")


def test_parallel_research_tool_combines(monkeypatch) -> None:
    monkeypatch.setattr(subagent_mod, "run_subagent", _echo)
    out = REGISTRY["parallel_research"].call(
        json.dumps({"tasks": ["where is X", "how does Y work"]})
    )
    assert "Parallel research findings" in out
    assert "### 1. where is X" in out and "found:where is X" in out
    assert "### 2. how does Y work" in out


def test_parallel_research_caps_and_validates(monkeypatch) -> None:
    monkeypatch.setattr(subagent_mod, "run_subagent", _echo)
    out = REGISTRY["parallel_research"].call(
        json.dumps({"tasks": [f"t{i}" for i in range(10)]})
    )
    assert out.count("### ") == 6  # capped at _MAX_PARALLEL
    empty = REGISTRY["parallel_research"].call(json.dumps({"tasks": []}))
    assert empty.startswith("ERROR:")


# ---- user subagent profiles (evi agents new) -------------------------------


def test_add_and_load_user_profile(tmp_path) -> None:
    f = tmp_path / "agents.toml"
    subagent_mod.add_user_profile(
        "security", "You are a security reviewer.", ("fs", "code"), path=f
    )
    loaded = subagent_mod.load_user_profiles(f)
    assert loaded == {
        "security": {
            "system_prompt": "You are a security reviewer.",
            "tool_categories": ("fs", "code"),
        }
    }


def test_add_user_profile_rejects_builtin_name(tmp_path) -> None:
    with pytest.raises(ValueError, match="built-in"):
        subagent_mod.add_user_profile("explore", "x", path=tmp_path / "a.toml")


def test_add_user_profile_rejects_bad_name(tmp_path) -> None:
    with pytest.raises(ValueError, match="no spaces"):
        subagent_mod.add_user_profile("two words", "x", path=tmp_path / "a.toml")


def test_add_user_profile_dup_needs_force(tmp_path) -> None:
    f = tmp_path / "agents.toml"
    subagent_mod.add_user_profile("rev", "first", path=f)
    with pytest.raises(ValueError, match="already exists"):
        subagent_mod.add_user_profile("rev", "second", path=f)
    # --force overwrites and keeps the file parseable
    subagent_mod.add_user_profile("rev", "second", path=f, overwrite=True)
    assert subagent_mod.load_user_profiles(f)["rev"]["system_prompt"] == "second"


def test_add_user_profile_escapes_quotes(tmp_path) -> None:
    f = tmp_path / "agents.toml"
    subagent_mod.add_user_profile("q", 'say "hi"\nline2', path=f)
    assert subagent_mod.load_user_profiles(f)["q"]["system_prompt"] == 'say "hi"\nline2'


def test_all_profiles_merges_user(monkeypatch, tmp_path) -> None:
    f = tmp_path / "agents.toml"
    subagent_mod.add_user_profile("mine", "custom", ("fs",), path=f)
    monkeypatch.setattr(subagent_mod, "_user_profiles_path", lambda: f)
    profs = subagent_mod.all_profiles()
    assert "explore" in profs and "mine" in profs  # built-in + user
    # built-ins still win over a same-named user entry
    subagent_mod.add_user_profile("explore2", "x", path=f)
    assert subagent_mod.get_profile("mine") == {
        "system_prompt": "custom", "tool_categories": ("fs",)
    }
