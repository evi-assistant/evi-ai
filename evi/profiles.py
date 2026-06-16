"""Profiles — partial config overlays that shadow `config.toml`.

Use case: same install, different machines. On the laptop you want one
config when you're at home (`base_url=http://ai-server:8000/v1`) and a
different one when you're away (`base_url=http://localhost:1234/v1`).
Rather than juggle two config files, drop a partial TOML in
`~/.evi/profiles/<name>.toml` and select it with `EVI_PROFILE=<name>` or
the `--profile <name>` CLI flag.

A profile is *partial*: only the sections/fields you include override the
base. Missing sections inherit completely from `config.toml`. This is the
behavior that makes the feature actually useful — most settings are the
same across machines.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import tomllib

from evi.config import HOME


PROFILES_DIR = HOME / "profiles"
ENV_VAR = "EVI_PROFILE"


def active_profile_name() -> str | None:
    """Return the current `EVI_PROFILE` env var, or None when unset."""
    name = os.environ.get(ENV_VAR, "").strip()
    return name or None


def profile_path(name: str) -> Path:
    return PROFILES_DIR / f"{name}.toml"


def list_profiles() -> list[str]:
    if not PROFILES_DIR.is_dir():
        return []
    return sorted(p.stem for p in PROFILES_DIR.glob("*.toml"))


def load_profile_overlay(name: str | None = None) -> dict[str, Any]:
    """Return the profile's parsed TOML as a dict (empty if none active)."""
    name = name or active_profile_name()
    if not name:
        return {}
    path = profile_path(name)
    if not path.is_file():
        return {}
    try:
        with path.open("rb") as f:
            data = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def merge_overlay(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    """Deep-merge `overlay` into `base`. Overlay wins at each leaf.

    Lists are replaced wholesale (not concatenated) so profiles can fully
    override e.g. `microsoft.scopes` instead of accidentally appending.
    """
    out: dict[str, Any] = {k: v for k, v in base.items()}
    for k, v in overlay.items():
        if (
            k in out
            and isinstance(out[k], dict)
            and isinstance(v, dict)
        ):
            out[k] = merge_overlay(out[k], v)
        else:
            out[k] = v
    return out
