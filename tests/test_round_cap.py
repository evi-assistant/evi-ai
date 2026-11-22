"""The interactive round cap (loop-interactive-round-cap): a chat turn takes
at most [llm] max_rounds model round trips (`evi chat --max-rounds` for a
session), the last with no tools and a hand-off, and `/continue` (the web
UI's Continue) starts a turn that picks it up."""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

import evi.tools.fs  # noqa: F401  registers read_file
from evi.config import Config
from evi.llm.agent import (
    CONTINUE_PROMPT,
    DEFAULT_ROUND_CAP,
    MAX_TURNS_REASON,
    Agent,
    Done,
)
from evi.tools.base import REGISTRY
from tests.conftest import llm_call, llm_text


def _agent(client, work: Path, max_rounds=None) -> Agent:
    cfg = Config()
    if max_rounds is not None:
        cfg.llm.max_rounds = max_rounds
    agent = Agent(client=client, config=cfg, transcripts=None, unattended_policy="allow",
                  tools=[REGISTRY["read_file"]])
    agent.cwd = str(work)
    return agent


def _read():
    return llm_call(("read_file", {"path": "f.txt"}))


def test_a_chat_turn_takes_max_rounds_round_trips(fake_llm, tmp_path):
    (tmp_path / "f.txt").write_text("x\n", encoding="utf-8")
    client = fake_llm([_read(), _read(), llm_text("Done: read it twice. Next: read it again.")])
    agent = _agent(client, tmp_path, max_rounds=3)
    events = list(agent.chat("go"))
    assert events[-1] == Done(MAX_TURNS_REASON)
    assert [bool(r.get("tools")) for r in client.requests] == [True, True, False]


def test_the_default_cap_is_six_and_an_explicit_max_turns_still_wins(fake_llm, tmp_path):
    assert DEFAULT_ROUND_CAP == 6 and Config().llm.max_rounds == 6
    (tmp_path / "f.txt").write_text("x\n", encoding="utf-8")
    client = fake_llm([_read(), llm_text("handed off")])
    events = list(_agent(client, tmp_path, max_rounds=50).chat("go", max_turns=2))
    assert events[-1] == Done(MAX_TURNS_REASON) and len(client.requests) == 2


@pytest.mark.parametrize("value, want", [(4, 4), (0, 6), (-2, 6), ("x", 6), (None, 6)])
def test_round_cap_reads_the_config_and_falls_back_to_the_default(value, want):
    agent = Agent(client=None, config=Config(), tools=[], transcripts=None)
    agent.config.llm.max_rounds = value
    assert agent.round_cap() == want


def test_the_session_override_wins_over_the_config():
    agent = Agent(client=None, config=Config(), tools=[], transcripts=None)
    agent.config.llm.max_rounds = 4
    agent.round_cap_override = 9
    assert agent.round_cap() == 9


def test_continue_chat_uses_the_cap_too(fake_llm, tmp_path):
    (tmp_path / "f.txt").write_text("x\n", encoding="utf-8")
    client = fake_llm([llm_text("first"), _read(), llm_text("hand-off")])
    agent = _agent(client, tmp_path, max_rounds=2)
    list(agent.chat("hi"))
    agent.rewind_to_last_user()
    assert list(agent.continue_chat())[-1] == Done(MAX_TURNS_REASON)


def test_the_max_rounds_key_loads_from_config_toml(tmp_path, monkeypatch):
    import evi.config as config_mod

    path = tmp_path / "config.toml"
    path.write_text("[llm]\nmax_rounds = 15\n", encoding="utf-8")
    monkeypatch.setattr(config_mod, "CONFIG_PATH", path)
    assert Config.load().llm.max_rounds == 15


# ---- /continue ------------------------------------------------------------------------


def test_slash_continue_sends_the_continue_prompt_in_the_cli_and_the_web():
    import evi.apps.cli.main as cli_main
    import evi.apps.web.server as server_mod
    from evi.commands import CommandStore

    agent = Agent(client=None, config=Config(), tools=[], transcripts=None)
    assert cli_main._dispatch_slash("/continue", agent, CommandStore()) == CONTINUE_PROMPT
    out = server_mod._handle_slash(agent, "/continue", CommandStore())
    assert (out.handled, out.expand_to) == (False, CONTINUE_PROMPT)
    assert "/continue" in server_mod._handle_slash(agent, "/help", CommandStore()).text


def test_the_repl_says_how_to_go_on_after_the_cap_and_continue_does(
    monkeypatch, tmp_path, capsys, fake_llm
):
    if threading.current_thread() is not threading.main_thread():
        pytest.skip("the REPL installs signal handlers on the main thread only")
    from tests.test_agent_cancel import _repl, _run

    (tmp_path / "f.txt").write_text("x\n", encoding="utf-8")
    client = fake_llm([_read(), llm_text("Done: half. Next: the rest."),
                       llm_text("did the rest")])
    agent = _agent(client, tmp_path, max_rounds=2)
    cli_main, _reads = _repl(monkeypatch, tmp_path, ["do it", "/continue"])
    _run(cli_main, agent)
    out = capsys.readouterr().out
    assert "stopped after 2 rounds" in out and "/continue goes on" in out
    assert "did the rest" in out
    users = [m["content"] for m in agent.history if m.get("role") == "user"]
    assert users[-1] == CONTINUE_PROMPT


def test_evi_chat_max_rounds_sets_the_session_override(monkeypatch):
    from typer.testing import CliRunner

    import evi.apps.cli.main as cli_main

    agent = Agent(client=None, config=Config(), tools=[], transcripts=None)
    seen: list[Agent] = []
    monkeypatch.setattr(cli_main, "_project_trust_prompt", lambda: None)
    monkeypatch.setattr(cli_main, "_build_agent", lambda *a, **k: agent)
    monkeypatch.setattr(cli_main, "_run_repl", seen.append)
    r = CliRunner().invoke(cli_main.app, ["chat", "--max-rounds", "12"])
    assert r.exit_code == 0, r.output
    assert seen == [agent] and agent.round_cap() == 12


def test_the_web_ui_offers_continue_after_the_cap():
    root = Path(__file__).resolve().parent.parent
    app = (root / "evi/apps/web/static/app.js").read_text(encoding="utf-8")
    assert "ev.reason === 'max_turns_reached'" in app and "sendMessage('/continue')" in app
