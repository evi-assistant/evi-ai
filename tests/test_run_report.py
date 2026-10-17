"""`evi run`'s run report, stream-json and resume flags on one versioned event
schema (loop-run-report).

The director could not tell whether a run finished, ran out of turns or was
blocked, or what it changed: `--format json` exited 0 on an error and
max_turns was fixed at 12. Now the run report (evi.headless, schema_version
1) names the session, why the run stopped, every tool call (redacted), the
denied tools, the summed usage and the files changed; the exit code follows
the stop reason; `--format stream-json` prints the run's events as NDJSON,
the same versioned events as the web UI's SSE stream (evi/events.py), and
both validate against the one schema checked in there (`events.SCHEMA`).

Acceptance criteria, each with its test below:
1. stream-json is NDJSON that parses line by line, seq increasing, a final
   result line — test_stream_json_is_ndjson_with_increasing_seq_and_a_result_line
2. `evi run --cwd <trusted tmp> -y 'create a.txt'` creates it and lists it in
   files_changed — test_run_in_a_trusted_cwd_creates_the_file_and_lists_it
3. max_turns: stop_reason max_turns and exit 3 in every format —
   test_hitting_max_turns_is_stop_reason_max_turns_and_exit_3
4. --resume continues a tool-using session against a strict backend —
   test_resume_continues_a_tool_using_session_on_a_strict_backend
5. SSE and stream-json validate against the same schema —
   test_sse_and_stream_json_send_the_same_events_on_one_schema
6. A secret planted in a tool argument is redacted in the report —
   test_a_secret_in_a_tool_argument_is_redacted_in_the_report

Agents run against the shared fake_llm client (tests/conftest.py); eVi's home,
config, transcripts and trust list are under tmp_path, never ~/.evi.
"""

from __future__ import annotations

import contextlib
import dataclasses
import itertools
import json
import os
import shutil
import subprocess
import typing
from pathlib import Path

import jsonschema  # not skipped when missing: CI has it (the mcp extra)
import pytest

import evi.tools.fs  # noqa: F401  registers write_file
from evi import events, headless, hooks as hooks_mod, redact
from evi.citations import Citation
from evi.config import Config
from evi.headless import ChangeTracker, HeadlessResult, run_headless, to_json
from evi.llm.agent import (
    Agent,
    CheckResult,
    ChecksFailed,
    ChecksSkipped,
    Done,
    Error,
    Event,
    FilesChanged,
    Guardrail,
    LogProbs,
    RouteInfo,
    Steering,
    StopBlocked,
    TextDelta,
    ThinkingDelta,
    ToolCall,
    ToolProgress,
    ToolResult,
    UsageStats,
)
from evi.tools.base import REGISTRY, Tool
from evi.transcripts import TranscriptStore
from tests._live_web import UI, sse_events, web_app
from tests.conftest import llm_call, llm_text
from tests.test_history_validity import _strict_check

ENVELOPE = ("schema_version", "seq", "ts")


# ---- helpers -----------------------------------------------------------------


@pytest.fixture
def home(monkeypatch, tmp_path):
    """eVi's home, config, transcripts, backends and trust list under
    tmp_path, no safe mode, and a process cwd of its own."""
    import evi.config as config_mod
    import evi.sessions as sessions_mod

    evi_home = tmp_path / "evi-home"
    evi_home.mkdir()
    monkeypatch.delenv("EVI_SAFE_MODE", raising=False)
    monkeypatch.setenv("EVI_HOME", str(evi_home))
    monkeypatch.setattr(config_mod, "HOME", evi_home)
    monkeypatch.setattr(config_mod, "CONFIG_PATH", evi_home / "config.toml")
    monkeypatch.setattr(config_mod, "ensure_dirs", lambda: None)
    monkeypatch.setattr(hooks_mod, "HOOKS_CONFIG_PATH", evi_home / "hooks.toml")
    monkeypatch.setattr("evi.guardrails.GUARDRAILS_PATH", evi_home / "guardrails.toml")
    monkeypatch.setattr("evi.plugins.plugin_dirs", lambda root=None: [])
    monkeypatch.setattr(sessions_mod, "TRANSCRIPTS_DIR", evi_home / "transcripts")
    monkeypatch.setattr(sessions_mod, "HANDOFF_DIR", evi_home / "handoff")
    monkeypatch.setattr("evi.backends.registry.BACKENDS_PATH", evi_home / "backends.json")
    (evi_home / "config.toml").write_text(
        "[tools]\nmemory = false\nskills = false\ntranscripts = false\n", encoding="utf-8")
    elsewhere = tmp_path / "process-cwd"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    return evi_home


def _echo() -> Tool:
    return Tool(name="echo", description="echo its argument", category="fs",
                parameters={"type": "object", "properties": {"x": {"type": "string"}}},
                func=lambda x="": f"echo:{x}")


def _agent(client, tools=(), **kw) -> Agent:
    return Agent(client=client, config=Config.load(), tools=list(tools), **kw)


def _cli(monkeypatch, build, *args: str, stdin: str | None = None):
    from typer.testing import CliRunner

    import evi.apps.cli.main as cli_main

    monkeypatch.setattr(cli_main, "_build_agent", build)
    return CliRunner().invoke(cli_main.app, ["run", *args], input=stdin)


def _builds(*agents: Agent):
    """A `_build_agent` that hands out `agents` in order, recording its kwargs."""
    queue = list(agents)
    calls: list[dict] = []

    def build(*_a, **k):
        calls.append(k)
        return queue.pop(0)

    build.calls = calls
    return build


def _validate_event(frame: dict) -> None:
    jsonschema.Draft202012Validator(events.SCHEMA).validate(frame)


def _validate_report(report: dict) -> None:
    jsonschema.Draft202012Validator(events.report_schema()).validate(report)


def _lines(stdout: str) -> list[dict]:
    """Every stdout line as JSON: fails on any line that isn't."""
    return [json.loads(line) for line in stdout.splitlines()]


def _strip(frame: dict) -> dict:
    return {k: v for k, v in frame.items() if k not in ENVELOPE}


class _Fake:
    def __init__(self, evs):
        self._events = evs

    def chat(self, prompt, max_turns=12):
        yield from self._events


# ---- the schema ----------------------------------------------------------------


def test_the_schema_is_valid_and_versioned() -> None:
    jsonschema.Draft202012Validator.check_schema(events.SCHEMA)
    jsonschema.Draft202012Validator.check_schema(events.report_schema())
    assert events.SCHEMA_VERSION == 1 and events.SCHEMA["$id"].endswith(":v1")
    assert events.schema() == events.SCHEMA and events.schema() is not events.SCHEMA


def _sample(cls):
    return {
        TextDelta: TextDelta("hi"), ThinkingDelta: ThinkingDelta("hm"),
        ToolCall: ToolCall("read_file", '{"path": "a"}'),
        ToolResult: ToolResult("read_file", "data", [Citation("1", "file", "a", "x", 1, 2)]),
        ToolProgress: ToolProgress(["read_file"], 2.0), UsageStats: UsageStats(1, 2, 3),
        LogProbs: LogProbs([{"token": "a", "logprob": -0.1}], -0.1, -0.1, 0),
        Guardrail: Guardrail("input", True, ["r"], [], "blocked"),
        RouteInfo: RouteInfo("m", "default"), Steering: Steering("also this"),
        Done: Done("stop"), Error: Error("boom"),
        CheckResult: CheckResult("pytest", "python -m pytest -q t.py", 1, 812, "1 failed", 1,
                                 "pytest"),
        ChecksSkipped: ChecksSkipped("not a trusted folder"),
        ChecksFailed: ChecksFailed(1, 2, ["pytest"]),
        StopBlocked: StopBlocked("pytest-gate", "tests fail", 1),
        FilesChanged: FilesChanged(["calc.py"]),
    }[cls]


def test_every_agent_event_kind_and_field_is_in_the_schema() -> None:
    """A new event or field must come with a schema change (and a version
    bump if it renames or drops one)."""
    stream = events.EventStream()
    kinds = typing.get_args(Event)
    assert len(kinds) == 17
    for cls in kinds:
        definition = events.SCHEMA["$defs"][cls.__name__]
        props = definition["allOf"][1]["properties"]
        assert {f.name for f in dataclasses.fields(cls)} <= set(props), cls.__name__
        frame = stream.frame(_sample(cls))
        _validate_event(frame)
        assert frame["kind"] == cls.__name__
    assert [stream.frame(Done("x"))["seq"] for _ in range(2)] == [18, 19]


def test_a_frame_that_breaks_the_contract_does_not_validate() -> None:
    good = events.EventStream().frame(ToolCall("t", "{}"))
    for bad in (
        {**good, "schema_version": 2},
        {**good, "seq": 0},
        {k: v for k, v in good.items() if k != "ts"},
        {**good, "kind": "Unknown"},
        {k: v for k, v in good.items() if k != "arguments"},
    ):
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.Draft202012Validator(events.SCHEMA).validate(bad)


def _isolated_report() -> dict:
    """A report as `evi run --worktree` leaves it: the worktree field is what
    evi.worktree.worktree_report returns."""
    res = HeadlessResult(text="done", stop_reason="done")
    res.worktree = {
        "path": "/repo/.worktrees/fix-1", "branch": "fix-1", "created": True,
        "base": "a" * 40, "head": "b" * 40,
        "commits": [{"sha": "b" * 40, "subject": "add x"}],
        "dirty": True, "dirty_files": ["?? x.txt"],
        "diffstat": {"files": 2, "insertions": 3, "deletions": 0},
    }
    return res.to_dict()


def test_the_report_schema_describes_the_worktree_field() -> None:
    """`worktree` is always present: null for a run that wasn't isolated, else
    every key of worktree_report, each of the type it has there."""
    _validate_report(HeadlessResult(text="x", stop_reason="done").to_dict())  # "worktree": null
    good = _isolated_report()
    _validate_report(good)
    wt = good["worktree"]
    # What git couldn't tell is null (a detached HEAD has no branch).
    _validate_report({**good, "worktree": {
        **wt, "branch": None, "head": None, "commits": None, "dirty": None,
        "dirty_files": None, "diffstat": None}})
    for bad_wt in (
        {k: v for k, v in wt.items() if k != "diffstat"},  # a key missing
        {**wt, "dirty": "yes"},
        {**wt, "created": None},  # never unknown
        {**wt, "base": None},
        {**wt, "commits": [{"sha": "b" * 40}]},  # a commit without its subject
        {**wt, "dirty_files": [1]},
        {**wt, "diffstat": {"files": 1, "insertions": -1, "deletions": 0}},
        {**wt, "diffstat": {"files": 1, "insertions": 1}},
        "fix-1",
    ):
        with pytest.raises(jsonschema.ValidationError):
            _validate_report({**good, "worktree": bad_wt})
    with pytest.raises(jsonschema.ValidationError):
        _validate_report({k: v for k, v in good.items() if k != "worktree"})
    # The RunResult event carries the same report.
    _validate_event(events.EventStream().frame(headless.result_payload(
        HeadlessResult(text="done", stop_reason="done", worktree=wt))))


def test_the_report_carries_checks_stop_blocks_and_denial_reasons() -> None:
    """loop-verify-gate / loop-implementer-policy: the verify checks, the
    stop-hook blocks and why no check ran are in the report; a denied call
    carries its reason (schema-additive: denied_tools stays the names)."""
    res = run_headless(_Fake([
        ToolCall("run_command", '{"command": "curl x"}'),
        ToolResult("run_command", "PERMISSION DENIED: run_command was not run: 'curl x' is "
                                  "not allowed (the implementer policy ...)."),
        TextDelta("fixed"),
        CheckResult("pytest", "python -m pytest -q t.py", 1, 812, "1 failed", 1, "pytest"),
        ChecksFailed(1, 2, ["pytest"]),
        TextDelta("fixed again"),
        CheckResult("pytest", "python -m pytest -q t.py", 0, 640, "1 passed", 2, "pytest"),
        StopBlocked("gate", "run the linter too", 2),
        TextDelta("done"),
        ChecksSkipped("not a trusted folder"),
        Done("stop"),
    ]), "go")
    report = res.to_dict()
    _validate_report(report)
    assert [(c["round"], c["exit"]) for c in report["checks"]] == [(1, 1), (2, 0)]
    assert report["stop_blocks"] == [{"hook": "gate", "reason": "run the linter too",
                                      "iteration": 2}]
    assert report["checks_skipped"] == "not a trusted folder"
    assert report["denied_tools"] == ["run_command"]
    [call] = report["tool_calls"]
    assert call["reason"].startswith("run_command was not run: 'curl x' is not allowed")
    assert res.turns_used == 4  # the gate sent the model back twice
    good = HeadlessResult(text="done", stop_reason="done").to_dict()
    for bad in (
        {**good, "checks": [{"name": "pytest"}]},
        {**good, "checks": [{**report["checks"][0], "round": 0}]},
        {**good, "stop_blocks": [{"hook": "gate", "reason": "x"}]},
        {**good, "checks_skipped": 3},
        {k: v for k, v in good.items() if k != "stop_blocks"},
        {**good, "stop_reason": "checks_passed"},
    ):
        with pytest.raises(jsonschema.ValidationError):
            _validate_report(bad)
    _validate_report({**good, "stop_reason": "checks_failed"})


# ---- stop reasons and exit codes (run_headless) ----------------------------------


@pytest.mark.parametrize(("done", "stop", "code"), [
    ("stop", "done", 0),
    ("length", "done", 0),
    ("max_turns_reached", "max_turns", 3),
    ("step_budget", "step_budget", 3),
    ("loop_detected", "loop_detected", 3),
    ("permission_denied", "permission_denied", 4),
    ("guardrail_blocked", "guardrail", 4),
    ("hook_blocked", "guardrail", 4),
    ("checks_failed", "checks_failed", 5),
    ("stop_hook_exhausted", "stop_hook_exhausted", 6),
    ("interrupted", "interrupted", 130),
])
def test_each_done_reason_has_its_stop_reason_and_exit_code(done, stop, code) -> None:
    res = run_headless(_Fake([TextDelta("x"), Done(done)]), "go")
    assert (res.stop_reason, res.done_reason, res.exit_code) == (stop, done, code)
    report = res.to_dict()
    _validate_report(report)
    assert report["stop_reason"] in events.STOP_REASONS


def test_a_backend_error_is_stop_reason_error_and_exit_1() -> None:
    res = run_headless(_Fake([TextDelta("half a rep"), Error("LLM request failed: 500")]), "go")
    assert (res.stop_reason, res.exit_code) == ("error", 1)
    assert res.error == "LLM request failed: 500" and res.turns_used == 1
    _validate_report(res.to_dict())


def test_a_finished_run_with_a_denied_call_is_done_and_lists_it(home, fake_llm) -> None:
    """Unattended, nothing to approve with: the model's run_command is denied,
    it answers anyway. The run is done (exit 0: the loop went on past the
    denial) and the report says what it could not do."""
    import evi.tools.shell  # noqa: F401  registers run_command

    agent = _agent(fake_llm([llm_call(("run_command", {"command": "echo hi"})),
                             llm_text("could not run it")]),
                   [REGISTRY["run_command"]], unattended_policy="deny")
    res = run_headless(agent, "go")
    assert (res.stop_reason, res.done_reason, res.exit_code) == ("done", "stop", 0)
    assert res.denied_tools == ["run_command"]
    [call] = res.tool_calls
    assert call["status"] == "denied" and call["output"].startswith("PERMISSION DENIED")
    assert res.text == "could not run it" and res.turns_used == 2
    _validate_report(res.to_dict())


def test_usage_is_summed_and_turns_counted_over_the_run() -> None:
    res = run_headless(_Fake([
        TextDelta("looking"), UsageStats(10, 5, 15), ToolCall("echo", '{"x": "1"}'),
        ToolResult("echo", "echo:1"), UsageStats(20, 5, 25), TextDelta("done"), Done("stop"),
    ]), "go")
    assert res.usage == {"prompt": 30, "completion": 10, "total": 40}
    assert res.turns_used == 2
    [call] = res.tool_calls
    assert call["name"] == "echo" and call["args"] == '{"x": "1"}' and call["status"] == "ok"
    assert isinstance(call["duration_ms"], int) and call["duration_ms"] >= 0


def test_the_report_names_the_session_backend_model_and_transcript(home, fake_llm, tmp_path):
    store = TranscriptStore(root=tmp_path / "transcripts")
    agent = _agent(fake_llm([llm_text("hello")]), transcripts=store)
    res = run_headless(agent, "hi")
    assert res.session_id == agent.session_id
    assert (res.backend, res.model) == (agent.config.llm.backend, agent.config.llm.model)
    assert Path(res.transcript_path).is_file()
    assert Path(res.transcript_path).parent.parent == tmp_path / "transcripts"
    assert res.files_changed is None  # not asked to track
    assert res.policy == "deny"  # no one to ask: [auto] unattended_policy
    _validate_report(res.to_dict())


# ---- acceptance 1: stream-json ---------------------------------------------------


def test_stream_json_is_ndjson_with_increasing_seq_and_a_result_line(home, monkeypatch,
                                                                     fake_llm):
    agent = _agent(fake_llm([llm_call(("echo", {"x": "1"})), llm_text("all done")]), [_echo()])
    r = _cli(monkeypatch, _builds(agent), "--format", "stream-json", "go")
    assert r.exit_code == 0, r.output
    frames = _lines(r.stdout)  # every line parses on its own
    assert [f["seq"] for f in frames] == list(range(1, len(frames) + 1))
    assert [f["ts"] for f in frames] == sorted(f["ts"] for f in frames)
    kinds = [f["kind"] for f in frames]
    assert kinds[0] == "RunStart" and kinds[-1] == "RunResult"
    assert {"ToolCall", "ToolResult", "TextDelta", "Done"} <= set(kinds)
    for frame in frames:
        _validate_event(frame)
    start, result = frames[0], frames[-1]
    assert start["session_id"] == agent.session_id == result["session_id"]
    assert start["max_turns"] == 12 and start["resumed"] is False
    assert result["stop_reason"] == "done" and result["text"] == "all done"
    assert result["tool_calls"][0]["output"] == "echo:1"


def test_stream_json_ends_with_a_result_line_on_sigint(home, monkeypatch):
    """Ctrl+C before the turn starts (building the agent): the stream still
    ends with RunResult, stop_reason interrupted, exit 130."""
    def build(*_a, **_k):
        raise KeyboardInterrupt

    r = _cli(monkeypatch, build, "--format", "stream-json", "go")
    assert r.exit_code == 130
    [frame] = _lines(r.stdout)
    _validate_event(frame)
    assert (frame["kind"], frame["seq"], frame["stop_reason"]) == ("RunResult", 1, "interrupted")


# ---- acceptance 5: SSE and stream-json, one schema --------------------------------


def test_sse_and_stream_json_send_the_same_events_on_one_schema(
    monkeypatch, tmp_path, fake_llm
) -> None:
    from fastapi.testclient import TestClient

    def script():
        return fake_llm([llm_call(("echo", {"x": "1"})), llm_text("all done")])

    web_agent = Agent(client=script(), config=Config(), tools=[_echo()], transcripts=None)
    c = TestClient(web_app(monkeypatch, tmp_path, web_agent))
    sse = sse_events(c.post("/api/chat", json={"session_id": "s", "message": "go"}).text)
    assert [f["seq"] for f in sse] == list(range(1, len(sse) + 1))
    for frame in sse:
        _validate_event(frame)

    monkeypatch.chdir(tmp_path)
    cli_agent = Agent(client=script(), config=Config(), tools=[_echo()], transcripts=None)
    r = _cli(monkeypatch, _builds(cli_agent), "--format", "stream-json", "go")
    assert r.exit_code == 0, r.output
    ndjson = _lines(r.stdout)
    for frame in ndjson:
        _validate_event(frame)

    def turn(frames):
        return [_strip(f) for f in frames
                if f["kind"] not in ("RunStart", "RunResult", "ToolProgress")]

    assert turn(sse) == turn(ndjson)
    kinds = [f["kind"] for f in turn(sse) if f["kind"] != "RouteInfo"]
    assert [k for k, _ in itertools.groupby(kinds)] == ["ToolCall", "ToolResult", "TextDelta",
                                                        "Done"]


def test_the_web_servers_own_events_validate_too(monkeypatch, tmp_path, fake_llm) -> None:
    """PermissionRequest / PermissionExpired (a prompt nobody answers), a
    slash command's SystemMessage, a re-roll with nothing to re-roll."""
    from fastapi.testclient import TestClient

    import evi.tools.shell  # noqa: F401  registers run_command

    agent = Agent(client=fake_llm([llm_call(("run_command", {"command": "echo hi"})),
                                   llm_text("ok")]),
                  config=Config(), tools=[REGISTRY["run_command"]], transcripts=None)
    c = TestClient(web_app(monkeypatch, tmp_path, agent))
    monkeypatch.setenv("EVI_DECISION_TIMEOUT_S", "0.2")
    # Every session shares `agent`: re-roll first, while it has no history.
    c.post("/api/chat", json={"session_id": "fresh", "message": "/help"})  # opens it
    noop = sse_events(c.post("/api/session/fresh/reroll", headers=UI).text)
    asked = sse_events(c.post("/api/chat", json={"session_id": "p", "message": "go"}).text)
    slash = sse_events(c.post("/api/chat", json={"session_id": "p", "message": "/help"}).text)
    assert {"PermissionRequest", "PermissionExpired"} <= {f["kind"] for f in asked}
    assert [f["kind"] for f in slash] == ["SystemMessage", "Done"]
    assert [(f["kind"], f["seq"]) for f in noop] == [("Error", 1), ("Done", 2)]
    for frame in asked + slash + noop:
        _validate_event(frame)


# ---- acceptance 2: --cwd, files_changed --------------------------------------------


def test_run_in_a_trusted_cwd_creates_the_file_and_lists_it(home, monkeypatch, tmp_path, fake_llm):
    from evi import trust

    work = tmp_path / "work"
    work.mkdir()
    (work / "keep.txt").write_text("untouched\n", encoding="utf-8")
    trust.grant(work)
    before = Path.cwd()
    agent = _agent(fake_llm([
        llm_call(("write_file", {"path": "a.txt", "content": "hello\nworld\n"})),
        llm_text("created a.txt"),
    ]), [REGISTRY["write_file"]])
    r = _cli(monkeypatch, _builds(agent), "--cwd", str(work), "-y", "--format", "json",
             "create a.txt")
    assert r.exit_code == 0, r.output
    assert (work / "a.txt").read_text(encoding="utf-8") == "hello\nworld\n"
    report = json.loads(r.stdout)
    _validate_report(report)
    assert report["files_changed"] == [
        {"path": "a.txt", "status": "added", "added": 2, "deleted": 0}]
    assert Path(report["cwd"]) == work.resolve() and report["trust"] == "trusted"
    assert report["policy"] == "allow-all" and report["stop_reason"] == "done"
    assert Path.cwd() == before  # the process's folder is put back


def test_an_untrusted_cwd_still_runs_with_its_project_layer_off(home, monkeypatch, tmp_path,
                                                               fake_llm):
    work = tmp_path / "untrusted"
    work.mkdir()
    (work / ".evi.toml").write_text('[llm]\nmodel = "repo-model"\n', encoding="utf-8")
    seen: list[str] = []

    def build(*_a, **_k):
        cfg = Config.load()  # what the real builder reads, from the run's folder
        seen.append(cfg.llm.model)
        return Agent(client=fake_llm([llm_text("hi")]), config=cfg, tools=[])

    r = _cli(monkeypatch, build, "--cwd", str(work), "--format", "json", "hi")
    assert r.exit_code == 0, r.output
    assert seen and seen[0] != "repo-model"
    assert json.loads(r.stdout)["trust"] == "untrusted"


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid",
                    "-c", "commit.gpgsign=false", "-c", "init.defaultBranch=main", *args],
                   cwd=cwd, check=True, capture_output=True, timeout=60)


@pytest.fixture
def repo(tmp_path):
    if shutil.which("git") is None:
        pytest.skip("git not installed")
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    (root / "keep.txt").write_text("one\ntwo\n", encoding="utf-8")
    (root / "gone.txt").write_text("bye\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "start")
    (root / "dirty.txt").write_text("already here\n", encoding="utf-8")  # dirty before
    return root


def test_files_changed_in_a_git_work_tree_are_what_the_run_did(repo) -> None:
    tracker = ChangeTracker.start(repo)
    (repo / "keep.txt").write_text("one\nTWO\nthree\n", encoding="utf-8")
    (repo / "new.txt").write_text("fresh\n", encoding="utf-8")
    (repo / "gone.txt").unlink()
    assert tracker.changes() == [
        {"path": "gone.txt", "status": "deleted", "added": 0, "deleted": 1},
        {"path": "keep.txt", "status": "modified", "added": 2, "deleted": 1},
        {"path": "new.txt", "status": "added", "added": 1, "deleted": 0},
    ]  # dirty.txt was dirty before and untouched: not listed


def test_files_committed_during_the_run_are_listed(repo) -> None:
    tracker = ChangeTracker.start(repo)
    (repo / "keep.txt").write_text("one\ntwo\nthree\n", encoding="utf-8")
    _git(repo, "commit", "-q", "-am", "the run's commit")
    assert tracker.changes() == [
        {"path": "keep.txt", "status": "modified", "added": 1, "deleted": 0}]


def test_files_changed_outside_git_and_when_too_big(tmp_path, monkeypatch) -> None:
    folder = tmp_path / "plain"
    (folder / "node_modules").mkdir(parents=True)
    (folder / "old.txt").write_text("x\n", encoding="utf-8")
    tracker = ChangeTracker.start(folder)
    (folder / "old.txt").write_text("x\ny\n", encoding="utf-8")
    (folder / "node_modules" / "dep.js").write_text("skipped\n", encoding="utf-8")
    (folder / "bin.dat").write_bytes(b"\0\1\2")
    assert tracker.changes() == [
        {"path": "bin.dat", "status": "added", "added": None, "deleted": 0},
        {"path": "old.txt", "status": "modified", "added": None, "deleted": None},
    ]
    monkeypatch.setattr(ChangeTracker, "WALK_MAX_FILES", 1)
    (folder / "two.txt").write_text("2\n", encoding="utf-8")
    assert ChangeTracker.start(folder).changes() is None  # too many to say


# ---- acceptance 3: max_turns -------------------------------------------------------


@pytest.mark.parametrize("fmt", ["text", "json", "stream-json"])
def test_hitting_max_turns_is_stop_reason_max_turns_and_exit_3(home, monkeypatch, fake_llm, fmt):
    agent = _agent(fake_llm([llm_call(("echo", {"x": "1"})), llm_call(("echo", {"x": "2"}))]),
                   [_echo()])
    r = _cli(monkeypatch, _builds(agent), "--max-turns", "2", "--format", fmt, "loop")
    assert r.exit_code == 3, r.output
    assert len(agent.client.requests) == 2  # --max-turns reached the agent
    if fmt == "text":
        assert "max-turns" in r.stderr
        return
    report = _lines(r.stdout)[-1]
    assert report["stop_reason"] == "max_turns" and report["done_reason"] == "max_turns_reached"
    assert report["turns_used"] == report["max_turns"] == 2
    if fmt == "stream-json":
        _validate_event(report)
    else:
        _validate_report(report)


# ---- acceptance 4: --resume / --continue -----------------------------------------------


def _strict(fake_llm, turns):
    """fake_llm that rejects a request a strict OpenAI-compatible server would
    (tests/test_history_validity._strict_check), recording the rejections."""
    client = fake_llm(turns)
    completions = client.chat.completions
    scripted = completions.create
    client.rejected = []

    def create(**kw):
        try:
            _strict_check(kw["messages"])
        except Exception as exc:
            client.rejected.append(str(exc))
            raise
        return scripted(**kw)

    completions.create = create
    return client


def test_resume_continues_a_tool_using_session_on_a_strict_backend(home, monkeypatch, fake_llm):
    store = TranscriptStore(root=home / "transcripts")
    first = _agent(fake_llm([llm_call(("echo", {"x": "1"})), llm_text("first done")]),
                   [_echo()], transcripts=store)
    r = _cli(monkeypatch, _builds(first), "--format", "json", "echo one")
    assert r.exit_code == 0, r.output
    report = json.loads(r.stdout)
    sid, transcript = report["session_id"], Path(report["transcript_path"])
    assert transcript.is_file()

    strict = _strict(fake_llm, [llm_call(("echo", {"x": "2"})), llm_text("resumed fine")])
    second = _agent(strict, [_echo()], transcripts=TranscriptStore(root=home / "transcripts"))
    assert second.session_id != sid
    r = _cli(monkeypatch, _builds(second), "--resume", sid, "--format", "json", "and again")
    assert r.exit_code == 0, r.output
    resumed = json.loads(r.stdout)
    _validate_report(resumed)
    assert strict.rejected == []
    assert (resumed["session_id"], resumed["stop_reason"], resumed["text"]) == (
        sid, "done", "resumed fine")
    assert resumed["transcript_path"] == str(transcript)  # the same session file
    sent = strict.requests[0]["messages"]
    assert [m["role"] for m in sent[1:]] == ["user", "assistant", "tool", "assistant", "user"]
    assert sent[3]["tool_call_id"] == sent[2]["tool_calls"][0]["id"]
    assert "and again" in sent[-1]["content"]

    # --continue picks the most recent session: the same one.
    third = _agent(_strict(fake_llm, [llm_text("still here")]), [_echo()],
                   transcripts=TranscriptStore(root=home / "transcripts"))
    r = _cli(monkeypatch, _builds(third), "--continue", "--format", "stream-json", "more")
    assert r.exit_code == 0, r.output
    frames = _lines(r.stdout)
    assert frames[0]["resumed"] is True and frames[0]["session_id"] == sid
    assert frames[-1]["session_id"] == sid and third.client.rejected == []


@pytest.mark.parametrize("args", [
    ("--resume", "no-such-session"),
    ("--continue",),
    ("--resume", "x", "--continue"),
    ("--format", "yaml"),
    ("--max-turns", "0"),
    ("--backend", "nowhere"),
    ("--cwd", "does/not/exist"),
])
def test_usage_errors_exit_2_without_building_an_agent(home, monkeypatch, args) -> None:
    r = _cli(monkeypatch, lambda *a, **k: pytest.fail("no agent may be built"), *args, "go")
    assert r.exit_code == 2, r.output


def test_model_and_backend_reach_the_agent_builder(home, monkeypatch, fake_llm) -> None:
    from evi.backends.registry import BackendEntry, add_backend

    entry = BackendEntry(name="box", kind="openai_compat", base_url="http://127.0.0.1:9/v1")
    add_backend(entry)
    build = _builds(_agent(fake_llm([llm_text("hi")])))
    r = _cli(monkeypatch, build, "--model", "m-1", "--backend", "BOX", "--format", "json", "hi")
    assert r.exit_code == 0, r.output
    [kwargs] = build.calls
    assert kwargs["model"] == "m-1" and kwargs["backend"].name == "box"


def test_a_delegate_cli_agent_backend_is_refused_with_a_report(home, monkeypatch) -> None:
    from evi.backends.registry import BackendEntry, add_backend

    add_backend(BackendEntry(name="cx", kind="codex"))
    r = _cli(monkeypatch, lambda *a, **k: pytest.fail("no agent may be built"),
             "--backend", "cx", "--format", "json", "go")
    assert r.exit_code == 1
    report = json.loads(r.stdout)
    assert report["stop_reason"] == "error" and "allow_cli_agent_unattended" in report["error"]


# ---- acceptance 6: redaction ------------------------------------------------------------

SECRET = "Zq8vN2kLp4Xr7Tw1Ys5M-env"  # shapeless: only the env var's value catches it
TOKEN = "ghp_" + "a1B2c3D4e5F6g7H8i9J0" * 2  # a GitHub token shape


@pytest.fixture
def planted(monkeypatch):
    for name in list(os.environ):
        if redact._SECRET_NAME.search(name):
            monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ACME_API_KEY", SECRET)
    redact.refresh()
    yield
    redact.refresh()


def _no_prefix(text: str, secret: str) -> bool:
    return not any(secret[i:i + 8] in text for i in range(len(secret) - 7))


@pytest.mark.parametrize("fmt", ["json", "stream-json"])
def test_a_secret_in_a_tool_argument_is_redacted_in_the_report(home, monkeypatch, fake_llm,
                                                               planted, fmt):
    agent = _agent(fake_llm([llm_call(("echo", {"x": f"key={SECRET} tok={TOKEN}"})),
                             llm_text("echoed")]), [_echo()])
    r = _cli(monkeypatch, _builds(agent), "--format", fmt, "echo my key")
    assert r.exit_code == 0, r.output
    assert _no_prefix(r.stdout, SECRET) and _no_prefix(r.stdout, TOKEN)
    report = _lines(r.stdout)[-1]
    [call] = report["tool_calls"]
    assert json.loads(call["args"]) == {
        "x": "key=[REDACTED:ACME_API_KEY] tok=[REDACTED:github_token]"}
    assert call["output"] == "echo:key=[REDACTED:ACME_API_KEY] tok=[REDACTED:github_token]"
    assert report["redactions"]["ACME_API_KEY"] >= 2
    if fmt == "stream-json":
        tool_call = next(f for f in _lines(r.stdout) if f["kind"] == "ToolCall")
        assert "[REDACTED:ACME_API_KEY]" in tool_call["arguments"]


def test_arguments_are_redacted_before_they_are_cut(planted) -> None:
    args = json.dumps({"x": "z" * (headless.MAX_SHOWN - 12) + SECRET})
    res = run_headless(_Fake([ToolCall("echo", args), ToolResult("echo", "ok"), Done("stop")]),
                       "go")
    [call] = res.tool_calls
    assert len(call["args"]) == headless.MAX_SHOWN and _no_prefix(call["args"], SECRET)
    assert _no_prefix(to_json(res), SECRET)


# ---- text format, batch ---------------------------------------------------------------


def test_text_format_prints_the_answer_and_notes_denials_on_stderr(home, monkeypatch,
                                                                     fake_llm):
    import evi.tools.shell  # noqa: F401  registers run_command

    agent = _agent(fake_llm([llm_call(("run_command", {"command": "echo hi"})),
                             llm_text("I could not run it")]), [REGISTRY["run_command"]])
    r = _cli(monkeypatch, _builds(agent), "go")
    assert r.exit_code == 0
    assert r.stdout.strip() == "I could not run it"
    assert "denied: run_command" in r.stderr


def test_batch_lines_carry_the_run_report(home, monkeypatch, fake_llm, tmp_path) -> None:
    from typer.testing import CliRunner

    import evi.apps.cli.main as cli_main

    prompts = tmp_path / "prompts.jsonl"
    prompts.write_text('{"id": "a", "prompt": "one"}\n{"id": "b", "prompt": "two"}\n',
                       encoding="utf-8")
    agents = [_agent(fake_llm([llm_call(("echo", {"x": "1"})), llm_text("A")]), [_echo()]),
              _agent(fake_llm([llm_call(("echo", {"x": "2"}))]), [_echo()])]
    monkeypatch.setattr(cli_main, "_build_agent", _builds(*agents))
    r = CliRunner().invoke(cli_main.app, ["batch", str(prompts), "--max-turns", "1"])
    assert r.exit_code == 0, r.output
    rows = [json.loads(line) for line in r.stdout.splitlines() if line.startswith("{")]
    assert [(row["id"], row["stop_reason"]) for row in rows] == [
        ("a", "max_turns"), ("b", "max_turns")]
    for row in rows:
        _validate_report({k: v for k, v in row.items() if k not in ("id", "prompt")})
        assert row["files_changed"] is None and row["schema_version"] == 1


def test_a_batch_item_that_raises_says_stop_reason_error() -> None:
    from evi import batch

    def boom(_item):
        raise RuntimeError("nope")

    [row] = batch.run_batch([{"id": 1, "prompt": "x"}], boom)
    assert row["stop_reason"] == "error" and "nope" in row["error"]


def test_an_unfilled_result_says_nothing_and_exits_0() -> None:
    assert HeadlessResult().stop_reason is None and HeadlessResult().exit_code == 0
    assert headless.exit_code("something-new") == 1


# ---- review round 1 ----------------------------------------------------------------
# Denied calls in a finished run, failures outside the turn, Ctrl+C while the
# files are listed, redaction of errors and tool names, folders git ignores,
# eVi's own output files, the SDK's pre-report `tools`.


@pytest.mark.parametrize("fmt", ["text", "json", "stream-json"])
def test_a_denied_read_under_the_actions_config_still_exits_0(home, monkeypatch, fake_llm, fmt):
    """The evi-run action's config approves no tool ([auto] auto_approve = [],
    evi_run_config.py) and its step fails with evi's exit code: the model's
    read_file is denied, it answers anyway, and that must not fail the step."""
    (home / "config.toml").write_text(
        "[auto]\nauto_approve = []\n"
        "[tools]\nmemory = false\nskills = false\ntranscripts = false\n", encoding="utf-8")
    agent = _agent(fake_llm([llm_call(("read_file", {"path": "README.md"})),
                             llm_text("This repo says hello.")]), [REGISTRY["read_file"]])
    r = _cli(monkeypatch, _builds(agent), "--format", fmt, "what does this repo do?")
    assert r.exit_code == 0, r.output
    if fmt == "text":
        assert r.stdout.strip() == "This repo says hello."
        assert "denied: read_file" in r.stderr
        return
    report = _lines(r.stdout)[-1]
    assert (report["stop_reason"], report["denied_tools"]) == ("done", ["read_file"])
    assert report["tool_calls"][0]["status"] == "denied"
    assert report["text"] == "This repo says hello."


@pytest.mark.parametrize("fmt", ["text", "json", "stream-json"])
def test_an_exception_mid_turn_still_ends_with_the_report(home, monkeypatch, fake_llm, fmt):
    agent = _agent(fake_llm([]))

    def chat(prompt, **_kw):
        yield TextDelta("half an answer")
        raise RuntimeError(f"the tool layer broke near {TOKEN}")

    agent.chat = chat  # type: ignore[method-assign]
    r = _cli(monkeypatch, _builds(agent), "--format", fmt, "go")
    assert r.exit_code == 1, r.output
    assert _no_prefix(r.output, TOKEN) and "Traceback" not in r.output
    if fmt == "text":
        assert r.stdout == ""  # a reply cut short is not the answer
        assert "error: RuntimeError: the tool layer broke" in r.stderr
        return
    frames = _lines(r.stdout)
    report = frames[-1]
    if fmt == "stream-json":
        assert [f["kind"] for f in frames] == ["RunStart", "TextDelta", "RunResult"]
        _validate_event(report)
    else:
        assert len(frames) == 1
        _validate_report(report)
    assert (report["stop_reason"], report["session_id"]) == ("error", agent.session_id)
    assert report["error"] == "RuntimeError: the tool layer broke near [REDACTED:github_token]"
    assert report["text"] == "half an answer" and report["turns_used"] == 1


@pytest.mark.parametrize("fmt", ["text", "json", "stream-json"])
def test_a_failure_before_the_turn_still_reports(home, monkeypatch, fmt):
    """Building the agent fails (an MCP server, a backend client): the run
    still ends with its report, the error redacted, not a traceback."""
    def build(*_a, **_k):
        raise OSError(f"could not start the MCP server: token {TOKEN}")

    r = _cli(monkeypatch, build, "--format", fmt, "hi")
    assert r.exit_code == 1, r.output
    assert "Traceback" not in r.output and _no_prefix(r.output, TOKEN)
    if fmt == "text":
        assert r.stdout == "" and "error: OSError: could not start" in r.stderr
        return
    [report] = _lines(r.stdout)
    assert report["stop_reason"] == "error"
    assert report["error"] == "OSError: could not start the MCP server: token [REDACTED:github_token]"
    if fmt == "stream-json":
        assert report["kind"] == "RunResult"
        _validate_event(report)
    else:
        _validate_report(report)


def test_ctrl_c_while_the_files_are_listed_keeps_the_report(home, monkeypatch, fake_llm):
    """The turn finished; a Ctrl+C during the files_changed scan after it
    skips the scan, not the report."""
    def interrupted(self):
        raise KeyboardInterrupt

    monkeypatch.setattr(ChangeTracker, "changes", interrupted)
    agent = _agent(fake_llm([llm_text("all done")]))
    r = _cli(monkeypatch, _builds(agent), "--format", "json", "go")
    assert r.exit_code == 0, r.output
    report = json.loads(r.stdout)
    assert (report["stop_reason"], report["text"], report["session_id"]) == (
        "done", "all done", agent.session_id)
    assert report["files_changed"] is None


def test_ctrl_c_after_the_agent_is_built_keeps_its_identity(home, monkeypatch, fake_llm):
    import evi.llm.agent as agent_mod

    agent = _agent(fake_llm([llm_text("never sent")]))

    def interrupted(*_a, **_k):
        raise KeyboardInterrupt

    monkeypatch.setattr(agent_mod, "make_unattended", interrupted)
    r = _cli(monkeypatch, _builds(agent), "--format", "stream-json", "go")
    assert r.exit_code == 130, r.output
    [result] = _lines(r.stdout)
    _validate_event(result)
    assert (result["kind"], result["stop_reason"], result["session_id"]) == (
        "RunResult", "interrupted", agent.session_id)
    assert result["policy"] and result["max_turns"] == 12


def test_a_secret_in_a_backend_error_is_redacted_in_the_stream_and_the_report() -> None:
    stream = events.EventStream()
    frames: list[dict] = []
    failed = Error(f"LLM request failed: 401 at https://x.invalid/v1?key={TOKEN}")
    res = run_headless(_Fake([failed]), "go", on_event=lambda e: frames.append(stream.frame(e)))
    [frame] = frames
    assert frame["message"] == res.error
    assert "[REDACTED:github_token]" in res.error and _no_prefix(json.dumps(frame), TOKEN)
    guard = events.event_payload(Guardrail("output", True, ["r"], [], f"blocked {TOKEN}"))
    assert guard["message"] == "blocked [REDACTED:github_token]"


def test_the_web_streams_own_error_is_redacted_too(monkeypatch, tmp_path, fake_llm) -> None:
    """The server's closing Error (a turn that raised) goes through the same
    serializer as an agent's."""
    from fastapi.testclient import TestClient

    agent = Agent(client=fake_llm([]), config=Config(), tools=[], transcripts=None)

    def broken(*_a, **_k):
        raise RuntimeError(f"no model at https://x.invalid/?key={TOKEN}")

    agent.chat = broken  # type: ignore[method-assign]
    c = TestClient(web_app(monkeypatch, tmp_path, agent))
    [frame] = sse_events(c.post("/api/chat", json={"session_id": "b", "message": "x"}).text)
    _validate_event(frame)
    assert frame["kind"] == "Error" and "[REDACTED:github_token]" in frame["message"]
    assert _no_prefix(frame["message"], TOKEN)


def test_a_secret_in_a_tool_name_is_redacted_in_the_report_and_the_stream() -> None:
    stream = events.EventStream()
    lines: list[str] = []
    res = run_headless(_Fake([ToolCall(TOKEN, "{}"), ToolResult(TOKEN, "ERROR: unknown tool"),
                              Done("stop")]), "go", on_event=lambda e: lines.append(stream.dumps(e)))
    [call] = res.tool_calls
    assert (call["name"], call["status"]) == ("[REDACTED:github_token]", "error")
    assert call["duration_ms"] is not None  # the result still found its call
    assert _no_prefix(to_json(res), TOKEN) and _no_prefix("\n".join(lines), TOKEN)


@pytest.mark.parametrize("ignore", ["scratch/", "*"])
def test_files_changed_in_a_folder_git_ignores_are_walked(repo, ignore) -> None:
    """A scratch folder a repo ignores, or a home directory that is a dotfiles
    repo ignoring everything: `git status` would show no new file, so the
    folder is walked instead of reporting nothing."""
    (repo / ".gitignore").write_text(ignore + "\n", encoding="utf-8")
    root = repo / "scratch" if ignore == "scratch/" else repo
    root.mkdir(exist_ok=True)
    tracker = ChangeTracker.start(root)
    assert tracker.top is None
    (root / "a.txt").write_text("one\n", encoding="utf-8")
    assert tracker.changes() == [{"path": "a.txt", "status": "added", "added": 1, "deleted": 0}]


@contextlib.contextmanager
def _stdout_into(path: Path):
    """Point this process's stdout (fd 1) at `path` meanwhile, as `> out.txt`."""
    with open(path, "ab") as f:
        saved = os.dup(1)
        os.dup2(f.fileno(), 1)
        try:
            yield
        finally:
            os.dup2(saved, 1)
            os.close(saved)


@pytest.mark.parametrize("in_git", [False, True])
def test_the_file_stdout_goes_to_is_not_a_change(request, tmp_path, in_git) -> None:
    """`evi run --format stream-json ... > out.txt` in the run's folder: the
    report going there is not a change the run made."""
    root = request.getfixturevalue("repo") if in_git else tmp_path / "plain"
    root.mkdir(exist_ok=True)
    out = root / "out.txt"
    out.write_text('{"kind": "RunStart"}\n', encoding="utf-8")
    with _stdout_into(out):
        tracker = ChangeTracker.start(root)
        os.write(1, b'{"kind": "TextDelta"}\n')
        (root / "made.txt").write_text("x\n", encoding="utf-8")
        changes = tracker.changes()
    assert out.read_text(encoding="utf-8").endswith('"TextDelta"}\n')  # it did go there
    assert [c["path"] for c in changes] == ["made.txt"]


@pytest.mark.parametrize("where", ["home", "transcripts"])
def test_evis_own_files_in_the_run_folder_are_not_changes(home, monkeypatch, tmp_path, fake_llm,
                                                          where) -> None:
    """eVi's home inside the run's folder (`evi run` from ~ with ~/.evi), or
    the agent's transcripts there: eVi writes them during the run."""
    import evi.config as config_mod

    work = tmp_path / "work"
    work.mkdir()
    transcripts = None
    if where == "home":
        monkeypatch.setattr(config_mod, "HOME", work / ".evi")
    else:
        transcripts = TranscriptStore(root=work / "logs" / "transcripts")

    def remember(x: str = "") -> str:  # writes eVi's home, as the memory tool does
        notes = config_mod.HOME / "memory" / "notes.md"
        notes.parent.mkdir(parents=True, exist_ok=True)
        notes.write_text(x, encoding="utf-8")
        return "remembered"

    tools = [REGISTRY["write_file"], Tool(name="remember", description="remember x",
                                          category="memory", func=remember,
                                          parameters={"type": "object",
                                                      "properties": {"x": {"type": "string"}}})]
    agent = _agent(fake_llm([
        llm_call(("write_file", {"path": "a.txt", "content": "hi\n"}), ("remember", {"x": "a"})),
        llm_text("ok"),
    ]), tools, transcripts=transcripts)
    r = _cli(monkeypatch, _builds(agent), "--cwd", str(work), "-y", "--format", "json", "go")
    assert r.exit_code == 0, r.output
    report = json.loads(r.stdout)
    if where == "home":
        assert (work / ".evi" / "memory" / "notes.md").is_file()
    else:
        assert Path(report["transcript_path"]).is_relative_to(work.resolve())
    assert report["files_changed"] == [
        {"path": "a.txt", "status": "added", "added": 1, "deleted": 0}]


def test_the_pre_report_tools_keyword_and_property_still_work() -> None:
    """evi.sdk re-exports HeadlessResult: `tools=` and `.tools` (the tool
    outputs as [{name, output}]) keep working over `tool_calls`."""
    old = [{"name": "t", "output": "o"}, {"name": "d", "output": "PERMISSION DENIED: no"}]
    res = HeadlessResult(text="hi", tools=old, stop_reason="done")
    assert res.tools == old
    assert [(c["name"], c["status"], c["output"]) for c in res.tool_calls] == [
        ("t", "ok", "o"), ("d", "denied", "PERMISSION DENIED: no")]
    _validate_report(res.to_dict())
    res.tools = [{"name": "x", "output": "y"}]
    assert [c["name"] for c in res.tool_calls] == ["x"]
    assert dataclasses.replace(res).tool_calls == res.tool_calls
    assert "tools" not in {f.name for f in dataclasses.fields(HeadlessResult)}
    assert HeadlessResult().tools == [] and HeadlessResult().tool_calls == []
