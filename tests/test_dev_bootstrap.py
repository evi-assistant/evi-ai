"""scripts/dev-bootstrap.{sh,ps1}: which venvs they replace, in --dry-run.

A dry run prints every step and changes nothing, so these run the real
scripts against fixture venvs under tmp_path. The rule under test: a venv is
replaced unasked only when it is plainly foreign (the interpreter pyvenv.cfg
names does not exist on this machine) and lives outside any git checkout, and
even then it is moved aside, not deleted. A venv whose interpreter exists but
which fails the probe, or any venv inside a checkout, needs --recreate /
-Recreate.

The dry-run flag always comes right after the script path, so an option that
swallowed the next word could never turn a test into a real run.

`--test` / `-Test` runs pytest in the checkout (a worktree too) once the
doctor check passes: the real runs below put stand-ins for `python -m evi`
and `python -m pytest` on PYTHONPATH, so they record where and how they were
called and never run the real suite.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from tests.test_self_build_commands import _git_bash

ROOT = Path(__file__).resolve().parents[1]
BASH = _git_bash()
POWERSHELL = shutil.which("powershell") if os.name == "nt" else None
SH_SCRIPT = str(ROOT / "scripts" / "dev-bootstrap.sh")
PS_SCRIPT = str(ROOT / "scripts" / "dev-bootstrap.ps1")


def _venv(path: Path, home: Path) -> Path:
    """A venv whose python is an empty file: it never starts."""
    path.mkdir(parents=True)
    (path / "pyvenv.cfg").write_text(f"home = {home}\nversion = 3.13.1\n", encoding="utf-8")
    for exe in (path / "Scripts" / "python.exe", path / "bin" / "python"):
        exe.parent.mkdir()
        exe.write_bytes(b"")
    return path


def _repo(tmp_path: Path, *, commit: bool = False) -> Path:
    r = tmp_path / "checkout"
    r.mkdir()
    subprocess.run(["git", "init", "-q", str(r)], check=True)
    if commit:
        subprocess.run(["git", "-C", str(r), "-c", "user.name=t", "-c", "user.email=t@t",
                        "commit", "-q", "--allow-empty", "-m", "init"], check=True)
    return r


def _env() -> dict[str, str]:
    e = {k: v for k, v in os.environ.items() if k != "EVI_DEV_VENV"}
    # a Python 3.12+ the scripts can find for the (printed) venv creation
    e["PATH"] = os.pathsep.join([str(Path(sys.executable).parent), e.get("PATH", "")])
    return e


def _sh(*args: str, dry_run: bool = True, env: dict[str, str] | None = None,
        timeout: float = 120) -> subprocess.CompletedProcess:
    if BASH is None:
        pytest.skip("no bash (Git Bash on Windows)")
    flags = ["--dry-run"] if dry_run else []
    return subprocess.run([BASH, SH_SCRIPT, *flags, *args],
                          capture_output=True, text=True, timeout=timeout, env=env or _env())


def _ps(*args: str, dry_run: bool = True, env: dict[str, str] | None = None,
        timeout: float = 120) -> subprocess.CompletedProcess:
    if POWERSHELL is None:
        pytest.skip("Windows PowerShell only")
    flags = ["-DryRun"] if dry_run else []
    return subprocess.run([POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                           PS_SCRIPT, *flags, *args],
                          capture_output=True, text=True, timeout=timeout, env=env or _env())


def _sh_path(p: Path | str) -> str:
    return Path(p).as_posix()


def _short_path(p: Path) -> str | None:
    """The 8.3 spelling of `p` on Windows, when it differs from the long one."""
    if os.name != "nt":
        return None
    import ctypes

    buf = ctypes.create_unicode_buffer(32768)
    n = ctypes.windll.kernel32.GetShortPathNameW(str(p), buf, len(buf))
    short = buf.value if 0 < n < len(buf) else ""
    return short if short and short.lower() != str(p).lower() else None


@pytest.fixture(scope="module")
def healthy_venv(tmp_path_factory) -> Path:
    path = tmp_path_factory.mktemp("healthy") / "venv"
    subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(path)],
                   check=True, capture_output=True, timeout=120)
    return path


@pytest.mark.timeout(240)
@pytest.mark.parametrize("shell", ["sh", "ps1"])
def test_healthy_venv_is_reused(shell, healthy_venv):
    # Guards the interpreter probe itself: Windows PowerShell 5.1 strips double
    # quotes from native arguments, which once made every venv look broken.
    r = _sh("--venv", _sh_path(healthy_venv)) if shell == "sh" else _ps("-Venv", str(healthy_venv))
    out = r.stdout + r.stderr
    assert r.returncode == 0, out
    assert "using venv" in out
    for step in ("rm -rf", "Remove-Item", "+ mv ", "Move-Item", "-m venv"):
        assert step not in out, step


@pytest.mark.timeout(240)
@pytest.mark.parametrize("shell", ["sh", "ps1"])
def test_foreign_venv_outside_the_checkout_is_moved_aside_and_recreated(shell, tmp_path):
    venv = _venv(tmp_path / "venvs" / "evi-313", home=tmp_path / "other-pc" / "Python313")
    if shell == "sh":
        r = _sh("--venv", _sh_path(venv))
        move = "+ mv -- "
    else:
        r = _ps("-Venv", str(venv))
        move = "+ Move-Item "
    out = r.stdout + r.stderr
    assert r.returncode == 0, out
    assert "is foreign" in out and "other-pc" in out
    assert move in out and "evi-313.foreign-" in out
    assert "rm -rf" not in out and "Remove-Item" not in out  # never deleted unasked
    assert "-m venv" in out or "uv venv" in out
    assert "pytest-xdist" in out  # the install step follows
    assert "dry run: nothing was changed" in out
    assert (venv / "pyvenv.cfg").is_file()  # a dry run moves nothing


@pytest.mark.timeout(240)
@pytest.mark.parametrize("shell", ["sh", "ps1"])
def test_venv_with_an_existing_interpreter_is_never_replaced_unasked(shell, tmp_path):
    # The interpreter pyvenv.cfg names exists, yet the probe fails: maybe a
    # transient error, maybe a bug in the probe. Deleting someone's working
    # venv on a guess is the one thing the bootstrap must not do.
    venv = _venv(tmp_path / "venvs" / "evi-313", home=Path(sys.base_prefix))
    r = _sh("--venv", _sh_path(venv)) if shell == "sh" else _ps("-Venv", str(venv))
    out = r.stdout + r.stderr
    assert r.returncode == 2, out
    assert "recreate" in out.lower()
    assert "rm -rf" not in out and "Remove-Item" not in out and "Move-Item" not in out


@pytest.mark.timeout(240)
@pytest.mark.parametrize("shell", ["sh", "ps1"])
def test_foreign_venv_inside_the_checkout_needs_recreate(shell, tmp_path):
    repo = _repo(tmp_path)
    venv = _venv(repo / ".venv", home=tmp_path / "other-pc" / "Python313")
    if shell == "sh":
        r = _sh("--repo", _sh_path(repo), "--venv", _sh_path(venv))
        forced = _sh("--repo", _sh_path(repo), "--venv", _sh_path(venv), "--recreate")
        removal = "+ rm -rf -- "
    else:
        r = _ps("-Repo", str(repo), "-Venv", str(venv))
        forced = _ps("-Repo", str(repo), "-Venv", str(venv), "-Recreate")
        removal = "+ Remove-Item -Recurse -Force "
    out = r.stdout + r.stderr
    assert r.returncode == 2, out
    assert "inside the git checkout" in out and "another machine" in out
    assert forced.returncode == 0, forced.stdout + forced.stderr
    assert removal in forced.stdout  # --recreate is the explicit consent to delete
    assert "dry run: nothing was changed" in forced.stdout


@pytest.mark.timeout(240)
@pytest.mark.parametrize("shell", ["sh", "ps1"])
def test_inside_the_checkout_guard_survives_another_spelling(shell, tmp_path):
    # The guard once compared path strings, so an upper-cased --venv (bash) or
    # an 8.3 -Repo (PowerShell) spelled its way past it into an rm -rf.
    repo = _repo(tmp_path)
    venv = _venv(repo / ".venv", home=tmp_path / "other-pc" / "Python313")
    if shell == "sh":
        if os.name == "nt":
            spelled = _sh_path(str(venv).upper())
        else:
            link = tmp_path / "link"
            try:
                link.symlink_to(repo, target_is_directory=True)
            except OSError:
                pytest.skip("cannot create a symlink here")
            spelled = _sh_path(link / ".venv")
        r = _sh("--repo", _sh_path(repo), "--venv", spelled)
    else:
        short = _short_path(repo)
        if short is None:
            pytest.skip("no 8.3 spelling for the temp directory")
        r = _ps("-Repo", short, "-Venv", str(venv))
    out = r.stdout + r.stderr
    assert r.returncode == 2, out
    assert "inside the git checkout" in out
    assert "rm -rf" not in out and "Remove-Item" not in out and "Move-Item" not in out


@pytest.mark.timeout(240)
@pytest.mark.parametrize("shell", ["sh", "ps1"])
def test_a_directory_that_is_not_a_venv_is_left_alone(shell, tmp_path):
    d = tmp_path / "not-a-venv"
    (d / "data").mkdir(parents=True)
    r = _sh("--venv", _sh_path(d)) if shell == "sh" else _ps("-Venv", str(d))
    assert r.returncode == 2
    assert "not a venv" in r.stdout + r.stderr


@pytest.mark.timeout(240)
def test_an_option_value_cannot_be_the_next_option(tmp_path):
    # `--extras --recreate` once took "--recreate" as the extras list; with
    # the dry-run flag last, the same slip ran the bootstrap for real.
    venv = tmp_path / "venvs" / "new"
    for args in (["--extras", "--recreate"], ["--venv", "--extras", "dev"],
                 ["--worktree", "--base", "HEAD"], ["--python"]):
        r = _sh("--venv", _sh_path(venv), *args)
        out = r.stdout + r.stderr
        assert r.returncode == 2, (args, out)
        assert "needs a value" in out, (args, out)
        assert "dry run: nothing was changed" not in out


@pytest.mark.timeout(240)
@pytest.mark.parametrize("shell", ["sh", "ps1"])
def test_worktree_dry_run_and_branch_validation(shell, tmp_path):
    repo = _repo(tmp_path, commit=True)
    venv = tmp_path / "venvs" / "new"
    if shell == "sh":
        r = _sh("--repo", _sh_path(repo), "--venv", _sh_path(venv), "--worktree", "fix/x")
        bad = _sh("--repo", _sh_path(repo), "--venv", _sh_path(venv), "--worktree", "bad..name")
    else:
        r = _ps("-Repo", str(repo), "-Venv", str(venv), "-Worktree", "fix/x")
        bad = _ps("-Repo", str(repo), "-Venv", str(venv), "-Worktree", "bad..name")
    out = r.stdout + r.stderr
    assert r.returncode == 0, out
    assert "append .worktrees/ to" in out
    assert "worktree add -b fix/x" in out and "fix__x" in out
    assert not (repo / ".worktrees").exists()  # dry run
    assert bad.returncode == 2 and "not a valid branch name" in bad.stdout + bad.stderr


@pytest.mark.timeout(240)
@pytest.mark.parametrize("shell", ["sh", "ps1"])
def test_worktree_is_reused_only_for_its_own_branch(shell, tmp_path):
    # `fix/x` and `fix__x` map to the same directory; reusing it for the
    # other name would put the implementer on the wrong branch.
    repo = _repo(tmp_path, commit=True)
    subprocess.run(["git", "-C", str(repo), "worktree", "add", "-q", "-b", "fix/x",
                    str(repo / ".worktrees" / "fix__x")], check=True)
    venv = tmp_path / "venvs" / "new"
    if shell == "sh":
        same = _sh("--repo", _sh_path(repo), "--venv", _sh_path(venv), "--worktree", "fix/x")
        other = _sh("--repo", _sh_path(repo), "--venv", _sh_path(venv), "--worktree", "fix__x")
    else:
        same = _ps("-Repo", str(repo), "-Venv", str(venv), "-Worktree", "fix/x")
        other = _ps("-Repo", str(repo), "-Venv", str(venv), "-Worktree", "fix__x")
    assert same.returncode == 0, same.stdout + same.stderr
    assert "reusing worktree" in same.stdout
    out = other.stdout + other.stderr
    assert other.returncode == 2, out
    assert "branch fix/x, not fix__x" in out


# ---- --test / -Test: pytest in the checkout, after the check ---------------------


def _stubs(root: Path) -> Path:
    """Stand-ins for `python -m evi` (the doctor check) and `python -m pytest`,
    to put on PYTHONPATH: each appends {who, cwd, argv} to $STUB_LOG and exits
    with $STUB_DOCTOR_RC / $STUB_PYTEST_RC (default 0)."""
    stubs = root / "stubs"
    for pkg, rc in (("evi", "STUB_DOCTOR_RC"), ("pytest", "STUB_PYTEST_RC")):
        (stubs / pkg).mkdir(parents=True)
        (stubs / pkg / "__init__.py").write_text("", encoding="utf-8")
        (stubs / pkg / "__main__.py").write_text(
            "import json, os, sys\n"
            "call = {'who': %r, 'cwd': os.getcwd(), 'argv': sys.argv[1:]}\n"
            "with open(os.environ['STUB_LOG'], 'a', encoding='utf-8') as f:\n"
            "    print(json.dumps(call), file=f)\n"
            "sys.exit(int(os.environ.get(%r, '0')))\n" % (pkg, rc),
            encoding="utf-8",
        )
    return stubs


def _calls(log: Path) -> list[dict]:
    if not log.exists():
        return []
    calls = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    log.unlink()
    return calls


def _same_dir(a: str | Path, b: str | Path) -> bool:
    return os.path.normcase(os.path.realpath(a)) == os.path.normcase(os.path.realpath(b))


def _git_status(repo: Path) -> str:
    return subprocess.run(["git", "-C", str(repo), "status", "--porcelain"],
                          capture_output=True, text=True, check=True).stdout


@pytest.mark.timeout(240)
@pytest.mark.parametrize("shell", ["sh", "ps1"])
def test_test_dry_run_prints_pytest_in_the_worktree_after_the_check(shell, tmp_path):
    repo = _repo(tmp_path, commit=True)
    venv = tmp_path / "venvs" / "new"
    pytest_args = ("tests/test_a.py", "-k", "foo", "-n", "2")
    if shell == "sh":
        base = ("--repo", _sh_path(repo), "--venv", _sh_path(venv), "--worktree", "fix/x")
        r = _sh(*base, "--test", *pytest_args)
        plain = _sh(*base)
    else:
        base = ("-Repo", str(repo), "-Venv", str(venv), "-Worktree", "fix/x")
        r = _ps(*base, "-Test", *pytest_args)
        plain = _ps(*base)
    out = r.stdout + r.stderr
    assert r.returncode == 0, out
    lines = r.stdout.splitlines()
    [doctor] = [i for i, ln in enumerate(lines) if "-m evi doctor --dev" in ln]
    [test] = [i for i, ln in enumerate(lines) if "-m pytest" in ln]
    assert doctor < test  # the check first
    line = lines[test]
    assert "fix__x" in line, line  # in the worktree
    assert line.endswith("-m pytest -q -p no:cacheprovider tests/test_a.py -k foo -n 2)"), line
    assert "dry run: nothing was changed" in out
    assert not (repo / ".worktrees").exists()
    assert plain.returncode == 0 and "-m pytest" not in plain.stdout  # only with --test


@pytest.mark.timeout(240)
@pytest.mark.parametrize("shell", ["sh", "ps1"])
def test_test_runs_pytest_in_the_worktree_once_the_check_passes(shell, tmp_path, healthy_venv):
    # Acceptance (loop-worktree-isolation): pytest runs inside a worktree
    # through the bootstrap helper, and its exit code is the script's.
    repo = _repo(tmp_path, commit=True)
    log = tmp_path / "stub.log"
    env = {**_env(), "PYTHONPATH": str(_stubs(tmp_path)), "STUB_LOG": str(log)}
    tree = repo / ".worktrees" / "fix-1"

    def boot(*extra: str, doctor: int = 0, tests: int = 0) -> subprocess.CompletedProcess:
        e = {**env, "STUB_DOCTOR_RC": str(doctor), "STUB_PYTEST_RC": str(tests)}
        if shell == "sh":
            return _sh("--repo", _sh_path(repo), "--venv", _sh_path(healthy_venv), *extra,
                       dry_run=False, env=e)
        extra = tuple({"--worktree": "-Worktree", "--test": "-Test"}.get(a, a) for a in extra)
        return _ps("-Repo", str(repo), "-Venv", str(healthy_venv), *extra, dry_run=False, env=e)

    r = boot("--worktree", "fix-1", "--test", "tests/test_a.py", "-k", "a and b")
    out = r.stdout + r.stderr
    assert r.returncode == 0, out
    doctor, run = _calls(log)
    assert doctor["who"] == "evi" and doctor["argv"][:2] == ["doctor", "--dev"]
    assert "--strict" in doctor["argv"]
    assert run["who"] == "pytest"
    assert run["argv"] == ["-q", "-p", "no:cacheprovider", "tests/test_a.py", "-k", "a and b"]
    assert _same_dir(run["cwd"], tree) and _same_dir(doctor["cwd"], tree), (run, doctor)
    assert "ready:" in out and "tests passed in" in out
    assert _git_status(repo) == ""  # the main checkout never lists the worktree

    # A failing suite fails the bootstrap with pytest's own exit code.
    r = boot("--worktree", "fix-1", "--test", "-x", tests=1)
    out = r.stdout + r.stderr
    assert r.returncode == 1, out
    assert "reusing worktree" in out and "tests failed in" in out and "pytest exit 1" in out
    assert [c["argv"] for c in _calls(log) if c["who"] == "pytest"] == [
        ["-q", "-p", "no:cacheprovider", "-x"]]

    # A failed check runs no tests and keeps its exit code.
    r = boot("--test", doctor=3)
    out = r.stdout + r.stderr
    assert r.returncode == 3, out
    assert "tests were not run" in out
    assert [c["who"] for c in _calls(log)] == ["evi"]

    # Without --worktree the tests run in the checkout itself.
    r = boot("--test")
    assert r.returncode == 0, r.stdout + r.stderr
    _, run = _calls(log)
    assert _same_dir(run["cwd"], repo) and run["argv"] == ["-q", "-p", "no:cacheprovider"]


@pytest.mark.timeout(240)
def test_powershell_refuses_arguments_no_option_takes_without_test(tmp_path):
    # pytest's arguments are whatever no option takes, so without -Test such
    # an argument is a typo: refused, never run or ignored.
    venv = tmp_path / "venvs" / "new"
    r = _ps("-Venv", str(venv), "tests/test_a.py")
    out = r.stdout + r.stderr
    assert r.returncode == 2, out
    assert "unknown argument(s): tests/test_a.py" in out
    assert "dry run: nothing was changed" not in out


def _path_without_python() -> str | None:
    """A PATH with git (and, through Git Bash, its POSIX tools) but no Python
    or uv, or None when this machine has no such PATH to offer."""
    git = shutil.which("git")
    if not git:
        return None
    dirs = [str(Path(git).parent)]
    if os.name == "nt":
        # Git for Windows: git.exe sits in <Git>/cmd or <Git>/mingw64/bin, and
        # the POSIX tools the script uses (sed, dirname, mv) in <Git>/usr/bin.
        root = next((d for d in Path(git).parents if (d / "usr" / "bin").is_dir()), None)
        if root is None:
            return None
        dirs += [str(root / "usr" / "bin"), str(root / "mingw64" / "bin"),
                 os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32")]
    path = os.pathsep.join(dirs)
    for exe in ("python", "python3", "py", "uv", "python0.0"):
        if shutil.which(exe, path=path):
            return None
    return path


@pytest.mark.timeout(240)
@pytest.mark.parametrize("shell", ["sh", "ps1"])
def test_a_foreign_venv_is_moved_aside_never_deleted(shell, tmp_path):
    # A real run, not a dry run. With no Python on PATH the script stops right
    # after setting the old venv aside, which is the step under test.
    path = _path_without_python()
    if path is None:
        pytest.skip("every PATH with git here also has a Python")
    env = {**_env(), "PATH": path}
    repo = _repo(tmp_path)
    venv = _venv(tmp_path / "venvs" / "evi-313", home=tmp_path / "other-pc" / "Python313")
    if shell == "sh":
        r = _sh("--repo", _sh_path(repo), "--venv", _sh_path(venv), "--python", "0.0",
                dry_run=False, env=env)
    else:
        r = _ps("-Repo", str(repo), "-Venv", str(venv), "-Python", "0.0",
                dry_run=False, env=env)
    out = r.stdout + r.stderr
    assert r.returncode == 2 and "no Python 3.12+ found" in out, out
    assert not venv.exists()
    [aside] = venv.parent.glob("evi-313.foreign-*")
    assert (aside / "pyvenv.cfg").read_text(encoding="utf-8").startswith("home = ")
    assert "moved it aside to" in out


@pytest.mark.timeout(240)
def test_a_linked_venv_is_never_replaced_by_powershell(tmp_path):
    # Windows PowerShell 5.1's Remove-Item -Recurse follows a junction into
    # its target, so even -Recreate must not touch a linked venv.
    if POWERSHELL is None:
        pytest.skip("Windows PowerShell only")
    target = _venv(tmp_path / "real" / "venv", home=tmp_path / "other-pc" / "Python313")
    link = tmp_path / "linked-venv"
    made = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)],
                          capture_output=True, text=True)
    if made.returncode != 0:
        pytest.skip(f"cannot create a junction: {made.stdout}{made.stderr}")
    for extra in ([], ["-Recreate"]):
        r = _ps("-Venv", str(link), *extra)
        out = r.stdout + r.stderr
        assert r.returncode == 2, out
        assert "is a link" in out
    assert (target / "pyvenv.cfg").is_file()


def _snapshot(dst: Path) -> Path:
    """This checkout as it is on disk (tracked and untracked, not ignored
    files) committed into a fresh repo at `dst`, so a real bootstrap never
    writes into the working checkout."""
    listed = subprocess.run(
        ["git", "-C", str(ROOT), "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        capture_output=True, check=True,
    ).stdout.decode("utf-8").split("\0")
    for rel in filter(None, listed):
        src = ROOT / rel
        if src.is_file():
            (dst / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst / rel)
    git = ["git", "-C", str(dst), "-c", "user.name=t", "-c", "user.email=t@t",
           "-c", "core.autocrlf=false"]
    subprocess.run([*git, "init", "-q"], check=True)
    subprocess.run([*git, "add", "-A"], check=True, capture_output=True)
    subprocess.run([*git, "commit", "-qm", "snapshot"], check=True)
    return dst


@pytest.mark.timeout(1800)
@pytest.mark.skipif(os.environ.get("EVI_TEST_REAL_BOOTSTRAP") != "1",
                    reason="installs packages: set EVI_TEST_REAL_BOOTSTRAP=1 (PIP_FIND_LINKS "
                           "and PIP_NO_INDEX are passed through for an offline wheelhouse)")
def test_real_bootstrap_builds_a_venv_where_collection_succeeds(tmp_path):
    # Acceptance: on a checkout carrying a foreign .venv, the bootstrap makes a
    # venv where `pytest --collect-only` succeeds (doctor --dev --strict, exit 0).
    repo = _snapshot(tmp_path / "checkout")
    _venv(repo / ".venv", home=tmp_path / "other-pc" / "Python313")
    venv = tmp_path / "venvs" / "dev"
    if BASH is not None:
        r = _sh("--repo", _sh_path(repo), "--venv", _sh_path(venv), dry_run=False,
                timeout=1500)
    else:
        r = _ps("-Repo", str(repo), "-Venv", str(venv), dry_run=False, timeout=1500)
    assert r.returncode == 0, r.stdout[-4000:] + r.stderr[-4000:]
    assert "ready:" in r.stdout
    assert "pytest --collect-only" in r.stdout and "tests collected" in r.stdout
    assert (repo / ".venv" / "pyvenv.cfg").is_file()  # the checkout's own venv is untouched
