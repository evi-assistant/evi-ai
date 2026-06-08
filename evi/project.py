"""Project context — auto-loaded `EVI.md` from the current working tree.

Mirrors how Claude Code uses `CLAUDE.md`: a markdown file checked into the
project that gives the agent durable, project-specific context (coding
conventions, where things live, terminology). Loaded from the nearest
ancestor of `cwd` that contains one, so you get sensible per-project
behavior just by `cd`ing into the right directory.

The file is read once at agent construction. If you edit `EVI.md` in a
long-running session, `Agent.reset()` re-reads it.

Size cap: 64 KB. Large project docs should be split and referenced; the
goal is "always-on context", not "stuff every README into every turn".
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover
    import tomli as tomllib


# EVI.md is eVi's own; AGENTS.md is the emerging cross-tool standard. EVI.md
# wins when both exist in the same directory.
PROJECT_FILENAMES = ("EVI.md", "evi.md", "AGENTS.md", "agents.md")
PROJECT_CONFIG_FILENAME = ".evi.toml"  # per-project config overlay
_MAX_BYTES = 64 * 1024


@dataclass(frozen=True)
class ProjectContext:
    path: Path
    content: str

    def format_for_prompt(self) -> str:
        """Render as a markdown block to append to the system prompt."""
        return f"## Project context (`{self.path}`)\n\n{self.content.strip()}\n"


def find_project_file(start: Path | None = None) -> Path | None:
    """Walk up from `start` (default cwd) looking for EVI.md.

    Returns the first match, or None. Stops at the filesystem root.
    """
    cur: Path | None = (start or Path.cwd()).resolve()
    while cur is not None:
        for name in PROJECT_FILENAMES:
            candidate = cur / name
            if candidate.is_file():
                return candidate
        parent = cur.parent
        if parent == cur:
            return None
        cur = parent
    return None


def load_project_context(start: Path | None = None) -> ProjectContext | None:
    """Return the parsed project file, or None if none exists / too large."""
    path = find_project_file(start)
    if path is None:
        return None
    try:
        data = path.read_bytes()
    except OSError:
        return None
    if len(data) > _MAX_BYTES:
        # Truncate rather than refuse — partial context beats none.
        data = data[:_MAX_BYTES]
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return None
    return ProjectContext(path=path, content=text)


# --- per-project config overlay (Phase 74) ------------------------------


def find_project_config(start: Path | None = None) -> Path | None:
    """Walk up from `start` (default cwd) for a `.evi.toml` project config."""
    cur: Path | None = (start or Path.cwd()).resolve()
    while cur is not None:
        candidate = cur / PROJECT_CONFIG_FILENAME
        if candidate.is_file():
            return candidate
        parent = cur.parent
        if parent == cur:
            return None
        cur = parent
    return None


def load_project_config_overlay(start: Path | None = None) -> dict:
    """Return the project `.evi.toml` parsed to a dict (empty if none).

    Merged on top of the user config (and any active profile) by Config.load,
    so a repo can pin its own model, tool toggles, permission rules, etc."""
    path = find_project_config(start)
    if path is None:
        return {}
    try:
        with path.open("rb") as f:
            data = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError):
        return {}
    return data if isinstance(data, dict) else {}
