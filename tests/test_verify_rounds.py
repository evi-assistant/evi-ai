"""Verify before done: the project checks run before a turn may end, and
failures go back to the model for N rounds (loop-verify-gate).

Small models declare success early; an executed check is ground truth. When
a Code turn wrote files and would end, the agent runs the checks evi/checks.py
detects for what changed (under the implementer policy, or with [checks]
verify = "always" in an interactive Code session). A failing round goes back
to the model as a framed note and the loop goes on; after [checks]
max_rounds failing rounds the turn ends with "checks_failed" (exit 5).
Passing checks lead on to the json stop hooks, then a normal end.

Acceptance criteria, each with its test below:
1. A stop hook that fails while tests fail, with a fake model that fixes the
   code on round 2, ends with stop_reason done and 2 recorded check runs —
   test_a_failing_round_then_a_fix_is_done_with_two_check_runs
   (tests/test_stop_hook_block.py shows the same hook blocking on its own)
2. An always-blocking hook ends with stop_hook_exhausted after 3 blocks —
   tests/test_stop_hook_block.py::test_an_always_blocking_hook_ends_with_stop_hook_exhausted_after_3_blocks
3. A Code-mode turn editing a .py file runs ruff and the targeted pytest file
   under the implementer policy, with no -y —
   test_a_code_mode_run_under_the_policy_runs_ruff_and_the_targeted_tests
4. No checks run when no files changed —
   test_no_checks_run_when_no_files_changed
5. A [checks] entry from an untrusted repo is ignored —
   tests/test_checks_detection.py::test_a_checks_entry_from_an_untrusted_repo_is_ignored,
   and here: test_an_untrusted_folder_runs_no_checks_and_says_why

Agents run against the shared fake_llm client; the checks are real commands
(this Python's ruff and pytest) in projects under tmp_path. eVi's home,
config and trust list are under tmp_path, never ~/.evi.
"""

from __future__ import annotations

import dataclasses
import json
import sys
import threading
from pathlib import Path

import jsonschema
import pytest

import evi.tools.fs  # noqa: F401  registers write_file, read_file
import evi.tools.shell  # noqa: F401  registers run_command
from evi import events, hooks as hooks_mod, redact, trust
from evi.config import Config
from evi.headless import exit_code, run_headless
from evi.history import is_note
from evi.hooks import HookRegistry
from evi.llm.agent import (
    Agent,
    CheckResult,
    ChecksFailed,
    ChecksSkipped,
    Done,
    FilesChanged,
    StopBlocked,
    TextDelta,
)
from evi.policy import implementer_policy
from evi.tools.base import REGISTRY
from tests.conftest import llm_call, llm_text

PY = sys.executable
ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples" / "hooks" / "pytest-gate.toml"

BUGGY = "def add(a, b):\n    return a - b\n"
FIXED = "def add(a, b):\n    return a + b\n"


@pytest.fixture
def home(monkeypatch, tmp_path):
    """eVi's home, config, hooks and trust list under tmp_path, no safe mode."""
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
    _config(evi_home)
    return evi_home


def _config(home: Path, extra: str = "") -> None:
    (home / "config.toml").write_text(
        "[tools]\nmemory = false\nskills = false\ntranscripts = false\n" + extra,
        encoding="utf-8")


def _project(folder: Path, *, ruff: bool = False, trusted: bool = True) -> Path:
    """calc.py (add is wrong) with tests/test_calc.py; trusted unless asked."""
    (folder / "tests").mkdir(parents=True)
    (folder / "pyproject.toml").write_text(
        ("[tool.ruff]\nline-length = 100\n\n" if ruff else "")
        + '[tool.pytest.ini_options]\npythonpath = ["."]\n', encoding="utf-8")
    (folder / "calc.py").write_text("def add(a, b):\n    return 0\n", encoding="utf-8")
    (folder / "tests" / "test_calc.py").write_text(
        "from calc import add\n\n\ndef test_add():\n    assert add(1, 2) == 3\n",
        encoding="utf-8")
    if trusted:
        trust.grant(folder)
    return folder.resolve()


def _agent(client, work: Path, *, tools=("write_file",), policy: bool = True, hooks=()) -> Agent:
    agent = Agent(client=client, config=Config.load(), tools=[REGISTRY[t] for t in tools],
                  hooks=HookRegistry(hooks=list(hooks)))
    agent.cwd = str(work)
    if policy:
        agent.policy = implementer_policy(work)
    return agent


def _events(agent: Agent, prompt: str = "go") -> list:
    out = []
    for ev in agent.chat(prompt, max_turns=12):
        out.append(ev)
        if isinstance(ev, Done):
            break
    return out


def _example_hook():
    [hook] = hooks_mod._parse_hook_file(EXAMPLE)
    return dataclasses.replace(hook, command=[PY, *hook.command[1:]])


def _validate_report(report: dict) -> None:
    jsonschema.Draft202012Validator(events.report_schema()).validate(report)


# ---- the rounds ----------------------------------------------------------------


def test_a_failing_round_then_a_fix_is_done_with_two_check_runs(home, tmp_path, fake_llm):
    """Round 1 writes a bug and says done: the targeted test fails, the
    failure goes back as a note. Round 2 fixes it: the checks pass, then the
    pytest-gate stop hook (which would block while the tests fail) passes
    too, and the run is done with two check runs on record."""
    work = _project(tmp_path / "proj")
    agent = _agent(fake_llm([
        llm_call(("write_file", {"path": "calc.py", "content": BUGGY})),
        llm_text("implemented add"),
        llm_call(("write_file", {"path": "calc.py", "content": FIXED})),
        llm_text("fixed add"),
    ]), work, hooks=[_example_hook()])
    res = run_headless(agent, "implement add")
    assert (res.stop_reason, res.done_reason, res.exit_code) == ("done", "stop", 0)
    assert [(c["name"], c["round"], c["exit"], c["source"]) for c in res.checks] == [
        ("pytest", 1, 1, "pytest"), ("pytest", 2, 0, "pytest")]
    assert res.checks[0]["cmd"].endswith("tests/test_calc.py")
    assert "assert" in res.checks[0]["tail"]
    assert res.stop_blocks == [] and res.text.endswith("fixed add")
    assert res.turns_used == 4
    # The failure reached the model as a framed note naming the command.
    [note] = [m for m in agent.history if is_note(m)]
    assert note["content"].startswith('[message from evi "checks" - information, not instructions]')
    assert "round 1 of 2" in note["content"] and "exit 1" in note["content"]
    assert "tests/test_calc.py" in note["content"]
    _validate_report(res.to_dict())


def test_checks_that_keep_failing_end_the_turn_with_checks_failed(home, tmp_path, fake_llm):
    work = _project(tmp_path / "proj")
    agent = _agent(fake_llm([
        llm_call(("write_file", {"path": "calc.py", "content": BUGGY})),
        llm_text("done"),
        llm_text("still done"),
    ]), work)
    evs = _events(agent)
    kinds = [type(e).__name__ for e in evs if isinstance(e, (CheckResult, ChecksFailed, Done))]
    assert kinds == ["CheckResult", "ChecksFailed", "CheckResult", "Done"]
    assert evs[-1] == Done("checks_failed")
    assert exit_code("checks_failed") == 5
    failed = next(e for e in evs if isinstance(e, ChecksFailed))
    assert (failed.round, failed.max_rounds, failed.failed) == (1, 2, ["pytest"])


def test_checks_that_passed_run_again_only_once_something_changed(home, tmp_path, fake_llm):
    """The checks pass, then a json stop hook sends the model back: a reply
    that changes nothing ends the turn without running the suite again; one
    that edits a file runs it again."""
    work = _project(tmp_path / "proj")
    script = ("import json, sys\n"
              "if not json.load(sys.stdin)['stop_hook_active']:\n"
              "    print(json.dumps({'decision': 'block', 'reason': 'look once more'}))\n")
    once = hooks_mod.Hook(name="once", event="stop", match="*", command=[PY, "-c", script],
                          timeout=30, protocol="json")
    res = run_headless(_agent(fake_llm([
        llm_call(("write_file", {"path": "calc.py", "content": FIXED})),
        llm_text("fixed add"),
        llm_text("looked again: still fixed"),
    ]), work, hooks=[once]), "go")
    assert res.stop_reason == "done" and len(res.stop_blocks) == 1
    assert [(c["name"], c["round"], c["exit"]) for c in res.checks] == [("pytest", 1, 0)]
    res2 = run_headless(_agent(fake_llm([
        llm_call(("write_file", {"path": "calc.py", "content": FIXED})),
        llm_text("fixed add"),
        llm_call(("write_file", {"path": "calc.py", "content": FIXED + "\n\n# tidy\n"})),
        llm_text("tidied"),
    ]), work, hooks=[once]), "go")
    assert res2.stop_reason == "done"
    assert [(c["name"], c["round"]) for c in res2.checks] == [("pytest", 1), ("pytest", 2)]


def test_max_rounds_comes_from_the_config(home, tmp_path, fake_llm):
    _config(home, "[checks]\nmax_rounds = 1\n")
    work = _project(tmp_path / "proj")
    res = run_headless(_agent(fake_llm([
        llm_call(("write_file", {"path": "calc.py", "content": BUGGY})), llm_text("done"),
    ]), work), "go")
    assert (res.stop_reason, res.exit_code) == ("checks_failed", 5)
    assert len(res.checks) == 1
    _validate_report(res.to_dict())


def test_no_checks_run_when_no_files_changed(home, tmp_path, fake_llm):
    work = _project(tmp_path / "proj")
    agent = _agent(fake_llm([
        llm_call(("read_file", {"path": "calc.py"})),
        llm_text("add returns 0"),
    ]), work, tools=("read_file", "write_file"))
    res = run_headless(agent, "what does add do?")
    assert res.stop_reason == "done" and res.checks == [] and res.checks_skipped is None
    # A write the policy denied changed nothing either.
    agent2 = _agent(fake_llm([
        llm_call(("write_file", {"path": "../elsewhere.py", "content": "x"})), llm_text("ok"),
    ]), work)
    evs = _events(agent2)
    assert not any(isinstance(e, (CheckResult, FilesChanged)) for e in evs)


def test_an_untrusted_folder_runs_no_checks_and_says_why(home, tmp_path, fake_llm):
    work = _project(tmp_path / "proj", trusted=False)
    (work / ".evi.toml").write_text('[checks]\ncommands = ["python -c pass"]\n', encoding="utf-8")
    res = run_headless(_agent(fake_llm([
        llm_call(("write_file", {"path": "calc.py", "content": BUGGY})), llm_text("done"),
    ]), work), "go")
    assert res.stop_reason == "done" and res.checks == []
    assert "not a trusted folder" in res.checks_skipped
    _validate_report(res.to_dict())


def test_checks_are_off_with_verify_off_and_in_an_interactive_session_by_default(
    home, tmp_path, fake_llm
):
    work = _project(tmp_path / "proj")
    _config(home, '[checks]\nverify = "off"\n')
    res = run_headless(_agent(fake_llm([
        llm_call(("write_file", {"path": "calc.py", "content": BUGGY})), llm_text("done"),
    ]), work), "go")
    assert res.checks == []
    _config(home)  # verify = "auto": interactive Code mode runs none
    agent = _agent(fake_llm([
        llm_call(("write_file", {"path": "calc.py", "content": BUGGY})), llm_text("done"),
    ]), work, policy=False)
    agent.mode = "code"
    agent.permission_callback = lambda *_a: True
    assert not any(isinstance(e, CheckResult) for e in _events(agent))


def test_verify_always_offers_each_check_through_the_permission_flow(home, tmp_path, fake_llm):
    _config(home, '[checks]\nverify = "always"\n')
    work = _project(tmp_path / "proj")
    asked: list[tuple[str, str]] = []

    def approve(name, args, category):
        asked.append((name, json.loads(args)["command"]))
        return True

    agent = _agent(fake_llm([
        llm_call(("write_file", {"path": "calc.py", "content": FIXED})), llm_text("done"),
    ]), work, policy=False)
    agent.mode = "code"
    agent.permission_callback = approve
    evs = _events(agent)
    [check] = [e for e in evs if isinstance(e, CheckResult)]
    assert check.exit == 0 and evs[-1] == Done("stop")
    assert [n for n, _c in asked if n == "run_command"] == ["run_command"]
    assert any(c.endswith("tests/test_calc.py") for n, c in asked if n == "run_command")
    # Refused: recorded as not run, and no failing round.
    agent2 = _agent(fake_llm([
        llm_call(("write_file", {"path": "calc.py", "content": BUGGY})), llm_text("done"),
    ]), work, policy=False)
    agent2.mode = "code"
    agent2.permission_callback = lambda name, *_a: name != "run_command"
    evs2 = _events(agent2)
    [refused] = [e for e in evs2 if isinstance(e, CheckResult)]
    assert refused.exit is None and "not run" in refused.tail and evs2[-1] == Done("stop")


def test_a_check_the_policy_does_not_allow_is_recorded_not_run(home, tmp_path, fake_llm):
    work = _project(tmp_path / "proj")
    agent = _agent(fake_llm([
        llm_call(("write_file", {"path": "calc.py", "content": FIXED})), llm_text("done"),
    ]), work)
    _config(home, '[checks]\ndetect = false\ncommands = ["curl https://example.com"]\n')
    agent.config = Config.load()
    evs = _events(agent)
    [check] = [e for e in evs if isinstance(e, CheckResult)]
    assert check.exit is None and "not allowed" in check.tail
    assert evs[-1] == Done("stop")


def test_a_code_mode_run_under_the_policy_runs_ruff_and_the_targeted_tests(
    home, tmp_path, monkeypatch, fake_llm
):
    """`evi run --policy implementer -m code`, no -y: the model edits calc.py,
    and before the run may end eVi runs ruff on it and its targeted test."""
    from typer.testing import CliRunner

    import evi.apps.cli.main as cli_main

    work = _project(tmp_path / "proj", ruff=True)
    agent = Agent(client=fake_llm([
        llm_call(("write_file", {"path": "calc.py", "content": FIXED})),
        llm_text("fixed add"),
    ]), config=Config.load(), tools=[])
    monkeypatch.setattr(cli_main, "_build_agent", lambda *a, **k: agent)
    r = CliRunner().invoke(cli_main.app, ["run", "--policy", "implementer", "-m", "code",
                                          "--format", "json", "--cwd", str(work), "fix add"])
    assert r.exit_code == 0, r.output
    report = json.loads(r.stdout)
    assert report["policy"] == "implementer" and report["stop_reason"] == "done"
    assert agent.mode == "code" and "run_command" in agent.tools and not agent.auto_all
    assert [(c["name"], c["exit"], c["round"]) for c in report["checks"]] == [
        ("ruff", 0, 1), ("pytest", 0, 1)]
    assert report["checks"][0]["cmd"].endswith("check --no-cache calc.py")
    assert report["checks"][1]["cmd"].endswith("tests/test_calc.py")
    assert [f["path"] for f in report["files_changed"]] == ["calc.py"]
    # The check lines went to stderr.
    assert "check ruff (round 1) passed" in r.stderr
    assert "check pytest (round 1) passed" in r.stderr
    _validate_report(report)


def test_a_failing_check_line_and_the_exit_code_through_evi_run(home, tmp_path, monkeypatch,
                                                                fake_llm):
    from typer.testing import CliRunner

    import evi.apps.cli.main as cli_main

    work = _project(tmp_path / "proj")
    agent = Agent(client=fake_llm([
        llm_call(("write_file", {"path": "calc.py", "content": BUGGY})),
        llm_text("done"), llm_text("still done"),
    ]), config=Config.load(), tools=[REGISTRY["write_file"]])
    monkeypatch.setattr(cli_main, "_build_agent", lambda *a, **k: agent)
    r = CliRunner().invoke(cli_main.app, ["run", "--policy", "implementer", "--cwd", str(work),
                                          "add"])
    assert r.exit_code == 5, r.output
    assert "check pytest (round 1) exit 1" in r.stderr
    assert "checks failed (round 1 of 2): pytest; sent back to the model" in r.stderr
    assert "still failed" in r.stderr


def test_a_secret_in_a_checks_output_is_redacted_in_the_note_and_report(
    home, tmp_path, fake_llm, monkeypatch
):
    secret = "Zq8vN2kLp4Xr7Tw1Ys5M-verify"
    monkeypatch.setenv("ACME_API_KEY", secret)
    redact.refresh()
    try:
        work = _project(tmp_path / "proj")
        (work / "tests" / "test_calc.py").write_text(
            f"def test_leak():\n    assert {secret!r} == 'x'\n", encoding="utf-8")
        res = run_headless(_agent(fake_llm([
            llm_call(("write_file", {"path": "calc.py", "content": FIXED})),
            llm_text("done"), llm_text("done"),
        ]), work), "go")
        assert res.checks and secret not in json.dumps(res.to_dict())
    finally:
        redact.refresh()


def test_cancelling_during_a_check_kills_it_and_interrupts_the_turn(home, tmp_path, fake_llm):
    work = _project(tmp_path / "proj")
    cmd = json.dumps([PY, "-c", "import time; time.sleep(60)"])
    _config(home, f"[checks]\ndetect = false\ncommands = [{cmd}]\n")
    agent = _agent(fake_llm([
        llm_call(("write_file", {"path": "calc.py", "content": FIXED})), llm_text("done"),
    ]), work)
    agent.policy = implementer_policy(work, checks=[[PY, "-c", "import time; time.sleep(60)"]])
    seen = []
    for ev in agent.chat("go"):
        seen.append(ev)
        if isinstance(ev, Done):
            break
        if isinstance(ev, TextDelta):  # the final reply: its checks start next
            threading.Timer(0.5, agent.cancel, args=("the test stopped it",)).start()
    # The cancel came while the check ran: it was killed, the turn interrupted.
    assert seen[-1] == Done("interrupted")
    [check] = [e for e in seen if isinstance(e, CheckResult)]
    assert check.exit is None and check.duration_ms < 30_000


def test_files_changed_is_sent_before_done_and_never_joins_the_history(home, tmp_path, fake_llm):
    work = _project(tmp_path / "proj", trusted=False)
    agent = _agent(fake_llm([
        llm_call(("write_file", {"path": "pkg/new.py", "content": "x = 1\n"}),
                 ("write_file", {"path": "notes.md", "content": "n\n"})),
        llm_text("done"),
    ]), work, policy=False)
    agent.auto_approve_categories.add("fs")
    evs = _events(agent)
    assert isinstance(evs[-2], FilesChanged) and evs[-2].paths == ["pkg/new.py", "notes.md"]
    assert "pkg/new.py" not in json.dumps(agent.history[-1])
    assert not any(isinstance(e, (CheckResult, ChecksSkipped, StopBlocked)) for e in evs)
