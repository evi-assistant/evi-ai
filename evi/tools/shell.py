"""Shell tool — run a shell command and capture its result.

The general-purpose counterpart to `run_python`: the agent can run an
arbitrary command (build, test, git, grep, a CLI) and get back the exit code
plus captured stdout/stderr. This is the `shell` tool category that eVi's
`code` mode already lists but previously had no tool for.

Safety posture (matches eVi's existing stance):
- The `[tools] shell` toggle is OFF by default — opt in explicitly.
- `category="shell"` is NOT in the default auto-approve set, so each call goes
  through the permission policy (mode + rules + hard_deny). A user can add a
  `hard_deny` rule like ``shell rm -rf*`` to block dangerous commands outright.
- A curated **destructive-command guard** (`evi/shell_guard.py`, on by default)
  is a second gate: commands like ``rm -rf ~``, ``git reset --hard``, disk
  formats, force-pushes, or secret-exfil can never run silently — they force a
  confirmation prompt (or are denied when there's no UI). Tune via `[auto]`
  `block_destructive` / `destructive_allow` / `destructive_disable_rules`.
- When `[tools] sandbox` is on AND a sandboxer (bwrap / sandbox-exec) is
  present, the command runs sandboxed (read-only FS except the workdir, no
  network), same as `run_python`. Falls back to unsandboxed with a note.
- Runs in the session working folder (evi.workdir) unless `cwd` is given.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from evi import workdir
from evi.tools.base import tool

_DEFAULT_TIMEOUT = 60
_MAX_TIMEOUT = 600
_MAX_OUTPUT = 32 * 1024


@tool(
    description=(
        "Run a shell command and return its exit code + combined stdout/stderr. "
        "Use for builds, tests, git, grep, and other CLIs. Runs in the session "
        "working folder unless `cwd` is set. `timeout` is seconds (default 60, "
        "max 600). Output is truncated if very large."
    ),
    category="shell",
)
def run_command(command: str, cwd: str = "", timeout: int = _DEFAULT_TIMEOUT) -> str:
    command = (command or "").strip()
    if not command:
        return "ERROR: command is required"
    timeout = max(1, min(int(timeout or _DEFAULT_TIMEOUT), _MAX_TIMEOUT))

    workdir_path = workdir.resolve(cwd) if cwd else workdir.get_cwd()
    if not Path(workdir_path).is_dir():
        return f"ERROR: not a directory: {workdir_path}"

    # Optional OS sandbox (read-only FS except workdir, no network) when enabled
    # and available — same posture as run_python.
    use_shell = True
    argv: list[str] | str = command
    note = ""
    try:
        from evi.config import Config

        if Config.load().tools.sandbox:
            from evi import sandbox

            if sandbox.available():
                argv = sandbox.wrap(["/bin/sh", "-c", command], str(workdir_path),
                                    allow_network=False)
                use_shell = False
            else:
                note = "(sandbox requested but no sandboxer on PATH — ran unsandboxed)\n"
    except Exception:  # noqa: BLE001
        pass

    try:
        res = subprocess.run(
            argv,
            shell=use_shell,
            cwd=str(workdir_path),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return f"ERROR: command timed out after {timeout}s"
    except OSError as exc:
        return f"ERROR: failed to run command: {exc}"

    out = (res.stdout or "") + (res.stderr or "")
    if len(out) > _MAX_OUTPUT:
        out = out[:_MAX_OUTPUT] + f"\n... [truncated, {len(out)} bytes total]"
    body = out.rstrip() or "(no output)"
    return f"{note}exit {res.returncode}\n{body}"
