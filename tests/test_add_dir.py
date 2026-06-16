"""Tests for the /add-dir REPL builtin (_handle_add_dir)."""

from __future__ import annotations

from pathlib import Path

from evi.apps.cli.main import _handle_add_dir
from evi.config import Config


class _FakeAgent:
    def __init__(self) -> None:
        self.config = Config()


def test_add_dir_appends_trusted_dir(tmp_path: Path) -> None:
    agent = _FakeAgent()
    target = tmp_path / "proj"
    target.mkdir()

    res = _handle_add_dir(agent, str(target), None)
    assert res == "continue"
    assert str(target.resolve()) in agent.config.auto.trusted_dirs


def test_add_dir_dedupes(tmp_path: Path) -> None:
    agent = _FakeAgent()
    target = tmp_path / "proj"
    target.mkdir()
    _handle_add_dir(agent, str(target), None)
    _handle_add_dir(agent, str(target), None)
    assert agent.config.auto.trusted_dirs.count(str(target.resolve())) == 1


def test_add_dir_rejects_nonexistent(tmp_path: Path) -> None:
    agent = _FakeAgent()
    _handle_add_dir(agent, str(tmp_path / "nope"), None)
    assert agent.config.auto.trusted_dirs == []
