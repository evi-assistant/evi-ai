"""``copilot`` backend — GitHub Copilot CLI integration (``@github/copilot``),
authenticated by a GitHub Copilot subscription (``copilot login`` / ``/login``, or
an existing GitHub credential) — no separate model API key. Like the other
CLI-agent backends it is NOT OpenAI-compatible on the wire; its ``make_client()``
returns a shim (``evi.llm.copilot_agent.CopilotAgentClient``) that adapts
``copilot -p … --output-format text -s`` into the ``chat.completions`` surface eVi
expects.

Copilot is an autonomous agent that drives its OWN tools, so eVi's tools don't
route through it (a chat / delegate provider). It picks the underlying model via
``--model`` (``auto`` lets Copilot choose); the exact set depends on your Copilot
plan, so ``list_models`` exposes a small curated set. Setup: ``npm i -g
@github/copilot`` then ``copilot login``. The import is lazy, so eVi runs fine
without the CLI until this backend is selected.
"""

from __future__ import annotations

from evi.backends.base import Backend, ModelInfo

# Resolved against your Copilot plan; 'auto' lets Copilot pick the model.
_MODELS: list[tuple[str, str]] = [
    ("auto", "Copilot (auto — Copilot picks the model)"),
    ("claude-sonnet-4.5", "Claude Sonnet 4.5 (via Copilot)"),
    ("gpt-5", "GPT-5 (via Copilot)"),
]


class CopilotAgentBackend(Backend):
    """GitHub Copilot over the local CLI. `base_url`/`api_key` are accepted for a
    uniform constructor but unused — auth is the local `copilot login`."""

    name = "copilot"

    def __init__(self, base_url: str = "", api_key: str = "", request_timeout: float = 180.0):
        self.base_url = base_url or ""
        self.api_key = api_key or ""  # unused; auth is the local copilot login
        self.request_timeout = float(request_timeout or 180.0)

    def make_client(self):
        from evi.llm.copilot_agent import CopilotAgentClient

        return CopilotAgentClient()

    def list_models(self) -> list[ModelInfo]:
        return [
            ModelInfo(id=mid, backend=self.name, name=label, family="copilot")
            for mid, label in _MODELS
        ]

    def supports_pull(self) -> bool:
        return False
