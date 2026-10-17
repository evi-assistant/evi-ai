"""monitor tool — watch a file or a command for a bounded window.

eVi's analogue of Claude Code's Monitor: tail a log file's new lines, or run a
command and collect its output, for up to `timeout` seconds — returning early
when an optional `until` regex matches. Because eVi tools are synchronous
(called between model turns), this is a *bounded* watch (poll-until-condition-
or-timeout) rather than an open-ended background stream: the model calls it,
waits for the window, and gets back what happened. Good for "babysit this build
/ CI run and tell me when it finishes" and "tail the server log for an error".

One tool, two kinds of call. Tailing a file only reads, and the permission
policy checks it as "code_read" (auto-approved by default), with the file as
its path argument, so tailing a protected secret (``.env``, a key) asks. A
command runs a shell line, so it is held to everything `run_command` is: the
call is checked as "shell" (`Tool.category_for`: the shell rules, hard_deny
and the destructive guard apply, and it is never auto-approved unless "shell"
is), it is refused outright in a session that doesn't offer the shell tools
(`[tools] shell = false`, outside a mode that turns them on), and it runs
under the OS sandbox when `[tools] sandbox` is on. It runs through
`evi.proc.run` like run_command: at the timeout, on an `until` match or when
the turn is cancelled (`ToolContext.cancel_token`), the command's whole
process tree is killed. A file tail also stops when the turn is cancelled.
"""

from __future__ import annotations

import os
import re
import threading
import time
from pathlib import Path
from typing import Any

from evi import proc
from evi.tools.base import ToolContext, tool

_MAX_TIMEOUT = 300.0
_MAX_LINES = 400
_MAX_OUTPUT = 16 * 1024


def _clip(lines: list[str]) -> str:
    out = "".join(lines)
    if len(lines) > _MAX_LINES:
        out = "".join(lines[-_MAX_LINES:])
        out = f"… [showing last {_MAX_LINES} lines]\n" + out
    return out[-_MAX_OUTPUT:]


def _tail_file(
    path: Path, until_rx, deadline: float, poll: float,
    cancel_token: threading.Event | None = None,
) -> str:
    if not path.is_file():
        return f"ERROR: not a file: {path}"
    collected: list[str] = []
    with path.open("r", encoding="utf-8", errors="replace") as f:
        f.seek(0, 2)  # start at EOF — we only want NEW lines
        while time.monotonic() < deadline:
            if cancel_token is not None and cancel_token.is_set():
                return (_clip(collected) if collected else "(no new lines)") + "\n[cancelled]"
            line = f.readline()
            if not line:
                time.sleep(poll)
                continue
            collected.append(line)
            if until_rx is not None and until_rx.search(line):
                return _clip(collected) + f"\n[matched /{until_rx.pattern}/]"
    if not collected:
        return f"(no new lines in {path.name} within the window)"
    return _clip(collected) + "\n[timed out]"


def _tool_settings(ctx: ToolContext | None) -> Any:
    """The `[tools]` settings of the session making the call (its agent's
    config), else `Config.load()`'s. None when they can't be read."""
    agent = getattr(ctx, "agent", None) if ctx is not None else None
    config = getattr(agent, "config", None)
    try:
        if config is None:
            from evi.config import Config

            config = Config.load()
        return config.tools
    except Exception:  # noqa: BLE001 — unreadable settings enable nothing
        return None


def _shell_offered(ctx: ToolContext | None, tools: Any) -> bool:
    """Whether the session making the call offers the shell tools: its
    agent's tool set holds one in the "shell" category (run_command), which
    `[tools] shell` or a mode that turns them on (code mode) put there, so a
    command runs here exactly where run_command could. Without a session's
    tool set (a direct call, MCP publish), `tools` (`[tools]`) says."""
    agent = getattr(ctx, "agent", None) if ctx is not None else None
    offered = getattr(agent, "tools", None)
    if isinstance(offered, dict):
        offered = list(offered.values())
    if isinstance(offered, (list, tuple)):
        return any(getattr(t, "category", None) == "shell" for t in offered)
    return bool(getattr(tools, "shell", False))


def _file_target(args: dict) -> list[str]:
    """The call's path arguments (`Tool.path_args`): the file it tails
    (kind='file', the default), so the permission policy's secret-read check
    and trusted dirs see it; a command line is none."""
    target = args.get("target")
    if args.get("kind", "file") == "file" and isinstance(target, str):
        return [target]
    return []


def _watch_command(
    command: str, until_rx, deadline: float, *, sandboxed: bool = False,
    cancel_token: threading.Event | None = None, scrub: bool = False,
) -> str:
    # Like run_command: under the OS sandbox when [tools] sandbox is on and a
    # sandboxer is present. It runs in the eVi process's cwd, as it always has
    # (evi.permissions.exec_cwd_of judges its redirects there).
    argv: str | list[str] = command
    use_shell = True
    prefix = ""
    if sandboxed:
        from evi import sandbox

        if sandbox.available():
            argv = sandbox.wrap(["/bin/sh", "-c", command], os.getcwd(), allow_network=False)
            use_shell = False
            scrub = True
        else:
            prefix = "(sandbox requested but no sandboxer on PATH — ran unsandboxed)\n"
    try:
        res = proc.run(
            argv, shell=use_shell, merge_stderr=True,
            timeout=max(0.0, deadline - time.monotonic()),
            cancel_token=cancel_token,
            until=until_rx.search if until_rx is not None else None,
            env=proc.scrubbed_env() if scrub else None,
        )
    except OSError as exc:
        return f"ERROR: could not start command: {exc}"

    if res.matched:
        note = f"[matched /{until_rx.pattern}/]"
    elif res.cancelled:
        note = "[cancelled: the turn was cancelled, so the command was stopped]"
    elif res.timed_out:
        note = "[timed out]"
    else:
        note = f"[command exited {res.returncode}]"
    lines = res.stdout.splitlines(keepends=True)
    body = _clip(lines) if lines else "(no output)"
    return prefix + body + "\n" + note


@tool(
    description=(
        "Watch something for up to `timeout` seconds, returning early if the "
        "`until` regex matches. kind='file' tails NEW lines appended to a log "
        "file; kind='command' runs a shell command and collects its output "
        "(good for babysitting a build/CI run). kind='command' needs the shell "
        "tools enabled ([tools] shell) and is approved like run_command. "
        "Returns what was observed."
    ),
    category="code",
    long=True,
    path_args_for=_file_target,
)
def monitor(
    target: str,
    kind: str = "file",
    until: str = "",
    timeout: float = 30.0,
    ctx: ToolContext | None = None,
) -> str:
    # The permission policy reads `kind` the same way (evi.permissions
    # .classify_call): exactly "command" runs a shell line, so reject anything
    # that isn't one of the two kinds rather than guess.
    if kind not in ("file", "command"):
        return f"ERROR: kind must be 'file' or 'command', not {kind!r}"
    if not isinstance(target, str) or not target.strip():
        return "ERROR: target must be a non-empty string (a file path or a command line)"
    tools = None
    if kind == "command":
        tools = _tool_settings(ctx)
        if not _shell_offered(ctx, tools):
            return (
                "ERROR: kind='command' runs a shell command, and this session has no "
                "shell tools (off: [tools] shell = false, or not enabled for it), so "
                "nothing was run. Tail a log file with kind='file', or ask the user "
                "to enable the shell tools."
            )
    timeout = min(max(float(timeout), 1.0), _MAX_TIMEOUT)
    try:
        until_rx = re.compile(until) if until else None
    except re.error as exc:
        return f"ERROR: bad until regex {until!r}: {exc}"
    deadline = time.monotonic() + timeout
    poll = 0.2
    cancel_token = getattr(ctx, "cancel_token", None)
    if kind == "command":
        return _watch_command(
            target, until_rx, deadline, sandboxed=bool(getattr(tools, "sandbox", False)),
            cancel_token=cancel_token, scrub=proc.runs_unattended(ctx),
        )
    return _tail_file(Path(target).expanduser(), until_rx, deadline, poll, cancel_token)
