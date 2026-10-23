"""CLI help keeps a literal [section] (Typer renders help with Rich markup,
which takes an unescaped [llm] as a style tag and drops it)."""

from __future__ import annotations

import re

from typer.main import get_command
from typer.testing import CliRunner

from evi.apps.cli.main import app

runner = CliRunner()
# Rich style names a help text may use on purpose.
_STYLES = {"red", "green", "yellow", "blue", "cyan", "magenta", "white", "black", "dim",
           "bold", "italic", "underline"}
# A [word] in a help text, escaped (\[word], which renders) or not (which Rich drops).
_WORD = re.compile(r"\\?\[([a-z_][a-z0-9_.-]*)\]")


def _help(*path: str) -> str:
    """The rendered --help as one line: Rich wraps it in a box at its own width."""
    out = runner.invoke(app, [*path, "--help"]).stdout
    return " ".join(out.replace("│", " ").split())


def test_backend_use_and_eval_baseline_record_keep_llm():
    assert "copies it into [llm]" in _help("backend", "use")
    assert "or [llm]." in _help("eval", "baseline", "record")


def test_no_command_help_loses_a_bracketed_word():
    wanted: list[tuple[list[str], str]] = []

    def collect(cmd, path: list[str]) -> None:
        for text in [cmd.help or "", *(getattr(p, "help", "") or "" for p in cmd.params)]:
            wanted.extend((path, w) for w in _WORD.findall(text) if w not in _STYLES)
        commands = getattr(cmd, "commands", None)  # a group (click.Group is not typer's)
        if isinstance(commands, dict):
            for name, sub in commands.items():
                collect(sub, [*path, name])

    collect(get_command(app), [])
    assert wanted, "the walk found no [word] at all: it is not reaching the commands"
    lost = [f"{' '.join(path)}: [{word}]" for path, word in wanted
            if f"[{word}]" not in _help(*path)]
    assert lost == [], "help that drops a [word]:\n" + "\n".join(lost)
