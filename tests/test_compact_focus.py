"""/compact <focus> in the REPL and the web chat puts the focus in the
summary request word for word; a bare /compact puts no focus line."""

import io
from types import SimpleNamespace

from rich.console import Console

import evi.apps.cli.main as cli_main
import evi.apps.web.server as server_mod
from evi.commands import CommandStore
from evi.config import Config
from evi.llm.agent import Agent


class _Summarizer:
    """A client for compaction's non-streaming summary request."""

    def __init__(self, *replies):
        self.replies, self.requests = list(replies), []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    def create(self, **kw):
        self.requests.append(kw)
        text = self.replies.pop(0)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=text))])


def _agent(client):
    agent = Agent(client=client, config=Config(), tools=[], transcripts=None)
    for i in range(20):
        agent.history += [{"role": "user", "content": f"q{i}"},
                          {"role": "assistant", "content": f"a{i}"}]
    return agent


def _cli(agent, args, tmp_path, monkeypatch):
    monkeypatch.setattr(cli_main, "console", Console(file=io.StringIO()))
    cli_main._handle_compact(agent, args, CommandStore(root=tmp_path))


def _prompt(client) -> str:
    return client.requests[0]["messages"][1]["content"]


def test_the_repl_compact_command_passes_its_focus(tmp_path, monkeypatch):
    client = _Summarizer("s")
    _cli(_agent(client), "  keep the API keys question  ", tmp_path, monkeypatch)
    assert "Pay particular attention to: keep the API keys question\n" in _prompt(client)


def test_the_web_compact_command_passes_its_focus(tmp_path):
    client = _Summarizer("s")
    server_mod._handle_slash(_agent(client), "/compact keep the API keys question",
                             CommandStore(root=tmp_path))
    assert "Pay particular attention to: keep the API keys question\n" in _prompt(client)


def test_a_bare_compact_has_no_focus_line(tmp_path, monkeypatch):
    client = _Summarizer("s")
    _cli(_agent(client), "", tmp_path, monkeypatch)
    client2 = _Summarizer("s")
    server_mod._handle_slash(_agent(client2), "/compact", CommandStore(root=tmp_path))
    for c in (client, client2):
        assert "Pay particular attention" not in _prompt(c)
