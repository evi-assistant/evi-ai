"""Capability detection + the tool-calling chip."""

from evi.capabilities import CHIP_LABELS, capabilities
from evi.toolcalling import model_supports_tools


def test_capabilities_has_all_chip_keys():
    caps = capabilities("qwen2.5-coder:7b")
    # Every key the UI can render must be present in the capabilities dict.
    for key in CHIP_LABELS:
        assert key in caps, f"capabilities() missing {key}"
    assert set(caps) == set(CHIP_LABELS)


def test_capabilities_empty_model_is_all_false():
    assert capabilities("") == {k: False for k in CHIP_LABELS}


def test_tools_known_families():
    for mid in (
        "qwen2.5-coder:7b", "qwen3:8b", "llama-3.1-8b-instruct",
        "mistral-nemo", "command-r:35b", "hermes-3-llama-3.1",
        "granite-3.1-8b", "deepseek-v3", "glm-4-9b", "gpt-4o",
        "claude-sonnet-4-6", "grok-2", "gemini-1.5-pro",
    ):
        assert model_supports_tools(mid), mid


def test_tools_anti_hints_win():
    # FIM/base/embedding/guard models look like tool callers but are not.
    for mid in (
        "deepseek-coder:6.7b", "codellama:13b", "starcoder2:7b",
        "nomic-embed-text", "bge-reranker-large", "llama-guard-3-8b",
        "qwen2.5-7b-base",
    ):
        assert not model_supports_tools(mid), mid


def test_tools_unknown_is_false():
    assert not model_supports_tools("some-random-7b")
    assert not model_supports_tools("")
