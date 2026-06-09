"""Offline moderation classifier — scores text with a local HF model.

Backs the `[[classifier]]` guardrail rule type. Loads a HuggingFace
text-classification model lazily and caches it per model id (first call may
download weights). Fully offline after that. Optional — install
`evi-assistant[moderation]` (transformers + torch).

`classify(model_id, text)` returns ``{label: score}`` for all labels; the
guardrail layer decides what to block. Raises `ModerationError` if the deps
aren't installed, which the guardrail catches and fails *open*.
"""

from __future__ import annotations

from typing import Any

# A small, widely-used toxicity model: labels are all "negative"
# (toxic / severe_toxic / obscene / threat / insult / identity_hate), so a
# bare `[[classifier]]` with no `labels` blocks sensibly on any of them.
DEFAULT_MODEL = "unitary/toxic-bert"

_PIPELINES: dict[str, Any] = {}


class ModerationError(Exception):
    """The moderation deps/model aren't available."""


def _pipeline(model_id: str):
    if model_id in _PIPELINES:
        return _PIPELINES[model_id]
    try:
        from transformers import pipeline  # type: ignore[import-not-found]
    except ImportError as exc:
        raise ModerationError(
            "classifier guardrails need transformers + torch — "
            "install with: pip install 'evi-assistant[moderation]'"
        ) from exc
    try:
        pipe = pipeline("text-classification", model=model_id, top_k=None)
    except Exception as exc:  # model download / load failure
        raise ModerationError(f"could not load moderation model {model_id!r}: {exc}") from exc
    _PIPELINES[model_id] = pipe
    return pipe


def classify(model_id: str, text: str) -> dict[str, float]:
    """Return {label: score} for `text` from the model (or the default)."""
    pipe = _pipeline(model_id or DEFAULT_MODEL)
    out = pipe(text[:2000])  # cap input; long inputs aren't needed to flag
    # transformers returns a list of dicts for one input (top_k=None), or a
    # nested list for a batch — normalise both.
    rows = out[0] if (out and isinstance(out[0], list)) else out
    return {
        str(d["label"]): float(d["score"])
        for d in rows
        if isinstance(d, dict) and "label" in d and "score" in d
    }


def reset_for_tests() -> None:
    _PIPELINES.clear()
