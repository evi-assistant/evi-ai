"""Tests for the shell command tool (run_command)."""

from __future__ import annotations

import sys

from evi import workdir
from evi.tools.base import REGISTRY
from evi.tools.shell import run_command


def test_registered_category():
    assert "run_command" in REGISTRY
    assert REGISTRY["run_command"].category == "shell"


def test_runs_and_captures_exit_and_output():
    out = run_command(f'{sys.executable} -c "print(\'shell-hi\')"')
    assert "shell-hi" in out
    assert "exit 0" in out


def test_empty_command_errors():
    assert run_command("").startswith("ERROR")


def test_nonzero_exit_reported():
    out = run_command(f'{sys.executable} -c "import sys; sys.exit(7)"')
    assert "exit 7" in out


def test_runs_in_session_cwd(tmp_path):
    (tmp_path / "marker.txt").write_text("x", encoding="utf-8")
    tok = workdir.set_cwd(tmp_path)
    try:
        # List the cwd; the marker file should be visible.
        out = run_command(
            f'{sys.executable} -c "import os; print(os.listdir())"'
        )
    finally:
        workdir.reset(tok)
    assert "marker.txt" in out


def test_explicit_cwd_missing_errors(tmp_path):
    assert run_command("echo hi", cwd=str(tmp_path / "nope")).startswith("ERROR")
