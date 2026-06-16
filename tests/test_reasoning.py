"""Tests for the reasoning-model capability gate (the 'does not support
thinking' 400 fix)."""

from __future__ import annotations

import pytest

from evi.reasoning import model_supports_reasoning


@pytest.mark.parametrize("model", [
    "deepseek-r1:14b",
    "deepseek-r1-distill-qwen-7b",
    "qwen3:8b",
    "qwq:32b",
    "magistral-small",
    "phi-4-reasoning",
    "phi-4-mini-reasoning",
    "gpt-5",
    "gpt-oss:20b",
    "o1",
    "o1-mini",
    "o3-mini",
    "o4-mini",
])
def test_reasoning_models_supported(model):
    assert model_supports_reasoning(model) is True


@pytest.mark.parametrize("model", [
    "qwen2.5:3b",
    "qwen2.5:14b-instruct-q4_K_M",
    "qwen2.5-coder:14b",
    "llama3.2:3b",
    "phi3.5:3.8b-mini",
    "mistral-small",
    "gemma2:9b",
    "command-r:35b",
    "",
])
def test_non_reasoning_models_not_supported(model):
    # These are exactly the models Ollama 400s on a thinking request.
    assert model_supports_reasoning(model) is False


def test_oseries_boundary_not_false_matching():
    # a stray "o1"/"o3" inside an unrelated name shouldn't trip the o-series rule
    assert model_supports_reasoning("histo1gram-model") is False
    assert model_supports_reasoning("two3things") is False


def test_gate_only_sends_effort_for_reasoning_models(monkeypatch, tmp_path):
    # End-to-end-ish: with reasoning_effort set high, the agent must NOT put
    # reasoning_effort in extra_body for a qwen2.5 model, but MUST for r1.
    monkeypatch.setattr("evi.config.HOME", tmp_path)
    monkeypatch.setattr("evi.config.CONFIG_PATH", tmp_path / "config.toml")
    from evi.config import Config
    from evi.llm.agent import Agent

    captured: dict = {}

    class _FakeStream:
        def __iter__(self):
            return iter(())  # no chunks -> turn ends immediately

    class _FakeCompletions:
        def create(self, **kwargs):
            captured.update(kwargs)
            return _FakeStream()

    class _FakeChat:
        completions = _FakeCompletions()

    class _FakeClient:
        chat = _FakeChat()

    def run_with(model: str) -> dict:
        captured.clear()
        cfg = Config.load()
        cfg.llm.model = model
        cfg.llm.reasoning_effort = "high"
        agent = Agent(client=_FakeClient(), config=cfg, tools=[])
        for _ in agent.chat("hi"):
            pass
        return captured.get("extra_body", {}) or {}

    assert "reasoning_effort" not in run_with("qwen2.5:14b-instruct-q4_K_M")
    assert run_with("deepseek-r1:14b").get("reasoning_effort") == "high"
