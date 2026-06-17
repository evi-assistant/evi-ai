"""Dedicated safety-guard model — Llama Guard / ShieldGemma style.

A *guard model* is a small model fine-tuned to classify a turn against a fixed
safety taxonomy (MLCommons hazard categories) and answer "safe" / "unsafe". It's
distinct from the other two guardrail layers:

- ``[[judge]]`` reuses the **main** chat model with a free-form ``policy`` prompt.
- ``[[classifier]]`` uses a HuggingFace **text-classification head** (toxic-bert).
- ``[[guard]]`` (this) uses a **dedicated generative guard model** served over the
  normal OpenAI chat schema — e.g. ``llama-guard3`` or ``shieldgemma`` pulled in
  Ollama — with its safety template built in. You enable it; you don't write a
  policy.

The model id comes from ``[models] guard`` (see :class:`evi.config.SpecialtyModels`)
and is reached through :class:`evi.llm.specialty.SpecialtyRegistry` so it can run
on a separate backend without unloading the chat model. Errors raise so the
guardrail layer can catch and fail *open* — a flaky guard never wedges a turn.
"""

from __future__ import annotations

from typing import Any

# Substring hints for known dedicated guard / safety-classifier model families.
# Matched against the lowercased model id. Used for the 🛡 capability chip and to
# warn when a guard model is mistakenly picked as the main chat model.
_GUARD_HINTS = (
    "llama-guard", "llama_guard", "llamaguard",
    "shieldgemma", "shield-gemma",
    "prompt-guard", "promptguard",
    "granite-guardian", "granite3-guardian",
    "nemoguard", "wildguard", "aegis", "md-judge", "beavertails",
    "duoguard", "polyguard",
)


class GuardError(Exception):
    """The guard model isn't available / the call failed."""


def model_is_guard(model_id: str) -> bool:
    """Heuristic: is this model id a dedicated safety-guard model?"""
    if not model_id:
        return False
    return any(h in model_id.lower() for h in _GUARD_HINTS)


def _parse_verdict(text: str) -> tuple[bool, str]:
    """Parse a guard model's reply → (allowed, reason).

    Guard models answer with ``safe`` or ``unsafe`` (Llama Guard puts the
    violated category codes, e.g. ``S1,S10``, on the next line; ShieldGemma
    answers ``Yes``/``No``). Default to *allowed* when the verdict is unclear so
    the layer fails open."""
    body = (text or "").strip()
    if not body:
        return True, ""
    first = body.splitlines()[0].strip().lower()
    # Llama Guard / generic
    if first.startswith("unsafe"):
        cats = body.splitlines()[1].strip() if len(body.splitlines()) > 1 else ""
        return False, f"unsafe {cats}".strip()
    if first.startswith("safe"):
        return True, ""
    # ShieldGemma: "Yes" = violates policy, "No" = safe.
    if first.startswith("yes"):
        return False, "policy violation"
    if first.startswith("no"):
        return True, ""
    return True, ""  # unknown shape → fail open


def classify_safety(
    model_id: str,
    text: str,
    *,
    role: str = "user",
    registry: Any = None,
    llm: Any = None,
) -> tuple[bool, str]:
    """Classify `text` with a guard model → (allowed, reason).

    `role` is "user" for input checks / "assistant" for output checks — guard
    models evaluate the last turn, so the role matters. The client comes from
    `registry.client_for("guard")` when a SpecialtyRegistry is given (honours
    `[models] guard_base_url`/`guard_backend`), else a client built from `llm`
    with the model swapped in. Raises GuardError on any failure."""
    mid = (model_id or "").strip()
    if not mid:
        raise GuardError("no guard model configured ([models] guard)")
    client = None
    if registry is not None:
        client = registry.client_for("guard")
    if client is None:
        if llm is None:
            raise GuardError("no client available for the guard model")
        from dataclasses import replace

        from evi.llm.client import make_client

        client = make_client(replace(llm, model=mid))
    try:
        resp = client.chat.completions.create(
            model=mid,
            messages=[{"role": role if role in ("user", "assistant") else "user",
                       "content": text}],
            temperature=0.0,
            max_tokens=64,
            stream=False,
        )
        out = (resp.choices[0].message.content or "") if resp.choices else ""
    except Exception as exc:  # network / model / schema error
        raise GuardError(f"guard model call failed: {exc}") from exc
    return _parse_verdict(out)
