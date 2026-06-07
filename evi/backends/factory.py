"""Backend lookup — string -> class, plus per-backend default URLs."""

from __future__ import annotations

from evi.backends.base import Backend
from evi.backends.llamacpp import LlamaCppBackend
from evi.backends.lmstudio import LMStudioBackend
from evi.backends.ollama import OllamaBackend
from evi.backends.openai_compat import OpenAICompatBackend


KNOWN_BACKENDS: dict[str, type[Backend]] = {
    "lmstudio": LMStudioBackend,
    "ollama": OllamaBackend,
    "llamacpp": LlamaCppBackend,
    "openai_compat": OpenAICompatBackend,
}


_DEFAULT_URLS: dict[str, str] = {
    "lmstudio": "http://localhost:1234/v1",
    "ollama": "http://localhost:11434/v1",
    "llamacpp": "http://localhost:8080/v1",
    "openai_compat": "http://localhost:8000/v1",
}


def default_base_url(kind: str) -> str:
    return _DEFAULT_URLS.get(kind, _DEFAULT_URLS["openai_compat"])


def get_backend(settings) -> Backend:
    """Construct a backend from `LLMSettings`.

    Falls back to OpenAI-compatible if the kind is unrecognised, so a typo
    in config.toml doesn't crash eVi at startup — just produces something
    that still works for chat against the configured URL.
    """
    kind = (getattr(settings, "backend", None) or "lmstudio").strip().lower()
    cls = KNOWN_BACKENDS.get(kind, OpenAICompatBackend)
    return cls(
        base_url=settings.base_url,
        api_key=settings.api_key,
        request_timeout=settings.request_timeout,
    )
