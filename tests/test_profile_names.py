"""Profile names must be plain file stems (1.0.53 hotfix).

`evi profile remove ../config` used to delete ~/.evi/config.toml, and
`profile add` / `show` could write or read any `.toml` reachable by a relative
path. profile_path() now refuses anything that isn't a simple name."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

import evi.apps.cli.main as cli_main
import evi.profiles as profiles_mod
from evi.profiles import (
    ProfileNameError,
    load_profile_overlay,
    profile_path,
    valid_profile_name,
)

GOOD = ["home", "work-laptop", "gpu_box", "home.lan", "A1", "x" * 64]
BAD = [
    "", ".", "..", "../config", "..\\config", "a/b", "a\\b", "/abs", "C:\\x",
    "C:x", "\\\\server\\share", "a..b", "trailing.", ".hidden", "-flag",
    "a b", "x\x00y", "x" * 65,
]


@pytest.mark.parametrize("name", GOOD)
def test_plain_names_are_valid(name: str) -> None:
    assert valid_profile_name(name)


@pytest.mark.parametrize("name", BAD)
def test_path_like_names_are_refused(name: str) -> None:
    assert not valid_profile_name(name)
    with pytest.raises(ProfileNameError):
        profile_path(name)


def test_valid_name_stays_under_profiles_dir(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(profiles_mod, "PROFILES_DIR", tmp_path / "profiles")
    p = profile_path("home.lan")
    assert p.parent == tmp_path / "profiles"
    assert p.name == "home.lan.toml"


def test_bad_active_profile_env_is_ignored(monkeypatch, tmp_path: Path) -> None:
    home = tmp_path / "home"
    (home / "profiles").mkdir(parents=True)
    (home / "secret.toml").write_text('[llm]\nbase_url = "http://evil"\n', encoding="utf-8")
    monkeypatch.setattr(profiles_mod, "PROFILES_DIR", home / "profiles")
    monkeypatch.setenv(profiles_mod.ENV_VAR, "../secret")
    assert load_profile_overlay() == {}


def _cli_home(monkeypatch, tmp_path: Path) -> Path:
    home = tmp_path / "home"
    profiles = home / "profiles"
    profiles.mkdir(parents=True)
    (home / "config.toml").write_text("# sentinel\n", encoding="utf-8")
    monkeypatch.setattr(profiles_mod, "PROFILES_DIR", profiles)
    monkeypatch.setattr(cli_main, "PROFILES_DIR", profiles)
    monkeypatch.setattr(cli_main, "ensure_dirs", lambda: None)
    return home


@pytest.mark.parametrize("name", ["../config", "..\\config"])
def test_cli_remove_refuses_traversal(monkeypatch, tmp_path: Path, name: str) -> None:
    home = _cli_home(monkeypatch, tmp_path)
    result = CliRunner().invoke(cli_main.app, ["profile", "remove", name])
    assert result.exit_code == 1
    assert "invalid profile name" in result.output
    assert (home / "config.toml").is_file()


def test_cli_add_refuses_traversal(monkeypatch, tmp_path: Path) -> None:
    home = _cli_home(monkeypatch, tmp_path)
    result = CliRunner().invoke(
        cli_main.app, ["profile", "add", "../pwned", "--model", "m"]
    )
    assert result.exit_code == 1
    assert not (home / "pwned.toml").exists()


def test_cli_add_and_remove_plain_name(monkeypatch, tmp_path: Path) -> None:
    home = _cli_home(monkeypatch, tmp_path)
    added = CliRunner().invoke(cli_main.app, ["profile", "add", "work", "--model", "m"])
    assert added.exit_code == 0, added.output
    assert (home / "profiles" / "work.toml").is_file()
    removed = CliRunner().invoke(cli_main.app, ["profile", "remove", "work"])
    assert removed.exit_code == 0, removed.output
    assert not (home / "profiles" / "work.toml").exists()
    assert (home / "config.toml").is_file()
