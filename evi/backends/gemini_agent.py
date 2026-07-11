"""``gemini`` backend — Claude-Code-style integration of the local Google Gemini
CLI, authenticated by a ``gemini`` Google login (a free tier, ~1000 req/day, no
API key). Like the other CLI-agent backends it is NOT OpenAI-compatible on the
wire — its ``make_client()`` returns a shim (``evi.llm.gemini_agent
.GeminiAgentClient``) that adapts ``gemini -p … -o json`` into the
``chat.completions`` surface eVi expects.

Gemini is an autonomous agent that drives its OWN tools, so like ``codex`` this is
a chat / delegate provider (eVi's tools don't route through it). There is no
``base_url`` / ``api_key`` (auth is the CLI login). Setup: ``npm i -g
@google/gemini-cli`` then run ``gemini`` once to log in. The import is lazy, so
eVi runs fine without the CLI until this backend is selected.
"""

from __future__ import annotations

from evi.backends.base import Backend, ModelInfo

_MODELS: list[tuple[str, str]] = [
    ("gemini-2.5-pro", "Gemini 2.5 Pro"),
    ("gemini-2.5-flash", "Gemini 2.5 Flash (fast)"),
]


class GeminiAgentBackend(Backend):
    """Gemini over the local CLI. `base_url`/`api_key` are accepted for a uniform
    constructor but unused — auth is the `gemini` login."""

    name = "gemini"

    def __init__(self, base_url: str = "", api_key: str = "", request_timeout: float = 120.0):
        self.base_url = base_url or ""
        self.api_key = api_key or ""  # unused; auth is the local gemini login
        self.request_timeout = float(request_timeout or 120.0)

    def make_client(self):
        from evi.llm.gemini_agent import GeminiAgentClient

        return GeminiAgentClient()

    def list_models(self) -> list[ModelInfo]:
        return [
            ModelInfo(id=mid, backend=self.name, name=label, family="gemini")
            for mid, label in _MODELS
        ]

    def supports_pull(self) -> bool:
        return False
