#!/usr/bin/env python3
"""Zip the frozen `evi-server` onedir for the sidecar update channel and print
its sha256. The archive contains the folder at its root (`evi-server/...`), so
the Rust client extracts it into `<home>/sidecar/<ver>/` and finds the binary at
`<home>/sidecar/<ver>/evi-server/evi-server[.exe]` (see sidecar_update.rs).

Usage:
    zip-sidecar.py --dir desktop/src-tauri/binaries/evi-server \
        --platform linux-x86_64 --out-dir sidecar-dist

Writes `<out-dir>/sidecar-<platform>.zip` and `<out-dir>/sidecar-<platform>.sha256`.
"""

from __future__ import annotations

import argparse
import hashlib
import pathlib
import sys
import zipfile

_PLATFORMS = {"windows-x86_64", "darwin-aarch64", "darwin-x86_64", "linux-x86_64"}


def sha256_file(path: pathlib.Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="zip-sidecar")
    ap.add_argument("--dir", required=True, help="the evi-server onedir folder")
    ap.add_argument("--platform", required=True, choices=sorted(_PLATFORMS))
    ap.add_argument("--out-dir", default="sidecar-dist")
    args = ap.parse_args(argv)

    src = pathlib.Path(args.dir)
    if not src.is_dir():
        print(f"error: {src} is not a directory", file=sys.stderr)
        return 1
    out_dir = pathlib.Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    zip_path = out_dir / f"sidecar-{args.platform}.zip"

    # Deflate so the Rust `zip` crate (default-features=false, ["deflate"]) reads it.
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
        for f in sorted(src.rglob("*")):
            if f.is_file():
                arc = pathlib.Path("evi-server") / f.relative_to(src)
                z.write(f, arcname=str(arc).replace("\\", "/"))

    digest = sha256_file(zip_path)
    (out_dir / f"sidecar-{args.platform}.sha256").write_text(digest, encoding="utf-8")
    size_mb = zip_path.stat().st_size / 1e6
    print(f"{args.platform}: {zip_path.name} ({size_mb:.1f} MB) sha256={digest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
