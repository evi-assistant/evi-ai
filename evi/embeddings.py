"""Embedding client — talks to whichever backend hosts the embed model.

LM Studio, Ollama, and llama.cpp all expose an OpenAI-style
`POST /v1/embeddings { model, input }` endpoint when an embedding model is
loaded. We piggyback on `LLMSettings.base_url`; the user picks an
embed-capable model name via `[llm] embed_model`.

The function returns a list-of-lists (one vector per input). Vectors are
plain Python floats so they can hop through numpy or JSON without
ceremony.
"""

from __future__ import annotations

import httpx

from evi.config import LLMSettings


_TIMEOUT = 60.0


def embed_texts(texts: list[str], settings: LLMSettings) -> list[list[float]]:
    """POST one batch to the embeddings endpoint; raise on failure."""
    if not texts:
        return []
    url = settings.base_url.rstrip("/") + "/embeddings"
    payload = {"model": settings.embed_model, "input": texts}
    headers = {"Authorization": f"Bearer {settings.api_key}"} if settings.api_key else None
    r = httpx.post(url, json=payload, headers=headers, timeout=_TIMEOUT)
    r.raise_for_status()
    data = r.json()
    items = data.get("data") or []
    return [list(item.get("embedding") or []) for item in items]
