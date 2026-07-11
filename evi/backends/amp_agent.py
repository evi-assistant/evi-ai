"""``amp`` backend — Sourcegraph Amp CLI integration, authenticated by ``amp
login`` (an Amp subscription / credit balance) or an ``AMP_API_KEY`` access token —
no per-token model API key. Like ``codex``/``gemini`` it is NOT OpenAI-compatible
on the wire; its ``make_client()`` returns a shim (``evi.llm.amp_agent.
AmpAgentClient``) that adapts ``amp -x --stream-json`` into the ``chat.completions``
surface eVi expects.

Amp is an autonomous agent that drives its OWN tools (per your ``amp permissions``),
so eVi's tools don't route through it — a chat / delegate provider. It selects the
underlying model/behaviour by AGENT MODE (``low``/``medium``/``high``), not a model
id, so ``list_models`` exposes those three. Setup: ``npm i -g @sourcegraph/amp``
then ``amp login`` (or set ``AMP_API_KEY``). The import is lazy, so eVi runs fine
without the CLI until this backend is selected.
"""

from __future__ import annotations

from evi.backends.base import Backend, ModelInfo

# Amp picks the underlying model via its AGENT MODE (-m/--mode), not a model id.
_MODES: list[tuple[str, str]] = [
    ("medium", "Amp — balanced (default)"),
    ("low", "Amp — fast / lighter"),
    ("high", "Amp — most capable"),
]


class AmpAgentBackend(Backend):
    """Sourcegraph Amp over the local CLI. `base_url`/`api_key` are accepted for a
    uniform constructor but unused — auth is `amp login` / `AMP_API_KEY`."""

    name = "amp"

    def __init__(self, base_url: str = "", api_key: str = "", request_timeout: float = 180.0):
        self.base_url = base_url or ""
        self.api_key = api_key or ""  # unused; auth is the local amp login / AMP_API_KEY
        self.request_timeout = float(request_timeout or 180.0)

    def make_client(self):
        from evi.llm.amp_agent import AmpAgentClient

        return AmpAgentClient()

    def list_models(self) -> list[ModelInfo]:
        return [
            ModelInfo(id=mid, backend=self.name, name=label, family="amp")
            for mid, label in _MODES
        ]

    def supports_pull(self) -> bool:
        return False
