"""LM Studio backend — OpenAI-compatible at /v1, no programmatic pull API."""

from __future__ import annotations

from openai import OpenAI

from evi.backends.base import Backend, window_tokens


class LMStudioBackend(Backend):
    name = "lmstudio"

    def __init__(
        self,
        base_url: str = "http://localhost:1234/v1",
        api_key: str = "lm-studio",  # LM Studio ignores but SDK requires a value
        request_timeout: float = 120.0,
    ) -> None:
        self.base_url = base_url
        self.api_key = api_key or "lm-studio"
        self.request_timeout = request_timeout

    def make_client(self) -> OpenAI:
        return OpenAI(
            base_url=self.base_url,
            api_key=self.api_key,
            timeout=self.request_timeout,
        )

    # LM Studio's `/v1/models` returns currently-loaded models only. Good
    # enough for `evi models list`; downloads happen in their UI or via the
    # `lms` CLI tool, which we don't shell out to.
    def list_models(self):
        return self._list_via_openai_endpoint()

    def context_window(self, model_id: str) -> int | None:
        """The window LM Studio loaded `model_id` with (its REST API,
        `/api/v0/models/{id}`: loaded_context_length). None while it isn't
        loaded, or from an LM Studio too old to have /api/v0."""
        from urllib.parse import quote

        from evi.portprobe import fast_get

        root = self.base_url.rstrip("/").removesuffix("/v1")
        r = fast_get(f"{root}/api/v0/models/{quote(model_id, safe='/')}")
        if r is None or r.status_code != 200:
            return None
        try:
            data = r.json()
        except ValueError:
            return None
        if not isinstance(data, dict) or data.get("state") != "loaded":
            return None
        return window_tokens(data.get("loaded_context_length"))
