"""On Windows a command line runs in PowerShell by default (evi.shells), with
exit codes that mean what they do in other shells; [tools] windows_shell =
"cmd" keeps cmd.exe."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from evi import shells
from evi.config import Config
from evi.tools.shell import _with_own_python, run_command

windows = pytest.mark.skipif(os.name != "nt", reason="PowerShell is the Windows shell")


def test_the_choice_defaults_to_powershell_and_takes_cmd(monkeypatch, tmp_path):
    assert Config().tools.windows_shell == "powershell" and shells.windows_shell() == "powershell"
    path = tmp_path / "config.toml"
    monkeypatch.setattr("evi.config.CONFIG_PATH", path)
    for written, read in (("cmd", "cmd"), ("CMD", "cmd"), ("fish", "powershell")):
        path.write_text(f'[tools]\nwindows_shell = "{written}"\n', encoding="utf-8")
        assert shells.windows_shell() == read


def test_a_quoted_program_gets_the_call_operator():
    assert shells.as_powershell('"C:/tools/x.exe" --v') == '& "C:/tools/x.exe" --v'
    assert shells.as_powershell("git status") == "git status"
    argv = shells.powershell_argv("git status")
    assert argv[-2] == "-Command" and "git status" in argv[-1]
    with pytest.raises(ValueError, match="script file"):
        shells.powershell_argv("x" * 40_000)


def test_a_bare_python_becomes_a_call_in_powershell(tmp_path):
    assert _with_own_python("python -m pytest", tmp_path, powershell=True) == (
        f'& "{sys.executable}" -m pytest')


@windows
def test_exit_codes_come_through_powershell(tmp_path):
    assert run_command('python -c "import sys; sys.exit(3)"', cwd=str(tmp_path)).startswith(
        "exit 3")
    assert run_command("exit 7", cwd=str(tmp_path)).startswith("exit 7")
    failed = run_command("Get-Item no_such_file_here", cwd=str(tmp_path))
    assert failed.startswith("exit 1") and "Cannot find path" in failed and "CLIXML" not in failed
    ok = run_command("Write-Output first; Write-Output 'ünïcødé ✓'", cwd=str(tmp_path))
    assert ok.startswith("exit 0") and "first" in ok and "ünïcødé ✓" in ok


@windows
def test_powershell_syntax_and_a_quoted_program_run(tmp_path):
    out = run_command('$n = 6 * 7; Write-Output "n is $n"', cwd=str(tmp_path))
    assert out.startswith("exit 0") and "n is 42" in out
    out = run_command(f'"{sys.executable}" -c "print(41 + 1)"', cwd=str(tmp_path))
    assert out.startswith("exit 0") and "42" in out


@windows
def test_a_folder_on_a_network_share_runs_powershell_there():
    from evi.permissions import _is_network_path

    here = Path(__file__).resolve().parent  # resolve() spells a mapped drive as UNC
    if not _is_network_path(here):
        pytest.skip("needs this checkout on a network share")
    out = run_command("(Get-Location).Path; Write-Output second", cwd=str(here))
    assert out.startswith("exit 0") and "second" in out, out
    assert "UNC paths are not supported" not in out


@windows
def test_cmd_stays_available(monkeypatch, tmp_path):
    monkeypatch.setattr("evi.shells.windows_shell", lambda: "cmd")
    out = run_command("echo one && echo two", cwd=str(tmp_path))
    assert out.startswith("exit 0") and "one" in out and "two" in out
