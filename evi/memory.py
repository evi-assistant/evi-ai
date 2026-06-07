"""Persistent memory for eVi — markdown files under %USERPROFILE%/.evi/memory/.

Memory is intentionally simple: one markdown file per topic, listed in an
auto-maintained index that the Agent embeds into its system prompt so the
model knows what's stored without us shoveling every byte into context.

The model decides what's worth remembering by calling `remember(name, content)`.
It pulls full text on demand via `recall(name)`. The index is regenerated on
every write/delete so it stays in sync with the directory.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from pathlib import Path

from evi.config import HOME, ensure_dirs


MEMORY_DIR = HOME / "memory"
INDEX_FILE = MEMORY_DIR / "INDEX.md"
ATTIC_DIR = MEMORY_DIR / ".attic"

# Allow safe filenames only — letters, digits, dashes, underscores.
_NAME_RE = re.compile(r"^[A-Za-z0-9_\-]+$")
_MAX_NAME_LEN = 64
_MAX_CONTENT_BYTES = 64 * 1024


@dataclass(frozen=True)
class MemoryEntry:
    name: str
    summary: str  # first non-empty line, used in the index


class MemoryStore:
    """Markdown-on-disk memory keyed by safe filename (no extension).

    Construction is cheap (just resolves the path). All other methods touch
    the filesystem so callers can rely on `list()` reflecting current state.
    """

    def __init__(self, root: Path | None = None) -> None:
        self.root = Path(root) if root is not None else MEMORY_DIR

    # --- public API ------------------------------------------------------

    @property
    def index_path(self) -> Path:
        return self.root / INDEX_FILE.name

    def list(self) -> list[MemoryEntry]:
        if not self.root.is_dir():
            return []
        entries: list[MemoryEntry] = []
        for p in sorted(self.root.glob("*.md")):
            if p.name == INDEX_FILE.name:
                continue
            entries.append(MemoryEntry(name=p.stem, summary=_first_line(p)))
        return entries

    def read(self, name: str) -> str:
        path = self._path_for(name)
        if not path.is_file():
            raise KeyError(name)
        return path.read_text(encoding="utf-8")

    def write(self, name: str, content: str) -> Path:
        self._validate_name(name)
        if len(content.encode("utf-8")) > _MAX_CONTENT_BYTES:
            raise ValueError(
                f"memory '{name}' would exceed {_MAX_CONTENT_BYTES} bytes"
            )
        ensure_dirs()
        self.root.mkdir(parents=True, exist_ok=True)
        path = self._path_for(name)
        path.write_text(content.rstrip() + "\n", encoding="utf-8")
        self._rebuild_index()
        return path

    def delete(self, name: str) -> bool:
        """Soft delete — move the file into `.attic/` so it's recoverable.

        Returns True if a file was moved, False if it didn't exist. The
        dream engine relies on this safety net: a mistaken `forget` during
        memory consolidation can be undone by hand.
        """
        path = self._path_for(name)
        if not path.is_file():
            return False
        attic = self.root / ".attic"
        attic.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d_%H%M%S")
        path.rename(attic / f"{name}-{stamp}.md")
        self._rebuild_index()
        return True

    def hard_delete(self, name: str) -> bool:
        """Permanently delete (no attic). Use sparingly."""
        path = self._path_for(name)
        if not path.is_file():
            return False
        path.unlink()
        self._rebuild_index()
        return True

    def restore_from_attic(self, attic_filename: str) -> Path | None:
        """Move a file out of `.attic/` back into active memory."""
        src = self.root / ".attic" / attic_filename
        if not src.is_file():
            return None
        # Strip the `-YYYYMMDD_HHMMSS.md` suffix to recover the original name.
        stem = src.stem  # e.g. "preferences-20260527_120000"
        if "-" in stem:
            name = stem.rsplit("-", 1)[0]
        else:
            name = stem
        dest = self._path_for(name)
        if dest.is_file():
            return None  # don't clobber a current entry
        src.rename(dest)
        self._rebuild_index()
        return dest

    def format_for_prompt(self) -> str:
        """Render the index as a system-prompt-friendly markdown block.

        Returns an empty string when there's nothing stored, so callers can
        unconditionally append the result without worrying about clutter.
        """
        entries = self.list()
        if not entries:
            return ""
        lines = ["## Memory index", ""]
        for e in entries:
            lines.append(f"- **{e.name}** — {e.summary}")
        lines.append("")
        lines.append("Use `recall(name)` to fetch full contents.")
        return "\n".join(lines)

    # --- internals -------------------------------------------------------

    def _path_for(self, name: str) -> Path:
        self._validate_name(name)
        return self.root / f"{name}.md"

    @staticmethod
    def _validate_name(name: str) -> None:
        if not name or len(name) > _MAX_NAME_LEN or not _NAME_RE.match(name):
            raise ValueError(
                f"invalid memory name {name!r} — use letters, digits, dash, underscore "
                f"(max {_MAX_NAME_LEN} chars)"
            )

    def _rebuild_index(self) -> None:
        entries = self.list()
        index_path = self.index_path
        if not entries:
            if index_path.exists():
                index_path.unlink()
            return
        lines = ["# eVi memory index", ""]
        for e in entries:
            lines.append(f"- `{e.name}` — {e.summary}")
        index_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _first_line(path: Path) -> str:
    try:
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip().lstrip("#").strip()
            if line:
                return line[:160]
    except (OSError, UnicodeDecodeError):
        pass
    return "(empty)"
