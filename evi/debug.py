"""Debug logging — one place to flip on verbose internals.

Set `EVI_DEBUG=1` (or use the CLI's `--debug`/`-d` flag) to print every
LLM request payload, tool call argument, and tool output to stderr. The
chat output on stdout stays clean either way.

This is a poor man's structured-tracing system: dropping a `dlog(...)`
beats wiring up OpenTelemetry for a personal assistant.
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any


_ENABLED: bool | None = None


def is_enabled() -> bool:
    """Cached check of the EVI_DEBUG env var. Re-evaluates if explicitly reset."""
    global _ENABLED
    if _ENABLED is None:
        _ENABLED = os.environ.get("EVI_DEBUG", "").strip() not in ("", "0", "false", "no")
    return _ENABLED


def set_enabled(value: bool) -> None:
    """Force the debug flag from code (used by the CLI flag callback)."""
    global _ENABLED
    _ENABLED = bool(value)
    os.environ["EVI_DEBUG"] = "1" if value else "0"


def dlog(tag: str, payload: Any = None, *, max_len: int = 4096) -> None:
    """Emit one tagged line to stderr when debug mode is on.

    `payload` is JSON-encoded if not already a string, then truncated.
    Falls back to repr() for unserialisable objects.
    """
    if not is_enabled():
        return
    if payload is None:
        sys.stderr.write(f"[evi-debug] {tag}\n")
        return
    if isinstance(payload, (dict, list)):
        try:
            body = json.dumps(payload, default=str)
        except (TypeError, ValueError):
            body = repr(payload)
    else:
        body = str(payload)
    if len(body) > max_len:
        body = body[:max_len] + f" …(+{len(body) - max_len} chars)"
    sys.stderr.write(f"[evi-debug] {tag}: {body}\n")
