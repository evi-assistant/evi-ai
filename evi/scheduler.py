"""Scheduler — fire `ScheduledTask`s on a cron schedule.

Wraps APScheduler's `BackgroundScheduler`. On each fire we build a one-shot
`Agent` (no shared history between runs), drain its event stream into a log
file under `~/.evi/logs/scheduled/`, and record the run on the task.

The scheduler is independent of the chat UI — running `evi scheduler` (CLI
daemon mode) is enough to fire jobs. When the FastAPI app is up it also
starts the scheduler in its lifespan context, so a single `evi web` process
covers both jobs.

Dependency is kept optional: `evi/scheduler.py` imports `apscheduler` only
inside `Scheduler.start()`. Users who don't `pip install 'evi-ai[scheduler]'`
get a clear error there instead of an ImportError at top of the module.
"""

from __future__ import annotations

import logging
from dataclasses import asdict
from datetime import datetime
from typing import Any

from evi.config import SCHEDULED_LOG_DIR, Config, ensure_dirs
from evi.llm.agent import Agent, Done, Error, TextDelta, ToolCall, ToolResult
from evi.llm.client import make_client
from evi.memory import MemoryStore
from evi.scheduled import ScheduledTask, TaskStore
from evi.skills import SkillStore
from evi.tools.base import get_enabled_tools


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
                "install with: pip install 'evi-ai[scheduler]'"
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
        text = _run_agent_once(task, config)
        log_path.write_text(text, encoding="utf-8")
        store.record_run(task.id, status="ok")
        return str(log_path)
    except Exception as exc:
        msg = f"ERROR: {type(exc).__name__}: {exc}"
        try:
            log_path.write_text(msg, encoding="utf-8")
        except OSError:
            pass
        store.record_run(task.id, status=msg[:200])
        return str(log_path)


def _run_agent_once(task: ScheduledTask, config: Config) -> str:
    """Build a fresh Agent, send the prompt, return assistant text + trace."""
    client = make_client(config.llm)
    toggles = asdict(config.tools)
    tools = get_enabled_tools(toggles)
    memory = MemoryStore() if toggles.get("memory") else None
    skills = SkillStore() if toggles.get("skills") else None

    base_prompt = (
        "You are Evi, a personal AI assistant running locally. "
        "You have access to tools — call them when they would help, but "
        "answer directly when you don't need them. " + _SCHEDULED_SYSTEM_SUFFIX
    )
    agent = Agent(
        client=client,
        config=config,
        tools=tools,
        system_prompt=base_prompt,
        memory=memory,
        skills=skills,
    )

    text_parts: list[str] = []
    trace: list[str] = []
    for event in agent.chat(task.prompt, max_turns=8):
        if isinstance(event, TextDelta):
            text_parts.append(event.text)
        elif isinstance(event, ToolCall):
            trace.append(f"-> {event.name} {event.arguments[:200]}")
        elif isinstance(event, ToolResult):
            preview = event.output[:200].replace("\n", " ")
            trace.append(f"<- {event.name}: {preview}")
        elif isinstance(event, Error):
            trace.append(f"!! {event.message}")
            break
        elif isinstance(event, Done):
            break

    header = (
        f"# scheduled task: {task.name}\n"
        f"# id: {task.id}\n"
        f"# cron: {task.cron}\n"
        f"# fired_at: {datetime.now().isoformat(timespec='seconds')}\n"
        f"# prompt: {task.prompt!r}\n"
        f"\n## response\n\n"
    )
    body = "".join(text_parts).strip() or "(no response text)"
    tail = ""
    if trace:
        tail = "\n\n## trace\n\n" + "\n".join(trace)
    return header + body + tail + "\n"


def _log_name(task: ScheduledTask) -> str:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return f"{task.id}_{stamp}.log"
