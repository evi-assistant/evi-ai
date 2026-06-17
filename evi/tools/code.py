"""Code execution tool — runs Python in a subprocess with a timeout.

Not a sandbox. Acceptable for personal-assistant use on a trusted machine.
Future: containerize via Docker if exposing the assistant beyond localhost.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from evi import workdir
from evi.tools.base import tool


_TIMEOUT_SECONDS = 10
_MAX_OUTPUT = 16 * 1024


def _python_exe() -> str | None:
    """A real Python interpreter to run snippets with.

    In the frozen desktop sidecar ``sys.executable`` is ``evi-server.exe`` (a
    PyInstaller binary, NOT a Python interpreter) — running a script through it
    fails with ``evi-server: error: unrecognized arguments``. So when frozen we
    look for a real interpreter on PATH; otherwise ``sys.executable`` is correct
    (pip/dev installs)."""
    if getattr(sys, "frozen", False):
        for name in ("python3", "python", "py"):
            found = shutil.which(name)
            if found:
                return found
        return None
    return sys.executable


@tool(
    description=(
        "Execute a Python 3 snippet in a fresh subprocess (10s timeout). "
        "Returns combined stdout+stderr. Use for arithmetic, data processing, "
        "and quick scripts. The snippet runs in a temp working directory. "
        "NOT for GUI apps or long-running/blocking programs (they hit the "
        "timeout) — for those, write the file with write_file and tell the user "
        "to run it, or launch it via the shell tool."
    ),
    category="code",
)
def run_python(code: str) -> str:
    exe = _python_exe()
    if exe is None:
        return (
            "ERROR: no Python interpreter found on PATH. run_python needs a real "
            "Python (the desktop app bundles its own non-interactive runtime, which "
            "can't execute scripts). Install Python 3, or run eVi from a Python "
            "environment."
        )
    with tempfile.TemporaryDirectory(prefix="evi-py-") as tmp:
        script = Path(tmp) / "snippet.py"
        script.write_text(code, encoding="utf-8")
        argv = [exe, str(script)]
        note = ""
        try:
            from evi.config import Config

            if Config.load().tools.sandbox:
                from evi import sandbox

                if sandbox.available():
                    argv = sandbox.wrap(argv, tmp, allow_network=False)
                else:
                    note = "(sandbox requested but no sandboxer on PATH — ran unsandboxed)\n"
        except Exception:  # noqa: BLE001
            pass
        try:
            res = subprocess.run(
                argv,
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
        return note + (out or f"(no output, exit={res.returncode})")


@tool(
    description=(
        "Outline a Python file's structure via its AST: functions (with args), "
        "classes (with methods), and imports — without reading the whole file. "
        "Use to navigate/understand code quickly."
    ),
    category="code",
)
def python_symbols(path: str) -> str:
    p = workdir.resolve(path)
    if not p.is_file():
        return f"ERROR: not a file: {p}"
    if p.suffix != ".py":
        return f"ERROR: not a Python file: {p}"
    from evi import pyanalyze

    try:
        info = pyanalyze.analyze_file(p)
    except SyntaxError as exc:
        return f"ERROR: syntax error in {p}: {exc}"
    except OSError as exc:
        return f"ERROR: cannot read {p}: {exc}"
    lines = [f"{p.name}: {len(info['functions'])} functions, "
             f"{len(info['classes'])} classes, {len(info['imports'])} imports"]
    for c in info["classes"]:
        lines.append(f"  class {c['name']}  (L{c['line']})")
        for m in c["methods"]:
            lines.append(f"    .{m}()")
    for f in info["functions"]:
        a = ", ".join(f["args"])
        kw = "async def" if f["async"] else "def"
        lines.append(f"  {kw} {f['name']}({a})  (L{f['line']})")
    if info["imports"]:
        lines.append("  imports: " + ", ".join(info["imports"][:40]))
    return "\n".join(lines)


@tool(
    description=(
        "Run the locally-installed linter for a file's language (ruff/eslint/"
        "go vet/clippy, by extension) and return its diagnostics. Use after "
        "editing a file to catch errors. No-op message when no linter is "
        "installed for that type."
    ),
    category="code",
)
def check_file(path: str) -> str:
    p = workdir.resolve(path)
    if not p.is_file():
        return f"ERROR: not a file: {p}"
    from evi import codeintel

    out = codeintel.diagnose(p)
    if len(out) > _MAX_OUTPUT:
        out = out[:_MAX_OUTPUT] + f"\n... [truncated, {len(out)} bytes total]"
    return out
