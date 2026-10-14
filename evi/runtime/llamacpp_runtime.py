"""Acquire a prebuilt `llama-server` binary — download + unpack the CPU build
for this OS from a pinned llama.cpp GitHub release, into `~/.evi/runtime/`.

CPU-only by design (Phase 1): it never mismatches a GPU/driver. The class of bug
this avoids is real — the RTX 5070 Ti (Blackwell / sm_120) needs the CUDA >=12.8
build; the CUDA 12.4 asset silently ignores the GPU. GPU acquisition (with a
compute-capability -> min-CUDA-build table) is Phase 3.

Stdlib-only (urllib + zipfile/tarfile) so it runs in the frozen desktop sidecar,
which does not bundle `huggingface_hub`.

Every download is checked against a pinned sha256 before it is extracted,
chmod'ed or run (`_ASSET_SHA256` here; `sha256` per entry in the model
catalog), and archives are unpacked with path checks so no member can land
outside the runtime dir. An install is staged beside the version dir and only
moved into place once every archive verified and unpacked, so a failure never
leaves a `llama-server` that looks installed without what it loads.
`scripts/pin_runtime_hashes.py` regenerates the pins.
"""

from __future__ import annotations

import hashlib
import os
import platform
import re
import secrets
import shutil
import tarfile
import tempfile
import time
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Callable, Iterable, Mapping

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

# sha256 of every asset in _CPU_ASSETS and _CUDA_ASSETS, from the release's
# published asset digests. A download that doesn't match is discarded before
# it is extracted, and an asset with no pin is never downloaded, so a version
# bump without re-pinning fails closed. Regenerate the block with
# `python scripts/pin_runtime_hashes.py` whenever LLAMACPP_VERSION changes.
# BEGIN PINNED SHA256 (scripts/pin_runtime_hashes.py)
_ASSET_SHA256: dict[str, str] = {
    "cudart-llama-bin-win-cuda-12.4-x64.zip":
        "8c79a9b226de4b3cacfd1f83d24f962d0773be79f1e7b75c6af4ded7e32ae1d6",
    "cudart-llama-bin-win-cuda-13.3-x64.zip":
        "1462a050eb4c684921ba51dcc4cc488a036674c3e73e9945ee705b854808d03e",
    "llama-b10453-bin-macos-arm64.tar.gz":
        "f1531b1c520f8b473d83352c5eec2f4f43bd0a54f9ca1366a6f202211cfbc098",
    "llama-b10453-bin-macos-x64.tar.gz":
        "ac13f6f6c90c193765921bf52dd5ecf2a9d506ee9c3eadd2d6fd49ca7a5de25d",
    "llama-b10453-bin-ubuntu-arm64.tar.gz":
        "b164e72dfb69c711275178e0d0fae54748042f039e4fe7386f1c0ea7019c109c",
    "llama-b10453-bin-ubuntu-x64.tar.gz":
        "550eb155a09c3051c7add5becf6d0badc3a4c33416807985963036b27b859fb4",
    "llama-b10453-bin-win-cpu-arm64.zip":
        "a8b984d478700777d4671cf33eccfddae42c1fd871e78efd43fee090131eec1f",
    "llama-b10453-bin-win-cpu-x64.zip":
        "70c07211d0027305f0be09cd755d79641ebb0bb646590ff3d498c66b22df29b0",
    "llama-b10453-bin-win-cuda-12.4-x64.zip":
        "84b863f70a8b4c2873e93385d0b208f24776ecd1b946a2cb6d5cda863d143c3d",
    "llama-b10453-bin-win-cuda-13.3-x64.zip":
        "92cb01d69bd52cf307914d7a7fc187d81434269db4fc9561eaa48f6ebdffef06",
}
# END PINNED SHA256

ProgressCB = Callable[[int, int], None]  # (downloaded_bytes, total_bytes)

_SHA256_RE = re.compile(r"[0-9a-f]{64}")
# Module-level tables of downloadable release assets: _CPU_ASSETS, _CUDA_ASSETS
# and any added later (a Vulkan build, say). Matched by name, so a new table is
# pinned and checked without anyone remembering to register it.
_ASSET_TABLE_RE = re.compile(r"_[A-Z0-9]+(?:_[A-Z0-9]+)*_ASSETS")
# A download or install still in progress writes its temp files at least every
# MiB; one untouched this long was abandoned by a killed process.
_STALE_SECONDS = 3600.0
_WORK_PREFIX = ".install-"


class IntegrityError(RuntimeError):
    """A download or archive failed verification: no pin, a sha256 mismatch,
    or a member that would land outside the extraction dir."""


def expected_sha256(value: str | None, what: str) -> str:
    """Normalize a pinned digest ("sha256:" prefix and case tolerated). Raises
    IntegrityError for a missing or malformed pin: nothing downloads unverified."""
    v = (value or "").strip().lower().removeprefix("sha256:")
    if not _SHA256_RE.fullmatch(v):
        raise IntegrityError(
            f"No pinned sha256 for {what}, so eVi won't download it unverified. "
            "This eVi build is missing the pin; developers re-pin with "
            "scripts/pin_runtime_hashes.py."
        )
    return v


def asset_sha256(asset: str) -> str:
    """The pinned sha256 of a llama.cpp release asset (IntegrityError if none)."""
    return expected_sha256(_ASSET_SHA256.get(asset), asset)


def _asset_leaves(value: object) -> set[str]:
    if isinstance(value, str):
        return {value}
    if isinstance(value, dict):
        value = list(value.values())  # values only: the keys are (os, arch) etc.
    if isinstance(value, (list, tuple, set, frozenset)):
        return set().union(*(_asset_leaves(v) for v in value))
    return set()


def asset_names(namespace: Mapping[str, object]) -> set[str]:
    """Every asset name (string leaf, never a key) in the `_*_ASSETS` tables
    of `namespace`."""
    return set().union(*(_asset_leaves(v) for k, v in namespace.items()
                         if _ASSET_TABLE_RE.fullmatch(k)))


def release_assets() -> set[str]:
    """Every llama.cpp release asset this module can download: the single list
    scripts/pin_runtime_hashes.py pins and the CI guard checks."""
    return asset_names(globals())


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


_ASSET_SIZE_CACHE: dict[str, str] = {}


def asset_size_label() -> str:
    """Human size of this platform's runtime download ("41 MB"), or "" if unknown.

    A HEAD against the pinned release — cheap, cached for the process, and
    deliberately failure-tolerant: the install manifest would rather omit a size
    than block on the network or state one it had to guess.
    """
    asset = cpu_asset()
    if not asset:
        return ""
    if asset in _ASSET_SIZE_CACHE:
        return _ASSET_SIZE_CACHE[asset]
    label = ""
    try:
        import urllib.request

        req = urllib.request.Request(f"{_RELEASE_BASE}/{asset}", method="HEAD")
        with urllib.request.urlopen(req, timeout=4) as resp:  # noqa: S310
            n = int(resp.headers.get("Content-Length") or 0)
        if n:
            label = f"{round(n / (1 << 20))} MB"
    except Exception:  # noqa: BLE001
        label = ""
    # Cache successes only: caching a failure would suppress the size for the
    # rest of the process, so one flaky moment would permanently hide how big
    # the download is.
    if label:
        _ASSET_SIZE_CACHE[asset] = label
    return label


def validate_server_binary(path: str | Path) -> bool:
    """True if `path` is really a `llama-server` (not just any executable). Used by
    the 'locate existing install' flow to vet a user-provided binary before it's
    saved. We check `--help` for the HTTP-server flags that `llama-server`
    advertises but the OTHER llama.cpp binaries (llama-cli / -quantize) and
    unrelated tools (python, ls, …) do not — so a mis-pointed path is rejected up
    front instead of being persisted and only failing later at server start."""
    p = Path(path)
    if not p.is_file():
        return False
    try:
        import subprocess

        kwargs: dict = {"capture_output": True, "timeout": 15}
        if platform.system().lower() == "windows":
            kwargs["creationflags"] = 0x0800_0000  # CREATE_NO_WINDOW
        r = subprocess.run([str(p), "--help"], **kwargs)  # noqa: S603
        blob = ((r.stdout or b"") + (r.stderr or b"")).lower()
        return b"--port" in blob and b"--host" in blob
    except Exception:  # noqa: BLE001
        return False


def detect_external() -> list[str]:
    """Existing `llama-server` binaries already on this machine (PATH + common
    install dirs), EXCLUDING eVi's own managed copy. Powers the one-click
    'locate existing install' option — the user confirms one instead of typing a
    path. Never runs the binary; just checks the filesystem (cheap)."""
    import shutil

    name = _server_name()
    managed = str(runtime_root()).lower()
    found: list[str] = []
    seen: set[str] = set()

    def add(cand: Path) -> None:
        try:
            resolved = str(cand.resolve())
        except Exception:  # noqa: BLE001
            resolved = str(cand)
        if resolved.lower().startswith(managed):
            return  # that's eVi's own managed binary, not an "existing" one
        key = resolved.lower()
        if key in seen:
            return
        seen.add(key)
        found.append(str(cand))

    on_path = shutil.which(name) or shutil.which("llama-server")
    if on_path and Path(on_path).is_file():
        add(Path(on_path))

    home = Path.home()
    for cand in (
        home / "llama.cpp" / name,
        home / "llama.cpp" / "build" / "bin" / name,
        home / ".local" / "bin" / name,
        Path("/usr/local/bin") / name,
        Path("/opt/homebrew/bin") / name,
        Path("/usr/bin") / name,
        Path("C:/llama.cpp") / name,
        Path("C:/Program Files/llama.cpp") / name,
        Path("C:/tools/llama.cpp") / name,
    ):
        try:
            if cand.is_file():
                add(cand)
        except Exception:  # noqa: BLE001
            pass
    return found


def download_file(
    url: str, dest: Path, on_bytes: ProgressCB | None = None, *, sha256: str
) -> Path:
    """Stream `url` to `dest` (stdlib only), verifying it against `sha256`.
    Reports (downloaded, total) bytes.

    Bytes go to a `.part` sidecar and are hashed as they stream; only a file
    whose digest matches the pin is atomically renamed onto `dest`. A mismatch,
    a missing pin or a failed transfer deletes the sidecar and raises, so
    nothing unverified (or truncated) is ever left where it would be trusted.
    Each call creates its own uniquely named sidecar, so two processes fetching
    the same `dest` never write into (or rename) each other's bytes.
    """
    expected = expected_sha256(sha256, dest.name)
    dest.parent.mkdir(parents=True, exist_ok=True)
    _sweep_stale_parts(dest)
    tmp = dest.with_name(f"{dest.name}.{secrets.token_hex(6)}.part")
    digest = hashlib.sha256()
    created = ok = False
    try:
        with open(tmp, "xb") as f:  # exclusive create: never someone else's file
            created = True
            req = urllib.request.Request(url, headers={"User-Agent": "evi-runtime"})
            with urllib.request.urlopen(req, timeout=60) as resp:
                total = int(resp.headers.get("Content-Length") or 0)
                done = 0
                while True:
                    chunk = resp.read(1 << 20)  # 1 MiB
                    if not chunk:
                        break
                    f.write(chunk)
                    digest.update(chunk)
                    done += len(chunk)
                    if on_bytes:
                        on_bytes(done, total)
        actual = digest.hexdigest()
        if actual != expected:
            raise IntegrityError(
                f"{dest.name} failed verification (sha256 {actual}, expected "
                f"{expected}); the download was discarded. Retry; if it keeps "
                "failing, the file changed upstream or was altered in transit."
            )
        tmp.replace(dest)
        ok = True
    finally:
        if created and not ok:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass  # e.g. an AV scanner holding it: never mask the real error
    return dest


def _sweep_stale_parts(dest: Path) -> None:
    """Delete `.part` sidecars of `dest` that a killed download left behind.
    Sidecar names are unique per call, so nothing else would ever reuse (and
    so overwrite) them; a live one is written every MiB and is kept."""
    cutoff = time.time() - _STALE_SECONDS
    prefix = dest.name + "."
    try:
        siblings = list(dest.parent.iterdir())
    except OSError:
        return
    for p in siblings:
        if p.name.startswith(prefix) and p.name.endswith(".part"):
            try:
                if p.stat().st_mtime < cutoff:
                    p.unlink()
            except OSError:
                pass


def _member_target(name: str, root: Path) -> Path:
    """Where archive member `name` lands under the resolved `root`. Raises
    IntegrityError for an absolute or drive path, a `..` component, or anything
    that resolves outside `root`, instead of silently rewriting the name."""
    posix = name.replace("\\", "/")
    win = PureWindowsPath(name)
    if (not posix or posix.startswith("/") or win.drive or win.root
            or ".." in PurePosixPath(posix).parts):
        raise IntegrityError(f"Unsafe archive member {name!r}; refusing to extract.")
    target = (root / posix).resolve()
    if target != root and root not in target.parents:
        raise IntegrityError(f"Archive member {name!r} escapes {root}; refusing to extract.")
    return target


def _extract(archive: Path, dest: Path) -> None:
    """Unpack a verified release archive into `dest`.

    Every member name is checked before anything is written, so an archive
    naming a path outside `dest` is refused before its first byte lands. Tar
    members then go through tarfile's 'data' filter again as they extract (no
    links out of `dest`, no device files, no setuid bits); zipfile has no
    filter, so its member names are the whole check (it never creates links).

    Not atomic by itself: an I/O error, or an escape only the data filter can
    see, stops it with earlier members already written (all inside `dest`).
    That's why `_install` only ever extracts into a private staging dir and
    discards it on any error.
    """
    dest.mkdir(parents=True, exist_ok=True)
    root = dest.resolve()
    if archive.suffix == ".zip":
        with zipfile.ZipFile(archive) as z:
            for name in z.namelist():
                _member_target(name, root)
            z.extractall(root)
    else:  # .tar.gz
        with tarfile.open(archive, "r:gz") as t:
            try:
                for member in t.getmembers():
                    _member_target(member.name, root)
                    tarfile.data_filter(member, str(root))
                t.extractall(root, filter="data")
            except tarfile.FilterError as exc:
                raise IntegrityError(
                    f"Unsafe member in {archive.name}: {exc}; refusing to extract."
                ) from exc


def _newest_mtime(path: Path) -> float:
    newest = path.lstat().st_mtime
    for p in path.rglob("*"):
        newest = max(newest, p.lstat().st_mtime)
    return newest


def _sweep_stale_work(parent: Path) -> None:
    """Remove install staging dirs a killed run left in `parent`; one that's
    still being written to (by another process) is kept."""
    cutoff = time.time() - _STALE_SECONDS
    for d in parent.glob(_WORK_PREFIX + "*"):
        try:
            if d.is_dir() and not d.is_symlink() and _newest_mtime(d) < cutoff:
                shutil.rmtree(d, ignore_errors=True)
        except OSError:
            pass


def _publish(tree: Path, dest: Path, reserved: frozenset[str] = frozenset()) -> None:
    """Move `tree`'s top-level entries into `dest`, the one holding the server
    binary LAST: `server_path()`/`gpu_server_path()` only find a server once
    everything it loads is already beside it. Leftovers of an earlier failed
    install are replaced; names in `reserved` (the CPU build's `gpu/` dir) are
    never touched."""
    entries = list(tree.iterdir()) if tree.is_dir() else []
    clash = sorted(e.name for e in entries if e.name.lower() in reserved)
    if clash:
        raise IntegrityError(f"Archive entry {clash[0]!r} would replace {dest / clash[0]}; "
                             "refusing to install.")
    name = _server_name()

    def holds_server(p: Path) -> bool:
        return p.name == name or (p.is_dir() and not p.is_symlink() and any(p.rglob(name)))

    dest.mkdir(parents=True, exist_ok=True)
    for src in sorted(entries, key=holds_server):  # False (the rest) first
        target = dest / src.name
        if target.is_dir() and not target.is_symlink():
            shutil.rmtree(target)
        elif src.is_dir() and (target.exists() or target.is_symlink()):
            target.unlink()
        os.replace(src, target)


def _install(assets: Iterable[str], dest: Path, on_bytes: ProgressCB | None,
             reserved: frozenset[str] = frozenset()) -> None:
    """Download, verify and unpack release `assets` into `dest`, all or nothing.

    Every pin is resolved before anything is fetched, every archive is verified
    before any is extracted, and they're all unpacked into a private staging
    dir beside the version dir that `_publish` moves into `dest` only once all
    of them succeeded. A bad hash, an unsafe member or an I/O error (full disk,
    a locked file) therefore never leaves a server that looks installed without
    its libraries; the staging dir and archives are always removed.
    """
    assets = list(assets)
    pins = {a: asset_sha256(a) for a in assets}  # no pin -> no download, no dirs
    parent = runtime_root().parent
    parent.mkdir(parents=True, exist_ok=True)
    _sweep_stale_work(parent)
    work = Path(tempfile.mkdtemp(prefix=f"{_WORK_PREFIX}{LLAMACPP_VERSION}-", dir=parent))
    try:
        archives = []
        for asset in assets:
            archive = work / asset
            download_file(f"{_RELEASE_BASE}/{asset}", archive, on_bytes, sha256=pins[asset])
            archives.append(archive)
        tree = work / "tree"
        for archive in archives:
            _extract(archive, tree)
        _publish(tree, dest, reserved)
    finally:
        shutil.rmtree(work, ignore_errors=True)


def ensure_runtime(on_bytes: ProgressCB | None = None) -> Path:
    """Return a ready `llama-server` path, downloading + extracting the CPU build
    for this OS if it isn't installed. Raises RuntimeError on an unsupported
    platform or a download/extract failure (IntegrityError, a RuntimeError, when
    the archive doesn't match its pinned sha256 — before anything is extracted,
    chmod'ed or run). Nothing is moved into the runtime dir unless the whole
    archive verified and unpacked."""
    existing = server_path()
    if existing is not None:
        return existing

    asset = cpu_asset()
    if asset is None:
        raise RuntimeError(
            f"No prebuilt llama.cpp CPU runtime for "
            f"{platform.system()}/{platform.machine()}."
        )
    _install([asset], runtime_root(), on_bytes, reserved=frozenset({"gpu"}))

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
    CUDA runtime for this machine/GPU.

    Both zips are verified and unpacked into a staging dir before anything
    reaches gpu/, so a bad cudart zip or a failed extraction can't leave a
    llama-server.exe without its DLLs that gpu_installed() would then report."""
    existing = gpu_server_path()
    if existing is not None:
        return existing
    plan = gpu_plan(compute_cap=compute_cap)
    if not plan or plan.get("mode") != "cuda":
        raise RuntimeError("No prebuilt GPU (CUDA) runtime for this machine.")
    _install(plan["assets"], gpu_root(), on_bytes)  # llama build + the cudart DLLs
    server = gpu_server_path()
    if server is None:
        raise RuntimeError("CUDA runtime extracted but llama-server not found.")
    return server
