"""Curated GGUF model catalog for the managed llama.cpp runtime.

Pure data + file helpers: load the catalog, resolve a model's on-disk path,
download it (dep-free, HF `resolve` URL), remove it, and pick a hardware-fit
default. Server orchestration (start/swap/config) lives in `setup.py`, which
imports this — one direction, no cycle.

Each entry pins an HF commit (`revision`) and the GGUF's `sha256`; a download
that doesn't match is discarded before it is ever loaded. A file already at a
model's path counts as installed only if it holds those pinned bytes: the path
is shared with `evi models pull hf:<repo>:<file>` (unpinned, from `main`) and
with copies made before pinning, so each file is hashed once and the result is
cached beside it in `<file>.sha256`. Refresh the pins with
`scripts/pin_runtime_hashes.py`.
"""

from __future__ import annotations

import hashlib
import json
import os
import urllib.error
from functools import lru_cache
from pathlib import Path

from evi.config import MODELS_DIR, ensure_dirs
from evi.runtime import llamacpp_runtime
from evi.runtime.llamacpp_runtime import IntegrityError, ProgressCB

_CATALOG_PATH = Path(__file__).resolve().parent.parent / "data" / "runtime_models.json"
_HF_BASE = "https://huggingface.co"
STARTER_ID = "qwen2.5-1.5b-instruct"


@lru_cache(maxsize=1)
def _load() -> tuple[dict, ...]:
    data = json.loads(_CATALOG_PATH.read_text(encoding="utf-8"))
    return tuple(data.get("models", []))


def catalog() -> list[dict]:
    return [dict(m) for m in _load()]


def get(model_id: str) -> dict | None:
    return next((dict(m) for m in _load() if m["id"] == model_id), None)


def starter() -> dict:
    return get(STARTER_ID) or catalog()[0]


def model_path(entry: dict) -> Path:
    return MODELS_DIR / entry["hf_repo"].replace("/", "__") / entry["filename"]


def _marker(path: Path) -> Path:
    return path.with_name(path.name + ".sha256")


def _stamp(st: os.stat_result) -> list[int]:
    # Which version of the file a cached digest belongs to: anything that
    # rewrites or replaces it changes at least one of these.
    return [st.st_size, st.st_mtime_ns, st.st_ino]


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(1 << 20):
            digest.update(chunk)
    return digest.hexdigest()


def _remember(path: Path, sha256: str, st: os.stat_result) -> None:
    try:
        _marker(path).write_text(json.dumps({"sha256": sha256, "stat": _stamp(st)}),
                                 encoding="utf-8")
    except OSError:
        pass  # read-only dir: we just hash again next time


def _file_sha256(path: Path) -> str | None:
    """sha256 of the file at `path`, hashed at most once per version of it (the
    digest is cached in `<file>.sha256`, keyed by size/mtime/inode, so a file
    swapped in by anything else is hashed afresh, never trusted). None if it
    can't be read."""
    try:
        st = path.stat()
        cached = json.loads(_marker(path).read_text(encoding="utf-8"))
        if cached.get("stat") == _stamp(st) and isinstance(cached.get("sha256"), str):
            return cached["sha256"]
    except (OSError, ValueError, AttributeError):
        pass
    try:
        st = path.stat()
        sha = _hash_file(path)
        if _stamp(path.stat()) != _stamp(st):
            return None  # changed while we read it: trust nothing
    except OSError:
        return None
    _remember(path, sha, st)
    return sha


def is_installed(entry: dict) -> bool:
    """The entry's GGUF is at its model path AND holds the pinned bytes. A
    file that doesn't (an `hf:` pull from `main`, a pre-pinning copy that
    differs, a partial copy) is not installed: `download` replaces it."""
    path = model_path(entry)
    if not path.is_file():
        return False
    try:
        want = llamacpp_runtime.expected_sha256(entry.get("sha256"), entry["filename"])
    except IntegrityError:
        return False
    return _file_sha256(path) == want


def download_url(entry: dict, revision: str | None = None) -> str:
    """The GGUF at the entry's pinned HF commit, so the bytes behind the URL
    can't move out from under its sha256 the way `main` can."""
    revision = revision or entry.get("revision") or "main"
    return f"{_HF_BASE}/{entry['hf_repo']}/resolve/{revision}/{entry['filename']}"


def download(entry: dict, on_bytes: ProgressCB | None = None) -> Path:
    """Download the entry's GGUF into ~/.evi/models/ (idempotent), verified
    against its pinned `sha256`. A mismatch or a missing pin raises
    IntegrityError and leaves nothing new at the model path; a file already
    there is kept only if it holds the pinned bytes, else the verified
    download replaces it."""
    ensure_dirs()
    dest = model_path(entry)
    want = llamacpp_runtime.expected_sha256(entry.get("sha256"), entry["filename"])
    if dest.is_file() and _file_sha256(dest) == want:
        return dest
    try:
        path = llamacpp_runtime.download_file(download_url(entry), dest, on_bytes, sha256=want)
    except urllib.error.HTTPError as exc:
        # The pinned commit is gone (an upstream history squash) but the file
        # usually isn't: fetch it from main, held to the very same sha256.
        if exc.code != 404 or (entry.get("revision") or "main") == "main":
            raise
        path = llamacpp_runtime.download_file(download_url(entry, "main"), dest, on_bytes,
                                              sha256=want)
    _remember(path, want, path.stat())
    return path


def remove(entry: dict) -> bool:
    p = model_path(entry)
    _marker(p).unlink(missing_ok=True)
    if p.is_file():
        p.unlink()
        return True
    return False


def _memory_gb() -> tuple[float, float]:
    """(ram_gb, vram_gb) for this machine — 0 vram when no GPU. Best-effort."""
    try:
        from evi.hardware import detect

        hw = detect()
        ram = float(getattr(hw, "ram_gb", 0) or 0) or 8.0
        vram = 0.0
        gpus = getattr(hw, "gpus", None) or []
        if gpus:
            vram = float(getattr(gpus[0], "vram_total_mb", 0) or 0) / 1024.0
        return ram, vram
    except Exception:  # noqa: BLE001
        return 8.0, 0.0


def recommended_id() -> str:
    """Largest GENERAL model that fits this machine (VRAM if a GPU is present,
    else ~70% of RAM to leave the OS headroom); the smallest general model if
    nothing else fits."""
    ram, vram = _memory_gb()
    budget = vram if vram else ram * 0.7
    key = "min_vram_gb" if vram else "min_ram_gb"
    generals = [m for m in _load() if "coding" not in m.get("tags", ())]
    fits = sorted(
        (m for m in generals if m.get(key, 99) <= budget),
        key=lambda m: m.get("params_b", 0),
    )
    chosen = fits[-1] if fits else min(generals, key=lambda m: m.get("params_b", 99))
    return chosen["id"]
