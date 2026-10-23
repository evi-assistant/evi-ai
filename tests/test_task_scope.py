"""Task runs stay inside files_in_scope; the implementer sees the director's
hints (Phase 1 polish, polish-task-scope).

files_in_scope used to grant protected files only: a task run could write
any other file in its worktree, and scope was checked only by eye in review.
Now `evi task start` (evi.tasks.run_task) runs under a *confined*
implementer policy (evi/policy.py, **Scope**), with a git backstop for what
a command writes anyway. Acceptance criteria, each with its test:

1. A write tool call (write_file, edit_file, apply_patch, each file an
   apply_patch names) to a path no files_in_scope entry matches is denied,
   with a reason naming the scope and telling the model to list the file as
   a concern in its hand-off —
   test_confined_writes_stay_inside_files_in_scope,
   test_every_file_an_apply_patch_names_must_be_in_scope,
   test_run_task_denies_an_out_of_scope_write_and_tells_the_model_why
   (Agent turn on the shared fake LLM).
2. A command that rewrites the files it names (ruff format, ruff check
   --fix and the like) is held to the same rule, and a folder argument
   (`ruff format .`) is denied: name the files instead —
   test_rewriting_commands_are_held_to_the_scope (and an output redirect,
   a ruff check --config, which may say `fix = true`, --add-ignore, and
   any ruff check option not known to rewrite nothing: it fails closed),
   test_an_argfile_never_hides_options_or_files_from_the_policy,
   test_tests_and_the_verify_gate_checks_still_run_on_any_file.
3. The backstop: run_task records out_of_scope (what the branch changed
   against its base, committed or not, outside files_in_scope) in
   last_result and the report's task section; `evi task show` prints it;
   `evi task verify` refuses (exit 2, nothing signed) unless
   --allow-out-of-scope REASON, which goes into the signed reviewer notes —
   test_the_backstop_records_a_file_a_command_wrote,
   test_changed_files_takes_commits_the_work_tree_and_untracked_files,
   test_a_runs_record_leaves_out_the_caches_its_tests_leave_untracked,
   test_verify_refuses_out_of_scope_work_unless_the_director_says_why,
   test_the_allowance_note_names_a_capped_list_of_files. A task without
   files_in_scope is refused before it starts —
   test_a_task_without_files_in_scope_is_refused_before_it_starts.
4. `evi run --policy implementer --scope GLOB` keeps today's meaning
   unless --confine; run_task builds the confined policy —
   test_evi_run_confine_holds_writes_to_the_scope (and
   test_run_task_denies_... asserts agent.policy.confine).
5. Tasks have an optional hints field: `evi task new --hint` (repeatable,
   joined) and --from, a "How to approach it" section after Why, and the
   MCP task_create tool takes it — test_hints_come_after_why_in_the_prompt,
   test_task_new_takes_hints_from_flags_and_a_file,
   test_run_task_denies_... (the prompt the model got),
   test_mcp_task_create_takes_hints.

Real git repositories live under tmp_path, and so do eVi's home, config and
trust list (never ~/.evi, never the real repository's .evi/tasks).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

import evi.tools.fs  # noqa: F401  registers write_file, edit_file, apply_patch
import evi.tools.shell  # noqa: F401  registers run_command
from evi import attest, tasks, trust
from evi import hooks as hooks_mod
from evi import worktree as wt
from evi.config import Config
from evi.llm.agent import Agent
from evi.policy import implementer_policy, in_scope, validate_scope
from evi.tools.base import REGISTRY
from tests.conftest import llm_call, llm_text

ROOT = Path(__file__).resolve().parents[1]
PY = Path(sys.executable).as_posix()
needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
# A required check that also writes a file outside the task's scope, as a
# test's own output would.
WRITES_STRAY = """\
import pathlib
pathlib.Path("stray.txt").write_text("left behind\\n", encoding="utf-8")
assert pathlib.Path("calc.py").read_text(encoding="utf-8") == "x = 2\\n"
"""
# A required check that leaves cache files untracked, as a test run does in a
# repository that doesn't ignore them, and a stray file.
WRITES_CACHES = """\
import pathlib
for name in ("__pycache__/calc.cpython-313.pyc", "sub/.pytest_cache/v/cache/lastfailed"):
    pathlib.Path(name).parent.mkdir(parents=True, exist_ok=True)
    pathlib.Path(name).write_text("x", encoding="utf-8")
pathlib.Path("__pycache__/tracked.pyc").write_text("new", encoding="utf-8")
pathlib.Path("stray.txt").write_text("left behind\\n", encoding="utf-8")
"""


def _git(cwd: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid",
         "-c", "commit.gpgsign=false", "-c", "init.defaultBranch=main", *args],
        cwd=cwd, capture_output=True, text=True, timeout=60,
    )
    if proc.returncode != 0:
        raise AssertionError(f"git {' '.join(args)}: {proc.stderr}")
    return proc.stdout


@pytest.fixture
def home(monkeypatch, tmp_path):
    """eVi's home, config, transcripts, backends and trust list under
    tmp_path, no safe mode, no agent markers in this process."""
    import evi.config as config_mod
    import evi.sessions as sessions_mod

    evi_home = tmp_path / "evi-home"
    evi_home.mkdir()
    monkeypatch.delenv("EVI_SAFE_MODE", raising=False)
    monkeypatch.delenv("EVI_AGENT", raising=False)
    monkeypatch.delenv("EVI_INTERACTIVE", raising=False)
    monkeypatch.setenv("EVI_HOME", str(evi_home))
    monkeypatch.setattr(config_mod, "HOME", evi_home)
    monkeypatch.setattr(config_mod, "CONFIG_PATH", evi_home / "config.toml")
    monkeypatch.setattr(config_mod, "AGENTS_CONFIG_PATH", evi_home / "agents.toml")
    monkeypatch.setattr(config_mod, "ensure_dirs", lambda: None)
    monkeypatch.setattr(hooks_mod, "HOOKS_CONFIG_PATH", evi_home / "hooks.toml")
    monkeypatch.setattr("evi.guardrails.GUARDRAILS_PATH", evi_home / "guardrails.toml")
    monkeypatch.setattr("evi.plugins.plugin_dirs", lambda root=None: [])
    monkeypatch.setattr(sessions_mod, "TRANSCRIPTS_DIR", evi_home / "transcripts")
    monkeypatch.setattr(sessions_mod, "HANDOFF_DIR", evi_home / "handoff")
    monkeypatch.setattr("evi.backends.registry.BACKENDS_PATH", evi_home / "backends.json")
    (evi_home / "config.toml").write_text(
        "[tools]\nmemory = false\nskills = false\ntranscripts = false\n", encoding="utf-8")
    return evi_home


@pytest.fixture
def root(home, tmp_path) -> Path:
    """A trusted folder (not a repository) for the policy's unit tests."""
    folder = tmp_path / "task"
    (folder / "sub" / "deep").mkdir(parents=True)
    (folder / "calc.py").write_text("x = 1\n", encoding="utf-8")
    (folder / "other.py").write_text("y = 1\n", encoding="utf-8")
    (folder / "sub" / "a.py").write_text("a = 1\n", encoding="utf-8")
    trust.grant(folder)
    return folder.resolve()


@pytest.fixture
def repo(home, tmp_path, monkeypatch) -> Path:
    """A trusted git repository on main with calc.py, a test that writes a
    stray file, EVI.md and eVi's .gitignore; the process's folder."""
    if shutil.which("git") is None:
        pytest.skip("git not installed")
    folder = tmp_path / "repo"
    folder.mkdir()
    _git(folder, "init", "-q")
    _git(folder, "config", "core.autocrlf", "false")
    (folder / "calc.py").write_text("x = 1\n", encoding="utf-8")
    (folder / "other.py").write_text("y = 1\n", encoding="utf-8")
    (folder / "check_stray.py").write_text(WRITES_STRAY, encoding="utf-8")
    (folder / "EVI.md").write_text("# Project\nthe implementer contract\n", encoding="utf-8")
    shutil.copyfile(ROOT / ".gitignore", folder / ".gitignore")
    _git(folder, "add", "-A")
    _git(folder, "commit", "-q", "-m", "start")
    trust.grant(folder)
    monkeypatch.chdir(folder)
    return folder.resolve()


def _check(pol, name: str, args: dict, root: Path):
    return pol.check(REGISTRY[name], json.dumps(args), workdir=str(root))


def _cli(*args: str):
    import evi.apps.cli.main as cli_main

    return CliRunner().invoke(cli_main.app, list(args))


def _record(repo: Path, task_id: str = "T1") -> dict:
    return tomllib.loads((repo / ".evi" / "tasks" / f"{task_id}.toml").read_text(encoding="utf-8"))


def _agent(client) -> Agent:
    return Agent(client=client, config=Config.load(), transcripts=None,
                 tools=[REGISTRY[n] for n in ("read_file", "write_file", "edit_file",
                                              "apply_patch", "run_command")])


# ---- 1. write tools under a confined policy ------------------------------------------


def test_confined_writes_stay_inside_files_in_scope(root):
    scope = ["calc.py", "sub/*.py"]
    pol = implementer_policy(root, scope, confine=True)
    assert pol.confine and not implementer_policy(root, scope).confine
    for name, args in (
        ("write_file", {"path": "calc.py", "content": "x = 2\n"}),
        ("edit_file", {"path": "calc.py", "old_string": "1", "new_string": "2"}),
        ("write_file", {"path": "sub/new.py", "content": ""}),
        ("write_file", {"path": "sub/deep/b.py", "content": ""}),  # fnmatch: * crosses folders
        ("apply_patch", {"path": "sub/a.py", "patch": "x"}),
    ):
        assert _check(pol, name, args, root).allowed, (name, args)
    for name, args in (
        ("write_file", {"path": "other.py", "content": "pwned"}),
        ("edit_file", {"path": "other.py", "old_string": "1", "new_string": "2"}),
        ("apply_patch", {"path": "other.py", "patch": "x"}),
        ("write_file", {"path": "sub/notes.txt", "content": "x"}),
        ("write_file", {"path": "sub/../other.py", "content": "x"}),
    ):
        v = _check(pol, name, args, root)
        assert not v.allowed, (name, args)
        # The reason names the scope and tells the model what to do instead.
        assert "not in the task's files_in_scope" in v.reason
        assert "concern in your hand-off" in v.reason
        assert "files_in_scope: calc.py, sub/*.py" in v.reason
    # Unconfined (`evi run --scope` without --confine): today's meaning.
    unconfined = implementer_policy(root, scope)
    assert _check(unconfined, "write_file", {"path": "other.py", "content": "x"}, root).allowed
    # describe() says what a confined run may edit.
    assert "files_in_scope" in pol.describe() and "files_in_scope" not in unconfined.describe()


def test_every_file_an_apply_patch_names_must_be_in_scope(root):
    """eVi's apply_patch edits one file, but the policy checks every path a
    write call names (`Tool.path_args`), so a patch tool touching several
    files is held to the scope file by file."""
    pol = implementer_policy(root, ["calc.py", "sub/*.py"], confine=True)

    def patch(*files: str) -> SimpleNamespace:
        return SimpleNamespace(name="apply_patch", category="fs",
                               path_args=lambda _data: list(files))

    assert pol.check(patch("calc.py", "sub/a.py"), "{}", workdir=str(root)).allowed
    v = pol.check(patch("calc.py", "other.py", "sub/a.py"), "{}", workdir=str(root))
    assert not v.allowed and "'other.py' is not in the task's files_in_scope" in v.reason


def test_a_broad_scope_still_grants_no_protected_file_when_confined(root):
    pol = implementer_policy(root, ["**"], confine=True)
    assert _check(pol, "write_file", {"path": "anything/at/all.py", "content": ""}, root).allowed
    v = _check(pol, "write_file", {"path": ".github/workflows/ci.yml", "content": ""}, root)
    assert not v.allowed and "files_in_scope doesn't name" in v.reason
    v = _check(pol, "write_file", {"path": ".evi/tasks/T1.toml", "content": ""}, root)
    assert not v.allowed and "no task may write it" in v.reason
    named = implementer_policy(root, [".github/workflows/ci.yml"], confine=True)
    assert _check(named, "write_file", {"path": ".github/workflows/ci.yml", "content": ""},
                  root).allowed


def test_an_empty_scope_lets_a_confined_run_change_nothing(root):
    pol = implementer_policy(root, [], confine=True)
    v = _check(pol, "write_file", {"path": "calc.py", "content": ""}, root)
    assert not v.allowed and "names no files_in_scope" in v.reason
    assert "concern in your hand-off" in v.reason
    # Reads stay as they are.
    assert _check(pol, "read_file", {"path": "other.py"}, root).allowed


def test_in_scope_is_a_glob_over_the_whole_path_or_a_folder():
    assert in_scope(["calc.py"], "calc.py")
    assert not in_scope(["calc.py"], "calc.pyc") and not in_scope(["calc.py"], "sub/calc.py")
    assert in_scope(["evi/*.py"], "evi/apps/cli/main.py")  # fnmatch: * crosses folders
    assert in_scope(["tests/fixtures"], "tests/fixtures/a/b.json")  # a folder entry
    assert in_scope(["./tests/fixtures/"], "tests/fixtures/a.json")
    assert not in_scope(["tests/fixtures"], "tests/fixtures2/a.json")
    assert in_scope(["sub\\a.py"], "sub/a.py")
    assert not in_scope([], "calc.py") and not in_scope(["*"], "") and not in_scope(["*"], ".")
    # Case-insensitive where the file system is.
    assert in_scope(["Calc.PY"], "calc.py") == (os.name == "nt")
    # `.` is the whole folder (validate_scope keeps it), not a scope matching nothing.
    assert validate_scope(["."]) == (".",) and validate_scope(["./."]) == (".",)
    assert in_scope(["."], "calc.py") and in_scope(["./."], "a/b/c.txt")
    assert not in_scope(["."], "")
    # A `**/` matches no folder too, as in a .gitignore, besides one or more.
    assert in_scope(["**/*.py"], "calc.py") and in_scope(["**/*.py"], "a/b/c.py")
    assert in_scope(["tests/**/test_*.py"], "tests/test_x.py")
    assert in_scope(["tests/**/test_*.py"], "tests/unit/deep/test_x.py")
    assert in_scope(["src/**/*.py"], "src/a.py") and not in_scope(["src/**/*.py"], "a.py")
    assert in_scope(["a/**/b/**/c.py"], "a/b/x/c.py") and in_scope(["a/**/b/**/c.py"], "a/b/c.py")
    assert not in_scope(["**/*.py"], "calc.txt")
    # Only a `**/` that is a whole part: `x**/` keeps fnmatch's meaning.
    assert not in_scope(["sub**/a.py"], "suba.py")
    # A name with glob characters in it covers itself, and a folder named so its files.
    assert in_scope(["pages/[id].tsx"], "pages/[id].tsx")
    assert in_scope(["a[1].py"], "a[1].py") and in_scope(["a[1].py"], "a1.py")  # and as a glob
    assert in_scope(["data/[raw]"], "data/[raw]/x.csv")
    assert not in_scope(["pages/[id].tsx"], "pages/x.tsx")


def test_a_literal_or_whole_folder_entry_lets_a_confined_write_through(root):
    """A denial never contradicts itself by listing the very entry that
    names the file; `.` covers the folder but still grants no protected file."""
    pol = implementer_policy(root, ["pages/[id].tsx", "**/*.md", "calc.py"], confine=True)
    assert _check(pol, "write_file", {"path": "pages/[id].tsx", "content": ""}, root).allowed
    assert _check(pol, "write_file", {"path": "README.md", "content": ""}, root).allowed
    v = _check(pol, "write_file", {"path": "pages/x.tsx", "content": ""}, root)
    assert not v.allowed and "not in the task's files_in_scope" in v.reason
    whole = implementer_policy(root, ["."], confine=True)
    assert _check(whole, "write_file", {"path": "sub/deep/new.py", "content": ""}, root).allowed
    v = _check(whole, "write_file", {"path": "EVI.md", "content": ""}, root)
    assert not v.allowed and "files_in_scope doesn't name" in v.reason


# ---- 2. commands that rewrite the files they name ------------------------------------------


@pytest.mark.parametrize(("command", "why"), [
    ("ruff format .", "a folder: name the files instead"),
    ("ruff format sub", "a folder: name the files instead"),
    ("ruff format", "names none"),
    ("ruff check --fix", "names none"),
    ("ruff format other.py", "'other.py' is not in the task's files_in_scope"),
    ("ruff check --fix other.py", "'other.py' is not in the task's files_in_scope"),
    ("ruff check --unsafe-fixes --select I calc.py other.py", "'other.py' is not in"),
    ("ruff check --add-noqa other.py", "'other.py' is not in"),
    # --add-ignore writes `# ruff: ignore[...]` comments into the files.
    ("ruff check --add-ignore other.py", "'other.py' is not in"),
    ("ruff check --add-ignore=why other.py", "'other.py' is not in"),
    ("ruff check --add-ignore .", "a folder: name the files instead"),
    ("ruff check --add-ignore", "names none"),
    (f'"{PY}" -m ruff check --no-cache --isolated --select F401 --add-ignore other.py',
     "'other.py' is not in"),
    # ruff check with an option not known to rewrite nothing (one a newer
    # ruff adds) fails closed, a short one in a cluster too.
    ("ruff check --add-something-later other.py", "'other.py' is not in"),
    ("ruff check --no-cache --add-something-later .", "a folder: name the files instead"),
    ("ruff check -nz .", "a folder: name the files instead"),
    ("ruff check --diff --add-something-later .", "a folder: name the files instead"),
    (f'"{PY}" -m ruff format .', "a folder: name the files instead"),
    (f'"{PY}" -m ruff check --fix-only sub', "a folder"),
    ("ruff format calc*.py", "name the files instead"),
    ("cargo clippy --fix", "whole crate"),
    # A --config may say `fix = true` (inline, or in the file it names).
    ('ruff check --config "fix = true" other.py', "'other.py' is not in"),
    ("ruff check --config=fix=true other.py", "'other.py' is not in"),
    ('ruff check --no-cache --select F401 --config "fix = true" calc.py other.py',
     "'other.py' is not in"),
    ('ruff check --config "fix = true"', "names none"),
    ("ruff check --config calc.py .", "a folder: name the files instead"),
    (f'"{PY}" -m ruff check --config "fix = true" sub', "a folder"),
    ("pytest -q > out.txt", "'out.txt' (a redirect)"),
    ("ruff check calc.py 2> sub/log.txt", "(a redirect)"),
])
def test_rewriting_commands_are_held_to_the_scope(root, command, why):
    pol = implementer_policy(root, ["calc.py", "sub/a.py"], confine=True)
    v = pol.check_command(command, exec_dir=root, dialect="bash")
    assert not v.allowed and why in v.reason, v
    assert "files_in_scope" in v.reason
    # Unconfined, the same line is allowed (as before): --scope grants
    # protected files only.
    assert implementer_policy(root, ["calc.py"]).check_command(
        command, exec_dir=root, dialect="bash").allowed, command


@pytest.mark.parametrize("command", [
    "ruff format calc.py sub/a.py",
    "ruff format --line-length 100 calc.py",
    "ruff check --fix --select E501 calc.py",
    "ruff check --fix --select=E501 -- calc.py",
    "ruff format --check .",
    "ruff format --diff .",
    "ruff check --fix --diff .",
    "ruff check .",  # a check rewrites nothing it is given (no --fix)
    # ... nor with the options known to rewrite nothing (short ones clustered).
    "ruff check --no-cache --select F401 --statistics --output-format concise -nq .",
    "ruff check --exit-zero --show-fixes --isolated --color never --ignore-noqa sub",
    "ruff check --add-ignore=why calc.py",  # a file in scope
    "ruff format --color never calc.py",  # --color's value is no file
    'ruff check --config "line-length = 100" calc.py',  # a --config: files in scope only
    'ruff check --config "fix = true" --diff .',  # --diff writes nothing
    "pytest -q > calc.py",  # a redirect into a file in scope
    "pytest -q 2>&1",
    "pytest -q > /dev/null",
    "cargo test -- --fix",  # after --, the tests' own argument
])
def test_commands_that_rewrite_nothing_outside_the_scope_run(root, command):
    pol = implementer_policy(root, ["calc.py", "sub/a.py"], confine=True)
    v = pol.check_command(command, exec_dir=root, dialect="bash")
    assert v.allowed, v


@pytest.mark.parametrize("confine", [True, False])
def test_an_argfile_never_hides_options_or_files_from_the_policy(root, confine):
    """pytest and ruff read more arguments from the file an @word names, so
    `pytest @calc.py` could carry --basetemp or --junitxml and `ruff check
    @calc.py` a --fix and a file out of scope: refused wherever the word is,
    confined or not, after a check command's prefix too."""
    pol = implementer_policy(root, ["calc.py"], confine=confine,
                             checks=[(PY, "-m", "pytest"), (PY, "-m", "ruff", "check")])
    for line in ("pytest @calc.py", "pytest -q -k @calc.py", "pytest -q -- @calc.py",
                 "ruff check @calc.py", "ruff format calc.py @calc.py",
                 f'"{PY}" -m pytest @calc.py', f'"{PY}" -m ruff check --select @calc.py'):
        v = pol.check_command(line, exec_dir=root, dialect="bash")
        assert not v.allowed and "reads more arguments from that file" in v.reason, line
    for argv in ((PY, "-m", "pytest", "@calc.py"), (PY, "-m", "ruff", "check", "@calc.py")):
        v = pol.allows_argv(argv, run_in=root)
        assert not v.allowed and "reads more arguments from that file" in v.reason, argv
    # An @ inside a word is no argfile.
    assert pol.check_command("pytest -q -k a@b calc.py", exec_dir=root, dialect="bash").allowed


def test_tests_and_the_verify_gate_checks_still_run_on_any_file(root):
    """Confinement is for writes: tests and linters run on any file, so the
    verify gate's targeted tests (a test file outside the scope) and ruff
    check still pass the policy."""
    (root / "tests").mkdir()
    (root / "tests" / "test_calc.py").write_text("def test_x(): pass\n", encoding="utf-8")
    pol = implementer_policy(root, ["calc.py"], confine=True,
                             checks=[(PY, "-m", "pytest"), (PY, "-m", "ruff", "check")])
    for argv in ((PY, "-m", "pytest", "-q", "-p", "no:cacheprovider", "tests/test_calc.py"),
                 (PY, "-m", "ruff", "check", "--no-cache", "calc.py", "other.py")):
        assert pol.allows_argv(argv, run_in=root).allowed, argv
    for line in ("pytest -q tests", "pytest -q tests/test_calc.py other.py", "ruff check sub"):
        assert pol.check_command(line, exec_dir=root, dialect="bash").allowed, line
    # ... but the check prefix doesn't let python -m ruff rewrite out of scope.
    v = pol.allows_argv((PY, "-m", "ruff", "check", "--fix", "other.py"), run_in=root)
    assert not v.allowed and "not in the task's files_in_scope" in v.reason


# ---- run_task on the fake LLM -----------------------------------------------------------


def test_run_task_offers_run_command_so_the_model_can_run_its_tests(repo, fake_llm):
    q = tasks.TaskQueue(repo)
    q.create(tasks.new_task("T1", title="Make x two", files_in_scope=["calc.py"]))
    client = fake_llm([
        llm_call(("run_command", {"command": "git status --short"}),
                 ("run_command", {"command": "curl https://example.com"})),
        llm_text("Done."),
    ])
    # Built with no shell tool, as [tools] shell = false (the default) builds it.
    agent = Agent(client=client, config=Config.load(), transcripts=None,
                  tools=[REGISTRY["write_file"]])
    run = tasks.run_task(q, "T1", lambda _wt: agent, holder="me")

    assert "run_command" in agent.tools and agent.mode == "code"
    assert [(c["name"], c["status"]) for c in run.result.tool_calls] == [
        ("run_command", "ok"), ("run_command", "denied")]  # the policy still decides


def test_run_task_denies_an_out_of_scope_write_and_tells_the_model_why(repo, fake_llm):
    q = tasks.TaskQueue(repo)
    q.create(tasks.new_task("T1", title="Make x two", why="the answer is two",
                            hints=["Only calc.py needs to change.", "Keep it one line."],
                            files_in_scope=["calc.py"]))
    patch = "<<<<<<< SEARCH\ny = 1\n=======\ny = 2\n>>>>>>> REPLACE"
    client = fake_llm([
        llm_call(("write_file", {"path": "other.py", "content": "y = 2\n"}),
                 ("apply_patch", {"path": "other.py", "patch": patch}),
                 ("run_command", {"command": "ruff format ."})),
        llm_call(("read_file", {"path": "calc.py"})),
        llm_call(("write_file", {"path": "calc.py", "content": "x = 2\n"})),
        llm_text("Done: calc.py sets x = 2. Concern: other.py:1 may want y = 2 too."),
    ])
    agent = _agent(client)
    run = tasks.run_task(q, "T1", lambda _wt: agent, holder="me")

    assert run.stop_reason == "done" and run.implemented, run
    assert agent.policy is not None and agent.policy.confine  # run_task confines
    assert agent.policy.extra_writable == ("calc.py",)
    calls = run.result.tool_calls
    assert [(c["name"], c["status"]) for c in calls] == [
        ("write_file", "denied"), ("apply_patch", "denied"), ("run_command", "denied"),
        ("read_file", "ok"), ("write_file", "ok")]
    for c in calls[:2]:
        assert "'other.py' is not in the task's files_in_scope" in c["reason"]
        assert "concern in your hand-off" in c["reason"]
        assert "files_in_scope: calc.py" in c["reason"]
    assert "a folder: name the files instead" in calls[2]["reason"]
    # The model saw the reason as the tool result.
    second = client.requests[1]["messages"]
    tool_results = [m.get("content") or "" for m in second if m.get("role") == "tool"]
    assert any("concern in your hand-off" in str(t) for t in tool_results), tool_results
    # Nothing outside the scope changed; the in-scope work is there.
    worktree = Path(run.record["worktree"])
    assert (worktree / "other.py").read_text(encoding="utf-8") == "y = 1\n"
    assert (worktree / "calc.py").read_text(encoding="utf-8") == "x = 2\n"
    # The backstop agrees: nothing out of scope, in the task and the report.
    assert run.record["last_result"]["out_of_scope"] == []
    report = json.loads(Path(run.report_path).read_text(encoding="utf-8"))
    assert report["task"]["out_of_scope"] == [] and report["task"]["files_in_scope"] == ["calc.py"]
    # The director's hints reached the prompt, after Why.
    prompt = next(str(m["content"]) for m in client.requests[0]["messages"]
                  if m["role"] == "user" and "Implement task T1" in str(m["content"]))
    assert "How to approach it:\nOnly calc.py needs to change.\nKeep it one line." in prompt
    assert prompt.index("Why:") < prompt.index("How to approach it:") < prompt.index(
        "Files in scope")


def test_the_backstop_records_a_file_a_command_wrote(repo, fake_llm):
    """A command the policy allowed (the task's own required check, run by
    the verify gate and by eVi after the turn) writes stray.txt: the policy
    can't see that, the backstop lists it."""
    q = tasks.TaskQueue(repo)
    q.create(tasks.new_task("T1", title="Make x two", files_in_scope=["calc.py"],
                            required_checks=[[PY, "check_stray.py"]]))
    client = fake_llm([
        llm_call(("read_file", {"path": "calc.py"})),
        llm_call(("write_file", {"path": "calc.py", "content": "x = 2\n"})),
        llm_text("Done."),
    ])
    run = tasks.run_task(q, "T1", lambda _wt: _agent(client), holder="me")
    assert run.implemented, (run.stop_reason, run.note)
    worktree = Path(run.record["worktree"])
    assert (worktree / "stray.txt").is_file()
    assert run.record["last_result"]["out_of_scope"] == ["stray.txt"]
    assert _record(repo)["last_result"]["out_of_scope"] == ["stray.txt"]
    assert "out_of_scope_more" not in _record(repo)["last_result"]  # none past the list
    report = json.loads(Path(run.report_path).read_text(encoding="utf-8"))
    assert report["task"]["out_of_scope"] == ["stray.txt"]
    assert report["task"]["out_of_scope_more"] == 0
    # `evi task show` prints it (text and json).
    shown = _cli("task", "show", "T1")
    assert shown.exit_code == 0, shown.output
    assert "out of scope: stray.txt" in shown.output and "--allow-out-of-scope" in shown.output
    data = json.loads(_cli("task", "show", "T1", "--format", "json").stdout)
    assert data["task"]["last_result"]["out_of_scope"] == ["stray.txt"]


@pytest.mark.parametrize("how", ["many", "git fails"])
def test_the_backstop_caps_its_list_and_never_fails_the_run(repo, fake_llm, monkeypatch, how):
    """A long list is cut (the task file is tracked) with the rest counted;
    git failing leaves the list out (verify checks again) and notes why."""
    q = tasks.TaskQueue(repo)
    q.create(tasks.new_task("T1", title="Make x two", files_in_scope=["calc.py"]))
    if how == "many":
        monkeypatch.setattr(tasks, "_OUT_OF_SCOPE_LISTED", 2)
        monkeypatch.setattr(tasks, "out_of_scope", lambda *_a, **_k: ["a.txt", "b.txt", "c.txt"])
    else:
        def broken(*_a, **_k):
            raise tasks.TaskError("git status failed")

        monkeypatch.setattr(tasks, "out_of_scope", broken)
    client = fake_llm([llm_call(("read_file", {"path": "calc.py"})),
                       llm_call(("write_file", {"path": "calc.py", "content": "x = 2\n"})),
                       llm_text("Done.")])
    run = tasks.run_task(q, "T1", lambda _wt: _agent(client), holder="me")
    assert run.implemented and run.recorded, run.note
    report = json.loads(Path(run.report_path).read_text(encoding="utf-8"))
    last = _record(repo)["last_result"]
    if how == "many":
        # The list holds paths only; the rest are counted beside it.
        assert last["out_of_scope"] == report["task"]["out_of_scope"] == ["a.txt", "b.txt"]
        assert last["out_of_scope_more"] == report["task"]["out_of_scope_more"] == 1
        shown = _cli("task", "show", "T1")
        assert "out of scope: a.txt, b.txt and 1 more (`evi task verify`" in shown.output
    else:
        assert "out_of_scope" not in last and report["task"]["out_of_scope"] is None
        assert "out_of_scope_more" not in last and report["task"]["out_of_scope_more"] is None
        assert "can't be told: git status failed" in run.note


@needs_git
def test_changed_files_takes_commits_the_work_tree_and_untracked_files(repo):
    iso = wt.open_isolated("task/T9", start=repo)
    try:
        path = iso.path
        (path / "calc.py").write_text("x = 2\n", encoding="utf-8")
        (path / "docs").mkdir()
        (path / "docs" / "note.md").write_text("n\n", encoding="utf-8")
        _git(path, "add", "-A")
        _git(path, "commit", "-q", "-m", "committed work")  # base...HEAD
        _git(path, "mv", "other.py", "renamed.py")  # a rename: both names
        (path / "EVI.md").write_text("changed\n", encoding="utf-8")  # uncommitted
        (path / "new dir").mkdir()
        (path / "new dir" / "a b.txt").write_text("x\n", encoding="utf-8")  # untracked
        (path / "__pycache__").mkdir()
        (path / "__pycache__" / "x.pyc").write_bytes(b"\0")  # ignored: left out
        assert tasks.changed_files(path, iso.base) == [
            "EVI.md", "calc.py", "docs/note.md", "new dir/a b.txt", "other.py", "renamed.py"]
        assert tasks.out_of_scope(path, iso.base, ["calc.py", "docs"]) == [
            "EVI.md", "new dir/a b.txt", "other.py", "renamed.py"]
        assert tasks.out_of_scope(path, iso.base, ["*"]) == []
        with pytest.raises(tasks.TaskError):
            tasks.changed_files(path, "")
        with pytest.raises(tasks.TaskError):
            tasks.changed_files(path, "--output=x")
    finally:
        wt.release(iso)


def test_a_runs_record_leaves_out_the_caches_its_tests_leave_untracked(repo, fake_llm):
    """In a repository that doesn't ignore them, a test run leaves
    __pycache__ / .pytest_cache files untracked: the run's out_of_scope
    record leaves them out (as evi.checks leaves them out of the changed
    files), a stray file stays in, and the strict list verify computes (the
    default) keeps them all."""
    (repo / ".gitignore").write_text(".evi/*\n!.evi/tasks/\n.worktrees/\n", encoding="utf-8")
    (repo / "check_caches.py").write_text(WRITES_CACHES, encoding="utf-8")
    (repo / "__pycache__").mkdir()
    (repo / "__pycache__" / "tracked.pyc").write_text("old", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "a repository that ignores no cache")
    q = tasks.TaskQueue(repo)
    q.create(tasks.new_task("T1", title="Make x two", files_in_scope=["calc.py"],
                            required_checks=[[PY, "check_caches.py"]]))
    client = fake_llm([llm_call(("read_file", {"path": "calc.py"})),
                       llm_call(("write_file", {"path": "calc.py", "content": "x = 2\n"})),
                       llm_text("Done.")])
    run = tasks.run_task(q, "T1", lambda _wt: _agent(client), holder="me")
    assert run.implemented, (run.stop_reason, run.note)
    # A cache file the branch tracks is a real change: it stays in.
    assert run.record["last_result"]["out_of_scope"] == ["__pycache__/tracked.pyc", "stray.txt"]
    worktree = Path(run.record["worktree"])
    strict = tasks.out_of_scope(worktree, run.record["base"], ["calc.py"])
    assert strict == ["__pycache__/calc.cpython-313.pyc", "__pycache__/tracked.pyc",
                      "stray.txt", "sub/.pytest_cache/v/cache/lastfailed"]


def test_a_task_without_files_in_scope_is_refused_before_it_starts(repo):
    """Confined to no file, a start could change nothing (and leave nothing
    to verify): refused before it claims the task or opens a worktree."""
    q = tasks.TaskQueue(repo)
    q.create(tasks.new_task("T1", title="Make x two"))
    before = q.path("T1").read_bytes()
    built: list = []
    with pytest.raises(tasks.TaskError, match="names no files_in_scope"):
        tasks.run_task(q, "T1", lambda _wt: built.append(1), holder="me")
    assert built == [] and q.path("T1").read_bytes() == before
    assert not (repo / ".worktrees").exists()
    res = _cli("task", "start", "T1")
    assert res.exit_code == 2 and "names no files_in_scope" in res.output, res.output
    assert q.path("T1").read_bytes() == before


# ---- evi task verify ---------------------------------------------------------------------


DIRECTOR_CHAIN = [
    (101, [r"C:\venv\Scripts\python.exe", r"C:\venv\Scripts\evi.exe", "task", "verify", "T1"]),
    (100, [r"C:\venv\Scripts\evi.exe", "task", "verify", "T1"]),
    (99, ["/usr/bin/bash", "-c", "evi task verify T1"]),
]


@pytest.fixture
def review(home, repo, monkeypatch) -> Path:
    """T1 in needs_review: started (worktree and branch task/T1), its work
    committed on the branch, calc.py in scope and docs/note.md not. This
    process is the director's `evi task verify T1` (no keyring, the review
    gate stubbed clean)."""
    monkeypatch.setattr(attest, "_keyring", lambda: None)
    monkeypatch.setattr(attest, "_process_chain", lambda: list(DIRECTOR_CHAIN))
    monkeypatch.setattr(attest, "_has_tty", lambda: False)
    monkeypatch.setattr(attest, "review_gate", lambda base, *, cwd=None, reviewer=None:
                        attest.ReviewResult(0, base, "Nothing found.\n\nAPPROVE"))
    q = tasks.TaskQueue(repo)
    q.create(tasks.new_task("T1", title="Make x two", files_in_scope=["calc.py"]))
    q.update("T1", lambda r: tasks.claim(r, holder="evi"))
    iso = wt.open_isolated("task/T1", start=repo)
    (iso.path / "calc.py").write_text("x = 2\n", encoding="utf-8")
    (iso.path / "docs").mkdir()
    (iso.path / "docs" / "note.md").write_text("an unasked-for note\n", encoding="utf-8")
    _git(iso.path, "add", "-A")
    _git(iso.path, "commit", "-q", "-m", "make x two")
    wt.release(iso)
    q.update("T1", lambda r: tasks.finish(
        r, holder="evi", implemented=True, stop_reason="done", exit_code=0,
        out_of_scope=["docs/note.md"],
        fields={"worktree": str(iso.path), "branch": iso.branch, "base": iso.base}))
    q.update("T1", tasks.submit)
    attest.init_key(yes=True)
    return repo


def test_verify_refuses_out_of_scope_work_unless_the_director_says_why(review):
    before = (review / ".evi" / "tasks" / "T1.toml").read_bytes()
    res = _cli("task", "verify", "T1", "--yes")
    assert res.exit_code == 2, res.output
    assert "outside its files_in_scope (docs/note.md)" in res.output
    assert "--allow-out-of-scope REASON" in res.output
    assert (review / ".evi" / "tasks" / "T1.toml").read_bytes() == before  # nothing signed

    # A task file edited to drop the recorded list changes nothing: verify
    # looks at the branch it signs.
    q = tasks.TaskQueue(review)
    record = q.read("T1")
    record["last_result"]["out_of_scope"] = []
    q.path("T1").write_text(tasks.dumps(record), encoding="utf-8")
    assert _cli("task", "verify", "T1", "--yes").exit_code == 2

    res = _cli("task", "verify", "T1", "--yes", "--reason", "looks right",
               "--allow-out-of-scope", "the director asked for the note")
    assert res.exit_code == 0, res.output
    signed = _record(review)
    assert signed["status"] == "verified"
    notes = signed["reviewer_notes"]
    assert [n["verdict"] for n in notes] == ["verified", "out_of_scope_allowed"]
    assert notes[1]["note"] == ("the director asked for the note (changed outside "
                                "files_in_scope: docs/note.md)")
    # The reason is signed: the attestation covers the notes, and holds.
    shown = json.loads(_cli("task", "show", "T1", "--format", "json").stdout)
    assert shown["label"] == "verified", shown["label"]
    edited = {**signed, "reviewer_notes": notes[:1]}
    q.path("T1").write_text(tasks.dumps(edited), encoding="utf-8")
    assert json.loads(_cli("task", "show", "T1", "--format", "json").stdout)[
        "label"] != "verified"


def test_the_allowance_note_names_a_capped_list_of_files():
    """The note goes into the tracked, signed task file: it names the first
    files and counts the rest, as verify's refusal does."""
    record = {**tasks.new_task("T1", title="t", files_in_scope=["calc.py"]),
              "status": "needs_review"}
    body = tasks.for_verdict(record, verdict="verified")
    files = [f"vendor/pkg{n:04}/mod.py" for n in range(5000)]
    note = tasks.allow_out_of_scope(body, files, "vendored on purpose")["reviewer_notes"][-1]
    assert note["verdict"] == "out_of_scope_allowed"
    assert note["note"].startswith("vendored on purpose (changed outside files_in_scope: "
                                   "vendor/pkg0000/mod.py, vendor/pkg0001/mod.py, ")
    assert note["note"].endswith("vendor/pkg0009/mod.py and 4990 more)")
    assert len(note["note"]) < 400
    assert tasks.paths_text(["a", "b"]) == "a, b"
    assert tasks.paths_text(["a", "b"], more=3) == "a, b and 3 more"
    with pytest.raises(tasks.TaskError, match="needs a reason"):
        tasks.allow_out_of_scope(body, files, " ")


def test_verify_without_out_of_scope_work_records_no_allowance(review):
    folder = review / ".worktrees" / "task__T1"
    _git(folder, "rm", "-q", "-r", "docs")
    _git(folder, "commit", "-q", "-m", "drop the note")
    res = _cli("task", "verify", "T1", "--yes", "--allow-out-of-scope", "not needed")
    assert res.exit_code == 0, res.output
    assert "--allow-out-of-scope is not recorded" in res.stderr
    assert [n["verdict"] for n in _record(review)["reviewer_notes"]] == []


# ---- evi run --confine -----------------------------------------------------------------------


def _run_cli(monkeypatch, agent, *args: str):
    import evi.apps.cli.main as cli_main

    monkeypatch.setattr(cli_main, "_build_agent", lambda *a, **k: agent)
    return CliRunner().invoke(cli_main.app, ["run", *args])


_READ_WRITE = [REGISTRY["read_file"], REGISTRY["write_file"]]


def test_evi_run_confine_holds_writes_to_the_scope(home, root, monkeypatch, fake_llm):
    def script():
        return fake_llm([
            llm_call(("read_file", {"path": "calc.py"}), ("read_file", {"path": "other.py"})),
            llm_call(("write_file", {"path": "calc.py", "content": "x = 2\n"}),
                     ("write_file", {"path": "other.py", "content": "y = 2\n"})),
            llm_text("done"),
        ])

    agent = Agent(client=script(), config=Config(), tools=_READ_WRITE)
    res = _run_cli(monkeypatch, agent, "--policy", "implementer", "--format", "json",
                   "--cwd", str(root), "--scope", "calc.py", "--confine", "go")
    assert res.exit_code == 0, res.output
    report = json.loads(res.stdout)
    assert agent.policy.confine and agent.policy.extra_writable == ("calc.py",)
    assert [c["status"] for c in report["tool_calls"]] == ["ok", "ok", "ok", "denied"]
    assert "not in the task's files_in_scope" in report["tool_calls"][3]["reason"]
    assert (root / "other.py").read_text(encoding="utf-8") == "y = 1\n"

    # Without --confine, --scope keeps its meaning: it grants protected files only.
    (root / "calc.py").write_text("x = 1\n", encoding="utf-8")
    agent = Agent(client=script(), config=Config(), tools=_READ_WRITE)
    res = _run_cli(monkeypatch, agent, "--policy", "implementer", "--format", "json",
                   "--cwd", str(root), "--scope", "calc.py", "go")
    assert res.exit_code == 0, res.output
    assert not agent.policy.confine
    assert [c["status"] for c in json.loads(res.stdout)["tool_calls"]] == ["ok"] * 4

    # --confine without --scope: nothing may change, and a note says so.
    agent = Agent(client=script(), config=Config(), tools=_READ_WRITE)
    res = _run_cli(monkeypatch, agent, "--policy", "implementer", "--format", "json",
                   "--cwd", str(root), "--confine", "go")
    assert res.exit_code == 0, res.output
    assert [c["status"] for c in json.loads(res.stdout)["tool_calls"]] == [
        "ok", "ok", "denied", "denied"]
    assert "--confine without --scope" in res.stderr


def test_evi_run_confine_needs_policy(home, monkeypatch):
    import evi.apps.cli.main as cli_main

    built: list = []
    monkeypatch.setattr(cli_main, "_build_agent", lambda *a, **k: built.append(1))
    res = CliRunner().invoke(cli_main.app, ["run", "--confine", "x"])
    assert res.exit_code == 2 and "--confine needs --policy" in res.output, res.output
    assert built == []  # nothing ran


# ---- 5. hints -------------------------------------------------------------------------------


def test_hints_come_after_why_in_the_prompt():
    record = tasks.new_task("T1", title="Fix it", why="it is broken",
                            hints="Look at evi/x.py:42 first.", acceptance=["a test"],
                            files_in_scope=["evi/x.py"])
    assert record["hints"] == "Look at evi/x.py:42 first."
    prompt = tasks.build_prompt(record, "/w")
    assert "Why:\nit is broken\n\nHow to approach it:\nLook at evi/x.py:42 first.\n\n" in prompt
    assert prompt.index("How to approach it") < prompt.index("Acceptance criteria")
    assert "a write to any other file is refused" in prompt
    # No hints, no section; a task without scope is told nothing may change.
    bare = tasks.build_prompt(tasks.new_task("T2", title="t"), "/w")
    assert "How to approach it" not in bare and "every write is refused" in bare
    # Lists are joined one per line; anything else is refused.
    assert tasks.new_task("T3", title="t", hints=["one", " ", "two"])["hints"] == "one\ntwo"
    with pytest.raises(tasks.TaskError, match="hints"):
        tasks.new_task("T4", title="t", hints={"a": 1})
    # An old task without the field still builds a prompt.
    old = {k: v for k, v in tasks.new_task("T5", title="t").items() if k != "hints"}
    assert "How to approach it" not in tasks.build_prompt(old, "/w")


def test_a_reopened_tasks_review_notes_reach_the_next_attempt():
    record = tasks.new_task("T1", title="Fix it", hints="Look at x.", acceptance=["a test"],
                            files_in_scope=["evi/x.py"])
    assert "Review notes" not in tasks.build_prompt(record, "/w")
    record = {**record, "status": "implemented"}
    record = tasks.reopen(record, note="Drop the stray comment in test_mixed.")
    record = tasks.reopen({**record, "status": "implemented"}, note="  ")  # no text, no note
    record["reviewer_notes"].append("an old bare-string note")
    prompt = tasks.build_prompt(record, "/w")
    assert ("Review notes from earlier attempts (the director's: address each one; this "
            "worktree still holds the earlier attempt's changes):\n"
            "- (reopened) Drop the stray comment in test_mixed.\n"
            "- an old bare-string note\n\n") in prompt
    assert prompt.index("How to approach it") < prompt.index("Review notes") \
        < prompt.index("Acceptance criteria")


def test_task_new_takes_hints_from_flags_and_a_file(repo, tmp_path):
    res = _cli("task", "new", "--title", "Make x two", "--scope", "calc.py",
               "--hint", "Change only the constant.", "--hint", "No new files.")
    assert res.exit_code == 0, res.output
    assert _record(repo)["hints"] == "Change only the constant.\nNo new files."
    shown = _cli("task", "show", "T1")
    assert "hints: Change only the constant." in shown.output
    spec = tmp_path / "spec.toml"
    assert "no --scope" not in res.stderr
    spec.write_text('title = "From a file"\nhints = ["first", "second"]\n', encoding="utf-8")
    res = _cli("task", "new", "--from", str(spec))
    assert res.exit_code == 0, res.output
    assert _record(repo, "T2")["hints"] == "first\nsecond"
    assert "no --scope" in res.stderr and "it refuses this task" in res.stderr
    spec.write_text('title = "From a file"\nhints = "just one"\n', encoding="utf-8")
    assert _cli("task", "new", "--from", str(spec), "--hint", "the flag wins").exit_code == 0
    assert _record(repo, "T3")["hints"] == "the flag wins"
    spec.write_text('title = "Bad"\nhints = 3\n', encoding="utf-8")
    res = _cli("task", "new", "--from", str(spec))
    assert res.exit_code == 2 and "hints must be" in res.output


def test_mcp_task_create_takes_hints(repo):
    from evi.mcp import agent_tools

    spec = next(s for s in agent_tools.tool_specs() if s["name"] == "task_create")
    assert spec["inputSchema"]["properties"]["hints"]["type"] == "string"
    runs = agent_tools.AgentRuns(repo, lambda b, m: (lambda w: None))
    try:
        out = json.loads(agent_tools.dispatch(runs, "task_create", {
            "title": "Make x two", "files_in_scope": ["calc.py"],
            "hints": "Start from calc.py."}))
        assert out["hints"] == "Start from calc.py." == _record(repo)["hints"]
        bad = agent_tools.dispatch(runs, "task_create", {"title": "x", "hints": 3})
        assert bad.startswith("ERROR:") and "hints must be" in bad
    finally:
        runs.close(timeout=20)
