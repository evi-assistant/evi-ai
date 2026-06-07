#!/usr/bin/env python3
"""PyInstaller entry point for the desktop "sidecar" server.

When the Tauri desktop app is built as a *standalone* bundle (no Python on
the target machine), this script is frozen by PyInstaller into a single
`evi-server[.exe]` binary and shipped alongside the app. The Rust shell
spawns it instead of `python -m uvicorn …`.

We import the FastAPI `app` OBJECT (not the "evi.apps.web.server:app"
import string) so PyInstaller's static analysis bundles it directly and we
don't rely on a runtime import-string resolution inside the frozen exe.

Build it with `scripts/build-sidecar.{ps1,sh}` — see docs/desktop-bundling.md.
This file does nothing special when run under a normal Python; it's just a
thin uvicorn launcher, so it's safe to run directly for testing too:

    python scripts/sidecar_entry.py --host 127.0.0.1 --port 8000
"""

from __future__ import annotations

import argparse


def _selfcheck() -> int:
    """Import everything the practical-tier bundle is supposed to carry and
    report. Lets the FROZEN exe prove its bundled deps load without needing
    a chat flow / LLM — run `evi-server --check` right after building.

    Surfaces missing PyInstaller hidden-imports immediately: the heavy deps
    (`fitz`/PyMuPDF, `numpy`) are imported lazily inside tool functions, so
    the normal startup path wouldn't touch them.
    """
    import importlib

    # Core import graph (fastapi/starlette/pydantic/uvicorn + all evi tools
    # registered at server import time).
    from evi.apps.web.server import app  # noqa: F401

    # Lazy heavy deps the practical tier bundles.
    checks = {
        "fitz (pymupdf / read_pdf)": "fitz",
        "numpy (index / embeddings)": "numpy",
        "python_multipart (upload / transcribe forms)": "python_multipart",
        "uvicorn http protocol": "uvicorn.protocols.http.auto",
        "uvicorn websockets protocol": "uvicorn.protocols.websockets.auto",
        "uvicorn lifespan": "uvicorn.lifespan.on",
    }
    failed = []
    for label, mod in checks.items():
        try:
            importlib.import_module(mod)
            print(f"  ok   {label}")
        except Exception as exc:  # noqa: BLE001
            print(f"  FAIL {label}: {type(exc).__name__}: {exc}")
            failed.append(mod)
    if failed:
        print(f"selfcheck FAILED — missing: {', '.join(failed)}")
        return 1
    print("selfcheck OK")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="evi-server")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--check", action="store_true",
                    help="Import bundled deps, print a report, and exit.")
    args = ap.parse_args(argv)

    if args.check:
        return _selfcheck()

    import uvicorn

    # Import the app object so the frozen binary carries it explicitly.
    from evi.apps.web.server import app

    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
