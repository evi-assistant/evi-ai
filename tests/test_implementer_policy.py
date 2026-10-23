"""The named "implementer" policy: worktree-confined writes, a test-command
allowlist, no network (loop-implementer-policy).

Phase 0 made unattended runs deny shell, and run_python asks unless
sandboxed, so a delegated task could not even run its tests without `-y`
(everything allowed). `evi run --policy implementer` (evi/policy.py) is the
narrow middle: the task reads and edits files in its own folder, runs tests,
linters and read-only git there, and nothing else. Every call is allowed or
denied with a reason, never asked.

Acceptance criteria, each with its test below:
1. A task run can run pytest and ruff inside its worktree but cannot write
   outside it, run curl, call web_fetch, or run 'pytest; curl x' —
   test_a_task_run_runs_tests_and_ruff_but_nothing_else (Agent.chat on
   fake_llm), with the unit-level cases in
   test_tests_linters_and_read_only_git_are_allowed and
   test_exploits_are_denied_and_the_reason_names_the_segment
2. A task naming .github/workflows/ci.yml in files_in_scope may edit it; an
   unnamed protected file is denied —
   test_a_protected_file_named_in_scope_is_editable_an_unnamed_one_is_not,
   test_only_an_explicit_scope_entry_grants_a_protected_file
3. No task can write .evi/tasks/** —
   test_no_task_can_write_evi_tasks_even_when_named (and the agent run in 2)
4. Denials appear in the run report's denied_tools with reasons —
   test_a_task_run_runs_tests_and_ruff_but_nothing_else (tool_calls[].reason)

The exploits the director listed each have a test: 'pytest; curl x',
'pytest && curl', 'git -c core.fsmonitor=x status', a symlink / junction out
of the root, .evi/tasks/x named in scope, a '**' scope not unlocking
.github/workflows. `evi run --policy` itself:
test_evi_run_policy_usage_errors_exit_2_without_a_run,
test_evi_run_policy_reports_the_policy_and_recommends_a_worktree.

Review regressions: an allowlisted command's own arguments stay in the root
(test_a_commands_own_arguments_stay_in_the_root,
test_ruff_cannot_rewrite_a_file_outside_the_root); no command line runs in
an untrusted folder, since each can run its code (test_commands_need_a_trusted_folder,
test_an_untrusted_repo_cannot_run_its_code_through_git_or_ruff,
test_evi_run_policy_in_an_untrusted_folder_runs_no_tests); relative paths
resolve where the tool resolves them, not at the root
(test_relative_paths_resolve_where_the_tool_does_not_at_the_root).

eVi's home, config and trust list live under tmp_path, never ~/.evi.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import jsonschema
import pytest

import evi.tools.code  # noqa: F401  registers run_python, check_file
import evi.tools.fs  # noqa: F401  registers read_file, write_file, ...
import evi.tools.git  # noqa: F401  registers git_status, ...
import evi.tools.memory  # noqa: F401  registers remember
import evi.tools.monitor  # noqa: F401  registers monitor
import evi.tools.shell  # noqa: F401  registers run_command
import evi.tools.subagent  # noqa: F401  registers delegate
import evi.tools.websearch  # noqa: F401  registers web_fetch, web_search
from evi import events, hooks as hooks_mod, permissions, trust
from evi.config import Config
from evi.headless import run_headless
from evi.llm.agent import Agent, policy_label
from evi.policy import (
    GRANTABLE,
    Policy,
    PolicyError,
    build,
    implementer_policy,
    scope_names,
    validate_scope,
)
from evi.tools.base import REGISTRY, Tool
from tests.conftest import llm_call, llm_text

PY = sys.executable


# ---- helpers -----------------------------------------------------------------


@pytest.fixture
def home(monkeypatch, tmp_path):
    """eVi's home, config, hooks and trust list under tmp_path, no safe mode."""
    import evi.config as config_mod
    import evi.sessions as sessions_mod

    evi_home = tmp_path / "evi-home"
    evi_home.mkdir()
    monkeypatch.delenv("EVI_SAFE_MODE", raising=False)
    monkeypatch.setenv("EVI_HOME", str(evi_home))
    monkeypatch.setattr(config_mod, "HOME", evi_home)
    monkeypatch.setattr(config_mod, "CONFIG_PATH", evi_home / "config.toml")
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
    """The task's folder: a small project, trusted (`evi trust`, on the trust
    list under tmp_path: tests may run there), with a sibling folder outside
    it."""
    folder = tmp_path / "task"
    (folder / "sub").mkdir(parents=True)
    (folder / "calc.py").write_text("x = 1\n", encoding="utf-8")
    (folder / ".env").write_text("TOKEN=s3cret\n", encoding="utf-8")
    (tmp_path / "outside").mkdir()
    (tmp_path / "outside" / "secret.txt").write_text("not yours\n", encoding="utf-8")
    trust.grant(folder)
    return folder.resolve()


def _check(pol: Policy, name: str, args: dict, root: Path, **kw):
    return pol.check(REGISTRY[name], json.dumps(args), workdir=str(root), **kw)


def _cmd(pol: Policy, command: str, root: Path, dialect: str | None = None):
    return pol.check_command(command, exec_dir=root, dialect=dialect)


def _validate_report(report: dict) -> None:
    jsonschema.Draft202012Validator(events.report_schema()).validate(report)


def _link(link: Path, target: Path) -> None:
    """A directory symlink, or on Windows a junction (no privilege needed)."""
    if os.name == "nt":
        import _winapi

        _winapi.CreateJunction(str(target), str(link))
    else:
        link.symlink_to(target, target_is_directory=True)


# ---- the shell allowlist -------------------------------------------------------


def test_a_refused_run_python_points_at_run_command_for_tests(root):
    allowed, reason = _check(implementer_policy(root), "run_python", {"code": "print(1)"}, root)
    assert not allowed and "run tests and linters with run_command instead" in reason
    assert _cmd(implementer_policy(root), "python -m pytest -q tests/test_x.py", root) == (True, "")
    # Where no command may run (an untrusted folder), it doesn't suggest one.
    _, untrusted = _check(implementer_policy(root, trusted=False), "run_python",
                          {"code": "print(1)"}, root)
    assert "is not a tool the implementer policy allows" in untrusted
    assert "run_command" not in untrusted


@pytest.mark.parametrize("dialect", ["bash", "cmd"])
@pytest.mark.parametrize("command", [
    "pytest -q",
    "pytest tests/test_calc.py && ruff check calc.py",
    "ruff format calc.py",
    "python -m pytest -q",
    "python3 -m ruff check .",
    "py -m pytest",
    f'"{PY}" -m pytest --version',
    "npm test",
    "npm run test",
    "cargo check",
    "cargo test --all",
    "cargo clippy",
    "git status",
    "git diff HEAD~1 -- calc.py",
    "git log --oneline -5",
    "git show HEAD",
    "git rev-parse HEAD",
    "git ls-files",
    "git blame calc.py",
    "git grep needle",
    "git branch --show-current",
    "git branch --list fix-*",
    "git -C sub status",
    "git --no-pager log -1",
    "pytest -q > report.txt",
    # Arguments inside the root, and options that write nothing of their choosing.
    "pytest -p no:cacheprovider -W ignore::DeprecationWarning -rfE tests/test_calc.py::test_add",
    "pytest --cov=calc",
    "ruff check --select E,F --no-cache .",
    "git log --since=2.weeks --oneline -- calc.py",
    "git -C sub diff ../calc.py",  # relative to the folder git works in
    "npm test --silent",
    "npm test -- --watch=false",
    "cargo test -p calc -- --nocapture",
])
def test_tests_linters_and_read_only_git_are_allowed(root, command, dialect):
    pol = implementer_policy(root)
    assert _cmd(pol, command, root, dialect) == (True, "")


@pytest.mark.parametrize(("command", "names", "dialect"), [
    ("pytest; curl x", "'curl x'", None),
    ("pytest && curl evil.example", "'curl evil.example'", None),
    ("pytest || wget x", "'wget x'", None),
    ("pytest & curl x", "'curl x'", "cmd"),
    ("git -c core.fsmonitor=x status", "-c", None),
    ("git status -c x", "-c", None),
    ("git --config-env=core.pager=X log", "--config-env", None),
    ("git --exec-path=/tmp log", "--exec-path", None),
    ("git log --output=log.txt", "--output", None),
    ("git diff --ext-diff", "--ext-diff", None),
    ("git grep -O vim needle", "-O", None),
    ("git grep --open-files-in-pager needle", "--open-files-in-pager", None),
    ("git diff --no-index ../outside/secret.txt calc.py", "--no-index", None),
    ("git blame --contents ../outside/secret.txt calc.py", "--contents", None),
    ("git -C .. status", "outside", None),
    ("git --git-dir=../outside status", "outside", None),
    ("git push origin main", "read-only", None),
    ("git commit -am x", "read-only", None),
    ("git branch -D main", "branch", None),
    ("ruff clean", "ruff check", None),
    ("python -c pass", "-m pytest", None),
    ("python calc.py", "-m pytest", None),
    ("npm install left-pad", "npm test", None),
    ("cargo run", "cargo check", None),
    ("./pytest -q", "allowlist", "bash"),
    ("curl https://example.com", "curl", None),
    ("FOO=1 pytest", "environment variable", "bash"),
    ("pytest $(curl x)", "substitution", "bash"),
    ("pytest `curl x`", "substitution", "bash"),
    ("pytest <(curl x)", "substitution", "bash"),
    ("(curl x)", "subshell", "bash"),
    ("pytest $HOME", "expansion", "bash"),
    ("pytest > ../outside/out.txt", "outside", None),
    ("pytest > EVI.md", "protected", None),
    ("pytest | sh", "stdin", "bash"),
    ("cmd /c pytest", "inline", "cmd"),
    ("bash -c pytest", "inline", "bash"),
    ("pytest \"unbalanced", "can't be checked", "bash"),
])
def test_exploits_are_denied_and_the_reason_names_the_segment(root, command, names, dialect):
    pol = implementer_policy(root)
    verdict = _cmd(pol, command, root, dialect)
    assert not verdict.allowed
    assert names in verdict.reason, verdict.reason


def test_a_command_must_run_inside_the_root(root, tmp_path):
    pol = implementer_policy(root)
    assert not pol.check_command("pytest", exec_dir=tmp_path / "outside").allowed
    v = _check(pol, "run_command", {"command": "pytest", "cwd": "../outside"}, root)
    assert not v.allowed and "outside" in v.reason
    assert _check(pol, "run_command", {"command": "pytest", "cwd": "sub"}, root).allowed


def test_monitor_follows_the_shell_and_path_rules(root, monkeypatch):
    pol = implementer_policy(root)
    monkeypatch.chdir(root)  # monitor's command runs in the process's folder
    assert _check(pol, "monitor", {"target": "pytest -q", "kind": "command"}, root).allowed
    v = _check(pol, "monitor", {"target": "pytest; curl x", "kind": "command"}, root)
    assert not v.allowed and "curl x" in v.reason
    assert _check(pol, "monitor", {"target": "calc.py", "kind": "file"}, root).allowed
    v = _check(pol, "monitor", {"target": "../outside/secret.txt", "kind": "file"}, root)
    assert not v.allowed and "outside" in v.reason


def test_check_commands_join_the_allowlist_as_exact_prefixes(root):
    pol = implementer_policy(root, checks=[("make", "lint"), SimpleNamespace(argv=("tox", "-e", "py"))])
    assert _cmd(pol, "make lint", root) == (True, "")
    # Only the command itself: words after it (a make variable can set the
    # shell make runs recipes with) are not allowed.
    v = _cmd(pol, "make lint FILES=calc.py", root)
    assert not v.allowed and "itself may run" in v.reason
    assert not _cmd(pol, "make lint SHELL=/bin/sh", root).allowed
    assert not _cmd(pol, "make install", root).allowed
    assert _cmd(pol, "tox -e py", root).allowed
    assert not _cmd(pol, "tox -e lint", root).allowed
    # A check eVi runs itself (no shell) goes through the same rules.
    assert pol.allows_argv(["make", "lint"]).allowed
    assert pol.allows_argv([PY, "-m", "pytest", "-q", "tests/test_calc.py"]).allowed
    assert not pol.allows_argv(["curl", "x"]).allowed
    # What the model adds after a prefix stays in the root too.
    v = _cmd(pol, "make lint FILES=../outside/x.py", root)
    assert not v.allowed
    assert not pol.allows_argv(["make", "lint", "../outside"]).allowed


def test_words_after_a_check_prefix_still_meet_the_builtin_rule(root):
    # A detected pytest check is a shell_allow prefix; an option the pytest
    # rule refuses (one reaches the network) stays refused after it.
    pol = implementer_policy(root, checks=[(PY, "-m", "pytest", "tests/test_calc.py")])
    assert pol.allows_argv([PY, "-m", "pytest", "tests/test_calc.py"]).allowed
    assert pol.allows_argv([PY, "-m", "pytest", "tests/test_calc.py", "-q"]).allowed
    v = pol.allows_argv([PY, "-m", "pytest", "tests/test_calc.py", "--pastebin=all"])
    assert not v.allowed and "--pastebin" in v.reason
    # A tool detect found on PATH (a full path, not a bare name) gets the rule
    # for its name: its file arguments pass, a refused option doesn't.
    ruff = str(root.parent / "bin" / "ruff.exe")
    pol = implementer_policy(root, checks=[(ruff, "check", "--no-cache")])
    assert pol.allows_argv([ruff, "check", "--no-cache", "calc.py"]).allowed
    assert not pol.allows_argv([ruff, "check", "--no-cache", "--output-file=x"]).allowed
    assert not pol.allows_argv([ruff, "check", "--no-cache", "../outside/x.py"]).allowed


# ---- a command's own arguments ---------------------------------------------------


@pytest.mark.parametrize(("command", "names"), [
    # A path argument outside the root: written (ruff), read (git's implicit
    # --no-index), or where the tool keeps its files.
    ("ruff format ../outside/x.py", "outside"),
    ("ruff check --fix ../outside", "outside"),
    ("python -m ruff format ../outside/x.py", "outside"),
    ("pytest ../outside", "outside"),
    ("pytest --rootdir=../outside", "outside"),
    ("pytest ~/x.py", "outside"),
    ("git diff ../outside/secret.txt calc.py", "outside"),
    ("git log -- ../outside", "outside"),
    ("git -C sub diff ../../outside/secret.txt", "outside"),
    ("cargo test --manifest-path ../outside/Cargo.toml", "outside"),
    ("cargo test --target-dir=../outside", "outside"),
    # Options that write a file of their choosing, reach the network or set
    # config that runs a program: refused wherever they point.
    ("pytest --junitxml=../outside/r.xml", "--junitxml"),
    ("pytest --junitxml=report.xml", "--junitxml"),
    ("pytest --basetemp=.git", "--basetemp"),
    ("pytest --pastebin=all", "--pastebin"),
    ("pytest -o cache_dir=x", "-o"),
    ("pytest -vo cache_dir=x", "-vo"),
    ("ruff check -o ../outside/r.txt calc.py", "-o"),
    ("ruff check --output-file=r.txt calc.py", "--output-file"),
    ("ruff check --cache-dir=.git/x calc.py", "--cache-dir"),
    ("cargo test --config target.x.runner=curl", "--config"),
    ("cargo test --conf target.x.runner=curl", "--conf"),
    ("cargo check -Zunstable-options", "-Z"),
    ("npm test --prefix ../outside", "npm option"),
    ("npm test --script-shell=bash", "npm option"),
    ("npm run test --node-options=--require=x", "npm option"),
    # git takes an abbreviation of a long option.
    ("git diff --outp=EVI.md", "--outp"),
    ("git diff --no-ind ../outside/secret.txt calc.py", "--no-ind"),
    # A protected file handed to a command that may write it.
    ("ruff format EVI.md", "files_in_scope doesn't name"),
    ("ruff check .git/hooks/x.py", "no task may write it"),
    ("cargo test -- --logfile .evi/tasks/T1.toml", "no task may write it"),
])
def test_a_commands_own_arguments_stay_in_the_root(root, command, names):
    verdict = _cmd(implementer_policy(root), command, root)
    assert not verdict.allowed
    assert names in verdict.reason, verdict.reason


def test_ruff_cannot_rewrite_a_file_outside_the_root(home, root, tmp_path, fake_llm):
    """For real: the model asks ruff to format a file outside the task's
    folder; the policy denies it and the file is untouched."""
    target = tmp_path / "outside" / "x.py"
    target.write_text("x=1\n", encoding="utf-8")
    client = fake_llm([
        llm_call(("run_command", {"command": f'"{PY}" -m ruff format --no-cache ../outside/x.py'}),
                 ("run_command", {"command": f'"{PY}" -m ruff format --no-cache calc.py'})),
        llm_text("formatted"),
    ])
    res = run_headless(_agent(client, root, ["run_command"]), "format it")
    assert [c["status"] for c in res.tool_calls] == ["denied", "ok"]
    assert "outside the run's folder" in res.tool_calls[0]["reason"]
    assert target.read_text(encoding="utf-8") == "x=1\n"


def test_commands_need_a_trusted_folder(home, tmp_path, monkeypatch):
    """Every allowlisted command can run the folder's own code (pytest, npm
    and cargo by design; git through its .git/config; python -m ruff by
    importing from the folder; on Windows the shell finds a ruff.bat or
    git.exe there first): in an untrusted folder the policy runs no command
    line, nor check_file's linter, as the verify checks are skipped there.
    Reading and editing files and the git_* tools still work."""
    folder = tmp_path / "untrusted"
    folder.mkdir()
    (folder / "calc.py").write_text("x = 1\n", encoding="utf-8")
    here = folder.resolve()
    pol = implementer_policy(folder)
    assert not pol.trusted and "not trusted" in pol.describe()
    assert "git_* tools" in pol.describe()
    for command in ("pytest -q", "python -m pytest", f'"{PY}" -m pytest', "npm test",
                    "npm run test", "cargo test", "cargo check", "ruff check calc.py",
                    "python -m ruff format calc.py", f'"{PY}" -m ruff check calc.py',
                    "git status", "git --no-pager log -1", "git diff", "git branch --show-current"):
        for dialect in ("bash", "cmd"):
            v = _cmd(pol, command, here, dialect)
            assert not v.allowed and "not a trusted folder" in v.reason, (command, dialect)
            assert "git_* tools" in v.reason and "evi trust" in v.reason
    monkeypatch.chdir(here)  # monitor's command runs in the process's folder
    v = _check(pol, "monitor", {"target": "git status", "kind": "command"}, here)
    assert not v.allowed and "not a trusted folder" in v.reason
    v = _check(pol, "check_file", {"path": "calc.py"}, here)
    assert not v.allowed and "not a trusted folder" in v.reason
    assert not pol.allows_argv([PY, "-m", "pytest", "-q"]).allowed
    assert not pol.allows_argv([PY, "-m", "ruff", "check", "calc.py"]).allowed
    # A project check command joins no allowlist there either.
    assert not pol.with_commands([["make", "lint"]]).allows_argv(["make", "lint"]).allowed
    for name, args in (("read_file", {"path": "calc.py"}),
                       ("write_file", {"path": "calc.py", "content": "x = 2\n"}),
                       ("git_status", {})):
        assert _check(pol, name, args, here).allowed, name
    # Trusted, they run; an explicit answer wins over the trust list.
    trust.grant(folder)
    assert implementer_policy(folder).trusted
    for command in ("pytest -q", "ruff check calc.py", "git status"):
        assert _cmd(implementer_policy(folder), command, here).allowed, command
    assert _check(implementer_policy(folder), "check_file", {"path": "calc.py"}, here).allowed
    assert not implementer_policy(folder, trusted=False).trusted
    assert not build("implementer", folder, trusted=False).trusted


def test_an_untrusted_repo_cannot_run_its_code_through_git_or_ruff(home, tmp_path, fake_llm,
                                                                    monkeypatch):
    """A working copy eVi did not create (evi/gitsafe.py) whose .git/config
    runs a program on `git status` (core.fsmonitor), that ships a `ruff`
    package `python -m ruff` imports from the folder and, on Windows, a
    ruff.bat the shell finds before PATH: under the policy, in that
    untrusted folder, none of them runs. The git_status tool (hardened)
    still reads the status, and the denials name the git_* tools."""
    repo = tmp_path / "downloaded"
    repo.mkdir()
    marker = tmp_path / "MARKER"

    def git(*args: str) -> None:
        subprocess.run(["git", "-C", str(repo), *args], check=True, timeout=60,
                       capture_output=True)

    git("init", "-q")
    (repo / "calc.py").write_text("x = 1\n", encoding="utf-8")
    git("config", "core.fsmonitor", f"echo fsmonitor >> '{marker.as_posix()}'; false")
    (repo / "ruff").mkdir()
    (repo / "ruff" / "__init__.py").write_text("", encoding="utf-8")
    (repo / "ruff" / "__main__.py").write_text(
        f"open({str(marker)!r}, 'a').write('ruff package\\n')\n", encoding="utf-8")
    shell_lines = ["git status", f'"{PY}" -m ruff check --no-cache calc.py']
    if os.name == "nt":
        # cmd.exe looks in the current folder before PATH unless this is set.
        monkeypatch.delenv("NoDefaultCurrentDirectoryInExePath", raising=False)
        (repo / "ruff.bat").write_text(f'@echo off\r\necho bat>> "{marker}"\r\n',
                                       encoding="utf-8")
        shell_lines.append("ruff check calc.py")
    # Controls: run plainly in the folder, each line really runs its code.
    for line in shell_lines:
        subprocess.run(line, shell=True, cwd=repo, timeout=120, capture_output=True)
    fired = marker.read_text(encoding="utf-8")
    assert "fsmonitor" in fired and "ruff package" in fired, fired
    assert os.name != "nt" or "bat" in fired, fired
    marker.unlink()

    monkeypatch.chdir(repo)
    client = fake_llm([
        llm_call(*[("run_command", {"command": line}) for line in shell_lines],
                 ("monitor", {"target": "git status", "kind": "command"}),
                 ("check_file", {"path": "calc.py"}),
                 ("git_status", {})),
        llm_text("done"),
    ])
    agent = _agent(client, repo.resolve(), ["run_command", "monitor", "check_file",
                                            "git_status"])
    assert not agent.policy.trusted
    res = run_headless(agent, "go")
    calls = res.tool_calls
    assert [c["status"] for c in calls] == ["denied"] * (len(shell_lines) + 2) + ["ok"], calls
    for c in calls[:-1]:
        assert "not a trusted folder" in c["reason"] and "git_* tools" in c["reason"], c
    assert not marker.exists(), marker.read_text(encoding="utf-8")
    _validate_report(res.to_dict())


def test_relative_paths_resolve_where_the_tool_does_not_at_the_root(
    home, root, tmp_path, monkeypatch, fake_llm
):
    """`evi run --policy` started in a subfolder without --cwd: the agent has
    no working folder of its own, so its tools resolve against the process's
    folder, while the policy's root is the repository's top. The policy must
    resolve a relative path there too, not against the root: else
    .evi/tasks, an unnamed workflow or a link out of the subfolder get
    through."""
    (root / ".evi").mkdir()
    (root / ".github").mkdir()
    _link(root / "sub" / "link", tmp_path / "outside")
    cases = [
        (root / ".evi", {"path": "tasks/T1.toml", "content": "status = 'verified'"},
         "no task may write it"),
        (root / ".github", {"path": "workflows/ci.yml", "content": "on: push\n"},
         "files_in_scope doesn't name"),
        (root / "sub", {"path": "link/escaped.txt", "content": "x"}, "outside"),
    ]
    for folder, args, why in cases:
        monkeypatch.chdir(folder)
        agent = _agent(fake_llm([llm_call(("write_file", args)), llm_text("ok")]), root,
                       ["write_file"])
        agent.cwd = ""  # no --cwd: the tools work in the process's folder
        res = run_headless(agent, "go")
        assert [c["status"] for c in res.tool_calls] == ["denied"], (folder, res.tool_calls)
        assert why in res.tool_calls[0]["reason"], res.tool_calls[0]["reason"]
        # Policy.check without a workdir resolves where a tool would, too.
        v = agent.policy.check(REGISTRY["write_file"], json.dumps(args))
        assert not v.allowed and why in v.reason
    assert not (root / ".evi" / "tasks").exists()
    assert not (root / ".github" / "workflows").exists()
    assert not (tmp_path / "outside" / "escaped.txt").exists()


# ---- tools ---------------------------------------------------------------------


@pytest.mark.parametrize(("name", "args"), [
    ("web_fetch", {"url": "https://example.com"}),
    ("web_search", {"query": "x"}),
    ("run_python", {"code": "print(1)"}),
    ("remember", {"name": "x", "content": "y"}),
    ("delegate", {"profile": "x", "task": "y"}),
])
def test_network_code_memory_and_subagent_tools_are_denied(root, name, args):
    v = _check(implementer_policy(root), name, args, root)
    assert not v.allowed and "not a tool the implementer policy allows" in v.reason


def test_unknown_and_mcp_tools_are_denied_even_under_a_known_name(root):
    pol = implementer_policy(root)
    for tool in (
        SimpleNamespace(name="frobnicate", category="general"),
        SimpleNamespace(name="read_file", category="mcp"),  # an MCP tool named like ours
        SimpleNamespace(name="srv__write", category="mcp"),
    ):
        v = pol.check(tool, json.dumps({"path": "calc.py"}), workdir=str(root))
        assert not v.allowed, tool
    assert pol.check(SimpleNamespace(name="search_tools", category="meta"), "{}").allowed


def test_reads_stay_inside_the_root_and_never_take_a_secret(root):
    pol = implementer_policy(root)
    assert _check(pol, "read_file", {"path": "calc.py"}, root).allowed
    assert _check(pol, "list_dir", {}, root).allowed
    assert _check(pol, "git_status", {}, root).allowed
    assert _check(pol, "search_files", {"pattern": "x", "glob": "*.py"}, root).allowed
    for name, args, why in (
        ("read_file", {"path": "../outside/secret.txt"}, "outside"),
        ("read_file", {"path": str(root.parent / "outside" / "secret.txt")}, "outside"),
        ("list_dir", {"path": ".."}, "outside"),
        ("find_files", {"pattern": "../*"}, "glob"),
        ("search_files", {"pattern": "x", "glob": "../**/*"}, "glob"),
        ("read_file", {"path": ".env"}, "secret"),
    ):
        v = _check(pol, name, args, root)
        assert not v.allowed and why in v.reason, (name, args, v)


def test_writes_stay_inside_the_root(root, tmp_path):
    pol = implementer_policy(root)
    assert _check(pol, "write_file", {"path": "new.py", "content": "x"}, root).allowed
    assert _check(pol, "edit_file", {"path": "calc.py", "old_string": "1",
                                     "new_string": "2"}, root).allowed
    for path in ("../outside/x.py", str(tmp_path / "outside" / "x.py"), "sub/../../x.py"):
        v = _check(pol, "write_file", {"path": path, "content": "x"}, root)
        assert not v.allowed and "outside" in v.reason, path


def test_a_symlink_or_junction_out_of_the_root_is_caught(root, tmp_path):
    pol = implementer_policy(root)
    _link(root / "link", tmp_path / "outside")
    for name, args in (
        ("write_file", {"path": "link/x.py", "content": "x"}),
        ("read_file", {"path": "link/secret.txt"}),
        ("list_dir", {"path": "link"}),
    ):
        v = _check(pol, name, args, root)
        assert not v.allowed and "outside" in v.reason, (name, v)


# ---- protected files and scope -----------------------------------------------------


def test_only_an_explicit_scope_entry_grants_a_protected_file(root):
    path = {"path": ".github/workflows/ci.yml", "content": "on: push\n"}
    for scope in ([], ["**"], [".github/**"], ["*.yml"], ["**/*.yml"], [".github/*/ci.yml"],
                  [".github/workflows/other.yml"]):
        v = _check(implementer_policy(root, scope), "write_file", path, root)
        assert not v.allowed and "files_in_scope doesn't name" in v.reason, scope
    for scope in ([".github/workflows/ci.yml"], [".github/workflows/*.yml"],
                  ["./.github/workflows/ci.yml"], [".github\\workflows\\ci.yml"]):
        assert _check(implementer_policy(root, scope), "write_file", path, root).allowed, scope
    # A file at the top is granted by its exact name only.
    evi_md = {"path": "EVI.md", "content": "x"}
    for scope in (["*"], ["*.md"], ["E*"], ["sub/EVI.md"]):
        assert not _check(implementer_policy(root, scope), "write_file", evi_md, root).allowed
    assert _check(implementer_policy(root, ["EVI.md"]), "write_file", evi_md, root).allowed
    assert not _check(implementer_policy(root, ["EVI.md"]), "write_file",
                      {"path": "sub/EVI.md", "content": "x"}, root).allowed


def test_every_grantable_entry_is_a_repo_relative_write_protected_path():
    assert set(GRANTABLE) == {".evi.toml", "EVI.md", "AGENTS.md", ".evi/**",
                              ".github/workflows/**", ".vscode/tasks.json"}
    assert set(GRANTABLE) <= set(permissions.WRITE_PROTECTED_PATHS)


def test_no_task_can_write_evi_tasks_even_when_named(root):
    for scope in ([".evi/tasks/x.toml"], [".evi/tasks/*"], [".evi/**"], ["**"]):
        pol = implementer_policy(root, scope)
        for path in (".evi/tasks/x.toml", ".evi/tasks"):
            v = _check(pol, "write_file", {"path": path, "content": "x"}, root)
            assert not v.allowed and "no task may write it" in v.reason, (scope, path)
    # .evi/ outside tasks/ is grantable by name, like any other protected file.
    assert _check(implementer_policy(root, [".evi/notes.md"]), "write_file",
                  {"path": ".evi/notes.md", "content": "x"}, root).allowed


@pytest.mark.parametrize("path", [".git/config", ".git/hooks/pre-commit", ".env",
                                  "keys/id_rsa", "sub/.npmrc"])
def test_git_internals_and_secrets_are_never_granted(root, path):
    v = _check(implementer_policy(root, [path]), "write_file", {"path": path, "content": "x"}, root)
    assert not v.allowed and "no task may write it" in v.reason


def test_a_user_protected_path_is_never_granted(root):
    protected = permissions.effective_protected_paths([*permissions.SECRET_PROTECTED_PATHS,
                                                       "docs/locked.md"])
    v = _check(implementer_policy(root, ["docs/locked.md"]), "write_file",
               {"path": "docs/locked.md", "content": "x"}, root, protected_paths=protected)
    assert not v.allowed and "no task may write it" in v.reason


@pytest.mark.parametrize("entry", ["/etc/passwd", "C:/Windows/x", "~/x", "../x", "a/../../x",
                                   "//host/share/x", ""])
def test_a_scope_entry_outside_the_folder_is_refused(entry):
    with pytest.raises(PolicyError):
        validate_scope([entry])


def test_scope_names_is_the_documented_rule():
    assert scope_names(".github/workflows/ci.yml", ".github/workflows/ci.yml")
    assert scope_names(".github/workflows/*.yml", ".github/workflows/ci.yml")
    assert not scope_names(".github/workflows/*.yml", ".github/workflows/deep/ci.yml")
    assert not scope_names("**", ".github/workflows/ci.yml")
    assert not scope_names("*", "EVI.md")
    assert scope_names("EVI.md", "EVI.md")


def test_unknown_policy_names_are_refused(root):
    with pytest.raises(PolicyError):
        build("yolo", root)
    assert build("Implementer", root).name == "implementer"


# ---- an agent under the policy (fake_llm) ---------------------------------------


def _agent(client, root: Path, tools: list[str], **kw) -> Agent:
    agent = Agent(client=client, config=Config.load(), tools=[REGISTRY[t] for t in tools])
    agent.cwd = str(root)
    agent.policy = implementer_policy(root, **kw)
    return agent


def test_a_task_run_runs_tests_and_ruff_but_nothing_else(home, root, fake_llm, tmp_path):
    """Unattended, no -y: pytest and ruff run in the task's folder; curl
    (alone or chained after pytest), web_fetch and a write outside are
    denied, and the report lists each denial with its reason."""
    client = fake_llm([
        llm_call(("run_command", {"command": f'"{PY}" -m pytest --version'}),
                 ("run_command", {"command": f'"{PY}" -m ruff check --no-cache calc.py'})),
        llm_call(("run_command", {"command": "pytest; curl x"}),
                 ("run_command", {"command": "curl https://example.com"}),
                 ("web_fetch", {"url": "https://example.com"}),
                 ("write_file", {"path": "../outside/pwned.txt", "content": "x"})),
        llm_call(("write_file", {"path": "notes.txt", "content": "done\n"})),
        llm_text("tests pass"),
    ])
    agent = _agent(client, root, ["run_command", "web_fetch", "write_file"])
    assert agent.permission_callback is None and not agent.auto_all
    res = run_headless(agent, "fix it")
    assert res.stop_reason == "done" and res.policy == "implementer" == policy_label(agent)
    statuses = [(c["name"], c["status"]) for c in res.tool_calls]
    assert statuses == [
        ("run_command", "ok"), ("run_command", "ok"),
        ("run_command", "denied"), ("run_command", "denied"), ("web_fetch", "denied"),
        ("write_file", "denied"), ("write_file", "ok"),
    ]
    assert res.tool_calls[0]["output"].startswith("exit 0") and "pytest" in res.tool_calls[0]["output"]
    assert res.tool_calls[1]["output"].startswith("exit 0")
    assert res.denied_tools == ["run_command", "web_fetch", "write_file"]
    reasons = [c.get("reason", "") for c in res.tool_calls if c["status"] == "denied"]
    assert "'curl x' is not allowed" in reasons[0]
    assert "curl" in reasons[1]
    assert "web_fetch is not a tool the implementer policy allows" in reasons[2]
    assert "outside the run's folder" in reasons[3]
    assert all("reason" not in c for c in res.tool_calls if c["status"] == "ok")
    assert not (tmp_path / "outside" / "pwned.txt").exists()
    assert (root / "notes.txt").read_text(encoding="utf-8") == "done\n"
    _validate_report(res.to_dict())


def test_allow_rules_yolo_and_unattended_allow_never_widen_it(home, root, fake_llm):
    (home / "config.toml").write_text(
        "[tools]\nmemory = false\nskills = false\ntranscripts = false\n"
        '[auto]\nmode = "yolo"\nunattended_policy = "allow"\n'
        'auto_approve = ["fs", "shell", "web", "code"]\n'
        'rules = ["allow run_command curl*", "allow web_fetch"]\n', encoding="utf-8")
    client = fake_llm([
        llm_call(("run_command", {"command": "curl x"}), ("web_fetch", {"url": "https://x.example"}),
                 ("write_file", {"path": "EVI.md", "content": "pwned"})),
        llm_text("ok"),
    ])
    agent = _agent(client, root, ["run_command", "web_fetch", "write_file"])
    agent.enable_auto_all()  # even /auto on (the SDK could set it): the policy decides
    res = run_headless(agent, "go")
    assert [c["status"] for c in res.tool_calls] == ["denied", "denied", "denied"]
    assert not (root / "EVI.md").exists()


def test_hard_deny_and_deny_rules_still_deny_first(home, root, fake_llm):
    (home / "config.toml").write_text(
        "[tools]\nmemory = false\nskills = false\ntranscripts = false\n"
        '[auto]\nhard_deny = ["shell pytest*"]\nrules = ["deny write_file *.lock"]\n',
        encoding="utf-8")
    client = fake_llm([
        llm_call(("run_command", {"command": "pytest -q"}),
                 ("write_file", {"path": "deps.lock", "content": "x"})),
        llm_text("ok"),
    ])
    res = run_headless(_agent(client, root, ["run_command", "write_file"]), "go")
    assert [c["status"] for c in res.tool_calls] == ["denied", "denied"]
    assert all("hard_deny or deny rule" in c["reason"] for c in res.tool_calls)


def test_a_protected_file_named_in_scope_is_editable_an_unnamed_one_is_not(home, root, fake_llm):
    client = fake_llm([
        llm_call(("write_file", {"path": ".github/workflows/ci.yml", "content": "on: push\n"}),
                 ("write_file", {"path": ".github/workflows/release.yml", "content": "x"}),
                 ("write_file", {"path": "EVI.md", "content": "x"}),
                 ("write_file", {"path": ".evi/tasks/T1.toml", "content": "status = 'verified'"})),
        llm_text("ok"),
    ])
    agent = _agent(client, root, ["write_file"],
                   files_in_scope=[".github/workflows/ci.yml", ".evi/tasks/T1.toml"])
    res = run_headless(agent, "fix ci")
    assert [c["status"] for c in res.tool_calls] == ["ok", "denied", "denied", "denied"]
    assert (root / ".github" / "workflows" / "ci.yml").is_file()
    assert not (root / ".github" / "workflows" / "release.yml").exists()
    assert not (root / "EVI.md").exists() and not (root / ".evi" / "tasks").exists()
    assert "no task may write it" in res.tool_calls[3]["reason"]
    assert res.denied_tools == ["write_file"]


def test_the_destructive_guard_is_forced_on(home, root, fake_llm):
    (home / "config.toml").write_text(
        "[tools]\nmemory = false\nskills = false\ntranscripts = false\n"
        "[auto]\nblock_destructive = false\n", encoding="utf-8")
    pol = implementer_policy(root, checks=[("git", "reset", "--hard")])
    agent = Agent(client=fake_llm([]), config=Config.load(), tools=[REGISTRY["run_command"]])
    agent.cwd, agent.policy = str(root), pol
    tool = REGISTRY["run_command"]
    v = agent._policy_verdict(tool, json.dumps({"command": "git reset --hard"}))
    assert not v.allowed and "destructive-command guard" in v.reason
    assert agent._permission_decision(tool, json.dumps({"command": "pytest -q"})) == "allow"


def test_a_call_the_policy_cannot_check_is_denied(home, root, fake_llm):
    agent = _agent(fake_llm([]), root, ["write_file"])

    class Broken(Policy):
        def check(self, *a, **k):
            raise RuntimeError("boom")

    agent.policy = Broken("implementer", root, (root,))
    v = agent._policy_verdict(REGISTRY["write_file"], json.dumps({"path": "a", "content": ""}))
    assert not v.allowed and "could not check" in v.reason


# ---- evi run --policy -----------------------------------------------------------------


def _cli(monkeypatch, build, *args: str):
    from typer.testing import CliRunner

    import evi.apps.cli.main as cli_main

    monkeypatch.setattr(cli_main, "_build_agent", build)
    return CliRunner().invoke(cli_main.app, ["run", *args])


@pytest.mark.parametrize("args", [
    ["--policy", "nope", "x"],
    ["--policy", "implementer", "-y", "x"],
    ["--policy", "implementer", "--scope", "/etc/passwd", "x"],
    ["--policy", "implementer", "--scope", "../elsewhere", "x"],
    ["--scope", "EVI.md", "x"],
])
def test_evi_run_policy_usage_errors_exit_2_without_a_run(home, monkeypatch, args):
    built: list = []
    res = _cli(monkeypatch, lambda *a, **k: built.append(1), *args)
    assert res.exit_code == 2, res.output
    assert built == []


def test_evi_run_policy_reports_the_policy_and_recommends_a_worktree(home, monkeypatch, root,
                                                                     fake_llm):
    subprocess.run(["git", "init", "-q", str(root)], check=True, timeout=60)
    (root / "sub" / "deep").mkdir()
    client = fake_llm([
        llm_call(("write_file", {"path": "../EVI.md", "content": "x"}),
                 ("run_command", {"command": "curl x"})),
        llm_text("done"),
    ])
    agent = Agent(client=client, config=Config(), tools=[REGISTRY["write_file"],
                                                         REGISTRY["run_command"]])
    res = _cli(monkeypatch, lambda *a, **k: agent, "--policy", "implementer", "--format", "json",
               "--cwd", str(root / "sub" / "deep"), "--scope", "README.md", "go")
    assert res.exit_code == 0, res.output
    report = json.loads(res.stdout)
    assert report["policy"] == "implementer"
    # The policy's folder is the repository's top, not --cwd itself.
    assert agent.policy.root == root and agent.policy.extra_writable == ("README.md",)
    assert [c["status"] for c in report["tool_calls"]] == ["denied", "denied"]
    assert "files_in_scope doesn't name" in report["tool_calls"][0]["reason"]
    assert "--worktree" in res.stderr
    _validate_report(report)


def test_evi_run_policy_in_an_untrusted_folder_runs_no_tests(home, monkeypatch, tmp_path,
                                                             fake_llm):
    folder = tmp_path / "untrusted"
    folder.mkdir()
    client = fake_llm([llm_call(("run_command", {"command": "pytest -q"})), llm_text("done")])
    agent = Agent(client=client, config=Config(), tools=[REGISTRY["run_command"]])
    res = _cli(monkeypatch, lambda *a, **k: agent, "--policy", "implementer", "--format", "json",
               "--cwd", str(folder), "go")
    assert res.exit_code == 0, res.output
    report = json.loads(res.stdout)
    assert not agent.policy.trusted
    assert [c["status"] for c in report["tool_calls"]] == ["denied"]
    assert "not a trusted folder" in report["tool_calls"][0]["reason"]
    assert "not a trusted folder" in res.stderr and "evi trust" in res.stderr
    assert "runs no commands" in res.stderr


def test_the_policy_is_not_a_tool(root):
    """Widening a scope takes a config edit, never an agent tool call: no tool
    can reach Agent.policy."""
    assert not any("policy" in name for name in REGISTRY)
    assert isinstance(REGISTRY["write_file"], Tool)


def test_the_verify_gate_takes_the_policys_trust_for_its_root(home, root, tmp_path, fake_llm):
    # The policy's trust decides for its own root (looked up when it was
    # made, or vouched for by a caller that made the folder); any other
    # folder still asks evi.trust.
    agent = _agent(fake_llm([]), root, ["write_file"], trusted=False)
    assert not agent._folder_trusted(root)  # trusted per evi.trust, not per the policy
    made = tmp_path / "made-by-the-harness"
    made.mkdir()
    agent = _agent(fake_llm([]), made, ["write_file"], trusted=True)
    assert agent._folder_trusted(made)
    assert not agent._folder_trusted(tmp_path / "task" / ".." / "elsewhere")


@pytest.mark.skipif(os.name != "nt", reason="UNC paths are a Windows thing")
def test_a_root_on_a_network_share_is_usable_and_other_shares_stay_refused(monkeypatch):
    # A task worktree on a shared checkout lives at a UNC path. The policy's
    # own root (chosen by eVi) may be resolved there; any other UNC path is
    # refused before anything opens it (resolving one connects and
    # authenticates to its host). Path.resolve is stubbed: no network here.
    import evi.policy as P

    root = Path(r"\\fileserver\projects\evi\.worktrees\task__T1")
    resolved: list[str] = []

    def fake_resolve(self, *a, **k):
        resolved.append(str(self))
        return self

    monkeypatch.setattr(Path, "resolve", fake_resolve)
    roots = (root,)
    assert P._resolve("evi/tools/fs.py", root, roots) == root / "evi" / "tools" / "fs.py"
    assert P._resolve(str(root / "x.py"), root, roots) == root / "x.py"
    resolved.clear()
    for outside in (r"\\attacker\share\x.txt",            # another host
                    r"\\fileserver\C$\Windows\win.ini",   # another share, same host
                    str(root) + r"evil\x",                # the root as a mere prefix
                    str(root) + r"\..\..\other\x",        # .. out of the root
                    r"..\..\pyproject.toml"):
        assert P._resolve(outside, root, roots) is None, outside
    assert resolved == []  # none of them was opened
    # Without the root among `roots`, the share stays refused too.
    assert P._resolve("evi/tools/fs.py", root, ()) is None

