"""``qwen`` backend — Qwen Code CLI integration (Alibaba's gemini-cli fork),
authenticated by a FREE Qwen OAuth login (sign in with a qwen.ai / Alibaba account,
~2000 req/day) — no API key. Like ``codex``/``gemini``/``amp`` it is NOT
OpenAI-compatible on the wire; its ``make_client()`` returns a shim
(``evi.llm.qwen_agent.QwenAgentClient``) that adapts ``qwen -p … -o json`` into the
``chat.completions`` surface eVi expects.

Qwen Code is an autonomous agent that drives its OWN tools, so eVi's tools don't
route through it (a chat / delegate provider). There is no ``base_url`` / ``api_key``
(auth is the CLI login) and no ``/v1/models`` endpoint, so ``list_models`` returns a
small curated set that resolves against your Qwen account. Setup: ``npm i -g
@qwen-code/qwen-code`` then run ``qwen`` once and pick 'Qwen' to sign in. The import
is lazy, so eVi runs fine without the CLI until this backend is selected.
"""

from __future__ import annotations

from evi.backends.base import Backend, ModelInfo

# Resolved against your Qwen account; free-tier OAuth serves the qwen3-coder models.
_MODELS: list[tuple[str, str]] = [
    ("qwen3-coder-plus", "Qwen3 Coder Plus (agentic coding, default)"),
    ("qwen3-coder-flash", "Qwen3 Coder Flash (faster / lighter)"),
]


class QwenAgentBackend(Backend):
    """Qwen Code over the local CLI. `base_url`/`api_key` are accepted for a
    uniform constructor but unused — auth is the free `qwen` OAuth login."""

    name = "qwen"

    def __init__(self, base_url: str = "", api_key: str = "", request_timeout: float = 180.0):
        self.base_url = base_url or ""
        self.api_key = api_key or ""  # unused; auth is the local qwen login
        self.request_timeout = float(request_timeout or 180.0)

    def make_client(self):
        from evi.llm.qwen_agent import QwenAgentClient

        return QwenAgentClient()

    def list_models(self) -> list[ModelInfo]:
        return [
            ModelInfo(id=mid, backend=self.name, name=label, family="qwen3-coder")
            for mid, label in _MODELS
        ]

    def supports_pull(self) -> bool:
        return False
