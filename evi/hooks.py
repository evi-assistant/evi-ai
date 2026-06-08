"""Hook system — run arbitrary commands around tool calls.

Config lives in `~/.evi/hooks.toml`:

    [[before_tool_call]]
    name = "audit"
    match = "*"                              # glob over tool names
    command = ["bash", "-c", "echo $EVI_HOOK_TOOL >> ~/.evi/logs/tools.log"]
    timeout = 5

    [[before_tool_call]]
    name = "block-system-writes"
    match = "write_file"
    command = ["python3", "/path/to/check.py"]
    veto_on_nonzero = true

    [[after_tool_call]]
    name = "notify"
    match = "generate_image"
    command = ["notify-send", "Image ready"]

When a hook fires we set these env vars in the child process:

  EVI_HOOK_EVENT    "before_tool_call" | "after_tool_call"
  EVI_HOOK_TOOL     fully-qualified tool name
  EVI_HOOK_ARGS_JSON  json of the call arguments
  EVI_HOOK_RESULT   only for after_tool_call: the tool's stringified output
                    (truncated to 4 KB to avoid blowing the env limit)

A before-hook with `veto_on_nonzero = true` that exits non-zero blocks the
tool call; its stderr becomes the tool result the LLM sees. After-hooks'
exit codes are logged but never block.
"""

from __future__ import annotations

import fnmatch
import logging
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover
    import tomli as tomllib

from evi.config import HOOKS_CONFIG_PATH


logger = logging.getLogger(__name__)


HookEvent = Literal["before_tool_call", "after_tool_call"]


_ENV_RESULT_LIMIT = 4 * 1024  # don't blow OS arg/env limits


@dataclass(frozen=True)
class Hook:
    name: str
    event: HookEvent
    match: str           # glob pattern, e.g. "*", "write_file", "fs.*"
    command: list[str]   # argv-style; we don't shell-eval
    timeout: float = 30.0
    veto_on_nonzero: bool = False  # only meaningful for before_*

    def applies_to(self, tool_name: str) -> bool:
        return fnmatch.fnmatch(tool_name, self.match)


@dataclass
class HookResult:
    hook_name: str
    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool = False

    @property
    def vetoed(self) -> bool:
        return self.exit_code != 0


# --- loader ---------------------------------------------------------------


def _parse_hook_file(p: Path) -> list[Hook]:
    """Parse one hooks.toml into a list of Hooks. Missing file = []; a malformed
    file or entry is skipped with a warning (one bad row can't take the rest down)."""
    if not p.is_file():
        return []
    try:
        with p.open("rb") as f:
            data = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        logger.warning("hooks file unreadable (%s); ignoring", exc)
        return []

    hooks: list[Hook] = []
    for event in ("before_tool_call", "after_tool_call"):
        for entry in data.get(event, []) or []:
            try:
                hooks.append(_parse_entry(event, entry))
            except (KeyError, ValueError, TypeError) as exc:
                logger.warning("skipping malformed hook entry: %s (%s)", entry, exc)
    return hooks


def load_hooks(path: Path | None = None) -> "HookRegistry":
    """Parse `~/.evi/hooks.toml` (or `path`) plus every installed plugin's
    `hooks.toml` into one `HookRegistry`.

    Missing files = empty registry, not an error. Plugin hooks are appended
    after the user's own so user rules are evaluated first.
    """
    hooks = _parse_hook_file(path or HOOKS_CONFIG_PATH)
    try:
        from evi.plugins import plugin_dirs

        for pd in plugin_dirs():
            hooks.extend(_parse_hook_file(pd / "hooks.toml"))
    except Exception as exc:  # plugin scanning must never break core hooks
        logger.warning("plugin hook scan failed (%s); ignoring", exc)
    return HookRegistry(hooks=hooks)


def _parse_entry(event: str, entry: dict) -> Hook:
    name = str(entry.get("name") or f"{event}-{entry.get('match', '*')}")
    match = str(entry.get("match") or "*")
    command_raw = entry.get("command")
    if isinstance(command_raw, str):
        # Allow a single-string form for ergonomics; not shell-expanded.
        command = [command_raw]
    elif isinstance(command_raw, list):
        command = [str(x) for x in command_raw]
    else:
        raise ValueError("hook.command must be a string or list of strings")
    if not command:
        raise ValueError("hook.command cannot be empty")
    timeout = float(entry.get("timeout", 30.0))
    veto = bool(entry.get("veto_on_nonzero", False))
    return Hook(
        name=name,
        event=event,  # type: ignore[arg-type]
        match=match,
        command=command,
        timeout=timeout,
        veto_on_nonzero=veto,
    )


# --- registry + runner ---------------------------------------------------


@dataclass
class HookRegistry:
    hooks: list[Hook] = field(default_factory=list)

    def for_event(self, event: HookEvent, tool_name: str) -> list[Hook]:
        return [h for h in self.hooks if h.event == event and h.applies_to(tool_name)]

    def run_before(
        self, tool_name: str, args_json: str
    ) -> tuple[list[HookResult], HookResult | None]:
        """Run all before-hooks for `tool_name`. Returns (results, veto)
        where `veto` is the first hook that vetoed (if any), else None."""
        results: list[HookResult] = []
        for hook in self.for_event("before_tool_call", tool_name):
            res = _run_hook(hook, tool_name, args_json, result_output=None)
            results.append(res)
            if hook.veto_on_nonzero and res.vetoed:
                return results, res
        return results, None

    def run_after(
        self, tool_name: str, args_json: str, tool_output: str
    ) -> list[HookResult]:
        results: list[HookResult] = []
        for hook in self.for_event("after_tool_call", tool_name):
            res = _run_hook(hook, tool_name, args_json, result_output=tool_output)
            results.append(res)
            if res.vetoed:
                logger.warning(
                    "after-hook %s exited %d: %s",
                    hook.name, res.exit_code, res.stderr[:200],
                )
        return results


def _run_hook(
    hook: Hook,
    tool_name: str,
    args_json: str,
    result_output: str | None,
) -> HookResult:
    """Spawn the hook command with EVI_HOOK_* env vars set."""
    import os

    env = dict(os.environ)
    env["EVI_HOOK_EVENT"] = hook.event
    env["EVI_HOOK_TOOL"] = tool_name
    env["EVI_HOOK_ARGS_JSON"] = args_json
    if result_output is not None:
        env["EVI_HOOK_RESULT"] = result_output[:_ENV_RESULT_LIMIT]

    try:
        proc = subprocess.run(
            hook.command,
            capture_output=True,
            text=True,
            env=env,
            timeout=hook.timeout,
        )
    except subprocess.TimeoutExpired as exc:
        return HookResult(
            hook_name=hook.name,
            exit_code=124,
            stdout=(exc.stdout or b"").decode(errors="replace") if isinstance(exc.stdout, bytes) else (exc.stdout or ""),
            stderr=f"hook {hook.name!r} timed out after {hook.timeout}s",
            timed_out=True,
        )
    except OSError as exc:
        return HookResult(
            hook_name=hook.name,
            exit_code=126,
            stdout="",
            stderr=f"hook {hook.name!r} failed to exec: {exc}",
        )
    return HookResult(
        hook_name=hook.name,
        exit_code=proc.returncode,
        stdout=proc.stdout or "",
        stderr=proc.stderr or "",
    )
