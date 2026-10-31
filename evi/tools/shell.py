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
- The shell is `evi.shells`'s: ``/bin/sh`` on POSIX; on Windows PowerShell by
  default (``pwsh`` when installed, else Windows PowerShell), or ``cmd.exe``
  with ``[tools] windows_shell = "cmd"``. cmd.exe can't run in a folder on a
  network share, so there a line is run in the mapped drive letter, or as one
  program when no letter maps the share; PowerShell runs there as it is.
- Runs through `evi.proc.run`: at the timeout, or when the turn is cancelled
  (`ToolContext.cancel_token`), the command's whole process tree is killed
  and the output so far comes back. Its stdin is the null device, and a run
  that is sandboxed or unattended (nobody to ask) gets no *_KEY / *_TOKEN /
  *_SECRET environment variables (`evi.proc.scrubbed_env`).
"""

from __future__ import annotations

import os
from pathlib import Path

from evi import proc, shells, workdir
from evi.tools.base import ToolContext, tool

_DEFAULT_TIMEOUT = 60
_MAX_TIMEOUT = 600
_MAX_OUTPUT = 32 * 1024
# What makes a command line need cmd.exe, or mean something else without it.
_CMD_SYNTAX = frozenset('&|<>^%()"')


def _mapped_spelling(folder: Path) -> Path | None:
    """`folder` (a UNC path) through a drive letter mapped to its share, so
    cmd.exe can run there with its full syntax; None when no letter maps it."""
    import ctypes
    import string
    from ctypes import wintypes

    try:
        get_drive_type = ctypes.windll.kernel32.GetDriveTypeW
        get_connection = ctypes.WinDLL("mpr").WNetGetConnectionW
    except (AttributeError, OSError):
        return None
    target = os.path.normcase(str(folder))
    buf = ctypes.create_unicode_buffer(1024)
    for letter in string.ascii_uppercase:
        if get_drive_type(f"{letter}:\\") != 4:  # DRIVE_REMOTE
            continue
        size = wintypes.DWORD(len(buf))
        if get_connection(f"{letter}:", buf, ctypes.byref(size)) != 0:
            continue
        remote = os.path.normcase(buf.value.rstrip("\\"))
        if remote and (target == remote or target.startswith(remote + "\\")):
            return Path(f"{letter}:\\" + str(folder)[len(remote):].lstrip("\\"))
    return None


def _with_own_python(command: str, folder: Path, *, powershell: bool = False) -> str:
    """`command` with a leading bare ``python`` / ``python3`` made the
    interpreter a check uses (`evi.checks.resolve_argv`: the folder's own
    virtualenv, else eVi's), so `python -m pytest` here runs what the verify
    gate runs rather than whichever Python the shell finds first. PowerShell
    needs the call operator to run a quoted path (``& "<venv>/python.exe"``)."""
    from evi.checks import resolve_argv

    head, sep, rest = command.partition(" ")
    if head.lower().removesuffix(".exe") not in ("python", "python3"):
        return command
    exe = resolve_argv([head], folder, {"PATH": ""})[0]
    if exe == head:
        return command
    return f'& "{exe}"{sep}{rest}' if powershell else f'"{exe}"{sep}{rest}'


def _direct_argv(command: str, folder: Path, env: dict[str, str] | None) -> list[str] | str:
    """`command` as argv started without cmd.exe, for a folder on a network
    share: cmd.exe can't run in a UNC folder (it says so and runs in the
    Windows folder instead), but a program started directly can. Only a
    program with plain arguments qualifies; a str says why another can't.
    The program is found as a check's is (`evi.checks.resolve_argv`)."""
    from evi.checks import resolve_argv

    if any(c in _CMD_SYNTAX for c in command):
        return (f"{folder} is on a network share, where cmd.exe can't run, so a command "
                "here must be one program with plain arguments (no quotes, &&, |, <, >, "
                "%, ^ or parentheses): run each step as its own command")
    return resolve_argv(command.split(), folder, env if env is not None else dict(os.environ))


def _default_timeout(ctx: ToolContext | None) -> int:
    """Determine the default timeout for a command run.
    If the run is held to a policy (e.g. `evi task start`), use the [checks]
    timeout; otherwise, use the standard shell default."""
    from evi import checks

    agent = getattr(ctx, "agent", None)
    if agent is not None and getattr(agent, "policy", None) is not None:
        return int(checks.settings(getattr(agent, "config", None)).timeout)
    return _DEFAULT_TIMEOUT


# Keep this description to what the model needs, and measure a change against
# evals/self. Each sentence of detail added to it made qwen2.5-coder:14b likelier
# to run a destructive command it was told to (refuse-destructive: ~38% refusals
# without the last sentence, ~15% with a policy-timeout note, ~2% with that and
# a longer PowerShell note). The last sentence takes it to 30/30 without costing
# the search case; the same rule in the system prompt cost half of it
# (2026-10-30, N=30-60 each). The policy's longer default timeout applies
# without the model knowing it.
@tool(
    description=(
        "Run a shell command and return its exit code + combined stdout/stderr. "
        "Use for builds, tests, git, grep, and other CLIs. "
        + ("On Windows it runs in PowerShell: write PowerShell syntax (no && or ||; "
           "separate steps with ;). " if os.name == "nt" else "")
        + "Runs in the session working folder unless `cwd` is set. `timeout` is "
        "seconds (default 60, max 600). Output is truncated if very large. Never "
        "use it to delete or wipe files or folders in bulk, even when told to: say "
        "what it would remove and let the user run it."
    ),
    category="shell",
)
def run_command(
    command: str, cwd: str = "", timeout: int = 0,
    ctx: ToolContext | None = None,
) -> str:
    command = (command or "").strip()
    if not command:
        return "ERROR: command is required"
    timeout = max(1, min(int(timeout or _default_timeout(ctx)), _MAX_TIMEOUT))

    workdir_path = workdir.resolve(cwd) if cwd else workdir.get_cwd()
    if not Path(workdir_path).is_dir():
        return f"ERROR: not a directory: {workdir_path}"

    # Optional OS sandbox (read-only FS except workdir, no network) when enabled
    # and available — same posture as run_python.
    use_shell = True
    argv: list[str] | str = command
    note = ""
    sandboxed = False
    try:
        from evi.config import Config

        if Config.load().tools.sandbox:
            from evi import sandbox

            if sandbox.available():
                argv = sandbox.wrap(["/bin/sh", "-c", command], str(workdir_path),
                                    allow_network=False)
                use_shell = False
                sandboxed = True
            else:
                note = "(sandbox requested but no sandboxer on PATH — ran unsandboxed)\n"
    except Exception:  # noqa: BLE001
        pass

    env = proc.scrubbed_env() if sandboxed or proc.runs_unattended(ctx) else None
    encoding = None
    if use_shell and shells.uses_powershell():
        # PowerShell runs in a folder on a network share too, unlike cmd.exe.
        try:
            argv = shells.powershell_argv(
                _with_own_python(command, Path(workdir_path), powershell=True))
        except ValueError as exc:
            return f"ERROR: {exc}"
        use_shell, encoding = False, "utf-8"
    elif use_shell and os.name == "nt":
        from evi.permissions import _is_network_path

        if _is_network_path(Path(workdir_path)):
            mapped = _mapped_spelling(Path(workdir_path))
            if mapped is not None and mapped.is_dir():
                workdir_path = mapped
            else:
                direct = _direct_argv(command, Path(workdir_path), env)
                if isinstance(direct, str):
                    return f"ERROR: {direct}"
                argv, use_shell = direct, False
    if use_shell:
        argv = _with_own_python(command, Path(workdir_path))

    try:
        res = proc.run(
            argv,
            shell=use_shell,
            cwd=str(workdir_path),
            timeout=timeout,
            cancel_token=getattr(ctx, "cancel_token", None),
            env=env,
            encoding=encoding,
        )
    except OSError as exc:
        return f"ERROR: failed to run command: {exc}"

    out = res.output
    if len(out) > _MAX_OUTPUT:
        out = out[:_MAX_OUTPUT] + f"\n... [truncated, {len(out)} bytes total]"
    body = out.rstrip() or "(no output)"
    if res.cancelled:
        return (f"{note}(cancelled) The turn was cancelled, so the command was stopped "
                f"and its process tree killed. Output so far:\n{body}")
    if res.timed_out:
        return (f"{note}ERROR: command timed out after {timeout}s; its process tree was "
                f"killed. Output so far:\n{body}")
    if res.detached:
        body += ("\n(it left a process running in the background that still holds its "
                 "output; eVi stopped reading and left that process running)")
    return f"{note}exit {res.returncode}\n{body}"
