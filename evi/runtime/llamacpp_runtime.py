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
    """Path to an installed `llama-server`, or None if not installed yet."""
    root = runtime_root()
    if not root.is_dir():
        return None
    name = _server_name()
    direct = root / name
    if direct.is_file():
        return direct
    # Some archives nest the binaries (e.g. build/bin/) — locate it.
    for p in root.rglob(name):
        if p.is_file():
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
