"""Tests for --safe-mode (EVI_SAFE_MODE): disable all customizations."""

from __future__ import annotations


def _isolate(monkeypatch, tmp_path):
    monkeypatch.setattr("evi.config.HOME", tmp_path)
    monkeypatch.setattr("evi.config.CONFIG_PATH", tmp_path / "config.toml")


def test_safe_mode_env_parsing(monkeypatch):
    from evi.apps.cli.main import _safe_mode

    for v in ("1", "true", "yes", "on", "TRUE"):
        monkeypatch.setenv("EVI_SAFE_MODE", v)
        assert _safe_mode() is True, v
    for v in ("0", "", "no", "off"):
        monkeypatch.setenv("EVI_SAFE_MODE", v)
        assert _safe_mode() is False, v


def test_safe_mode_disables_customizations(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setenv("EVI_SAFE_MODE", "1")
    from evi.apps.cli.main import _build_agent

    a = _build_agent(register=False)
    # every customization channel is off
    assert a.project is None
    assert a.memory is None
    assert a.skills is None
    assert a.hooks is None
    assert a.guardrails is None


def test_normal_mode_loads_customizations(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    monkeypatch.delenv("EVI_SAFE_MODE", raising=False)
    from evi.apps.cli.main import _build_agent

    a = _build_agent(register=False)
    # the defaults (memory + hooks) load when not in safe mode
    assert a.memory is not None
    assert a.hooks is not None
