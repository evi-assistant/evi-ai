"""``codex`` backend — Claude-Code-style integration of the local OpenAI Codex
CLI, authenticated by ``codex login`` (ChatGPT Plus/Pro/Business subscription), no
API key. Like ``claude_agent`` it is NOT OpenAI-compatible on the wire — its
``make_client()`` returns a shim (``evi.llm.codex_agent.CodexAgentClient``) that
adapts ``codex exec --json`` into the ``chat.completions`` surface eVi expects.

Unlike ``claude_agent``, Codex is an autonomous agent that drives its OWN tools,
so eVi's tools don't route through it (it's a chat / delegate provider). There is
no ``base_url`` / ``api_key`` (auth is the CLI login) and no ``/v1/models``
endpoint, so ``list_models`` returns a small curated set. Setup: ``npm i -g
@openai/codex`` then ``codex login``. The SDK-less import is lazy, so eVi runs fine
without the CLI until this backend is selected.
"""

from __future__ import annotations

from evi.backends.base import Backend, ModelInfo

# Codex resolves these against your ChatGPT plan; override per-call via -m/config.
_MODELS: list[tuple[str, str]] = [
    ("gpt-5-codex", "GPT-5 Codex (agentic coding, default)"),
    ("gpt-5", "GPT-5 (general)"),
]


class CodexAgentBackend(Backend):
    """OpenAI Codex over the local CLI. `base_url`/`api_key` are accepted for a
    uniform constructor but unused — auth is `codex login`."""

    name = "codex"

    def __init__(self, base_url: str = "", api_key: str = "", request_timeout: float = 120.0):
        self.base_url = base_url or ""
        self.api_key = api_key or ""  # unused; auth is the local codex login
        self.request_timeout = float(request_timeout or 120.0)

    def make_client(self):
        from evi.llm.codex_agent import CodexAgentClient

        return CodexAgentClient()

    def list_models(self) -> list[ModelInfo]:
        return [
            ModelInfo(id=mid, backend=self.name, name=label, family="gpt-5")
            for mid, label in _MODELS
        ]

    def supports_pull(self) -> bool:
        return False
