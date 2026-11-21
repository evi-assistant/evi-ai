"""`[tools] sandbox_required`: code execution runs sandboxed or not at all.

With the setting on and no OS sandboxer on PATH (always so on Windows),
run_python, run_command and monitor commands answer with an error and start
no process, in every permission mode; with it off they keep the old
"ran unsandboxed" fallback. A repo's `.evi.toml` may switch it on, never off,
and `evi doctor` reports the state.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

import evi.hooks as hooks_mod
import evi.tools.code  # noqa: F401  registers run_python
import evi.tools.monitor  # noqa: F401  registers monitor
import evi.tools.shell  # noqa: F401  registers run_command
from evi import doctor, sandbox
from evi.config import Config
from evi.llm.agent import Agent, ToolResult
from evi.tools.base import REGISTRY, ToolContext

MARK = "echo hi > marker.txt"


@pytest.fixture
def home(monkeypatch, tmp_path):
    """eVi's home and config under tmp_path (never ~/.evi), no safe mode, and
    a process cwd of its own (where monitor's commands run)."""
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
    cwd = tmp_path / "process-cwd"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    return evi_home


@pytest.fixture
def windows(monkeypatch):
    """No sandboxer exists: the platform is Windows."""
    monkeypatch.setattr(sandbox, "_system", lambda: "Windows")


@pytest.fixture
def no_spawn(monkeypatch) -> list:
    tried: list = []

    def popen(*a, **k):
        tried.append(a)
        raise AssertionError(f"a process was started: {a!r}")

    monkeypatch.setattr(subprocess, "Popen", popen)
    return tried


def _user(home: Path, body: str) -> None:
    (home / "config.toml").write_text(body, encoding="utf-8")


def _cfg(**tools) -> Config:
    cfg = Config()
    cfg.tools.shell = True
    cfg.tools.memory = cfg.tools.skills = cfg.tools.transcripts = False
    for k, v in tools.items():
        setattr(cfg.tools, k, v)
    return cfg


# ---- the helper -------------------------------------------------------------


def test_required_implies_requested_and_refuses_without_a_sandboxer(windows):
    on = SimpleNamespace(tools=SimpleNamespace(sandbox=False, sandbox_required=True))
    assert sandbox.required(on) and sandbox.requested(on) and not sandbox.active(on)
    msg = sandbox.refusal(on, tool="run_python")
    assert msg.startswith("ERROR: run_python did not run")
    assert "sandbox_required" in msg and "Nothing was executed" in msg


def test_no_refusal_when_off_or_a_sandboxer_is_present(monkeypatch):
    off = SimpleNamespace(tools=SimpleNamespace(sandbox=True, sandbox_required=False))
    monkeypatch.setattr(sandbox, "_system", lambda: "Windows")
    assert sandbox.refusal(off) is None
    on = SimpleNamespace(tools=SimpleNamespace(sandbox=True, sandbox_required=True))
    monkeypatch.setattr(sandbox, "_system", lambda: "Linux")
    monkeypatch.setattr(sandbox.shutil, "which", lambda n: "/usr/bin/bwrap" if n == "bwrap" else None)
    assert sandbox.refusal(on) is None


def test_linux_without_bwrap_names_the_fix(monkeypatch):
    monkeypatch.setattr(sandbox, "_system", lambda: "Linux")
    monkeypatch.setattr(sandbox.shutil, "which", lambda n: None)
    on = SimpleNamespace(tools=SimpleNamespace(sandbox=True, sandbox_required=True))
    assert "install bubblewrap" in sandbox.refusal(on)


def test_takes_tool_settings_directly(windows):
    tools = SimpleNamespace(sandbox=False, sandbox_required=True)
    assert sandbox.required(tools) and sandbox.refusal(tools, tool="monitor")


def test_an_unreadable_config_requires_the_sandbox(monkeypatch):
    class Broken:
        @property
        def tools(self):
            raise RuntimeError("unreadable")

    assert sandbox.required(Broken()) is True
    assert sandbox.requested(Broken()) is False


# ---- the three tools ----------------------------------------------------------


def test_run_python_refuses(home, windows, no_spawn, tmp_path):
    _user(home, "[tools]\nsandbox_required = true\n")
    out = REGISTRY["run_python"].call(json.dumps({"code": "print(1)"}))
    assert out.startswith("ERROR: run_python did not run") and not no_spawn


def test_run_command_refuses(home, windows, no_spawn):
    _user(home, "[tools]\nshell = true\nsandbox_required = true\n")
    out = REGISTRY["run_command"].call(json.dumps({"command": MARK}))
    assert out.startswith("ERROR: run_command did not run") and not no_spawn
    assert not (Path.cwd() / "marker.txt").exists()


def test_monitor_command_refuses_from_the_session_config(home, windows, no_spawn):
    ctx = ToolContext(agent=SimpleNamespace(config=_cfg(sandbox_required=True)))
    out = REGISTRY["monitor"].call(json.dumps({"target": MARK, "kind": "command"}), ctx)
    assert out.startswith("ERROR: monitor did not run") and not no_spawn


def test_off_keeps_the_unsandboxed_fallback(home, windows):
    _user(home, "[tools]\nsandbox = true\n")
    out = REGISTRY["run_python"].call(json.dumps({"code": "print(41 + 1)"}))
    assert out.startswith("(sandbox requested but no sandboxer") and "42" in out


def test_agent_loop_refuses_in_yolo(home, windows, no_spawn, tmp_path, fake_llm):
    # Approval is not the gate: in yolo the call is allowed and still refused.
    _user(home, "[tools]\nshell = true\nsandbox_required = true\n")
    client = fake_llm([
        fake_llm.call(("run_command", {"command": MARK}),
                      ("run_python", {"code": "print(1)"})),
        fake_llm.text("done"),
    ])
    agent = Agent(client=client, config=_cfg(sandbox_required=True),
                  tools=[REGISTRY["run_command"], REGISTRY["run_python"]])
    agent.cwd = str(tmp_path)
    agent.enable_auto_all()
    res = {e.name: e.output for e in agent.chat("go") if isinstance(e, ToolResult)}
    assert res["run_command"].startswith("ERROR: run_command did not run")
    assert res["run_python"].startswith("ERROR: run_python did not run")
    assert not no_spawn and not (tmp_path / "marker.txt").exists()


# ---- project config and doctor --------------------------------------------------


def test_a_project_may_require_it_but_not_lift_it():
    from evi import project

    on = project.filter_project_overlay({"tools": {"sandbox_required": True}})
    off = project.filter_project_overlay({"tools": {"sandbox_required": False}})
    assert on[0] == {"tools": {"sandbox_required": True}}
    assert off[0] == {} and "tools.sandbox_required" in off[1]


@pytest.mark.parametrize(("body", "status", "words"), [
    ("", "ok", "off"),
    ("[tools]\nsandbox = true\n", "warn", "runs unsandboxed"),
    ("[tools]\nsandbox_required = true\n", "fail", "refuse to run"),
])
def test_doctor_reports_the_state(home, windows, body, status, words):
    _user(home, body)
    [check] = doctor._check_sandbox()
    assert check.status == status and words in check.detail
    if status == "fail":
        assert check.fix and "sandbox_required = false" in check.fix[0]


def test_doctor_ok_with_a_sandboxer(home, monkeypatch):
    monkeypatch.setattr(sandbox, "_system", lambda: "Linux")
    monkeypatch.setattr(sandbox.shutil, "which", lambda n: "/usr/bin/bwrap" if n == "bwrap" else None)
    _user(home, "[tools]\nsandbox_required = true\n")
    [check] = doctor._check_sandbox()
    assert check.status == "ok" and "bwrap" in check.detail and "required" in check.detail
