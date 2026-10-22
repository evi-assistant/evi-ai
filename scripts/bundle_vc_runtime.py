#!/usr/bin/env python3
"""Make sure the frozen sidecar carries the Visual C++ runtime its DLLs need.

    <build-venv python> scripts/bundle_vc_runtime.py dist/evi-server

PyMuPDF (PDF reading) links MSVCP140.dll, VCRUNTIME140.dll and
VCRUNTIME140_1.dll. PyInstaller bundles a DLL only if the build machine has
it: Python ships the two VCRUNTIME ones, but MSVCP140.dll comes from the
Visual C++ Redistributable in System32. Freezing on a machine without the
redistributable (CI's runners have it; a fresh or locked-down box may not)
gave a sidecar whose PDF support failed to load on any PC without it.

For each runtime DLL missing from `<onedir>/_internal/`, this copies one in,
from (best version first): System32, the running Python's own folder, or a
copy a wheel in this venv carries (numpy's `numpy.libs/msvcp140-<hash>.dll`,
the same Microsoft DLL under a mangled name). A candidate counts only if its
Authenticode signature is valid and Microsoft's. Exit 1 when a DLL can't be
found; then install the Visual C++ Redistributable on the build machine.
Run it with the build venv's Python (build-sidecar.ps1 does), so the venv's
packages are the ones searched. Does nothing off Windows.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import site
import subprocess
import sys
from pathlib import Path

# The VC++ runtime DLLs the sidecar's native modules import (objdump -p on
# pymupdf's _mupdf.pyd / _extra.pyd / mupdfcpp64.dll). The api-ms-win-crt-*
# (UCRT) set is part of Windows 10+.
REQUIRED = ("MSVCP140.dll", "VCRUNTIME140.dll", "VCRUNTIME140_1.dll")


def _mangled(name: str) -> re.Pattern[str]:
    """`msvcp140.dll` or a wheel's `msvcp140-<hex>.dll`, but not
    `msvcp140_1.dll` (a different DLL)."""
    stem = re.escape(Path(name).stem)
    return re.compile(rf"{stem}(-[0-9a-f]+)?\.dll", re.IGNORECASE)


def candidates(name: str) -> list[Path]:
    """Places a copy of `name` may be found, in search order."""
    found: list[Path] = []
    windir = Path(os.environ.get("SystemRoot", r"C:\Windows"))
    for folder in (windir / "System32", Path(sys.base_prefix), Path(sys.prefix)):
        p = folder / name
        if p.is_file():
            found.append(p)
    pattern = _mangled(name)
    for sp in {*site.getsitepackages(), site.getusersitepackages()}:
        root = Path(sp)
        if not root.is_dir():
            continue
        for p in root.rglob("*.dll"):
            if pattern.fullmatch(p.name):
                found.append(p)
    # site-packages entries overlap (sys.prefix and its Lib/site-packages):
    # one file, once.
    unique: dict[str, Path] = {}
    for p in found:
        unique.setdefault(os.path.normcase(os.path.realpath(p)), p)
    return list(unique.values())


def signature(paths: list[Path]) -> dict[str, dict]:
    """{path: {"valid": bool, "microsoft": bool, "version": str}} via
    PowerShell's Get-AuthenticodeSignature (Windows only)."""
    if not paths:
        return {}
    # The paths go in through the environment: `-Command` doesn't bind extra
    # arguments to $args.
    script = (
        "$out = @(); foreach ($p in ($env:EVI_VC_PATHS | ConvertFrom-Json)) { "
        "$s = Get-AuthenticodeSignature -LiteralPath $p; "
        "$out += [pscustomobject]@{path=$p; status=[string]$s.Status; "
        "subject=[string]$s.SignerCertificate.Subject; "
        "version=[string](Get-Item -LiteralPath $p).VersionInfo.FileVersion} }; "
        "ConvertTo-Json -InputObject $out -Compress"
    )
    env = {**os.environ, "EVI_VC_PATHS": json.dumps([str(p) for p in paths])}
    out = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True, text=True, encoding="utf-8", errors="replace", check=True, env=env,
    ).stdout
    rows = json.loads(out or "[]")
    return {
        r["path"]: {
            "valid": r["status"] == "Valid",
            "microsoft": "O=Microsoft Corporation" in (r["subject"] or ""),
            "version": r["version"] or "",
        }
        for r in (rows if isinstance(rows, list) else [rows])
    }


def _version_key(v: str) -> tuple[int, ...]:
    return tuple(int(x) for x in re.findall(r"\d+", v)[:4]) or (0,)


def pick(name: str, check=signature) -> Path | None:
    """The newest Microsoft-signed copy of `name`, or None."""
    paths = candidates(name)
    try:
        info = check(paths)
    except (OSError, subprocess.CalledProcessError, ValueError) as e:
        detail = getattr(e, "stderr", "") or e
        print(f"bundle_vc_runtime: couldn't check the signatures of {name} copies: {detail}",
              file=sys.stderr)
        return None
    ok = [p for p in paths if info.get(str(p), {}).get("valid") and info[str(p)]["microsoft"]]
    if not ok:
        return None
    return max(ok, key=lambda p: _version_key(info[str(p)]["version"]))


def present(internal: Path, name: str) -> bool:
    return any(p.name.lower() == name.lower() for p in internal.glob("*.dll"))


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        print(__doc__.strip().splitlines()[2].strip(), file=sys.stderr)
        return 2
    if os.name != "nt":
        return 0
    internal = Path(args[0]) / "_internal"
    if not internal.is_dir():
        print(f"bundle_vc_runtime: {internal} doesn't exist (run PyInstaller --onedir first)",
              file=sys.stderr)
        return 2
    missing = []
    for name in REQUIRED:
        if present(internal, name):
            continue
        src = pick(name)
        if src is None:
            missing.append(name)
            continue
        shutil.copy2(src, internal / name)
        print(f">> bundled {name} from {src}")
    if missing:
        print(f"bundle_vc_runtime: no Microsoft-signed {', '.join(missing)} on this machine; "
              "install the Visual C++ Redistributable (https://aka.ms/vs/17/release/"
              "vc_redist.x64.exe) and build again", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
