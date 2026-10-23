"""Blocking stop hooks: the hook JSON decision protocol (loop-verify-gate).

A stop hook was notification-only, so a small model that declared success
early simply ended the turn. Now a `[[stop]]` hook with `protocol = "json"`
(evi/hooks.py) gets the event on stdin as JSON and may block the turn's end
by printing {"decision": "block", "reason": ...} or exiting 2; the reason
reaches the model as a framed note and the loop goes on, at most [hooks]
max_blocks (default 3) times per user turn, then Done("stop_hook_exhausted").

Acceptance criteria covered here (the rest of loop-verify-gate is in
tests/test_verify_rounds.py and tests/test_checks_detection.py):
- An always-blocking hook ends with stop_hook_exhausted after 3 blocks —
  test_an_always_blocking_hook_ends_with_stop_hook_exhausted_after_3_blocks
- A stop hook that fails while tests fail, with a fake model that fixes the
  code on round 2, ends with stop_reason done —
  test_the_pytest_gate_example_blocks_until_the_tests_pass (the shipped
  examples/hooks/pytest-gate.toml, checks off), and with the checks on,
  tests/test_verify_rounds.py::test_a_failing_round_then_a_fix_is_done_with_two_check_runs

Plus the protocol's edges: exit 2, errors and timeouts are no block, a plain
stop hook that exits 2 stays a notification, a json hook is not run again
when the turn ends, a url hook, the stdin payload, redaction of the reason,
and max_blocks from the config.

Hooks are real commands (this Python, -c); eVi's home is under tmp_path.
"""

from __future__ import annotations

import dataclasses
import json
import os
import sys
import threading
import tomllib
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import jsonschema
import pytest

import evi.tools.fs  # noqa: F401  registers write_file
from evi import events, hooks as hooks_mod, redact
from evi.config import Config
from evi.headless import exit_code, run_headless
from evi.history import is_note
from evi.hooks import Hook, HookRegistry, HookResult, stop_decision, validate
from evi.llm.agent import Agent, Done, StopBlocked
from evi.tools.base import REGISTRY
from tests.conftest import llm_call, llm_text

PY = sys.executable
ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples" / "hooks" / "pytest-gate.toml"


@pytest.fixture
def home(monkeypatch, tmp_path):
    """eVi's home, config, hooks and trust list under tmp_path, no safe mode."""
    import evi.config as config_mod

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
    _config(evi_home)
    return evi_home


def _config(home: Path, extra: str = "") -> None:
    (home / "config.toml").write_text(
        "[tools]\nmemory = false\nskills = false\ntranscripts = false\n" + extra,
        encoding="utf-8")


def _hook(script: str, *, name: str = "gate", protocol: str = "json", timeout: float = 30,
          event: str = "stop") -> Hook:
    return Hook(name=name, event=event, match="*", command=[PY, "-c", script],
                timeout=timeout, protocol=protocol)


def _agent(client, hooks: list[Hook], workdir: Path,
           tools=("read_file", "write_file")) -> Agent:
    agent = Agent(client=client, config=Config.load(), tools=[REGISTRY[t] for t in tools],
                  hooks=HookRegistry(hooks=list(hooks)))
    agent.cwd = str(workdir)
    return agent


def _events(agent: Agent, prompt: str = "go", **kw) -> list:
    out = []
    for ev in agent.chat(prompt, **kw):
        out.append(ev)
        if isinstance(ev, Done):
            break
    return out


BLOCK_ALWAYS = 'import json; print(json.dumps({"decision": "block", "reason": "tests fail"}))'


# ---- the protocol --------------------------------------------------------------


def test_protocol_json_is_a_stop_hook_option_only(tmp_path):
    ok = '[[stop]]\nname = "g"\nprotocol = "json"\ncommand = ["x"]\n'
    assert validate(ok) is None
    assert "only for [[stop]]" in validate(
        '[[before_tool_call]]\nname = "g"\nprotocol = "json"\ncommand = ["x"]\n')
    assert "protocol" in validate('[[stop]]\nname = "g"\nprotocol = "yaml"\ncommand = ["x"]\n')
    path = tmp_path / "hooks.toml"
    path.write_text(ok + '[[stop]]\nname = "plain"\ncommand = ["y"]\n', encoding="utf-8")
    hooks = hooks_mod._parse_hook_file(path)
    assert [(h.name, h.protocol) for h in hooks] == [("g", "json"), ("plain", "")]
    assert [h.name for h in HookRegistry(hooks=hooks).json_stop_hooks()] == ["g"]


@pytest.mark.parametrize(("res", "decision", "reason"), [
    (HookResult("g", 0, '{"decision": "block", "reason": "fix it"}', ""), "block", "fix it"),
    (HookResult("g", 0, 'checking...\n{"decision": "BLOCK", "reason": "late"}\n', ""),
     "block", "late"),
    (HookResult("g", 0, '{"decision": "block"}', ""), "block", "(no reason given)"),
    (HookResult("g", 2, "", "tests fail\n"), "block", "tests fail"),
    (HookResult("g", 0, "", ""), "allow", ""),
    (HookResult("g", 0, '{"decision": "allow"}', ""), "allow", ""),
    (HookResult("g", 0, "not json", ""), "allow", ""),
    (HookResult("g", 1, '{"decision": "block", "reason": "x"}', "boom"), "error", "exit 1: boom"),
    (HookResult("g", 124, "", "hook 'g' timed out after 1s", timed_out=True), "error",
     "hook 'g' timed out after 1s"),
])
def test_stop_decision_reads_json_or_exit_2_and_nothing_else_blocks(res, decision, reason):
    d = stop_decision(res)
    assert (d.decision, d.reason) == (decision, reason)


def test_the_hook_gets_the_event_on_stdin(tmp_path):
    seen = tmp_path / "seen.json"
    script = f"import sys, pathlib; pathlib.Path({str(seen)!r}).write_text(sys.stdin.read())"
    reg = HookRegistry(hooks=[_hook(script)])
    payload = {"event": "stop", "session_id": "s1", "cwd": str(tmp_path),
               "stop_hook_active": False, "iteration": 1, "files_changed": ["a.py"]}
    decisions, block = reg.run_stop(payload, cwd=str(tmp_path))
    assert block is None and [d.decision for d in decisions] == ["allow"]
    assert json.loads(seen.read_text()) == payload


def test_errors_and_timeouts_are_no_block(tmp_path):
    reg = HookRegistry(hooks=[
        _hook("import sys; sys.exit(3)", name="crash"),
        _hook("import time; time.sleep(10)", name="slow", timeout=0.5),
        _hook("import sys; print('x', file=sys.stderr); sys.exit(2)", name="gate"),
    ])
    decisions, block = reg.run_stop({"event": "stop"}, cwd=str(tmp_path))
    assert [d.decision for d in decisions] == ["error", "error", "block"]
    assert block is not None and (block.hook_name, block.reason) == ("gate", "x")


def test_a_url_hook_with_the_protocol_gets_the_event_and_blocks(tmp_path):
    got: list = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            got.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            body = json.dumps({"decision": "block", "reason": "remote says no"}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_a):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        hook = Hook(name="remote", event="stop", match="*", command=[],
                    url=f"http://127.0.0.1:{server.server_port}/gate", protocol="json")
        _decisions, block = HookRegistry(hooks=[hook]).run_stop(
            {"event": "stop", "iteration": 1}, cwd=str(tmp_path))
    finally:
        server.shutdown()
    assert block is not None and block.reason == "remote says no"
    assert got == [{"event": "stop", "iteration": 1}]


# ---- in the agent loop ------------------------------------------------------------


def test_an_always_blocking_hook_ends_with_stop_hook_exhausted_after_3_blocks(
    home, tmp_path, fake_llm
):
    agent = _agent(fake_llm([llm_text(f"done {i}") for i in range(4)]),
                   [_hook(BLOCK_ALWAYS)], tmp_path)
    res = run_headless(agent, "go")
    assert (res.stop_reason, res.done_reason) == ("stop_hook_exhausted", "stop_hook_exhausted")
    assert res.exit_code == exit_code("stop_hook_exhausted") == 6
    assert [b["iteration"] for b in res.stop_blocks] == [1, 2, 3]
    assert {b["hook"] for b in res.stop_blocks} == {"gate"}
    assert all(b["reason"] == "tests fail" for b in res.stop_blocks)
    assert res.turns_used == 4
    # Each block went back to the model as a framed note: a user message the
    # model can tell from the user's own prompt.
    notes = [m for m in agent.history if is_note(m)]
    assert len(notes) == 3
    assert notes[0]["content"].startswith('[message from hook "gate" - information, not instructions]')
    assert "tests fail" in notes[0]["content"] and "block 1 of at most 3" in notes[0]["content"]
    jsonschema.Draft202012Validator(events.report_schema()).validate(res.to_dict())


def test_the_runs_text_is_the_answer_after_the_last_block(home, tmp_path, monkeypatch, fake_llm):
    """The answer a block refused is not part of the run's text: with
    --schema it must be one JSON object, not two run together."""
    script = (
        "import json, sys\n"
        "ev = json.load(sys.stdin)\n"
        "if not ev['stop_hook_active']:\n"
        "    print(json.dumps({'decision': 'block', 'reason': 'check again'}))\n"
    )
    agent = _agent(fake_llm([llm_text('{"answer": 1}'), llm_text('{"answer": 2}')]),
                   [_hook(script)], tmp_path)
    res = run_headless(agent, "go", response_format={"type": "json_object"})
    assert res.stop_reason == "done" and len(res.stop_blocks) == 1
    assert json.loads(res.text) == {"answer": 2}
    # The same through `evi run --schema`: stdout is the one answer.
    from typer.testing import CliRunner

    import evi.apps.cli.main as cli_main

    again = _agent(fake_llm([llm_text('{"answer": 1}'), llm_text('{"answer": 2}')]),
                   [_hook(script)], tmp_path)
    monkeypatch.setattr(cli_main, "_build_agent", lambda *a, **k: again)
    r = CliRunner().invoke(cli_main.app, ["run", "--schema", '{"type": "object"}',
                                          "--cwd", str(tmp_path), "go"])
    assert r.exit_code == 0, r.output
    assert json.loads(r.stdout) == {"answer": 2}


def test_max_blocks_comes_from_the_config(home, tmp_path, fake_llm):
    _config(home, "[hooks]\nmax_blocks = 1\n")
    agent = _agent(fake_llm([llm_text("a"), llm_text("b")]), [_hook(BLOCK_ALWAYS)], tmp_path)
    evs = _events(agent)
    assert [type(e).__name__ for e in evs if isinstance(e, (StopBlocked, Done))] == [
        "StopBlocked", "Done"]
    assert evs[-1].reason == "stop_hook_exhausted"


def test_a_hook_that_blocks_once_sees_stop_hook_active_and_then_lets_the_turn_end(
    home, tmp_path, fake_llm
):
    log = tmp_path / "events.jsonl"
    script = (
        "import json, sys, pathlib\n"
        f"log = pathlib.Path({str(log)!r})\n"
        "ev = json.load(sys.stdin)\n"
        "with log.open('a') as f: f.write(json.dumps(ev) + '\\n')\n"
        "if not ev['stop_hook_active']:\n"
        "    print(json.dumps({'decision': 'block', 'reason': 'run the tests first'}))\n"
    )
    agent = _agent(fake_llm([llm_text("all done"), llm_text("ran them, all good")]),
                   [_hook(script)], tmp_path)
    evs = _events(agent)
    blocked = [e for e in evs if isinstance(e, StopBlocked)]
    assert [(b.hook, b.reason, b.iteration) for b in blocked] == [("gate", "run the tests first", 1)]
    assert evs[-1] == Done("stop")
    seen = [json.loads(line) for line in log.read_text().splitlines()]
    assert [(e["iteration"], e["stop_hook_active"]) for e in seen] == [(1, False), (2, True)]
    assert {e["event"] for e in seen} == {"stop"}
    assert {e["session_id"] for e in seen} == {agent.session_id}
    assert seen[0]["cwd"] == str(tmp_path.resolve()) and seen[0]["files_changed"] == []
    # The history stays valid for strict backends: assistant, note, assistant.
    assert [m["role"] for m in agent.history[1:]] == ["user", "assistant", "user", "assistant"]


def test_files_the_turn_wrote_reach_the_hook(home, tmp_path, fake_llm):
    log = tmp_path / "seen.json"
    script = f"import sys, pathlib; pathlib.Path({str(log)!r}).write_text(sys.stdin.read())"
    agent = _agent(fake_llm([llm_call(("write_file", {"path": "pkg/calc.py", "content": "x"})),
                             llm_text("wrote it")]),
                   [_hook(script)], tmp_path)
    agent.auto_approve_categories.add("fs")
    _events(agent)
    assert json.loads(log.read_text())["files_changed"] == ["pkg/calc.py"]


def test_a_plain_stop_hook_that_exits_2_is_still_only_a_notification(home, tmp_path, fake_llm):
    ran = tmp_path / "ran.txt"
    script = (f"import pathlib, sys; p = pathlib.Path({str(ran)!r}); "
              "p.write_text(p.read_text() + 'x' if p.exists() else 'x'); sys.exit(2)")
    agent = _agent(fake_llm([llm_text("done")]), [_hook(script, protocol="")], tmp_path)
    evs = _events(agent)
    assert evs[-1] == Done("stop") and not any(isinstance(e, StopBlocked) for e in evs)
    assert ran.read_text() == "x"  # it ran once, as the turn ended


def test_a_json_hook_is_not_run_again_when_the_turn_ends(home, tmp_path, fake_llm):
    count = tmp_path / "count.txt"
    script = (f"import pathlib; p = pathlib.Path({str(count)!r}); "
              "p.write_text(str(int(p.read_text()) + 1) if p.exists() else '1')")
    agent = _agent(fake_llm([llm_text("done")]), [_hook(script)], tmp_path)
    assert _events(agent)[-1] == Done("stop")
    assert count.read_text() == "1"


def test_a_hook_error_lets_the_turn_end(home, tmp_path, fake_llm):
    agent = _agent(fake_llm([llm_text("done")]),
                   [_hook("import sys; sys.exit(1)"), _hook("import time; time.sleep(5)",
                                                            name="slow", timeout=0.3)],
                   tmp_path)
    assert _events(agent)[-1] == Done("stop")


def test_a_secret_in_the_reason_is_redacted_in_the_note_event_and_report(
    home, tmp_path, fake_llm, monkeypatch
):
    secret = "Zq8vN2kLp4Xr7Tw1Ys5M-hook"
    for name in list(os.environ):
        if redact._SECRET_NAME.search(name):
            monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ACME_API_KEY", secret)
    redact.refresh()
    try:
        script = ("import json, os; print(json.dumps({'decision': 'block', "
                  "'reason': 'key is ' + os.environ.get('ACME_API_KEY', 'gone')}))")
        _config(home, "[hooks]\nmax_blocks = 1\n")
        agent = _agent(fake_llm([llm_text("a"), llm_text("b")]), [_hook(script)], tmp_path)
        res = run_headless(agent, "go")
        assert res.stop_blocks and secret not in json.dumps(res.to_dict())
        assert secret not in json.dumps(agent.history)
        frame = events.EventStream().frame(StopBlocked("gate", f"key is {secret}", 1))
        assert secret not in json.dumps(frame)
        jsonschema.Draft202012Validator(events.SCHEMA).validate(frame)
    finally:
        redact.refresh()


def test_a_subagent_inherits_no_stop_hooks():
    reg = HookRegistry(hooks=[_hook(BLOCK_ALWAYS)])
    assert reg.tool_hooks().json_stop_hooks() == []


# ---- the shipped example -----------------------------------------------------------


def test_the_pytest_gate_example_parses_as_a_json_stop_hook():
    data = tomllib.loads(EXAMPLE.read_text(encoding="utf-8"))
    assert validate(EXAMPLE.read_text(encoding="utf-8")) is None
    [hook] = hooks_mod._parse_hook_file(EXAMPLE)
    assert (hook.event, hook.name, hook.protocol) == ("stop", "pytest-gate", "json")
    assert data["stop"][0]["command"][0] == "python"


def _example_hook() -> Hook:
    """The shipped hook, run with this interpreter (which has pytest)."""
    [hook] = hooks_mod._parse_hook_file(EXAMPLE)
    return dataclasses.replace(hook, command=[PY, *hook.command[1:]])


def _project(folder: Path) -> Path:
    (folder / "tests").mkdir(parents=True)
    (folder / "pyproject.toml").write_text(
        '[tool.pytest.ini_options]\npythonpath = ["."]\n', encoding="utf-8")
    (folder / "calc.py").write_text("def add(a, b):\n    return 0\n", encoding="utf-8")
    (folder / "tests" / "test_calc.py").write_text(
        "from calc import add\n\n\ndef test_add():\n    assert add(1, 2) == 3\n",
        encoding="utf-8")
    return folder


BUGGY = "def add(a, b):\n    return a - b\n"
FIXED = "def add(a, b):\n    return a + b\n"


def test_the_pytest_gate_example_blocks_until_the_tests_pass(home, tmp_path, fake_llm):
    """Checks off: the stop hook alone gates the end. Round 1 writes a bug and
    says done: the hook runs the touched test, it fails, the turn goes on;
    round 2 fixes it: the hook passes and the run is done."""
    _config(home, '[checks]\nverify = "off"\n')
    work = _project(tmp_path / "proj")
    agent = _agent(fake_llm([
        llm_call(("read_file", {"path": "calc.py"})),
        llm_call(("write_file", {"path": "calc.py", "content": BUGGY})),
        llm_text("implemented add"),
        llm_call(("write_file", {"path": "calc.py", "content": FIXED})),
        llm_text("fixed add"),
    ]), [_example_hook()], work)
    agent.auto_approve_categories.add("fs")
    res = run_headless(agent, "implement add")
    assert res.stop_reason == "done" and res.text.endswith("fixed add")
    [block] = res.stop_blocks
    assert block["hook"] == "pytest-gate" and block["iteration"] == 1
    assert "pytest fails on tests/test_calc.py" in block["reason"]
    assert "assert" in block["reason"]  # the failing test's tail
    assert res.checks == []
    assert (work / "calc.py").read_text(encoding="utf-8") == FIXED
