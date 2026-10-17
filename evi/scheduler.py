"""Scheduler — fire `ScheduledTask`s on a cron schedule.

Wraps APScheduler's `BackgroundScheduler`. On each fire we build a one-shot
`Agent` (no shared history between runs) through `evi.sdk.build_agent` (so safe
mode, hooks and guardrails apply), drain its event stream into a log file under
`~/.evi/logs/scheduled/`, and record the run on the task. The log and the
recorded status are redacted (`evi.redact`) before they are written, and the
trace redacts each call and result before cutting it short.

Nobody is there to approve a tool call, so a scheduled run is unattended: the
auto-approved categories run, and anything that would ask follows
``[auto] unattended_policy`` — denied by default. The log header names the
policy, and a run where a call was denied records a non-ok status
("denied: …") so `evi schedule list` shows it. A delegate CLI-agent backend is
refused unless ``[llm] allow_cli_agent_unattended`` is set.

The scheduler is independent of the chat UI — running `evi scheduler` (CLI
daemon mode) is enough to fire jobs. When the FastAPI app is up it also
starts the scheduler in its lifespan context, so a single `evi web` process
covers both jobs.

Dependency is kept optional: `evi/scheduler.py` imports `apscheduler` only
inside `Scheduler.start()`. Users who don't `pip install 'evi-assistant[scheduler]'`
get a clear error there instead of an ImportError at top of the module.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from evi.config import SCHEDULED_LOG_DIR, Config, ensure_dirs
from evi.llm.agent import (
    Done,
    Error,
    TextDelta,
    ToolCall,
    ToolResult,
    is_permission_denial,
    make_unattended,
)
from evi.llm.cli_agent import unattended
from evi.redact import Redactor
from evi.scheduled import ScheduledTask, TaskStore


logger = logging.getLogger(__name__)


_SCHEDULED_SYSTEM_SUFFIX = (
    "You are running as a scheduled task — no human is reading in real time. "
    "Be thorough; the output is captured to a log file."
)


class Scheduler:
    """Owns a BackgroundScheduler and keeps it in sync with the TaskStore.

    `start()` loads every enabled task and schedules it. `reload()` re-syncs
    the running scheduler with the store (call after CLI/web admin edits).
    `stop()` shuts down cleanly.
    """

    def __init__(self, store: TaskStore | None = None) -> None:
        self.store = store or TaskStore()
        self._bg: Any | None = None  # apscheduler.schedulers.background.BackgroundScheduler
        self.started = False

    # --- lifecycle -------------------------------------------------------

    def start(self) -> None:
        if self.started:
            return
        try:
            from apscheduler.schedulers.background import BackgroundScheduler
            from apscheduler.triggers.cron import CronTrigger
        except ImportError as exc:
            raise RuntimeError(
                "scheduler requires apscheduler — "
                "install with: pip install 'evi-assistant[scheduler]'"
            ) from exc
        ensure_dirs()
        self._bg = BackgroundScheduler(daemon=True)
        self._BgCronTrigger = CronTrigger  # stash so reload() can reuse it
        self._sync_jobs()
        self._bg.start()
        self.started = True

    def stop(self) -> None:
        if self._bg is not None:
            try:
                self._bg.shutdown(wait=False)
            except Exception:
                pass
            self._bg = None
        self.started = False

    def reload(self) -> None:
        """Re-sync running jobs with whatever is currently in the store."""
        if self._bg is None:
            return
        # Wipe & re-add. Cheap for the volume we expect (dozens of tasks max).
        for job in list(self._bg.get_jobs()):
            job.remove()
        self._sync_jobs()

    # --- one-shot helpers ------------------------------------------------

    def run_now(self, task_id: str) -> str:
        """Execute a task immediately on the calling thread. Returns log path."""
        task = self.store.get(task_id)
        return _execute(task, self.store)

    # --- internals -------------------------------------------------------

    def _sync_jobs(self) -> None:
        assert self._bg is not None
        for task in self.store.list():
            if not task.enabled:
                continue
            try:
                trigger = self._BgCronTrigger.from_crontab(task.cron)
            except Exception as exc:
                logger.warning(
                    "task %s (%s) has invalid cron %r: %s",
                    task.id,
                    task.name,
                    task.cron,
                    exc,
                )
                continue
            self._bg.add_job(
                _execute,
                trigger=trigger,
                args=[task, self.store],
                id=task.id,
                name=task.name,
                replace_existing=True,
                misfire_grace_time=300,
            )


# --- job execution -------------------------------------------------------


def _execute(task: ScheduledTask, store: TaskStore) -> str:
    """Run one scheduled task, capture output to a log file, record status."""
    ensure_dirs()
    config = Config.load()
    log_path = SCHEDULED_LOG_DIR / _log_name(task)
    try:
        # Unattended: a delegate CLI agent is refused up front, and any client
        # built inside refuses on whatever thread later calls it.
        with unattended("scheduled task", config):
            if task.kind == "eval":
                text, status = _run_eval_once(task, config)
            else:
                text, status = _run_agent_once(task, config)
        # Secrets never reach the log or the task's recorded status: the
        # response, the prompt and an error can hold one as well as the trace.
        red = Redactor()
        log_path.write_text(red(text), encoding="utf-8")
        store.record_run(task.id, status=red(status))
        return str(log_path)
    except Exception as exc:
        msg = Redactor()(f"ERROR: {type(exc).__name__}: {exc}")
        try:
            log_path.write_text(msg, encoding="utf-8")
        except OSError:
            pass
        store.record_run(task.id, status=msg[:200])
        return str(log_path)


def _run_agent_once(task: ScheduledTask, config: Config) -> tuple[str, str]:
    """Build a fresh Agent, send the prompt, return (log text, status).

    The status is "ok", "denied: <tools>" when the unattended policy (or the
    guard) refused a call, or "error: <message>" when the turn failed."""
    from evi.sdk.builder import build_agent

    base_prompt = (
        "You are eVi, a personal AI assistant running locally. "
        "You have access to tools — call them when they would help, but "
        "answer directly when you don't need them. " + _SCHEDULED_SYSTEM_SUFFIX
    )
    # build_agent: safe mode, hooks and guardrails apply. No project context:
    # a scheduled run has no project folder.
    agent = build_agent(config=config, system_prompt=base_prompt, enable_project=False)
    # Nobody to ask: anything that would ask follows [auto] unattended_policy.
    policy = make_unattended(agent)

    text_parts: list[str] = []
    trace: list[str] = []
    denied: list[str] = []
    error: str | None = None
    # The log is a written record: redact, then cut (a key cut in half no
    # longer matches). _execute redacts the whole log again as it writes it.
    red = Redactor()
    for event in agent.chat(task.prompt, max_turns=8):
        if isinstance(event, TextDelta):
            text_parts.append(event.text)
        elif isinstance(event, ToolCall):
            trace.append(f"-> {event.name} {red.args(event.arguments)[:200]}")
        elif isinstance(event, ToolResult):
            preview = red(event.output)[:200].replace("\n", " ")
            trace.append(f"<- {event.name}: {preview}")
            if is_permission_denial(event.output) and event.name not in denied:
                denied.append(event.name)
        elif isinstance(event, Error):
            trace.append(f"!! {event.message}")
            error = event.message
            break
        elif isinstance(event, Done):
            break

    header = (
        f"# scheduled task: {task.name}\n"
        f"# id: {task.id}\n"
        f"# cron: {task.cron}\n"
        f"# fired_at: {datetime.now().isoformat(timespec='seconds')}\n"
        f"# policy: {policy}\n"
        f"# prompt: {task.prompt!r}\n"
        f"\n## response\n\n"
    )
    body = "".join(text_parts).strip() or "(no response text)"
    tail = ""
    if trace:
        tail = "\n\n## trace\n\n" + "\n".join(trace)
    if error is not None:
        status = f"error: {error}"
    elif denied:
        status = f"denied: {', '.join(denied)} (policy {policy})"
    else:
        status = "ok"
    return header + body + tail + "\n", red(status)[:200]


def _run_eval_once(task: ScheduledTask, config: Config) -> tuple[str, str]:
    """Run an eval suite (task.prompt = suite name). Returns (log_text, status)
    where status is "ok (P/T)" when all pass, else "fail (P/T)" — so drift shows
    up in `evi schedule list`.

    Cases run on the text-only engine (evals.make_runners) with every tool
    approved; the log header says so. Unlike `evi eval run`, which holds each
    case to the implementer policy in a throwaway workspace, it fails a case
    with behavioral assertions (evals.run_eval says why)."""
    from evi import evals
    from evi.headless import run_headless
    from evi.sdk.builder import build_agent

    suite = evals.load_suite(task.prompt)

    # As the web eval runner: no hooks or guardrails, so a hook that vetoes or
    # rewrites a call can't make the scores depend on this machine.
    bare = {"enable_memory": False, "enable_skills": False, "enable_project": False,
            "enable_hooks": False, "enable_guardrails": False}

    def run_one(case) -> str:
        agent = build_agent(config=config, **bare)
        agent.enable_auto_all()
        res = run_headless(agent, case.prompt)
        return res.text or (f"ERROR: {res.error}" if res.error else "")

    def judge_fn(case, output) -> tuple[bool, str]:
        agent = build_agent(config=config, tools=[], **bare)
        prompt = (
            "Grade the ANSWER against the RUBRIC. Reply PASS or FAIL on the first "
            f"line, then a one-line reason.\n\nRUBRIC: {case.judge}\n\nANSWER:\n{output}"
        )
        res = run_headless(agent, prompt)
        first = (res.text or "").strip().splitlines()[0] if res.text else ""
        return first.strip().upper().startswith("PASS"), first[:200]

    report = evals.run_eval(suite, run_one, judge_fn=judge_fn)
    p, t = report["passed"], report["total"]
    status = f"ok ({p}/{t})" if p == t else f"fail ({p}/{t})"
    lines = [
        f"# scheduled eval: {task.name}",
        f"# suite: {suite.name}  ({p}/{t} passed)",
        f"# fired_at: {datetime.now().isoformat(timespec='seconds')}",
        "# policy: allow-all (eval suite cases, text-only engine)",
        "",
    ]
    for c in report["cases"]:
        mark = "PASS" if c["passed"] else "FAIL"
        lines.append(f"[{mark}] {c['name']}")
        for f in c["failures"]:
            lines.append(f"    {f}")
    return "\n".join(lines) + "\n", status


def _log_name(task: ScheduledTask) -> str:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return f"{task.id}_{stamp}.log"
