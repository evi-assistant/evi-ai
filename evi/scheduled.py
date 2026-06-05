"""Scheduled task store — one JSON file per task under ~/.evi/scheduled/.

A scheduled task is just a saved prompt plus a cron expression: when the
scheduler fires it, a fresh `Agent` is built, the prompt is sent, the final
assistant text is captured to a log file. No conversation history persists
between firings.

We persist as one JSON file per task instead of a single index so concurrent
writes (CLI + web admin endpoints, eventually) don't trample each other.
"""

from __future__ import annotations

import json
import secrets
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from evi.config import SCHEDULED_DIR


@dataclass
class ScheduledTask:
    id: str
    name: str
    cron: str                 # crontab-style: "min hour dom month dow"
    prompt: str
    enabled: bool = True
    created_at: float = field(default_factory=time.time)
    last_run: float | None = None
    last_status: str | None = None  # "ok" | "error: ..."

    def to_dict(self) -> dict[str, object]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "ScheduledTask":
        return cls(
            id=str(data["id"]),
            name=str(data.get("name", data["id"])),
            cron=str(data["cron"]),
            prompt=str(data["prompt"]),
            enabled=bool(data.get("enabled", True)),
            created_at=float(data.get("created_at", time.time())),
            last_run=data.get("last_run"),
            last_status=data.get("last_status"),
        )


class TaskStore:
    """Filesystem-backed CRUD over `~/.evi/scheduled/*.json`.

    Methods raise `KeyError` for missing IDs and `ValueError` for invalid
    input. Persistence uses atomic writes (write to temp, then rename) so a
    crash mid-save can't corrupt a task file.
    """

    def __init__(self, root: Path | None = None) -> None:
        self.root = Path(root) if root is not None else SCHEDULED_DIR

    def add(
        self,
        *,
        name: str,
        cron: str,
        prompt: str,
        enabled: bool = True,
    ) -> ScheduledTask:
        if not name or not cron or not prompt:
            raise ValueError("name, cron, and prompt are all required")
        task = ScheduledTask(
            id=secrets.token_hex(4),
            name=name,
            cron=cron,
            prompt=prompt,
            enabled=enabled,
        )
        self._write(task)
        return task

    def list(self) -> list[ScheduledTask]:
        if not self.root.is_dir():
            return []
        out: list[ScheduledTask] = []
        for p in sorted(self.root.glob("*.json")):
            try:
                out.append(ScheduledTask.from_dict(json.loads(p.read_text("utf-8"))))
            except (OSError, json.JSONDecodeError, KeyError):
                continue
        return out

    def get(self, task_id: str) -> ScheduledTask:
        path = self._path(task_id)
        if not path.is_file():
            raise KeyError(task_id)
        return ScheduledTask.from_dict(json.loads(path.read_text("utf-8")))

    def remove(self, task_id: str) -> bool:
        path = self._path(task_id)
        if not path.is_file():
            return False
        path.unlink()
        return True

    def update(self, task: ScheduledTask) -> None:
        if not self._path(task.id).is_file():
            raise KeyError(task.id)
        self._write(task)

    def set_enabled(self, task_id: str, enabled: bool) -> ScheduledTask:
        task = self.get(task_id)
        task.enabled = enabled
        self._write(task)
        return task

    def record_run(self, task_id: str, status: str) -> None:
        task = self.get(task_id)
        task.last_run = time.time()
        task.last_status = status
        self._write(task)

    # --- internals -------------------------------------------------------

    def _path(self, task_id: str) -> Path:
        if not task_id or "/" in task_id or "\\" in task_id or ".." in task_id:
            raise ValueError(f"invalid task id {task_id!r}")
        return self.root / f"{task_id}.json"

    def _write(self, task: ScheduledTask) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        path = self._path(task.id)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(task.to_dict(), indent=2), encoding="utf-8")
        tmp.replace(path)
