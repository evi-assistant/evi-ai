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
    assert voice.ENGINES == ("system", "coqui", "f5", "piper", "kokoro")


def test_available_engines_shape():
    avail = voice.available_engines()
    assert set(avail) == set(voice.ENGINES)
    assert all(isinstance(v, bool) for v in avail.values())


def test_unknown_engine_raises(tmp_path):
    with pytest.raises(voice.VoiceError):
        voice.synthesize("hi", tmp_path / "x.wav", engine="nope")


@pytest.mark.parametrize("engine", ["coqui", "f5", "piper", "kokoro"])
def test_missing_deps_raise_voiceerror(tmp_path, engine, monkeypatch):
    # Ensure the engine looks unavailable regardless of the test host.
    monkeypatch.setattr(voice.shutil, "which", lambda _name: None)
    monkeypatch.setattr(voice.importlib.util, "find_spec", lambda _name: None)
    with pytest.raises(voice.VoiceError):
        voice.synthesize("hello", tmp_path / "out.wav", engine=engine)


def test_kokoro_in_available_engines():
    assert "kokoro" in voice.available_engines()


def test_write_wav_int16_roundtrip(tmp_path):
    import wave

    import numpy as np

    out = tmp_path / "t.wav"
    sig = np.array([0.0, 0.5, -0.5, 1.0, -1.0], dtype="float32")
    voice._write_wav_int16(out, sig, 24000)
    with wave.open(str(out), "rb") as w:
        assert w.getframerate() == 24000 and w.getnchannels() == 1
        assert w.getsampwidth() == 2 and w.getnframes() == 5


def test_default_stt_model_reads_config(tmp_path, monkeypatch):
    import evi.config as config_mod

    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text("[models]\nstt = \"large-v3-turbo\"\n", encoding="utf-8")
    monkeypatch.setattr(config_mod, "CONFIG_PATH", cfg_path)
    monkeypatch.setattr(config_mod, "HOME", tmp_path)
    assert voice._default_stt_model() == "large-v3-turbo"


def test_default_stt_model_falls_back(tmp_path, monkeypatch):
    import evi.config as config_mod

    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text("[models]\nstt = \"\"\n", encoding="utf-8")
    monkeypatch.setattr(config_mod, "CONFIG_PATH", cfg_path)
    monkeypatch.setattr(config_mod, "HOME", tmp_path)
    assert voice._default_stt_model() == "tiny.en"


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
