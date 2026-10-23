"""Tests for the shell command tool (run_command)."""

from __future__ import annotations

import os
import sys

import pytest

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


# ---- a folder on a network share (Windows) -------------------------------------------


def test_a_plain_command_becomes_argv_and_shell_syntax_is_refused(tmp_path):
    from evi.tools.shell import _direct_argv

    assert _direct_argv("python3 -m pytest -q tests/x.py", tmp_path, {}) == [
        sys.executable, "-m", "pytest", "-q", "tests/x.py"]
    for command in ("pytest && curl x", "pytest | more", 'git log "-1"', "echo %PATH%",
                    "pytest > out.txt"):
        why = _direct_argv(command, tmp_path, {})
        assert isinstance(why, str) and "network share, where cmd.exe can't run" in why


def _unc_spelling(path):
    """`path` through this machine's administrative share, or None."""
    drive, rest = os.path.splitdrive(str(path))
    if sys.platform != "win32" or len(drive) != 2:
        return None
    unc = rf"\\localhost\{drive[0]}$" + rest
    try:
        return unc if os.path.isdir(unc) else None
    except OSError:
        return None


def test_a_command_in_a_unc_folder_runs_there_not_in_the_windows_folder(tmp_path):
    unc = _unc_spelling(tmp_path)
    if unc is None:
        pytest.skip(r"needs Windows and a reachable \\localhost\<drive>$ share")
    (tmp_path / "where.py").write_text("import os\nprint(os.listdir())\n", encoding="utf-8")
    (tmp_path / "marker.txt").write_text("x", encoding="utf-8")
    out = run_command("python where.py", cwd=unc)
    assert out.startswith("exit 0") and "marker.txt" in out, out
    assert "UNC paths are not supported" not in out
    assert run_command("python where.py && echo hi", cwd=unc).startswith(
        "ERROR: ") and not (tmp_path / "hi").exists()
