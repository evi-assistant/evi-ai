#!/usr/bin/env python3
"""Write the evi-run action's report outputs from `evi run`'s captured stdout.

    printf '%s\\n' "$out" | python evi_run_outputs.py <format> <exit-code>

Appends `exit_code`, `stop_reason` and `session_id` to $GITHUB_OUTPUT.
`stop_reason` and `session_id` come from eVi's run report: the JSON object
`--format json` prints, or the last line of `--format stream-json` (its
RunResult event). `--format text` prints no report, so they are empty there
(the exit code still says why the run stopped: 0 done, 1 error, 2 usage,
3 max_turns, 4 permission_denied or guardrail, 6 stop_hook_exhausted,
130 interrupted).

Only values eVi itself writes pass: a stop_reason from its fixed list
(STOP_REASONS, the same as evi.events.STOP_REASONS) and a session id of
plain id characters. Anything else is left empty, so text a model put in
the output can neither forge an output nor add a line to $GITHUB_OUTPUT.
"""

from __future__ import annotations

import json
import os
import re
import sys

STOP_REASONS = (
    "done", "max_turns", "loop_detected", "step_budget", "interrupted", "error",
    "permission_denied", "guardrail", "stop_hook_exhausted",
)
_SESSION_ID = re.compile(r"[A-Za-z0-9._-]{1,128}")


def report_of(fmt: str, stdout: str) -> dict | None:
    """The run report in `stdout`, or None (text format, or not a report)."""
    text = stdout.strip()
    if fmt == "stream-json":
        lines = text.splitlines()
        text = lines[-1] if lines else ""
    elif fmt != "json":
        return None
    try:
        report = json.loads(text)
    except ValueError:
        return None
    if not isinstance(report, dict):
        return None
    if fmt == "stream-json" and report.get("kind") != "RunResult":
        return None
    return report


def outputs(fmt: str, code: str, stdout: str) -> dict[str, str]:
    report = report_of(fmt, stdout) or {}
    stop = report.get("stop_reason")
    session = report.get("session_id")
    return {
        "exit_code": code if code.isdigit() else "",
        "stop_reason": stop if stop in STOP_REASONS else "",
        "session_id": session if isinstance(session, str) and _SESSION_ID.fullmatch(session)
        else "",
    }


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 2:
        print("usage: evi_run_outputs.py <format> <exit-code> < stdout", file=sys.stderr)
        return 2
    values = outputs(args[0], args[1], sys.stdin.buffer.read().decode("utf-8", "replace"))
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.writelines(f"{key}={value}\n" for key, value in values.items())
    return 0


if __name__ == "__main__":
    sys.exit(main())
