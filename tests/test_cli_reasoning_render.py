"""The CLI renders a turn's thinking and its answer as plain text: no Rich
markup (a closing tag printed on its own, or a '[/]' in the text, raised
MarkupError and ended `evi chat`) and no control characters (the model's or
the server's text must not drive the terminal)."""

from __future__ import annotations

import inspect
import io
from types import SimpleNamespace

import pytest
from rich.console import Console

import evi.apps.cli.main as cli_main
from evi.config import Config
from evi.llm.agent import Agent


def _ns_chunk(finish: str | None = None, **delta) -> SimpleNamespace:
    # A duck-typed streamed chunk, as the fake_llm fixture replays them.
    fields = {"content": None, "tool_calls": None, **delta}
    return SimpleNamespace(choices=[SimpleNamespace(
        delta=SimpleNamespace(**fields), finish_reason=finish, logprobs=None)], usage=None)


def _console(monkeypatch, terminal: bool = False) -> io.StringIO:
    out = io.StringIO()
    monkeypatch.setattr(cli_main, "console",
                        Console(file=out, force_terminal=terminal, width=10_000))
    return out


def _agent(fake_llm, thinking: str, answer: str) -> Agent:
    # The thinking arrives in <think> tags, which the agent's tag parser turns
    # into ThinkingDelta events.
    client = fake_llm([[_ns_chunk(content=f"<think>{thinking}</think>"),
                        _ns_chunk(content=answer, finish="stop")]])
    return Agent(client=client, config=Config(), tools=[], transcripts=None)


@pytest.mark.parametrize("thinking", ["six times seven", "see [/dim] tag", "a [/] b",
                                      "[dim italic]open"])
def test_print_turn_shows_thinking_then_the_answer_as_typed(fake_llm, monkeypatch, thinking):
    out = _console(monkeypatch)
    cli_main._print_turn(_agent(fake_llm, thinking, "It is [bold]42[/bold]."), "6*7?")
    text = out.getvalue()
    assert thinking in text  # shown, not parsed as markup
    assert "It is [bold]42[/bold]." in text
    assert text.index(thinking) < text.index("It is")


@pytest.mark.parametrize("thinking", ["six times seven", "a [/] b"])
def test_the_repl_survives_thinking_followed_by_an_answer(fake_llm, monkeypatch, tmp_path,
                                                         thinking):
    from tests.test_agent_cancel import _repl, _run

    out = _console(monkeypatch)
    _cli, reads = _repl(monkeypatch, tmp_path, ["what is 6*7?"])
    _run(cli_main, _agent(fake_llm, thinking, "It is 42."))
    assert reads == ["what is 6*7?"]
    assert thinking in out.getvalue() and "It is 42." in out.getvalue()


def test_no_escape_sequence_from_the_model_reaches_the_terminal(fake_llm, monkeypatch):
    # Exploit regression: an OSC title change, a screen clear and an OSC 52
    # clipboard write in the thinking and the answer are shown escaped.
    out = _console(monkeypatch, terminal=True)
    thinking = "think \x1b]0;pwned\x1b\\ then \x1b]52;c;ZXZpbA==\x07"
    cli_main._print_turn(_agent(fake_llm, thinking, "answer \x1b[2J done"), "q")
    text = out.getvalue()
    for raw in ("\x1b]0;", "\x1b]52;", "\x1b[2J", "\x07"):
        assert raw not in text
    assert "\\x1b]0;pwned" in text and "\\x1b[2J" in text


def test_print_model_text_takes_no_markup_and_no_emoji_codes(monkeypatch):
    out = _console(monkeypatch)
    cli_main._print_model_text("[/dim italic] :thumbs_up: [link=http://x]y[/link]",
                               "dim italic")
    cli_main._print_model_text(" one\ntwo")  # a newline stays a newline
    assert out.getvalue() == "[/dim italic] :thumbs_up: [link=http://x]y[/link] one\ntwo"


def test_no_thinking_renderer_prints_a_bare_markup_tag():
    # The four renderers (chat REPL, review, voice loop, _print_turn) all go
    # through _print_model_text; a lone "[/dim italic]" print always raised.
    source = inspect.getsource(cli_main)
    assert 'console.print("[dim italic]"' not in source
    assert 'console.print("[/dim italic]"' not in source
