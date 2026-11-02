"""Reasoning-model capability check.

Some backends (notably Ollama) treat an OpenAI ``reasoning_effort`` request as a
"thinking" request and **reject it with a 400** for models that don't support
thinking (e.g. ``qwen2.5:3b`` → ``"qwen2.5:3b" does not support thinking``),
while reasoning models (DeepSeek-R1, Qwen3, o-series) accept it. So we only
forward ``reasoning_effort`` when the active model looks like a reasoning model —
mirroring :func:`evi.vision.model_supports_vision` /
:func:`evi.audio_input.model_supports_audio`.
"""

from __future__ import annotations

import re
from typing import Any, Mapping

# Substring hints for reasoning-capable model ids (matched case-insensitively).
_REASONING_HINTS = (
    "r1",                 # deepseek-r1, deepseek-r1-distill-*
    "deepseek-reasoner",
    "qwq",                # Qwen QwQ
    "qwen3",              # Qwen3 (thinking) — NB: qwen2.5 does NOT support it
    "magistral",          # Mistral Magistral
    "reasoning",          # phi-4-reasoning, phi-4-mini-reasoning, …
    "thinking",           # explicitly-named thinking builds
    "gpt-5",              # OpenAI reasoning-by-default
    "gpt-oss",            # OpenAI open reasoning models
)

# o-series ids (o1 / o3 / o4-mini[-…]) on a token boundary so we don't match a
# stray "o1" inside an unrelated name.
_OSERIES_RE = re.compile(r"(?:^|[\s:/_-])o[134](?:-[a-z0-9.]+)?(?:$|[\s:/_-])")


def model_supports_reasoning(model_id: str) -> bool:
    """Heuristic: does this model id accept a ``reasoning_effort`` / thinking
    request? Conservative — unknown/most local models return False so we never
    send thinking to a model that 400s on it."""
    if not model_id:
        return False
    name = model_id.lower()
    if any(hint in name for hint in _REASONING_HINTS):
        return True
    return bool(_OSERIES_RE.search(name))


# Values that mean "don't think at all".
_OFF = ("off", "none", "false", "0", "disabled")

# eVi effort levels with no `reasoning_effort` value of the same name: OpenAI
# takes minimal/low/medium/high, and Ollama and the harmony templates
# low/medium/high, so "max" (`/effort max`, `/effort ultracode`) asks for the
# highest level they share rather than a 400.
_EFFORT_ALIASES = {"max": "high"}


def reasoning_extra_body(effort: str, model_id: str) -> dict:
    """Request-body fragment for the configured ``reasoning_effort``.

    Centralizes the thinking knob so the agent loop just merges the result:

    - non-reasoning model → ``{}`` (never send thinking; Ollama 400s on it)
    - "off"/"none"        → ``{"think": False}`` to disable thinking on a model
      that would otherwise think by default (Ollama reads top-level ``think``;
      other backends ignore the unknown key). Mirrors Claude Code's
      "turn extended thinking off".
    - "medium" (default)  → ``{}`` (let the model use its own default)
    - low/high/…          → ``{"reasoning_effort": <value>}``
    - "max"               → ``{"reasoning_effort": "high"}`` (the highest level
      backends accept; see _EFFORT_ALIASES)
    """
    effort = (effort or "").strip().lower()
    if not model_supports_reasoning(model_id):
        return {}
    if effort in _OFF:
        return {"think": False}
    effort = _EFFORT_ALIASES.get(effort, effort)
    if effort and effort != "medium":
        return {"reasoning_effort": effort}
    return {}


# The fields a server sends a model's reasoning in, beside `content`, in a
# streamed delta or a whole message: `reasoning_content` (DeepSeek's API,
# llama.cpp's --reasoning-format deepseek, LM Studio, vLLM before 0.16) and
# `reasoning` (vLLM 0.16 on, Ollama's /v1, OpenRouter, LM Studio for gpt-oss).
REASONING_FIELDS = ("reasoning_content", "reasoning")


def reasoning_text(obj: Any) -> str:
    """The reasoning text a streamed delta or a whole message carries outside
    its `content`: the first of REASONING_FIELDS holding a non-empty string,
    read as a key of a mapping, else as an attribute, else from pydantic's
    `model_extra` (where the openai SDK keeps fields it doesn't declare).
    Only the first one counts, since some servers send the same text under
    both names. Anything else (None, no such field, a non-string value such
    as a reasoning object) is ""."""
    if obj is None:
        return ""
    extra = getattr(obj, "model_extra", None)
    for name in REASONING_FIELDS:
        if isinstance(obj, Mapping):
            value = obj.get(name)
        else:
            value = getattr(obj, name, None)
            if value is None and isinstance(extra, Mapping):
                value = extra.get(name)
        if isinstance(value, str) and value:
            return value
    return ""
