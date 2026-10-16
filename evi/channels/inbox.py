"""Staged channel memories — what a paired sender asked eVi to remember.

With `[channels] memory_mode = "staged"`, a channel's `remember` never writes
the owner's memory. Each note lands here instead, as one JSON file under
``~/.evi/inbox/channel/`` carrying where it came from
(``source = "channel:<channel>:<chat>"``) and ``trust = "untrusted"``. The
inbox sits outside the memory directory, so no session's memory index lists a
staged note and nothing recalls it: until the owner applies it, it changes
nothing a desktop session loads.

The owner reviews the inbox with `list_staged` and takes each note with
`apply` (written into memory) or `discard`. This module is the store; the
review UI is the memory inbox on the roadmap.
"""

from __future__ import annotations

import json
import re
import secrets
import time
from pathlib import Path
from typing import Any

from evi.config import HOME
from evi.memory import _MAX_CONTENT_BYTES, MemoryStore

# A paired sender can call remember in a loop; past this many waiting notes
# staging refuses rather than filling the disk.
MAX_STAGED = 200

_ID_RE = re.compile(r"^[A-Za-z0-9_\-]+$")


def inbox_dir() -> Path:
    return HOME / "inbox" / "channel"


def _path_for(item_id: str) -> Path:
    if not item_id or not _ID_RE.match(item_id):
        raise ValueError(f"invalid inbox id {item_id!r}")
    return inbox_dir() / f"{item_id}.json"


def stage_memory(
    name: str, content: str, tags: list[str] | None = None, *, source: str, sender: str = ""
) -> Path:
    """Stage one channel-originated `remember` and return its file.

    `tags` None keeps whatever tags the memory has when it is applied (as
    `remember` does); a list replaces them. Raises ValueError for a name or a
    size `MemoryStore.write` would refuse, or when the inbox is full."""
    MemoryStore._validate_name(name)
    if len(content.encode("utf-8")) > _MAX_CONTENT_BYTES:
        raise ValueError(f"memory '{name}' would exceed {_MAX_CONTENT_BYTES} bytes")
    folder = inbox_dir()
    folder.mkdir(parents=True, exist_ok=True)
    if sum(1 for _ in folder.glob("*.json")) >= MAX_STAGED:
        raise ValueError("the channel inbox is full: the owner has to review it first")
    item_id = f"{time.strftime('%Y%m%d_%H%M%S')}-{name}-{secrets.token_hex(3)}"
    path = folder / f"{item_id}.json"
    path.write_text(json.dumps({
        "kind": "memory",
        "op": "remember",
        "name": name,
        "content": content,
        "tags": list(tags) if tags is not None else None,
        "source": source,
        "sender": sender,
        "trust": "untrusted",
        "at": time.time(),
    }, indent=2), encoding="utf-8")
    return path


def list_staged() -> list[dict[str, Any]]:
    """Every staged note, oldest first, each with its ``id``."""
    folder = inbox_dir()
    if not folder.is_dir():
        return []
    out: list[dict[str, Any]] = []
    for p in sorted(folder.glob("*.json")):
        try:
            item = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(item, dict):
            out.append({"id": p.stem, **item})
    return out


def apply(item_id: str, store: MemoryStore | None = None) -> Path | None:
    """Write a staged note into `store` (default: the owner's memory) and drop
    it from the inbox. Returns the memory file, or None if no such note."""
    path = _path_for(item_id)
    if not path.is_file():
        return None
    item = json.loads(path.read_text(encoding="utf-8"))
    written = (store or MemoryStore()).write(
        str(item["name"]), str(item["content"]), tags=item.get("tags"))
    path.unlink()
    return written


def discard(item_id: str) -> bool:
    """Drop a staged note without applying it."""
    path = _path_for(item_id)
    if not path.is_file():
        return False
    path.unlink()
    return True


class StagedMemoryStore(MemoryStore):
    """The owner's memory, read-only, with `remember` staged into the inbox.

    `evi.tools.memory.remember` sees a read-only store and calls `stage`, so a
    channel's note never reaches the directory this store reads."""

    def __init__(self, root: Path | None = None, *, source: str, sender: str = "") -> None:
        super().__init__(root, read_only=True)
        self.source = source
        self.sender = sender

    def stage(self, name: str, content: str, tags: list[str] | None = None) -> Path:
        return stage_memory(name, content, tags, source=self.source, sender=self.sender)
