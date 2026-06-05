"""Backup / restore for `~/.evi/`.

Tar/gzip is the right tool: portable, ubiquitous, supported by every OS
without extra deps. We deliberately don't ship Python wheels of model
files — backups include the *config* (which model + backend), not the
multi-GB downloaded weights.

Default excludes:

- `models/`       — multi-GB GGUFs you'd just re-pull
- `transcripts/`  — per-machine, often large, regenerated as you use Evi
- `logs/`         — purely operational
- `images/`       — per-machine outputs
- `screenshots/`  — per-machine outputs
- `uploads/`      — per-machine temp files
- `.attic/`       — soft-deleted memory; surfaces on restore via filename

Pass `--include` for any of those if you really want them.
"""

from __future__ import annotations

import os
import tarfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from evi.config import HOME, ensure_dirs


# Top-level paths under HOME that we skip unless asked.
DEFAULT_EXCLUDES = frozenset({
    "models",
    "transcripts",
    "logs",
    "images",
    "screenshots",
    "uploads",
})


@dataclass(frozen=True)
class BackupSummary:
    archive: Path
    file_count: int
    bytes_packed: int
    excluded_top: list[str]


def _resolve_excludes(includes: set[str]) -> set[str]:
    """Default excludes minus anything the caller decided to keep."""
    return set(DEFAULT_EXCLUDES) - {x.strip().lower() for x in includes}


def create_backup(
    *,
    out_path: Path | None = None,
    home: Path | None = None,
    includes: set[str] | None = None,
) -> BackupSummary:
    """Create a `.tar.gz` of the Evi home directory and return a summary.

    `includes` overrides specific default excludes (e.g. `{"models"}` to
    pack the downloaded weights).
    """
    ensure_dirs()
    base = home or HOME
    excludes = _resolve_excludes(includes or set())

    if out_path is None:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        out_path = base / f"evi-backup-{stamp}.tar.gz"

    file_count = 0
    bytes_packed = 0

    def _filter(info: tarfile.TarInfo) -> tarfile.TarInfo | None:
        nonlocal file_count, bytes_packed
        # info.name starts with "evi/" — strip that to inspect the leading
        # top-level dir under HOME.
        path_in_archive = info.name.split("/", 1)[1] if "/" in info.name else ""
        top = path_in_archive.split("/", 1)[0]
        if top in excludes:
            return None
        # Skip the backup file itself if it lives under HOME (it does by default).
        if Path(base / path_in_archive).resolve() == out_path.resolve():
            return None
        if info.isfile():
            file_count += 1
            bytes_packed += info.size
        return info

    with tarfile.open(out_path, "w:gz") as tar:
        tar.add(str(base), arcname="evi", filter=_filter)

    return BackupSummary(
        archive=out_path,
        file_count=file_count,
        bytes_packed=bytes_packed,
        excluded_top=sorted(excludes),
    )


@dataclass(frozen=True)
class RestoreSummary:
    archive: Path
    file_count: int
    home: Path


def restore_backup(
    archive: Path,
    *,
    home: Path | None = None,
    overwrite: bool = False,
) -> RestoreSummary:
    """Extract a backup archive into the Evi home dir.

    By default we refuse to overwrite an existing non-empty HOME. Pass
    `overwrite=True` to merge — files in the archive replace existing
    counterparts, but other on-disk content is left alone.
    """
    ensure_dirs()
    base = home or HOME

    if not overwrite:
        # Empty HOME means nothing notable to lose. We consider HOME
        # non-empty if it contains anything besides the dirs ensure_dirs()
        # already creates.
        ignorable = {
            "tokens", "logs", "models", "profiles", "commands",
            "scheduled", "skills", "memory", "transcripts",
            "images", "screenshots", "uploads",
        }
        leftover = [
            p for p in base.iterdir()
            if p.name not in ignorable and not p.name.startswith(".")
        ]
        if leftover:
            raise RuntimeError(
                f"refusing to restore over a non-empty {base}: "
                f"set overwrite=True to proceed"
            )

    file_count = 0
    with tarfile.open(archive, "r:gz") as tar:
        for member in tar.getmembers():
            # Strip the "evi/" arcname prefix and reroot under `base`.
            rel = member.name
            if rel.startswith("evi/"):
                rel = rel[len("evi/"):]
            elif rel == "evi":
                continue  # the root entry itself
            if not rel:
                continue
            # Path traversal guard.
            if rel.startswith("/") or ".." in rel.split("/"):
                continue
            target = base / rel
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
            elif member.isfile():
                target.parent.mkdir(parents=True, exist_ok=True)
                extracted = tar.extractfile(member)
                if extracted is None:
                    continue
                with extracted as src, target.open("wb") as out:
                    while True:
                        chunk = src.read(64 * 1024)
                        if not chunk:
                            break
                        out.write(chunk)
                # Preserve mtime for diff-friendliness.
                try:
                    os.utime(target, (member.mtime, member.mtime))
                except OSError:
                    pass
                file_count += 1

    return RestoreSummary(archive=archive, file_count=file_count, home=base)
