"""Curated GGUF model catalog for the managed llama.cpp runtime.

Pure data + file helpers: load the catalog, resolve a model's on-disk path,
download it (dep-free, HF `resolve` URL), remove it, and pick a hardware-fit
default. Server orchestration (start/swap/config) lives in `setup.py`, which
imports this — one direction, no cycle.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

from evi.config import MODELS_DIR, ensure_dirs
from evi.runtime import llamacpp_runtime
from evi.runtime.llamacpp_runtime import ProgressCB

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


def is_installed(entry: dict) -> bool:
    return model_path(entry).is_file()


def download_url(entry: dict) -> str:
    return f"{_HF_BASE}/{entry['hf_repo']}/resolve/main/{entry['filename']}"


def download(entry: dict, on_bytes: ProgressCB | None = None) -> Path:
    """Download the entry's GGUF into ~/.evi/models/ (idempotent)."""
    ensure_dirs()
    dest = model_path(entry)
    if dest.is_file():
        return dest
    return llamacpp_runtime.download_file(download_url(entry), dest, on_bytes)


def remove(entry: dict) -> bool:
    p = model_path(entry)
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
