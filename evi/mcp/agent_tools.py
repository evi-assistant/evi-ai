"""The director's handoff API: the opt-in ``agent`` category of `evi mcp serve`.

The director (the MCP client that spawned ``evi mcp serve --categories
...,agent`` over stdio, or holds the bearer token of ``--http --token``)
files tasks in the repository's queue (`evi.tasks`), starts them, follows a
run, steers or cancels it and collects its report, through a stable API
instead of a chat (docs/roadmap.md, Phase 1, loop-task-queue). The category
is never in `evi.mcp.publish.DEFAULT_CATEGORIES`: only an explicit
``--categories ...,agent`` serves it, and over HTTP only with a token, on
loopback too (`evi.mcp.publish` refuses otherwise).

These handlers are the MCP server's alone. They are never in
`evi.tools.base.REGISTRY`, so no in-process agent (``evi chat``, the web,
a task's own run) has them in its tool list: an agent can't dispatch tasks
to itself. This module is the only MCP tool that steers a turn
(``run_steer``).

Tools (`TOOL_NAMES`; every result is JSON text, a failure ``ERROR: ...``)
    task_create(title, why?, acceptance[]?, files_in_scope[]?,
    required_checks[]?, implementer?, id?) -> the task as filed (open).
    task_list(status?) -> [{id, title, status, label, ...}] and
    task_show(id) -> {task, label, path}: ``label`` is the status as the
    director's attestation has it (`status_label`, as ``evi task list|show``
    shows it). This server runs as an agent's process (``EVI_AGENT=1``), so
    it never reads the director key, and a signed verdict reads ``unverified
    (...)`` here: the director checks one from their own shell.
    task_start(id, backend?, model?, max_turns?, base?) -> {run_id, state,
    position} at once: the run is queued for the worker (below).
    run_status(run_id) -> {state, task_id, timing, turns_used, steering,
    the last few events summarised and redacted, and stop_reason, exit_code
    and task_status once it ended}.
    run_steer(run_id, text) -> the text, framed as a note from the director,
    joins the running agent's steering queue (`Agent.steer`, applied at the
    next step of its turn; a note sent before the agent exists waits for
    it). run_cancel(run_id, reason?) -> a queued run is dropped (the task
    untouched); a running one is cancelled (`Agent.cancel` for its turn, and
    `evi.tasks.run_task`'s cancel token for the required checks it runs
    after the turn) and ends with stop_reason "interrupted", the task back
    to open with the attempt counted; a cancel that comes once the outcome
    is being recorded leaves it as it is (the run's note says so) and stops
    the evidence bundle's own run of the required checks.
    run_result(run_id) -> the v1 run report (`evi.headless`, with
    `evi.tasks.run_task`'s ``task`` section) and the task's status after it.
    A lone surrogate in the model's text goes out as its \\uXXXX escape, so
    a result is always writable (still JSON, read back as the same string).

    No tool verifies, rejects, signs or reopens a task, or writes a status
    but the ones `evi.tasks.run_task` itself writes (claimed, then
    implemented or back to open): only ``evi task verify|reject`` from the
    director's own shell does (`evi.attest`), and `evi.attest.authorize`
    refuses in this process anyway.

The worker (`AgentRuns`)
    One background thread per server runs the starts one at a time (one
    GPU), first in first out, through `evi.tasks.run_task`, the function
    ``evi task start`` runs: the same claim, worktree, implementer policy,
    verify gate, required checks, run report, evidence bundle
    (`evi.evidence.collect_task`, as ``evi task start`` collects it,
    whatever the run's stop_reason: a run that stopped before its checks, an
    error or a cancel, gets none run again, and a cancel, the limit or
    closing the server stops the bundle's checks too) and task bookkeeping. The agent is built by the caller's factory (`evi
    mcp serve` passes the CLI's own, as ``evi task start`` builds it). At
    most ``queue_limit`` (16) runs wait; a start past that is refused. Each
    run is an in-memory record under an opaque run_id (its state, a ring of
    recent events, the agent for steer and cancel); a finished one is kept
    for a day, the newest ``keep_finished`` (32) of them. A run that fails
    in an unexpected way ends as an error; the worker goes on with the next.

    A run's claim is a short lease (``lease_minutes``, 10) that a heartbeat
    renews every 2 minutes once the run's agent is built (before that,
    `evi.tasks.run_task` may still put the task back as it was), so a long
    run is never taken over, and the claim of a server that died runs out
    soon and can be reclaimed (`evi.tasks.claim`). A run that goes past
    ``[tasks] max_run_minutes`` (default 60) is cancelled, as run_cancel
    cancels it. Closing the server (`AgentRuns.close`) cancels the running
    run, waits a moment for it to record its outcome, and drops the queued
    ones.

    While a run goes on, the server process's folder is the run's worktree,
    as ``evi task start``'s is (a ``monitor`` command runs in the process's
    folder, and the agent is built there); the queue the tools work on is
    always the repository the server was started for (`AgentRuns.root`).
    That is why `evi.mcp.publish` serves beside this category only the
    categories whose tools never work in the process's folder
    (`evi.mcp.publish.AGENT_COMPANIONS`).
"""

from __future__ import annotations

import collections
import contextlib
import datetime as _dt
import json
import os
import secrets
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping

from evi import tasks

CATEGORY = "agent"
TOOL_NAMES = (
    "task_create", "task_list", "task_show", "task_start",
    "run_status", "run_steer", "run_cancel", "run_result",
)
RUN_STATES = ("queued", "running", "done", "cancelled", "error")
# Runs that may wait behind the running one.
QUEUE_LIMIT = 16
# Finished runs kept for run_status / run_result: the newest this many, for
# at most KEEP_SECONDS after they ended.
KEEP_FINISHED = 32
KEEP_SECONDS = 24 * 3600
# A run's claim on its task: this many minutes, renewed every
# HEARTBEAT_SECONDS while the run is alive.
LEASE_MINUTES = 10
HEARTBEAT_SECONDS = 120
# The [tasks] max_run_minutes a run gets when the setting is missing or bad.
DEFAULT_MAX_RUN_MINUTES = 60
# How often a run's monitor wakes: to apply a cancel, check the deadline and
# renew the lease when due.
TICK_SECONDS = 0.25
# Events a run keeps, and how many run_status shows.
EVENTS_KEPT = 50
STATUS_EVENTS = 10
SUMMARY_CHARS = 200
# How long `close` waits for the running run to wind down and record itself.
CLOSE_TIMEOUT = 15.0
# max_turns a task_start may ask for (the CLI's default is 30).
MAX_TURNS_LIMIT = 200
# The model's text a run keeps for its summary while it streams (cut at a
# space, so no secret is cut in two before it is redacted).
_TEXT_KEPT = 8000
# How a run_steer note reaches the model: a user message in the turn.
STEER_FRAME = ("Note from the director, sent while you were working (take it into account "
               "and carry on):\n")

# make_agent_for(backend, model) -> make_agent(worktree) -> Agent. The outer
# call raises evi.tasks.TaskError for a backend it can't use (an unknown
# name), so task_start refuses at once.
AgentFactory = Callable[[str, str], Callable[[Any], Any]]


class AgentToolError(RuntimeError):
    """A request an agent-category tool refuses (its result is ``ERROR: ...``)."""


def _iso(when: float | None) -> str | None:
    if when is None:
        return None
    return _dt.datetime.fromtimestamp(when, _dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[
        :-3] + "Z"


def status_label(verifier: Any, record: Mapping[str, Any], root: str | os.PathLike[str]) -> str:
    """The task's status as ``evi task list|show`` shows it (`evi.attest`'s
    status_label): a verdict only with a valid attestation at its branch's
    current head (None when the branch is gone; the branch itself, never a
    tag of its name) and over its evidence as it is now, read only where
    `evi.tasks.evidence_path` allows (the task file is tracked)."""
    from evi import attest

    head = None
    evidence = ""
    if attest.claims_verdict(tasks.status_of(record)) and tasks.ATTESTATION in record:
        branch = str(record.get("branch") or "").strip()
        if branch:
            try:
                head = attest.branch_head(tasks.head_ref(branch), cwd=root)
            except attest.AttestError:
                head = None  # merged and deleted, or never made
        try:
            evidence = attest.evidence_hash(tasks.evidence_path(record, root))
        except tasks.TaskError:
            evidence = "refused"  # matches no signed hash: reads as changed
    return verifier.status_label(record, evidence_hash=evidence, head=head)


# ---- one run -------------------------------------------------------------------------


@dataclass
class _Run:
    run_id: str
    task_id: str
    backend: str
    model: str
    base: str
    max_turns: int
    make_agent: Callable[[Any], Any]
    state: str = "queued"
    queued_at: float = field(default_factory=time.time)
    started_at: float | None = None
    ended_at: float | None = None
    started_mono: float | None = None
    built_mono: float | None = None  # when its agent was built: the heartbeat starts then
    # Set with cancel_reason: run_task stops the required checks it runs after the turn.
    cancelled: threading.Event = field(default_factory=threading.Event)
    agent: Any = None
    events: collections.deque = field(
        default_factory=lambda: collections.deque(maxlen=EVENTS_KEPT))
    text: str = ""  # the model's text since the last other event (summarised)
    rounds: Any = None  # evi.headless._Rounds: the turns used so far
    pending_steer: list[str] = field(default_factory=list)
    steering_sent: int = 0
    steering_applied: int = 0
    steering_unapplied: int = 0
    cancel_reason: str = ""
    limit_text: str = ""
    stop_reason: str | None = None
    exit_code: int | None = None
    implemented: bool = False
    turns_used: int | None = None
    error: str = ""
    note: str = ""
    recorded: bool = True
    report: dict | None = None
    report_path: str = ""
    task_status: str = ""

    @property
    def finished(self) -> bool:
        return self.state in ("done", "cancelled", "error")


def _summarise(event: Any) -> tuple[str, str] | None:
    """(kind, one-line summary) of an agent event for run_status, redacted
    (`evi.events.event_payload` redacts tool calls, results and errors; the
    rest goes through `evi.redact` here). None for an event not worth a line
    (thinking, usage, progress); text is summarised by the caller."""
    from evi.events import event_kind, event_payload
    from evi.redact import Redactor

    kind = event_kind(event)
    if kind in ("TextDelta", "ThinkingDelta", "UsageStats", "ToolProgress", "LogProbs",
                "RouteInfo"):
        return None
    payload = event_payload(event)
    red = Redactor()
    if kind == "ToolCall":
        text = f"{payload.get('name')} {payload.get('arguments') or ''}"
    elif kind == "ToolResult":
        text = f"{payload.get('name')}: {payload.get('output') or ''}"
    elif kind == "Steering":
        text = ("applied: " if payload.get("applied", True) else "not applied: ") + red(
            str(payload.get("text") or ""))
    elif kind == "CheckResult":
        status = "passed" if payload.get("exit") == 0 else (
            f"exit {payload.get('exit')}" if payload.get("exit") is not None else "did not finish")
        cmd = red(str(payload.get("cmd") or ""))
        text = f"{payload.get('name')} (round {payload.get('round')}) {status}: {cmd}"
    elif kind == "ChecksFailed":
        text = (f"round {payload.get('round')} of {payload.get('max_rounds')}: "
                + ", ".join(map(str, payload.get("failed") or [])))
    elif kind == "ChecksSkipped":
        text = red(str(payload.get("reason") or ""))
    elif kind == "StopBlocked":
        text = f"{payload.get('hook')}: {red(str(payload.get('reason') or ''))}"
    elif kind == "FilesChanged":
        text = red(", ".join(map(str, payload.get("paths") or [])))
    elif kind == "Guardrail":
        text = red(str(payload.get("message") or ""))
    elif kind == "Done":
        text = str(payload.get("reason") or "")
    elif kind == "Error":
        text = str(payload.get("message") or "")
    else:
        text = ""
    return kind, _line(text)


def _line(text: str) -> str:
    """`text` as one printable line of at most SUMMARY_CHARS."""
    flat = " ".join(tasks.printable(text).split())
    return flat if len(flat) <= SUMMARY_CHARS else flat[:SUMMARY_CHARS - 3] + "..."


def _text_summary(text: str) -> str:
    """The tail of the model's text so far, redacted before it is cut."""
    from evi.redact import redact

    flat = " ".join(tasks.printable(redact(text)).split())
    return flat if len(flat) <= SUMMARY_CHARS else "..." + flat[-(SUMMARY_CHARS - 3):]


# ---- the registry and its worker ------------------------------------------------------


class AgentRuns:
    """The runs one MCP server started for the repository whose main
    checkout is `root` (see the module doc). `make_agent_for` builds each
    run's agent (`AgentFactory`)."""

    def __init__(self, root: str | os.PathLike[str], make_agent_for: AgentFactory) -> None:
        self.root = Path(root)
        self.make_agent_for = make_agent_for
        self.holder = f"{tasks.default_holder()} (evi mcp serve, pid {os.getpid()})"
        self.queue_limit = QUEUE_LIMIT
        self.keep_finished = KEEP_FINISHED
        self.keep_seconds = KEEP_SECONDS
        self.lease_minutes = LEASE_MINUTES
        self.heartbeat_seconds: float = HEARTBEAT_SECONDS
        self.tick: float = TICK_SECONDS
        # Seconds a run may take; None: [tasks] max_run_minutes at its start.
        self.max_run_seconds: float | None = None
        self._cond = threading.Condition(threading.RLock())
        self._runs: dict[str, _Run] = {}
        self._queue: collections.deque[_Run] = collections.deque()
        self._current: _Run | None = None
        self._worker: threading.Thread | None = None
        self._closing = False

    @property
    def queue(self) -> tasks.TaskQueue:
        return tasks.TaskQueue(self.root)

    # -- task tools --

    def create_task(self, args: Mapping[str, Any]) -> dict[str, Any]:
        record = tasks.new_task(
            str(args.get("id") or "").strip(),
            title=str(args.get("title") or ""),
            why=str(args.get("why") or ""),
            acceptance=args.get("acceptance") or [],
            files_in_scope=args.get("files_in_scope") or [],
            required_checks=args.get("required_checks") or [],
            implementer=str(args.get("implementer") or ""),
        )
        return self.queue.create(record)

    def list_tasks(self, status: str = "") -> dict[str, Any]:
        from evi import attest

        queue = self.queue
        records, problems = queue.all()
        verifier = attest.Verifier()  # one for the whole list
        want = status.strip()
        rows = [{
            "id": r.get("id"), "title": r.get("title") or "", "status": r.get("status") or "",
            "label": status_label(verifier, r, queue.root),
            "implementer": r.get("implementer") or "", "branch": r.get("branch") or "",
            "attempts": r.get("attempts") or 0,
        } for r in records if not want or tasks.status_of(r) == want]
        return {"tasks": rows, "problems": [{"id": i, "problem": p} for i, p in problems]}

    def show_task(self, task_id: str) -> dict[str, Any]:
        from evi import attest

        queue = self.queue
        record = queue.read(task_id)
        return {"task": record, "label": status_label(attest.Verifier(), record, queue.root),
                "path": str(queue.path(str(record["id"])))}

    # -- starting a run --

    def start(self, task_id: str, *, backend: str = "", model: str = "", max_turns: int = 30,
              base: str = "") -> dict[str, Any]:
        """Queue a run of task `task_id` (see the module doc); TaskError for
        a task that can't be started now, nothing queued."""
        from evi import trust

        task_id = tasks.validate_id(task_id)
        queue = self.queue
        record = queue.read(task_id)
        task_id = str(record["id"])
        status = tasks.status_of(record)
        lease = tasks.lease_of(record)
        if status == "claimed":
            if tasks.lease_live(record) and lease.get("holder") != self.holder:
                raise tasks.LeaseHeld(
                    f"task {task_id} is claimed by {lease.get('holder') or 'someone'} until "
                    f"{lease.get('expires_at') or '?'}; start it once that claim runs out or "
                    "is released")
        elif status != "open":
            raise tasks.IllegalTransition(
                f"task {task_id} is {status or '(no status)'!r}: only an open task (or a "
                "claimed one whose lease ran out) can be started")
        try:
            trusted = trust.status(queue.root).trusted
        except Exception:  # noqa: BLE001 — fail closed
            trusted = False
        if not trusted:
            raise tasks.TaskError(f"{queue.root} is not a trusted folder, so a task can't run "
                                  "there; the director runs `evi trust` there first")
        make_agent = self.make_agent_for(backend.strip(), model.strip())
        with self._cond:
            if self._closing:
                raise tasks.TaskError("the MCP server is shutting down")
            for other in self._runs.values():
                if not other.finished and other.task_id.casefold() == task_id.casefold():
                    raise tasks.TaskError(f"task {task_id} is already {other.state} as run "
                                          f"{other.run_id}")
            if len(self._queue) >= self.queue_limit:
                raise tasks.TaskError(f"the run queue is full ({len(self._queue)} runs wait); "
                                      "start this task once one has run")
            run = _Run(run_id=f"run-{secrets.token_hex(6)}", task_id=task_id,
                       backend=backend.strip(), model=model.strip(), base=base.strip(),
                       max_turns=max_turns, make_agent=make_agent)
            self._runs[run.run_id] = run
            self._queue.append(run)
            position = len(self._queue) - 1 + (1 if self._current is not None else 0)
            if self._worker is None or not self._worker.is_alive():
                self._worker = threading.Thread(target=self._work, name="evi-mcp-agent-worker",
                                                daemon=True)
                self._worker.start()
            self._cond.notify_all()
            return {"run_id": run.run_id, "task_id": task_id, "state": run.state,
                    "position": position}

    # -- following a run --

    def _get(self, run_id: str) -> _Run:
        with self._cond:
            self._prune()
            run = self._runs.get(str(run_id or "").strip())
        if run is None:
            raise AgentToolError(f"no run {str(run_id)[:80]!r} on this server (finished runs "
                                 "are kept for a while; a task's last report is in task_show's "
                                 "last_result)")
        return run

    def status(self, run_id: str) -> dict[str, Any]:
        run = self._get(run_id)
        with self._cond:
            now = time.time()
            started = run.started_at
            end = run.ended_at if run.ended_at is not None else now
            events = [dict(e) for e in list(run.events)[-STATUS_EVENTS:]]
            if run.text:
                events = [*events, {"kind": "Text", "summary": _text_summary(run.text)}][
                    -STATUS_EVENTS:]
            out: dict[str, Any] = {
                "run_id": run.run_id, "task_id": run.task_id, "state": run.state,
                "queued_at": _iso(run.queued_at), "started_at": _iso(started),
                "ended_at": _iso(run.ended_at),
                "elapsed_s": round(end - started, 1) if started is not None else None,
                "turns_used": (run.turns_used if run.turns_used is not None
                               else (run.rounds.count if run.rounds is not None else 0)),
                "max_turns": run.max_turns,
                "steering": {"sent": run.steering_sent, "applied": run.steering_applied,
                             "unapplied": run.steering_unapplied},
                "events": events,
            }
            if run.state == "queued":
                waiting = list(self._queue)
                out["position"] = (waiting.index(run) if run in waiting else 0) + (
                    1 if self._current is not None else 0)
            if run.cancel_reason:
                out["cancel_reason"] = run.cancel_reason
            if run.finished:
                out.update({"stop_reason": run.stop_reason, "exit_code": run.exit_code,
                            "task_status": run.task_status, "implemented": run.implemented})
                if run.error:
                    out["error"] = run.error
                if run.note:
                    out["note"] = run.note
            return out

    def steer(self, run_id: str, text: str) -> dict[str, Any]:
        run = self._get(run_id)
        body = str(text or "").strip()
        if not body:
            raise AgentToolError("run_steer needs some text")
        note = STEER_FRAME + body
        with self._cond:
            if run.finished:
                return {"queued": False, "state": run.state, "reason": "the run is over"}
            run.steering_sent += 1
            agent = run.agent
            if agent is None:
                run.pending_steer.append(note)
                return {"queued": True, "state": run.state,
                        "note": "the run's agent isn't built yet; the note waits for it"}
        queued = bool(agent.steer(note))
        pending = agent.pending_steering() if hasattr(agent, "pending_steering") else None
        return {"queued": queued, "state": run.state, "pending": pending}

    def cancel(self, run_id: str, reason: str = "") -> dict[str, Any]:
        run = self._get(run_id)
        why = str(reason or "").strip() or "the director cancelled the run"
        with self._cond:
            if run.finished:
                return {"cancelled": False, "state": run.state, "reason": "the run is over"}
            if run.state == "queued":
                with contextlib.suppress(ValueError):
                    self._queue.remove(run)
                run.cancel_reason = why
                self._end(run, "cancelled", steering_unapplied=len(run.pending_steer),
                          note="cancelled before it started; the task is as it was")
                return {"cancelled": True, "state": run.state}
            if not run.cancel_reason:
                run.cancel_reason = why
            run.cancelled.set()
            agent = run.agent
        if agent is not None:
            # Again by the run's monitor until the turn ends: a cancel before
            # the turn starts would go to no turn (Agent.cancel).
            agent.cancel(run.cancel_reason)
        return {"cancelled": True, "state": run.state,
                "note": "the run stops at its next step (in its turn, or the required check "
                        "eVi runs after it) and ends interrupted, unless its outcome is "
                        "already being recorded (then only its evidence bundle's checks stop, "
                        "and the outcome stands); poll run_status"}

    def result(self, run_id: str) -> dict[str, Any]:
        run = self._get(run_id)
        with self._cond:
            if not run.finished:
                raise AgentToolError(f"run {run.run_id} is still {run.state}; poll run_status "
                                     "until it is done")
            out = {"run_id": run.run_id, "task_id": run.task_id, "state": run.state,
                   "stop_reason": run.stop_reason, "exit_code": run.exit_code,
                   "implemented": run.implemented, "recorded": run.recorded,
                   "report_path": run.report_path or None, "report": run.report,
                   "task_status": run.task_status}
            if run.error:
                out["error"] = run.error
            if run.note:
                out["note"] = run.note
            if run.cancel_reason:
                out["cancel_reason"] = run.cancel_reason
        try:
            from evi import attest

            record = self.queue.read(run.task_id)
            out["task_status"] = tasks.status_of(record)
            out["task_label"] = status_label(attest.Verifier(), record, self.root)
        except tasks.TaskError as exc:
            out["task_label"] = f"unreadable: {exc}"
        return out

    # -- shutting down --

    def close(self, timeout: float = CLOSE_TIMEOUT) -> None:
        """Stop taking runs: the queued ones are dropped (cancelled), the
        running one is cancelled and given `timeout` seconds to record its
        outcome. Idempotent."""
        with self._cond:
            self._closing = True
            while self._queue:
                run = self._queue.popleft()
                run.cancel_reason = run.cancel_reason or "the MCP server shut down"
                self._end(run, "cancelled", steering_unapplied=len(run.pending_steer),
                          note="the MCP server shut down before it started; the task is as "
                               "it was")
            current = self._current
            agent = None
            if current is not None:
                current.cancel_reason = current.cancel_reason or "the MCP server is shutting down"
                current.cancelled.set()
                agent = current.agent
            worker = self._worker
            self._cond.notify_all()
        if agent is not None:
            with contextlib.suppress(Exception):
                agent.cancel(current.cancel_reason)
        if worker is not None and worker is not threading.current_thread():
            worker.join(timeout)

    # -- the worker --

    def _end(self, run: _Run, state: str, **fields: Any) -> None:
        """Mark `run` finished (caller holds the lock)."""
        for key, value in fields.items():
            setattr(run, key, value)
        run.state = state
        run.ended_at = time.time()
        run.agent = None  # nothing steers or cancels a finished run
        self._prune()

    def _prune(self) -> None:
        """Drop finished runs past KEEP_SECONDS, then all but the newest
        `keep_finished` (caller holds the lock)."""
        now = time.time()
        done = sorted((r for r in self._runs.values() if r.finished),
                      key=lambda r: r.ended_at or 0.0)
        for i, run in enumerate(done):
            if (now - (run.ended_at or now) > self.keep_seconds
                    or i < len(done) - self.keep_finished):
                self._runs.pop(run.run_id, None)

    def _work(self) -> None:
        while True:
            with self._cond:
                while not self._queue and not self._closing:
                    self._cond.wait()
                if not self._queue:
                    return
                run = self._queue.popleft()
                if run.state != "queued":
                    continue
                run.state = "running"
                run.started_at = time.time()
                run.started_mono = time.monotonic()
                self._current = run
            try:
                self._execute(run)
            except BaseException as exc:  # the worker outlives a run it couldn't finish
                self._fail(run, exc)
                if not isinstance(exc, Exception):
                    raise
            finally:
                with self._cond:
                    self._current = None
                    self._prune()

    def _limit(self) -> tuple[float, str]:
        """(seconds, how to say it) a run may take."""
        if self.max_run_seconds is not None:
            return float(self.max_run_seconds), f"{self.max_run_seconds:g} seconds"
        minutes = DEFAULT_MAX_RUN_MINUTES
        try:
            from evi.config import Config

            value = int(Config.load().tasks.max_run_minutes)
            if value >= 1:
                minutes = value
        except Exception:  # noqa: BLE001 — a config that can't be read gets the default
            pass
        return minutes * 60.0, f"[tasks] max_run_minutes ({minutes})"

    def _execute(self, run: _Run) -> None:
        from evi import headless

        queue = self.queue
        run.rounds = headless._Rounds()
        limit, run.limit_text = self._limit()
        base = run.base
        if not base:
            with contextlib.suppress(Exception):
                from evi.config import Config

                base = Config.load().worktree.base_ref or ""
        try:
            previous = os.getcwd()
        except OSError:  # the process's folder is gone
            previous = str(self.root)
        stop = threading.Event()
        monitor = threading.Thread(target=self._monitor, args=(run, queue, stop, limit),
                                   name=f"evi-mcp-{run.run_id}", daemon=True)
        monitor.start()
        try:
            outcome = tasks.run_task(
                queue, run.task_id, lambda wt: self._build(run, wt), holder=self.holder,
                base=base or None, max_turns=run.max_turns, ttl_minutes=self.lease_minutes,
                backend=run.backend, on_event=lambda event: self._on_event(run, event),
                evidence=lambda done: self._evidence(queue, run, done),
                cancel_token=run.cancelled,
            )
            self._record(run, outcome)
        except Exception as exc:  # noqa: BLE001 — the server outlives a run that can't go
            self._fail(run, exc)
        finally:
            stop.set()
            monitor.join(5)
            with contextlib.suppress(OSError):
                os.chdir(previous)

    def _fail(self, run: _Run, exc: BaseException) -> None:
        """`run` ended in `exc` before it was marked finished: an error, the
        task given back if this server still holds it. Never raises."""
        if run.finished:
            return
        with contextlib.suppress(Exception):
            self._release_ours(self.queue, run)
        message = str(exc) if isinstance(exc, tasks.TaskError) else (
            f"{type(exc).__name__}: {exc}")
        status = ""
        with contextlib.suppress(Exception):
            status = self._status_now(run)
        with self._cond:
            if not run.finished:
                self._end(run, "error", error=message, task_status=status)

    def _build(self, run: _Run, wt: Any) -> Any:
        """`run`'s agent for the worktree `wt`: what the factory builds there,
        with the notes steered before it existed queued on it. The worktree
        is the process's folder from here to the end of the run, as for
        ``evi task start``: its .evi.toml and EVI.md, where stdio MCP servers
        start, and where a ``monitor`` command runs."""
        if run.cancelled.is_set():
            # run_task records a stop here as the run's interruption.
            raise KeyboardInterrupt
        os.chdir(wt.path)
        agent = run.make_agent(wt)
        with self._cond:
            run.agent = agent
            run.built_mono = time.monotonic()
            pending, run.pending_steer = run.pending_steer, []
        for note in pending:
            agent.steer(note)
        return agent

    def _evidence(self, queue: tasks.TaskQueue, run: _Run,
                  done: tasks.TaskRun) -> dict[str, Any]:
        """The attempt's evidence bundle, from the run's own report, as ``evi
        task start`` collects it (`evi.evidence.collect_task`); its task
        fields. A run that stopped before its checks (an error, a cancel)
        gets none of them run again (`run_stop`, the bundle says why), and a
        cancel, the limit or closing the server stops the one running and
        starts no other (the run's cancel token), so the run records its
        outcome soon after."""
        from evi import evidence
        from evi.config import Config

        report = None
        if done.report_path is not None:
            with contextlib.suppress(OSError, ValueError):
                report = json.loads(Path(done.report_path).read_text(encoding="utf-8"))
        task_part = report.get("task") if isinstance(report, dict) else None
        attempt = task_part.get("attempt") if isinstance(task_part, dict) else None
        if not isinstance(attempt, int) or attempt < 1:
            attempt = int(done.record.get("attempts") or 0) + 1
        bundle = evidence.collect_task(queue.root, done.record, worktree=done.worktree.path,
                                       run_report=report, attempt=attempt, config=Config.load(),
                                       run_stop=done.stop_reason, cancel_token=run.cancelled)
        return evidence.record_fields(bundle)

    def _on_event(self, run: _Run, event: Any) -> None:
        try:
            line = _summarise(event)
            from evi.llm.agent import Steering, TextDelta

            with self._cond:
                if run.rounds is not None:
                    run.rounds.see(event)
                if isinstance(event, TextDelta):
                    text = run.text + str(event.text or "")
                    if len(text) > _TEXT_KEPT:
                        cut = len(text) - _TEXT_KEPT
                        space = text.find(" ", cut)
                        text = text[space + 1:] if space != -1 else ""
                    run.text = text
                    return
                if line is None:
                    return
                if run.text:
                    run.events.append({"kind": "Text", "summary": _text_summary(run.text)})
                    run.text = ""
                if isinstance(event, Steering) and event.applied:
                    run.steering_applied += 1
                kind, summary = line
                run.events.append({"kind": kind, "summary": summary,
                                   "at": _iso(time.time())})
                reason, agent = run.cancel_reason, run.agent
            if reason and agent is not None:
                agent.cancel(reason)
        except Exception:  # noqa: BLE001 — a summary never stops the run
            pass

    def _monitor(self, run: _Run, queue: tasks.TaskQueue, stop: threading.Event,
                 limit: float) -> None:
        """While `run` runs: apply its cancel (again, until its turn ends),
        cancel it past `limit` seconds, renew its lease when due. The first
        renewal is a heartbeat after the agent was built: until run_task's
        steps before the turn are over, it may put the task back as it was,
        which it does only while the record is as it wrote it."""
        next_beat: float | None = None
        while not stop.wait(self.tick):
            now = time.monotonic()
            with self._cond:
                if (run.started_mono is not None and now - run.started_mono >= limit
                        and not run.cancel_reason):
                    run.cancel_reason = f"the run went past {run.limit_text}"
                if run.cancel_reason:
                    run.cancelled.set()
                reason, agent, built = run.cancel_reason, run.agent, run.built_mono
            if reason and agent is not None:
                with contextlib.suppress(Exception):
                    agent.cancel(reason)
            if next_beat is None and built is not None:
                next_beat = built + self.heartbeat_seconds
            if next_beat is not None and now >= next_beat:
                self._renew(queue, run)
                next_beat = now + self.heartbeat_seconds

    def _renew(self, queue: tasks.TaskQueue, run: _Run) -> None:
        """Push `run`'s lease on its task out to `lease_minutes` from now, while
        the task is still claimed by this server (nothing else is written)."""
        expires = tasks.format_time(
            tasks.now_utc() + _dt.timedelta(minutes=max(1, int(self.lease_minutes))))

        def beat(current: dict[str, Any]) -> dict[str, Any]:
            return tasks.annotate(current, holder=self.holder,
                                  lease={"holder": self.holder, "expires_at": expires})

        with contextlib.suppress(tasks.TaskError, OSError, ValueError):
            queue.update(run.task_id, beat)

    def _release_ours(self, queue: tasks.TaskQueue, run: _Run) -> None:
        """A run that failed before it recorded anything: if the task is
        still claimed by this server, give it back (open)."""
        def back(current: dict[str, Any]) -> dict[str, Any]:
            if (tasks.status_of(current) == "claimed"
                    and tasks.lease_of(current).get("holder") == self.holder):
                return tasks.release(current, holder=self.holder)
            raise tasks.TaskError("not this server's claim")

        with contextlib.suppress(tasks.TaskError, OSError, ValueError):
            queue.update(run.task_id, back)

    def _status_now(self, run: _Run) -> str:
        try:
            return tasks.status_of(self.queue.read(run.task_id))
        except tasks.TaskError:
            return ""

    def _record(self, run: _Run, outcome: tasks.TaskRun) -> None:
        """`run` ended: keep what `evi.tasks.run_task` did."""
        res = outcome.result
        report = None
        if outcome.report_path is not None:
            with contextlib.suppress(OSError, ValueError):
                report = json.loads(Path(outcome.report_path).read_text(encoding="utf-8"))
        if report is None:
            with contextlib.suppress(Exception):
                report = res.to_dict()
        stop = outcome.stop_reason
        state = "cancelled" if stop == "interrupted" else ("error" if stop == "error" else "done")
        note = outcome.note
        if run.cancel_reason and state == "done":
            late = ("the cancel came once the run's turn and required checks were over, so "
                    "its outcome stands")
            note = f"{late}; {note}" if note else late
        agent = run.agent
        unapplied = 0
        take = getattr(agent, "take_pending_steering", None)
        if callable(take):
            with contextlib.suppress(Exception):
                unapplied = len(take())
        with self._cond:
            if run.text:
                run.events.append({"kind": "Text", "summary": _text_summary(run.text)})
                run.text = ""
            run.steering_unapplied = unapplied + len(run.pending_steer)
            self._end(
                run, state, stop_reason=stop, exit_code=outcome.exit_code,
                implemented=outcome.implemented, turns_used=int(getattr(res, "turns_used", 0) or 0),
                error=str(getattr(res, "error", "") or "") if stop == "error" else "",
                note=note, recorded=outcome.recorded, report=report,
                report_path=str(outcome.report_path or ""),
                task_status=tasks.status_of(outcome.record),
            )


# ---- the MCP surface --------------------------------------------------------------------

_STR = {"type": "string"}
_STRS = {"type": "array", "items": _STR}


def tool_specs() -> list[dict[str, Any]]:
    """The agent category's MCP tools: name, description, inputSchema."""
    def spec(name: str, description: str, properties: dict[str, Any],
             required: tuple[str, ...] = ()) -> dict[str, Any]:
        schema: dict[str, Any] = {"type": "object", "properties": properties,
                                  "additionalProperties": False}
        if required:
            schema["required"] = list(required)
        return {"name": name, "description": description, "inputSchema": schema}

    run_id = {"run_id": {"type": "string", "description": "The run_id task_start returned."}}
    return [
        spec("task_create",
             "File a new task in the repository's queue (.evi/tasks/<id>.toml, tracked). It "
             "starts open. Returns the task as filed.",
             {"title": {"type": "string", "description": "What the task is, in one line."},
              "why": {"type": "string", "description": "Why: the bug or the need."},
              "acceptance": {**_STRS, "description": "The acceptance criteria."},
              "files_in_scope": {**_STRS, "description": "Globs relative to the repository "
                                                         "the run may write."},
              "required_checks": {"type": "array", "description": (
                  "Commands that prove the work: a command line (split the POSIX way) or an "
                  "argv array. eVi runs each in the task's worktree; the task is implemented "
                  "only if they pass."),
                  "items": {"anyOf": [_STR, _STRS]}},
              "implementer": {"type": "string", "description": "A hint: evi-local, "
                                                              "evi-frontier or director."},
              "id": {"type": "string", "description": "The task id (default: the next T<n>)."}},
             ("title",)),
        spec("task_list",
             "List the repository's tasks: id, title, status, label (the status as the "
             "director's attestation has it), implementer, branch, attempts.",
             {"status": {"type": "string", "description": "Only tasks with this status."}}),
        spec("task_show", "Show one task: every field, its label and its file.",
             {"id": {"type": "string", "description": "The task id."}}, ("id",)),
        spec("task_start",
             "Start a task: it is queued and run in its own git worktree (branch task/<id>) "
             "under the implementer policy, one run at a time; eVi then runs its required "
             "checks and records the outcome (implemented, or open again). Returns {run_id, "
             "state} at once; follow it with run_status and collect it with run_result.",
             {"id": {"type": "string", "description": "The task id."},
              "backend": {"type": "string", "description": "A saved backend's name (default: "
                                                           "the configured one)."},
              "model": {"type": "string", "description": "A model id for this run."},
              "max_turns": {"type": "integer", "minimum": 1, "maximum": MAX_TURNS_LIMIT,
                            "description": "Most model round trips (default 30)."},
              "base": {"type": "string", "description": "The branch or commit a new task "
                                                        "branch starts from."}},
             ("id",)),
        spec("run_status",
             "A run's state (queued, running, done, cancelled, error), timing, turns used and "
             "its last few events (redacted); stop_reason and the task's status once it ended.",
             run_id, ("run_id",)),
        spec("run_steer",
             "Send the running agent a note: it joins the turn at its next step, as a note "
             "from the director, without interrupting the work in flight.",
             {**run_id, "text": {"type": "string", "description": "The note."}},
             ("run_id", "text")),
        spec("run_cancel",
             "Cancel a run: a queued one is dropped (the task untouched); a running one stops "
             "at its next step with stop_reason interrupted and the task goes back to open.",
             {**run_id, "reason": {"type": "string", "description": "Why (shown to the run)."}},
             ("run_id",)),
        spec("run_result",
             "A finished run's report (the v1 run report with its task section) and the "
             "task's status after it.",
             run_id, ("run_id",)),
    ]


def _need(args: Mapping[str, Any], key: str) -> str:
    value = args.get(key)
    if not isinstance(value, str) or not value.strip():
        raise AgentToolError(f"{key} is required")
    return value


def _list_arg(args: Mapping[str, Any], key: str) -> Any:
    value = args.get(key)
    if value is not None and not isinstance(value, list):
        raise AgentToolError(f"{key} must be a list")
    return value or []


def _writable(text: str) -> str:
    """`text` with each lone surrogate (the model's text may carry one) as
    its \\uXXXX escape: the MCP transport can't write one, and in a JSON
    result the escape reads back as the same string (`evi.tasks._write_report`
    writes a run report the same way)."""
    return text.encode("utf-8", "backslashreplace").decode("utf-8")


def dispatch(runs: AgentRuns, name: str, arguments: Mapping[str, Any] | None) -> str:
    """Run the agent-category tool `name`: its result as JSON text, or
    ``ERROR: ...`` (`evi.mcp.publish.is_error_text`); writable either way
    (`_writable`)."""
    args = dict(arguments or {})
    try:
        known = {s["name"]: s for s in tool_specs()}
        spec = known.get(name)
        if spec is None:
            return f"ERROR: unknown tool {name!r}"
        extra = sorted(set(args) - set(spec["inputSchema"]["properties"]))
        if extra:
            raise AgentToolError(f"{name} takes no {', '.join(map(repr, extra))}")
        if name == "task_create":
            for key in ("acceptance", "files_in_scope", "required_checks"):
                _list_arg(args, key)
            out: Any = runs.create_task(args)
        elif name == "task_list":
            out = runs.list_tasks(str(args.get("status") or ""))
        elif name == "task_show":
            out = runs.show_task(_need(args, "id"))
        elif name == "task_start":
            turns = args.get("max_turns")
            turns = 30 if turns is None else turns
            if isinstance(turns, bool) or not isinstance(turns, int) or not (
                    1 <= turns <= MAX_TURNS_LIMIT):
                raise AgentToolError(f"max_turns must be a whole number from 1 to "
                                     f"{MAX_TURNS_LIMIT}")
            out = runs.start(_need(args, "id"), backend=str(args.get("backend") or ""),
                             model=str(args.get("model") or ""), max_turns=turns,
                             base=str(args.get("base") or ""))
        elif name == "run_status":
            out = runs.status(_need(args, "run_id"))
        elif name == "run_steer":
            out = runs.steer(_need(args, "run_id"), _need(args, "text"))
        elif name == "run_cancel":
            out = runs.cancel(_need(args, "run_id"), str(args.get("reason") or ""))
        else:  # run_result
            out = runs.result(_need(args, "run_id"))
    except (tasks.TaskError, AgentToolError) as exc:
        return _writable(f"ERROR: {exc}")
    return _writable(json.dumps(out, ensure_ascii=False, default=str))
