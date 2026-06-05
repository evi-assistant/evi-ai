#!/usr/bin/env python3
"""evi-tools — provision the external binaries Evi can use.

This lives OUTSIDE the `evi` package on purpose: it's a bootstrap / ops
helper, not part of the importable library. It manages the helper programs
some Evi tools shell out to:

    - tesseract   (OCR tool)
    - ffmpeg      (audio decode for some STT paths)
    - ollama      (a local LLM backend)

Policy: **prefer the OS package manager** (winget/choco on Windows, brew on
macOS, apt/dnf/pacman on Linux). Only when none is available do we fall
back to downloading a prebuilt binary into `~/.evi/tools/bin/` — keeping
the per-OS/arch download URLs (a maintenance + security surface) to the
minimum that actually needs them (ffmpeg has clean static builds; tesseract
does not, so there we point at the package manager / installer).

Usage:
    python scripts/evi_tools.py list
    python scripts/evi_tools.py install tesseract
    python scripts/evi_tools.py install ffmpeg
    python scripts/evi_tools.py path        # prints the PATH line to add
    python scripts/evi_tools.py remove ffmpeg

`evi doctor` reports what's missing; this closes the loop by installing it.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path


# --- paths ---------------------------------------------------------------


def evi_home() -> Path:
    """Honour EVI_HOME, else ~/.evi — matches evi.config without importing it
    (so this script runs even if the package import is broken)."""
    return Path(os.environ.get("EVI_HOME") or (Path.home() / ".evi"))


def tools_bin_dir() -> Path:
    return evi_home() / "tools" / "bin"


# --- platform / package-manager detection --------------------------------


def os_kind() -> str:
    if os.name == "nt":
        return "windows"
    if sys.platform == "darwin":
        return "macos"
    return "linux"


# Ordered preference per OS; first one found on PATH wins.
_PM_BY_OS = {
    "windows": ["winget", "choco", "scoop"],
    "macos": ["brew"],
    "linux": ["apt-get", "dnf", "pacman", "zypper"],
}


def detect_package_manager() -> str | None:
    for pm in _PM_BY_OS.get(os_kind(), []):
        if shutil.which(pm):
            return pm
    return None


# --- tool registry -------------------------------------------------------

# For each tool: the binary name to probe, and the package id per manager.
# `None` for a manager means "not packaged there / use fallback".
TOOLS: dict[str, dict] = {
    "tesseract": {
        "binary": "tesseract",
        "purpose": "OCR tool (ocr_image / ocr_screen)",
        "pkg": {
            "winget": "UB-Mannheim.TesseractOCR",
            "choco": "tesseract",
            "scoop": "tesseract",
            "brew": "tesseract",
            "apt-get": "tesseract-ocr",
            "dnf": "tesseract",
            "pacman": "tesseract",
            "zypper": "tesseract-ocr",
        },
        # No clean portable binary — fall back to a manual pointer.
        "download": None,
        "manual": "https://tesseract-ocr.github.io/tessdoc/Installation.html",
    },
    "ffmpeg": {
        "binary": "ffmpeg",
        "purpose": "audio decode for some STT paths",
        "pkg": {
            "winget": "Gyan.FFmpeg",
            "choco": "ffmpeg",
            "scoop": "ffmpeg",
            "brew": "ffmpeg",
            "apt-get": "ffmpeg",
            "dnf": "ffmpeg",
            "pacman": "ffmpeg",
            "zypper": "ffmpeg",
        },
        # Clean static builds exist per-OS; used only when no pkg manager.
        "download": {
            "windows": "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip",
            "linux": "https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-amd64-static.tar.xz",
            "macos": "https://evermeet.cx/ffmpeg/getrelease/zip",
        },
        "manual": "https://ffmpeg.org/download.html",
    },
    "ollama": {
        "binary": "ollama",
        "purpose": "local LLM backend (then: evi models backend ollama)",
        "pkg": {
            "winget": "Ollama.Ollama",
            "choco": "ollama",
            "scoop": "ollama",
            "brew": "ollama",
            # On Linux the official one-liner is the blessed path.
            "apt-get": None,
            "dnf": None,
            "pacman": "ollama",
            "zypper": None,
        },
        "download": None,
        "manual": "https://ollama.com/download",
        # Linux official installer when no usable package.
        "script": {"linux": "curl -fsSL https://ollama.com/install.sh | sh"},
    },
}


# --- install-command planning (pure, testable) ---------------------------


def pkg_install_command(tool: str, pm: str) -> list[str] | None:
    """Return the argv to install `tool` via package manager `pm`, or None
    if `pm` doesn't package it."""
    spec = TOOLS[tool]["pkg"].get(pm)
    if not spec:
        return None
    if pm == "winget":
        return ["winget", "install", "--silent", "--accept-package-agreements",
                "--accept-source-agreements", "--id", spec]
    if pm == "choco":
        return ["choco", "install", "-y", spec]
    if pm == "scoop":
        return ["scoop", "install", spec]
    if pm == "brew":
        return ["brew", "install", spec]
    if pm == "apt-get":
        return ["sudo", "apt-get", "install", "-y", spec]
    if pm == "dnf":
        return ["sudo", "dnf", "install", "-y", spec]
    if pm == "pacman":
        return ["sudo", "pacman", "-S", "--noconfirm", spec]
    if pm == "zypper":
        return ["sudo", "zypper", "install", "-y", spec]
    return None


def plan_install(tool: str, pm: str | None) -> tuple[str, object]:
    """Decide how to install. Returns (kind, detail):

    - ("pkg", argv)        — run the package-manager command
    - ("script", cmdline)  — run a shell one-liner (e.g. Ollama on Linux)
    - ("download", url)    — fetch a prebuilt binary into ~/.evi/tools/bin
    - ("manual", url)      — no automated path; point the user at instructions
    """
    info = TOOLS[tool]
    if pm is not None:
        argv = pkg_install_command(tool, pm)
        if argv is not None:
            return ("pkg", argv)
    osk = os_kind()
    script = info.get("script", {}).get(osk)
    if script:
        return ("script", script)
    dl = (info.get("download") or {}).get(osk)
    if dl:
        return ("download", dl)
    return ("manual", info.get("manual", ""))


# --- actions -------------------------------------------------------------


def _found(binary: str) -> str | None:
    """which() that also searches ~/.evi/tools/bin even if not on PATH."""
    hit = shutil.which(binary)
    if hit:
        return hit
    local = tools_bin_dir() / (binary + (".exe" if os_kind() == "windows" else ""))
    return str(local) if local.is_file() else None


def cmd_list(_args) -> int:
    pm = detect_package_manager()
    print(f"package manager: {pm or '(none detected)'}")
    print(f"tools bin dir:   {tools_bin_dir()}\n")
    for name, info in TOOLS.items():
        loc = _found(info["binary"])
        mark = "OK " if loc else "-- "
        where = f"  {loc}" if loc else ""
        print(f"[{mark}] {name:<10} {info['purpose']}{where}")
    return 0


def cmd_path(_args) -> int:
    d = tools_bin_dir()
    if os_kind() == "windows":
        print(f'$env:PATH = "{d};$env:PATH"   # PowerShell (this session)')
        print(f'setx PATH "{d};%PATH%"        # persist (new shells)')
    else:
        print(f'export PATH="{d}:$PATH"       # add to ~/.bashrc or ~/.zshrc')
    return 0


def _download_into_tools(url: str, binary: str) -> int:
    """Best-effort: download an archive and extract the binary into
    ~/.evi/tools/bin. Archive layouts vary, so we search for the binary by
    name after extraction."""
    import io
    import tarfile
    import tempfile
    import urllib.request
    import zipfile

    dest = tools_bin_dir()
    dest.mkdir(parents=True, exist_ok=True)
    print(f"downloading {url} …")
    try:
        with urllib.request.urlopen(url, timeout=60) as r:  # noqa: S310
            blob = r.read()
    except Exception as exc:  # noqa: BLE001
        print(f"download failed: {exc}", file=sys.stderr)
        return 1

    exe = binary + (".exe" if os_kind() == "windows" else "")
    with tempfile.TemporaryDirectory() as tmp:
        tmpd = Path(tmp)
        try:
            if url.endswith(".zip") or url.endswith("/zip"):
                zipfile.ZipFile(io.BytesIO(blob)).extractall(tmpd)
            else:
                tarfile.open(fileobj=io.BytesIO(blob)).extractall(tmpd)  # noqa: S202
        except Exception as exc:  # noqa: BLE001
            print(f"extract failed: {exc}", file=sys.stderr)
            return 1
        found = next((p for p in tmpd.rglob(exe) if p.is_file()), None)
        if found is None:
            print(f"could not find {exe} inside the archive", file=sys.stderr)
            return 1
        target = dest / exe
        shutil.copy2(found, target)
        if os_kind() != "windows":
            target.chmod(0o755)
    print(f"installed → {dest / exe}")
    print("run `python scripts/evi_tools.py path` to put it on PATH.")
    return 0


def cmd_install(args) -> int:
    tool = args.tool
    if tool not in TOOLS:
        print(f"unknown tool: {tool} (known: {', '.join(TOOLS)})", file=sys.stderr)
        return 2
    if _found(TOOLS[tool]["binary"]) and not args.force:
        print(f"{tool} already available (use --force to reinstall)")
        return 0

    pm = detect_package_manager()
    kind, detail = plan_install(tool, pm)

    if kind == "pkg":
        print("running:", " ".join(detail))
        if args.dry_run:
            return 0
        return subprocess.call(detail)
    if kind == "script":
        print("running:", detail)
        if args.dry_run:
            return 0
        return subprocess.call(detail, shell=True)  # noqa: S602 (trusted constant)
    if kind == "download":
        if args.dry_run:
            print(f"would download: {detail}")
            return 0
        return _download_into_tools(detail, TOOLS[tool]["binary"])
    # manual
    print(f"No automated install for {tool} on this platform.")
    print(f"Install it manually: {detail}")
    return 1


def cmd_remove(args) -> int:
    """Only removes a binary we placed in ~/.evi/tools/bin. Package-manager
    installs should be removed with the package manager."""
    tool = args.tool
    if tool not in TOOLS:
        print(f"unknown tool: {tool}", file=sys.stderr)
        return 2
    exe = TOOLS[tool]["binary"] + (".exe" if os_kind() == "windows" else "")
    target = tools_bin_dir() / exe
    if target.is_file():
        target.unlink()
        print(f"removed {target}")
        return 0
    print(f"{tool} is not in {tools_bin_dir()} "
          "(if installed via a package manager, remove it there).")
    return 1


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="evi-tools", description=__doc__.split("\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list", help="show known tools + whether they're found")
    sub.add_parser("path", help="print the line to add ~/.evi/tools/bin to PATH")

    pi = sub.add_parser("install", help="install a tool (pkg manager first)")
    pi.add_argument("tool", help=f"one of: {', '.join(TOOLS)}")
    pi.add_argument("--force", action="store_true", help="reinstall even if found")
    pi.add_argument("--dry-run", action="store_true", help="print the plan, don't run")

    pr = sub.add_parser("remove", help="remove a tool placed in ~/.evi/tools/bin")
    pr.add_argument("tool")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return {
        "list": cmd_list,
        "path": cmd_path,
        "install": cmd_install,
        "remove": cmd_remove,
    }[args.cmd](args)


if __name__ == "__main__":
    raise SystemExit(main())
