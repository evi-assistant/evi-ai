"""Voice tools — let the agent speak responses aloud or transcribe the mic."""

from __future__ import annotations

from evi.tools.base import tool
from evi.voice import VoiceError, listen, speak


@tool(
    description=(
        "Speak the given text aloud using the local TTS engine. "
        "Use this when the user has asked you to read something out or "
        "when audio output is more appropriate than text. Returns 'ok' "
        "on success, an error string otherwise."
    ),
    category="voice",
)
def speak_text(text: str) -> str:
    if not text.strip():
        return "ERROR: empty text"
    try:
        speak(text, blocking=False)  # don't block the agent loop
    except VoiceError as exc:
        return f"ERROR: {exc}"
    return "ok"


@tool(
    description=(
        "Record from the default microphone for `duration` seconds and "
        "transcribe via local Whisper. Returns the recognised text. "
        "First call downloads the chosen model (~75 MB for tiny.en)."
    ),
    category="voice",
)
def transcribe_microphone(duration: float = 5.0, model: str = "tiny.en") -> str:
    try:
        return listen(duration=duration, model=model) or "(no speech detected)"
    except VoiceError as exc:
        return f"ERROR: {exc}"
