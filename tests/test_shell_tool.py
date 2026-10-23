"""Tests for the shell command tool (run_command)."""

from __future__ import annotations

import os
import sys
from pathlib import Path

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


def test_a_unc_folder_behind_a_mapped_drive_runs_cmd_there_with_full_syntax():
    from evi.permissions import _is_network_path
    from evi.tools.shell import _mapped_spelling

    here = Path(__file__).resolve().parent  # resolve() spells a mapped drive as UNC
    if sys.platform != "win32" or not _is_network_path(here):
        pytest.skip("needs Windows and this checkout on a mapped network drive")
    mapped = _mapped_spelling(here)
    assert mapped is not None and len(mapped.drive) == 2
    assert os.path.samefile(mapped, here)
    out = run_command('python -c "import os; print(os.getcwd())" && echo second', cwd=str(here))
    assert out.startswith("exit 0") and "second" in out, out
    assert str(mapped).lower() in out.lower() and "UNC paths are not supported" not in out


def test_a_bare_python_is_the_interpreter_the_checks_use(tmp_path):
    from evi.tools.shell import _with_own_python

    assert _with_own_python("python3 -m pytest -q", tmp_path) == (
        f'"{sys.executable}" -m pytest -q')
    assert _with_own_python("pythonista run", tmp_path) == "pythonista run"
    assert _with_own_python("ruff check .", tmp_path) == "ruff check ."
    out = run_command('python -c "import sys; print(sys.prefix)"', cwd=str(tmp_path))
    assert out.splitlines()[-1].strip() == sys.prefix, out
    here = Path(__file__).resolve().parent  # through a mapped drive here, if any
    out = run_command('python -c "import sys; print(sys.prefix)"', cwd=str(here))
    assert out.splitlines()[-1].strip() == sys.prefix, out
