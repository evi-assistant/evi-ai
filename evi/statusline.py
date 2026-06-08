"""Customizable REPL status line.

When `[statusline] enabled` is on, the chat REPL prints a dim status line above
each prompt. Two ways to customize:

- **format** — a template with `{tokens}`: {model} {used} {ceiling} {pct}
  {branch} {goal} {effort} {fast}. ({goal}/{fast} expand to " · goal: …" /
  " · fast" only when set, so the default reads cleanly.)
- **command** — a shell command (Claude-Code-style) that receives the state as
  JSON on stdin and whose stdout becomes the status line. Overrides `format`
  when set + it produces output.

Pure-ish: `render()` and `build_state()` are easy to unit-test; `status_line()`
ties it to an Agent + config.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

DEFAULT_FORMAT = "{model} · {pct}% ctx · {branch}{goal}{fast}"


def git_branch(cwd: str | Path | None = None) -> str:
    try:
        r = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True, text=True, timeout=2,
            cwd=str(cwd) if cwd else None,
        )
        return r.stdout.strip() if r.returncode == 0 else ""
    except Exception:  # noqa: BLE001
        return ""


def build_state(agent) -> dict:
    used, ceiling = agent.token_usage()
    pct = (used * 100) // ceiling if ceiling else 0
    return {
        "model": agent.config.llm.model,
        "used": used,
        "ceiling": ceiling,
        "pct": pct,
        "branch": git_branch() or "-",
        "goal": agent.goal or "",
        "effort": (agent.config.llm.reasoning_effort or "medium"),
        "fast": "fast" if agent.config.llm.fast_mode else "",
    }


def render(state: dict, fmt: str = DEFAULT_FORMAT) -> str:
    """Render the format template against `state`. Falls back to the default
    on a bad template so a typo can't break the REPL."""
    view = dict(state)
    view["goal"] = f" · goal: {state['goal']}" if state.get("goal") else ""
    view["fast"] = f" · {state['fast']}" if state.get("fast") else ""
    try:
        return fmt.format(**view)
    except (KeyError, IndexError, ValueError):
        return DEFAULT_FORMAT.format(**view)


def render_via_command(command: str, state: dict, timeout: float = 5.0) -> str | None:
    """Run a user command with the state JSON on stdin; return its stdout (one
    line) or None on failure/empty."""
    try:
        r = subprocess.run(
            command, shell=True, input=json.dumps(state),
            capture_output=True, text=True, timeout=timeout,
        )
    except Exception:  # noqa: BLE001
        return None
    out = (r.stdout or "").strip()
    return out or None


def status_line(agent, config) -> str | None:
    """The rendered status line, or None when disabled."""
    sl = getattr(config, "statusline", None)
    if sl is None or not getattr(sl, "enabled", False):
        return None
    state = build_state(agent)
    cmd = getattr(sl, "command", "") or ""
    if cmd:
        out = render_via_command(cmd, state)
        if out is not None:
            return out
    return render(state, getattr(sl, "format", DEFAULT_FORMAT) or DEFAULT_FORMAT)
