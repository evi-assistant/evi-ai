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
import tempfile
from dataclasses import dataclass
from pathlib import Path

import tomllib

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
    enabled: bool = True


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


def _is_url(source: str) -> bool:
    return source.startswith(("http://", "https://"))


def _looks_like_zip(source: str) -> bool:
    return source.lower().endswith(".zip")


def _looks_like_git(source: str) -> bool:
    return (
        source.startswith(("git@", "ssh://"))
        or source.endswith(".git")
        or (_is_url(source) and not _looks_like_zip(source))
    )


def _find_manifest_dir(base: Path) -> Path:
    """Locate the dir holding plugin.toml within an extracted archive — at the
    root, or nested one level (zips usually wrap everything in a top folder)."""
    if (base / "plugin.toml").is_file():
        return base
    for child in sorted(p for p in base.iterdir() if p.is_dir()):
        if (child / "plugin.toml").is_file():
            return child
    raise PluginError("no plugin.toml found in archive")


def _fetch_and_extract_zip(source: str, tmp: Path) -> Path:
    """Download (if a URL) and unzip `source` into `tmp`; return the plugin dir."""
    import zipfile

    if _is_url(source):
        import urllib.request

        zip_path = tmp / "plugin.zip"
        try:
            with urllib.request.urlopen(source, timeout=60) as resp:  # noqa: S310
                zip_path.write_bytes(resp.read())
        except OSError as exc:
            raise PluginError(f"download failed: {exc}") from exc
    else:
        zip_path = Path(source).expanduser()
        if not zip_path.is_file():
            raise PluginError(f"zip not found: {source}")
    dest = tmp / "unzipped"
    try:
        with zipfile.ZipFile(zip_path) as zf:
            zf.extractall(dest)
    except (OSError, zipfile.BadZipFile) as exc:
        raise PluginError(f"bad zip: {exc}") from exc
    return _find_manifest_dir(dest)


def install(source: str, name: str | None = None, root: Path | None = None) -> str:
    """Install a plugin from a local directory, a git URL, or a `.zip`
    (local file or http(s) URL). Returns its name."""
    tmp: Path | None = None
    try:
        if _looks_like_zip(source):
            tmp = Path(tempfile.mkdtemp(prefix="evi-plugin-"))
            plugin_src = _fetch_and_extract_zip(source, tmp)
        elif _looks_like_git(source):
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
                raise PluginError(f"not a directory, git URL, or zip: {source}")

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


_STARTER_COMMAND = """\
---
description: Example slash command from the {name} plugin
---

This is a starter command. Replace this body with the prompt you want
`/{name}:hello` to run. Anything the user types after the command name is
appended as `$ARGUMENTS`.
"""

_STARTER_SKILL = """\
---
name: {name}-example
description: Example skill from the {name} plugin — replace this description so the picker knows when to use it
---

# {name} example skill

Replace this with the skill's instructions. The frontmatter `description`
above is what the skill picker matches against, so make it specific.
"""


def init_plugin(
    name: str,
    dest_dir: Path | None = None,
    *,
    description: str = "",
    root: Path | None = None,
    install_now: bool = False,
) -> Path:
    """Scaffold a new plugin skeleton and return its directory.

    By default the skeleton is written to ``<dest_dir>/<slug>`` (``dest_dir``
    defaults to the current directory) so it can be developed and then
    ``evi plugin add``-ed. Pass ``install_now=True`` to scaffold straight into
    the installed-plugins dir (``~/.evi/plugins/<slug>``) so it's live at once.

    Creates ``plugin.toml`` plus a starter ``commands/hello.md`` and
    ``skills/<slug>-example/SKILL.md`` so the plugin works immediately.
    """
    slug = _slug(name)
    if not _NAME_RE.match(slug):
        raise PluginError(f"invalid plugin name {slug!r}")

    if install_now:
        dest = _plugins_dir(root) / slug
    else:
        base = Path(dest_dir).expanduser() if dest_dir is not None else Path.cwd()
        dest = base / slug
    if (dest / "plugin.toml").exists():
        raise PluginError(f"plugin already exists at {dest}")

    (dest / "commands").mkdir(parents=True, exist_ok=True)
    (dest / "skills" / f"{slug}-example").mkdir(parents=True, exist_ok=True)
    (dest / "plugin.toml").write_text(
        f'name = "{slug}"\n'
        f'description = "{description}"\n'
        'version = "0.1.0"\n'
        '# default_enabled = false   # uncomment to ship installed-but-off\n',
        encoding="utf-8",
    )
    (dest / "commands" / "hello.md").write_text(
        _STARTER_COMMAND.format(name=slug), encoding="utf-8"
    )
    (dest / "skills" / f"{slug}-example" / "SKILL.md").write_text(
        _STARTER_SKILL.format(name=slug), encoding="utf-8"
    )
    return dest


def _state_path(root: Path | None = None) -> Path:
    return _plugins_dir(root) / ".state.json"


def _load_state(root: Path | None = None) -> dict[str, bool]:
    """User enable/disable overrides ({name: bool}). Missing/bad file = {}."""
    import json

    try:
        data = json.loads(_state_path(root).read_text(encoding="utf-8"))
        return {str(k): bool(v) for k, v in data.items()} if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def is_enabled(name: str, root: Path | None = None) -> bool:
    """Whether a plugin is active: a user override wins, else the manifest's
    ``default_enabled`` (default True). Disabled plugins contribute no
    commands/skills/hooks/MCP/agents."""
    state = _load_state(root)
    if name in state:
        return state[name]
    try:
        return bool(_read_manifest(_plugins_dir(root) / _slug(name)).get("default_enabled", True))
    except PluginError:
        return True


def set_enabled(name: str, enabled: bool, root: Path | None = None) -> bool:
    """Persist an enable/disable override. False if no such installed plugin."""
    import json

    pd = _plugins_dir(root) / _slug(name)
    if not (pd / "plugin.toml").is_file():
        return False
    state = _load_state(root)
    state[_slug(name)] = enabled
    p = _state_path(root)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    return True


def plugin_dirs(root: Path | None = None) -> list[Path]:
    """ENABLED installed plugin directories (each has a plugin.toml). Used by the
    hook/MCP/command/skill/agent loaders — a disabled plugin is skipped so none
    of its content loads."""
    d = _plugins_dir(root)
    if not d.is_dir():
        return []
    state = _load_state(root)

    def _on(pd: Path) -> bool:
        if pd.name in state:
            return state[pd.name]
        try:
            return bool(_read_manifest(pd).get("default_enabled", True))
        except PluginError:
            return True

    return [
        p for p in sorted(d.iterdir())
        if p.is_dir() and (p / "plugin.toml").is_file() and _on(p)
    ]


def plugin_bin_dirs(root: Path | None = None) -> list[Path]:
    """`bin/` directories of enabled plugins that exist (for PATH)."""
    return [b for pd in plugin_dirs(root) if (b := pd / "bin").is_dir()]


def activate_plugin_bins(root: Path | None = None) -> list[str]:
    """Prepend enabled plugins' `bin/` dirs to PATH (idempotent). Returns the
    dirs added this call. Lets a plugin ship executables its commands/skills
    can invoke. Mirrors Claude Code adding plugin `bin/` to PATH."""
    import os

    added: list[str] = []
    sep = os.pathsep
    current = os.environ.get("PATH", "")
    entries = current.split(sep) if current else []
    for b in plugin_bin_dirs(root):
        s = str(b)
        if s not in entries:
            entries.insert(0, s)
            added.append(s)
    if added:
        os.environ["PATH"] = sep.join(entries)
    return added


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
            nskill = len(list(sdir.rglob("SKILL.md"))) if sdir.is_dir() else 0
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
                    enabled=is_enabled(pd.name, root),
                )
            )
    return out


def remove(name: str, root: Path | None = None) -> bool:
    dest = _plugins_dir(root) / _slug(name)
    if not dest.is_dir():
        return False
    shutil.rmtree(dest, ignore_errors=True)
    return True
