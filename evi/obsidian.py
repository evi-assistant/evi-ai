"""Sync Evi's memory with an Obsidian vault.

Obsidian is just a folder of markdown files. We treat a sub-directory of
the vault (`<vault>/<subdir>/`) as the mirror of `~/.evi/memory/`. Each
entry is written with YAML frontmatter so Dataview / Bases queries can
filter them:

    ---
    source: evi-memory
    name: preferences
    created: 2026-05-27T10:00:00
    updated: 2026-05-27T15:30:00
    ---

    # Preferences

    The body of the memory entry…

`push` is the safe default — Evi memory is the source of truth, vault
entries are overwritten / created. `pull` does the reverse. `sync` runs
both directions and resolves conflicts by last-modified time.

`.attic/` (soft-deleted memory) is never pushed to the vault. If you've
deleted a memory in Evi and pull from a vault that still has it, the
entry is restored to live memory — recovering a previously soft-deleted
file.
"""

from __future__ import annotations

import re
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from evi.memory import MemoryStore


_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.S)
_INDEX_FILENAME = "INDEX.md"
_SOURCE_TAG = "evi-memory"


@dataclass
class SyncStats:
    """Result of a sync operation. Lists names + a one-line summary."""

    pushed: list[str]
    pulled: list[str]
    skipped: list[str]
    deleted_locally: list[str]  # entries we found in vault but not in memory
    errors: list[str]

    def summary(self) -> str:
        bits: list[str] = []
        if self.pushed:
            bits.append(f"pushed {len(self.pushed)}")
        if self.pulled:
            bits.append(f"pulled {len(self.pulled)}")
        if self.skipped:
            bits.append(f"skipped {len(self.skipped)}")
        if self.errors:
            bits.append(f"errors {len(self.errors)}")
        return ", ".join(bits) or "no changes"


def _vault_target_dir(vault_path: Path, subdir: str) -> Path:
    """Resolve the directory where Evi files live inside the vault."""
    return (vault_path / subdir).expanduser().resolve()


def _strip_frontmatter(text: str) -> tuple[dict[str, str], str]:
    """Return (meta, body). If no frontmatter, meta is empty and body == text."""
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return {}, text
    block = m.group(1)
    body = text[m.end():]
    meta: dict[str, str] = {}
    for line in block.splitlines():
        if ":" not in line:
            continue
        k, _, v = line.partition(":")
        meta[k.strip()] = v.strip().strip('"').strip("'")
    return meta, body


def _build_frontmatter(name: str, body: str, source_path: Path | None) -> str:
    """Compose YAML frontmatter for a vault file."""
    now = datetime.now().isoformat(timespec="seconds")
    created = now
    if source_path and source_path.is_file():
        try:
            ts = source_path.stat().st_mtime
            created = datetime.fromtimestamp(ts).isoformat(timespec="seconds")
        except OSError:
            pass
    return (
        "---\n"
        f"source: {_SOURCE_TAG}\n"
        f"name: {name}\n"
        f"created: {created}\n"
        f"updated: {now}\n"
        "---\n\n"
        + body.lstrip()
    )


def _vault_files(target_dir: Path) -> list[Path]:
    if not target_dir.is_dir():
        return []
    return sorted(
        p for p in target_dir.glob("*.md") if p.name != _INDEX_FILENAME
    )


def push(
    store: MemoryStore,
    vault_path: str | Path,
    subdir: str = "Evi",
    *,
    dry_run: bool = False,
) -> SyncStats:
    """Copy every live memory entry into `<vault>/<subdir>/<name>.md`.

    Overwrites whatever's there. Files in the vault that have no live
    counterpart are left alone — `pull` is the way to learn about them,
    `sync` cleans them up.
    """
    target_dir = _vault_target_dir(Path(vault_path), subdir)
    pushed: list[str] = []
    errors: list[str] = []

    if not target_dir.parent.is_dir():
        # The vault root itself must exist.
        return SyncStats(
            pushed=[], pulled=[], skipped=[], deleted_locally=[],
            errors=[f"vault path does not exist: {target_dir.parent}"],
        )

    if not dry_run:
        target_dir.mkdir(parents=True, exist_ok=True)

    for entry in store.list():
        try:
            body = store.read(entry.name)
        except KeyError as exc:
            errors.append(f"{entry.name}: {exc}")
            continue
        # Strip any existing frontmatter from the source body — we write
        # our own.
        _meta, clean_body = _strip_frontmatter(body)
        out = _build_frontmatter(
            entry.name, clean_body, source_path=store._path_for(entry.name),
        )
        target = target_dir / f"{entry.name}.md"
        if not dry_run:
            target.write_text(out, encoding="utf-8")
        pushed.append(entry.name)

    return SyncStats(
        pushed=pushed, pulled=[], skipped=[], deleted_locally=[],
        errors=errors,
    )


def pull(
    store: MemoryStore,
    vault_path: str | Path,
    subdir: str = "Evi",
    *,
    dry_run: bool = False,
) -> SyncStats:
    """Read each markdown file from `<vault>/<subdir>/` into memory.

    Frontmatter is stripped before storage. Vault files with names that
    don't pass the safe-name regex are skipped with an error.
    """
    target_dir = _vault_target_dir(Path(vault_path), subdir)
    pulled: list[str] = []
    skipped: list[str] = []
    errors: list[str] = []

    if not target_dir.is_dir():
        return SyncStats(
            pushed=[], pulled=[], skipped=[], deleted_locally=[],
            errors=[f"vault subdir not found: {target_dir}"],
        )

    for f in _vault_files(target_dir):
        name = f.stem
        try:
            text = f.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            errors.append(f"{f.name}: {exc}")
            continue
        _meta, body = _strip_frontmatter(text)
        try:
            if not dry_run:
                store.write(name, body)
            pulled.append(name)
        except ValueError as exc:
            skipped.append(f"{f.name} ({exc})")

    return SyncStats(
        pushed=[], pulled=pulled, skipped=skipped, deleted_locally=[],
        errors=errors,
    )


def sync(
    store: MemoryStore,
    vault_path: str | Path,
    subdir: str = "Evi",
    *,
    dry_run: bool = False,
) -> SyncStats:
    """Bidirectional sync. Newer side wins on a per-entry basis."""
    target_dir = _vault_target_dir(Path(vault_path), subdir)
    pushed: list[str] = []
    pulled: list[str] = []
    skipped: list[str] = []
    errors: list[str] = []

    if not target_dir.parent.is_dir():
        return SyncStats(
            pushed=[], pulled=[], skipped=[], deleted_locally=[],
            errors=[f"vault path does not exist: {target_dir.parent}"],
        )
    if not dry_run:
        target_dir.mkdir(parents=True, exist_ok=True)

    mem_entries = {e.name: e for e in store.list()}
    vault_files = {f.stem: f for f in _vault_files(target_dir)}

    for name in set(mem_entries) | set(vault_files):
        mem = mem_entries.get(name)
        vault = vault_files.get(name)

        mem_path = store._path_for(name) if mem is not None else None
        mem_mtime = mem_path.stat().st_mtime if mem_path and mem_path.is_file() else 0.0
        vault_mtime = vault.stat().st_mtime if vault and vault.is_file() else 0.0

        try:
            if mem and not vault:
                # Memory has it, vault doesn't — push.
                body = store.read(name)
                _m, clean = _strip_frontmatter(body)
                out = _build_frontmatter(name, clean, source_path=mem_path)
                if not dry_run:
                    (target_dir / f"{name}.md").write_text(out, encoding="utf-8")
                pushed.append(name)
            elif vault and not mem:
                # Vault has it, memory doesn't — pull (or restore).
                text = vault.read_text(encoding="utf-8")
                _m, body = _strip_frontmatter(text)
                if not dry_run:
                    store.write(name, body)
                pulled.append(name)
            elif mem and vault:
                # Both have it — newer wins.
                if mem_mtime > vault_mtime:
                    body = store.read(name)
                    _m, clean = _strip_frontmatter(body)
                    out = _build_frontmatter(name, clean, source_path=mem_path)
                    if not dry_run:
                        (target_dir / f"{name}.md").write_text(out, encoding="utf-8")
                    pushed.append(name)
                elif vault_mtime > mem_mtime:
                    text = vault.read_text(encoding="utf-8")
                    _m, body = _strip_frontmatter(text)
                    if not dry_run:
                        store.write(name, body)
                    pulled.append(name)
                else:
                    skipped.append(name)
        except (OSError, UnicodeDecodeError, ValueError) as exc:
            errors.append(f"{name}: {exc}")

    return SyncStats(
        pushed=pushed, pulled=pulled, skipped=skipped, deleted_locally=[],
        errors=errors,
    )


def status(
    store: MemoryStore,
    vault_path: str | Path,
    subdir: str = "Evi",
) -> dict[str, list[str]]:
    """Report what's where without changing anything. Useful for inspection."""
    target_dir = _vault_target_dir(Path(vault_path), subdir)
    mem_names = {e.name for e in store.list()}
    vault_names = {f.stem for f in _vault_files(target_dir)} if target_dir.is_dir() else set()
    return {
        "only_in_memory": sorted(mem_names - vault_names),
        "only_in_vault": sorted(vault_names - mem_names),
        "in_both": sorted(mem_names & vault_names),
        "vault_dir": [str(target_dir)],
    }


# Convenience for tests: copy a directory tree (used by sync tests to
# pre-seed a fake vault). Not exposed via the CLI.
def _copy_tree(src: Path, dst: Path) -> None:
    dst.mkdir(parents=True, exist_ok=True)
    for f in src.glob("*.md"):
        shutil.copy2(f, dst / f.name)
