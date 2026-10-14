"""`evi doctor --dev`: venv, safe.directory, dev tools and line-ending checks.

Every fixture lives under tmp_path: a throwaway git repo, hand-written
pyvenv.cfg files. Nothing here reads or writes ~/.evi.
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

import evi.apps.cli.main as cli_main
from evi import doctor

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="needs git")

GIT_IDENT = ["-c", "user.name=t", "-c", "user.email=t@example.invalid",
             "-c", "commit.gpgsign=false", "-c", "core.autocrlf=false"]


def _git(repo: Path, *args: str) -> str:
    r = subprocess.run(["git", *GIT_IDENT, "-C", str(repo), *args],
                       capture_output=True, text=True, check=True)
    return r.stdout


def _eol_states(repo: Path) -> dict[str, str]:
    """path -> 'i/lf w/crlf' from `git ls-files --eol`."""
    out = {}
    for line in _git(repo, "ls-files", "--eol").splitlines():
        info, _, path = line.partition("\t")
        f = info.split()
        out[path] = f"{f[0]} {f[1]}"
    return out


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A repo shaped like eVi's: eol=lf everywhere, *.ps1 CRLF, binaries untouched."""
    r = tmp_path / "checkout"
    r.mkdir()
    _git(r, "init", "-q")
    (r / ".gitattributes").write_bytes(
        b"* text=auto eol=lf\n*.ps1 text eol=crlf\n*.png binary\n")
    (r / "a.py").write_bytes(b"x = 1\ny = 2\n")
    (r / "b.md").write_bytes(b"# b\n")
    (r / "sp ace.md").write_bytes(b"spaced\n")
    (r / "s.ps1").write_bytes(b"Write-Host hi\r\n")
    (r / "img.png").write_bytes(b"\x89PNG\r\n\x1a\n\x00\r\n")
    _git(r, "add", "-A")
    _git(r, "commit", "-qm", "init")
    return r


def _by_name(checks, name):
    return [c for c in checks if c.name == name]


# --- line endings -----------------------------------------------------------


def test_clean_checkout_reports_ok(repo):
    [c] = doctor.check_eol(repo)
    assert c.status == "ok" and c.fix == ()


def test_crlf_working_files_are_reported_with_a_recheckout_command(repo):
    # The state an editor plus `git add` leaves: CRLF on disk, LF in the index,
    # and stat data that makes git (and `git status`) call the file clean.
    (repo / "a.py").write_bytes(b"x = 1\r\ny = 2\r\n")
    (repo / "sp ace.md").write_bytes(b"spaced\r\n")
    _git(repo, "add", "a.py", "sp ace.md")
    assert _eol_states(repo)["a.py"] == "i/lf w/crlf"
    assert _git(repo, "status", "--porcelain") == ""

    [c] = doctor.check_eol(repo)
    assert c.status == "warn"
    assert "a.py is CRLF, wants LF" in c.detail
    [fix] = c.fix
    assert fix == ("git --literal-pathspecs rm --cached -q -- a.py 'sp ace.md'; "
                   "git --literal-pathspecs checkout HEAD -- a.py 'sp ace.md'")
    # doctor never acts: the file is untouched.
    assert (repo / "a.py").read_bytes() == b"x = 1\r\ny = 2\r\n"

    # The printed command works (a plain `git checkout` would skip these files).
    for part in fix.split("; "):
        subprocess.run(shlex.split(part), cwd=repo, check=True)
    assert (repo / "a.py").read_bytes() == b"x = 1\ny = 2\n"
    assert (repo / "sp ace.md").read_bytes() == b"spaced\n"
    assert doctor.check_eol(repo)[0].status == "ok"
    assert _git(repo, "status", "--porcelain") == ""


def test_lf_ps1_is_reported_because_gitattributes_wants_crlf(repo):
    (repo / "s.ps1").write_bytes(b"Write-Host hi\n")
    _git(repo, "add", "s.ps1")
    [c] = doctor.check_eol(repo)
    assert "s.ps1 is LF, wants CRLF" in c.detail
    assert "git --literal-pathspecs checkout HEAD -- s.ps1" in c.fix[0]


def test_files_with_local_edits_are_kept_out_of_the_recheckout(repo):
    (repo / "a.py").write_bytes(b"x = 1\r\ny = 3\r\n")   # a real edit, CRLF
    (repo / "b.md").write_bytes(b"# b\r\n")              # EOL only
    _git(repo, "add", "b.md")
    [c] = doctor.check_eol(repo)
    assert c.status == "warn"
    assert "local edits (a.py)" in c.detail
    [fix] = c.fix
    assert "b.md" in fix and "a.py" not in fix


def test_crlf_in_the_index_suggests_renormalize(tmp_path):
    r = tmp_path / "old"
    r.mkdir()
    _git(r, "init", "-q")
    (r / "c.txt").write_bytes(b"one\r\ntwo\r\n")
    _git(r, "add", "c.txt")
    _git(r, "commit", "-qm", "crlf blob")  # committed before any .gitattributes
    (r / ".gitattributes").write_bytes(b"* text=auto eol=lf\n")
    _git(r, "add", ".gitattributes")
    _git(r, "commit", "-qm", "attrs")
    assert _eol_states(r)["c.txt"].startswith("i/crlf")

    checks = doctor.check_eol(r)
    [idx] = _by_name(checks, "line endings (index)")
    assert idx.status == "warn" and "c.txt" in idx.detail
    assert idx.fix == ("git add --renormalize .",)
    assert not _by_name(checks, "line endings")  # one fix at a time

    _git(r, "add", "--renormalize", ".")
    assert _eol_states(r)["c.txt"].startswith("i/lf")


def test_binary_files_are_ignored(repo):
    (repo / "img.png").write_bytes(b"\r\n\r\n\x00")
    assert doctor.check_eol(repo)[0].status == "ok"


# --- venvs ------------------------------------------------------------------


def _fake_venv(path: Path, *, home: Path, version: str = "3.13.1",
               command: str | None = None, with_python: bool = True) -> Path:
    path.mkdir(parents=True)
    lines = [f"home = {home}", "include-system-site-packages = false",
             f"version = {version}"]
    if command:
        lines.append(f"command = {command}")
    (path / "pyvenv.cfg").write_text("\n".join(lines) + "\n", encoding="utf-8")
    if with_python:
        exe = doctor.venv_python(path)
        exe.parent.mkdir(parents=True)
        exe.write_bytes(b"")
    return path


def _foreign_venv(path: Path, tmp_path: Path) -> Path:
    gone = tmp_path / "other-machine" / "Python313"  # does not exist here
    return _fake_venv(path, home=gone,
                      command=f"{gone / 'python.exe'} -m venv C:\\evi\\.venv")


def test_foreign_venv_carried_by_the_checkout_is_a_warning(repo, tmp_path):
    _foreign_venv(repo / ".venv", tmp_path)
    checks = doctor.run_dev_checks(repo, collect=False)
    [c] = _by_name(checks, f"venv: {repo / '.venv'}")
    assert c.status == "warn"
    assert "foreign" in c.detail and "other-machine" in c.detail
    assert "another machine" in c.detail
    assert c.fix == ()  # the bootstrap never deletes a checkout's other venvs


def test_foreign_venv_in_use_fails_and_points_at_the_bootstrap(repo, tmp_path):
    venv = _foreign_venv(repo / ".venv", tmp_path)
    checks = doctor.run_dev_checks(repo, venv=venv, collect=False)
    [c] = _by_name(checks, f"venv: {venv}")
    assert c.status == "fail" and "foreign" in c.detail
    assert any("dev-bootstrap" in f for f in c.fix)
    [tools] = _by_name(checks, "dev tools")
    assert tools.status == "warn" and "not usable" in tools.detail


def test_relocated_venv_is_a_warning(tmp_path):
    venv = _fake_venv(tmp_path / "moved", home=Path(sys.base_prefix),
                      command=f"python -m venv {tmp_path / 'original'}")
    c = doctor.inspect_venv(venv)
    assert c.status == "warn" and "created at" in c.detail


def test_old_python_venv_fails(tmp_path):
    venv = _fake_venv(tmp_path / "old", home=Path(sys.base_prefix), version="3.11.9")
    c = doctor.inspect_venv(venv)
    assert c.status == "fail" and "3.12+" in c.detail


def test_missing_interpreter_and_missing_cfg_fail(tmp_path):
    venv = _fake_venv(tmp_path / "noexe", home=Path(sys.base_prefix), with_python=False)
    assert doctor.inspect_venv(venv).status == "fail"
    (tmp_path / "plain").mkdir()
    c = doctor.inspect_venv(tmp_path / "plain")
    assert c.status == "fail" and "pyvenv.cfg" in c.detail


@pytest.mark.skipif(sys.prefix == sys.base_prefix, reason="tests are not running in a venv")
def test_the_running_venv_is_healthy():
    assert doctor.inspect_venv(Path(sys.prefix)).status == "ok"


# --- the rest ---------------------------------------------------------------


def test_safe_directory_fix_is_taken_from_gits_message():
    stderr = (
        "fatal: detected dubious ownership in repository at '//HOST/projects/evi'\n"
        "'//HOST/projects/evi' may refer to a non-local directory\n"
        "To add an exception for this directory, call:\n\n"
        "\tgit config --global --add safe.directory '%(prefix)///HOST/projects/evi'\n"
    )
    assert doctor.safe_directory_fix(stderr) == (
        "git config --global --add safe.directory '%(prefix)///HOST/projects/evi'")
    assert doctor.safe_directory_fix("fatal: not a git repository") is None


def test_outside_a_checkout_fails(tmp_path):
    [c] = doctor.run_dev_checks(tmp_path / "nowhere", collect=False)
    assert c.name == "checkout" and c.status == "fail"


def test_dev_checks_cover_the_toolchain(repo):
    checks = doctor.run_dev_checks(repo, collect=False)
    names = {c.name for c in checks}
    for want in ("checkout", "python", "evi import", "git safe.directory",
                 "dev tools", "pytest-xdist", "playwright", "line endings"):
        assert want in names, want
    assert _by_name(checks, "git safe.directory")[0].status == "ok"
    # the fixture is not the checkout evi is imported from
    assert _by_name(checks, "evi import")[0].status == "warn"
    assert not _by_name(checks, "pytest --collect-only")  # collect=False


def test_collect_only_runs_in_the_checkout(repo):
    (repo / "test_one.py").write_text("def test_one():\n    pass\n", encoding="utf-8")
    [c] = _by_name(doctor.run_dev_checks(repo, collect=True), "pytest --collect-only")
    assert c.status == "ok" and "1 test" in c.detail
    (repo / "test_two.py").write_text("def test_two(:\n", encoding="utf-8")
    [c] = _by_name(doctor.run_dev_checks(repo, collect=True), "pytest --collect-only")
    assert c.status == "fail"
    [fix] = c.fix  # aimed at the interpreter checked, run in the checkout
    assert Path(sys.executable).as_posix() in fix and "--collect-only" in fix


def test_cli_doctor_dev_reports_a_foreign_venv(repo, tmp_path, monkeypatch):
    monkeypatch.setattr("evi.config.HOME", tmp_path / "home")
    monkeypatch.setattr("evi.config.CONFIG_PATH", tmp_path / "home" / "config.toml")
    venv = _foreign_venv(repo / ".venv", tmp_path)
    runner = CliRunner()
    r = runner.invoke(cli_main.app, ["doctor", "--dev", "--repo", str(repo), "--no-collect"])
    assert r.exit_code == 0, r.output
    assert "foreign" in r.output
    r = runner.invoke(cli_main.app, ["doctor", "--dev", "--repo", str(repo),
                                     "--venv", str(venv), "--no-collect", "--strict"])
    assert r.exit_code == 1
    assert "dev-bootstrap" in r.output


def test_cli_prints_the_eol_fix_command_unwrapped(repo, tmp_path, monkeypatch):
    monkeypatch.setattr("evi.config.HOME", tmp_path / "home")
    monkeypatch.setattr("evi.config.CONFIG_PATH", tmp_path / "home" / "config.toml")
    names = [f"long_module_name_{i:02d}.py" for i in range(8)]
    for n in names:
        (repo / n).write_bytes(b"v = 1\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "more")
    for n in names:
        (repo / n).write_bytes(b"v = 1\r\n")
    _git(repo, "add", "-A")
    r = CliRunner().invoke(cli_main.app, ["doctor", "--dev", "--repo", str(repo), "--no-collect"],
                           env={"COLUMNS": "60"})
    assert r.exit_code == 0, r.output
    lines = [ln.strip() for ln in r.output.splitlines()]
    expected = ("git --literal-pathspecs rm --cached -q -- " + " ".join(names)
                + "; git --literal-pathspecs checkout HEAD -- " + " ".join(names))
    assert expected in lines  # one line, copy-pasteable, despite the narrow console


# --- review round 1 ---------------------------------------------------------


def _run_fix(repo: Path, fix: str) -> None:
    """Run a printed `a; b` fix the way a shell would, without a shell."""
    for part in fix.split("; "):
        subprocess.run(shlex.split(part), cwd=repo, check=True)


def test_eol_fix_uses_literal_pathspecs_so_a_glob_name_spares_edited_files(repo):
    # `[ab].md` is a glob pathspec to git: it also matches a.md and b.md.
    (repo / "a.md").write_bytes(b"a\n")
    (repo / "[ab].md").write_bytes(b"glob\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "glob names")
    (repo / "a.md").write_bytes(b"a, edited and not committed\n")  # LF, a real edit
    (repo / "[ab].md").write_bytes(b"glob\r\n")                     # EOL only
    _git(repo, "--literal-pathspecs", "add", "[ab].md")

    [c] = doctor.check_eol(repo)
    [fix] = c.fix
    assert "'[ab].md'" in fix and "--literal-pathspecs" in fix
    _run_fix(repo, fix)
    assert (repo / "a.md").read_bytes() == b"a, edited and not committed\n"
    assert (repo / "[ab].md").read_bytes() == b"glob\n"


def test_eol_fix_never_puts_a_name_with_a_quote_in_the_command(repo):
    # Double quotes would let bash run $(...), and no quoting carries `'` in
    # both bash and PowerShell, so such a name is listed for a fix by hand.
    evil = "it's $(touch PWNED).md"
    names = (evil, "a,b.md", "@scope.md")
    for name in names:
        (repo / name).write_bytes(b"x\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "odd names")
    for name in names:
        (repo / name).write_bytes(b"x\r\n")
    _git(repo, "add", "-A")

    [c] = doctor.check_eol(repo)
    [fix] = c.fix
    assert evil not in fix and '"' not in fix
    assert evil in c.detail and "by hand" in c.detail
    # `,` builds an array and a leading `@` splats in PowerShell: quoted.
    assert "'a,b.md'" in fix and "'@scope.md'" in fix
    _run_fix(repo, fix)
    assert (repo / "a,b.md").read_bytes() == b"x\n"
    assert not (repo / "PWNED").exists()


def test_doctor_runs_no_code_from_a_checkout_it_does_not_come_from(repo, tmp_path,
                                                                   monkeypatch):
    # conftest.py runs on collection; a json.py in the current directory would
    # shadow the stdlib for a non-isolated `python -c`; core.fsmonitor in
    # .git/config runs on a plain `git ls-files`.
    markers = tmp_path / "markers"
    markers.mkdir()
    (repo / "conftest.py").write_text(
        f"open({str(markers / 'conftest')!r}, 'w').close()\n", encoding="utf-8")
    (repo / "json.py").write_text(
        f"open({str(markers / 'json')!r}, 'w').close()\n"
        "raise SystemExit('json.py from the checkout was imported')\n", encoding="utf-8")
    (repo / "test_x.py").write_text("def test_x():\n    pass\n", encoding="utf-8")
    hook = (markers / "fsmonitor").as_posix()
    _git(repo, "config", "core.fsmonitor", f"echo ran > '{hook}'; false")
    # A clean filter runs whenever git compares a stat-dirty working file with
    # the index (`git diff`), which is what the EOL check must decide.
    (repo / ".git" / "info").mkdir(exist_ok=True)
    (repo / ".git" / "info" / "attributes").write_bytes(b"*.py filter=evil\n")
    clean = (markers / "clean-filter").as_posix()
    _git(repo, "config", "filter.evil.clean", f"echo ran > '{clean}'; cat")
    (repo / "a.py").write_bytes(b"x = 1\r\ny = 3\r\n")  # CRLF, and a real edit
    monkeypatch.chdir(repo)

    checks = doctor.run_dev_checks(repo)  # default: collect only the eVi checkout
    [c] = _by_name(checks, "pytest --collect-only")
    assert c.status == "warn" and "skipped" in c.detail and "--collect" in c.detail
    assert _by_name(checks, "dev tools")[0].status == "ok"   # the probe started
    [eol] = _by_name(checks, "line endings")
    assert "local edits (a.py)" in eol.detail  # told apart without running the filter
    assert sorted(p.name for p in markers.iterdir()) == []

    # Asking for it is the explicit opt-in.
    (repo / "json.py").unlink()
    [c] = _by_name(doctor.run_dev_checks(repo, collect=True), "pytest --collect-only")
    assert c.status == "ok", c.detail
    assert (markers / "conftest").exists()


def test_the_evi_checkout_and_its_worktrees_are_collected(tmp_path, monkeypatch):
    main = tmp_path / "evi-main"
    main.mkdir()
    _git(main, "init", "-q")
    (main / "README.md").write_bytes(b"x\n")
    _git(main, "add", "-A")
    _git(main, "commit", "-qm", "init")
    worktree = main / ".worktrees" / "fix__x"
    _git(main, "worktree", "add", "-q", "-b", "fix/x", str(worktree))
    other = tmp_path / "other"
    other.mkdir()
    _git(other, "init", "-q")
    monkeypatch.setattr(doctor, "_evi_root", lambda: main)

    assert doctor.is_evi_checkout(main)
    assert doctor.is_evi_checkout(worktree)
    assert not doctor.is_evi_checkout(other)


def test_the_real_checkout_is_the_evi_checkout():
    assert doctor.is_evi_checkout(Path(__file__).resolve().parents[1])


def test_probe_ignores_modules_in_the_current_directory(tmp_path, monkeypatch):
    (tmp_path / "json.py").write_text("raise SystemExit('shadowed')\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PYTHONPATH", str(tmp_path))
    info = doctor._probe_python(sys.executable)
    assert info is not None and info["mods"]["pytest"]


@pytest.mark.parametrize(("command", "created"), [
    ('C:\\py\\python.exe -m venv --without-pip --prompt="my env" C:\\venvs\\x',
     "C:\\venvs\\x"),
    ("/usr/bin/python3 -m venv --upgrade-deps /home/a b/venv", "/home/a b/venv"),
    ("/usr/bin/python3 -m venv /srv/v", "/srv/v"),
])
def test_venv_created_at_survives_prompts_and_spaces(command, created):
    assert doctor._venv_created_at({"command": command}) == created


def test_venv_with_a_spaced_prompt_is_not_reported_as_moved(tmp_path):
    venv = tmp_path / "v1"
    _fake_venv(venv, home=Path(sys.base_prefix),
               command=f'python -m venv --without-pip --prompt="my env" {venv}')
    c = doctor.inspect_venv(venv)
    assert c.status == "ok", c.detail


def test_venv_fixes_name_the_venv_and_recreate_when_the_bootstrap_needs_it(repo, tmp_path):
    recreate = "-Recreate" if os.name == "nt" else "--recreate"
    venv_opt = "-Venv" if os.name == "nt" else "--venv"
    # Too old, interpreter present: the bootstrap replaces it only with --recreate.
    old = _fake_venv(tmp_path / "venvs" / "old", home=Path(sys.base_prefix), version="3.11.9")
    [fix] = doctor.inspect_venv(old).fix
    assert f"{venv_opt} {doctor._shell_arg(str(old))} {recreate}" in fix
    # Plainly foreign and outside any checkout: the bootstrap replaces it unasked.
    foreign = _foreign_venv(tmp_path / "venvs" / "foreign", tmp_path)
    [fix] = doctor.inspect_venv(foreign).fix
    assert fix.endswith(f"{venv_opt} {doctor._shell_arg(str(foreign))}")
    # Foreign, but inside a checkout: --recreate again.
    inside = _foreign_venv(repo / ".venv", tmp_path)
    [fix] = doctor.inspect_venv(inside).fix
    assert fix.endswith(recreate)
    # Moved: its python still starts, so no bootstrap command would fix it.
    moved = _fake_venv(tmp_path / "venvs" / "moved", home=Path(sys.base_prefix),
                       command=f"python -m venv {tmp_path / 'elsewhere'}")
    assert doctor.inspect_venv(moved).fix == ()


@pytest.mark.parametrize(("has_pip", "uv"), [(True, None), (False, "uv"), (False, None)])
def test_install_hints_target_the_checked_interpreter(repo, monkeypatch, has_pip, uv):
    mods = {"pytest": False, "pytest_timeout": False, "ruff": False, "xdist": False,
            "playwright": False, "pip": has_pip}
    monkeypatch.setattr(doctor, "_probe_python",
                        lambda python: {"version": [3, 13, 0], "mods": mods})
    monkeypatch.setattr(doctor.shutil, "which", lambda name: uv if name == "uv" else None)
    venv = repo / "venv"
    python = str(doctor.venv_python(venv))
    checks = doctor._check_dev_tools(python, repo, collect=False, venv=venv)
    [fix] = _by_name(checks, "dev tools")[0].fix
    spec = f"'{repo.as_posix()}[dev]'"
    py = doctor._shell_exe(python)
    if has_pip:
        assert fix == f"{py} -m pip install -e {spec}"
    elif uv:
        assert fix.startswith("uv pip install --python ") and fix.endswith(f"-e {spec}")
    else:
        assert "dev-bootstrap" in fix and ("--reinstall" in fix or "-Reinstall" in fix)
    for c in checks:
        for f in c.fix:
            assert not f.startswith("python ")  # never a bare `python` from PATH


def test_cli_collects_a_foreign_checkout_only_when_asked(repo, tmp_path, monkeypatch):
    monkeypatch.setattr("evi.config.HOME", tmp_path / "home")
    monkeypatch.setattr("evi.config.CONFIG_PATH", tmp_path / "home" / "config.toml")
    marker = tmp_path / "conftest-ran"
    (repo / "conftest.py").write_text(f"open({str(marker)!r}, 'w').close()\n",
                                      encoding="utf-8")
    runner = CliRunner()
    r = runner.invoke(cli_main.app, ["doctor", "--dev", "--repo", str(repo)])
    assert r.exit_code == 0, r.output
    assert "skipped" in r.output and not marker.exists()
    r = runner.invoke(cli_main.app, ["doctor", "--dev", "--repo", str(repo), "--collect"])
    assert r.exit_code == 0, r.output
    assert marker.exists()
