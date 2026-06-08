"""Plugins — installable bundles that extend eVi.

A plugin is a directory with a ``plugin.toml`` manifest and a ``commands/``
folder of slash commands (more component types — skills, hooks, subagent
profiles — are a planned follow-up). Installed plugins live under
``~/.evi/plugins/<name>/``; the command loader scans each plugin's ``commands/``
automatically (exposed as ``/<plugin>:<command>``), so install/remove is just
managing the plugin directory — no copying into the user's own dirs, no clobber.

    plugin.toml:
        name = "git-helpers"
        description = "Handy git slash commands"
        version = "0.1.0"

Install from a local directory or a git URL:

    evi plugin add ./my-plugin
    evi plugin add https://github.com/you/evi-git-helpers.git

All functions take an optional ``root`` (the eVi home) for tests.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover
    import tomli as tomllib

import re

import evi.config as config

_NAME_RE = re.compile(r"^[A-Za-z0-9_\-]+$")


class PluginError(Exception):
    """A plugin is missing, malformed, or couldn't be installed."""


@dataclass
class Plugin:
    name: str
    description: str
    version: str
    path: Path
    commands: int
    skills: int = 0


def _plugins_dir(root: Path | None = None) -> Path:
    return (root if root is not None else config.HOME) / "plugins"


def _slug(name: str) -> str:
    return Path(name).name.removesuffix(".git")


def _read_manifest(d: Path) -> dict:
    m = d / "plugin.toml"
    if not m.is_file():
        raise PluginError(f"no plugin.toml in {d}")
    try:
        return tomllib.loads(m.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise PluginError(f"bad plugin.toml: {exc}") from exc


def _looks_like_git(source: str) -> bool:
    return (
        source.startswith(("http://", "https://", "git@", "ssh://"))
        or source.endswith(".git")
    )


def install(source: str, name: str | None = None, root: Path | None = None) -> str:
    """Install a plugin from a local directory or a git URL. Returns its name."""
    tmp: Path | None = None
    try:
        if _looks_like_git(source):
            tmp = Path(tempfile.mkdtemp(prefix="evi-plugin-"))
            res = subprocess.run(
                ["git", "clone", "--depth", "1", source, str(tmp)],
                capture_output=True, text=True,
            )
            if res.returncode != 0:
                raise PluginError("git clone failed:\n" + (res.stderr or res.stdout))
            plugin_src = tmp
        else:
            plugin_src = Path(source).expanduser()
            if not plugin_src.is_dir():
                raise PluginError(f"not a directory or git URL: {source}")

        manifest = _read_manifest(plugin_src)
        pname = _slug(name or str(manifest.get("name") or plugin_src.name))
        if not _NAME_RE.match(pname):
            raise PluginError(f"invalid plugin name {pname!r}")

        dest = _plugins_dir(root) / pname
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists():
            shutil.rmtree(dest)
        shutil.copytree(plugin_src, dest, ignore=shutil.ignore_patterns(".git"))
        return pname
    finally:
        if tmp is not None:
            shutil.rmtree(tmp, ignore_errors=True)


def list_plugins(root: Path | None = None) -> list[Plugin]:
    d = _plugins_dir(root)
    out: list[Plugin] = []
    if d.is_dir():
        for pd in sorted(p for p in d.iterdir() if p.is_dir()):
            try:
                m = _read_manifest(pd)
            except PluginError:
                continue
            cdir = pd / "commands"
            ncmd = len(list(cdir.rglob("*.md"))) if cdir.is_dir() else 0
            sdir = pd / "skills"
            nskill = len([p for p in sdir.iterdir() if (p / "SKILL.md").is_file()]) if sdir.is_dir() else 0
            out.append(
                Plugin(
                    name=pd.name,
                    description=str(m.get("description", "")),
                    version=str(m.get("version", "")),
                    path=pd,
                    commands=ncmd,
                    skills=nskill,
                )
            )
    return out


def remove(name: str, root: Path | None = None) -> bool:
    dest = _plugins_dir(root) / _slug(name)
    if not dest.is_dir():
        return False
    shutil.rmtree(dest, ignore_errors=True)
    return True
