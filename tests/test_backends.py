"""Tests for the backend abstraction.

We don't hit a real LM Studio / Ollama / llama.cpp — `httpx.MockTransport`
gives us a deterministic transport per test. `make_client` is verified to
return the right OpenAI base_url for each backend kind.
"""

from __future__ import annotations

import json
from typing import Callable

import httpx
import pytest

from evi.backends import (
    KNOWN_BACKENDS,
    LlamaCppBackend,
    LMStudioBackend,
    OllamaBackend,
    OpenAICompatBackend,
    default_base_url,
    get_backend,
)
from evi.config import LLMSettings


def _patch_httpx(monkeypatch: pytest.MonkeyPatch, handler: Callable[[httpx.Request], httpx.Response]) -> None:
    """Replace httpx.get/post/stream/request with mock-transport-backed versions."""
    transport = httpx.MockTransport(handler)

    def _get(url, **kwargs):
        kwargs.pop("timeout", None)
        with httpx.Client(transport=transport) as c:
            return c.get(url, **kwargs)

    def _post(url, **kwargs):
        kwargs.pop("timeout", None)
        with httpx.Client(transport=transport) as c:
            return c.post(url, **kwargs)

    def _request(method, url, **kwargs):
        kwargs.pop("timeout", None)
        with httpx.Client(transport=transport) as c:
            return c.request(method, url, **kwargs)

    class _StreamCtx:
        def __init__(self, method, url, **kwargs):
            kwargs.pop("timeout", None)
            self._client = httpx.Client(transport=transport)
            self._req = self._client.build_request(method, url, **kwargs)

        def __enter__(self):
            self._resp = self._client.send(self._req, stream=True)
            return self._resp

        def __exit__(self, *exc):
            self._resp.close()
            self._client.close()

    monkeypatch.setattr(httpx, "get", _get)
    monkeypatch.setattr(httpx, "post", _post)
    monkeypatch.setattr(httpx, "request", _request)
    monkeypatch.setattr(httpx, "stream", _StreamCtx)


# ---- factory dispatch ---------------------------------------------------


def test_factory_resolves_known_kinds() -> None:
    for kind, cls in KNOWN_BACKENDS.items():
        settings = LLMSettings(backend=kind, base_url=default_base_url(kind))
        backend = get_backend(settings)
        assert isinstance(backend, cls)


def test_factory_falls_back_for_unknown_kind() -> None:
    settings = LLMSettings(backend="acme-cloud", base_url="http://x/v1")
    backend = get_backend(settings)
    assert isinstance(backend, OpenAICompatBackend)


def test_default_urls_per_backend() -> None:
    assert default_base_url("lmstudio") == "http://localhost:1234/v1"
    assert default_base_url("ollama") == "http://localhost:11434/v1"
    assert default_base_url("llamacpp") == "http://localhost:8080/v1"


# ---- LM Studio: /v1/models -> ModelInfo ---------------------------------


def test_lmstudio_list_via_openai_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        assert req.url.path.endswith("/models")
        return httpx.Response(
            200,
            json={"data": [{"id": "qwen2.5-14b-instruct"}, {"id": "llama-3.1-8b"}]},
        )

    _patch_httpx(monkeypatch, handler)
    b = LMStudioBackend()
    models = b.list_models()
    assert [m.id for m in models] == ["qwen2.5-14b-instruct", "llama-3.1-8b"]
    assert all(m.backend == "lmstudio" and m.loaded for m in models)


def test_make_client_pointed_at_base_url() -> None:
    b = LMStudioBackend(base_url="http://10.0.0.5:1234/v1")
    client = b.make_client()
    assert str(client.base_url).startswith("http://10.0.0.5:1234")


# ---- Ollama: rich native API --------------------------------------------


def test_ollama_native_base_strips_v1() -> None:
    assert OllamaBackend(base_url="http://h:11434/v1").native_base == "http://h:11434"
    assert OllamaBackend(base_url="http://h:11434").native_base == "http://h:11434"


def test_ollama_list_models_includes_details(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        assert req.url.path == "/api/tags"
        return httpx.Response(
            200,
            json={
                "models": [
                    {
                        "name": "qwen2.5:14b",
                        "size": 9_000_000_000,
                        "details": {
                            "family": "qwen2",
                            "parameter_size": "14B",
                            "quantization_level": "Q4_K_M",
                        },
                    }
                ]
            },
        )

    _patch_httpx(monkeypatch, handler)
    models = OllamaBackend().list_models()
    assert len(models) == 1
    m = models[0]
    assert m.id == "qwen2.5:14b"
    assert m.family == "qwen2"
    assert m.parameters == "14B"
    assert m.quantization == "Q4_K_M"
    assert m.size_bytes == 9_000_000_000


def test_ollama_model_info_via_show(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        assert req.url.path == "/api/show"
        return httpx.Response(
            200,
            json={"details": {"family": "qwen2", "parameter_size": "14B"}},
        )

    _patch_httpx(monkeypatch, handler)
    info = OllamaBackend().model_info("qwen2.5:14b")
    assert info is not None
    assert info.parameters == "14B"


def test_ollama_pull_streams_progress(monkeypatch: pytest.MonkeyPatch) -> None:
    lines = [
        json.dumps({"status": "pulling manifest"}).encode(),
        json.dumps({"status": "downloading", "completed": 100, "total": 1000}).encode(),
        json.dumps({"status": "success"}).encode(),
    ]

    def handler(req: httpx.Request) -> httpx.Response:
        assert req.url.path == "/api/pull"
        return httpx.Response(200, content=b"\n".join(lines))

    _patch_httpx(monkeypatch, handler)
    progress = list(OllamaBackend().pull_model("qwen2.5:14b"))
    statuses = [p.status for p in progress]
    assert "downloading" in statuses
    assert "success" in statuses
    middle = next(p for p in progress if p.status == "downloading")
    assert middle.downloaded == 100 and middle.total == 1000


def test_ollama_supports_pull_lmstudio_does_not() -> None:
    assert OllamaBackend().supports_pull() is True
    assert LMStudioBackend().supports_pull() is False
    assert LlamaCppBackend().supports_pull() is False
    assert OpenAICompatBackend().supports_pull() is False


def test_list_models_tolerates_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="kaboom")

    _patch_httpx(monkeypatch, handler)
    assert LMStudioBackend().list_models() == []
    assert OllamaBackend().list_models() == []


# ---- llama.cpp port fallback (8080..8090) -------------------------------


def test_llamacpp_keeps_configured_port_when_live(monkeypatch: pytest.MonkeyPatch) -> None:
    import evi.portprobe as pp

    monkeypatch.setattr(pp, "is_openai_server", lambda url, **k: True)

    def no_scan(*a, **k):  # discovery must NOT run if configured port works
        raise AssertionError("should not scan when configured port is live")

    monkeypatch.setattr(pp, "discover_llamacpp_url", no_scan)
    b = LlamaCppBackend(base_url="http://localhost:8080/v1")
    assert str(b.make_client().base_url).startswith("http://localhost:8080")


def test_llamacpp_discovers_alt_port(monkeypatch: pytest.MonkeyPatch) -> None:
    import evi.portprobe as pp

    monkeypatch.setattr(pp, "is_openai_server", lambda url, **k: False)
    monkeypatch.setattr(pp, "discover_llamacpp_url", lambda url, **k: "http://127.0.0.1:8083/v1")
    b = LlamaCppBackend(base_url="http://localhost:8080/v1")
    assert str(b.make_client().base_url).startswith("http://127.0.0.1:8083")


def test_llamacpp_falls_back_to_configured_when_none_found(monkeypatch: pytest.MonkeyPatch) -> None:
    import evi.portprobe as pp

    monkeypatch.setattr(pp, "is_openai_server", lambda url, **k: False)
    monkeypatch.setattr(pp, "discover_llamacpp_url", lambda url, **k: None)
    b = LlamaCppBackend(base_url="http://localhost:8080/v1")
    assert str(b.make_client().base_url).startswith("http://localhost:8080")


def test_llamacpp_discovery_can_be_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    import evi.portprobe as pp

    def boom(*a, **k):
        raise AssertionError("no probing when discover_ports=False")

    monkeypatch.setattr(pp, "is_openai_server", boom)
    monkeypatch.setattr(pp, "discover_llamacpp_url", boom)
    b = LlamaCppBackend(base_url="http://localhost:8080/v1", discover_ports=False)
    assert str(b.make_client().base_url).startswith("http://localhost:8080")


def test_llamacpp_resolution_is_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    import evi.portprobe as pp

    calls = {"n": 0}

    def count(url, **k):
        calls["n"] += 1
        return False

    monkeypatch.setattr(pp, "is_openai_server", count)
    monkeypatch.setattr(pp, "discover_llamacpp_url", lambda url, **k: None)
    b = LlamaCppBackend(base_url="http://localhost:8080/v1")
    b.make_client()
    b.make_client()
    assert calls["n"] == 1  # resolved once, then cached
