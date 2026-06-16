"""Named presets for online, OpenAI-compatible model providers.

eVi ships local-first defaults (lmstudio/ollama/llamacpp); these presets make the
common *online* gateways one command away. Each resolves to the ``openai_compat``
backend + the provider's ``base_url`` + the name of the env var that holds the
API key — so the secret stays in your environment, not in ``config.toml``
(``api_key = "env:OPENROUTER_API_KEY"``; resolved at client-build time).

Native-protocol providers are deliberately out of scope: the Anthropic preset
targets Anthropic's **OpenAI-compatible** endpoint, not the native Messages API
(which isn't OpenAI-shaped and would need a dedicated backend).
"""

from __future__ import annotations

import os
from dataclasses import dataclass

ENV_KEY_PREFIX = "env:"


@dataclass(frozen=True)
class OnlinePreset:
    name: str
    base_url: str
    api_key_env: str          # env var holding the API key
    default_model: str = ""   # a sensible starting model (override with --model)
    api: str = "chat"         # "chat" or "responses" (OpenAI only)
    note: str = ""


# All resolve to backend kind = openai_compat. Default models are starting
# points (provider model ids drift) — set your own with `--model`.
ONLINE_PRESETS: dict[str, OnlinePreset] = {
    "openrouter": OnlinePreset(
        "openrouter", "https://openrouter.ai/api/v1", "OPENROUTER_API_KEY",
        note="Pick a model slug, e.g. anthropic/claude-3.5-sonnet.",
    ),
    "openai": OnlinePreset(
        "openai", "https://api.openai.com/v1", "OPENAI_API_KEY",
        default_model="gpt-4o",
        note="Set [llm] api=responses to use the Responses API + server tools.",
    ),
    "xai": OnlinePreset(
        "xai", "https://api.x.ai/v1", "XAI_API_KEY", default_model="grok-2-latest",
    ),
    "anthropic": OnlinePreset(
        "anthropic", "https://api.anthropic.com/v1/", "ANTHROPIC_API_KEY",
        default_model="claude-sonnet-4-5",
        note="OpenAI-compatible endpoint (NOT the native Messages API).",
    ),
    "groq": OnlinePreset(
        "groq", "https://api.groq.com/openai/v1", "GROQ_API_KEY",
        default_model="llama-3.3-70b-versatile",
    ),
    "together": OnlinePreset(
        "together", "https://api.together.xyz/v1", "TOGETHER_API_KEY",
    ),
}


def get_preset(name: str) -> OnlinePreset | None:
    return ONLINE_PRESETS.get((name or "").strip().lower())


def resolve_api_key(api_key: str) -> str:
    """Resolve an ``api_key`` value: an ``env:VARNAME`` reference reads the named
    environment variable (empty if unset); anything else is returned verbatim.
    Keeps provider secrets out of plaintext config when the user opts in."""
    if api_key and api_key.startswith(ENV_KEY_PREFIX):
        return os.environ.get(api_key[len(ENV_KEY_PREFIX):], "")
    return api_key
