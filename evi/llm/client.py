"""LLM client wrapper — dispatches to the configured backend.

`LLMSettings.backend` picks the backend kind; `evi.backends` builds the
right object and we just unwrap its OpenAI-compatible chat client. The
backend object itself is exposed via `get_backend(settings)` for code that
wants the richer surface (model listing, pulls, …).
"""

from __future__ import annotations

from openai import OpenAI

from evi.backends import Backend, get_backend
from evi.config import LLMSettings


def make_client(settings: LLMSettings) -> OpenAI:
    """Return an OpenAI SDK client routed at the configured backend."""
    return get_backend(settings).make_client()


def make_backend(settings: LLMSettings) -> Backend:
    """Return the full Backend object (model mgmt + chat client)."""
    return get_backend(settings)
