#!/usr/bin/env python3
"""Regenerate the fixtures for the sidecar-update e2e tests in src/sidecar_update.rs.

- sidecar-windows-x86_64.zip / .sha256: written by scripts/zip-sidecar.py itself
  (the release pipeline's archiver) over a fake `evi-server` onedir, so the Rust
  side is tested against a real archive and a digest computed by hashlib.
- hostile.zip: entries that try to climb out of the extraction dir, one safe
  entry, and a unix-mode 0755 binary (as the Linux/macOS runners write it).

Usage (from the repo root):
    python desktop/src-tauri/tests/fixtures/sidecar-update/make_fixtures.py
"""

from __future__ import annotations

import pathlib
import subprocess
import sys
import tempfile
import zipfile

HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parents[4]  # sidecar-update -> fixtures -> tests -> src-tauri -> desktop -> repo


def build_onedir(base: pathlib.Path) -> pathlib.Path:
    d = base / "evi-server"
    (d / "_internal" / "lib").mkdir(parents=True)
    (d / "evi-server.exe").write_bytes(b"MZ fake evi-server.exe for the sidecar-update e2e\n")
    (d / "evi-server").write_bytes(b"#!/bin/sh\n# fake evi-server for the e2e\nexit 0\n")
    # Compressible, so the archive really goes through the deflate path.
    lines = (b"eVi sidecar fixture line %03d\n" % i for i in range(64))
    (d / "_internal" / "base_library.txt").write_bytes(b"".join(lines))
    (d / "_internal" / "lib" / "data.bin").write_bytes(bytes(range(256)))
    return d


def write_hostile(path: pathlib.Path) -> None:
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("../escape.txt", b"must not land outside the staging dir\n")
        z.writestr("evi-server/../../escape2.txt", b"must not land outside the staging dir\n")
        z.writestr("evi-server/ok.txt", b"inside\n")
        exe = zipfile.ZipInfo("evi-server/evi-server", date_time=(2026, 1, 1, 0, 0, 0))
        exe.create_system = 3  # unix
        exe.external_attr = 0o100755 << 16
        exe.compress_type = zipfile.ZIP_DEFLATED
        z.writestr(exe, b"#!/bin/sh\nexit 0\n")


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        onedir = build_onedir(pathlib.Path(tmp))
        subprocess.run(
            [
                sys.executable,
                str(REPO / "scripts" / "zip-sidecar.py"),
                "--dir", str(onedir),
                "--platform", "windows-x86_64",
                "--out-dir", str(HERE),
            ],
            check=True,
        )
    write_hostile(HERE / "hostile.zip")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
