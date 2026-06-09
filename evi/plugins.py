"""Plugins — installable bundles that extend eVi.

A plugin is a directory with a ``plugin.toml`` manifest that can bundle several
component types, each auto-discovered from a well-known sub-path:

    commands/     slash commands         -> /<plugin>:<command>
    skills/       skills (SKILL.md)      -> picked up by the skill loader
    hooks.toml    before/after-tool hooks (merged after the user's own)
    mcp.json      MCP servers            -> namespaced <plugin>:<name>
    agents.toml   subagent profiles      -> namespaced <plugin>:<name>

Installed plugins live under ``~/.evi/plugins/<name>/``; loaders scan each plugin
directory automatically, so install/remove is just managing that directory —
no copying into the user's own dirs, no clobber.

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

import json
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
    hooks: int = 0
    mcp: int = 0
    agents: int = 0


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


def _count_hooks(p: Path) -> int:
    """Count hook entries in a plugin's hooks.toml (0 if absent/malformed)."""
    if not p.is_file():
        return 0
    try:
        data = tomllib.loads(p.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return 0
    return sum(len(data.get(ev, []) or []) for ev in ("before_tool_call", "after_tool_call"))


def _count_mcp(p: Path) -> int:
    """Count server entries in a plugin's mcp.json (0 if absent/malformed)."""
    if not p.is_file():
        return 0
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return 0
    return len(data) if isinstance(data, list) else 0


def _count_agents(p: Path) -> int:
    """Count subagent profiles in a plugin's agents.toml (0 if absent/malformed)."""
    if not p.is_file():
        return 0
    try:
        data = tomllib.loads(p.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return 0
    return len(data.get("agent", []) or [])


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


def plugin_dirs(root: Path | None = None) -> list[Path]:
    """Installed plugin directories (each has a plugin.toml). Used by the hook
    and MCP loaders to pick up `<plugin>/hooks.toml` and `<plugin>/mcp.json`."""
    d = _plugins_dir(root)
    if not d.is_dir():
        return []
    return [p for p in sorted(d.iterdir()) if p.is_dir() and (p / "plugin.toml").is_file()]


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
                    hooks=_count_hooks(pd / "hooks.toml"),
                    mcp=_count_mcp(pd / "mcp.json"),
                    agents=_count_agents(pd / "agents.toml"),
                )
            )
    return out


def remove(name: str, root: Path | None = None) -> bool:
    dest = _plugins_dir(root) / _slug(name)
    if not dest.is_dir():
        return False
    shutil.rmtree(dest, ignore_errors=True)
    return True
