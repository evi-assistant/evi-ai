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
    [[session_start]]        # when an interactive session begins (setup)
    [[session_end]]          # when an interactive session ends (teardown)

When a hook fires we set these env vars in the child process:

  EVI_HOOK_EVENT    the event name (before_tool_call, user_prompt_submit, …)
  EVI_HOOK_TOOL     tool name for tool events; the event name for lifecycle ones
  EVI_HOOK_ARGS_JSON  tool call args; the prompt for user_prompt_submit; the
                    finish reason for stop. Whole whenever it fits in one env
                    variable (_ENV_ARGS_LIMIT); see "Big payloads" below
  EVI_HOOK_ARGS_FILE  path of a temp file holding the whole payload: set when
                    EVI_HOOK_ARGS_JSON was cut, or always for a hook with
                    `args_via_file = true`. Deleted after the hook exits
  EVI_HOOK_ARGS_TRUNCATED  "1" when EVI_HOOK_ARGS_JSON was cut
  EVI_HOOK_RESULT   only for after_tool_call: the tool's stringified output
                    (truncated to 4 KB to avoid blowing the env limit)
  EVI_EFFORT        the active reasoning effort (low/medium/high/max)
  EVI_CWD           the session working folder the tools resolve paths against

A command hook runs in that session folder (EVI_CWD), and its stdin is the null
device, so a hook that reads stdin gets EOF instead of hanging on the terminal
(or on the sidecar's pipe).

Big payloads: one env variable holds about 32,000 characters on Windows and
128 KiB on Linux. A payload over that is cut for EVI_HOOK_ARGS_JSON (a JSON
object keeps every key and stays valid JSON; its longest string values are
shortened to one length, or, when that can't fit, it keeps a prefix) and the
whole of it goes in EVI_HOOK_ARGS_FILE. A guard must never judge a cut
payload, so a vetoing hook that has not set `args_via_file = true` is not run
on one: the action is blocked (fail closed).

A before-hook (or user_prompt_submit / before_compact) with
`veto_on_nonzero = true` that exits non-zero blocks the action; for a tool, its
stderr becomes the tool result the LLM sees. After-hooks never block, and
neither does a plain `stop` hook.

Blocking stop hooks (verify-before-done): a `[[stop]]` hook with
`protocol = "json"` runs each time the model would end its turn, before the
turn ends (and after the project checks, evi/checks.py, passed), and may send
it back to work:

    [[stop]]
    name = "pytest-gate"
    protocol = "json"
    command = ["python", "/path/to/gate.py"]

Its stdin is one JSON object: {"event": "stop", "session_id", "cwd",
"stop_hook_active" (true once a block has already sent the model back this
turn), "iteration" (1 at the turn's first would-be end), "files_changed" (the
paths the turn changed, relative to cwd)}; EVI_HOOK_ARGS_JSON holds the same.
The hook runs in that cwd, the session's working folder. It blocks by
printing {"decision": "block", "reason": "..."} or by exiting 2 (its stderr is
the reason). Exit 0 with no such JSON lets the turn end. A timeout or any other
exit is a hook error: logged, never a block. A url hook with the protocol is
POSTed that object and blocks with the same JSON in its 2xx response. The
reason reaches the model as a framed note and the loop goes on, at most
`[hooks] max_blocks` (default 3) times per user turn; a block after that ends
the turn with "stop_hook_exhausted" (evi.llm.agent). Only the `json` protocol
blocks: a plain stop hook that happens to exit 2 stays a notification, and a
`json` hook is not run again as one when the turn ends. A subagent runs no
lifecycle hooks (`HookRegistry.tool_hooks`). eVi loads hooks only from your
own hooks.toml and installed plugins, never from a repository.
"""

from __future__ import annotations

import atexit
import contextvars
import fnmatch
import json
import logging
import os
import shutil
import subprocess
import tempfile
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import tomllib

from evi.config import HOOKS_CONFIG_PATH


logger = logging.getLogger(__name__)


HookEvent = Literal[
    "before_tool_call",
    "after_tool_call",
    # Lifecycle events (not tied to a tool). Match against "*" (the default).
    "user_prompt_submit",   # fires before a turn; veto blocks the prompt
    "before_compact",       # fires before history compaction; veto skips it
    "stop",                 # fires after a turn completes (notification)
    "session_start",        # fires when an interactive session begins
    "session_end",          # fires when an interactive session ends
]

# Tool-scoped events match against a tool name; lifecycle events don't.
TOOL_EVENTS = ("before_tool_call", "after_tool_call")
LIFECYCLE_EVENTS = (
    "user_prompt_submit", "before_compact", "stop",
    "session_start", "session_end",
)
ALL_EVENTS = TOOL_EVENTS + LIFECYCLE_EVENTS
# Events whose hooks can block the action (with veto_on_nonzero).
VETO_EVENTS = ("before_tool_call", "user_prompt_submit", "before_compact")
# Hook `protocol` values: "" (the env-var protocol above) or "json" (a stop
# hook that gets the event on stdin and may block the turn's end).
PROTOCOLS = ("", "json")
# A stop hook's exit code that blocks (with protocol "json"); its stderr is why.
BLOCK_EXIT = 2


_ENV_RESULT_LIMIT = 4 * 1024  # don't blow OS arg/env limits
# The most of a payload EVI_HOOK_ARGS_JSON holds, in the units the OS counts one
# environment string in. Windows documents 32,767 UTF-16 units per variable;
# Linux refuses any single env string over 128 KiB (MAX_ARG_STRLEN, E2BIG),
# counting its "NAME=" and NUL. Both are kept a little under; a payload that
# fits reaches the hook whole, exactly as before the cap existed.
_ENV_UNIT_BYTES = 2 if os.name == "nt" else 1  # UTF-16 units vs UTF-8 bytes
_ENV_ARGS_LIMIT = 32_000 if os.name == "nt" else 120 * 1024


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
    # The hook reads the whole payload from EVI_HOOK_ARGS_FILE (always written
    # for it), so it may judge one too big for EVI_HOOK_ARGS_JSON. A vetoing
    # hook without it is not run on a cut payload: the action is blocked.
    args_via_file: bool = False
    # Optional per-argument conditions: ((arg_name, glob), …). The hook fires
    # only when the tool name matches AND every listed argument (stringified)
    # matches its glob — e.g. arg_match = { path = "*.env" } so a write-guard
    # only triggers on dotenv files. Mirrors Claude Code's conditional hooks.
    arg_match: tuple[tuple[str, str], ...] = ()
    # "json": a stop hook that gets the event as JSON on stdin and may block
    # the turn's end (see the module doc). "" for every other hook.
    protocol: str = ""

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


@dataclass
class StopDecision:
    """What one `protocol = "json"` stop hook decided: "block" (with its
    reason), "allow", or "error" (a timeout or an unexpected exit, with what
    went wrong; never a block)."""

    hook_name: str
    decision: str  # "block" | "allow" | "error"
    reason: str = ""

    @property
    def blocked(self) -> bool:
        return self.decision == "block"


def _last_json_object(text: str) -> dict | None:
    """The JSON object a hook printed: its whole stdout, else the last line
    that parses as one."""
    stripped = (text or "").strip()
    candidates = [stripped] + [line.strip() for line in reversed(stripped.splitlines())]
    for candidate in candidates:
        if not candidate.startswith("{"):
            continue
        try:
            data = json.loads(candidate)
        except (ValueError, RecursionError):
            continue
        if isinstance(data, dict):
            return data
    return None


def stop_decision(res: HookResult) -> StopDecision:
    """A json stop hook's result as its decision (see the module doc)."""
    if res.timed_out:
        return StopDecision(res.hook_name, "error", res.stderr.strip() or "timed out")
    if res.exit_code == BLOCK_EXIT:
        why = res.stderr.strip() or res.stdout.strip() or "(no reason given)"
        return StopDecision(res.hook_name, "block", why)
    if res.exit_code != 0:
        why = res.stderr.strip()[:500] or f"exit {res.exit_code}"
        return StopDecision(res.hook_name, "error", f"exit {res.exit_code}: {why}")
    data = _last_json_object(res.stdout)
    if data is not None and str(data.get("decision", "")).strip().lower() == "block":
        reason = data.get("reason")
        why = str(reason).strip() if reason not in (None, "") else ""
        return StopDecision(res.hook_name, "block", why or "(no reason given)")
    return StopDecision(res.hook_name, "allow")


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

    Safe mode returns an empty registry — gating here rather than at each call
    site covers the SDK and `evi review`, which build hooks without going
    through the CLI's flag handling.
    """
    from evi import safemode

    if safemode.enabled():
        return HookRegistry(hooks=[])
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
    via_file = entry.get("args_via_file", False)
    if not isinstance(via_file, bool):
        raise ValueError("hook.args_via_file must be true or false")
    am_raw = entry.get("arg_match") or {}
    if am_raw and not isinstance(am_raw, dict):
        raise ValueError("hook.arg_match must be a table of arg = glob")
    arg_match = tuple((str(k), str(v)) for k, v in am_raw.items())
    protocol = entry.get("protocol", "")
    if not isinstance(protocol, str) or protocol.strip().lower() not in PROTOCOLS:
        raise ValueError('hook.protocol must be "json" (or left out)')
    protocol = protocol.strip().lower()
    if protocol and event != "stop":
        raise ValueError('protocol = "json" is only for [[stop]] hooks')
    return Hook(
        name=name,
        event=event,  # type: ignore[arg-type]
        match=match,
        command=command,
        url=url,
        timeout=timeout,
        veto_on_nonzero=veto,
        args_via_file=via_file,
        arg_match=arg_match,
        protocol=protocol,
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

    def can_veto(
        self, event: HookEvent, tool_name: str = "", args_json: str | None = None
    ) -> bool:
        """Whether a hook that can block `event` (for this tool call) is set.
        The agent fails closed on it when the hooks could not be run at all."""
        return event in VETO_EVENTS and any(
            h.veto_on_nonzero for h in self.for_event(event, tool_name, args_json)
        )

    def tool_hooks(self) -> "HookRegistry":
        """A copy with only the tool hooks (before/after_tool_call): what a
        subagent inherits from the agent that spawned it. Lifecycle hooks belong
        to the user's own turn: a subagent's task is not a prompt the user
        typed, and its end is not the end of the user's turn."""
        return HookRegistry(hooks=[h for h in self.hooks if h.event in TOOL_EVENTS])

    def run_before(
        self, tool_name: str, args_json: str, *, cwd: str = ""
    ) -> tuple[list[HookResult], HookResult | None]:
        """Run all before-hooks for `tool_name`. Returns (results, veto)
        where `veto` is the first hook that vetoed (if any), else None.
        `cwd` is the session folder the call runs in ("" = the process cwd)."""
        results: list[HookResult] = []
        for hook in self.for_event("before_tool_call", tool_name, args_json):
            res = _run_hook(hook, tool_name, args_json, result_output=None, cwd=cwd)
            results.append(res)
            if hook.veto_on_nonzero and res.vetoed:
                return results, res
        return results, None

    def run_after(
        self, tool_name: str, args_json: str, tool_output: str, *, cwd: str = ""
    ) -> list[HookResult]:
        results: list[HookResult] = []
        for hook in self.for_event("after_tool_call", tool_name, args_json):
            res = _run_hook(hook, tool_name, args_json, result_output=tool_output, cwd=cwd)
            results.append(res)
            if res.vetoed:
                logger.warning(
                    "after-hook %s exited %d: %s",
                    hook.name, res.exit_code, res.stderr[:200],
                )
        return results

    def run_lifecycle(
        self, event: HookEvent, *, payload: str = "", cwd: str = ""
    ) -> tuple[list[HookResult], HookResult | None]:
        """Run hooks for a non-tool lifecycle event (user_prompt_submit,
        before_compact, stop). `payload` (e.g. the prompt) is exposed to the
        hook as EVI_HOOK_ARGS_JSON. Returns (results, veto) — veto is the first
        hook that exited non-zero with veto_on_nonzero (blocks the action)."""
        results: list[HookResult] = []
        for hook in self.for_event(event, ""):
            if hook.protocol == "json":
                continue  # a blocking stop hook: run_stop ran it before the end
            res = _run_hook(hook, event, payload, result_output=None, cwd=cwd)
            results.append(res)
            if hook.veto_on_nonzero and res.vetoed:
                return results, res
        return results, None

    def json_stop_hooks(self) -> list[Hook]:
        """The `protocol = "json"` stop hooks: the ones that can block."""
        return [h for h in self.for_event("stop", "") if h.protocol == "json"]

    def run_stop(
        self, payload: dict, *, cwd: str = ""
    ) -> tuple[list[StopDecision], StopDecision | None]:
        """Run the `protocol = "json"` stop hooks at a would-be end of the
        turn, `payload` (the event object, see the module doc) on each one's
        stdin. Returns (decisions, block): the first hook that blocked, after
        which no further hook runs, else None. A hook error is logged and
        counts as no block."""
        decisions: list[StopDecision] = []
        text = json.dumps(payload)  # ASCII: any hook's stdin decoding reads it
        for hook in self.json_stop_hooks():
            res = _run_hook(hook, "stop", text, result_output=None, cwd=cwd, stdin=text)
            decision = stop_decision(res)
            decisions.append(decision)
            if decision.decision == "error":
                logger.warning("stop hook %s failed (not a block): %s",
                               hook.name, decision.reason[:200])
            if decision.blocked:
                return decisions, decision
        return decisions, None


# --- the registry an agent's tools run under -------------------------------
#
# Set by the agent around each tool call (on the tool's own copied context, from
# its own `hooks`), so a tool that spawns a sub-agent — `delegate`, parallel
# research — hands the same hooks on. It only ever flows down: nothing reads it
# back into the agent loop.

_ACTIVE: contextvars.ContextVar["HookRegistry | None"] = contextvars.ContextVar(
    "evi_active_hooks", default=None
)


def bind(registry: "HookRegistry | None") -> contextvars.Token:
    """Make `registry` the active one for this context. Returns a token for
    :func:`unbind`."""
    return _ACTIVE.set(registry)


def unbind(token: contextvars.Token | None) -> None:
    if token is not None:
        try:
            _ACTIVE.reset(token)
        except (ValueError, LookupError):
            pass


def active() -> "HookRegistry | None":
    """The hooks of the agent whose tool is running here, or None."""
    return _ACTIVE.get()


# --- one registry for a long-lived server -----------------------------------
#
# `evi web` / the desktop app build many agents (one per tab, plus workflow,
# recipe and routine runs). They share one registry, loaded once and reloaded IN
# PLACE when Settings → Hooks saves, so open sessions pick the edit up without
# a restart.

_SHARED: "HookRegistry | None" = None
_SHARED_LOCK = threading.Lock()


def shared_registry() -> "HookRegistry":
    """The process-wide registry, loaded on first use (`load_hooks`, so safe
    mode gives an empty one)."""
    global _SHARED
    with _SHARED_LOCK:
        if _SHARED is None:
            _SHARED = load_hooks()
        return _SHARED


def reload_shared() -> "HookRegistry":
    """Re-read hooks.toml (+ plugin hooks) into the shared registry in place, so
    every agent already holding it sees the change on its next hook run."""
    global _SHARED
    fresh = load_hooks()
    with _SHARED_LOCK:
        if _SHARED is None:
            _SHARED = fresh
        else:
            _SHARED.hooks = fresh.hooks
        return _SHARED


def _run_http_hook(
    hook: Hook,
    tool_name: str,
    args_json: str,
    result_output: str | None,
    cwd: str = "",
    body: str | None = None,
) -> HookResult:
    """POST the event as JSON to `hook.url` (`body`, when given, is the JSON
    text to POST instead: a json stop hook's event). A 2xx response is
    success (exit 0); any other status becomes the exit code, so
    `veto_on_nonzero` still works (a before-hook URL that returns 4xx/5xx
    blocks the call)."""
    import urllib.error
    import urllib.request

    payload: dict[str, str] = {
        "event": hook.event,
        "tool": tool_name,
        "args_json": args_json,
        "effort": _current_effort(),
        "cwd": cwd or os.getcwd(),
    }
    if result_output is not None:
        payload["result"] = result_output[:_ENV_RESULT_LIMIT]
    req = urllib.request.Request(
        hook.url,
        data=(body if body is not None else json.dumps(payload)).encode("utf-8"),
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
    cwd: str = "",
    stdin: str | None = None,
) -> HookResult:
    """Run a hook: POST to its URL, or spawn its command with EVI_HOOK_* env,
    in the session folder `cwd` ("" = the process cwd). `stdin` (a json stop
    hook's event) is the command's stdin, or the url hook's POST body;
    otherwise stdin is the null device."""
    if hook.url:
        return _run_http_hook(hook, tool_name, args_json, result_output, cwd, body=stdin)

    _retry_pending()
    env = dict(os.environ)
    env.pop("EVI_HOOK_ARGS_TRUNCATED", None)
    env.pop("EVI_HOOK_ARGS_FILE", None)
    env["EVI_HOOK_EVENT"] = hook.event
    env["EVI_HOOK_TOOL"] = _env_safe(tool_name)
    env["EVI_EFFORT"] = _current_effort()
    env["EVI_CWD"] = _env_safe(cwd or os.getcwd())
    if result_output is not None:
        env["EVI_HOOK_RESULT"] = _env_safe(result_output[:_ENV_RESULT_LIMIT])

    payload = args_json or ""
    whole = _env_safe(payload)
    cut = _env_size(whole) > _ENV_ARGS_LIMIT
    guard = hook.veto_on_nonzero and hook.event in VETO_EVENTS
    if cut and guard and not hook.args_via_file:
        # The hook would judge input it can't see all of (padding can push a
        # write's `path` past any cut). Don't run it: block (fail closed).
        return _too_big(hook, whole)
    env["EVI_HOOK_ARGS_JSON"] = _shorten(whole, _ENV_ARGS_LIMIT) if cut else whole
    if cut:
        env["EVI_HOOK_ARGS_TRUNCATED"] = "1"
    # The file holds the payload exactly. It is written only when the env copy
    # is cut or the hook asks for it, so small prompts and args stay off disk.
    args_file = ""
    if cut or hook.args_via_file:
        args_file = _write_args_file(payload.encode("utf-8", errors="replace"))
        if args_file:
            env["EVI_HOOK_ARGS_FILE"] = args_file
        elif cut and guard:
            return _too_big(hook, whole, file_failed=True)
    try:
        return _spawn_hook(hook, env, cwd, stdin=stdin)
    finally:
        _drop_args_file(args_file)


def _too_big(hook: Hook, whole: str, file_failed: bool = False) -> HookResult:
    """The result for a guard not run on input too big for EVI_HOOK_ARGS_JSON:
    a veto (exit 126, like a hook that can't be started)."""
    why = (
        "and EVI_HOOK_ARGS_FILE could not be written"
        if file_failed
        else "To check input this large, set args_via_file = true on the hook "
        "and read EVI_HOOK_ARGS_FILE."
    )
    logger.warning(
        "hook %s not run: its %d-character input is over EVI_HOOK_ARGS_JSON's "
        "limit; the action is blocked", hook.name, len(whole),
    )
    return HookResult(
        hook_name=hook.name,
        exit_code=126,
        stdout="",
        stderr=(
            f"hook {hook.name!r} could not check this: its input "
            f"({len(whole):,} characters) is too large for EVI_HOOK_ARGS_JSON"
            + (" " if file_failed else ". ") + why
        ),
    )


# --- payload helpers ----------------------------------------------------------


def _env_safe(text: str) -> str:
    """`text` in a form an env value can carry: NUL (no OS allows one) becomes
    the JSON escape \\u0000 and a lone surrogate becomes '?'. The args file
    keeps the payload exactly."""
    return text.replace("\x00", "\\u0000").encode("utf-8", errors="replace").decode("utf-8")


def _env_size(text: str) -> int:
    """Size of an env value in the units the OS limits (see _ENV_ARGS_LIMIT)."""
    if _ENV_UNIT_BYTES == 2:
        return len(text.encode("utf-16-le")) // 2
    return len(text.encode("utf-8"))


def _prefix(text: str, limit: int) -> str:
    """The longest prefix of `text` within `limit`, cut at a character."""
    enc = "utf-16-le" if _ENV_UNIT_BYTES == 2 else "utf-8"
    return text.encode(enc)[: limit * _ENV_UNIT_BYTES].decode(enc, errors="ignore")


def _shorten(text: str, limit: int) -> str:
    """Fit a payload into `limit` for EVI_HOOK_ARGS_JSON. A JSON object or array
    keeps every key and stays valid JSON: its longest string values are cut,
    each ending in a marker. Anything else (a prompt) keeps a prefix."""
    try:
        data = json.loads(text)
    except (ValueError, RecursionError):
        data = None
    if isinstance(data, (dict, list)):
        try:
            fitted = _shorten_json(data, limit)
        except (ValueError, RecursionError):
            fitted = None
        if fitted is not None:
            return fitted
    return _prefix(text, limit)


def _shorten_json(data: dict | list, limit: int) -> str | None:
    """`data` as JSON within `limit`, or None. Every string value longer than
    one shared length keeps that many characters plus a marker; the length is
    the largest that fits. None when even cutting each of them to its bare
    marker is over `limit` (the caller keeps a prefix instead).

    This runs on the loop thread before the hook starts, outside its timeout,
    so the work is bounded: one dump of the whole document, then a binary
    search on the shared length whose dumps (about log2 of the longest
    string's length) each stay near `limit`."""
    # (node, key, original, marker) for each string worth cutting: one no
    # longer than its marker can't get shorter.
    leaves: list[tuple[dict | list, object, str, str]] = []
    stack: list[dict | list] = [data]
    while stack:
        node = stack.pop()
        for key, value in (node.items() if isinstance(node, dict) else enumerate(node)):
            if isinstance(value, str):
                mark = f"...[cut {len(value)} chars; see EVI_HOOK_ARGS_FILE]"
                if len(value) > len(mark):
                    leaves.append((node, key, value, mark))
            elif isinstance(value, (dict, list)):
                stack.append(value)

    def dump() -> str:
        return _env_safe(json.dumps(data, ensure_ascii=False))

    def cut_to(keep: int) -> None:
        # A string is cut only when that makes it shorter.
        for node, key, value, mark in leaves:
            node[key] = (  # type: ignore[index]
                value if len(value) <= keep + len(mark) else value[:keep] + mark
            )

    text = dump()  # a compact re-dump may already fit
    if _env_size(text) <= limit:
        return text
    cut_to(0)
    text = dump()
    base = _env_size(text)
    if not leaves or base > limit:
        return None

    def least_size(keep: int) -> int:
        # Every kept character costs at least one unit and a marker exactly
        # its length, so the real size is never below this. A length this
        # rules out is never dumped, which keeps every dump near `limit`.
        return base + sum(min(keep, len(v) - len(m)) for _, _, v, m in leaves)

    lo, hi = 0, max(len(v) for _, _, v, _ in leaves)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if least_size(mid) > limit:
            hi = mid - 1
            continue
        cut_to(mid)
        candidate = dump()
        if _env_size(candidate) <= limit:
            lo, text = mid, candidate
        else:
            hi = mid - 1
    return text


# --- the args file --------------------------------------------------------------
#
# Payload files live in one private directory per process (mkdtemp: 0700 on
# POSIX) and are deleted after their hook exits. One that can't be deleted then
# (on Windows, a background child the hook started still has it open) is
# retried before the next hook runs and when eVi exits, which also removes the
# directory.

_ARGS_LOCK = threading.Lock()
_ARGS_DIRS: list[str] = []
_PENDING: list[str] = []


def _args_dir(fresh: bool = False) -> str:
    with _ARGS_LOCK:
        if _ARGS_DIRS and not fresh and os.path.isdir(_ARGS_DIRS[-1]):
            return _ARGS_DIRS[-1]
        try:
            path = tempfile.mkdtemp(prefix="evi-hooks-")
        except OSError:
            return ""
        if not _ARGS_DIRS:
            atexit.register(_cleanup_args_files)
        _ARGS_DIRS.append(path)
        return path


def _write_args_file(raw: bytes) -> str:
    """Write a hook's payload to a private temp file and return its path ("" on
    failure). The caller deletes it (`_drop_args_file`) once the hook has run."""
    for fresh in (False, True):  # the directory may have been removed under us
        folder = _args_dir(fresh)
        if not folder:
            return ""
        try:
            fd, path = tempfile.mkstemp(prefix="args-", suffix=".json", dir=folder)
        except OSError:
            continue
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(raw)
        except OSError:
            _drop_args_file(path)
            return ""
        return path
    return ""


def _drop_args_file(path: str) -> None:
    if not path:
        return
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
    except OSError:
        with _ARGS_LOCK:
            _PENDING.append(path)


def _retry_pending() -> None:
    with _ARGS_LOCK:
        pending = list(_PENDING)
        _PENDING.clear()
    for path in pending:
        _drop_args_file(path)


def _cleanup_args_files() -> None:
    """Delete every payload file still left, and their directories (atexit)."""
    _retry_pending()
    with _ARGS_LOCK:
        dirs = list(_ARGS_DIRS)
    for folder in dirs:
        shutil.rmtree(folder, ignore_errors=True)


def _spawn_hook(
    hook: Hook, env: dict[str, str], cwd: str = "", stdin: str | None = None
) -> HookResult:
    # The null device, not our stdin: a hook that reads stdin would otherwise
    # block on the terminal (or the desktop sidecar's pipe) until its timeout.
    # A json stop hook reads its event there instead.
    # Its output is read as UTF-8 (the event it gets is ASCII JSON).
    feed: dict = (
        {"stdin": subprocess.DEVNULL} if stdin is None
        else {"input": stdin, "encoding": "utf-8", "errors": "replace"}
    )
    try:
        proc = subprocess.run(
            hook.command,
            **feed,
            capture_output=True,
            text=True,
            env=env,
            # The session folder, so a hook resolves a relative path the way
            # the tool it guards does.
            cwd=cwd or None,
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
    except (OSError, ValueError) as exc:
        # ValueError: an argument the OS can't take (a NUL in the command).
        # Reported like a failed exec, so a guard blocks.
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
