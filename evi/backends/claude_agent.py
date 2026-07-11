"""``claude_agent`` backend — talk to Claude through the local ``claude`` CLI
(Claude Agent SDK) using your Max/Pro subscription login, no ``ANTHROPIC_API_KEY``.

Unlike every other backend, this one is NOT OpenAI-compatible on the wire — its
``make_client()`` returns a shim (``evi.llm.claude_agent.ClaudeAgentClient``) that
adapts the Agent SDK's async loop into the ``chat.completions.create`` surface the
agent expects. There is no ``base_url`` / ``api_key`` (auth is the CLI login) and
no ``/v1/models`` endpoint, so ``list_models`` returns a curated set of aliases
that the CLI resolves to whatever your plan currently serves.

Setup: install the ``claude`` CLI, log in on your Max/Pro plan, and
``pip install 'evi-assistant[claude-agent]'``. The SDK import is lazy (in
``make_client``), so eVi runs fine without it until this backend is selected.
"""

from __future__ import annotations

from evi.backends.base import Backend, ModelInfo

# Aliases the `claude` CLI resolves to the current model on your plan. Kept to
# aliases (not dated ids) so the list never advertises a model the CLI rejects.
_MODELS: list[tuple[str, str]] = [
    ("opus", "Claude Opus (latest on your plan)"),
    ("sonnet", "Claude Sonnet (latest on your plan)"),
    ("haiku", "Claude Haiku (fast, latest on your plan)"),
]


class ClaudeAgentBackend(Backend):
    """Claude over the local CLI. `base_url`/`api_key` are accepted for a uniform
    constructor but unused — authentication is the `claude` login."""

    name = "claude_agent"

    def __init__(self, base_url: str = "", api_key: str = "", request_timeout: float = 120.0):
        self.base_url = base_url or ""
        self.api_key = api_key or ""  # unused; auth is the local claude CLI login
        self.request_timeout = float(request_timeout or 120.0)

    def make_client(self):
        # Lazy import: the SDK is an optional dependency; only needed once this
        # backend is actually selected. Raises ClaudeAgentUnavailable if missing.
        from evi.llm.claude_agent import ClaudeAgentClient

        return ClaudeAgentClient()

    def list_models(self) -> list[ModelInfo]:
        return [
            ModelInfo(id=mid, backend=self.name, name=label, family="claude")
            for mid, label in _MODELS
        ]

    def supports_pull(self) -> bool:
        return False
