"""Best-effort OS sandboxing for code execution.

Wraps a subprocess argv so it runs with the filesystem read-only except a
writable work dir, and (by default) no network — used by the `run_python` tool
when `[tools] sandbox` is on. Sandboxers:

- **Linux**: ``bwrap`` (bubblewrap) — ``--ro-bind / /`` + writable workdir +
  ``--unshare-net``.
- **macOS**: ``sandbox-exec`` with an SBPL profile (deny file-write outside the
  workdir/tmp; deny network).
- **Windows / no sandboxer**: not available — `wrap()` returns the argv
  unchanged (caller runs unsandboxed; `available()` reports False so the UI /
  diagnostics can say so).

`wrap()`/`available()`/`status()` are pure enough to unit-test by mocking the
platform + PATH lookups; we don't execute a real sandbox in tests.
"""

from __future__ import annotations

import platform
import shutil
from pathlib import Path


def _system() -> str:
    return platform.system()


def available() -> bool:
    """True iff a sandboxer for this OS is on PATH."""
    s = _system()
    if s == "Linux":
        return shutil.which("bwrap") is not None
    if s == "Darwin":
        return shutil.which("sandbox-exec") is not None
    return False


def status() -> dict:
    s = _system()
    launcher = {"Linux": "bwrap", "Darwin": "sandbox-exec"}.get(s)
    return {
        "platform": s,
        "launcher": launcher or "",
        "available": available(),
    }


def _macos_profile(workdir: str, allow_network: bool) -> str:
    net = "" if allow_network else "(deny network*)\n"
    wd = str(workdir).replace('"', '\\"')
    return (
        "(version 1)\n"
        "(allow default)\n"
        f"{net}"
        "(deny file-write*)\n"
        '(allow file-write* (subpath "/tmp") (subpath "/private/tmp")'
        f' (subpath "{wd}"))\n'
    )


def wrap(argv: list[str], workdir: str | Path, allow_network: bool = False) -> list[str]:
    """Return `argv` wrapped to run sandboxed, or `argv` unchanged if no
    sandboxer is available on this OS."""
    wd = str(Path(workdir))
    s = _system()
    if s == "Linux" and shutil.which("bwrap"):
        cmd = [
            "bwrap",
            "--ro-bind", "/", "/",
            "--dev", "/dev",
            "--proc", "/proc",
            "--tmpfs", "/tmp",
            "--bind", wd, wd,
            "--chdir", wd,
        ]
        if not allow_network:
            cmd.append("--unshare-net")
        cmd.append("--")
        return cmd + list(argv)
    if s == "Darwin" and shutil.which("sandbox-exec"):
        return ["sandbox-exec", "-p", _macos_profile(wd, allow_network), *argv]
    return list(argv)
