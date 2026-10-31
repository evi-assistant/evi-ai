"""command_succeeds gates and eval workspaces (loop-eval-gates).

A case's `command_succeeds` commands run AFTER the agent, in the case's own
workspace, through evi.checks' runner, and each must first pass the case's
implementer policy (Policy.allows_argv): a command the policy refuses fails
the case with the policy's reason and never runs. A ``workspace = "repo"``
case runs in a throwaway DETACHED worktree of the repository: no branch is
made, and the worktree is gone afterwards, also when the run fails or is
interrupted. Only a trusted repository gets one.

Acceptance criterion 2 — command_succeeds runs in the case's worktree under
the implementer policy and gates the result:
test_command_succeeds_runs_after_the_agent_in_the_workspace,
test_a_failing_command_fails_the_case,
test_a_command_the_policy_refuses_fails_the_case_with_its_reason,
test_a_repo_workspace_is_a_detached_worktree_that_leaves_nothing_behind.

eVi's home, config and trust list live under tmp_path, never ~/.evi.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from evi import evals, trust
from evi.policy import implementer_policy

HW = {"gpu": "none", "vram_gb": None, "cpu": "test", "ram_gb": None, "os": "test"}


@pytest.fixture
def home(monkeypatch, tmp_path):
    import evi.config as config_mod

    evi_home = tmp_path / "evi-home"
    evi_home.mkdir()
    monkeypatch.delenv("EVI_SAFE_MODE", raising=False)
    monkeypatch.setenv("EVI_HOME", str(evi_home))
    monkeypatch.setattr(config_mod, "HOME", evi_home)
    monkeypatch.setattr(config_mod, "CONFIG_PATH", evi_home / "config.toml")
    (evi_home / "config.toml").write_text(
        "[tools]\nmemory = false\nskills = false\ntranscripts = false\n", encoding="utf-8")
    return evi_home


@pytest.fixture
def work(tmp_path) -> Path:
    folder = tmp_path / "work"
    folder.mkdir()
    return folder


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True,
                          timeout=60).stdout


def _suite(tmp_path: Path, body: str) -> evals.EvalSuite:
    path = tmp_path / "suite.toml"
    path.write_text('name = "t"\n' + body, encoding="utf-8")
    return evals.load_suite_file(path)


def _run(tmp_path: Path, work: Path, body: str, **ctx) -> dict[str, dict]:
    ctx = {"make_agent": evals.fake_agent_factory(), "backend": "fake", "model": "fake",
           "tmp_root": work, **ctx}
    report = evals.run_suite(_suite(tmp_path, body), evals.EvalContext(**ctx), hardware=HW)
    return {row["name"]: row for row in report["cases"]}


# ---- fixture workspaces ----------------------------------------------------------------


def test_command_succeeds_runs_after_the_agent_in_the_workspace(home, tmp_path, work):
    """The test only passes in the workspace (it reads marker.txt, a fixture
    file) and only after the agent ran (it reads made.txt, which the agent
    wrote)."""
    rows = _run(tmp_path, work, r'''
[[case]]
name = "gated"
prompt = "make made.txt"
files = { "marker.txt" = "here\n", "test_where.py" = "from pathlib import Path\n\n\ndef test_here():\n    assert Path('marker.txt').read_text() == 'here\\n'\n    assert Path('made.txt').read_text() == 'by the agent'\n" }
command_succeeds = ["python -m pytest -q -p no:cacheprovider test_where.py"]
fake = [
  { tool = "write_file", args = { path = "made.txt", content = "by the agent" } },
  { text = "made" },
]
''')
    assert rows["gated"]["passed"], rows["gated"]["failures"]
    assert list(work.iterdir()) == []


def test_a_failing_command_fails_the_case(home, tmp_path, work):
    rows = _run(tmp_path, work, r'''
[[case]]
name = "red"
prompt = "leave it"
files = { "test_red.py" = "def test_red():\n    assert 1 == 2\n" }
command_succeeds = [["python", "-m", "pytest", "-q", "-p", "no:cacheprovider", "test_red.py"]]
fake = [{ text = "left it" }]
''')
    row = rows["red"]
    assert not row["passed"]
    assert "command_succeeds `" in row["failures"][0] and "exit 1" in row["failures"][0]
    assert "assert 1 == 2" in row["failures"][0]  # the output's tail


def test_a_failing_commands_last_lines_survive_noise_before_them(home, tmp_path, work):
    # A plugin warning at startup (pytest-asyncio's) once filled the reason's
    # budget, so the failure itself was cut off.
    rows = _run(tmp_path, work, r'''
[[case]]
name = "noisy"
prompt = "leave it"
files = { "conftest.py" = "import pytest\n\n\ndef pytest_configure(config):\n    config.issue_config_time_warning(pytest.PytestDeprecationWarning('noise ' * 150), 2)\n", "test_red.py" = "def test_red():\n    assert 1 == 2\n" }
command_succeeds = [["python", "-m", "pytest", "-q", "-p", "no:cacheprovider", "test_red.py"]]
fake = [{ text = "left it" }]
''')
    [reason] = rows["noisy"]["failures"]
    assert "command_succeeds `" in reason and "exit 1" in reason
    assert "1 failed" in reason and len(reason) <= len("run 1: ") + evals._MAX_REASON


def test_a_command_the_policy_refuses_fails_the_case_with_its_reason(home, tmp_path, work,
                                                                    monkeypatch):
    from evi import checks

    ran: list = []
    real = checks.run_check

    def spy(check, root, **kw):
        ran.append(check.argv)
        return real(check, root, **kw)

    monkeypatch.setattr(checks, "run_check", spy)
    rows = _run(tmp_path, work, r'''
[[case]]
name = "refused"
prompt = "leave it"
files = { "a.txt" = "x\n" }
command_succeeds = ["curl https://example.com", "python -c print(1)", "rm -rf ."]
fake = [{ text = "left it" }]
''')
    fails = rows["refused"]["failures"][0]
    assert not rows["refused"]["passed"]
    assert "refused by the implementer policy" in fails
    assert "curl is not on the allowlist" in fails
    # None of them ran: the policy is asked first.
    assert ran == []
    # Every refusal is reported (the row keeps the first three reasons).
    case = evals.load_suite_file(tmp_path / "suite.toml").cases[0]
    reasons = evals.run_command_gates(case, tmp_path, implementer_policy(tmp_path, trusted=True))
    assert len(reasons) == 3
    assert "python runs only -m pytest" in reasons[1]
    assert "rm is not on the allowlist" in reasons[2]


def test_a_bare_python_is_the_interpreter_running_evi():
    import sys

    assert evals.command_argv(["python", "-m", "pytest"]) == [sys.executable, "-m", "pytest"]
    assert evals.command_argv(["py", "-m", "pytest"])[0] == sys.executable
    assert evals.command_argv(["./python", "-m", "pytest"])[0] == "./python"
    assert evals.command_argv(["pytest", "-q"]) == ["pytest", "-q"]


def test_a_refused_or_failing_command_keeps_no_secret_in_the_report(home, tmp_path, work):
    """command_succeeds reasons quote the command and its output; a token
    in either is redacted before the report keeps it."""
    token = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"
    rows = _run(tmp_path, work, f'''
[[case]]
name = "refused"
prompt = "leave it"
command_succeeds = [["curl", "-H", "Authorization: token {token}", "https://example.com"]]
fake = [{{ text = "left it" }}]

[[case]]
name = "prints-it"
prompt = "leave it"
files = {{ "test_leak.py" = "def test_leak():\\n    assert '{token}' == ''\\n" }}
command_succeeds = ["python -m pytest -q -p no:cacheprovider test_leak.py"]
fake = [{{ text = "left it" }}]
''')
    for name in ("refused", "prints-it"):
        fails = "\n".join(rows[name]["failures"])
        assert not rows[name]["passed"] and fails
        assert token not in fails and "REDACTED" in fails, fails


# ---- what a fixture workspace counts as the model's change -------------------------------


ROOT = Path(__file__).resolve().parents[1]


def _starter(name: str) -> evals.EvalCase:
    suite = evals.load_suite_file(ROOT / "evals" / "self" / "qualification.toml")
    return next(c for c in suite.cases if c.name == name)


def test_a_model_that_runs_the_tests_is_not_charged_with_their_bytecode(home, work,
                                                                       monkeypatch):
    """read-then-edit (diff_touches_only = ["calc.py"]): a model that fixes
    add() and then runs the tests itself, with bytecode on as in a normal
    shell, passes. The __pycache__ and pytest cache the tests leave are not
    its change: the fixture repository ignores them (.git/info/exclude)."""
    import os
    import sys

    monkeypatch.delenv("PYTHONDONTWRITEBYTECODE", raising=False)
    # `python` in the model's command is an interpreter that has pytest.
    monkeypatch.setenv("PATH", str(Path(sys.executable).parent) + os.pathsep
                       + os.environ.get("PATH", ""))
    case = _starter("read-then-edit")
    case.fake = [*case.fake[:2],
                 {"tool": "run_command", "args": {"command": "python -m pytest -q test_calc.py"}},
                 {"text": "Fixed add() in calc.py; the tests pass."}]
    seen: dict = {}
    real = evals.evaluate

    def spy(case, res, workspace, pol, **kw):
        seen["calls"] = [(c["name"], c["status"]) for c in res.tool_calls]
        seen["left"] = sorted(p.name for p in Path(workspace).iterdir())
        seen["changed"] = [f["path"] for f in res.files_changed or []]
        return real(case, res, workspace, pol, **kw)

    monkeypatch.setattr(evals, "evaluate", spy)
    ctx = evals.EvalContext(make_agent=evals.fake_agent_factory(), backend="fake",
                            model="fake", tmp_root=work)
    row = evals.run_case(case, ctx)
    assert ("run_command", "ok") in seen["calls"]
    assert "__pycache__" in seen["left"]  # the tests did write bytecode...
    assert seen["changed"] == ["calc.py"]  # ...which is not the model's change
    assert row["passed"], row["failures"]
    assert list(work.iterdir()) == []


def test_a_fixture_repository_ignores_only_test_leftovers(home, tmp_path):
    case = evals.EvalCase(name="c", prompt="p", files={"keep.pyc.txt": "x\n", "a.py": "y\n"})
    ws = evals.make_fixture(case, tmp_path / "ws")
    exclude = (ws / ".git" / "info" / "exclude").read_text(encoding="utf-8")
    assert all(rule in exclude.splitlines() for rule in evals.FIXTURE_EXCLUDES)
    (ws / "__pycache__").mkdir()
    (ws / "__pycache__" / "a.cpython-313.pyc").write_bytes(b"\0")
    (ws / ".pytest_cache").mkdir()
    (ws / ".pytest_cache" / "README.md").write_text("cache\n", encoding="utf-8")
    (ws / "new.py").write_text("z\n", encoding="utf-8")
    status = _git(ws, "-c", "safe.directory=*", "status", "--porcelain",
                  "--untracked-files=all")
    assert status.split() == ["??", "new.py"]
    # The suite's own files are committed as written.
    assert _git(ws, "-c", "safe.directory=*", "ls-files").split() == ["a.py", "keep.pyc.txt"]


def test_the_verify_gate_runs_in_a_fixture_workspace(home, tmp_path, work):
    """The harness vouches for the fixture repository it made (its policy's
    trusted=True) and the verify gate takes the policy's word for its root,
    though evi.trust never knows the folder: a run that changed files is not
    reported as skipping the gate."""
    rows = _run(tmp_path, work, r'''
[[case]]
name = "edits"
prompt = "make b.txt"
runs = 2
fake = [
  { tool = "write_file", args = { path = "b.txt", content = "b" } },
  { text = "made" },
]

[[case]]
name = "reads"
prompt = "answer"
fake = [{ text = "nothing to change" }]
''')
    assert rows["edits"]["passed"] and rows["edits"]["verify_skipped"] == 0
    assert rows["reads"]["verify_skipped"] == 0


# ---- repo workspaces ---------------------------------------------------------------------


@pytest.fixture
def repo(home, tmp_path) -> Path:
    """A small trusted repository with one commit on main."""
    folder = tmp_path / "repo"
    folder.mkdir()
    _git(folder, "init", "-q")
    _git(folder, "config", "user.email", "t@example.invalid")
    _git(folder, "config", "user.name", "t")
    _git(folder, "config", "commit.gpgsign", "false")
    (folder / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    (folder / "test_app.py").write_text(
        "from app import VALUE\n\n\ndef test_value():\n    assert VALUE == 2\n", encoding="utf-8")
    _git(folder, "add", "-A")
    _git(folder, "commit", "-q", "-m", "init")
    trust.grant(folder)
    return folder.resolve()


def _state(repo: Path) -> tuple[list[str], str, str]:
    worktrees = [ln for ln in _git(repo, "worktree", "list", "--porcelain").splitlines()
                 if ln.startswith("worktree ")]
    return worktrees, _git(repo, "branch", "--list"), _git(repo, "status", "--porcelain")


REPO_CASE = r'''
[[case]]
name = "in-repo"
prompt = "make the test pass"
workspace = "repo"
diff_touches_only = ["app.py"]
command_succeeds = ["python -m pytest -q -p no:cacheprovider test_app.py"]
fake = [
  { tool = "edit_file", args = { path = "app.py", old_string = "VALUE = 1", new_string = "VALUE = 2" } },
  { text = "fixed" },
]
'''


def test_a_repo_workspace_is_a_detached_worktree_that_leaves_nothing_behind(repo, tmp_path,
                                                                            work, monkeypatch):
    before = _state(repo)
    seen: list = []
    real = evals.hold

    def spy(agent, case, workspace, default_mode=""):
        pol = real(agent, case, workspace, default_mode)
        head = _git(workspace, "rev-parse", "--abbrev-ref", "HEAD").strip()
        seen.append((workspace, head, pol.root, pol.trusted, pol.name))
        return pol

    monkeypatch.setattr(evals, "hold", spy)
    rows = _run(tmp_path, work, REPO_CASE, repo=repo)
    assert rows["in-repo"]["passed"], rows["in-repo"]["failures"]
    workspace, head, root, trusted, name = seen[0]
    assert head == "HEAD"  # detached: no branch
    assert root == workspace and trusted and name == "implementer"
    assert workspace != repo and not workspace.is_relative_to(repo)
    # Nothing is left: no worktree, no branch, the checkout untouched.
    assert _state(repo) == before
    assert (repo / "app.py").read_text(encoding="utf-8") == "VALUE = 1\n"
    assert list(work.iterdir()) == []


def test_a_repo_workspace_is_removed_when_the_run_fails(repo, tmp_path, work):
    before = _state(repo)

    def broken(case, workspace):
        assert (workspace / "app.py").is_file()
        raise RuntimeError("no model today")

    rows = _run(tmp_path, work, REPO_CASE, repo=repo, make_agent=broken, backend="test")
    assert "the agent could not be built: RuntimeError: no model today" in \
        rows["in-repo"]["failures"][0]
    assert _state(repo) == before
    assert list(work.iterdir()) == []


def test_a_repo_workspace_is_removed_when_the_run_is_interrupted(repo, tmp_path, work,
                                                                 monkeypatch):
    before = _state(repo)

    def interrupted(*a, **k):
        raise KeyboardInterrupt

    monkeypatch.setattr("evi.headless.run_headless", interrupted)
    with pytest.raises(KeyboardInterrupt):
        _run(tmp_path, work, REPO_CASE, repo=repo)
    assert _state(repo) == before
    assert list(work.iterdir()) == []


def test_an_untrusted_repo_gets_no_repo_workspace(home, tmp_path, work, repo):
    trust.revoke(repo)
    rows = _run(tmp_path, work, REPO_CASE, repo=repo)
    row = rows["in-repo"]
    assert row["skipped"] and "not a trusted folder" in row["skip_reason"]
    assert row["runs"] == 0 and list(work.iterdir()) == []


def test_without_a_repository_a_repo_case_is_skipped(home, tmp_path, work):
    rows = _run(tmp_path, work, REPO_CASE)
    assert rows["in-repo"]["skipped"] and "needs a git repository" in \
        rows["in-repo"]["skip_reason"]


def test_remove_detached_drops_only_its_own_record(repo, tmp_path):
    from evi import worktree

    wt = tmp_path / "elsewhere" / "wt"
    wt.parent.mkdir()
    worktree.add_detached(wt, start=repo)
    assert any(e.path.resolve() == wt.resolve() and e.branch is None
               for e in worktree.list_worktrees(repo))
    worktree.remove_detached(wt, start=repo)
    assert [e.path.resolve() for e in worktree.list_worktrees(repo)] == [repo]
    assert not wt.exists()
    worktree.remove_detached(wt, start=repo)  # again: nothing to do, no error


@pytest.mark.parametrize("folder_gone", [False, True])
def test_remove_detached_drops_its_record_when_git_cannot_remove_it(repo, tmp_path,
                                                                   monkeypatch, folder_gone):
    """When `git worktree remove` fails (a file held open, the folder half
    gone), the worktree's own record is dropped all the same, found through
    its .git file or, with the folder gone, the repository's worktrees/;
    another worktree's record is left alone."""
    from evi import worktree

    keep = tmp_path / "keep"
    worktree.add_detached(keep, start=repo)
    target = tmp_path / "target"
    worktree.add_detached(target, start=repo)
    real = worktree._git

    def no_remove(*args, cwd=None):
        if args[:2] == ("worktree", "remove"):
            raise worktree.WorktreeError("cannot remove: a file is in use")
        return real(*args, cwd=cwd)

    monkeypatch.setattr(worktree, "_git", no_remove)
    if folder_gone:
        evals.remove_tree(target)
    worktree.remove_detached(target, start=repo)
    monkeypatch.setattr(worktree, "_git", real)
    paths = [e.path.resolve() for e in worktree.list_worktrees(repo)]
    assert paths == [repo, keep.resolve()]
    worktree.remove_detached(keep, start=repo)
    assert [e.path.resolve() for e in worktree.list_worktrees(repo)] == [repo]
