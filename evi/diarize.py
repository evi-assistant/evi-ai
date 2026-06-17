"""Speaker diarization — "who spoke when" on an audio file.

Layers on top of the existing STT pipeline (``evi/voice.py`` transcribes *what*
was said; this attributes *who*). Uses ``pyannote.audio``'s pretrained
speaker-diarization pipeline. Heavy (torch) and gated/licensed on Hugging Face,
so it's an optional extra — ``pip install 'evi-assistant[diarize]'`` — and most
pyannote models need an HF access token the first time (passed here or via the
``HF_TOKEN`` / ``HUGGINGFACE_TOKEN`` env var).

Lazy, cached pipeline per model id (mirrors ``evi/moderation.py``). Raises
``DiarizeError`` when the deps/model/token are missing so callers can degrade
gracefully (plain transcript without speaker labels).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

# pyannote's current pretrained pipeline. Override via [models] diarize.
DEFAULT_MODEL = "pyannote/speaker-diarization-3.1"

_PIPELINES: dict[str, Any] = {}


class DiarizeError(Exception):
    """Diarization deps / model / token aren't available."""


@dataclass
class Segment:
    """One contiguous span attributed to one speaker."""

    speaker: str
    start: float  # seconds
    end: float


def have_diarize() -> bool:
    """True if pyannote.audio is importable (deps installed)."""
    try:
        import pyannote.audio  # type: ignore[import-not-found]  # noqa: F401
    except Exception:
        return False
    return True


def _resolve_token(hf_token: str = "") -> str:
    return (
        hf_token
        or os.environ.get("HF_TOKEN", "")
        or os.environ.get("HUGGINGFACE_TOKEN", "")
        or os.environ.get("HUGGING_FACE_HUB_TOKEN", "")
    ).strip()


def _pipeline(model_id: str, hf_token: str = ""):
    if model_id in _PIPELINES:
        return _PIPELINES[model_id]
    try:
        from pyannote.audio import Pipeline  # type: ignore[import-not-found]
    except ImportError as exc:
        raise DiarizeError(
            "speaker diarization needs pyannote.audio — "
            "install with: pip install 'evi-assistant[diarize]'"
        ) from exc
    token = _resolve_token(hf_token)
    try:
        pipe = Pipeline.from_pretrained(model_id, use_auth_token=token or None)
    except Exception as exc:  # download / gated-model / token failure
        raise DiarizeError(
            f"could not load diarization model {model_id!r}: {exc} "
            "(most pyannote models are gated — accept the terms on Hugging Face "
            "and set HF_TOKEN)"
        ) from exc
    if pipe is None:
        raise DiarizeError(
            f"diarization model {model_id!r} failed to load — check your HF token "
            "and that you've accepted the model's terms on Hugging Face"
        )
    _PIPELINES[model_id] = pipe
    return pipe


def diarize(
    audio_path: str,
    model_id: str = "",
    *,
    hf_token: str = "",
    num_speakers: int | None = None,
) -> list[Segment]:
    """Return speaker segments for `audio_path`, sorted by start time.

    `num_speakers` pins the count when known (improves accuracy); leave None to
    let the model decide. Raises DiarizeError if deps/model/token are missing."""
    pipe = _pipeline(model_id or DEFAULT_MODEL, hf_token)
    kwargs: dict[str, Any] = {}
    if num_speakers and num_speakers > 0:
        kwargs["num_speakers"] = int(num_speakers)
    try:
        annotation = pipe(audio_path, **kwargs)
    except Exception as exc:
        raise DiarizeError(f"diarization failed on {audio_path!r}: {exc}") from exc
    segments = [
        Segment(speaker=str(label), start=float(turn.start), end=float(turn.end))
        for turn, _track, label in annotation.itertracks(yield_label=True)
    ]
    segments.sort(key=lambda s: s.start)
    return segments


def reset_for_tests() -> None:
    _PIPELINES.clear()
