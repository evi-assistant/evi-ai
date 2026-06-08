"""File checkpointing + rewind.

Every file write eVi makes (via the `write_file` tool) is journalled with the
file's *prior* state, so a bad edit can be undone — the file-level analogue of
the conversation edit/branch/reroll eVi already has.

Storage under ``~/.evi/checkpoints/``:
- ``journal.jsonl`` — one entry per write: ``{seq, ts, path, op, blob?, size?}``
  where ``op`` is ``create`` (file didn't exist → undo deletes it),
  ``modify`` (prior bytes saved as a blob → undo restores them), or ``skip``
  (file too large to snapshot).
- ``blobs/<sha256>`` — the prior contents, content-addressed (dedup'd).

`rewind(seq)` walks entries with ``seq >= target`` newest-first, restoring each,
then trims the journal. All functions take an optional ``root`` for tests.
"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

import evi.config as config

# Don't snapshot files bigger than this (avoids ballooning the store on a write
# over a huge file); such writes are journalled as "skip" — not undoable.
MAX_BLOB_BYTES = 5 * 1024 * 1024


def _root(root: Path | None) -> Path:
    return (root if root is not None else config.HOME) / "checkpoints"


def _journal(root: Path | None) -> Path:
    return _root(root) / "journal.jsonl"


def _entries(root: Path | None) -> list[dict]:
    j = _journal(root)
    if not j.exists():
        return []
    out: list[dict] = []
    for line in j.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def _write_entries(root: Path | None, entries: list[dict]) -> None:
    _journal(root).write_text(
        "".join(json.dumps(e) + "\n" for e in entries), encoding="utf-8"
    )


def record_before_write(path: str | Path, root: Path | None = None) -> int:
    """Journal the current state of `path` BEFORE it's overwritten/created.

    Returns the checkpoint seq. Best-effort — callers wrap in try/except so a
    checkpoint failure never blocks the actual write."""
    r = _root(root)
    r.mkdir(parents=True, exist_ok=True)
    p = Path(path).expanduser()
    entries = _entries(root)
    seq = (entries[-1]["seq"] + 1) if entries else 1
    entry: dict = {"seq": seq, "ts": time.time(), "path": str(p)}
    if p.is_file():
        data = p.read_bytes()
        if len(data) > MAX_BLOB_BYTES:
            entry["op"] = "skip"
        else:
            h = hashlib.sha256(data).hexdigest()
            blobs = r / "blobs"
            blobs.mkdir(parents=True, exist_ok=True)
            (blobs / h).write_bytes(data)
            entry["op"] = "modify"
            entry["blob"] = h
            entry["size"] = len(data)
    else:
        entry["op"] = "create"
    with _journal(root).open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")
    return seq


def list_checkpoints(limit: int = 20, root: Path | None = None) -> list[dict]:
    """Most-recent-last journal entries (up to `limit`)."""
    return _entries(root)[-limit:]


def rewind(seq: int | None = None, root: Path | None = None) -> list[tuple[str, str]]:
    """Undo writes with journal ``seq >= target`` (default: just the latest).

    Restores modified files to their prior bytes and deletes files that were
    newly created. Returns [(path, action), …]; trims the undone entries from
    the journal so a second rewind continues further back."""
    entries = _entries(root)
    if not entries:
        return []
    target = seq if seq is not None else entries[-1]["seq"]
    undone = [e for e in entries if e["seq"] >= target]
    actions: list[tuple[str, str]] = []
    for e in reversed(undone):
        p = Path(e["path"])
        op = e.get("op")
        if op == "create":
            if p.is_file():
                p.unlink()
                actions.append((str(p), "deleted (was newly created)"))
        elif op == "modify":
            blob = _root(root) / "blobs" / e.get("blob", "")
            if blob.is_file():
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_bytes(blob.read_bytes())
                actions.append((str(p), "restored"))
        else:  # skip / unknown — can't restore
            actions.append((str(p), "could not restore (not snapshotted)"))
    _write_entries(root, [e for e in entries if e["seq"] < target])
    return actions
