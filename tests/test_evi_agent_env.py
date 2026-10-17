"""EVI_AGENT=1 in every process an eVi agent runs, whoever starts it
(Phase 1, loop-task-queue: evi/attest.py's contract says `evi task verify`
must not ship before then).

Two mechanisms, each tested here:

- eVi's agent-hosting entry points mark their own process
  (`evi.proc.mark_agent_process`) at startup, so every child inherits it,
  however it is started: `evi run`, `evi batch`, `evi workflow run`,
  `evi team run`, `evi mcp serve`, `evi channel serve`, `evi scheduler`,
  `evi schedule run-now`, `evi task start`, every CLI command that builds an
  agent (`_build_agent`: `evi chat` and the rest) and the web server (the
  desktop sidecar runs that same app). Director tooling (`evi task
  verify|reject`, `evi attest`) never marks itself.
- the spawners that build an environment of their own set it there too:
  evi.proc (run_command, run_python, monitor, the CLI-agent drivers),
  hooks, MCP stdio servers, the linter / formatter runner, OCR and the
  Claude Agent SDK; git (`evi.gitsafe`) passes on what the process has.

One test per spawner family shows the child seeing EVI_AGENT=1: a real
child process where one can be had (a probe script printing its
environment), else the environment handed to the library that spawns it.
The test process itself is never left marked (tests/conftest.py puts it
back after every test), and redaction still knows the director key after
a process marks itself.
"""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from evi import proc

_ENV_PROBE = """\
import os, sys
print("EVI_AGENT=" + os.environ.get("EVI_" + "AGENT", "<unset>"))
sys.exit(int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].isdigit() else 0)
"""


@pytest.fixture(autouse=True)
def _not_an_agent_here(monkeypatch):
    """This test process is no agent's to start with (an eVi agent may be
    running the suite); monkeypatch puts EVI_AGENT back as it was."""
    monkeypatch.delenv("EVI_AGENT", raising=False)
    monkeypatch.delenv("EVI_INTERACTIVE", raising=False)


@pytest.fixture
def probe(tmp_path: Path) -> Path:
    path = tmp_path / "probe.py"
    path.write_text(_ENV_PROBE, encoding="utf-8")
    return path


def _cli(*args: str):
    import evi.apps.cli.main as cli_main

    return CliRunner().invoke(cli_main.app, list(args))


def _marked() -> bool:
    return os.environ.get("EVI_AGENT") == "1"


# ---- marking the process -------------------------------------------------------------


def test_mark_agent_process_sets_it_for_every_child(probe):
    import subprocess

    assert not _marked()
    plain = subprocess.run([sys.executable, str(probe)], capture_output=True, text=True,
                           timeout=60)
    assert "EVI_AGENT=<unset>" in plain.stdout
    proc.mark_agent_process()
    assert _marked()
    # A child started any which way (no explicit env) inherits it.
    child = subprocess.run([sys.executable, str(probe)], capture_output=True, text=True,
                           timeout=60)
    assert "EVI_AGENT=1" in child.stdout
    proc.mark_agent_process()  # idempotent
    assert _marked()


def test_marking_reads_the_director_key_for_redaction_first(monkeypatch, tmp_path):
    """Once marked, evi.attest no longer reads the director key here; it was
    read for redaction before, so it is still redacted."""
    from evi import attest, redact

    monkeypatch.setattr(attest, "_home", lambda: tmp_path)
    monkeypatch.setattr(attest, "_keyring", lambda: None)
    monkeypatch.setattr(attest, "_process_chain",
                        lambda: [(5, ["evi", "attest", "init"]), (4, ["bash"])])
    attest.init_key(yes=True)
    key = attest._file_get()
    redact.refresh()
    try:
        proc.mark_agent_process()
        assert attest.key_id() is None  # not read in an agent's process
        assert key.hex() not in redact.redact(f"leaked {key.hex()} here")
    finally:
        redact.refresh()


def test_the_test_process_is_put_back_after_a_marking_test(tmp_path):
    """tests/conftest.py's _restore_evi_agent: a test that runs an entry point
    in-process leaves no EVI_AGENT behind for the next test (run in a child
    pytest that loads that conftest as a plugin)."""
    import subprocess

    (tmp_path / "test_marks.py").write_text(
        "import os\n"
        "from evi import proc\n\n"
        "def test_a_marks():\n"
        "    proc.mark_agent_process()\n"
        "    assert os.environ['EVI_' + 'AGENT'] == '1'\n\n"
        "def test_b_is_clean():\n"
        "    assert 'EVI_' + 'AGENT' not in os.environ\n",
        encoding="utf-8")
    root = Path(__file__).resolve().parents[1]
    env = {**os.environ, "PYTHONPATH": str(root), "PYTHONUTF8": "1"}
    out = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-p", "tests.conftest",
         "--rootdir", str(tmp_path), "-o", "addopts=", str(tmp_path / "test_marks.py")],
        cwd=str(tmp_path), env=env, capture_output=True, text=True, timeout=300)
    assert out.returncode == 0 and "2 passed" in out.stdout, out.stdout + out.stderr


# ---- the entry points ------------------------------------------------------------------


class _Agent:
    """Just enough of an agent for the CLI commands under test."""

    tools: dict = {}
    auto_all = False
    permission_callback = None
    permission_batch_callback = None
    cwd = ""
    session_id = None
    mode = ""
    policy = None

    def enable_auto_all(self) -> None:
        self.auto_all = True


@pytest.fixture
def no_agents(monkeypatch):
    """`_build_agent` hands out stand-ins, and `run_headless` runs nothing."""
    import evi.apps.cli.main as cli_main
    import evi.headless as headless

    monkeypatch.setattr(cli_main, "_build_agent", lambda *a, **k: _Agent())
    monkeypatch.setattr(headless, "run_headless",
                        lambda agent, prompt, **kw: headless.HeadlessResult(
                            text="ok", stop_reason="done"))


def test_evi_run_marks_the_process(no_agents):
    res = _cli("run", "hello")
    assert res.exit_code == 0, res.output
    assert _marked()


def test_evi_batch_marks_the_process(no_agents, tmp_path):
    items = tmp_path / "items.jsonl"
    items.write_text('{"id": "1", "prompt": "hi"}\n', encoding="utf-8")
    res = _cli("batch", str(items))
    assert res.exit_code == 0, res.output
    assert _marked()


def test_evi_workflow_run_marks_the_process(no_agents, monkeypatch):
    from evi import workflows

    monkeypatch.setattr(workflows, "load_workflow", lambda name: workflows.Workflow(
        name="w", steps=[workflows.WStep(id="one", prompt="do it")]))
    res = _cli("workflow", "run", "w", "--json")
    assert res.exit_code == 0, res.output
    assert _marked()


def test_evi_team_run_marks_the_process(monkeypatch):
    import evi.apps.cli.main as cli_main
    from evi import teams

    class _Store:
        def load(self):
            return []

        def any_active(self):
            return True

    monkeypatch.setattr(teams, "TeamStore", _Store)
    monkeypatch.setattr(teams, "ready_tasks", lambda tasks: [])
    monkeypatch.setattr(teams, "run_team", lambda *a, **k: [], raising=False)
    monkeypatch.setattr(cli_main.Config, "load", classmethod(lambda cls: cli_main.Config()))
    _cli("team", "run")  # what it then does with an empty list is not the point
    assert _marked()


def test_every_cli_command_that_builds_an_agent_marks_the_process(monkeypatch):
    """`_build_agent` (evi chat, evi sessions resume, recipes, evals ...)."""
    import evi.apps.cli.main as cli_main
    import evi.sdk.builder as builder

    monkeypatch.setattr(cli_main, "_ensure_mcp", lambda config: None)
    monkeypatch.setattr(builder, "build_agent", lambda **kw: _Agent())
    cli_main._build_agent(register=False)
    assert _marked()


def test_evi_mcp_serve_marks_the_process(monkeypatch):
    pytest.importorskip("mcp")
    import evi.mcp.publish as publish

    served: list = []
    monkeypatch.setattr(publish, "serve", lambda *a, **k: served.append(_marked()))
    res = _cli("mcp", "serve")
    assert res.exit_code == 0, res.output
    assert served == [True]


def test_evi_channel_serve_marks_the_process(monkeypatch):
    from evi.channels import telegram

    monkeypatch.setattr(telegram, "start_if_configured", lambda *a, **k: None)
    res = _cli("channel", "serve")
    assert res.exit_code == 1  # off: nothing to serve, but the process was marked first
    assert _marked()


@pytest.mark.parametrize("args", [["scheduler"], ["schedule", "run-now", "job1"]])
def test_the_scheduler_marks_the_process(monkeypatch, args):
    import evi.scheduler as scheduler

    class _Scheduler:
        def __init__(self):
            self.store = SimpleNamespace(get=lambda _id: SimpleNamespace(last_status="ok"))

        def start(self):
            raise RuntimeError("no scheduler here")

        def run_now(self, task_id):
            return "log.txt"

    monkeypatch.setattr(scheduler, "Scheduler", _Scheduler)
    _cli(*args)
    assert _marked()


def test_the_web_server_marks_the_process_when_it_starts(monkeypatch, tmp_path):
    """`evi web`, the desktop sidecar and the dev uvicorn all run this app;
    its lifespan marks the process before it starts MCP servers, the
    scheduler or a channel."""
    import asyncio

    import evi.apps.web.server as server
    import evi.config as config_mod

    monkeypatch.setenv("EVI_SAFE_MODE", "1")  # no MCP servers, no scheduler
    monkeypatch.setattr(config_mod, "HOME", tmp_path)
    monkeypatch.setattr(config_mod, "CONFIG_PATH", tmp_path / "config.toml")
    app = server.create_app()
    assert not _marked()  # building the app (or importing it) marks nothing

    async def start_and_stop() -> bool:
        async with app.router.lifespan_context(app):
            return _marked()

    assert asyncio.run(start_and_stop())


def test_director_tooling_never_marks_itself(monkeypatch, tmp_path):
    from evi import attest

    monkeypatch.setattr(attest, "_home", lambda: tmp_path)
    monkeypatch.setattr(attest, "_keyring", lambda: None)
    monkeypatch.setattr(attest, "_process_chain",
                        lambda: [(5, ["evi", "attest", "init", "--yes"]), (4, ["bash"])])
    assert _cli("attest", "init", "--yes").exit_code == 0
    assert _cli("attest", "status").exit_code == 0
    assert not _marked()


# ---- the spawners ----------------------------------------------------------------------


def test_proc_run_and_spawn_children(probe):
    """run_command, run_python and monitor (evi.proc.run), the CLI-agent
    drivers (run_cli, the codex / amp drivers: evi.proc.spawn)."""
    from evi.llm.cli_agent import run_cli

    assert "EVI_AGENT=1" in proc.run([sys.executable, str(probe)], timeout=60).stdout
    assert "EVI_AGENT=1" in run_cli([sys.executable, str(probe)], input="", timeout=60).stdout
    assert not _marked()


def test_git_children_inherit_it_from_a_marked_process(monkeypatch):
    """evi.gitsafe passes on the process's environment (minus what points
    git elsewhere): marked, its git children have EVI_AGENT=1. Unmarked
    (director tooling's git), they don't."""
    import subprocess

    from evi import gitsafe

    real_run = subprocess.run
    seen: list[dict] = []

    def spy(args, *a, **kw):
        seen.append(dict(kw.get("env") or {}))
        return real_run(args, *a, **kw)

    monkeypatch.setattr(gitsafe.subprocess, "run", spy)
    gitsafe.run_git(["--version"], timeout=60)
    assert seen and "EVI_AGENT" not in seen[-1]
    proc.mark_agent_process()
    assert gitsafe.git_env()["EVI_AGENT"] == "1"
    out = gitsafe.run_git(["--version"], timeout=60)
    assert out.returncode == 0 and seen[-1].get("EVI_AGENT") == "1"


def test_a_hook_command_gets_it(probe):
    from evi import hooks

    hook = hooks.Hook(name="probe", event="after_tool_call", match="*",
                      command=[sys.executable, str(probe)])
    result = hooks._run_hook(hook, "write_file", "{}", "ok")
    assert result.exit_code == 0 and "EVI_AGENT=1" in result.stdout
    assert not _marked()


def test_an_mcp_stdio_server_gets_it_and_its_config_cannot_clear_it(monkeypatch):
    pytest.importorskip("mcp")
    import mcp
    import mcp.client.stdio as stdio_mod

    from evi.mcp.manager import MCPManager
    from evi.mcp.servers import MCPServer
    from tests.test_mcp import _FakeStreams, _FakeTransportSession

    captured: dict = {}

    def fake_stdio(params):
        captured["env"] = dict(params.env or {})
        return _FakeStreams(2)

    monkeypatch.setattr(stdio_mod, "stdio_client", fake_stdio)
    monkeypatch.setattr(mcp, "ClientSession", _FakeTransportSession)
    server = MCPServer(name="local", command="noop", args=[], env={"EVI_AGENT": ""})
    mgr = MCPManager([server])
    try:
        mgr.start()
        # The SDK's default environment keeps only a few variables, so this
        # is never inherited: the manager sets it.
        assert captured["env"].get("EVI_AGENT") == "1"
    finally:
        mgr.stop()


def test_the_linter_and_formatter_get_it(probe, tmp_path, monkeypatch):
    from evi import codeintel

    target = tmp_path / "a.py"
    target.write_text("x = 1\n", encoding="utf-8")
    monkeypatch.setitem(codeintel._LINTERS, ".py", [[sys.executable, str(probe), "1"]])
    assert "EVI_AGENT=1" in codeintel.diagnose(target)  # a finding: exit 1, its output
    marker = tmp_path / "formatted.txt"
    fmt = tmp_path / "fmt.py"
    fmt.write_text("import os, pathlib\npathlib.Path(r'%s').write_text("
                   "os.environ.get('EVI_' + 'AGENT', '<unset>'))\n" % marker, encoding="utf-8")
    monkeypatch.setitem(codeintel._FORMATTERS, ".py", [[sys.executable, str(fmt)]])
    assert codeintel.format_file(target) == (True, sys.executable)
    assert marker.read_text(encoding="utf-8") == "1"


def test_ocr_gets_it(tmp_path, monkeypatch):
    from evi.tools import ocr

    if os.name == "nt":
        tool = tmp_path / "tesseract.cmd"
        tool.write_text("@echo off\r\necho EVI_AGENT=%EVI_AGENT%\r\n", encoding="utf-8")
    else:
        tool = tmp_path / "tesseract"
        tool.write_text('#!/bin/sh\necho "EVI_AGENT=$EVI_AGENT"\n', encoding="utf-8")
        tool.chmod(tool.stat().st_mode | stat.S_IXUSR)
    image = tmp_path / "shot.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\n")
    monkeypatch.setenv("EVI_TESSERACT_CMD", str(tool))
    assert "EVI_AGENT=1" in ocr._run_tesseract(image, "eng")


def test_the_claude_agent_sdk_cli_gets_it(monkeypatch):
    from evi.llm import claude_agent as ca
    from tests.test_claude_agent import _FakeResultMessage, _fake_sdk

    seen: dict = {}

    async def query(*, prompt, options):
        async for _ in prompt:
            pass
        seen["env"] = dict(options.env)
        yield _FakeResultMessage(usage={"input_tokens": 1, "output_tokens": 1})

    monkeypatch.setattr(ca, "_import_sdk", lambda: _fake_sdk(query))
    client = ca.ClaudeAgentClient()
    list(client.chat.completions.create(model="haiku", stream=True,
                                        messages=[{"role": "user", "content": "hi"}]))
    assert seen["env"] == {"EVI_AGENT": "1"}  # on top of eVi's environment, which the SDK keeps
