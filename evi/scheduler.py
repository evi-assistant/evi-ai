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

An eval task (``kind = "eval"``) runs one of your suites as ``evi eval run``
does (`evi.evals.run_suite`: each case in a throwaway workspace under the
implementer policy, which its log header names) and keeps the report.

The Companion (evi/companion/) needs no task: with ``[companion] enabled =
true`` in your config.toml the scheduler adds two built-in jobs of its own
(`companion_jobs`, re-read at every `reload`): ``companion-check-in`` every
15 minutes, which runs a check-in once ``check_every_hours`` have passed
since the last (and a brief missed at its time), and ``companion-brief``
daily at ``brief_at``. Both are no-ops unless due, so a reload (every minute
under `evi scheduler`) never shifts them. Off (or in safe mode): no jobs.

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
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable

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
        for job in companion_jobs():
            try:
                trigger = self._BgCronTrigger.from_crontab(job.cron)
            except Exception as exc:
                logger.warning("companion job %s has invalid cron %r: %s", job.id, job.cron, exc)
                continue
            self._bg.add_job(
                job.func,
                trigger=trigger,
                id=job.id,
                name=job.name,
                replace_existing=True,
                misfire_grace_time=300,
                coalesce=True,
                max_instances=1,
            )
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


# --- the Companion's built-in jobs ----------------------------------------


@dataclass(frozen=True)
class CompanionJob:
    """A built-in scheduler job of the Companion."""

    id: str
    name: str
    cron: str
    func: Callable[[], Any]


# How often the check-in job looks whether a check-in (or a missed brief) is due.
COMPANION_TICK_CRON = "*/15 * * * *"


def companion_jobs(config: Config | None = None) -> list[CompanionJob]:
    """The Companion's jobs for `config` (default: loaded now): none unless
    ``[companion] enabled`` is true in the user layer (evi.companion.engine.
    user_enabled) and safe mode is off; never raises."""
    try:
        from evi import safemode
        from evi.companion import engine

        if safemode.enabled():
            return []
        cfg = config if config is not None else Config.load()
        s = engine.settings(cfg)
        if not s.enabled:
            return []
        hour, minute = s.brief_at
    except Exception as exc:  # noqa: BLE001 — a bad config means no companion jobs
        logger.warning("companion jobs not scheduled: %s", exc)
        return []
    return [
        CompanionJob("companion-check-in", "Companion check-in", COMPANION_TICK_CRON,
                     _companion_tick),
        CompanionJob("companion-brief", "Companion morning brief", f"{minute} {hour} * * *",
                     _companion_brief),
    ]


def _companion_tick() -> None:
    """The check-in job: a check-in when due (evi.companion.engine.Companion.tick)."""
    try:
        from evi.companion.engine import Companion

        Companion().tick()
    except Exception as exc:  # noqa: BLE001 — a job must not take the scheduler down
        logger.warning("companion check-in job failed: %s", exc)


def _companion_brief() -> None:
    """The brief job: the morning brief, once a day."""
    try:
        from evi.companion.engine import Companion

        Companion().brief()
    except Exception as exc:  # noqa: BLE001
        logger.warning("companion brief job failed: %s", exc)


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
    """Run an eval suite (task.prompt = one of your suites) as `evi eval run`
    does (`evals.run_suite`): each case in a throwaway workspace under the
    implementer policy, on the configured [llm] backend and model
    (`evals.configured_context`), with no user hooks (the engine builds bare
    agents, so the scores don't depend on this machine). Keeps the report
    (`evals.write_report`) and returns (log_text, status): "ok (P/T)" (P
    passed of the suite's T cases) when the report is green
    (`evals.report_ok`: an expected failure excused) and every case ran,
    else "fail (P/T)", so drift shows up in `evi schedule list`; expected
    failures and skipped cases are counted after P/T ("fail (1/4, 3
    skipped)"). A run that skipped a case fails: nobody is watching to
    notice it checked less than the suite (a repo case with no trusted
    repository to make a worktree of)."""
    from evi import evals
    from evi.policy import IMPLEMENTER

    suite = evals.load_suite(task.prompt)
    report = evals.run_suite(suite, evals.configured_context(config))
    path, _page = evals.write_report(report)
    p, t = report["passed"], report["total"]
    ok = evals.report_ok(report) and report["ran"] > 0 and not report["skipped"]
    counts = [f"{p}/{t}"]
    if report["expected_failures"]:
        counts.append(f"{report['expected_failures']} expected to fail")
    if report["skipped"]:
        counts.append(f"{report['skipped']} skipped")
    status = f"{'ok' if ok else 'fail'} ({', '.join(counts)})"
    lines = [
        f"# scheduled eval: {task.name}",
        f"# suite: {suite.name}  ({p}/{t} passed, {report['expected_failures']} expected "
        f"to fail, {report['skipped']} skipped)",
        f"# fired_at: {datetime.now().isoformat(timespec='seconds')}",
        f"# model: {report['model']} ({report['backend']})",
        f"# policy: {IMPLEMENTER} (each case in a throwaway workspace)",
        f"# report: {path}",
        "",
    ]
    for row in report["cases"]:
        lines.append(f"[{evals.verdict(row)}] {row['name']}")
        if row.get("skipped"):
            lines.append(f"    skipped: {row.get('skip_reason')}")
        for f in row.get("failures") or []:
            lines.append(f"    {f}")
    return "\n".join(lines) + "\n", status


def _log_name(task: ScheduledTask) -> str:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return f"{task.id}_{stamp}.log"
