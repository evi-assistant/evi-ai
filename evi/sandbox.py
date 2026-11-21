"""Best-effort OS sandboxing for code execution.

Wraps a subprocess argv so it runs with the filesystem read-only except a
writable work dir, and (by default) no network — used by the `run_python`,
`run_command` and `monitor` (kind=command) tools when `[tools] sandbox` is
on. Sandboxers:

- **Linux**: ``bwrap`` (bubblewrap) — ``--ro-bind / /`` + writable workdir +
  ``--unshare-net``.
- **macOS**: ``sandbox-exec`` with an SBPL profile (deny file-write outside the
  workdir/tmp; deny network).
- **Windows / no sandboxer**: not available — `wrap()` returns the argv
  unchanged (caller runs unsandboxed; `available()` reports False so the UI /
  diagnostics can say so), unless `[tools] sandbox_required` is on: then
  `refusal()` returns the message the tool answers with instead of running.

`wrap()`/`available()`/`status()` are pure enough to unit-test by mocking the
platform + PATH lookups; we don't execute a real sandbox in tests.
"""

from __future__ import annotations

import platform
import shutil
from pathlib import Path
from typing import Any


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


def _tools(config: Any) -> Any:
    """The `[tools]` settings of `config`: a Config, its ToolSettings itself,
    or None for `Config.load()`."""
    if config is None:
        from evi.config import Config

        config = Config.load()
    if hasattr(config, "sandbox"):
        return config
    return getattr(config, "tools", None)


def required(config: Any = None) -> bool:
    """True iff `[tools] sandbox_required` is on in `config` (default:
    `Config.load()`): code execution must run sandboxed or not at all.
    An unreadable config requires it, so code execution refuses rather than
    running unsandboxed when eVi cannot tell what the user asked for."""
    try:
        return bool(getattr(_tools(config), "sandbox_required", False))
    except Exception:  # noqa: BLE001 — fail closed
        return True


def requested(config: Any = None) -> bool:
    """True iff `[tools] sandbox` (or `sandbox_required`, which implies it) is
    on in `config` (default: `Config.load()`). False when the config can't be
    read: `required` is what fails closed."""
    try:
        tools = _tools(config)
        return bool(getattr(tools, "sandbox", False)
                    or getattr(tools, "sandbox_required", False))
    except Exception:  # noqa: BLE001 — an unreadable config requests nothing
        return False


def refusal(config: Any = None, tool: str = "this tool") -> str | None:
    """The error `tool` answers with instead of running, or None to run.
    Not None only when the sandbox is required and no sandboxer is on PATH:
    `[tools] sandbox_required` turns the "ran unsandboxed" fallback into a
    refusal."""
    if not required(config) or available():
        return None
    s = _system()
    how = {
        "Linux": "install bubblewrap so `bwrap` is on PATH",
        "Darwin": "`sandbox-exec` should ship with macOS; check PATH",
    }.get(s, f"no sandboxer exists for {s or 'this OS'}")
    return (
        f"ERROR: {tool} did not run: [tools] sandbox_required is on and no OS "
        f"sandboxer is available here ({how}). Nothing was executed. To run "
        "code unsandboxed, the user must set sandbox_required = false in "
        "~/.evi/config.toml."
    )


def active(config: Any = None) -> bool:
    """True iff code execution runs sandboxed now: `[tools] sandbox` is on
    (`requested`) and a sandboxer is on PATH (`available`). The permission
    policy checks a sandboxed run_python as reading code, not running it."""
    return requested(config) and available()


def status(config: Any = None) -> dict:
    s = _system()
    launcher = {"Linux": "bwrap", "Darwin": "sandbox-exec"}.get(s)
    return {
        "platform": s,
        "launcher": launcher or "",
        "available": available(),
        "required": required(config),
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
