"""Agent teams — a shared, claimable task list with dependencies.

eVi's analogue of Claude Code's agent teams. A **lead** decomposes a goal into a
list of tasks (with `blocked_by` dependencies) persisted to a shared JSON file;
**teammates** (subagents) then claim ready tasks, work them, and record results,
draining the list in dependency order with bounded parallelism.

This is distinct from the other orchestration primitives:
- ultracode  — a *fixed* pipeline over one task (decompose→solve→verify→synth).
- workflows  — a *declarative* TOML DAG the user authors.
- teams      — a *dynamic, claimable* task list (the lead fills it; teammates
               pull from it), persisted so it can be inspected/resumed.

The core is model-free and thread-safe: :class:`TeamStore` guards every mutation
with a lock and persists atomically, and :func:`drain_team` takes an injected
``run_one(task) -> str`` (exactly like ``ultracode.run_ultracode``), so the
draining logic is unit-testable without a backend.
"""

from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

# Task lifecycle: pending -> in_progress -> completed | failed; a task whose
# dependency failed can never become ready and is reported "blocked".
PENDING = "pending"
IN_PROGRESS = "in_progress"
COMPLETED = "completed"
FAILED = "failed"
BLOCKED = "blocked"


@dataclass
class TeamTask:
    id: str
    subject: str
    status: str = PENDING
    owner: str = ""
    blocked_by: list[str] = field(default_factory=list)
    result: str = ""


def _default_path() -> Path:
    from evi.config import HOME

    return HOME / "team.json"


class TeamStore:
    """A shared task list persisted to one JSON file. Thread-safe: claims and
    status changes are serialised so concurrent teammates can't double-claim."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or _default_path()
        self._lock = threading.Lock()

    # --- persistence -----------------------------------------------------

    def load(self) -> list[TeamTask]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        out: list[TeamTask] = []
        for d in data if isinstance(data, list) else []:
            if isinstance(d, dict) and d.get("id") and d.get("subject"):
                out.append(TeamTask(
                    id=str(d["id"]), subject=str(d["subject"]),
                    status=str(d.get("status", PENDING)), owner=str(d.get("owner", "")),
                    blocked_by=[str(b) for b in d.get("blocked_by", []) or []],
                    result=str(d.get("result", "")),
                ))
        return out

    def _save(self, tasks: list[TeamTask]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps([asdict(t) for t in tasks], indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, self.path)  # atomic on POSIX + Windows

    # --- mutations (all under the lock) ----------------------------------

    def add(self, subject: str, blocked_by: list[str] | None = None) -> TeamTask:
        with self._lock:
            tasks = self.load()
            tid = _next_id(tasks)
            task = TeamTask(id=tid, subject=subject.strip(), blocked_by=list(blocked_by or []))
            tasks.append(task)
            self._save(tasks)
            return task

    def clear(self) -> None:
        with self._lock:
            self._save([])

    def claim(self, worker: str) -> TeamTask | None:
        """Atomically take the first ready task (pending + all deps completed),
        mark it in_progress/owned, and return it. None if nothing is ready."""
        with self._lock:
            tasks = self.load()
            done = {t.id for t in tasks if t.status == COMPLETED}
            for t in tasks:
                if t.status == PENDING and all(b in done for b in t.blocked_by):
                    t.status = IN_PROGRESS
                    t.owner = worker
                    self._save(tasks)
                    return t
            return None

    def complete(self, task_id: str, result: str) -> None:
        self._finish(task_id, COMPLETED, result)

    def fail(self, task_id: str, error: str) -> None:
        self._finish(task_id, FAILED, error)

    def _finish(self, task_id: str, status: str, text: str) -> None:
        with self._lock:
            tasks = self.load()
            for t in tasks:
                if t.id == task_id:
                    t.status = status
                    t.result = text
            self._save(tasks)

    # --- queries (no lock needed; read a fresh snapshot) -----------------

    def any_active(self) -> bool:
        """True while work can still progress: something in_progress, or a
        pending task whose deps are all completed or themselves still active."""
        tasks = self.load()
        statuses = {t.id: t.status for t in tasks}
        if any(t.status == IN_PROGRESS for t in tasks):
            return True
        done = {tid for tid, s in statuses.items() if s == COMPLETED}
        for t in tasks:
            if t.status != PENDING:
                continue
            # runnable now, or waiting only on deps that can still complete
            if all(b in done or statuses.get(b) in (PENDING, IN_PROGRESS) for b in t.blocked_by):
                return True
        return False


def _next_id(tasks: list[TeamTask]) -> str:
    nums = [int(t.id[1:]) for t in tasks if t.id[:1] == "t" and t.id[1:].isdigit()]
    return f"t{(max(nums) + 1) if nums else 1}"


def ready_tasks(tasks: list[TeamTask]) -> list[TeamTask]:
    """Pending tasks whose dependencies are all completed (pure helper)."""
    done = {t.id for t in tasks if t.status == COMPLETED}
    return [t for t in tasks if t.status == PENDING and all(b in done for b in t.blocked_by)]


def drain_team(store, run_one, *, max_workers: int = 3, poll: float = 0.02) -> list[TeamTask]:
    """Run teammates that claim ready tasks and execute them via ``run_one(task)
    -> str`` until the list can make no more progress. Returns the final tasks.

    ``run_one`` must never need to raise — but if it does, the task is marked
    failed (its dependents become unrunnable and are reported, never hung)."""
    from evi.workflows import fan_out

    def worker(idx: int) -> None:
        name = f"teammate-{idx + 1}"
        while True:
            task = store.claim(name)
            if task is None:
                if store.any_active():
                    time.sleep(poll)  # a dependency is still being worked
                    continue
                return  # drained (or only permanently-blocked tasks remain)
            try:
                store.complete(task.id, run_one(task))
            except Exception as exc:  # noqa: BLE001 — a teammate failure can't hang the team
                store.fail(task.id, f"ERROR: {type(exc).__name__}: {exc}")

    n = max(1, max_workers)
    fan_out(worker, list(range(n)), n)
    return store.load()


def plan_schema() -> dict:
    """JSON schema for a lead's decomposition — a list of {subject, blocked_by}."""
    return {
        "type": "object",
        "properties": {
            "tasks": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string"},
                        "subject": {"type": "string"},
                        "blocked_by": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["id", "subject"],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["tasks"],
        "additionalProperties": False,
    }


def populate_from_plan(store: TeamStore, plan: list[dict]) -> list[TeamTask]:
    """Add a lead's decomposition to the store, remapping the lead's own task ids
    (e.g. "1", "t1") to the store's ids so blocked_by references stay valid."""
    id_map: dict[str, str] = {}
    created: list[TeamTask] = []
    # First pass: create tasks, recording the lead id -> store id mapping.
    for i, spec in enumerate(plan):
        lead_id = str(spec.get("id") or i + 1)
        subject = str(spec.get("subject") or "").strip()
        if not subject:
            continue
        task = store.add(subject)
        id_map[lead_id] = task.id
        created.append(task)
    # Second pass: rewrite blocked_by through the id map (drop danglers).
    tasks = store.load()
    by_id = {t.id: t for t in tasks}
    for spec, task in zip(plan, created):
        deps = [id_map[str(b)] for b in (spec.get("blocked_by") or []) if str(b) in id_map]
        if deps:
            by_id[task.id].blocked_by = deps
    with store._lock:
        store._save(list(by_id.values()))
    return store.load()
