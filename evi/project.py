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


def find_project_files(start: Path | None = None) -> list[Path]:
    """Every project file from the filesystem root down to `start` (outermost
    first, nearest last), one per directory. Enables monorepo-style layered
    context: a repo-root EVI.md plus a package-level one."""
    cur: Path | None = (start or Path.cwd()).resolve()
    found: list[Path] = []
    while cur is not None:
        for name in PROJECT_FILENAMES:
            candidate = cur / name
            if candidate.is_file():
                found.append(candidate)
                break  # one per directory
        parent = cur.parent
        if parent == cur:
            break
        cur = parent
    found.reverse()  # root → nearest, so the most-specific context comes last
    return found


def load_project_context(start: Path | None = None) -> ProjectContext | None:
    """Aggregate project files from root → cwd into one context.

    A single file is returned verbatim (backwards compatible). Multiple levels
    are concatenated with per-file headers, outermost first, capped at
    `_MAX_BYTES` total — partial context beats none."""
    files = find_project_files(start)
    sections: list[tuple[Path, str]] = []
    for path in files:
        try:
            text = path.read_bytes().decode("utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        sections.append((path, text))
    if not sections:
        return None

    nearest = sections[-1][0]
    if len(sections) == 1:
        content = sections[0][1]
    else:
        content = "\n\n".join(f"### {p}\n\n{t.strip()}" for p, t in sections)
    if len(content.encode("utf-8")) > _MAX_BYTES:
        marker = "\n…(project context truncated)"
        budget = _MAX_BYTES - len(marker.encode("utf-8"))
        content = content.encode("utf-8")[:budget].decode("utf-8", errors="ignore") + marker
    return ProjectContext(path=nearest, content=content)


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
