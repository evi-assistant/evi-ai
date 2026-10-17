"""A CLI REPL session resumes with the history it had (polish-small-fixes,
part 1).

`evi sessions resume` rebuilds a session's history from its transcript
(evi.sessions.history_from_transcript, replay=True). Agent.chat logs every
message it adds, but the REPL added some of its own straight to agent.history
without logging them: the `!cmd` output it folds into the context, and the
user/assistant exchange of `/ultra <task>` and of a turn auto-run through the
ultracode pipeline. A resume lost them, and a compaction afterwards (whose
`kept` counts logged messages) replayed its kept tail one message off. The REPL
now logs them as it adds them (_add_to_history).

The test drives the real REPL loop (_run_repl, input scripted) on the shared
fake_llm, through every kind of message a REPL session gets outside a plain
prompt: `!cmd` output, /ultra and auto-ultracode exchanges, steering applied
mid-turn, a /goal and a /plan folded into the prompt, and a /compact summary;
then compares the live history with the transcript's replay and with what
`evi sessions resume` restores. Transcripts, eVi's home and the REPL's
commands folder are under tmp_path, never ~/.evi.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from evi import sessions
from evi.config import Config
from evi.llm.agent import Agent
from evi.sessions import find_session, history_from_transcript, session_files
from evi.tools.base import Tool
from evi.transcripts import COMPACTION, TranscriptStore

SUMMARY = "they said hello, ran commands, set a goal and planned"


# ---- helpers -------------------------------------------------------------


@pytest.fixture
def env(monkeypatch, tmp_path):
    """eVi's home and transcripts under tmp_path, and a REPL whose input is
    `env.typed` (EOF after it), with no atexit hook left behind and
    EVI_INTERACTIVE restored after the test. `env.before_read`, if set, is
    called each time the REPL asks for its next line (the previous one is
    done by then)."""
    import evi.apps.cli.main as cli_main
    import evi.config as config_mod
    import evi.repl_input as repl_input
    from evi.commands import CommandStore

    home = tmp_path / "evi-home"
    home.mkdir()
    monkeypatch.setenv("EVI_HOME", str(home))
    monkeypatch.delenv("EVI_SAFE_MODE", raising=False)
    monkeypatch.setattr(config_mod, "HOME", home)
    monkeypatch.setattr(config_mod, "CONFIG_PATH", home / "config.toml")
    root = tmp_path / "transcripts"
    monkeypatch.setattr(sessions, "TRANSCRIPTS_DIR", root)
    monkeypatch.setattr(sessions, "HANDOFF_DIR", tmp_path / "handoff")
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.chdir(work)  # where a `!cmd` runs

    state = SimpleNamespace(root=root, typed=[], cli=cli_main, before_read=None)

    class _Input:
        def __init__(self, agent):
            self.lines = iter(state.typed)

        def read(self, prompt):
            if state.before_read is not None:
                state.before_read()
            try:
                return next(self.lines)
            except StopIteration:
                raise EOFError

    monkeypatch.setattr(repl_input, "ReplInput", _Input)
    monkeypatch.setattr(
        cli_main, "CommandStore", lambda *a, **k: CommandStore(root=tmp_path / "commands")
    )
    monkeypatch.setattr(cli_main.atexit, "register", lambda *a, **k: None)
    monkeypatch.setenv("EVI_INTERACTIVE", "0")  # restored after; the REPL sets it
    return state


def _scripted(fake_llm, turns: list, summary: str = SUMMARY):
    """The shared fake_llm client for `turns`; a non-streamed request (the
    compaction summary) gets `summary`."""
    client = fake_llm(turns)
    completions = client.chat.completions
    scripted = completions.create

    def create(**kw):
        if not kw.get("stream"):
            msg = SimpleNamespace(content=summary)
            return SimpleNamespace(choices=[SimpleNamespace(message=msg)])
        return scripted(**kw)

    completions.create = create
    return client


def _agent(client, root: Path, *, tools=(), keep: int | None = None) -> Agent:
    cfg = Config()
    if keep is not None:
        cfg.llm.compact_keep_recent = keep
    return Agent(client=client, config=cfg, tools=list(tools),
                 transcripts=TranscriptStore(root=root), session_id="S1",
                 unattended_policy="allow")


def _replay(root: Path, sid: str = "S1") -> list[dict]:
    path = find_session(sid, root=root)
    assert path is not None, f"no transcript for {sid}"
    return history_from_transcript(path, replay=True)


def _resume(monkeypatch, env, fake_llm, sid: str = "S1") -> Agent:
    """`evi sessions resume <sid>`, with the REPL replaced by a recorder: the
    agent as the resume left it, before any new prompt."""
    from typer.testing import CliRunner

    cli_main = env.cli
    seen: list[Agent] = []
    monkeypatch.setattr(cli_main, "_project_trust_prompt", lambda *a, **k: None)
    monkeypatch.setattr(cli_main, "_build_agent",
                        lambda *a, **k: _agent(fake_llm([]), env.root))
    monkeypatch.setattr(cli_main, "_run_repl", seen.append)
    r = CliRunner().invoke(cli_main.app, ["sessions", "resume", sid])
    assert r.exit_code == 0, r.output
    assert len(seen) == 1
    restored = seen[0]
    assert f"{len(restored.history) - 1} messages restored" in r.output
    return restored


def _contents(history: list[dict]) -> list[str]:
    return [str(m.get("content") or "") for m in history]


# ---- the REPL session ------------------------------------------------------


def test_a_repl_session_resumes_with_exactly_the_history_it_had(
    monkeypatch, env, fake_llm
) -> None:
    holder: list[Agent] = []

    def look(x: str = "") -> str:
        # A follow-up typed while the turn runs: applied at its next step.
        holder[0].steer("also check the docs")
        return f"looked at {x}"

    tool = Tool(name="look", description="look at something", category="fs",
                parameters={"type": "object", "properties": {"x": {"type": "string"}}},
                func=look)
    client = _scripted(fake_llm, [
        fake_llm.text("hi there"),                                 # hello
        fake_llm.call(("look", {"x": "parser"})),                  # look at the parser
        fake_llm.text("looked, and checked the docs"),
        fake_llm.text("it printed bang-output"),                   # after the !cmd
        fake_llm.text("working towards the goal"),                 # under /goal
        fake_llm.text("1. change the parser 2. test it"),          # /plan
        fake_llm.text("carrying on after the summary"),            # after /compact
    ])
    agent = _agent(client, env.root, tools=[tool], keep=4)
    holder.append(agent)
    ultra_tasks: list[str] = []

    def ultracode(task: str, **_k):
        ultra_tasks.append(task)
        return SimpleNamespace(answer=f"ultracode answer to: {task}")

    monkeypatch.setattr(env.cli, "_run_ultracode_cli", ultracode)
    substantive = "please refactor the whole parser module for me now"
    env.typed = [
        "hello",
        "look at the parser",
        "!echo bang-output",
        "what did it print?",
        "/ultra build the tokenizer",
        "/ultra",            # auto-ultracode on: a substantive turn runs the pipeline
        substantive,
        "/ultra",            # and off again
        "/goal ship the parser",
        "work on it",
        "/goal clear",
        "/plan sketch the change",
        "!echo second-bang",
        "/compact",
        "carry on",
    ]
    # After every line, before the next: a resume then would have restored
    # the history as it was (so the steering, the goal and the plan prompts
    # line up too, before the compaction folds them into its summary).
    after: list[tuple[int, list[dict], list[dict]]] = []

    def check() -> None:
        path = find_session("S1", root=env.root)
        replayed = history_from_transcript(path, replay=True) if path else []
        after.append((len(after), replayed, list(agent.history[1:])))

    env.before_read = check

    env.cli._run_repl(agent)

    assert len(after) == len(env.typed) + 1  # once per line, and at EOF
    for step, replayed, had in after:
        typed = env.typed[step - 1] if step else "(start)"
        assert replayed == had, f"misaligned after {typed!r}"
    assert any("also check the docs" == m["content"] for m in after[2][2])  # the steering
    assert client.chat.completions.calls == 7  # every scripted turn ran
    assert ultra_tasks == ["build the tokenizer", substantive]
    live = agent.history[1:]
    text = "\n".join(_contents(live))
    # Every kind of message the session got outside a plain prompt is there:
    # the summary (the compaction), the second !cmd's output after it (in the
    # kept tail), the steering, the goal and plan folded into their prompts.
    assert live[0]["content"].startswith("[compacted:") and SUMMARY in live[0]["content"]
    assert "[ran shell command: `echo second-bang` → exit 0]\nsecond-bang" in text
    assert "carrying on after the summary" in text
    lines = [
        line for f in session_files("S1", root=env.root)
        for line in f.read_text(encoding="utf-8").splitlines()
    ]
    assert sum('"type": "compaction"' in line for line in lines) == 1
    logged = "\n".join(lines)
    for said in ("also check the docs", "[ongoing goal: ship the parser]", "Plan-only mode",
                 "echo bang-output", "ultracode answer to: build the tokenizer",
                 f"ultracode answer to: {substantive}"):
        assert said in logged, said

    # The replay of the transcript is the live history, message for message.
    assert _replay(env.root) == live
    # And `evi sessions resume` restores exactly that.
    restored = _resume(monkeypatch, env, fake_llm)
    assert restored.session_id == "S1"
    assert restored.history[1:] == live


def test_every_repl_added_message_before_a_compaction_lines_up(
    monkeypatch, env, fake_llm
) -> None:
    """Without a compaction to fold them away: each REPL-added message (the
    `!cmd` output, the /ultra and auto-ultracode exchanges) is in the replay,
    in its place, and a resume restores them all."""
    client = _scripted(fake_llm, [fake_llm.text("one"), fake_llm.text("two")])
    agent = _agent(client, env.root)
    monkeypatch.setattr(env.cli, "_run_ultracode_cli",
                        lambda task, **_k: SimpleNamespace(answer=f"ultra: {task}"))
    long_turn = "write the migration script for the users table please"
    env.typed = ["first", "!echo from-the-shell", "/ultra plan it", "/ultra", long_turn,
                 "/ultra", "second"]

    env.cli._run_repl(agent)

    live = agent.history[1:]
    assert [(m["role"], m["content"].splitlines()[0]) for m in live] == [
        ("user", "first"), ("assistant", "one"),
        ("user", "[ran shell command: `echo from-the-shell` → exit 0]"),
        ("user", "plan it"), ("assistant", "ultra: plan it"),
        ("user", long_turn), ("assistant", f"ultra: {long_turn}"),
        ("user", "second"), ("assistant", "two"),
    ]
    assert _replay(env.root) == live
    assert _resume(monkeypatch, env, fake_llm).history[1:] == live


def test_a_compaction_after_a_shell_note_keeps_the_same_tail(
    monkeypatch, env, fake_llm
) -> None:
    """A compaction's `kept` counts the logged messages of the tail it kept:
    with the `!cmd` note unlogged, the replay kept one message too many from
    before the cut. Now the replayed tail is the live one."""
    client = _scripted(fake_llm, [fake_llm.text(t) for t in ("r1", "r2", "r3")])
    agent = _agent(client, env.root, keep=2)
    env.typed = ["q1", "q2", "q3", "!echo tail-note", "/compact"]

    env.cli._run_repl(agent)

    records = [
        line for f in session_files("S1", root=env.root)
        for line in f.read_text(encoding="utf-8").splitlines() if COMPACTION in line
    ]
    assert len(records) == 1
    live = agent.history[1:]
    assert "tail-note" in live[-1]["content"]
    assert _replay(env.root) == live
    assert _resume(monkeypatch, env, fake_llm).history[1:] == live


def test_no_transcript_still_adds_the_note(monkeypatch, env, fake_llm) -> None:
    """With transcripts off there is nothing to log: the note still joins the
    history, as before."""
    agent = Agent(client=fake_llm([]), config=Config(), tools=[], transcripts=None,
                  unattended_policy="allow")
    env.typed = ["!echo nothing-logged"]
    env.cli._run_repl(agent)
    assert agent.history[-1]["content"].endswith("nothing-logged")
    assert not env.root.exists()


def test_a_disk_error_never_fails_the_repl(monkeypatch, env, fake_llm) -> None:
    class _Full(TranscriptStore):
        def write(self, entry) -> None:
            raise OSError("disk full")

    agent = Agent(client=fake_llm([]), config=Config(), tools=[],
                  transcripts=_Full(root=env.root), unattended_policy="allow")
    env.typed = ["!echo still-here"]
    env.cli._run_repl(agent)
    assert agent.history[-1]["content"].endswith("still-here")
