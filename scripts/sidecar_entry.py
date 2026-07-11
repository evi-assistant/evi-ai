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


def _claude_check() -> int:
    """Diagnostic: import the FULL server app (like the sidecar does), then run one
    real claude_agent turn on a worker thread — the exact shape the web server uses.
    Prints the active event-loop policy so we can see if a Selector loop (no Windows
    subprocess support) is what breaks the SDK's control handshake."""
    import asyncio
    import queue
    import threading

    from evi.apps.web.server import app  # noqa: F401 — force server-time imports
    print("event_loop_policy:", type(asyncio.get_event_loop_policy()).__name__)

    from evi.backends.claude_agent import ClaudeAgentBackend
    client = ClaudeAgentBackend().make_client()
    q: queue.Queue = queue.Queue()

    def w() -> None:
        try:
            parts = []
            for ch in client.chat.completions.create(
                model="sonnet",
                messages=[{"role": "user", "content": "say hi in 3 words"}],
                stream=True,
            ):
                if ch.choices and ch.choices[0].delta and ch.choices[0].delta.content:
                    parts.append(ch.choices[0].delta.content)
            q.put("OK: " + ("".join(parts)[:120] or "(empty)"))
        except Exception as exc:  # noqa: BLE001
            q.put(f"ERR: {type(exc).__name__}: {exc}")

    threading.Thread(target=w, daemon=True).start()
    print("claude_agent turn ->", q.get())
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="evi-server")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--check", action="store_true",
                    help="Import bundled deps, print a report, and exit.")
    ap.add_argument("--claude-check", action="store_true",
                    help="Diagnostic: run one claude_agent turn and exit.")
    args = ap.parse_args(argv)

    if args.check:
        return _selfcheck()
    if args.claude_check:
        return _claude_check()

    import uvicorn

    # Import the app object so the frozen binary carries it explicitly.
    from evi.apps.web.server import app

    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
