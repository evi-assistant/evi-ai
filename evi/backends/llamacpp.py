"""llama.cpp server backend — `llama-server` binary, OpenAI-compatible at /v1.

`list_models` returns whichever model the server was started with (usually
one). No pull / delete — load models by restarting `llama-server` with a
different `-m <path>`.

Port fallback: llama.cpp defaults to :8080, but that port is commonly taken
(other dev servers love 8080 too). When the configured port doesn't answer
like a llama.cpp server, we scan the next few ports (8080..8090) and use the
first one that does. The resolved URL is cached on the instance.
"""

from __future__ import annotations

from openai import OpenAI

from evi.backends.base import Backend


class LlamaCppBackend(Backend):
    name = "llamacpp"

    def __init__(
        self,
        base_url: str = "http://localhost:8080/v1",
        api_key: str = "llamacpp",
        request_timeout: float = 120.0,
        discover_ports: bool = True,
    ) -> None:
        self.base_url = base_url
        self.api_key = api_key or "llamacpp"
        self.request_timeout = request_timeout
        self.discover_ports = discover_ports
        self._resolved_url: str | None = None

    def _effective_base_url(self) -> str:
        """Resolve the URL to actually connect to, scanning 8080..8090 for a
        live llama.cpp server if the configured port isn't one. Cached so the
        scan happens at most once per instance."""
        if not self.discover_ports:
            return self.base_url
        if self._resolved_url is not None:
            return self._resolved_url

        from evi.portprobe import discover_llamacpp_url, is_openai_server

        # Honour the configured port if it already serves an LLM.
        if is_openai_server(self.base_url, api_key=self.api_key):
            self._resolved_url = self.base_url
            return self._resolved_url

        found = discover_llamacpp_url(self.base_url, api_key=self.api_key)
        # Fall back to the configured URL if nothing answered, so behaviour is
        # unchanged when llama.cpp simply isn't running.
        self._resolved_url = found or self.base_url
        return self._resolved_url

    def make_client(self) -> OpenAI:
        return OpenAI(
            base_url=self._effective_base_url(),
            api_key=self.api_key,
            timeout=self.request_timeout,
        )

    # `/v1/models` returns the single loaded model on `llama-server`.
    def list_models(self):
        # Probe via the resolved URL so a non-default port is found.
        effective = self._effective_base_url()
        if effective != self.base_url:
            original, self.base_url = self.base_url, effective
            try:
                return self._list_via_openai_endpoint()
            finally:
                self.base_url = original
        return self._list_via_openai_endpoint()
