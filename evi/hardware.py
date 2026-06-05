"""Hardware detection — GPUs (NVIDIA only for now) + system RAM.

Used by `evi.recommend` to pick a model that actually fits. We deliberately
keep this best-effort: missing nvidia-smi means "no GPU we can see" rather
than a crash, and missing psutil falls back to a platform-native byte read.

NVIDIA-only because the user's three machines are all NVIDIA (940MX / 5070
Ti / P40). AMD and Apple Silicon are workable but each needs their own
detection path; punt to a follow-up.
"""

from __future__ import annotations

import ctypes
import logging
import os
import shutil
import subprocess
from dataclasses import dataclass


logger = logging.getLogger(__name__)


@dataclass
class GPU:
    name: str
    vram_total_mb: int
    vram_free_mb: int | None = None
    driver_version: str | None = None
    compute_capability: str | None = None  # "5.0", "6.1", "8.9", …


@dataclass
class HardwareInfo:
    gpus: list[GPU]
    ram_total_bytes: int
    platform: str  # "windows" / "linux" / "darwin"

    @property
    def primary_gpu(self) -> GPU | None:
        if not self.gpus:
            return None
        # Pick the card with the most VRAM (matches user intent on multi-GPU rigs).
        return max(self.gpus, key=lambda g: g.vram_total_mb)

    @property
    def ram_total_gb(self) -> float:
        return self.ram_total_bytes / (1024 ** 3)


# ---- GPU detection -------------------------------------------------------


def detect_gpus() -> list[GPU]:
    """Return all NVIDIA GPUs reported by nvidia-smi, sorted by VRAM."""
    if shutil.which("nvidia-smi") is None:
        return []
    try:
        out = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=name,memory.total,memory.free,driver_version,compute_cap",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    if out.returncode != 0:
        return []
    gpus: list[GPU] = []
    for raw in out.stdout.strip().splitlines():
        fields = [p.strip() for p in raw.split(",")]
        if len(fields) < 5:
            # Older driver: compute_cap missing.
            fields = fields + [""] * (5 - len(fields))
        try:
            total = int(float(fields[1]))
        except ValueError:
            continue
        try:
            free = int(float(fields[2]))
        except ValueError:
            free = None
        gpus.append(
            GPU(
                name=fields[0],
                vram_total_mb=total,
                vram_free_mb=free,
                driver_version=fields[3] or None,
                compute_capability=fields[4] or None,
            )
        )
    return sorted(gpus, key=lambda g: g.vram_total_mb, reverse=True)


# ---- RAM detection -------------------------------------------------------


def detect_ram_bytes() -> int:
    """Total physical RAM in bytes. Best-effort, OS-specific fallbacks."""
    # Preferred: psutil. Optional dep — we don't want a hard failure.
    try:
        import psutil

        return int(psutil.virtual_memory().total)
    except ImportError:
        pass

    # Linux: /proc/meminfo
    try:
        with open("/proc/meminfo", encoding="ascii") as f:
            for line in f:
                if line.startswith("MemTotal:"):
                    return int(line.split()[1]) * 1024
    except OSError:
        pass

    # Windows: GlobalMemoryStatusEx via ctypes.
    if os.name == "nt":
        try:
            kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]

            class _MEMORYSTATUSEX(ctypes.Structure):
                _fields_ = [
                    ("dwLength", ctypes.c_uint32),
                    ("dwMemoryLoad", ctypes.c_uint32),
                    ("ullTotalPhys", ctypes.c_uint64),
                    ("ullAvailPhys", ctypes.c_uint64),
                    ("ullTotalPageFile", ctypes.c_uint64),
                    ("ullAvailPageFile", ctypes.c_uint64),
                    ("ullTotalVirtual", ctypes.c_uint64),
                    ("ullAvailVirtual", ctypes.c_uint64),
                    ("ullAvailExtendedVirtual", ctypes.c_uint64),
                ]

            ms = _MEMORYSTATUSEX()
            ms.dwLength = ctypes.sizeof(_MEMORYSTATUSEX)
            if kernel32.GlobalMemoryStatusEx(ctypes.byref(ms)):
                return int(ms.ullTotalPhys)
        except Exception:
            pass

    # macOS: sysctl hw.memsize
    if os.uname().sysname == "Darwin" if hasattr(os, "uname") else False:
        try:
            out = subprocess.run(
                ["sysctl", "-n", "hw.memsize"], capture_output=True, text=True, timeout=2
            )
            return int(out.stdout.strip())
        except Exception:
            pass

    return 0


def detect() -> HardwareInfo:
    plat = "windows" if os.name == "nt" else (
        "darwin" if (hasattr(os, "uname") and os.uname().sysname == "Darwin") else "linux"
    )
    return HardwareInfo(
        gpus=detect_gpus(),
        ram_total_bytes=detect_ram_bytes(),
        platform=plat,
    )
