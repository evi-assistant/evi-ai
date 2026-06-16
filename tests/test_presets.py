"""Tests for online provider presets + env-var API-key resolution."""

from __future__ import annotations

from evi.backends.presets import (
    ONLINE_PRESETS,
    get_preset,
    resolve_api_key,
)


def test_presets_all_openai_compatible_shape():
    # Every preset has the fields the CLI relies on; base_url is https.
    for name, p in ONLINE_PRESETS.items():
        assert p.name == name
        assert p.base_url.startswith("https://")
        assert p.api_key_env  # an env var name to read the key from
        assert p.api in ("chat", "responses")


def test_get_preset_case_insensitive():
    assert get_preset("OpenRouter") is ONLINE_PRESETS["openrouter"]
    assert get_preset("nope") is None


def test_resolve_api_key_env_reference(monkeypatch):
    monkeypatch.setenv("MY_PROVIDER_KEY", "sk-secret")
    assert resolve_api_key("env:MY_PROVIDER_KEY") == "sk-secret"


def test_resolve_api_key_env_missing_is_empty(monkeypatch):
    monkeypatch.delenv("ABSENT_KEY", raising=False)
    assert resolve_api_key("env:ABSENT_KEY") == ""


def test_resolve_api_key_literal_passthrough():
    assert resolve_api_key("sk-literal") == "sk-literal"
    assert resolve_api_key("") == ""


def test_get_backend_resolves_env_key(monkeypatch):
    monkeypatch.setenv("XAI_TEST_KEY", "sk-xai")
    from evi.backends import get_backend
    from evi.config import LLMSettings

    s = LLMSettings(
        backend="openai_compat",
        base_url="https://api.x.ai/v1",
        api_key="env:XAI_TEST_KEY",
    )
    backend = get_backend(s)
    # the backend received the resolved key, not the env: reference
    assert getattr(backend, "api_key", None) == "sk-xai"


def test_anthropic_preset_targets_compat_endpoint():
    # Guard the documented caveat: the anthropic preset must NOT point at the
    # native Messages API (/v1/messages); it uses the OpenAI-compat endpoint.
    p = ONLINE_PRESETS["anthropic"]
    assert p.base_url.rstrip("/").endswith("/v1")
    assert "messages" not in p.base_url
