"""Ollama backend — OpenAI-compatible at /v1, but with a rich native API for
model management (list/pull/show/delete) under `/api/`.

When `base_url` is the OpenAI-compatible one (`http://host:11434/v1`), we
derive the native API root by stripping `/v1`. This matches Ollama's
documented setup.
"""

from __future__ import annotations

import json
from typing import Iterator

from openai import OpenAI

from evi.backends.base import Backend, ModelInfo, PullProgress


class OllamaBackend(Backend):
    name = "ollama"

    def __init__(
        self,
        base_url: str = "http://localhost:11434/v1",
        api_key: str = "ollama",
        request_timeout: float = 120.0,
    ) -> None:
        self.base_url = base_url
        self.api_key = api_key or "ollama"
        self.request_timeout = request_timeout

    @property
    def native_base(self) -> str:
        """Root of Ollama's native API (`/api/...`), stripped of any `/v1`."""
        return self.base_url.rstrip("/").removesuffix("/v1")

    def make_client(self) -> OpenAI:
        return OpenAI(
            base_url=self.base_url,
            api_key=self.api_key,
            timeout=self.request_timeout,
        )

    def supports_pull(self) -> bool:
        return True

    def list_models(self) -> list[ModelInfo]:
        import httpx

        try:
            r = httpx.get(
                f"{self.native_base}/api/tags", timeout=self.request_timeout
            )
            r.raise_for_status()
        except Exception:
            return []
        out: list[ModelInfo] = []
        for entry in r.json().get("models", []) or []:
            details = entry.get("details") or {}
            out.append(
                ModelInfo(
                    id=str(entry.get("name", "")),
                    backend=self.name,
                    name=str(entry.get("name", "")),
                    size_bytes=entry.get("size"),
                    family=details.get("family"),
                    parameters=details.get("parameter_size"),
                    quantization=details.get("quantization_level"),
                    loaded=False,  # Ollama loads on first request, lazily
                )
            )
        return out

    def model_info(self, model_id: str) -> ModelInfo | None:
        import httpx

        try:
            r = httpx.post(
                f"{self.native_base}/api/show",
                json={"name": model_id},
                timeout=self.request_timeout,
            )
            r.raise_for_status()
        except Exception:
            return None
        data = r.json()
        details = data.get("details") or {}
        return ModelInfo(
            id=model_id,
            backend=self.name,
            name=model_id,
            family=details.get("family"),
            parameters=details.get("parameter_size"),
            quantization=details.get("quantization_level"),
        )

    def pull_model(self, model_id: str) -> Iterator[PullProgress]:
        """Stream the NDJSON progress events from `/api/pull`.

        Each line is `{"status": "...", "digest": "...", "total": N, "completed": M}`
        — we map them to `PullProgress` for the CLI to render.
        """
        import httpx

        with httpx.stream(
            "POST",
            f"{self.native_base}/api/pull",
            json={"name": model_id},
            timeout=None,
        ) as r:
            r.raise_for_status()
            for line in r.iter_lines():
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                yield PullProgress(
                    status=str(entry.get("status", "")),
                    downloaded=entry.get("completed"),
                    total=entry.get("total"),
                    detail=entry.get("digest"),
                )

    def delete_model(self, model_id: str) -> None:
        import httpx

        r = httpx.request(
            "DELETE",
            f"{self.native_base}/api/delete",
            json={"name": model_id},
            timeout=self.request_timeout,
        )
        r.raise_for_status()
