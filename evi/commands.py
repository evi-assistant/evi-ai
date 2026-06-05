"""User-defined slash commands — saved markdown prompt templates.

Drop a file at `~/.evi/commands/<name>.md`; typing `/<name>` in the REPL
sends its content as the next user message. The file may include `{args}`
as a placeholder: typing `/<name> foo bar` substitutes `foo bar` for it.

Example `~/.evi/commands/commit.md`:

    Run `git diff` to see what changed, then propose a conventional-commits
    style commit message. Args (if any): {args}

We don't try to parse YAML frontmatter here — a command is just a prompt.
If you want triggering metadata, that's a Skill (`evi/skills.py`).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from evi.config import COMMANDS_DIR


_NAME_RE = re.compile(r"^[A-Za-z0-9_\-]+$")


@dataclass(frozen=True)
class SlashCommandEntry:
    name: str
    path: Path
    summary: str  # first non-empty line, for /help


class CommandStore:
    """Loader for `~/.evi/commands/*.md`. Stateless; every call rescans."""

    def __init__(self, root: Path | None = None) -> None:
        self.root = Path(root) if root is not None else COMMANDS_DIR

    def list(self) -> list[SlashCommandEntry]:
        if not self.root.is_dir():
            return []
        out: list[SlashCommandEntry] = []
        for p in sorted(self.root.glob("*.md")):
            name = p.stem
            if not _NAME_RE.match(name):
                continue
            summary = _first_line(p)
            out.append(SlashCommandEntry(name=name, path=p, summary=summary))
        return out

    def get(self, name: str) -> SlashCommandEntry | None:
        path = self.root / f"{name}.md"
        if not _NAME_RE.match(name) or not path.is_file():
            return None
        return SlashCommandEntry(name=name, path=path, summary=_first_line(path))

    def expand(self, name: str, args: str = "") -> str | None:
        """Return the command body with `{args}` substituted, or None."""
        entry = self.get(name)
        if entry is None:
            return None
        body = entry.path.read_text(encoding="utf-8")
        return body.replace("{args}", args).strip()


def _first_line(path: Path) -> str:
    """Return a one-line summary for `/help`.

    Prefers the first prose line. If only a markdown header exists, falls
    back to that. Empty files surface as "(no summary)".
    """
    header: str | None = None
    try:
        for raw in path.read_text(encoding="utf-8").splitlines():
            stripped = raw.strip()
            if not stripped:
                continue
            if stripped.startswith("#"):
                if header is None:
                    header = stripped.lstrip("#").strip()
                continue
            return stripped[:160]
    except (OSError, UnicodeDecodeError):
        pass
    return (header[:160] if header else "(no summary)")
