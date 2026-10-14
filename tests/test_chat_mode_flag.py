"""`evi chat --mode` applies a tool preset, like `evi run --mode`.

docs/self-build.md told eVi to run `evi chat --mode code`, but chat() had no
--mode option, so the documented self-development loop failed with "No such
option". The REPL itself is stubbed here (it needs a real console); the full
command runs against a fake backend in tests/test_self_build_commands.py.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

import evi.apps.cli.main as cli_main
from evi.modes import MODES, mode_tools


@pytest.fixture
def chat_env(monkeypatch, tmp_path):
    """Stub the agent build and the REPL; return the list of agents the REPL
    was started with."""
    # `chat --cwd` sets the session working folder (a ContextVar) in this very
    # thread; restore it so later tests on the same xdist worker see the
    # process cwd again.
    from evi import workdir

    token = workdir.set_cwd(None)
    yield _stub_chat(monkeypatch, tmp_path)
    workdir.reset(token)


def _stub_chat(monkeypatch, tmp_path) -> list:
    monkeypatch.setattr("evi.config.HOME", tmp_path)
    monkeypatch.setattr("evi.config.CONFIG_PATH", tmp_path / "config.toml")
    sentinel = {"config_tool": object()}
    monkeypatch.setattr(
        cli_main, "_build_agent",
        lambda *a, **k: SimpleNamespace(tools=dict(sentinel), cwd=None, project=None),
    )
    started: list = []
    monkeypatch.setattr(cli_main, "_run_repl", started.append)
    return started


@pytest.mark.parametrize("flag", ["--mode", "-m"])
def test_chat_mode_code_starts_with_the_code_tool_list(chat_env, flag):
    r = CliRunner().invoke(cli_main.app, ["chat", flag, "code"])
    assert r.exit_code == 0, r.output
    [agent] = chat_env
    expected = {t.name for t in mode_tools("code")}
    assert expected  # the CLI imports the built-in tools, so the preset is not empty
    assert set(agent.tools) == expected
    cats = set(MODES["code"].categories)
    assert all(t.category in cats for t in agent.tools.values())
    assert "run_command" in agent.tools  # the shell tool is in Code only


def test_chat_mode_cowork_is_a_different_preset(chat_env):
    r = CliRunner().invoke(cli_main.app, ["chat", "--mode", "cowork"])
    assert r.exit_code == 0, r.output
    assert set(chat_env[0].tools) == {t.name for t in mode_tools("cowork")}
    assert "run_command" not in chat_env[0].tools


def test_chat_without_mode_keeps_the_configured_tools(chat_env):
    r = CliRunner().invoke(cli_main.app, ["chat"])
    assert r.exit_code == 0, r.output
    assert set(chat_env[0].tools) == {"config_tool"}


def test_chat_unknown_mode_is_refused_before_the_session_starts(chat_env):
    r = CliRunner().invoke(cli_main.app, ["chat", "--mode", "turbo"])
    assert r.exit_code == 2
    assert "unknown mode" in r.output and "code" in r.output
    assert chat_env == []  # no REPL was started


def test_chat_mode_combines_with_cwd(chat_env, tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    r = CliRunner().invoke(cli_main.app, ["chat", "--mode", "code", "--cwd", str(work)])
    assert r.exit_code == 0, r.output
    [agent] = chat_env
    assert agent.cwd == str(work.resolve())
    assert set(agent.tools) == {t.name for t in mode_tools("code")}


def test_chat_mode_keeps_safe_modes_cut(chat_env, monkeypatch):
    # build_agent drops the memory, skills and MCP tools in safe mode; the
    # preset is read from the whole registry, so --mode must drop them again.
    from evi import safemode

    monkeypatch.setenv(safemode.ENV_VAR, "1")
    r = CliRunner().invoke(cli_main.app, ["chat", "--mode", "code"])
    assert r.exit_code == 0, r.output
    [agent] = chat_env
    cats = {t.category for t in agent.tools.values()}
    assert not cats & {"memory", "skills", "mcp"}, sorted(agent.tools)
    assert "remember" not in agent.tools and "invoke_skill" not in agent.tools
    assert "run_command" in agent.tools  # the rest of the preset is intact
    expected = {t.name for t in mode_tools("code")
                if t.category not in ("memory", "skills", "mcp")}
    assert set(agent.tools) == expected


def test_worktree_chat_starts_the_repl_in_the_worktree(chat_env, monkeypatch, tmp_path):
    # `evi worktree chat` calls chat() directly; with the new --mode option it
    # must still pass real values, not Typer's OptionInfo defaults.
    work = tmp_path / "wt"
    work.mkdir()
    monkeypatch.chdir(tmp_path)  # restored after the test (worktree chat chdirs)
    monkeypatch.setattr("evi.worktree.find_worktree_for", lambda branch: work)
    r = CliRunner().invoke(cli_main.app, ["worktree", "chat", "feature-x"])
    assert r.exit_code == 0, r.output
    assert "unknown mode" not in r.output
    [agent] = chat_env
    assert set(agent.tools) == {"config_tool"}
    assert Path.cwd() == work
