"""Audio input — attach audio clips to chat turns for omni-capable models.

OpenAI's audio-input schema (used by `gpt-4o-audio` and mirrored by local
omni models like Qwen2.5-Omni and MiniCPM-o served via vLLM):

    {
        "role": "user",
        "content": [
            {"type": "text", "text": "what is said in this clip?"},
            {
                "type": "input_audio",
                "input_audio": {"data": "<base64>", "format": "wav"},
            },
        ],
    }

For models that DON'T accept audio natively, the agent degrades by running
the clip through local Whisper (the same `evi.voice` STT path) and folding
the transcript into the text — so "talk about this clip" still works
everywhere, just with one extra step.
"""

from __future__ import annotations

import base64
from pathlib import Path
from typing import Iterable


# Substrings in a model id that indicate native audio input.
_AUDIO_HINTS = (
    "omni",            # qwen2.5-omni, qwen2-omni
    "audio",           # gpt-4o-audio, *-audio-*
    "minicpm-o",       # MiniCPM-o is omni (audio + vision)
    "qwen2.5-omni",
    "qwen2-audio",
)

# Extension → OpenAI `format` value. OpenAI documents wav + mp3; vLLM omni
# serving accepts more. Anything not here is skipped (we can't label it).
_AUDIO_FORMATS = {
    ".wav": "wav",
    ".mp3": "mp3",
    ".flac": "flac",
    ".m4a": "m4a",
    ".ogg": "ogg",
    ".webm": "webm",
}


def model_supports_audio(model_id: str) -> bool:
    """Heuristic: does this model id look like it accepts audio input?"""
    if not model_id:
        return False
    name = model_id.lower()
    return any(hint in name for hint in _AUDIO_HINTS)


def _audio_part(path: Path) -> dict | None:
    fmt = _AUDIO_FORMATS.get(path.suffix.lower())
    if fmt is None:
        return None
    try:
        data = path.read_bytes()
    except OSError:
        return None
    return {
        "type": "input_audio",
        "input_audio": {
            "data": base64.b64encode(data).decode("ascii"),
            "format": fmt,
        },
    }


def build_audio_content(text: str, audio_paths: Iterable[str | Path]) -> list[dict]:
    """Return an OpenAI-style multipart content list with input_audio parts.

    Skips missing files and unsupported extensions so a stale reference
    doesn't break the turn. Always returns at least the text part.
    """
    parts: list[dict] = [{"type": "text", "text": text}]
    for raw in audio_paths:
        p = Path(raw).expanduser()
        if not p.is_file():
            continue
        part = _audio_part(p)
        if part is not None:
            parts.append(part)
    return parts


def transcribe_for_fallback(audio_paths: Iterable[str | Path]) -> str:
    """Transcribe clips via local Whisper for non-audio models.

    Returns a text block summarising each clip's transcript, or a note that
    transcription was unavailable. Never raises — STT being absent must not
    break a chat turn.
    """
    lines: list[str] = []
    for raw in audio_paths:
        p = Path(raw).expanduser()
        if not p.is_file():
            lines.append(f"[audio {raw}: file not found]")
            continue
        try:
            from evi.voice import transcribe_wav

            text = transcribe_wav(p)
            lines.append(f"[audio {p.name} transcript] {text}" if text else f"[audio {p.name}: no speech detected]")
        except Exception as exc:  # noqa: BLE001  (VoiceError / missing dep / decode)
            lines.append(f"[audio {p.name}: transcription unavailable ({type(exc).__name__})]")
    return "\n".join(lines)
