"""EVI_AGENT=1 in every process an agent starts (loop-director-attestation:
"Agent-run processes get EVI_AGENT=1, and attest refuses to sign when it is
set").

evi/proc.py's `spawn` adds it to whatever environment the caller passes, so
everything that starts commands through it has it: run_command, run_python
and monitor (through `run`), the delegate CLI-agent drivers (`run_cli`), the
web server's sessions, which is also what the desktop app's sidecar runs.
Director tooling (`evi.attest`) refuses to sign, and never reads the key, in
such a process. Also here: `evi workflow run` is an unattended entry point
like `evi run` (its steps' commands get no *_KEY / *_TOKEN / *_SECRET).

The probes are script files, not `python -c` code: the shell guard refuses a
command line that names EVI_AGENT or imports evi.attest (evi/shell_guard.py),
and the agent-level tests below go through it.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

import evi.tools.shell  # noqa: F401  registers run_command
from evi import proc
from evi.config import Config
from evi.llm.agent import Agent, ToolResult
from evi.tools.base import REGISTRY
from tests._live_web import Scripted
from tests.conftest import llm_call, llm_text

ROOT = Path(__file__).resolve().parents[1]

_ENV_PROBE = """\
import os, sys
print("EVI_AGENT=" + os.environ.get("EVI_" + "AGENT", "<unset>"))
print("PROBE_VAR=" + os.environ.get("PROBE_VAR", "<unset>"))
"""

# What director tooling does first in a process (evi.attest.authorize).
_ATTEST_PROBE = """\
import importlib
attest = importlib.import_module("evi." + "attest")
print("why:", attest.agent_parent())
try:
    attest.authorize("sign a verdict", yes=True)
except attest.AgentRefused as exc:
    print("refused:", exc)
else:
    print("AUTHORIZED")
print("key id:", attest.key_id())
"""


@pytest.fixture
def probe(tmp_path: Path):
    def write(code: str, name: str = "probe.py") -> Path:
        path = tmp_path / name
        path.write_text(code, encoding="utf-8")
        return path

    return write


@pytest.fixture(autouse=True)
def _not_an_agent_here(monkeypatch):
    """This test process itself is no agent's (an eVi agent may be running
    the suite): what the children see comes from spawn."""
    monkeypatch.delenv("EVI_AGENT", raising=False)
    monkeypatch.delenv("EVI_INTERACTIVE", raising=False)


def test_run_gives_the_command_evi_agent(probe) -> None:
    res = proc.run([sys.executable, str(probe(_ENV_PROBE))], timeout=60)
    assert "EVI_AGENT=1" in res.stdout, res.output
    assert "EVI_AGENT" not in os.environ  # eVi's own environment is untouched


def test_an_explicit_or_scrubbed_environment_still_gets_evi_agent(probe, monkeypatch) -> None:
    monkeypatch.setenv("PROBE_VAR", "kept")
    monkeypatch.setenv("PROBE_TOKEN", "dropped-by-the-scrub")
    env = proc.scrubbed_env()
    res = proc.run([sys.executable, str(probe(_ENV_PROBE))], timeout=60, env=env)
    assert "EVI_AGENT=1" in res.stdout and "PROBE_VAR=kept" in res.stdout, res.output
    assert "EVI_AGENT" not in env  # the caller's dict is not changed
    assert proc.agent_env({"A": "b"}) == {"A": "b", "EVI_AGENT": "1"}


def test_a_driver_process_gets_evi_agent(probe) -> None:
    """The delegate CLI-agent drivers start their CLI through spawn too."""
    from evi.llm.cli_agent import run_cli

    res = run_cli([sys.executable, str(probe(_ENV_PROBE))], input="", timeout=60)
    assert "EVI_AGENT=1" in res.stdout, res.stdout + res.stderr


def test_director_tooling_refuses_from_inside_an_agent_turn(probe) -> None:
    """Agent.chat end to end: the agent's run_command starts a process that
    tries what director tooling does first; evi.attest refuses it and reads
    no key there."""
    script = probe(_ATTEST_PROBE, "attest_probe.py")
    command = f'"{sys.executable}" "{script}"'
    agent = Agent(
        client=Scripted([llm_call(("run_command", {"command": command, "timeout": 120})),
                         llm_text("done")]),
        config=Config(), tools=[REGISTRY["run_command"]], transcripts=None, unattended_policy="allow",
    )
    agent.cwd = str(ROOT)
    events = list(agent.chat("try to verify the task"))
    out = next(e for e in events if isinstance(e, ToolResult)).output
    assert "why: EVI_AGENT is set" in out, out
    assert "refused:" in out and "AUTHORIZED" not in out
    assert "key id: None" in out


def test_a_web_session_command_gets_evi_agent(monkeypatch, tmp_path, probe) -> None:
    """The web server's sessions run tools through the same runner; the
    desktop app's sidecar is this same server app (checked below)."""
    from fastapi.testclient import TestClient

    from tests._live_web import sse_events, web_app

    command = f'"{sys.executable}" "{probe(_ENV_PROBE)}"'
    agent = Agent(
        client=Scripted([llm_call(("run_command", {"command": command, "timeout": 120})),
                         llm_text("done")]),
        config=Config(), tools=[REGISTRY["run_command"]], transcripts=None,
    )
    agent.enable_auto_all()
    c = TestClient(web_app(monkeypatch, tmp_path, agent))
    events = sse_events(c.post("/api/chat", json={"session_id": "w1", "message": "go"}).text)
    out = next(e for e in events if e["kind"] == "ToolResult")["output"]
    assert "EVI_AGENT=1" in out, json.dumps(events)


def test_the_desktop_sidecar_serves_that_same_app() -> None:
    """Both desktop launch paths run evi.apps.web.server's app: the frozen
    sidecar imports it, the dev shell runs it under uvicorn. So a desktop
    session's commands go through the runner tested above."""
    entry = (ROOT / "scripts" / "sidecar_entry.py").read_text(encoding="utf-8")
    assert "from evi.apps.web.server import app" in entry
    main_rs = ROOT / "desktop" / "src-tauri" / "src" / "main.rs"
    if main_rs.exists():
        assert '"evi.apps.web.server:app"' in main_rs.read_text(encoding="utf-8")


# ---- evi workflow run is unattended -------------------------------------------------


def test_workflow_steps_run_as_an_unattended_entry_point(monkeypatch) -> None:
    from typer.testing import CliRunner

    import evi.apps.cli.main as cli_main
    import evi.headless as headless
    from evi import workflows
    from evi.llm.cli_agent import current_unattended

    wf = workflows.Workflow(name="w", steps=[workflows.WStep(id="one", prompt="do it"),
                                             workflows.WStep(id="two", prompt="and this",
                                                             parallel=True),
                                             workflows.WStep(id="three", prompt="and that",
                                                             parallel=True)])
    monkeypatch.setattr(workflows, "load_workflow", lambda name: wf)
    monkeypatch.setattr(cli_main, "_build_agent", lambda *a, **k: _StepAgent())
    monkeypatch.setattr(cli_main.Config, "load", classmethod(lambda cls: Config()))
    seen: list = []

    def fake_run(agent, prompt, **_kw):
        seen.append(current_unattended())
        return headless.HeadlessResult(text=f"ran {prompt}")

    monkeypatch.setattr(headless, "run_headless", fake_run)
    r = CliRunner().invoke(cli_main.app, ["workflow", "run", "w", "--json"])
    assert r.exit_code == 0, r.output
    assert len(seen) == 3 and all(m == ("workflow", False) for m in seen), seen


def test_workflow_refuses_a_delegate_cli_agent_backend(monkeypatch) -> None:
    from typer.testing import CliRunner

    import evi.apps.cli.main as cli_main
    from evi import workflows

    cfg = Config()
    cfg.llm.backend = "codex"
    monkeypatch.setattr(workflows, "load_workflow",
                        lambda name: workflows.Workflow(name="w", steps=[
                            workflows.WStep(id="one", prompt="x")]))
    monkeypatch.setattr(cli_main.Config, "load", classmethod(lambda cls: cfg))
    monkeypatch.setattr(cli_main, "_build_agent",
                        lambda *a, **k: pytest.fail("no step may run"))
    r = CliRunner().invoke(cli_main.app, ["workflow", "run", "w"])
    assert r.exit_code == 1
    assert "unattended" in r.output


class _StepAgent:
    tools: dict = {}
    auto_all = False

    def enable_auto_all(self) -> None:
        self.auto_all = True
