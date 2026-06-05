"""LM Studio backend — OpenAI-compatible at /v1, no programmatic pull API."""

from __future__ import annotations

from openai import OpenAI

from evi.backends.base import Backend


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
