"""Generic OpenAI-compatible backend — catchall for unknown servers.

Use this when pointing at a hosted endpoint we don't recognise: a remote
Evi web server, a vLLM/OpenLLM/llamafile instance someone else stood up,
or a SaaS gateway. Only the chat client is functional; model management
falls back to the `/models` endpoint (and only if it exists).
"""

from __future__ import annotations

from openai import OpenAI

from evi.backends.base import Backend


class OpenAICompatBackend(Backend):
    name = "openai_compat"

    def __init__(
        self,
        base_url: str = "http://localhost:8000/v1",
        api_key: str = "",
        request_timeout: float = 120.0,
    ) -> None:
        self.base_url = base_url
        self.api_key = api_key
        self.request_timeout = request_timeout

    def make_client(self) -> OpenAI:
        return OpenAI(
            base_url=self.base_url,
            api_key=self.api_key or "none",
            timeout=self.request_timeout,
        )
