"""Tests for scheduled-task persistence and the Scheduler runner.

The scheduler tests don't depend on apscheduler being installed — we
exercise `_execute` / `_run_agent_once` directly with a stubbed Agent. The
APScheduler integration in `Scheduler.start()` is a thin pass-through to a
trusted library and is verified manually.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import evi.scheduler as scheduler_mod
from evi.llm.agent import Done, TextDelta, ToolCall, ToolResult
from evi.scheduled import ScheduledTask, TaskStore


# ---- TaskStore CRUD -----------------------------------------------------


@pytest.fixture
def store(tmp_path: Path) -> TaskStore:
    return TaskStore(root=tmp_path)


def test_add_and_get(store: TaskStore) -> None:
    t = store.add(name="morning", cron="0 9 * * *", prompt="hi")
    fetched = store.get(t.id)
    assert fetched.name == "morning"
    assert fetched.cron == "0 9 * * *"
    assert fetched.enabled is True


def test_list_sorted_and_skips_garbage(store: TaskStore, tmp_path: Path) -> None:
    store.add(name="a", cron="* * * * *", prompt="p")
    store.add(name="b", cron="* * * * *", prompt="p")
    (tmp_path / "not-a-task.json").write_text("{garbage", encoding="utf-8")
    tasks = store.list()
    assert {t.name for t in tasks} == {"a", "b"}


def test_remove_returns_existence(store: TaskStore) -> None:
    t = store.add(name="x", cron="* * * * *", prompt="p")
    assert store.remove(t.id) is True
    assert store.remove(t.id) is False


def test_set_enabled(store: TaskStore) -> None:
    t = store.add(name="x", cron="* * * * *", prompt="p")
    store.set_enabled(t.id, False)
    assert store.get(t.id).enabled is False
    store.set_enabled(t.id, True)
    assert store.get(t.id).enabled is True


def test_record_run_updates_metadata(store: TaskStore) -> None:
    t = store.add(name="x", cron="* * * * *", prompt="p")
    store.record_run(t.id, status="ok")
    refetched = store.get(t.id)
    assert refetched.last_status == "ok"
    assert refetched.last_run is not None


def test_invalid_inputs(store: TaskStore) -> None:
    with pytest.raises(ValueError):
        store.add(name="", cron="* * * * *", prompt="x")
    with pytest.raises(ValueError):
        store._path("..")  # path-traversal attempt
    with pytest.raises(KeyError):
        store.get("nonexistent")


def test_atomic_write_no_tmp_left_behind(store: TaskStore, tmp_path: Path) -> None:
    store.add(name="x", cron="* * * * *", prompt="p")
    leftovers = list(tmp_path.glob("*.tmp"))
    assert leftovers == []


# ---- Scheduler job execution -------------------------------------------


class _FakeAgent:
    def __init__(self, **kwargs) -> None:
        self.init_kwargs = kwargs

    def chat(self, prompt: str, max_turns: int = 6):
        yield TextDelta(text="hello ")
        yield TextDelta(text=prompt[:5])
        yield ToolCall(name="read_file", arguments='{"path":"x"}')
        yield ToolResult(name="read_file", output="contents-of-x")
        yield Done(reason="stop")


@pytest.fixture
def stubbed_runtime(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Redirect scheduled log dir + stub the LLM client + Agent."""
    monkeypatch.setattr(scheduler_mod, "SCHEDULED_LOG_DIR", tmp_path)
    monkeypatch.setattr(scheduler_mod, "make_client", lambda *_: None)
    monkeypatch.setattr(scheduler_mod, "Agent", _FakeAgent)
    monkeypatch.setattr(scheduler_mod, "get_enabled_tools", lambda _: [])
    monkeypatch.setattr(scheduler_mod, "ensure_dirs", lambda: None)
    return tmp_path


def test_execute_writes_log_and_records_status(
    stubbed_runtime: Path, tmp_path: Path
) -> None:
    store = TaskStore(root=tmp_path / "store")
    task = store.add(name="daily", cron="0 9 * * *", prompt="summarize today")

    log_path_str = scheduler_mod._execute(task, store)
    log_path = Path(log_path_str)
    assert log_path.is_file()
    body = log_path.read_text("utf-8")
    assert "summarize today" in body
    assert "hello summa" in body  # text deltas concatenated
    assert "trace" in body
    assert "read_file" in body

    refetched = store.get(task.id)
    assert refetched.last_status == "ok"
    assert refetched.last_run is not None


def test_execute_records_error_on_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, stubbed_runtime: Path
) -> None:
    def _boom(*_args, **_kwargs):
        raise RuntimeError("model is down")

    monkeypatch.setattr(scheduler_mod, "_run_agent_once", _boom)
    store = TaskStore(root=tmp_path / "store")
    task = store.add(name="x", cron="* * * * *", prompt="p")

    log_path = scheduler_mod._execute(task, store)
    assert "ERROR" in Path(log_path).read_text("utf-8")
    assert store.get(task.id).last_status.startswith("ERROR")


def test_scheduler_start_without_apscheduler_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If apscheduler isn't installed we surface a clear error, not ImportError."""
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name.startswith("apscheduler"):
            raise ImportError("not installed")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    from evi.scheduler import Scheduler

    sched = Scheduler()
    with pytest.raises(RuntimeError, match="apscheduler"):
        sched.start()


def test_scheduled_task_json_roundtrip(tmp_path: Path) -> None:
    """Persistence shape stays stable across save/load."""
    store = TaskStore(root=tmp_path)
    t = store.add(name="r", cron="*/5 * * * *", prompt="hello")
    saved = json.loads((tmp_path / f"{t.id}.json").read_text("utf-8"))
    assert saved["name"] == "r"
    assert saved["cron"] == "*/5 * * * *"
    rebuilt = ScheduledTask.from_dict(saved)
    assert rebuilt.id == t.id
