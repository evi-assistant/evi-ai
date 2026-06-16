"""Local code intelligence — formatters + linters by file extension.

A dependency-light alternative to a full LSP integration: pick a
LOCALLY-INSTALLED formatter or linter for a file's language and shell out to
it. Everything is optional and degrades gracefully (missing tool → skipped),
mirroring eVi's tesseract/ffmpeg optional-tool pattern. No hosted service.

Used by `[tools] format_on_edit` (auto-format after a write) and the
`check_file` tool (on-demand diagnostics) — eVi's take on opencode's
formatter + LSP-diagnostics feedback.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

# ext -> candidate formatter argv (file path appended); first installed wins.
_FORMATTERS: dict[str, list[list[str]]] = {
    ".py": [["ruff", "format"], ["black", "-q"]],
    ".pyi": [["ruff", "format"], ["black", "-q"]],
    ".js": [["prettier", "--write"]],
    ".jsx": [["prettier", "--write"]],
    ".ts": [["prettier", "--write"]],
    ".tsx": [["prettier", "--write"]],
    ".json": [["prettier", "--write"]],
    ".css": [["prettier", "--write"]],
    ".html": [["prettier", "--write"]],
    ".md": [["prettier", "--write"]],
    ".go": [["gofmt", "-w"]],
    ".rs": [["rustfmt"]],
}

# ext -> candidate linter argv (file path appended); diagnostics on stdout/stderr.
_LINTERS: dict[str, list[list[str]]] = {
    ".py": [["ruff", "check"], ["pyflakes"]],
    ".js": [["eslint"]],
    ".jsx": [["eslint"]],
    ".ts": [["eslint"]],
    ".tsx": [["eslint"]],
    ".go": [["go", "vet"]],
    ".rs": [["cargo", "clippy", "-q"]],
}

_FORMAT_TIMEOUT = 30
_LINT_TIMEOUT = 60


def _first_available(cmds: list[list[str]]) -> list[str] | None:
    for c in cmds:
        if shutil.which(c[0]):
            return c
    return None


def format_file(path: str | Path) -> tuple[bool, str]:
    """Format `path` in place with the first available formatter for its type.
    Returns (ran, tool_name). (False, "") when no formatter is configured or
    installed, or on a formatter error (never raises)."""
    p = Path(path)
    cmds = _FORMATTERS.get(p.suffix.lower())
    if not cmds:
        return (False, "")
    cmd = _first_available(cmds)
    if cmd is None:
        return (False, "")
    try:
        subprocess.run([*cmd, str(p)], capture_output=True, text=True,
                       timeout=_FORMAT_TIMEOUT)
    except (OSError, subprocess.SubprocessError):
        return (False, cmd[0])
    return (True, cmd[0])


def diagnose(path: str | Path) -> str:
    """Run the first available linter for `path` and return its diagnostics
    (or a clear note when none is configured/installed). Never raises."""
    p = Path(path)
    cmds = _LINTERS.get(p.suffix.lower())
    if not cmds:
        return f"(no linter configured for {p.suffix or 'this file type'})"
    cmd = _first_available(cmds)
    if cmd is None:
        tried = ", ".join(c[0] for c in cmds)
        return f"(no linter installed for {p.suffix} — tried: {tried})"
    try:
        res = subprocess.run([*cmd, str(p)], capture_output=True, text=True,
                             timeout=_LINT_TIMEOUT)
    except (OSError, subprocess.SubprocessError) as exc:
        return f"{cmd[0]} failed: {exc}"
    out = ((res.stdout or "") + (res.stderr or "")).strip()
    return out or f"{cmd[0]}: no issues found"
