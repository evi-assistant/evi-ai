"""The shell a command line runs in: run_command, monitor's command watch, and
the syntax the permission checks read it in.

POSIX: ``/bin/sh`` (``subprocess.run(shell=True)``). Windows: PowerShell by
default (``[tools] windows_shell = "powershell"``), as ``pwsh`` (PowerShell 7)
when it is on PATH, else Windows PowerShell 5.1; ``"cmd"`` keeps ``cmd.exe``.
A line goes to PowerShell with ``-Command`` behind a short prelude (UTF-8
output, no progress records) and an epilogue that makes the process exit
with the last native command's exit code, or 1 when the last statement
failed, so an exit code means what it does in other shells.
"""

from __future__ import annotations

import os
import shutil
from functools import lru_cache
from pathlib import Path

CHOICES = ("powershell", "cmd")
DEFAULT = "powershell"
# CreateProcess takes at most 32 767 characters; leave room for the program
# path, the flags and the quoting.
MAX_POWERSHELL_LINE = 30_000

_PRELUDE = (
    "$ProgressPreference = 'SilentlyContinue'; "
    "try { [Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false) } catch {}; "
    "$OutputEncoding = [System.Text.UTF8Encoding]::new($false); "
    "$global:LASTEXITCODE = 0\n"
)
_EPILOGUE = (
    "\n$__evi_ok = $?\n"
    "if ($LASTEXITCODE) { exit $LASTEXITCODE }\n"
    "if (-not $__evi_ok) { exit 1 }\n"
)


def windows_shell() -> str:
    """``[tools] windows_shell``: "powershell" (the default) or "cmd"."""
    try:
        from evi.config import Config

        value = str(getattr(Config.load().tools, "windows_shell", DEFAULT) or DEFAULT)
    except Exception:  # noqa: BLE001 — an unreadable config keeps the default
        return DEFAULT
    value = value.strip().lower()
    return value if value in CHOICES else DEFAULT


def uses_powershell() -> bool:
    """Whether a command line runs in PowerShell here (Windows, not opted out)."""
    return os.name == "nt" and windows_shell() == "powershell"


def dialect() -> str:
    """The `evi.shellsplit` dialect of the shell a command line runs in."""
    if os.name != "nt":
        return "bash"
    return "pwsh" if windows_shell() == "powershell" else "cmd"


@lru_cache(maxsize=1)
def powershell_exe() -> str:
    """``pwsh`` when it is on PATH, else Windows PowerShell (on PATH, or in
    its fixed place under the Windows folder)."""
    found = shutil.which("pwsh") or shutil.which("powershell")
    if found:
        return found
    root = os.environ.get("SystemRoot", r"C:\Windows")
    return str(Path(root) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe")


def as_powershell(line: str) -> str:
    """`line` as PowerShell runs it: one that starts with a quoted program
    path (``"C:/tools/x.exe" --flag``, as cmd.exe takes it) gets the call
    operator, without which PowerShell reads the quoted path as a string."""
    stripped = line.lstrip()
    return f"& {stripped}" if stripped[:1] in ('"', "'") else line


def powershell_argv(line: str) -> list[str]:
    """argv that runs `line` (through `as_powershell`) in PowerShell, exit
    code as described above. ValueError for a line too long for a Windows
    command line."""
    script = _PRELUDE + as_powershell(line) + _EPILOGUE
    if len(script) > MAX_POWERSHELL_LINE:
        raise ValueError(
            f"the command is {len(line):,} characters, more than PowerShell's command line "
            f"takes (about {MAX_POWERSHELL_LINE:,}): put it in a script file and run that"
        )
    return [powershell_exe(), "-NoLogo", "-NoProfile", "-NonInteractive",
            "-ExecutionPolicy", "Bypass", "-Command", script]
