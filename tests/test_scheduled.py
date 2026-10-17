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
from evi.llm.agent import Done, Error, TextDelta, ToolCall, ToolResult
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
    """Redirect scheduled log dir + config under tmp_path, and stub the Agent
    (every scheduled run is built through evi.sdk.builder.build_agent)."""
    import evi.config as config_mod

    monkeypatch.setattr(config_mod, "HOME", tmp_path)
    monkeypatch.setattr(config_mod, "CONFIG_PATH", tmp_path / "config.toml")
    monkeypatch.setattr(scheduler_mod, "SCHEDULED_LOG_DIR", tmp_path)
    monkeypatch.setattr("evi.sdk.builder.build_agent", _FakeAgent)
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
    assert "# policy: deny" in body  # unattended, fail-closed by default

    refetched = store.get(task.id)
    assert refetched.last_status == "ok"
    assert refetched.last_run is not None


# ---- scheduled evals -----------------------------------------------------


def test_kind_persists(store: TaskStore) -> None:
    t = store.add(name="nightly", cron="0 3 * * *", prompt="smoke", kind="eval")
    assert store.get(t.id).kind == "eval"


def test_kind_defaults_to_prompt_for_old_files() -> None:
    t = ScheduledTask.from_dict({"id": "x", "cron": "* * * * *", "prompt": "hi"})
    assert t.kind == "prompt"


def _canned_report(passed, total):
    """A run_suite report (evals.REPORT_SCHEMA) where `passed` of `total`
    cases passed."""
    from evi import evals
    from evi.evals import EvalCase, RunOutcome

    rows = [evals.case_row(EvalCase(name=f"c{i}", prompt="p"),
                           [RunOutcome(i < passed, [] if i < passed else ["x"])])
            for i in range(total)]
    return evals.summarize({
        "schema_version": 1, "kind": evals.REPORT_KIND, "suite": "smoke", "model": "m",
        "backend": "b", "n": None, "evi_version": "0", "started_at": "2026-10-17T00:00:00Z",
        "finished_at": "2026-10-17T00:01:00Z", "duration_s": 60.0,
        "hardware": {"gpu": "none", "vram_gb": None, "cpu": "t", "ram_gb": None, "os": "t"},
        "baseline": None, "cases": rows})


def _stub_eval_engine(monkeypatch, report) -> list:
    """Stub the engine scheduled evals run (evals.run_suite) and where it keeps
    the report; returns the EvalContexts it was run with."""
    from evi import evals
    from evi.evals import EvalSuite

    ran: list = []
    monkeypatch.setattr(evals, "load_suite", lambda name, root=None: EvalSuite(name="smoke"))
    monkeypatch.setattr(evals, "find_repo", lambda start=None: None)
    monkeypatch.setattr(evals, "run_suite", lambda s, ctx, **kw: ran.append(ctx) or report)
    monkeypatch.setattr(evals, "write_report", lambda r, root=None: (Path("r.json"),
                                                                     Path("r.html")))
    return ran


def test_execute_eval_all_pass(stubbed_runtime, tmp_path, monkeypatch) -> None:
    ran = _stub_eval_engine(monkeypatch, _canned_report(2, 2))
    store = TaskStore(root=tmp_path / "store")
    task = store.add(name="nightly", cron="0 3 * * *", prompt="smoke", kind="eval")

    body = Path(scheduler_mod._execute(task, store)).read_text("utf-8")
    assert "scheduled eval" in body and "[PASS] c0" in body and "2/2" in body
    # The implementer policy the engine holds each case to, not allow-all.
    assert "# policy: implementer" in body and "allow-all" not in body
    assert "# report: r.json" in body
    assert store.get(task.id).last_status == "ok (2/2)"
    [ctx] = ran
    assert not ctx.fake  # the configured backend, never the fake one


def test_execute_eval_failure_status(stubbed_runtime, tmp_path, monkeypatch) -> None:
    _stub_eval_engine(monkeypatch, _canned_report(1, 2))
    store = TaskStore(root=tmp_path / "store")
    task = store.add(name="nightly", cron="0 3 * * *", prompt="smoke", kind="eval")

    body = Path(scheduler_mod._execute(task, store)).read_text("utf-8")
    assert "[FAIL] c1" in body and "    run 1: x" in body
    assert store.get(task.id).last_status == "fail (1/2)"


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


# ---- secret redaction in the log ------------------------------------------

# Shapeless, so only the known-value set (the env var) can catch it.
SECRET = "Zq8vN2kLp4Xr7Tw1Ys5M-env"
MARK = "[REDACTED:ACME_API_KEY]"


@pytest.fixture
def env_key(monkeypatch: pytest.MonkeyPatch):
    import os

    from evi import redact

    for name in list(os.environ):
        if redact._SECRET_NAME.search(name):
            monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ACME_API_KEY", SECRET)
    redact.refresh()
    yield
    redact.refresh()


def _no_prefix(text: str) -> bool:
    """No 8-character run of SECRET shows: not whole, not cut short."""
    return not any(SECRET[i:i + 8] in text for i in range(len(SECRET) - 7))


def test_a_scheduled_run_whose_tool_echoes_an_env_key_logs_it_redacted(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, stubbed_runtime: Path, env_key
) -> None:
    """A real Agent (scripted model, a tool that prints an env var) through
    _execute: the key is in the call's arguments, a duplicate key's earlier
    value, the tool's output and the reply, and the log holds none of it."""
    import os

    from evi.config import Config
    from evi.llm.agent import Agent
    from evi.tools.base import Tool
    from tests._live_web import Scripted
    from tests.conftest import llm_call, llm_text

    echo = Tool(
        name="echo_env", description="print an environment variable",
        parameters={"type": "object", "properties": {"name": {"type": "string"},
                                                     "note": {"type": "string"}}},
        func=lambda name="", note="": f"{name}={os.environ.get(name, '')}",
    )
    agent = Agent(
        client=Scripted([
            llm_call(("echo_env", {"name": "ACME_API_KEY", "note": f"k={SECRET}"}),
                     ("echo_env", '{"name": "ACME_API_KEY", "note": "' + SECRET + '", "note": "x"}')),
            llm_text(f"The key is {SECRET}."),
        ]),
        config=Config(), tools=[echo], transcripts=None, unattended_policy="allow",
    )
    monkeypatch.setattr("evi.sdk.builder.build_agent", lambda *a, **k: agent)
    store = TaskStore(root=tmp_path / "store")
    task = store.add(name="daily", cron="0 9 * * *", prompt=f"check {SECRET}")

    body = Path(scheduler_mod._execute(task, store)).read_text("utf-8")
    assert _no_prefix(body), body
    assert f"<- echo_env: ACME_API_KEY={MARK}" in body
    assert f'-> echo_env {{"name": "ACME_API_KEY", "note": "k={MARK}"}}' in body
    assert f"The key is {MARK}." in body and f"check {MARK}" in body
    assert store.get(task.id).last_status == "ok"


def test_the_scheduled_trace_redacts_before_it_cuts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, stubbed_runtime: Path, env_key
) -> None:
    """Each trace line is cut to 200 characters; a key straddling the cut would
    leave a prefix no redaction of the whole log could match."""

    class _Straddling:
        def __init__(self, **kwargs) -> None:
            pass

        def chat(self, prompt: str, max_turns: int = 6):
            yield ToolCall(name="t", arguments=json.dumps({"q": "a" * 182 + SECRET}))
            yield ToolResult(name="t", output="y" * 190 + SECRET)
            yield Error(message=f"backend said: bad key {SECRET}")

    monkeypatch.setattr("evi.sdk.builder.build_agent", _Straddling)
    store = TaskStore(root=tmp_path / "store")
    task = store.add(name="x", cron="* * * * *", prompt="p")

    body = Path(scheduler_mod._execute(task, store)).read_text("utf-8")
    assert _no_prefix(body), body
    assert "y" * 190 + "[REDACTED:" in body
    status = store.get(task.id).last_status
    assert status == f"error: backend said: bad key {MARK}"


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
