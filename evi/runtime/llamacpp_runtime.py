"""Acquire a prebuilt `llama-server` binary — download + unpack the CPU build
for this OS from a pinned llama.cpp GitHub release, into `~/.evi/runtime/`.

CPU-only by design (Phase 1): it never mismatches a GPU/driver. The class of bug
this avoids is real — the RTX 5070 Ti (Blackwell / sm_120) needs the CUDA >=12.8
build; the CUDA 12.4 asset silently ignores the GPU. GPU acquisition (with a
compute-capability -> min-CUDA-build table) is Phase 3.

Stdlib-only (urllib + zipfile/tarfile) so it runs in the frozen desktop sidecar,
which does not bundle `huggingface_hub`.
"""

from __future__ import annotations

import platform
import tarfile
import urllib.request
import zipfile
from pathlib import Path
from typing import Callable

from evi.config import RUNTIME_DIR

# Pinned llama.cpp release — bump deliberately, like any dependency. Binaries are
# pulled from this tag's GitHub release assets.
LLAMACPP_VERSION = "b10453"
_RELEASE_BASE = (
    f"https://github.com/ggml-org/llama.cpp/releases/download/{LLAMACPP_VERSION}"
)

# (system, normalized-arch) -> CPU build asset. Windows ships .zip; others .tar.gz.
_CPU_ASSETS: dict[tuple[str, str], str] = {
    ("windows", "x86_64"): f"llama-{LLAMACPP_VERSION}-bin-win-cpu-x64.zip",
    ("windows", "arm64"): f"llama-{LLAMACPP_VERSION}-bin-win-cpu-arm64.zip",
    ("linux", "x86_64"): f"llama-{LLAMACPP_VERSION}-bin-ubuntu-x64.tar.gz",
    ("linux", "arm64"): f"llama-{LLAMACPP_VERSION}-bin-ubuntu-arm64.tar.gz",
    ("darwin", "arm64"): f"llama-{LLAMACPP_VERSION}-bin-macos-arm64.tar.gz",
    ("darwin", "x86_64"): f"llama-{LLAMACPP_VERSION}-bin-macos-x64.tar.gz",
}

ProgressCB = Callable[[int, int], None]  # (downloaded_bytes, total_bytes)


def _norm_arch(machine: str) -> str:
    m = machine.lower()
    if m in ("amd64", "x86_64", "x64"):
        return "x86_64"
    if m in ("arm64", "aarch64"):
        return "arm64"
    return m


def cpu_asset(system: str | None = None, machine: str | None = None) -> str | None:
    """The CPU-build asset name for this OS/arch, or None if unsupported.

    `system`/`machine` are injectable for tests (default to the real platform).
    """
    sys_name = (system or platform.system()).lower()
    arch = _norm_arch(machine or platform.machine())
    return _CPU_ASSETS.get((sys_name, arch))


def supported() -> bool:
    return cpu_asset() is not None


def runtime_root() -> Path:
    return RUNTIME_DIR / "llama" / LLAMACPP_VERSION


def _server_name() -> str:
    return "llama-server.exe" if platform.system().lower() == "windows" else "llama-server"


def server_path() -> Path | None:
    """Path to the installed CPU `llama-server`, or None if not installed yet.
    Excludes the ``gpu/`` subdir (that build is found via ``gpu_server_path``)."""
    root = runtime_root()
    if not root.is_dir():
        return None
    name = _server_name()
    direct = root / name
    if direct.is_file():
        return direct
    # Some archives nest the binaries (e.g. build/bin/) — locate it, but never
    # pick up the separate CUDA build under gpu/.
    for p in root.rglob(name):
        if p.is_file() and "gpu" not in p.relative_to(root).parts:
            return p
    return None


def is_installed() -> bool:
    return server_path() is not None


def download_file(url: str, dest: Path, on_bytes: ProgressCB | None = None) -> Path:
    """Stream `url` to `dest` (stdlib only). Reports (downloaded, total) bytes.

    Writes to a `.part` sidecar and atomically renames on success, so a killed
    download never leaves a truncated file that looks complete.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".part")
    req = urllib.request.Request(url, headers={"User-Agent": "evi-runtime"})
    with urllib.request.urlopen(req, timeout=60) as resp, open(tmp, "wb") as f:
        total = int(resp.headers.get("Content-Length") or 0)
        done = 0
        while True:
            chunk = resp.read(1 << 20)  # 1 MiB
            if not chunk:
                break
            f.write(chunk)
            done += len(chunk)
            if on_bytes:
                on_bytes(done, total)
    tmp.replace(dest)
    return dest


def _extract(archive: Path, dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    if archive.suffix == ".zip":
        with zipfile.ZipFile(archive) as z:
            z.extractall(dest)
    else:  # .tar.gz
        with tarfile.open(archive, "r:gz") as t:
            t.extractall(dest)


def ensure_runtime(on_bytes: ProgressCB | None = None) -> Path:
    """Return a ready `llama-server` path, downloading + extracting the CPU build
    for this OS if it isn't installed. Raises RuntimeError on an unsupported
    platform or a download/extract failure."""
    existing = server_path()
    if existing is not None:
        return existing

    asset = cpu_asset()
    if asset is None:
        raise RuntimeError(
            f"No prebuilt llama.cpp CPU runtime for "
            f"{platform.system()}/{platform.machine()}."
        )
    root = runtime_root()
    root.mkdir(parents=True, exist_ok=True)
    archive = root / asset
    download_file(f"{_RELEASE_BASE}/{asset}", archive, on_bytes)
    _extract(archive, root)
    try:
        archive.unlink()  # reclaim the archive once extracted
    except OSError:
        pass

    server = server_path()
    if server is None:
        raise RuntimeError("llama.cpp runtime extracted but llama-server not found.")
    if platform.system().lower() != "windows":
        server.chmod(0o755)
    return server


# ---- GPU (CUDA) acquisition — Phase 3 ------------------------------------
# Prebuilt CUDA llama.cpp is Windows-only in this release. macOS gets GPU for
# free (the base build is Metal — just pass -ngl); Linux has no prebuilt CUDA
# here, so it stays CPU. Each Windows entry is (llama build zip, cudart runtime
# DLLs zip) — both unzip into the same gpu/ dir next to llama-server.exe.
_CUDA_ASSETS: dict[str, dict[str, tuple[str, str]]] = {
    "x86_64": {
        "13.3": (f"llama-{LLAMACPP_VERSION}-bin-win-cuda-13.3-x64.zip",
                 "cudart-llama-bin-win-cuda-13.3-x64.zip"),
        "12.4": (f"llama-{LLAMACPP_VERSION}-bin-win-cuda-12.4-x64.zip",
                 "cudart-llama-bin-win-cuda-12.4-x64.zip"),
    },
}


def _cuda_build_for(compute_cap: str | None) -> str | None:
    """Which CUDA toolkit build to use for an NVIDIA GPU's compute capability.

    Blackwell (sm_120 / cc >= 12.0) needs CUDA >= 12.8 — the **13.3** build; the
    12.4 build silently ignores it (the exact bug we hit on the RTX 5070 Ti).
    Older archs (cc 5.0–8.9) use the smaller, broadly-compatible 12.4 build.
    """
    try:
        cc = float(compute_cap)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if cc >= 12.0:
        return "13.3"
    if cc >= 5.0:  # llama.cpp CUDA builds target sm_50+
        return "12.4"
    return None


def gpu_plan(
    *, system: str | None = None, machine: str | None = None, compute_cap: str | None = None
) -> dict | None:
    """How to GPU-accelerate on this machine, or None if the managed path can't.

    Returns ``{"mode": "cuda", "build": "13.3", "assets": (llama_zip, cudart_zip)}``
    on Windows+NVIDIA, ``{"mode": "metal"}`` on macOS (no download — the base
    build is Metal), or None (Linux / unsupported arch / unknown cc).
    """
    sys_name = (system or platform.system()).lower()
    if sys_name == "darwin":
        return {"mode": "metal"}
    if sys_name == "windows":
        arch = _norm_arch(machine or platform.machine())
        table = _CUDA_ASSETS.get(arch)
        build = _cuda_build_for(compute_cap) if table else None
        if not build:
            return None
        return {"mode": "cuda", "build": build, "assets": table[build]}
    return None  # linux: no prebuilt CUDA in this release


def gpu_root() -> Path:
    return runtime_root() / "gpu"


def gpu_server_path() -> Path | None:
    root = gpu_root()
    if not root.is_dir():
        return None
    for p in root.rglob(_server_name()):
        if p.is_file():
            return p
    return None


def gpu_installed() -> bool:
    return gpu_server_path() is not None


def ensure_gpu_runtime(compute_cap: str | None, on_bytes: ProgressCB | None = None) -> Path:
    """Download + extract the CUDA build (build + cudart DLLs) for this GPU into
    ~/.evi/runtime/llama/<ver>/gpu/. Raises RuntimeError if there's no prebuilt
    CUDA runtime for this machine/GPU."""
    existing = gpu_server_path()
    if existing is not None:
        return existing
    plan = gpu_plan(compute_cap=compute_cap)
    if not plan or plan.get("mode") != "cuda":
        raise RuntimeError("No prebuilt GPU (CUDA) runtime for this machine.")
    root = gpu_root()
    root.mkdir(parents=True, exist_ok=True)
    for asset in plan["assets"]:  # llama build, then the cudart DLLs
        archive = root / asset
        download_file(f"{_RELEASE_BASE}/{asset}", archive, on_bytes)
        _extract(archive, root)
        try:
            archive.unlink()
        except OSError:
            pass
    server = gpu_server_path()
    if server is None:
        raise RuntimeError("CUDA runtime extracted but llama-server not found.")
    return server
