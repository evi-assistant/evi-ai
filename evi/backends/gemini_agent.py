"""``gemini`` backend — Claude-Code-style integration of the local Google Gemini
CLI, authenticated the way that CLI is: a Gemini API key (``GEMINI_API_KEY``) or
Vertex AI. Google stopped serving Gemini CLI to free, Google AI Pro and Ultra
"Login with Google" accounts on 2026-06-18 (they moved to the closed Antigravity
CLI, which eVi doesn't drive). Like the other CLI-agent backends it is NOT
OpenAI-compatible on the wire — its ``make_client()`` returns a shim
(``evi.llm.gemini_agent.GeminiAgentClient``) that adapts ``gemini -o json`` (the
conversation on stdin) into the ``chat.completions`` surface eVi expects.

Gemini is an autonomous agent that drives its OWN tools, so like ``codex`` this is
a chat / delegate provider (eVi's tools don't route through it); it runs in
gemini's ``default`` approval mode, never ``yolo``. There is no ``base_url`` /
``api_key`` here (the CLI reads its own key). Setup: ``npm i -g
@google/gemini-cli`` and an API key or Vertex AI. The import is lazy, so eVi runs
fine without the CLI until this backend is selected.
"""

from __future__ import annotations

from evi.backends.base import Backend, ModelInfo

_MODELS: list[tuple[str, str]] = [
    ("gemini-2.5-pro", "Gemini 2.5 Pro"),
    ("gemini-2.5-flash", "Gemini 2.5 Flash (fast)"),
]


class GeminiAgentBackend(Backend):
    """Gemini over the local CLI. `base_url`/`api_key` are accepted for a uniform
    constructor but unused — the `gemini` CLI uses its own API key / Vertex AI."""

    name = "gemini"

    def __init__(self, base_url: str = "", api_key: str = "", request_timeout: float = 120.0):
        self.base_url = base_url or ""
        self.api_key = api_key or ""  # unused; the gemini CLI reads its own key
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
