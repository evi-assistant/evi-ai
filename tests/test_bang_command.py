"""Tests for the `!cmd` REPL shell passthrough (_run_bang_command)."""

from __future__ import annotations

import sys

from evi.apps.cli.main import _run_bang_command


class _FakeAgent:
    def __init__(self) -> None:
        self.history: list[dict] = []


def test_bang_runs_and_folds_output_into_history() -> None:
    agent = _FakeAgent()
    _run_bang_command(agent, f'{sys.executable} -c "print(\'hello-bang\')"')
    assert len(agent.history) == 1
    entry = agent.history[0]
    assert entry["role"] == "user"
    assert "hello-bang" in entry["content"]
    assert "exit 0" in entry["content"]


def test_bang_empty_is_noop() -> None:
    agent = _FakeAgent()
    _run_bang_command(agent, "")
    assert agent.history == []


def test_bang_nonzero_exit_recorded() -> None:
    agent = _FakeAgent()
    _run_bang_command(agent, f'{sys.executable} -c "import sys; sys.exit(3)"')
    assert agent.history and "exit 3" in agent.history[0]["content"]
