"""Tests for the profile overlay system."""

from __future__ import annotations

from pathlib import Path

import pytest

import evi.profiles as profiles_mod
from evi.profiles import (
    ENV_VAR,
    active_profile_name,
    list_profiles,
    load_profile_overlay,
    merge_overlay,
)


def _redirect_profiles_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    monkeypatch.setattr(profiles_mod, "PROFILES_DIR", tmp_path)
    return tmp_path


def test_active_profile_name_reads_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(ENV_VAR, raising=False)
    assert active_profile_name() is None
    monkeypatch.setenv(ENV_VAR, "home")
    assert active_profile_name() == "home"
    monkeypatch.setenv(ENV_VAR, "   ")
    assert active_profile_name() is None


def test_list_profiles(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _redirect_profiles_dir(monkeypatch, tmp_path)
    assert list_profiles() == []
    (tmp_path / "home.toml").write_text("[llm]\n")
    (tmp_path / "away.toml").write_text("[llm]\n")
    (tmp_path / "notes.txt").write_text("ignored")
    assert list_profiles() == ["away", "home"]


def test_load_profile_overlay_happy(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _redirect_profiles_dir(monkeypatch, tmp_path)
    (tmp_path / "home.toml").write_text(
        '[llm]\nbackend = "openai_compat"\nbase_url = "http://srv:8000/v1"\n'
    )
    overlay = load_profile_overlay("home")
    assert overlay == {
        "llm": {"backend": "openai_compat", "base_url": "http://srv:8000/v1"}
    }


def test_load_profile_overlay_missing_returns_empty(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _redirect_profiles_dir(monkeypatch, tmp_path)
    assert load_profile_overlay("nope") == {}


def test_load_profile_overlay_malformed_returns_empty(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _redirect_profiles_dir(monkeypatch, tmp_path)
    (tmp_path / "broken.toml").write_text("not = valid = toml")
    assert load_profile_overlay("broken") == {}


def test_merge_overlay_deep_merge() -> None:
    base = {
        "llm": {"backend": "lmstudio", "model": "qwen2.5-7b", "temperature": 0.7},
        "tools": {"fs": True, "image": False},
    }
    overlay = {
        "llm": {"model": "qwen2.5-32b"},
        "tools": {"image": True, "mcp": True},
    }
    merged = merge_overlay(base, overlay)
    # Overrides land:
    assert merged["llm"]["model"] == "qwen2.5-32b"
    assert merged["tools"]["image"] is True
    assert merged["tools"]["mcp"] is True
    # Untouched fields preserved:
    assert merged["llm"]["backend"] == "lmstudio"
    assert merged["llm"]["temperature"] == 0.7
    assert merged["tools"]["fs"] is True


def test_merge_overlay_replaces_lists_wholesale() -> None:
    base = {"microsoft": {"scopes": ["Mail.Read", "User.Read"]}}
    overlay = {"microsoft": {"scopes": ["Calendars.Read"]}}
    merged = merge_overlay(base, overlay)
    assert merged["microsoft"]["scopes"] == ["Calendars.Read"]


def test_config_load_honors_profile(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """End-to-end: Config.load() picks up the active profile's overrides."""
    import evi.config as config_mod

    # Stand up a fake EVI_HOME so we don't trample the user's real config.
    home = tmp_path / "evi-home"
    home.mkdir()
    monkeypatch.setattr(config_mod, "HOME", home)
    monkeypatch.setattr(config_mod, "CONFIG_PATH", home / "config.toml")
    monkeypatch.setattr(profiles_mod, "PROFILES_DIR", home / "profiles")

    (home / "config.toml").write_text(
        '[llm]\nbackend = "lmstudio"\nmodel = "qwen2.5-7b-instruct"\n'
    )
    (home / "profiles").mkdir()
    (home / "profiles" / "home.toml").write_text(
        '[llm]\nbackend = "openai_compat"\nbase_url = "http://srv:8000/v1"\n'
    )
    monkeypatch.setenv(ENV_VAR, "home")

    cfg = config_mod.Config.load()
    assert cfg.llm.backend == "openai_compat"
    assert cfg.llm.base_url == "http://srv:8000/v1"
    # Model came from the base — profile didn't override it.
    assert cfg.llm.model == "qwen2.5-7b-instruct"
