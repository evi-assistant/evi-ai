"""LLM backend abstraction — one interface, three transports.

All four backend implementations expose an OpenAI-compatible chat endpoint
(that's the whole point — Evi's `Agent` loop only knows the OpenAI SDK),
but they differ on:

- default port and API-key handling
- whether they have a model-management API (Ollama yes; LM Studio and
  llama.cpp server: mostly no — Ollama wins for `evi models pull`)
- how `list_models` enumerates what's available

Use `get_backend(settings)` to dispatch. New backends slot in by adding a
subclass + a kind-name entry in `_REGISTRY`.
"""

from evi.backends.base import Backend, ModelInfo, PullProgress
from evi.backends.factory import KNOWN_BACKENDS, default_base_url, get_backend
from evi.backends.lmstudio import LMStudioBackend
from evi.backends.ollama import OllamaBackend
from evi.backends.llamacpp import LlamaCppBackend
from evi.backends.openai_compat import OpenAICompatBackend

__all__ = [
    "Backend",
    "ModelInfo",
    "PullProgress",
    "KNOWN_BACKENDS",
    "default_base_url",
    "get_backend",
    "LMStudioBackend",
    "OllamaBackend",
    "LlamaCppBackend",
    "OpenAICompatBackend",
]
