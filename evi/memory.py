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

# Tags are stored as an invisible trailing HTML comment so they don't show in
# rendered markdown, don't disturb the first-line summary, and leave untagged
# legacy memories parsing cleanly as tag-less.
_TAGS_RE = re.compile(r"(?im)^<!--\s*tags:\s*(.*?)\s*-->\s*$")


def _normalize_tags(tags) -> tuple[str, ...]:
    """Lower-case, strip, dedupe (order-preserving), drop empties."""
    seen: dict[str, None] = {}
    for t in tags or ():
        t = str(t).strip().lower()
        if t:
            seen.setdefault(t, None)
    return tuple(seen)


def _parse_tags(text: str) -> tuple[str, ...]:
    m = _TAGS_RE.search(text)
    return _normalize_tags(m.group(1).split(",")) if m else ()


def _strip_tags_marker(text: str) -> str:
    return _TAGS_RE.sub("", text).rstrip()


def _tags_marker(tags: tuple[str, ...]) -> str:
    return f"<!-- tags: {', '.join(tags)} -->"


@dataclass(frozen=True)
class MemoryEntry:
    name: str
    summary: str  # first non-empty line, used in the index
    tags: tuple[str, ...] = ()


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
            try:
                text = p.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                text = ""
            entries.append(
                MemoryEntry(name=p.stem, summary=_summary(text), tags=_parse_tags(text))
            )
        return entries

    def read(self, name: str) -> str:
        """Return the memory body (the tags marker is metadata, stripped out)."""
        path = self._path_for(name)
        if not path.is_file():
            raise KeyError(name)
        return _strip_tags_marker(path.read_text(encoding="utf-8")) + "\n"

    def tags_of(self, name: str) -> tuple[str, ...]:
        path = self._path_for(name)
        if not path.is_file():
            return ()
        return _parse_tags(path.read_text(encoding="utf-8"))

    def by_tag(self, tag: str) -> list[MemoryEntry]:
        """All entries carrying `tag` (case-insensitive)."""
        want = tag.strip().lower()
        return [e for e in self.list() if want in e.tags]

    def all_tags(self) -> list[str]:
        tags: set[str] = set()
        for e in self.list():
            tags.update(e.tags)
        return sorted(tags)

    def write(self, name: str, content: str, tags=None) -> Path:
        self._validate_name(name)
        if len(content.encode("utf-8")) > _MAX_CONTENT_BYTES:
            raise ValueError(
                f"memory '{name}' would exceed {_MAX_CONTENT_BYTES} bytes"
            )
        ensure_dirs()
        self.root.mkdir(parents=True, exist_ok=True)
        path = self._path_for(name)
        body = _strip_tags_marker(content)
        # tags=None preserves whatever's already on disk (a content edit
        # shouldn't silently drop tags); tags=[] clears them.
        if tags is None:
            new_tags = self.tags_of(name) if path.is_file() else ()
        else:
            new_tags = _normalize_tags(tags)
        out = body
        if new_tags:
            out += "\n\n" + _tags_marker(new_tags)
        path.write_text(out.rstrip() + "\n", encoding="utf-8")
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
            suffix = f"  _[{', '.join(e.tags)}]_" if e.tags else ""
            lines.append(f"- **{e.name}** — {e.summary}{suffix}")
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
            suffix = f"  [{', '.join(e.tags)}]" if e.tags else ""
            lines.append(f"- `{e.name}` — {e.summary}{suffix}")
        index_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _summary(text: str) -> str:
    """First meaningful line of a memory body, for the index/summary."""
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("<!--"):
            continue  # skip blanks + the tags marker / other comments
        line = line.lstrip("#").strip()
        if line:
            return line[:160]
    return "(empty)"


def _first_line(path: Path) -> str:
    try:
        return _summary(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError):
        return "(empty)"
