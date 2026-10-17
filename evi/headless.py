"""Headless (non-interactive) single-shot runs.

Backs `evi run "<prompt>"` — drain one agent turn-loop to completion and return
a structured result, for scripts / CI / cron. The CLI builds the agent (and
sets a non-interactive permission policy); this module just drains + formats, so
it's testable with a fake agent.

The result is the run report, versioned with the event contract
(`evi.events.SCHEMA_VERSION`; `HeadlessResult.to_dict`, the ``report``
definition of `evi.events.SCHEMA`): the session and its transcript, the
backend, model and working folder, the turns used, why the run stopped, every
tool call (its name, arguments and output redacted, the latter two then cut
short, its status and how long it took, and a denied call's reason), the
calls the permission checks denied, the error (redacted), the token usage
summed over the run, how long it took, the files it changed (when asked to
track them), the verify checks it ran (``checks``: name, cmd, exit,
duration_ms, tail, round, source; ``checks_skipped`` says why none ran in an
untrusted folder) and the stop-hook blocks (``stop_blocks``: hook, reason,
iteration; evi.llm.agent's verify gate), the policy it ran under
(`evi.llm.agent.policy_label`: a named policy such as "implementer",
`evi.policy`, else "deny", "allow", "allow-all" or "interactive") and, for a
run isolated in a git worktree (`evi run --worktree`), what it left there
(`worktree`).

``stop_reason`` is one of `evi.events.STOP_REASONS`, and `exit_code` turns it
into `evi run`'s exit code:

- ``done`` (0) — the model finished. Tool calls the permission checks denied
  on the way don't change that (the loop goes on past a denial, and an
  unattended run is denied whatever no rule approves): they are listed in
  ``denied_tools``, for the caller to judge;
- ``error`` (1) — the backend failed (the turn's Error event), or, with
  ``report_errors``, the turn raised;
- ``max_turns`` (3) — the run used every turn it was given (``max_turns``);
  ``step_budget`` and ``loop_detected`` (3) likewise ran out;
- ``permission_denied`` (4) — the turn itself stopped on a denial (its Done
  reason says so); ``guardrail`` (4) — a guardrail or a user_prompt_submit
  hook refused it;
- ``checks_failed`` (5) — the project checks still failed after ``[checks]
  max_rounds`` failing rounds (evi/checks.py);
- ``stop_hook_exhausted`` (6) — a ``protocol = "json"`` stop hook kept
  blocking past ``[hooks] max_blocks``;
- ``interrupted`` (130, 128 + SIGINT) — it was cancelled.

The agent's own Done reason is kept as ``done_reason``.

With ``cancel_on_sigint`` (what `evi run` asks for), Ctrl+C / SIGINT cancels
the turn (`Agent.cancel`) instead of raising KeyboardInterrupt through it: the
turn ends with Done("interrupted"), its commands' process trees are killed,
and the result comes back as usual with ``stop_reason="interrupted"``. A
second SIGINT stops waiting for the turn to wind down (the result is still
returned, never a traceback). A Ctrl+C while the files changed are listed,
after the turn, skips that (``files_changed`` null) and keeps the turn's
report.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import signal
import stat
import time
from dataclasses import InitVar, dataclass, field
from pathlib import Path
from typing import Any, Callable

from evi.events import SCHEMA_VERSION, STOP_REASONS
from evi.llm.agent import (
    CANCELLED,
    INTERRUPTED_REASON,
    CheckResult,
    ChecksFailed,
    ChecksSkipped,
    Done,
    Error,
    StopBlocked,
    TextDelta,
    ThinkingDelta,
    ToolCall,
    ToolResult,
    UsageStats,
    is_permission_denial,
    policy_label,
)

# What a SIGINT-cancelled turn tells its notes (Agent.cancel's reason).
SIGINT_REASON = "the run was interrupted (Ctrl+C / SIGINT)"

# How many characters of a tool call's arguments and output a result keeps,
# after they are redacted (so no prefix of a secret is kept either).
MAX_SHOWN = 2000

# An agent's Done reason as a report's stop_reason. Any other reason ("stop",
# "length", "tool_calls", "end_turn", ...) is the model finishing: "done".
_STOP_OF_DONE = {
    **{reason: reason for reason in STOP_REASONS if reason != "done"},
    "max_turns_reached": "max_turns",
    INTERRUPTED_REASON: "interrupted",
    "guardrail_blocked": "guardrail",
    "hook_blocked": "guardrail",
    "content_filter": "guardrail",
}

# `evi run`'s exit code for each stop_reason. 2 is a usage error (a bad flag,
# no prompt, an unknown session or backend): no run happened.
EXIT_CODES = {
    "done": 0,
    "error": 1,
    "max_turns": 3,
    "step_budget": 3,
    "loop_detected": 3,
    "permission_denied": 4,
    "guardrail": 4,
    "checks_failed": 5,
    "stop_hook_exhausted": 6,
    "interrupted": 130,
}
USAGE_EXIT = 2
# How much of a denied call's reason a report keeps.
MAX_REASON = 500
# How a denial's text starts (evi.llm.agent._DENIAL_PREFIXES): the rest is
# its reason.
_DENIAL_LEADS = ("PERMISSION DENIED: ", "BLOCKED by the destructive-command guard: ")


def stop_reason_of(done_reason: str | None) -> str:
    """A turn's Done reason as a report's stop_reason (see the module doc)."""
    return _STOP_OF_DONE.get(str(done_reason or "").strip().lower(), "done")


def exit_code(stop_reason: str | None) -> int:
    """`evi run`'s exit code for `stop_reason` (None: nothing said, done)."""
    return EXIT_CODES.get(stop_reason or "done", 1)


@dataclass
class HeadlessResult:
    # The model's text; when the verify gate sent it back (a stop-hook block,
    # a failing check round), only what it wrote after the last of those.
    text: str = ""
    # One entry per tool call, in call order: {name, args, status,
    # duration_ms, output} (`evi.events.SCHEMA` $defs/tool_call).
    tool_calls: list[dict] = field(default_factory=list)
    # {"prompt", "completion", "total"} summed over the run's model calls;
    # None when the backend reported no usage.
    usage: dict | None = None
    error: str | None = None
    # How a tool call that would ask was settled in this run (policy_label).
    policy: str | None = None
    # Why the run stopped: one of evi.events.STOP_REASONS; None if it never
    # said (a result nobody filled in).
    stop_reason: str | None = None
    # The turn's own Done reason ("stop", "max_turns_reached", ...).
    done_reason: str | None = None
    session_id: str | None = None
    transcript_path: str | None = None
    backend: str | None = None
    model: str | None = None
    cwd: str | None = None
    # The working folder's trust state (evi.trust), when the caller checked.
    trust: str | None = None
    turns_used: int = 0
    max_turns: int | None = None
    # The tools whose calls the permission checks denied, in order.
    denied_tools: list[str] = field(default_factory=list)
    duration_s: float = 0.0
    # [{path, status, added, deleted}]; None when not tracked.
    files_changed: list[dict] | None = None
    # The verify checks the run ran (evi/checks.py: CheckRun.record), in order.
    checks: list[dict] = field(default_factory=list)
    # Why no check ran although files changed (an untrusted folder), or None.
    checks_skipped: str | None = None
    # [{hook, reason, iteration}]: each time a json stop hook blocked the end.
    stop_blocks: list[dict] = field(default_factory=list)
    # What was redacted from the tool calls and the error, by kind.
    redactions: dict[str, int] = field(default_factory=dict)
    # The isolated run's git worktree (evi.worktree.worktree_report): path,
    # branch, base and head, commits, dirty state, diffstat; None when the
    # run was not isolated.
    worktree: dict | None = None
    # Deprecated, for SDK callers from before the report: the tool outputs
    # as [{name, output}], taken as `tool_calls` when none are given. Read
    # (or set) them as `.tools`, below the class.
    tools: InitVar[list[dict] | None] = None

    def __post_init__(self, tools: list[dict] | None) -> None:
        # Not over tool_calls given too: dataclasses.replace passes both.
        if tools is not None and not self.tool_calls:
            _set_tools(self, tools)

    @property
    def interrupted(self) -> bool:
        return self.stop_reason == "interrupted"

    @property
    def exit_code(self) -> int:
        return exit_code(self.stop_reason)

    def to_dict(self) -> dict[str, Any]:
        """The run report (`evi.events.report_schema()`)."""
        return {
            "schema_version": SCHEMA_VERSION,
            "session_id": self.session_id,
            "transcript_path": self.transcript_path,
            "backend": self.backend,
            "model": self.model,
            "cwd": self.cwd,
            "trust": self.trust,
            "turns_used": self.turns_used,
            "max_turns": self.max_turns,
            "stop_reason": self.stop_reason,
            "done_reason": self.done_reason,
            "text": self.text,
            "error": self.error,
            "tool_calls": [dict(c) for c in self.tool_calls],
            "denied_tools": list(self.denied_tools),
            "usage": dict(self.usage) if self.usage is not None else None,
            "duration_s": self.duration_s,
            "files_changed": (
                [dict(f) for f in self.files_changed] if self.files_changed is not None else None
            ),
            "checks": [dict(c) for c in self.checks],
            "checks_skipped": self.checks_skipped,
            "stop_blocks": [dict(b) for b in self.stop_blocks],
            "policy": self.policy,
            "redactions": dict(self.redactions),
            "worktree": dict(self.worktree) if self.worktree is not None else None,
        }


def _get_tools(res: HeadlessResult) -> list[dict]:
    """The tool outputs, as before the report: [{name, output}]. A fresh
    list each time: add calls to `tool_calls`."""
    return [{"name": c["name"], "output": c.get("output") or ""} for c in res.tool_calls]


def _set_tools(res: HeadlessResult, tools: list[dict]) -> None:
    """Replace `tool_calls` with tool outputs in the old shape."""
    res.tool_calls = [
        {"name": str(t.get("name", "")), "args": None, "status": _call_status(str(out)),
         "duration_ms": None, "output": str(out)}
        for t in tools for out in [t.get("output") or ""]
    ]


# Set on the class after @dataclass made `tools` an InitVar (a constructor
# keyword), so `HeadlessResult(tools=[...])`, `res.tools` and
# `res.tools = [...]` work as they did before the report.
HeadlessResult.tools = property(_get_tools, _set_tools, doc=_get_tools.__doc__)  # type: ignore[assignment]


def _text_or_none(value: Any) -> str | None:
    return str(value) if value not in (None, "") else None


def run_identity(agent: Any) -> dict[str, str | None]:
    """The session, backend, model and working folder `agent` runs with."""
    llm = getattr(getattr(agent, "config", None), "llm", None)
    try:
        cwd = str(getattr(agent, "cwd", "") or os.getcwd())
    except OSError:  # the process's folder is gone
        cwd = None
    return {
        "session_id": _text_or_none(getattr(agent, "session_id", None)),
        "backend": _text_or_none(getattr(llm, "backend", None)),
        "model": _text_or_none(getattr(llm, "model", None)),
        "cwd": cwd,
    }


def start_payload(agent: Any, max_turns: int, *, resumed: bool = False) -> dict[str, Any]:
    """The RunStart event a stream-json run opens with."""
    return {"kind": "RunStart", **run_identity(agent), "max_turns": max_turns,
            "policy": policy_label(agent), "resumed": resumed}


def result_payload(res: HeadlessResult) -> dict[str, Any]:
    """The RunResult event a stream-json run ends with: the report."""
    return {"kind": "RunResult", **res.to_dict()}


def stopped_result(agent: Any, stop_reason: str, *, error: str | None = None,
                   max_turns: int | None = None) -> HeadlessResult:
    """The report of a run that stopped outside its turn (a Ctrl+C or a
    failure while the agent was built, say). It names the agent's session,
    backend, model and policy when there is an agent, as its RunStart did;
    `error` is redacted."""
    from evi.redact import Redactor

    res = HeadlessResult(stop_reason=stop_reason, max_turns=max_turns)
    if agent is not None:
        for key, value in run_identity(agent).items():
            setattr(res, key, value)
        res.policy = policy_label(agent)
        res.transcript_path = _transcript_path(agent, res.session_id)
    if error is not None:
        red = Redactor()
        res.error = red(error)
        res.redactions = dict(red.counts)
    return res


def _call_status(output: str) -> str:
    """A tool call's status, from the result the agent gave it."""
    if is_permission_denial(output):
        return "denied"
    if output.startswith(CANCELLED):
        return "cancelled"
    if output.startswith("BLOCKED BY HOOK"):
        return "blocked"
    if output.startswith("ERROR"):
        return "error"
    return "ok"


class _Rounds:
    """Counts the model round trips (turns) a run used, from its events: a
    round's reply starts with text, usage or a tool call, and the next one
    can only start after its tool results. A round that showed nothing (an
    empty reply, a request that failed) is counted when the turn ends on it."""

    def __init__(self) -> None:
        self.count = 0
        self._open = False

    def see(self, event: Any) -> None:
        if isinstance(event, (TextDelta, ThinkingDelta, ToolCall, UsageStats)):
            if not self._open:
                self.count += 1
                self._open = True
        elif isinstance(event, (ToolResult, StopBlocked, ChecksFailed)):
            # The verify gate sent the model back: its next reply is a round.
            self._open = False

    def end(self, done_reason: str | None, failed: bool) -> None:
        if not self._open and (failed or (done_reason and stop_reason_of(done_reason) == "done")):
            self.count += 1


class _SigintCancels:
    """While installed, SIGINT cancels `agent`'s turn instead of raising
    KeyboardInterrupt; a second SIGINT raises it (to stop waiting). Only on
    the main thread, where Python runs signal handlers: elsewhere (a web
    request's thread, a batch worker) SIGINT is left as it is. The handler
    only sets the agent's cancel token, which is safe from a signal handler
    on the turn's own thread (Agent.cancel)."""

    def __init__(self, agent) -> None:
        self.agent = agent
        self.pressed = False
        self._previous: object = None
        self._installed = False

    def __enter__(self) -> "_SigintCancels":
        try:
            self._previous = signal.signal(signal.SIGINT, self._on_sigint)
            self._installed = True
        except ValueError:  # not the main thread
            pass
        return self

    def __exit__(self, *_exc: object) -> None:
        if self._installed:
            previous = signal.default_int_handler if self._previous is None else self._previous
            signal.signal(signal.SIGINT, previous)
            self._installed = False

    def cancel(self) -> bool:
        """Cancel the turn; False when the agent can't be cancelled."""
        cancel = getattr(self.agent, "cancel", None)
        if not callable(cancel):
            return False
        cancel(SIGINT_REASON)
        return True

    def _on_sigint(self, _signum: int, _frame: object) -> None:
        if self.pressed or not self.cancel():
            raise KeyboardInterrupt
        self.pressed = True


def run_headless(
    agent,
    prompt: str,
    max_turns: int = 12,
    response_format: dict | None = None,
    *,
    cancel_on_sigint: bool = False,
    on_event: Callable[[Any], None] | None = None,
    track_files: str | os.PathLike[str] | None = None,
    report_errors: bool = False,
) -> HeadlessResult:
    """Run one prompt through `agent` to completion and collect the report.

    `response_format` (e.g. a json_schema wrapper) is forwarded to the backend
    for Structured Outputs when the agent supports it. `cancel_on_sigint`: a
    Ctrl+C cancels the turn and the result says so (see the module doc).
    `on_event` is called with every event of the turn as it arrives (what
    `evi run --format stream-json` prints). `track_files`: the folder whose
    changes the report lists as ``files_changed`` (`ChangeTracker`; eVi's
    home and the agent's transcripts are not the run's changes).
    `report_errors`: an exception the turn raises comes back as the report's
    ``error`` (stop_reason "error", with what the turn did before it)
    instead of propagating.
    """
    from evi import config as _config
    from evi.redact import Redactor

    started = time.monotonic()
    parts: list[str] = []
    res = HeadlessResult(policy=policy_label(agent), max_turns=max_turns, **run_identity(agent))
    tracker: ChangeTracker | None = None
    chat_kwargs = {"max_turns": max_turns}
    if response_format is not None:
        chat_kwargs["response_format"] = response_format
    red = Redactor()
    rounds = _Rounds()
    waiting: list[tuple[dict, float]] = []  # calls with no result yet, in call order
    done_reason: str | None = None
    guard = _SigintCancels(agent) if cancel_on_sigint else contextlib.nullcontext()
    events = None
    second_sigint = False
    try:
        with guard:
            try:
                if track_files is not None:
                    # Under the guard: a Ctrl+C meanwhile cancels the turn
                    # about to start.
                    tracker = ChangeTracker.start(track_files, exclude=(
                        _config.HOME, getattr(getattr(agent, "transcripts", None), "root", None),
                    ))
                events = agent.chat(prompt, **chat_kwargs)
                # A Ctrl+C that came before the turn existed went to no turn.
                if cancel_on_sigint and guard.pressed:
                    guard.cancel()
                for event in events:
                    if on_event is not None:
                        on_event(event)
                    rounds.see(event)
                    if isinstance(event, TextDelta):
                        parts.append(event.text)
                    elif isinstance(event, ToolCall):
                        record = {"name": red(event.name), "args": _shown(red.args(event.arguments)),
                                  "status": "cancelled", "duration_ms": None, "output": None}
                        res.tool_calls.append(record)
                        waiting.append((record, time.monotonic()))
                    elif isinstance(event, ToolResult):
                        _settle(res, waiting, event, red)
                    elif isinstance(event, UsageStats):
                        _add_usage(res, event)
                    elif isinstance(event, CheckResult):
                        res.checks.append({
                            "name": event.name, "cmd": red(event.cmd), "exit": event.exit,
                            "duration_ms": max(0, int(event.duration_ms or 0)),
                            "tail": red(event.tail), "round": max(1, int(event.round or 1)),
                            "source": event.source,
                        })
                    elif isinstance(event, ChecksSkipped):
                        res.checks_skipped = red(event.reason)
                    elif isinstance(event, StopBlocked):
                        res.stop_blocks.append({
                            "hook": red(event.hook), "reason": red(event.reason),
                            "iteration": max(1, int(event.iteration or 1)),
                        })
                        # The verify gate refused that answer: the run's text
                        # is the one the model gives next (one JSON object
                        # for --schema, not two run together).
                        parts.clear()
                    elif isinstance(event, ChecksFailed):
                        parts.clear()
                    elif isinstance(event, Error):
                        res.error = red(str(event.message))
                    elif isinstance(event, Done):
                        done_reason = event.reason
                        break
            finally:
                # Still under the guard: a turn left mid-tools stops its calls
                # (Agent._run_calls), and a Ctrl+C meanwhile is handled below.
                close = getattr(events, "close", None)
                if callable(close):
                    with contextlib.suppress(Exception):
                        close()
    except KeyboardInterrupt:
        if not cancel_on_sigint:
            raise
        # A second Ctrl+C: stop waiting for the turn to wind down.
        second_sigint = True
    except Exception as exc:
        if not report_errors:
            raise
        res.error = red(f"{type(exc).__name__}: {exc}")
    res.text = "".join(parts).strip()
    res.done_reason = done_reason
    rounds.end(done_reason, failed=res.error is not None)
    res.turns_used = rounds.count
    res.stop_reason = _stop_reason(res, done_reason, second_sigint)
    res.transcript_path = _transcript_path(agent, res.session_id)
    if tracker is not None:
        try:
            res.files_changed = tracker.changes()
        except KeyboardInterrupt:
            # The turn is over; a Ctrl+C now skips the listing, not the report.
            if not cancel_on_sigint:
                raise
            res.files_changed = None
    res.redactions = dict(red.counts)
    res.duration_s = round(time.monotonic() - started, 3)
    return res


def _shown(text: Any) -> str:
    """A redacted argument or output as a report keeps it: cut to MAX_SHOWN."""
    return (text if isinstance(text, str) else str(text))[:MAX_SHOWN]


def _settle(res: HeadlessResult, waiting: list[tuple[dict, float]], event: ToolResult,
            red: Any) -> None:
    """Fill in the call `event` answers: results come in call order, so the
    first call of that name still waiting (a result with no call, from an
    agent that reports none, gets a record of its own)."""
    output = event.output if isinstance(event.output, str) else str(event.output)
    name = red(event.name)  # the model chose it
    for i, (record, began) in enumerate(waiting):
        if record["name"] == name:
            del waiting[i]
            record["duration_ms"] = max(0, int((time.monotonic() - began) * 1000))
            break
    else:
        record = {"name": name, "args": None, "duration_ms": None}
        res.tool_calls.append(record)
    record["status"] = _call_status(output)
    record["output"] = _shown(red(output))
    if record["status"] == "denied":
        record["reason"] = _denial_reason(red(output))
        if name not in res.denied_tools:
            res.denied_tools.append(name)


def _denial_reason(output: str) -> str:
    """Why a call was denied: its (redacted) result's text after the denial
    prefix, its first line, cut to MAX_REASON characters."""
    text = output
    for lead in _DENIAL_LEADS:
        if text.startswith(lead):
            text = text[len(lead):]
            break
    first = text.strip().splitlines()[0] if text.strip() else ""
    return first[:MAX_REASON]


def _add_usage(res: HeadlessResult, event: UsageStats) -> None:
    usage = res.usage or {"prompt": 0, "completion": 0, "total": 0}
    usage["prompt"] += int(event.prompt_tokens or 0)
    usage["completion"] += int(event.completion_tokens or 0)
    usage["total"] += int(event.total_tokens or 0)
    res.usage = usage


def _stop_reason(res: HeadlessResult, done_reason: str | None, second_sigint: bool) -> str:
    """Why the run stopped (see the module doc): a cancel first, then a
    backend error, then the turn's own reason. Denied calls in a run the
    model finished leave it "done" (they are in denied_tools)."""
    reason = stop_reason_of(done_reason) if done_reason is not None else None
    if second_sigint or reason == "interrupted":
        return "interrupted"
    if res.error is not None:
        return "error"
    return reason or "done"


def _transcript_path(agent: Any, session_id: str | None) -> str | None:
    """The file the run's session was logged to, when it was logged."""
    root = getattr(getattr(agent, "transcripts", None), "root", None)
    if not session_id or root is None:
        return None
    try:
        from evi.sessions import find_session

        path = find_session(session_id, root=Path(root))
    except (OSError, TypeError, ValueError):
        return None
    return str(path) if path is not None else None


def to_json(res: HeadlessResult) -> str:
    """The run report as one line of JSON (`evi run --format json`)."""
    return json.dumps(res.to_dict())


# ---- files changed --------------------------------------------------------------


def _line_count(path: Path) -> int | None:
    """Lines in a new file; None for a binary or very large one."""
    try:
        if path.stat().st_size > 8 * 1024 * 1024:
            return None
        data = path.read_bytes()
    except OSError:
        return None
    if b"\0" in data:
        return None
    if not data:
        return 0
    return data.count(b"\n") + (0 if data.endswith(b"\n") else 1)


def _digest(path: Path) -> str | None:
    """A fingerprint of a file: its content's hash, or for a file over 1 MB
    its size and modification time; None when there is no file."""
    try:
        if path.is_dir():
            return "dir"
        st = path.stat()
        if st.st_size > 1024 * 1024:
            return f"big:{st.st_size}:{st.st_mtime_ns}"
        return hashlib.sha1(path.read_bytes(), usedforsecurity=False).hexdigest()
    except FileNotFoundError:
        return None
    except OSError:
        return "unreadable"


def _own_outputs() -> frozenset[tuple[int, int]]:
    """(st_dev, st_ino) of the regular files this process's stdout and
    stderr write to: ``evi run --format stream-json ... > out.txt`` writes
    the report there, which is not a change the run made."""
    found = set()
    for fd in (1, 2):
        try:
            st = os.fstat(fd)
        except (OSError, ValueError):
            continue
        if stat.S_ISREG(st.st_mode) and st.st_ino:
            found.add((st.st_dev, st.st_ino))
    return frozenset(found)


class ChangeTracker:
    """What a run changed under its working folder (`root`).

    Inside a git work tree it compares `git status` before and after the run
    (the files dirty before are fingerprinted, so only what the run did
    shows), plus what commits made during the run touched, and counts lines
    with `git diff --numstat` against the commit checked out when the run
    started (a new untracked file: its lines). git runs hardened
    (`evi.gitsafe.run_git`: no repository hooks, filters or fsmonitor).
    Elsewhere it compares the size and modification time of the files under
    `root` (skipping .git, node_modules, virtualenvs and caches), with lines
    only for new files. So does a folder git ignores (a new file in it would
    be ignored: a scratch folder inside a repo, a home directory that is a
    dotfiles repo ignoring ``*``), where `git status` would see nothing; in
    a work tree, a new file that a .gitignore pattern matches is not listed.
    Paths are relative to `root`, with `/`.

    What eVi itself writes is not the run's change: the folders in
    `exclude` (eVi's home, the transcripts) when they are inside `root`, and
    the files this process's stdout and stderr go to (``evi run ... >
    out.txt``).

    `changes()` is None when nothing could be compared: git failed or
    reported more than MAX_ENTRIES dirty files, or the folder outside git
    holds more than WALK_MAX_FILES files or took longer than WALK_SECONDS
    to walk (a home directory, say)."""

    MAX_ENTRIES = 20_000
    WALK_MAX_FILES = 5_000
    WALK_SECONDS = 1.0
    SKIP_DIRS = frozenset({
        ".git", "node_modules", ".venv", "venv", "__pycache__", ".mypy_cache",
        ".pytest_cache", ".ruff_cache", ".tox",
    })
    GIT_TIMEOUT = 60.0
    # A name no .gitignore lists on its own: if it is ignored right in
    # `root`, so is any new file there.
    IGNORE_PROBE = "evi-files-changed-probe"

    def __init__(self, root: str | os.PathLike[str],
                 exclude: tuple[str | os.PathLike[str] | None, ...] = ()) -> None:
        self.root = Path(root).resolve()
        self.top: Path | None = None  # the git work tree's top, inside one
        self.head: str | None = None  # the commit checked out at the start
        self.before: dict[str, Any] | None = None
        self.exclude = self._inside_root(exclude)
        self.outputs = _own_outputs()

    @classmethod
    def start(cls, root: str | os.PathLike[str],
              exclude: tuple[str | os.PathLike[str] | None, ...] = ()) -> "ChangeTracker":
        """Snapshot `root` before the run."""
        tracker = cls(root, exclude)
        try:
            tracker.top = tracker._work_tree_top()
            if tracker.top is not None and tracker._ignored():
                tracker.top = None  # git sees nothing new here: walk it
            if tracker.top is not None:
                tracker.head = tracker._head()
            tracker.before = tracker._state()
        except Exception:  # noqa: BLE001 — a report without files_changed beats no run
            tracker.before = None
        return tracker

    def changes(self) -> list[dict] | None:
        """The files changed since `start`: [{path, status, added, deleted}]."""
        if self.before is None:
            return None
        try:
            after = self._state()
            if after is None:
                return None
            return self._compare(self.before, after)
        except Exception:  # noqa: BLE001
            return None

    # -- what eVi writes --

    def _inside_root(self, paths: tuple[str | os.PathLike[str] | None, ...]) -> tuple[Path, ...]:
        """The folders of `paths` strictly inside `root` (one that holds
        `root` would hide everything)."""
        out = []
        for p in paths:
            if p is None:
                continue
            try:
                folder = Path(p).resolve()
            except (OSError, RuntimeError, TypeError, ValueError):
                continue
            if folder != self.root and folder.is_relative_to(self.root):
                out.append(folder)
        return tuple(out)

    def _evis_own(self, path: Path) -> bool:
        """`path` is eVi's own writing: under an excluded folder, or the file
        stdout / stderr goes to."""
        if any(path.is_relative_to(folder) for folder in self.exclude):
            return True
        if self.outputs:
            try:
                st = path.stat()
            except OSError:
                return False
            return (st.st_dev, st.st_ino) in self.outputs
        return False

    # -- git --

    def _git(self, *args: str) -> str | None:
        from evi import gitsafe

        proc = gitsafe.run_git(
            ["--no-optional-locks", "--literal-pathspecs", *args],
            cwd=self.top or self.root, timeout=self.GIT_TIMEOUT,
        )
        return proc.stdout if proc.returncode == 0 else None

    def _work_tree_top(self) -> Path | None:
        """The top of the git work tree `root` is in; None outside git.
        Raises when git can't say (or the work tree isn't the repository's
        own, gitsafe.is_own_work_tree): nothing is compared then."""
        from evi import gitsafe
        from evi.project import git_toplevel

        if git_toplevel(self.root) is None:
            return None
        proc = gitsafe.run_git(
            ["rev-parse", "--is-inside-work-tree", "--show-prefix", "--show-toplevel"],
            cwd=self.root, timeout=self.GIT_TIMEOUT,
        )
        lines = proc.stdout.splitlines() if proc.returncode == 0 else []
        if len(lines) < 3 or lines[0].strip() != "true" or not gitsafe.is_own_work_tree(
            self.root, lines[1]
        ):
            raise RuntimeError(f"git can't describe the work tree at {self.root}")
        return Path(lines[2].strip()).resolve()

    def _head(self) -> str | None:
        out = self._git("rev-parse", "--verify", "--quiet", "HEAD")  # None: no commit yet
        return (out or "").strip() or None

    def _ignored(self) -> bool:
        """A new file right in `root` would be ignored by git (root, or a
        folder above it, is ignored)."""
        from evi import gitsafe

        probe = Path(os.path.relpath(self.root / self.IGNORE_PROBE, self.top)).as_posix()
        # Not through _git: check-ignore refuses --literal-pathspecs.
        proc = gitsafe.run_git(["--no-optional-locks", "check-ignore", "-q", "--", probe],
                               cwd=self.top, timeout=self.GIT_TIMEOUT)
        return proc.returncode == 0

    def _state(self) -> dict[str, Any] | None:
        return self._git_state() if self.top is not None else self._walk_state()

    def _git_state(self) -> dict[str, Any] | None:
        """{path: (status, fingerprint)} of the files git reports dirty."""
        out = self._git("status", "--porcelain=v1", "-z", "--untracked-files=all",
                        "--no-renames")
        if out is None:
            return None
        fields = [f for f in out.split("\0") if f]
        if len(fields) > self.MAX_ENTRIES:
            return None
        state: dict[str, Any] = {}
        skip = False
        for entry in fields:
            if skip:  # the original path of a rename / copy
                skip = False
                continue
            xy, path = entry[:2], entry[3:]
            skip = xy[0] in "RC"
            state[path] = (xy, _digest(self.top / path))
        return state

    def _walk_state(self) -> dict[str, Any] | None:
        """{path: (size, mtime)} of every file under root."""
        state: dict[str, Any] = {}
        deadline = time.monotonic() + self.WALK_SECONDS
        for dirpath, dirnames, filenames in os.walk(self.root):
            dirnames[:] = [d for d in dirnames if d not in self.SKIP_DIRS
                           and Path(dirpath, d) not in self.exclude]
            for name in filenames:
                if len(state) >= self.WALK_MAX_FILES or time.monotonic() > deadline:
                    return None
                path = Path(dirpath, name)
                try:
                    st = path.lstat()
                except OSError:
                    continue
                state[path.relative_to(self.root).as_posix()] = (st.st_size, st.st_mtime_ns)
        return state

    def _compare(self, before: dict[str, Any], after: dict[str, Any]) -> list[dict]:
        base = self.top if self.top is not None else self.root
        paths = {p for p in before.keys() | after.keys() if before.get(p) != after.get(p)}
        numstat: dict[str, tuple[int | None, int | None]] = {}
        tracked_before: set[str] = set()
        if self.top is not None:
            head_now = self._head()
            if head_now and head_now != self.head:  # the run made commits
                if self.head:
                    out = self._git("diff", "--name-only", "-z", "--no-renames",
                                    self.head, head_now)
                else:
                    out = self._git("ls-tree", "-r", "-z", "--name-only", head_now)
                paths |= {p for p in (out or "").split("\0") if p}
        paths = {p for p in paths if not self._evis_own(base / p)}
        if self.top is not None:
            if self.head and paths:
                tracked_before = {
                    p for p in (self._git("ls-tree", "-r", "-z", "--name-only", self.head)
                                or "").split("\0") if p
                }
                numstat = self._numstat()
        out_rows: list[dict] = []
        for path in sorted(paths):
            if self.top is not None:
                existed = (before[path][1] is not None) if path in before else (
                    path in tracked_before)
            else:
                existed = path in before
            exists = os.path.lexists(base / path)
            if not existed and not exists:
                continue  # made and removed again during the run
            status = "modified" if existed and exists else ("added" if exists else "deleted")
            added, deleted = numstat.get(path, (None, None))
            if path not in numstat and status == "added":
                added, deleted = _line_count(base / path), 0
            out_rows.append({"path": self._relative(base / path), "status": status,
                             "added": added, "deleted": deleted})
        return out_rows

    def _numstat(self) -> dict[str, tuple[int | None, int | None]]:
        """{path: (added, deleted)} of the work tree against the start commit."""
        out = self._git("diff", "--numstat", "-z", "--no-renames", self.head)
        stats: dict[str, tuple[int | None, int | None]] = {}
        for entry in (out or "").split("\0"):
            bits = entry.split("\t", 2)
            if len(bits) != 3 or not bits[2]:
                continue
            added, deleted, path = bits
            stats[path] = (int(added) if added.isdigit() else None,
                           int(deleted) if deleted.isdigit() else None)
        return stats

    def _relative(self, path: Path) -> str:
        try:
            return Path(os.path.relpath(path, self.root)).as_posix()
        except ValueError:  # another drive
            return path.as_posix()
