"""Code execution tool — runs Python in a subprocess with a timeout.

Not a sandbox. Acceptable for personal-assistant use on a trusted machine.
Future: containerize via Docker if exposing the assistant beyond localhost.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

from evi.tools.base import tool


_TIMEOUT_SECONDS = 10
_MAX_OUTPUT = 16 * 1024


@tool(
    description=(
        "Execute a Python 3 snippet in a fresh subprocess (10s timeout). "
        "Returns combined stdout+stderr. Use for arithmetic, data processing, "
        "and quick scripts. The snippet runs in a temp working directory."
    ),
    category="code",
)
def run_python(code: str) -> str:
    with tempfile.TemporaryDirectory(prefix="evi-py-") as tmp:
        script = Path(tmp) / "snippet.py"
        script.write_text(code, encoding="utf-8")
        try:
            res = subprocess.run(
                [sys.executable, str(script)],
                capture_output=True,
                text=True,
                timeout=_TIMEOUT_SECONDS,
                cwd=tmp,
            )
        except subprocess.TimeoutExpired:
            return f"ERROR: timeout after {_TIMEOUT_SECONDS}s"
        out = (res.stdout or "") + (res.stderr or "")
        if len(out) > _MAX_OUTPUT:
            out = out[:_MAX_OUTPUT] + f"\n... [truncated, {len(out)} bytes total]"
        return out or f"(no output, exit={res.returncode})"
