"""monitor tool — watch a file or a command for a bounded window.

eVi's analogue of Claude Code's Monitor: tail a log file's new lines, or run a
command and collect its output, for up to `timeout` seconds — returning early
when an optional `until` regex matches. Because eVi tools are synchronous
(called between model turns), this is a *bounded* watch (poll-until-condition-
or-timeout) rather than an open-ended background stream: the model calls it,
waits for the window, and gets back what happened. Good for "babysit this build
/ CI run and tell me when it finishes" and "tail the server log for an error".
"""

from __future__ import annotations

import queue
import re
import subprocess
import threading
import time
from pathlib import Path

from evi.tools.base import tool

_MAX_TIMEOUT = 300.0
_MAX_LINES = 400
_MAX_OUTPUT = 16 * 1024


def _clip(lines: list[str]) -> str:
    out = "".join(lines)
    if len(lines) > _MAX_LINES:
        out = "".join(lines[-_MAX_LINES:])
        out = f"… [showing last {_MAX_LINES} lines]\n" + out
    return out[-_MAX_OUTPUT:]


def _tail_file(path: Path, until_rx, deadline: float, poll: float) -> str:
    if not path.is_file():
        return f"ERROR: not a file: {path}"
    collected: list[str] = []
    with path.open("r", encoding="utf-8", errors="replace") as f:
        f.seek(0, 2)  # start at EOF — we only want NEW lines
        while time.monotonic() < deadline:
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


def _watch_command(command: str, until_rx, deadline: float, poll: float) -> str:
    try:
        proc = subprocess.Popen(
            command, shell=True, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, bufsize=1,
        )
    except OSError as exc:
        return f"ERROR: could not start command: {exc}"

    q: queue.Queue = queue.Queue()

    def _reader() -> None:
        try:
            assert proc.stdout is not None
            for line in proc.stdout:
                q.put(line)
        finally:
            q.put(None)  # sentinel

    threading.Thread(target=_reader, daemon=True).start()

    collected: list[str] = []
    note = "[timed out]"
    while time.monotonic() < deadline:
        try:
            line = q.get(timeout=poll)
        except queue.Empty:
            if proc.poll() is not None:  # process ended, drained
                note = f"[command exited {proc.returncode}]"
                break
            continue
        if line is None:  # stdout closed — wait so returncode is populated
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                pass
            note = f"[command exited {proc.returncode}]"
            break
        collected.append(line)
        if until_rx is not None and until_rx.search(line):
            note = f"[matched /{until_rx.pattern}/]"
            break

    if proc.poll() is None:  # still running at timeout/match — stop it
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
    body = _clip(collected) if collected else "(no output)"
    return body + "\n" + note


@tool(
    description=(
        "Watch something for up to `timeout` seconds, returning early if the "
        "`until` regex matches. kind='file' tails NEW lines appended to a log "
        "file; kind='command' runs a shell command and collects its output "
        "(good for babysitting a build/CI run). Returns what was observed."
    ),
    category="code",
    long=True,
)
def monitor(target: str, kind: str = "file", until: str = "", timeout: float = 30.0) -> str:
    timeout = min(max(float(timeout), 1.0), _MAX_TIMEOUT)
    try:
        until_rx = re.compile(until) if until else None
    except re.error as exc:
        return f"ERROR: bad until regex {until!r}: {exc}"
    deadline = time.monotonic() + timeout
    poll = 0.2
    if kind == "command":
        return _watch_command(target, until_rx, deadline, poll)
    return _tail_file(Path(target).expanduser(), until_rx, deadline, poll)
