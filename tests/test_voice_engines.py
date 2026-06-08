"""Tests for the pluggable TTS engine layer (Phase 91).

The neural engines (coqui / f5 / piper) aren't installed in CI, so these
exercise the registry, routing, and the graceful "deps missing" error path
rather than real synthesis.
"""

from __future__ import annotations

import pytest

from evi import voice
from evi.config import Config, VoiceSettings


def test_engines_registry():
    assert voice.ENGINES == ("system", "coqui", "f5", "piper")


def test_available_engines_shape():
    avail = voice.available_engines()
    assert set(avail) == set(voice.ENGINES)
    assert all(isinstance(v, bool) for v in avail.values())


def test_unknown_engine_raises(tmp_path):
    with pytest.raises(voice.VoiceError):
        voice.synthesize("hi", tmp_path / "x.wav", engine="nope")


@pytest.mark.parametrize("engine", ["coqui", "f5", "piper"])
def test_missing_deps_raise_voiceerror(tmp_path, engine, monkeypatch):
    # Ensure the engine looks unavailable regardless of the test host.
    monkeypatch.setattr(voice.shutil, "which", lambda _name: None)
    monkeypatch.setattr(voice.importlib.util, "find_spec", lambda _name: None)
    with pytest.raises(voice.VoiceError):
        voice.synthesize("hello", tmp_path / "out.wav", engine=engine)


def test_speak_routes_to_engine(monkeypatch):
    # engine != system must go through synthesize(), not the platform path.
    called = {}

    def fake_synth(text, out, **kw):
        called["engine"] = kw.get("engine") or "?"
        called["text"] = text
        raise voice.VoiceError("stub: no engine")

    # synthesize is looked up at call time inside speak via module global.
    monkeypatch.setattr(voice, "synthesize", lambda text, out, **kw: fake_synth(text, out, **kw))
    with pytest.raises(voice.VoiceError):
        voice.speak("hi there", engine="coqui", model="m", clone_sample="ref.wav")
    assert called["text"] == "hi there"


def test_system_engine_unchanged(monkeypatch):
    # engine="system" should never touch synthesize().
    monkeypatch.setattr(voice, "detect_backend", lambda: "none")
    with pytest.raises(voice.VoiceError):
        voice.speak("hi", engine="system")


def test_voice_settings_defaults():
    vs = VoiceSettings()
    assert vs.engine == "system" and vs.language == "en"


def test_config_load_has_voice(tmp_path, monkeypatch):
    monkeypatch.setattr("evi.config.CONFIG_PATH", tmp_path / "config.toml")
    cfg = Config.load()  # writes defaults then reloads
    assert cfg.voice.engine == "system"
