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

    [[after_tool_call]]
    name = "webhook"
    match = "*"
    url = "https://example.com/evi-hook"   # POSTs {event, tool, args_json,
                                           # result} as JSON instead of spawning

A hook uses `command` (argv, spawned) OR `url` (HTTP POST). For a url hook a
2xx response is success; any other status becomes the "exit code", so
`veto_on_nonzero = true` on a before-hook url that returns 4xx/5xx blocks the
call.

Besides the tool events, hooks can fire on lifecycle events (use `match = "*"`,
the default):

    [[user_prompt_submit]]   # before each turn; veto_on_nonzero blocks the prompt
    name = "no-secrets"
    command = ["python3", "/path/check_prompt.py"]
    veto_on_nonzero = true

    [[before_compact]]       # before history compaction; veto keeps it intact
    [[stop]]                 # after a turn completes (notification; veto ignored)

When a hook fires we set these env vars in the child process:

  EVI_HOOK_EVENT    the event name (before_tool_call, user_prompt_submit, …)
  EVI_HOOK_TOOL     tool name for tool events; the event name for lifecycle ones
  EVI_HOOK_ARGS_JSON  tool call args; the prompt for user_prompt_submit
  EVI_HOOK_RESULT   only for after_tool_call: the tool's stringified output
  EVI_EFFORT        the active reasoning effort (low/medium/high/max)
                    (truncated to 4 KB to avoid blowing the env limit)

A before-hook (or user_prompt_submit / before_compact) with
`veto_on_nonzero = true` that exits non-zero blocks the action; for a tool, its
stderr becomes the tool result the LLM sees. After-hooks and `stop` never block.
"""

from __future__ import annotations

import fnmatch
import json
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


HookEvent = Literal[
    "before_tool_call",
    "after_tool_call",
    # Lifecycle events (not tied to a tool). Match against "*" (the default).
    "user_prompt_submit",   # fires before a turn; veto blocks the prompt
    "before_compact",       # fires before history compaction; veto skips it
    "stop",                 # fires after a turn completes (notification)
]

# Tool-scoped events match against a tool name; lifecycle events don't.
TOOL_EVENTS = ("before_tool_call", "after_tool_call")
LIFECYCLE_EVENTS = ("user_prompt_submit", "before_compact", "stop")
ALL_EVENTS = TOOL_EVENTS + LIFECYCLE_EVENTS


_ENV_RESULT_LIMIT = 4 * 1024  # don't blow OS arg/env limits


def _current_effort() -> str:
    """The active reasoning effort, surfaced to hooks (EVI_EFFORT / payload). A
    Bash hook can `$EVI_EFFORT` and an HTTP hook reads `effort`. Defaults to
    'medium' and never raises."""
    try:
        from evi.config import Config

        return (Config.load().llm.reasoning_effort or "medium").strip().lower()
    except Exception:  # noqa: BLE001
        return "medium"


@dataclass(frozen=True)
class Hook:
    name: str
    event: HookEvent
    match: str           # glob pattern, e.g. "*", "write_file", "fs.*"
    command: list[str]   # argv-style; we don't shell-eval (empty for url hooks)
    url: str = ""        # if set, POST the event JSON here instead of spawning
    timeout: float = 30.0
    veto_on_nonzero: bool = False  # only meaningful for before_*
    # Optional per-argument conditions: ((arg_name, glob), …). The hook fires
    # only when the tool name matches AND every listed argument (stringified)
    # matches its glob — e.g. arg_match = { path = "*.env" } so a write-guard
    # only triggers on dotenv files. Mirrors Claude Code's conditional hooks.
    arg_match: tuple[tuple[str, str], ...] = ()

    def applies_to(self, tool_name: str) -> bool:
        return fnmatch.fnmatch(tool_name, self.match)

    def matches(self, tool_name: str, args_json: str | None = None) -> bool:
        """Whether this hook fires for `tool_name` (and, when `args_json` is
        given, its argument conditions). `args_json=None` skips arg matching —
        used for lifecycle events, which carry no tool args."""
        if not self.applies_to(tool_name):
            return False
        if not self.arg_match or args_json is None:
            return True
        try:
            args = json.loads(args_json) if args_json else {}
        except (ValueError, TypeError):
            args = {}
        if not isinstance(args, dict):
            return False
        return all(
            fnmatch.fnmatch(str(args.get(k, "")), pat) for k, pat in self.arg_match
        )


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
    for event in ALL_EVENTS:
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
    url = str(entry.get("url") or "")
    command_raw = entry.get("command")
    if isinstance(command_raw, str):
        # Allow a single-string form for ergonomics; not shell-expanded.
        command = [command_raw]
    elif isinstance(command_raw, list):
        command = [str(x) for x in command_raw]
    elif command_raw is None:
        command = []
    else:
        raise ValueError("hook.command must be a string or list of strings")
    if not url and not command:
        raise ValueError("hook needs a non-empty command or a url")
    timeout = float(entry.get("timeout", 30.0))
    veto = bool(entry.get("veto_on_nonzero", False))
    am_raw = entry.get("arg_match") or {}
    if am_raw and not isinstance(am_raw, dict):
        raise ValueError("hook.arg_match must be a table of arg = glob")
    arg_match = tuple((str(k), str(v)) for k, v in am_raw.items())
    return Hook(
        name=name,
        event=event,  # type: ignore[arg-type]
        match=match,
        command=command,
        url=url,
        timeout=timeout,
        veto_on_nonzero=veto,
        arg_match=arg_match,
    )


# --- editor helpers (raw read / validate / write) ---------------------------
#
# The runtime loader above deliberately *skips* malformed entries (one bad row
# can't take chat down). The editor is the opposite: it must REJECT bad input
# loudly, before it's saved — including typo'd event names, which the loader
# would silently never fire.


def read_raw(path: Path | None = None) -> str:
    p = path or HOOKS_CONFIG_PATH
    try:
        return p.read_text(encoding="utf-8") if p.is_file() else ""
    except OSError:
        return ""


def validate(text: str) -> str | None:
    """Return an error string for bad hooks TOML, or None when it's saveable."""
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        return f"not valid TOML: {exc}"
    for key in data:
        if key not in ALL_EVENTS:
            return (
                f"unknown event {key!r} — valid events: {', '.join(ALL_EVENTS)}"
            )
    for event in ALL_EVENTS:
        entries = data.get(event, []) or []
        if not isinstance(entries, list):
            return f"{event} must be an array of tables ([[{event}]])"
        for i, entry in enumerate(entries, 1):
            if not isinstance(entry, dict):
                return f"{event} entry {i} is not a table"
            try:
                _parse_entry(event, entry)
            except (KeyError, ValueError, TypeError) as exc:
                return f"{event} entry {i} ({entry.get('name', 'unnamed')}): {exc}"
    return None


def write_raw(text: str, path: Path | None = None) -> None:
    p = path or HOOKS_CONFIG_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


# --- registry + runner ---------------------------------------------------


@dataclass
class HookRegistry:
    hooks: list[Hook] = field(default_factory=list)

    def for_event(
        self, event: HookEvent, tool_name: str, args_json: str | None = None
    ) -> list[Hook]:
        return [
            h for h in self.hooks
            if h.event == event and h.matches(tool_name, args_json)
        ]

    def run_before(
        self, tool_name: str, args_json: str
    ) -> tuple[list[HookResult], HookResult | None]:
        """Run all before-hooks for `tool_name`. Returns (results, veto)
        where `veto` is the first hook that vetoed (if any), else None."""
        results: list[HookResult] = []
        for hook in self.for_event("before_tool_call", tool_name, args_json):
            res = _run_hook(hook, tool_name, args_json, result_output=None)
            results.append(res)
            if hook.veto_on_nonzero and res.vetoed:
                return results, res
        return results, None

    def run_after(
        self, tool_name: str, args_json: str, tool_output: str
    ) -> list[HookResult]:
        results: list[HookResult] = []
        for hook in self.for_event("after_tool_call", tool_name, args_json):
            res = _run_hook(hook, tool_name, args_json, result_output=tool_output)
            results.append(res)
            if res.vetoed:
                logger.warning(
                    "after-hook %s exited %d: %s",
                    hook.name, res.exit_code, res.stderr[:200],
                )
        return results

    def run_lifecycle(
        self, event: HookEvent, *, payload: str = ""
    ) -> tuple[list[HookResult], HookResult | None]:
        """Run hooks for a non-tool lifecycle event (user_prompt_submit,
        before_compact, stop). `payload` (e.g. the prompt) is exposed to the
        hook as EVI_HOOK_ARGS_JSON. Returns (results, veto) — veto is the first
        hook that exited non-zero with veto_on_nonzero (blocks the action)."""
        results: list[HookResult] = []
        for hook in self.for_event(event, ""):
            res = _run_hook(hook, event, payload, result_output=None)
            results.append(res)
            if hook.veto_on_nonzero and res.vetoed:
                return results, res
        return results, None


def _run_http_hook(
    hook: Hook,
    tool_name: str,
    args_json: str,
    result_output: str | None,
) -> HookResult:
    """POST the event as JSON to `hook.url`. A 2xx response is success (exit 0);
    any other status becomes the exit code, so `veto_on_nonzero` still works
    (a before-hook URL that returns 4xx/5xx blocks the call)."""
    import urllib.error
    import urllib.request

    payload: dict[str, str] = {
        "event": hook.event,
        "tool": tool_name,
        "args_json": args_json,
        "effort": _current_effort(),
    }
    if result_output is not None:
        payload["result"] = result_output[:_ENV_RESULT_LIMIT]
    req = urllib.request.Request(
        hook.url,
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json", "User-Agent": "evi-hook"},
    )
    try:
        with urllib.request.urlopen(req, timeout=hook.timeout) as resp:
            body = resp.read().decode(errors="replace")
            status = resp.status
        return HookResult(
            hook_name=hook.name,
            exit_code=0 if 200 <= status < 300 else status,
            stdout=body[:_ENV_RESULT_LIMIT],
            stderr="" if 200 <= status < 300 else f"HTTP {status}",
        )
    except urllib.error.HTTPError as exc:
        return HookResult(
            hook_name=hook.name, exit_code=exc.code, stdout="", stderr=f"HTTP {exc.code}"
        )
    except (TimeoutError, urllib.error.URLError, OSError) as exc:
        timed = isinstance(exc, TimeoutError) or isinstance(
            getattr(exc, "reason", None), TimeoutError
        )
        return HookResult(
            hook_name=hook.name,
            exit_code=124 if timed else 126,
            stdout="",
            stderr=f"hook {hook.name!r} HTTP failed: {exc}",
            timed_out=timed,
        )


def _run_hook(
    hook: Hook,
    tool_name: str,
    args_json: str,
    result_output: str | None,
) -> HookResult:
    """Run a hook: POST to its URL, or spawn its command with EVI_HOOK_* env."""
    if hook.url:
        return _run_http_hook(hook, tool_name, args_json, result_output)

    import os

    env = dict(os.environ)
    env["EVI_HOOK_EVENT"] = hook.event
    env["EVI_HOOK_TOOL"] = tool_name
    env["EVI_HOOK_ARGS_JSON"] = args_json
    env["EVI_EFFORT"] = _current_effort()
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
