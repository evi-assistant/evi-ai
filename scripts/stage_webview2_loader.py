#!/usr/bin/env python3
"""Stage WebView2Loader.dll for a Windows GNU-toolchain desktop build.

    python scripts/stage_webview2_loader.py [--target TRIPLE]

With the MSVC toolchain (what CI uses) the WebView2 loader is linked into
evi-desktop.exe and there is nothing to do: this prints nothing. With the GNU
toolchain (`x86_64-pc-windows-gnu`, e.g. a machine without the MSVC Build
Tools) the exe loads WebView2Loader.dll at startup, and an installer without
it fails on launch with "WebView2Loader.dll was not found". tauri-build copies
the DLL next to the exe in `target/`, but the bundler never packs it.

For a GNU target this copies the DLL that ships inside the locked
`webview2-com-sys` crate (Microsoft's signed loader, the one the exe was
linked against; found with `cargo metadata`, so it works before the first
compile) to desktop/src-tauri/binaries/WebView2Loader.dll, and prints the
Tauri config overlay that bundles it next to the exe:

    src-tauri/tauri.webview2-gnu.conf.json

Pass that to `tauri build` as an extra `--config` (scripts/build-desktop.*
do). Exit status 1 with a message on stderr when the DLL can't be found.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TAURI_DIR = ROOT / "desktop" / "src-tauri"
DEST = TAURI_DIR / "binaries" / "WebView2Loader.dll"
# Relative to desktop/, where `npm run tauri build` runs.
OVERLAY = "src-tauri/tauri.webview2-gnu.conf.json"
# Rust target arch -> the folder webview2-com-sys keeps each loader in.
ARCH_DIRS = {"x86_64": "x64", "i686": "x86", "aarch64": "arm64"}


def host_triple() -> str | None:
    """The target cargo builds for: CARGO_BUILD_TARGET, else rustc's host."""
    if os.environ.get("CARGO_BUILD_TARGET"):
        return os.environ["CARGO_BUILD_TARGET"]
    try:
        out = subprocess.run(["rustc", "-vV"], capture_output=True, text=True,
                             encoding="utf-8", errors="replace", check=True).stdout
    except (OSError, subprocess.CalledProcessError):
        return None
    for line in out.splitlines():
        if line.startswith("host:"):
            return line.split(":", 1)[1].strip()
    return None


def needs_loader(triple: str | None) -> bool:
    """Whether `triple` loads WebView2Loader.dll at runtime (Windows, GNU ABI)."""
    return bool(triple) and triple.endswith(("-windows-gnu", "-windows-gnullvm"))


def crate_dir(manifest: Path = TAURI_DIR / "Cargo.toml") -> Path | None:
    """The locked webview2-com-sys package's source folder (`cargo metadata`)."""
    out = subprocess.run(
        ["cargo", "metadata", "--locked", "--format-version", "1",
         "--manifest-path", str(manifest)],
        capture_output=True, text=True, encoding="utf-8", errors="replace", check=True,
    ).stdout
    for pkg in json.loads(out)["packages"]:
        if pkg["name"] == "webview2-com-sys":
            return Path(pkg["manifest_path"]).parent
    return None


def stage(triple: str, source_dir: Path, dest: Path | None = None) -> Path:
    """Copy `<source_dir>/<arch>/WebView2Loader.dll` to `dest` (default `DEST`)."""
    dest = DEST if dest is None else dest
    arch = ARCH_DIRS.get(triple.split("-", 1)[0])
    if arch is None:
        raise FileNotFoundError(f"no WebView2 loader for target {triple!r}")
    src = source_dir / arch / "WebView2Loader.dll"
    if not src.is_file():
        raise FileNotFoundError(f"{src} doesn't exist")
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dest)
    return dest


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--target", help="Rust target triple (default: CARGO_BUILD_TARGET or rustc's host)")
    args = ap.parse_args(argv)
    triple = args.target or host_triple()
    if not needs_loader(triple):
        return 0  # MSVC (or not Windows): the loader is linked in
    try:
        source = crate_dir()
        if source is None:
            raise FileNotFoundError("webview2-com-sys isn't in desktop/src-tauri/Cargo.lock")
        dest = stage(triple, source)
    except (OSError, subprocess.CalledProcessError, ValueError, KeyError) as e:
        print(f"stage_webview2_loader: can't stage WebView2Loader.dll for {triple}: {e}",
              file=sys.stderr)
        return 1
    print(f"staged {dest} for {triple}", file=sys.stderr)
    print(OVERLAY)
    return 0


if __name__ == "__main__":
    sys.exit(main())
