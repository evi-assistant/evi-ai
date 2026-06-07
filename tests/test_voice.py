"""Tests for the voice TTS wrapper (subprocess mocked)."""

from __future__ import annotations

import os
import subprocess

import pytest

import evi.voice as voice_mod
from evi.voice import VoiceError, detect_backend, speak


def test_detect_backend_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(os, "name", "nt")
    assert detect_backend() == "windows"


def test_detect_backend_linux_with_espeak_ng(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(os, "name", "posix")
    # No uname or non-Darwin uname.
    if hasattr(os, "uname"):
        monkeypatch.setattr(os, "uname", lambda: type("U", (), {"sysname": "Linux"})())
    monkeypatch.setattr(
        voice_mod.shutil, "which",
        lambda name: "/usr/bin/espeak-ng" if name == "espeak-ng" else None,
    )
    assert detect_backend() == "espeak-ng"


def test_detect_backend_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(os, "name", "posix")
    if hasattr(os, "uname"):
        monkeypatch.setattr(os, "uname", lambda: type("U", (), {"sysname": "Linux"})())
    monkeypatch.setattr(voice_mod.shutil, "which", lambda _: None)
    assert detect_backend() == "none"


def test_speak_raises_when_no_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(voice_mod, "detect_backend", lambda: "none")
    with pytest.raises(VoiceError, match="no TTS backend"):
        speak("hello")


def test_speak_runs_espeak_command(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict = {}

    monkeypatch.setattr(voice_mod, "detect_backend", lambda: "espeak-ng")

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, returncode=0)

    monkeypatch.setattr(voice_mod.subprocess, "run", fake_run)
    speak("hello", rate=180, blocking=True)
    assert captured["cmd"][0] == "espeak-ng"
    assert "hello" in captured["cmd"]
    assert "180" in captured["cmd"]


def test_speak_handles_empty_text(monkeypatch: pytest.MonkeyPatch) -> None:
    # No backend should be invoked for empty text.
    def boom():
        raise AssertionError("should not be called")
    monkeypatch.setattr(voice_mod, "detect_backend", boom)
    speak("")  # no-op


def test_listen_propagates_missing_deps(monkeypatch: pytest.MonkeyPatch) -> None:
    """When sounddevice/numpy aren't installed, surface a clear error."""
    import sys

    monkeypatch.setitem(sys.modules, "sounddevice", None)
    monkeypatch.setitem(sys.modules, "numpy", None)
    with pytest.raises(VoiceError, match="evi-assistant\\[stt\\]"):
        voice_mod.listen(duration=0.1)


def test_listen_records_and_transcribes(monkeypatch: pytest.MonkeyPatch) -> None:
    """End-to-end with fakes: stub sounddevice + numpy + WhisperModel."""
    import sys
    import types

    # Fake sounddevice.
    sd_mod = types.ModuleType("sounddevice")
    captured: dict = {}

    def fake_rec(frames, samplerate, channels, dtype):
        captured["frames"] = frames
        captured["samplerate"] = samplerate
        return _FakeArray()
    sd_mod.rec = fake_rec  # type: ignore[attr-defined]
    sd_mod.wait = lambda: None  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "sounddevice", sd_mod)

    # Fake numpy (just enough for the reshape + astype calls).
    np_mod = types.ModuleType("numpy")
    np_mod.float32 = float  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "numpy", np_mod)

    # Fake faster_whisper.
    class _Seg:
        def __init__(self, text):
            self.text = text

    class _Whisper:
        def __init__(self, *a, **kw):
            pass

        def transcribe(self, samples, **kw):
            return iter([_Seg("hello "), _Seg("world")]), None

    fw_mod = types.ModuleType("faster_whisper")
    fw_mod.WhisperModel = _Whisper  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "faster_whisper", fw_mod)
    # Bust the cache from previous tests.
    monkeypatch.setattr(voice_mod, "_WHISPER_MODEL", None)
    monkeypatch.setattr(voice_mod, "_WHISPER_MODEL_KEY", None)

    out = voice_mod.listen(duration=1.0)
    assert out == "hello world"
    assert captured["samplerate"] == 16000


class _FakeArray:
    """Stand-in for the numpy array sounddevice would return."""

    def reshape(self, _shape):
        return self

    def astype(self, _dtype):
        return self


def test_speak_nonblocking_uses_popen(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict = {}

    monkeypatch.setattr(voice_mod, "detect_backend", lambda: "espeak")

    class FakePopen:
        def __init__(self, cmd, **kwargs):
            captured["cmd"] = cmd

    monkeypatch.setattr(voice_mod.subprocess, "Popen", FakePopen)
    speak("hi", blocking=False)
    assert captured["cmd"][0] == "espeak"
