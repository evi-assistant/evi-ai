"""Tests for the context window probe.

We use the same HTTP mocking strategy as the backend tests to simulate
Ollama's `/api/ps` responses.
"""

from __future__ import annotations

import httpx
import pytest

from evi.backends.base import (
    window_tokens,
)
from evi.backends import (
    LlamaCppBackend,
    LMStudioBackend,
    OllamaBackend,
    OpenAICompatBackend,
)
from tests.test_backends import _patch_httpx


def test_window_tokens_logic() -> None:
    # AC 4: window_tokens validation
    assert window_tokens(8192) == 8192
    assert window_tokens(0) is None
    assert window_tokens(-1) is None
    assert window_tokens(True) is None
    assert window_tokens("8192") is None
    assert window_tokens(None) is None
    assert window_tokens(2.5) is None


def test_backend_base_returns_none() -> None:
    # AC 4: Base Backend returns None
    b = OpenAICompatBackend("http://h/v1")
    assert b.context_window("m") is None


def test_ollama_context_window_success(monkeypatch: pytest.MonkeyPatch) -> None:
    # AC 1: Successful probe of /api/ps
    def handler(req: httpx.Request) -> httpx.Response:
        assert req.url.path == "/api/ps"
        return httpx.Response(
            200,
            json={"models": [{"name": "qwen2.5-coder:14b", "model": "qwen2.5-coder:14b", "context_length": 8192}]},
        )

    _patch_httpx(monkeypatch, handler)
    b = OllamaBackend("http://host:11434/v1")
    assert b.context_window("qwen2.5-coder:14b") == 8192


def test_ollama_context_window_tag_matching(monkeypatch: pytest.MonkeyPatch) -> None:
    # AC 2: Tag matching (latest) and missing models
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"models": [{"name": "llama3:latest", "model": "llama3:latest", "context_length": 4096}]},
        )

    _patch_httpx(monkeypatch, handler)
    b = OllamaBackend()
    # Match without tag -> :latest
    assert b.context_window("llama3") == 4096
    # Model not in list
    assert b.context_window("missing") is None


def test_ollama_context_window_robustness(monkeypatch: pytest.MonkeyPatch) -> None:
    # AC 3: None, never an exception, for various failure modes
    # 500 answer
    def handler_500(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(500)
    _patch_httpx(monkeypatch, handler_500)
    assert OllamaBackend().context_window("m") is None

    # Body not JSON
    def handler_text(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="not json")
    _patch_httpx(monkeypatch, handler_text)
    assert OllamaBackend().context_window("m") is None

    # JSON list instead of dict
    def handler_list(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[1, 2, 3])
    _patch_httpx(monkeypatch, handler_list)
    assert OllamaBackend().context_window("m") is None

    # "models" not a list
    def handler_bad_models(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"models": "not a list"})
    _patch_httpx(monkeypatch, handler_bad_models)
    assert OllamaBackend().context_window("m") is None

    # context_length is True, 0, or string
    def handler_bad_values(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"models": [
                {"name": "m1:7b", "context_length": True},
                {"name": "m2:7b", "context_length": 0},
                {"name": "m3:7b", "context_length": "8192"},
                {"name": "ok:7b", "context_length": 8192},
            ]},
        )
    _patch_httpx(monkeypatch, handler_bad_values)
    b = OllamaBackend()
    # The names match, so only the reported value decides (ok:7b shows a good one counts).
    assert b.context_window("m1:7b") is None
    assert b.context_window("m2:7b") is None
    assert b.context_window("m3:7b") is None
    assert b.context_window("ok:7b") == 8192

    # Unreachable server (portprobe.fast_get returning None)
    # We mock fast_get directly since _patch_httpx mocks the transport,
    # but the code calls fast_get which is a wrapper.
    import evi.portprobe
    monkeypatch.setattr(evi.portprobe, "fast_get", lambda *a, **k: None)
    assert OllamaBackend().context_window("m") is None


def test_llamacpp_reads_n_ctx_from_props(monkeypatch: pytest.MonkeyPatch) -> None:
    b = LlamaCppBackend("http://h:8080/v1", discover_ports=False)
    for props, want in (
        ({"default_generation_settings": {"n_ctx": 8192}}, 8192),  # current builds
        ({"n_ctx": 4096}, 4096),  # older builds
        ({"default_generation_settings": {"n_ctx": 0}, "n_ctx": 2048}, 2048),
        ({"default_generation_settings": {}}, None),
    ):
        seen: list[str] = []

        def handler(req: httpx.Request, props=props, seen=seen) -> httpx.Response:
            seen.append(str(req.url))
            return httpx.Response(200, json=props)

        _patch_httpx(monkeypatch, handler)
        assert b.context_window("x") == want, props
        assert seen == ["http://h:8080/props"]


def test_lmstudio_reads_the_loaded_window(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/api/v0/models/qwen/qwen3-8b":
            return httpx.Response(200, json={"state": "loaded", "loaded_context_length": 4096,
                                             "max_context_length": 32768})
        return httpx.Response(404)

    _patch_httpx(monkeypatch, handler)
    b = LMStudioBackend("http://h:1234/v1")
    assert b.context_window("qwen/qwen3-8b") == 4096
    assert b.context_window("other") is None  # a 404

    def not_loaded(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"state": "not-loaded", "loaded_context_length": 4096})

    _patch_httpx(monkeypatch, not_loaded)
    assert b.context_window("qwen/qwen3-8b") is None


def test_llamacpp_and_lmstudio_answer_none_to_a_bad_body(monkeypatch: pytest.MonkeyPatch) -> None:
    backends = [LlamaCppBackend("http://h:8080/v1", discover_ports=False),
                LMStudioBackend("http://h:1234/v1")]
    for response in (httpx.Response(200, text="not json"), httpx.Response(200, json=[1, 2, 3])):
        _patch_httpx(monkeypatch, lambda req, r=response: r)
        assert [b.context_window("m") for b in backends] == [None, None]
