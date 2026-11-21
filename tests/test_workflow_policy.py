"""`evi workflow run` settles a call that would ask by a policy, not allow-all.

At a terminal it asks (`--policy ask`, one question at a time across
parallel steps, each naming its step); with nobody to ask it follows
`[auto] unattended_policy` (deny by default) like the web app's Dispatch;
`--policy deny` refuses, `--policy implementer` holds each step to the
implementer policy, and `--yes` auto-approves everything as before.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

import evi.tools.shell  # noqa: F401  registers run_command
from evi import hooks as hooks_mod
from evi.config import Config
from evi.llm.agent import Agent
from evi.tools.base import REGISTRY

MARK = "echo hi > marker.txt"
_QUIET = "memory = false\nskills = false\ntranscripts = false\n"


@pytest.fixture
def home(monkeypatch, tmp_path):
    """eVi's home under tmp_path (never ~/.evi), a shell-enabled config, a
    process cwd of its own, and EVI_AGENT put back afterwards."""
    import evi.config as config_mod

    evi_home = tmp_path / "evi-home"
    (evi_home / "workflows").mkdir(parents=True)
    monkeypatch.delenv("EVI_SAFE_MODE", raising=False)
    monkeypatch.delenv("EVI_AGENT", raising=False)
    monkeypatch.setenv("EVI_HOME", str(evi_home))
    monkeypatch.setattr(config_mod, "HOME", evi_home)
    monkeypatch.setattr(config_mod, "CONFIG_PATH", evi_home / "config.toml")
    monkeypatch.setattr(config_mod, "ensure_dirs", lambda: None)
    monkeypatch.setattr(hooks_mod, "HOOKS_CONFIG_PATH", evi_home / "hooks.toml")
    monkeypatch.setattr("evi.guardrails.GUARDRAILS_PATH", evi_home / "guardrails.toml")
    monkeypatch.setattr("evi.plugins.plugin_dirs", lambda root=None: [])
    (evi_home / "config.toml").write_text("[tools]\nshell = true\n" + _QUIET, encoding="utf-8")
    cwd = tmp_path / "process-cwd"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    return evi_home


def _workflow(home: Path, *steps: tuple[str, bool]) -> None:
    body = "".join(
        f'[[steps]]\nid = "{sid}"\nprompt = "run it"\n' + ("parallel = true\n" if par else "")
        for sid, par in steps
    )
    (home / "workflows" / "w.toml").write_text('name = "w"\n\n' + body, encoding="utf-8")


def _run(monkeypatch, fake_llm, *args: str, steps: int = 1, ask=None, terminal=False):
    """`evi workflow run w ARGS`, each step's model calling run_command once.
    `ask` is the terminal prompt the built agents carry; `terminal` whether
    a prompt can reach the user."""
    import evi.apps.cli.main as cli_main

    built: list[Agent] = []

    def build(*_a, **_k):
        client = fake_llm([fake_llm.call(("run_command", {"command": MARK})),
                           fake_llm.text("done")])
        agent = Agent(client=client, config=Config.load(), tools=[REGISTRY["run_command"]],
                      permission_callback=ask, permission_batch_callback=None)
        built.append(agent)
        return agent

    monkeypatch.setattr(cli_main, "_build_agent", build)
    monkeypatch.setattr(cli_main, "_can_ask_about_trust", lambda: terminal)
    r = CliRunner().invoke(cli_main.app, ["workflow", "run", "w", *args])
    return r, built


def _marker() -> bool:
    return (Path.cwd() / "marker.txt").exists()


def test_nobody_to_ask_denies_and_says_how_to_opt_back_in(home, monkeypatch, fake_llm):
    _workflow(home, ("a", False))
    r, built = _run(monkeypatch, fake_llm, "--json")
    assert r.exit_code == 0, r.output
    assert "policy: deny" in r.stderr and "--yes" in r.stderr
    assert json.loads(r.stdout) == {"a": "done"}
    assert built[0].auto_all is False and built[0].permission_callback is None
    assert not _marker()


def test_unattended_policy_allow_is_honoured_by_default(home, monkeypatch, fake_llm):
    (home / "config.toml").write_text(
        '[tools]\nshell = true\n' + _QUIET + '[auto]\nunattended_policy = "allow"\n',
        encoding="utf-8")
    _workflow(home, ("a", False))
    r, _ = _run(monkeypatch, fake_llm, "--json")
    assert r.exit_code == 0 and "policy: allow" in r.stderr and _marker()


def test_policy_deny_refuses_even_when_unattended_allows(home, monkeypatch, fake_llm):
    (home / "config.toml").write_text(
        '[tools]\nshell = true\n' + _QUIET + '[auto]\nunattended_policy = "allow"\n',
        encoding="utf-8")
    _workflow(home, ("a", False))
    r, _ = _run(monkeypatch, fake_llm, "--json", "--policy", "deny")
    assert r.exit_code == 0 and "policy: deny" in r.stderr and not _marker()


def test_yes_auto_approves_as_before(home, monkeypatch, fake_llm):
    _workflow(home, ("a", False))
    r, built = _run(monkeypatch, fake_llm, "--json", "--yes")
    assert r.exit_code == 0, r.output
    assert "allow-all (--yes)" in r.stderr and built[0].auto_all and _marker()


def test_at_a_terminal_each_call_is_asked_naming_its_step(home, monkeypatch, fake_llm):
    asked: list = []

    def ask(name, args, category):
        asked.append(name)
        return False

    _workflow(home, ("first", False))
    r, _ = _run(monkeypatch, fake_llm, ask=ask, terminal=True)
    assert r.exit_code == 0, r.output
    assert asked == ["run_command"] and "step first asks" in r.stdout
    assert "policy ask" in r.stdout and not _marker()


def test_parallel_steps_ask_one_at_a_time(home, monkeypatch, fake_llm):
    import evi.apps.cli.main as cli_main

    held: list = []

    def ask(name, args, category):
        held.append(cli_main._WORKFLOW_ASK_LOCK.locked())
        return False

    _workflow(home, ("one", True), ("two", True))
    r, _ = _run(monkeypatch, fake_llm, ask=ask, terminal=True)
    assert r.exit_code == 0, r.output
    assert held == [True, True]
    assert "step one asks" in r.stdout and "step two asks" in r.stdout


def test_implementer_holds_each_step_to_the_policy(home, monkeypatch, fake_llm):
    _workflow(home, ("a", False))
    r, built = _run(monkeypatch, fake_llm, "--json", "--policy", "implementer")
    assert r.exit_code == 0, r.output
    assert "policy: implementer" in r.stderr
    assert built[0].policy is not None and built[0].policy.name == "implementer"
    assert not _marker()


@pytest.mark.parametrize(("args", "terminal", "words"), [
    (["--policy", "implementer", "--yes"], False, "can't be used together"),
    (["--policy", "sometimes"], False, "unknown --policy"),
    (["--policy", "ask"], False, "needs a terminal"),
    (["--policy", "ask", "--json"], True, "needs a terminal"),
])
def test_usage_errors(home, monkeypatch, fake_llm, args, terminal, words):
    _workflow(home, ("a", False))
    r, built = _run(monkeypatch, fake_llm, *args, terminal=terminal)
    assert r.exit_code == 2 and words in r.output and not built
