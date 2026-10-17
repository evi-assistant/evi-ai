"""Evals — behavioral regression gates for prompts, skills and models.

A suite is a TOML file of cases; each case is a prompt plus deterministic
assertions. Run it against the agent to get a pass rate per case — catch a
prompt, repair or model regression, or compare two models on the same suite.

    ~/.evi/evals/<name>.toml:

        name = "smoke"
        description = "Sanity checks"

        [[case]]
        name = "math"
        prompt = "What is 2+2? Reply with just the number."
        contains = ["4"]
        not_contains = ["error"]

        [[case]]
        name = "fix-add"
        prompt = "add() in calc.py subtracts. Fix it."
        fixture = "fixtures/calc"           # a folder next to this file
        tool_order = ["read_file", "edit_file"]
        diff_touches_only = ["calc.py"]
        command_succeeds = ["python -m pytest -q test_calc.py"]
        runs = 5
        pass_rate = 0.8

**Answer assertions** (on the run's final text): `contains` (all substrings
present), `not_contains` (none present), `regex` (search matches), `equals`
(exact, after strip); `ignore_case` makes them case-insensitive; `judge` is a
rubric a model grades (PASS / FAIL).

**Behavioral assertions** (on the v1 run report, `evi.headless`):

- `expect_tools` — each must be called; an entry that is itself a list is met
  by any one of its names (``[["search_files", "find_files"]]``);
- `forbid_tools` — none may be called; `tool_order` — these names appear, in
  this order, among the calls (a subsequence);
- `tool_args` — ``[{tool, arg, glob | regex, absent = false, status,
  ignore_case}]``: some call of `tool` has `arg` matching (only `arg` present
  when neither is given); with ``absent = true`` no call may match (no
  run_command whose command matches ``rm *``). Denied calls count as called;
  `status` ("ok", "denied", "error", ...) narrows to calls that ended so;
- `stop_reason` — the report's stop_reason (otherwise a run that stopped with
  "error" or "interrupted" fails);
- `command_succeeds` — commands (a line split like a POSIX shell, or an argv
  list) run AFTER the agent in the case's workspace; each must exit 0. They
  run through `evi.checks.run_check` and must pass the case's implementer
  policy (`evi.policy.Policy.allows_argv`), else the case fails with the
  policy's reason. A bare ``python`` / ``python3`` / ``py`` is the interpreter
  running eVi, as the project checks use it;
- `file_contains` — ``[{path, contains | regex}]`` on the workspace's files
  after the run (bytes as written: CRLF stays CRLF);
- `diff_touches_only` — globs every changed file (the report's
  files_changed) must match; ``[]`` means nothing may change.

**Workspace.** Every run is headless (`evi.headless.run_headless`) under the
implementer policy (`evi.policy.implementer_policy`) in a workspace made for
it and removed afterwards, also on failure or Ctrl+C: ``workspace =
"fixture"`` (default) is a fresh temporary git repository seeded from the
case's `files` (``{path = content}``) and/or `fixture` (a folder relative to
the suite file), one initial commit; ``workspace = "repo"`` is a throwaway
DETACHED worktree of the repository at HEAD (`evi.worktree.add_detached`: no
branch is made), only for a trusted repository. The policy is built with
``trusted = True`` only because the harness made the workspace itself from a
suite it loaded from a trusted place (see Trust); the verify gate follows
``[checks] verify`` as for any policy run, and takes the policy's trust
for the workspace, so it runs in fixtures and repo worktrees alike; each row
still counts the runs it was skipped in (``verify_skipped``: the folder
wasn't trusted, a ChecksSkipped event), so a pass rate is never read as
including a gate that didn't run. A fixture repository ignores
bytecode and tool caches (``.git/info/exclude``), so a model that runs the
tests isn't charged with the ``__pycache__`` they leave. A run never touches
your checkout. The agent gets the tools of the case's `mode` (default
"code") that the policy can allow and that work in the session's folder
(`evi.worktree.confined_tools`), and `max_turns` turns (default 12). A call
to a tool the run doesn't offer never meets `expect_tools`, `tool_order` or
a `tool_args` that must match, and a `fake` script that calls one fails the
run (the script and the case disagree).

**Runs and gates.** `runs` (default 1) runs the case that many times; it
passes when passes / runs >= `pass_rate` (default 1.0). `expected_fail =
true` reports the case but never fails the suite (a known gap: the CRLF case
until the edit-recovery work lands); `tags` label cases for ``evi eval run
--tag / --skip-tag``. A key a case doesn't know is an error (a typo must not
silently pass).

**Fake backend.** ``--backend fake`` replays a case's `fake` script (a list
of turns, ``{text = "..."}`` or ``{tool = "name", args = {...}}``) through the
real Agent loop (`evi.evals_fake.ScriptedClient`): CI checks the harness and
the suite with it. A case without a script, or with a judge rubric, is
skipped under it (reported, never failed).

**Trust.** Suites load from ``~/.evi/evals`` (yours) and from
``<repo>/evals/self`` only when that repository is trusted (`evi.trust`): an
untrusted repository's suites are skipped, with the reason (they would run
commands it provides). ``self`` names all of ``evals/self/*.toml`` as one
suite, ``self/<file>`` one file.

**Reports.** `run_suite` returns a report (`REPORT_SCHEMA`: suite, model,
backend, n, evi version, start and finish, hardware; one row per case: runs,
passes, pass_rate, threshold, passed, skipped, failures, mean turns and
duration, stop_reasons) and `write_report` keeps it under ``<eVi
home>/evals/reports/<suite>/<UTC time>-<model>.json`` with a static,
script-free ``.html`` rendering; failure reasons are redacted
(`evi.redact`). `compare` diffs two reports (or a report against a
baseline); `apply_baseline` fails a case whose pass rate fell below a
baseline's minus a tolerance, also an expected_fail one, and fails the
report for a baseline case that didn't run (skipped, no longer in the
suite, or left out by a ``--tag`` / ``--skip-tag`` filter): dropping,
renaming or tagging away a case must not get past the gate. Only a run
the caller marks partial (``evi eval run --partial``) excuses the cases a
filter left out, and lists them under the report's ``baseline.excluded``.

**Baseline.** ``evals/self/baseline.json`` (`BASELINE_SCHEMA`: model,
quantization, hardware, N and each case's pass rate on the reference model)
is the entry gate for prompt, repair and catalog changes. Only the
director's ``evi eval baseline record`` writes it (it refuses where an eVi
agent may have started it, `evi.attest.agent_parent`, and refuses tag
filters: a baseline is the whole suite), and no agent may: it is in
`evi.policy.HARD_PROTECTED` and `evi.permissions.WRITE_PROTECTED_PATHS`.

**One engine.** ``evi eval run``, the web Evals panel's Run button
(``POST /api/evals/run``) and scheduled eval tasks (`evi.scheduler`) all run
`run_suite` and keep the report with `write_report`, so they give one
verdict; the web and the scheduler run on the configured [llm] backend and
model (`configured_context`), never the fake one.
"""

from __future__ import annotations

import fnmatch
import html
import json
import os
import platform
import re
import shlex
import shutil
import stat
import sys
import tempfile
import time
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Iterable, Mapping, Sequence

import evi.config as config

import tomllib


class EvalError(Exception):
    """A suite file is missing or malformed, or a run can't be set up."""


class SuiteSkipped(EvalError):
    """A suite that is not loaded on purpose (an untrusted repository's)."""


REPORT_SCHEMA_VERSION = 1
BASELINE_SCHEMA_VERSION = 1
REPORT_KIND = "eval-report"
# `evi eval run self`: the repository's own suites, <repo>/evals/self/*.toml.
SELF = "self"
SELF_DIR = ("evals", "self")
BASELINE_NAME = "baseline.json"
WORKSPACES = ("fixture", "repo")
DEFAULT_MAX_TURNS = 12
DEFAULT_MODE = "code"
FAKE_BACKEND = "fake"
# The unattended entry point eval runs are marked as (evi.llm.cli_agent).
ENTRY = "eval"
# A run that stopped so fails unless the case expects that stop_reason.
_FAILED_STOPS = ("error", "interrupted")
# How much of one failure reason (a command's output tail) a report keeps,
# and how many failing runs a row lists.
_MAX_REASON = 600
_MAX_LISTED_RUNS = 10
# Reports `latest_reports` reads at most per suite (so one suite run often,
# a scheduled one, can't push the others out), and the biggest it parses.
_MAX_REPORT_FILES = 500
_MAX_REPORT_BYTES = 8 * 1024 * 1024
_PYTHON = re.compile(r"(?:python(?:3(?:\.\d+)?)?|py)\Z")
_SLUG = re.compile(r"[^A-Za-z0-9._-]+")
# What a fixture repository ignores (.git/info/exclude): what running its
# tests or linters leaves behind is not the model's change.
FIXTURE_EXCLUDES = ("__pycache__/", "*.py[cod]", ".pytest_cache/", ".ruff_cache/",
                    ".mypy_cache/")


# ---- the case schema --------------------------------------------------------


@dataclass
class ToolArgMatch:
    """A `tool_args` entry: a call of `tool` whose `arg` matches."""

    tool: str
    arg: str
    glob: str | None = None
    regex: str | None = None
    absent: bool = False
    status: str | None = None
    ignore_case: bool = False

    def describe(self) -> str:
        if self.glob is not None:
            what = f"glob {self.glob!r}"
        elif self.regex is not None:
            what = f"/{self.regex}/"
        else:
            what = "(present)"
        return what + (f" (status {self.status})" if self.status else "")


@dataclass
class FileMatch:
    """A `file_contains` entry: the workspace file `path` holds `contains`
    (a substring) or matches `regex`."""

    path: str
    contains: str | None = None
    regex: str | None = None


@dataclass
class EvalCase:
    name: str
    prompt: str
    contains: list[str] = field(default_factory=list)
    not_contains: list[str] = field(default_factory=list)
    regex: str = ""
    equals: str | None = None
    ignore_case: bool = False
    mode: str = ""
    judge: str = ""   # LLM-as-judge rubric; graded by a model (needs judge_fn)
    # Behavioral assertions, on the run report (see the module doc).
    expect_tools: list[str | list[str]] = field(default_factory=list)
    forbid_tools: list[str] = field(default_factory=list)
    tool_order: list[str] = field(default_factory=list)
    tool_args: list[ToolArgMatch] = field(default_factory=list)
    stop_reason: str = ""
    command_succeeds: list[tuple[str, ...]] = field(default_factory=list)
    file_contains: list[FileMatch] = field(default_factory=list)
    diff_touches_only: list[str] | None = None
    # The workspace and how often to run.
    files: dict[str, str] = field(default_factory=dict)
    fixture: str = ""
    workspace: str = "fixture"
    max_turns: int = DEFAULT_MAX_TURNS
    runs: int = 1
    pass_rate: float = 1.0
    fake: list[dict[str, Any]] | None = None
    expected_fail: bool = False
    tags: list[str] = field(default_factory=list)
    # The folder of the suite file the case came from (`fixture` is relative
    # to it); None for a case built in code.
    base_dir: Path | None = None


@dataclass
class EvalSuite:
    name: str
    description: str = ""
    cases: list[EvalCase] = field(default_factory=list)
    path: Path | None = None
    # "user" (~/.evi/evals) or "self" (<repo>/evals/self, a trusted repo).
    source: str = "user"
    # The repository a "self" suite belongs to.
    repo: Path | None = None


_CASE_KEYS = frozenset({
    "name", "prompt", "contains", "not_contains", "regex", "equals", "ignore_case", "mode",
    "judge", "expect_tools", "forbid_tools", "tool_order", "tool_args", "stop_reason",
    "command_succeeds", "file_contains", "diff_touches_only", "files", "fixture", "workspace",
    "max_turns", "runs", "pass_rate", "fake", "expected_fail", "tags",
})
_TOOL_ARG_KEYS = frozenset({"tool", "arg", "glob", "regex", "absent", "status", "ignore_case"})
_FILE_KEYS = frozenset({"path", "contains", "regex"})
_FAKE_KEYS = frozenset({"text", "tool", "args"})


def evals_dir(root: Path | None = None) -> Path:
    return (root if root is not None else config.HOME) / "evals"


def reports_dir(root: Path | None = None) -> Path:
    """``<eVi home>/evals/reports``: one folder per suite."""
    return evals_dir(root) / "reports"


def _slug(name: str) -> str:
    return Path(name).name.removesuffix(".toml")


def fs_slug(name: str) -> str:
    """`name` (a suite or model) as one safe file-name part."""
    return _SLUG.sub("-", str(name or "")).strip("-.") or "unnamed"


def _as_list(v: Any) -> list[str]:
    if isinstance(v, list):
        return [str(x) for x in v]
    if v in (None, ""):
        return []
    return [str(v)]


def _where(path: Path, i: int, name: str) -> str:
    return f"{path.name}: case {i} ({name})" if name else f"{path.name}: case {i}"


def _str_list(v: Any, where: str, key: str) -> list[str]:
    if v is None:
        return []
    if isinstance(v, str):
        return [v]
    if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
        raise EvalError(f"{where}: `{key}` must be a list of strings")
    return list(v)


def _bool(v: Any, where: str, key: str) -> bool:
    if not isinstance(v, bool):
        raise EvalError(f"{where}: `{key}` must be true or false")
    return v


def _relative(value: Any, where: str, key: str) -> str:
    """A workspace-relative path as given in a suite: ``/``-separated, no
    absolute path, drive, ``..`` or ``.git`` part."""
    if not isinstance(value, str) or not value.strip():
        raise EvalError(f"{where}: `{key}` needs a non-empty relative path")
    s = value.strip().replace("\\", "/")
    while s.startswith("./"):
        s = s[2:]
    parts = PurePosixPath(s).parts
    if (s.startswith(("/", "~")) or re.match(r"[A-Za-z]:", s) or not parts
            or ".." in parts or any(p.lower() == ".git" for p in parts)):
        raise EvalError(f"{where}: `{key}` {value!r} must stay inside the workspace "
                        "(relative, no '..' or .git)")
    return s


def _argv(command: Any, where: str) -> tuple[str, ...]:
    if isinstance(command, list) and all(isinstance(w, str) for w in command):
        words = tuple(w for w in command if w)
    elif isinstance(command, str):
        try:
            words = tuple(shlex.split(command, posix=True))
        except ValueError as exc:
            raise EvalError(f"{where}: `command_succeeds` {command!r} can't be split: "
                            f"{exc}") from exc
    else:
        raise EvalError(f"{where}: `command_succeeds` entries are command lines or argv lists")
    if not words:
        raise EvalError(f"{where}: `command_succeeds` has an empty command")
    return words


def _tool_args(raw: Any, where: str) -> list[ToolArgMatch]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise EvalError(f"{where}: `tool_args` must be a list of tables")
    out = []
    for entry in raw:
        if not isinstance(entry, dict):
            raise EvalError(f"{where}: each `tool_args` entry is a table {{tool, arg, ...}}")
        unknown = sorted(set(entry) - _TOOL_ARG_KEYS)
        if unknown:
            raise EvalError(f"{where}: unknown key {unknown[0]!r} in `tool_args`")
        tool, arg = entry.get("tool"), entry.get("arg")
        if not isinstance(tool, str) or not tool or not isinstance(arg, str) or not arg:
            raise EvalError(f"{where}: a `tool_args` entry needs `tool` and `arg`")
        if "glob" in entry and "regex" in entry:
            raise EvalError(f"{where}: a `tool_args` entry takes `glob` or `regex`, not both")
        for k in ("glob", "regex", "status"):
            if k in entry and not isinstance(entry[k], str):
                raise EvalError(f"{where}: `tool_args` `{k}` must be a string")
        if "regex" in entry:
            try:
                re.compile(entry["regex"])
            except re.error as exc:
                raise EvalError(f"{where}: bad `tool_args` regex: {exc}") from exc
        out.append(ToolArgMatch(
            tool=tool, arg=arg, glob=entry.get("glob"), regex=entry.get("regex"),
            absent=_bool(entry.get("absent", False), where, "tool_args.absent"),
            status=entry.get("status"),
            ignore_case=_bool(entry.get("ignore_case", False), where, "tool_args.ignore_case"),
        ))
    return out


def _file_contains(raw: Any, where: str) -> list[FileMatch]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise EvalError(f"{where}: `file_contains` must be a list of tables")
    out = []
    for entry in raw:
        if not isinstance(entry, dict):
            raise EvalError(f"{where}: each `file_contains` entry is a table {{path, contains}}")
        unknown = sorted(set(entry) - _FILE_KEYS)
        if unknown:
            raise EvalError(f"{where}: unknown key {unknown[0]!r} in `file_contains`")
        if ("contains" in entry) == ("regex" in entry):
            raise EvalError(f"{where}: a `file_contains` entry takes `contains` or `regex`")
        value = entry.get("contains", entry.get("regex"))
        if not isinstance(value, str):
            raise EvalError(f"{where}: `file_contains` contains / regex must be a string")
        if "regex" in entry:
            try:
                re.compile(value)
            except re.error as exc:
                raise EvalError(f"{where}: bad `file_contains` regex: {exc}") from exc
        out.append(FileMatch(path=_relative(entry.get("path"), where, "file_contains.path"),
                             contains=entry.get("contains"), regex=entry.get("regex")))
    return out


def _fake(raw: Any, where: str) -> list[dict[str, Any]] | None:
    if raw is None:
        return None
    if not isinstance(raw, list):
        raise EvalError(f"{where}: `fake` must be a list of turns")
    out = []
    for turn in raw:
        if not isinstance(turn, dict):
            raise EvalError(f"{where}: each `fake` turn is a table {{text}} or {{tool, args}}")
        unknown = sorted(set(turn) - _FAKE_KEYS)
        if unknown:
            raise EvalError(f"{where}: unknown key {unknown[0]!r} in a `fake` turn")
        if ("text" in turn) == ("tool" in turn):
            raise EvalError(f"{where}: a `fake` turn is either {{text}} or {{tool, args}}")
        if "text" in turn:
            if not isinstance(turn["text"], str) or "args" in turn:
                raise EvalError(f"{where}: a `fake` text turn is {{text = \"...\"}}")
            out.append({"text": turn["text"]})
            continue
        args = turn.get("args", {})
        if not isinstance(turn["tool"], str) or not turn["tool"] or not isinstance(args, dict):
            raise EvalError(f"{where}: a `fake` tool turn is {{tool = \"name\", args = {{...}}}}")
        out.append({"tool": turn["tool"], "args": dict(args)})
    return out


def _expect_tools(raw: Any, where: str) -> list[str | list[str]]:
    if raw is None:
        return []
    if isinstance(raw, str):
        return [raw]
    out: list[str | list[str]] = []
    if isinstance(raw, list):
        for entry in raw:
            if isinstance(entry, str) and entry:
                out.append(entry)
            elif (isinstance(entry, list) and entry
                  and all(isinstance(x, str) and x for x in entry)):
                out.append(list(entry))
            else:
                break
        else:
            return out
    raise EvalError(f"{where}: `expect_tools` is a list of tool names (or lists of "
                    "alternatives)")


def _parse_case(c: Any, i: int, path: Path) -> EvalCase:
    from evi.events import STOP_REASONS

    if not isinstance(c, dict) or not isinstance(c.get("prompt"), str) \
            or not c["prompt"].strip():
        raise EvalError(f"{path.name}: case {i} is missing a non-empty `prompt`")
    name = str(c.get("name") or f"case{i}").strip()
    where = _where(path, i, name)
    unknown = sorted(set(c) - _CASE_KEYS)
    if unknown:
        raise EvalError(f"{where}: unknown key {unknown[0]!r} (a typo would silently pass)")

    def num(key: str, default: Any, cast: type, low: float, high: float | None = None) -> Any:
        value = c.get(key, default)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or (
                cast is int and not isinstance(value, int)):
            raise EvalError(f"{where}: `{key}` must be a number")
        if value < low or (high is not None and value > high):
            bound = f"between {low} and {high}" if high is not None else f"at least {low}"
            raise EvalError(f"{where}: `{key}` must be {bound}")
        return cast(value)

    workspace = str(c.get("workspace", "fixture"))
    if workspace not in WORKSPACES:
        raise EvalError(f"{where}: `workspace` is \"fixture\" or \"repo\"")
    stop = c.get("stop_reason", "")
    if not isinstance(stop, str) or (stop and stop not in STOP_REASONS):
        raise EvalError(f"{where}: `stop_reason` must be one of {', '.join(STOP_REASONS)}")
    files = c.get("files", {})
    if not isinstance(files, dict) or not all(isinstance(v, str) for v in files.values()):
        raise EvalError(f"{where}: `files` is a table {{path = \"content\"}}")
    fixture = c.get("fixture", "")
    if not isinstance(fixture, str):
        raise EvalError(f"{where}: `fixture` is a folder path")
    if fixture:
        fixture = _relative(fixture, where, "fixture")
        folder = (path.parent / fixture).resolve()
        if not folder.is_relative_to(path.parent.resolve()) or not folder.is_dir():
            raise EvalError(f"{where}: `fixture` {fixture!r} is not a folder next to {path.name}")
    if workspace == "repo" and (files or fixture):
        raise EvalError(f"{where}: `files` / `fixture` seed a fixture workspace, not \"repo\"")
    pass_rate = num("pass_rate", 1.0, float, 0.0, 1.0)
    if pass_rate <= 0:
        raise EvalError(f"{where}: `pass_rate` must be above 0")
    diff_only = c.get("diff_touches_only")
    for key in ("regex",):
        if c.get(key):
            try:
                re.compile(str(c[key]))
            except re.error as exc:
                raise EvalError(f"{where}: bad `{key}`: {exc}") from exc
    return EvalCase(
        name=name,
        prompt=c["prompt"].strip(),
        contains=_as_list(c.get("contains")),
        not_contains=_as_list(c.get("not_contains")),
        regex=str(c.get("regex", "")),
        equals=(str(c["equals"]) if "equals" in c else None),
        ignore_case=bool(c.get("ignore_case", False)),
        mode=str(c.get("mode", "")).strip(),
        judge=str(c.get("judge", "")).strip(),
        expect_tools=_expect_tools(c.get("expect_tools"), where),
        forbid_tools=_str_list(c.get("forbid_tools"), where, "forbid_tools"),
        tool_order=_str_list(c.get("tool_order"), where, "tool_order"),
        tool_args=_tool_args(c.get("tool_args"), where),
        stop_reason=stop,
        command_succeeds=[_argv(x, where) for x in _command_list(c.get("command_succeeds"),
                                                                where)],
        file_contains=_file_contains(c.get("file_contains"), where),
        diff_touches_only=(None if diff_only is None
                           else _str_list(diff_only, where, "diff_touches_only")),
        files={_relative(k, where, "files"): v for k, v in files.items()},
        fixture=fixture,
        workspace=workspace,
        max_turns=num("max_turns", DEFAULT_MAX_TURNS, int, 1),
        runs=num("runs", 1, int, 1),
        pass_rate=pass_rate,
        fake=_fake(c.get("fake"), where),
        expected_fail=_bool(c.get("expected_fail", False), where, "expected_fail"),
        tags=_str_list(c.get("tags"), where, "tags"),
        base_dir=path.parent,
    )


def _command_list(raw: Any, where: str) -> list[Any]:
    if raw is None:
        return []
    if isinstance(raw, str):
        return [raw]
    if not isinstance(raw, list):
        raise EvalError(f"{where}: `command_succeeds` is a list of commands")
    return list(raw)


def load_suite_file(path: Path) -> EvalSuite:
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise EvalError(f"could not read {path.name}: {exc}") from exc

    raw = data.get("case", [])
    if not isinstance(raw, list) or not raw:
        raise EvalError(f"{path.name}: a suite needs at least one [[case]]")
    cases = [_parse_case(c, i, path) for i, c in enumerate(raw, 1)]
    _no_duplicates(cases, path.name)
    return EvalSuite(
        name=str(data.get("name") or path.stem).strip(),
        description=str(data.get("description", "")).strip(),
        cases=cases,
        path=path,
    )


def _no_duplicates(cases: Sequence[EvalCase], where: str) -> None:
    seen: set[str] = set()
    for case in cases:
        if case.name in seen:
            raise EvalError(f"{where}: two cases are named {case.name!r} (reports key on it)")
        seen.add(case.name)


def load_suite(name: str, root: Path | None = None) -> EvalSuite:
    path = evals_dir(root) / f"{_slug(name)}.toml"
    if not path.is_file():
        raise EvalError(f"no eval suite named {_slug(name)!r} (looked in {evals_dir(root)})")
    return load_suite_file(path)


def shadowed(suite: EvalSuite) -> bool:
    """Whether `suite`, one of yours, can't be run by its file name: a
    ``self.toml`` (`evi eval run self` means the repository's evals/self)."""
    return suite.source == "user" and suite.path is not None and suite.path.stem == SELF


def list_suites(root: Path | None = None,
                problems: list[tuple[Path, str]] | None = None) -> list[EvalSuite]:
    """Your suites (``~/.evi/evals/*.toml``). One that doesn't load is left
    out, and added to `problems` (path, why) when given. A ``self.toml`` is
    listed but can't be run under that name (`shadowed`)."""
    d = evals_dir(root)
    out: list[EvalSuite] = []
    if d.is_dir():
        for p in sorted(d.glob("*.toml")):
            try:
                out.append(load_suite_file(p))
            except EvalError as exc:
                if problems is not None:
                    problems.append((p, str(exc)))
                continue
    return out


# ---- the repository's own suites (evals/self) ----------------------------------


def find_repo(start: Path | None = None) -> Path | None:
    """The top of the git work tree `start` (default cwd) is in, or None."""
    from evi.project import git_toplevel

    try:
        top = git_toplevel(start)
    except OSError:
        return None
    return Path(top).resolve() if top is not None else None


def self_dir(repo: Path) -> Path:
    return Path(repo).joinpath(*SELF_DIR)


def self_untrusted(repo: Path) -> str | None:
    """Why `repo`'s evals/self suites are skipped (it is not trusted), or
    None when they may load."""
    try:
        from evi import trust

        ok = bool(trust.status(Path(repo)).trusted)
    except Exception:  # noqa: BLE001 — fail closed
        ok = False
    if ok:
        return None
    return (f"{repo} is not a trusted folder, so its evals/self suites are skipped: they "
            "would run commands the repository provides (`evi trust` it first)")


def is_self_name(name: str) -> bool:
    n = str(name or "").strip().replace("\\", "/")
    return n == SELF or n.startswith(SELF + "/")


def load_self_suite(name: str, repo: Path) -> EvalSuite:
    """``self`` (every ``<repo>/evals/self/*.toml`` as one suite) or
    ``self/<file>`` (one file). SuiteSkipped for an untrusted repository."""
    why = self_untrusted(repo)
    if why:
        raise SuiteSkipped(why)
    folder = self_dir(repo)
    n = str(name).strip().replace("\\", "/")
    if n == SELF:
        files = sorted(folder.glob("*.toml")) if folder.is_dir() else []
        if not files:
            raise EvalError(f"no eval suites in {folder}")
        suites = [load_suite_file(p) for p in files]
        cases = [c for s in suites for c in s.cases]
        _no_duplicates(cases, str(folder))
        return EvalSuite(name=SELF, description="the repository's own suites (evals/self)",
                         cases=cases, path=folder, source=SELF, repo=Path(repo))
    stem = n[len(SELF) + 1:].removesuffix(".toml")
    path = folder / f"{stem}.toml"
    if not stem or "/" in stem or not path.is_file():
        raise EvalError(f"no eval suite {n!r} (looked for {path})")
    suite = load_suite_file(path)
    return replace(suite, name=f"{SELF}/{stem}", source=SELF, repo=Path(repo))


def resolve_suite(name: str, *, root: Path | None = None,
                  start: Path | None = None) -> EvalSuite:
    """The suite `name` names: ``self`` / ``self/<file>`` in the repository
    at `start` (default cwd), else one of yours (`load_suite`)."""
    if is_self_name(name):
        repo = find_repo(start)
        if repo is None:
            raise EvalError("not inside a git repository, so there is no evals/self suite")
        return load_self_suite(name, repo)
    return load_suite(name, root)


# ---- answer assertions and the judge ---------------------------------------------


def check_case(case: EvalCase, output: str) -> tuple[bool, list[str]]:
    """Return (passed, failure_reasons) for one case's answer assertions
    against the run's final text."""
    fails: list[str] = []
    hay = output.lower() if case.ignore_case else output

    def norm(s: str) -> str:
        return s.lower() if case.ignore_case else s

    for sub in case.contains:
        if norm(sub) not in hay:
            fails.append(f"missing {sub!r}")
    for sub in case.not_contains:
        if norm(sub) in hay:
            fails.append(f"unexpected {sub!r}")
    if case.regex:
        if not re.search(case.regex, output, re.I if case.ignore_case else 0):
            fails.append(f"no match for /{case.regex}/")
    if case.equals is not None:
        a, b = output.strip(), case.equals.strip()
        if case.ignore_case:
            a, b = a.lower(), b.lower()
        if a != b:
            fails.append("output != equals")
    return (not fails, fails)


def _judge_prompt(case: EvalCase, output: str) -> str:
    return (
        "Grade the ANSWER against the RUBRIC. Reply with exactly PASS or FAIL "
        "on the first line, then a one-line reason.\n\n"
        f"RUBRIC: {case.judge}\n\nANSWER:\n{output}"
    )


def make_judge(agent_factory: Callable[[], Any]) -> Callable[[EvalCase, str], tuple[bool, str]]:
    """A `judge` rubric grader: a fresh agent from `agent_factory()` with no
    tools answers PASS or FAIL."""
    from evi.headless import run_headless

    def judge_fn(case: EvalCase, output: str) -> tuple[bool, str]:
        agent = agent_factory()
        agent.tools = {}  # the grader answers from text alone, no tools
        res = run_headless(agent, _judge_prompt(case, output))
        text = (res.text or "").strip()
        first = text.splitlines()[0] if text else ""
        return first.strip().upper().startswith("PASS"), (first[:200] or "no judge output")

    return judge_fn


# ---- workspaces --------------------------------------------------------------------


def _force_remove(func: Callable[..., Any], path: str, _exc: BaseException) -> None:
    """shutil.rmtree's onexc: git keeps its objects read-only (Windows won't
    delete those), so make the file writable and try once more."""
    try:
        os.chmod(path, stat.S_IWRITE | stat.S_IREAD)
        func(path)
    except OSError:
        pass


def remove_tree(path: Path) -> None:
    """Delete `path` (a workspace eVi made), read-only files too. Best effort."""
    if os.path.lexists(path):
        shutil.rmtree(path, onexc=_force_remove)


def _inside(root: Path, rel: str) -> Path | None:
    """`rel` (workspace-relative) under `root`, or None when it would leave it."""
    try:
        p = (root / rel).resolve()
    except (OSError, RuntimeError, ValueError):
        return None
    return p if p != root and p.is_relative_to(root) else None


def _git(folder: Path, *args: str) -> None:
    from evi import gitsafe

    try:
        proc = gitsafe.run_git(list(args), cwd=folder, timeout=120)
    except (OSError, gitsafe.GitNotFoundError, gitsafe.UnsafeRepoError) as exc:
        raise EvalError(f"git {args[0]} failed in the workspace: {exc}") from exc
    except Exception as exc:  # noqa: BLE001 — a timeout
        raise EvalError(f"git {args[0]} failed in the workspace: {exc}") from exc
    if proc.returncode != 0:
        why = (proc.stderr or proc.stdout or "").strip()[:300]
        raise EvalError(f"git {' '.join(args[:2])} failed in the workspace: {why}")


def fixture_source(case: EvalCase) -> Path | None:
    """The folder a case's `fixture` names (relative to its suite file)."""
    if not case.fixture:
        return None
    if case.base_dir is None:
        raise EvalError(f"case {case.name}: `fixture` needs the suite file's folder")
    base = Path(case.base_dir).resolve()
    folder = (base / case.fixture).resolve()
    if not folder.is_relative_to(base) or not folder.is_dir():
        raise EvalError(f"case {case.name}: `fixture` {case.fixture!r} is not a folder "
                        f"under {base}")
    return folder


def make_fixture(case: EvalCase, dest: Path) -> Path:
    """A fresh git repository at `dest` holding the case's `fixture` folder
    and `files` (written byte for byte), in one commit. Line endings are
    kept as they are (``core.autocrlf = false`` in the repository), and the
    repository ignores bytecode and tool caches (`FIXTURE_EXCLUDES`, in
    ``.git/info/exclude``, so the tree itself stays as the suite wrote it)."""
    dest = Path(dest)
    dest.mkdir(parents=True)
    src = fixture_source(case)
    if src is not None:
        for p in src.rglob("*"):
            if p.is_symlink() or os.path.isjunction(p):
                raise EvalError(f"case {case.name}: the fixture holds a link ({p}); "
                                "a fixture is plain files")
        shutil.copytree(src, dest, dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns(".git", "__pycache__"))
    root = dest.resolve()
    for rel, content in case.files.items():
        target = _inside(root, rel)
        if target is None:
            raise EvalError(f"case {case.name}: `files` entry {rel!r} leaves the workspace")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content.encode("utf-8"))
    _git(root, "init", "-q")
    _git(root, "config", "core.autocrlf", "false")
    _git(root, "config", "core.safecrlf", "false")
    info = root / ".git" / "info"
    try:
        info.mkdir(parents=True, exist_ok=True)
        with open(info / "exclude", "a", encoding="utf-8", newline="\n") as fh:
            fh.write("\n# eVi evals: what running the tests leaves behind\n"
                     + "\n".join(FIXTURE_EXCLUDES) + "\n")
    except OSError as exc:
        raise EvalError(f"case {case.name}: the fixture's ignore rules could not be "
                        f"written: {exc}") from exc
    _git(root, "add", "-A")
    _git(root, "-c", "user.name=eVi evals", "-c", "user.email=evals@evi.invalid",
         "-c", "commit.gpgsign=false", "commit", "-q", "--allow-empty", "--no-verify",
         "-m", f"eval fixture: {case.name}")
    return root


# ---- running a case -------------------------------------------------------------


def eval_tools(mode: str = "") -> list:
    """The tools an eval run gets: those of `mode` (default "code") that
    the implementer policy can allow (`evi.policy`) and that work in the
    session's folder, not eVi's process folder (`evi.worktree.confined_tools`),
    sorted by name so every run offers the same list."""
    from evi.modes import mode_tools
    from evi.policy import PURE_TOOLS, READ_TOOLS, SHELL_TOOLS, WRITE_TOOLS
    from evi.tools import register_builtin_tools
    from evi.worktree import confined_tools

    register_builtin_tools()
    known = {**READ_TOOLS, **WRITE_TOOLS, **SHELL_TOOLS, **PURE_TOOLS}
    tools = [t for t in mode_tools(mode or DEFAULT_MODE) if known.get(t.name) == t.category]
    return sorted(confined_tools(tools), key=lambda t: t.name)


@dataclass
class RunOutcome:
    """One run of a case."""

    passed: bool
    failures: list[str] = field(default_factory=list)
    turns: int = 0
    duration_s: float = 0.0
    stop_reason: str | None = None
    # The agent's verify gate was skipped (ChecksSkipped: neither the run's
    # policy nor evi.trust trusted the folder).
    verify_skipped: bool = False


AgentFactory = Callable[[EvalCase, Path], Any]
Judge = Callable[[EvalCase, str], tuple[bool, str]]


@dataclass
class EvalContext:
    """What running a suite needs besides the suite.

    - make_agent(case, workspace): a fresh Agent for one run (its client is
      the model under test, or the fake script); the harness then holds it
      to the workspace and the implementer policy (`hold`).
    - backend / model: what the report says ran ("fake" for the scripted
      model).
    - judge: the `judge` rubric grader (None: a judged case fails, or is
      skipped under the fake backend).
    - repo: the repository a ``workspace = "repo"`` case gets a worktree of.
    - default_mode: the tool preset of a case without `mode`.
    - tmp_root: where workspaces are made (default: the system temp folder).
    """

    make_agent: AgentFactory
    backend: str = ""
    model: str = ""
    judge: Judge | None = None
    repo: Path | None = None
    default_mode: str = ""
    tmp_root: Path | None = None

    @property
    def fake(self) -> bool:
        return self.backend == FAKE_BACKEND


def skip_reason(case: EvalCase, ctx: EvalContext) -> str | None:
    """Why `case` doesn't run in `ctx` (reported as skipped), or None."""
    if ctx.fake and case.fake is None:
        return "no fake script (the fake backend replays a case's `fake` turns)"
    if ctx.fake and case.judge:
        return "a judge rubric needs a model, not the fake backend"
    if case.workspace == "repo":
        if ctx.repo is None:
            return "workspace = \"repo\" needs a git repository to make a worktree of"
        if self_untrusted(ctx.repo):
            return (f"{ctx.repo} is not a trusted folder, so no worktree of it is made: the "
                    "case's commands would run its code (`evi trust` it first)")
    return None


def hold(agent: Any, case: EvalCase, workspace: Path, default_mode: str = "") -> Any:
    """Hold `agent` to the run: the workspace as its folder, nobody to ask,
    the eval tools (`eval_tools`) and the implementer policy with the
    project's check commands allowed (trusted: the harness made the
    workspace). Returns the policy. EvalError for a CLI-agent backend, which
    runs its own tools outside any policy."""
    from evi import checks, policy
    from evi.llm.agent import make_unattended
    from evi.llm.cli_agent import DELEGATE_BACKENDS

    cfg = getattr(agent, "config", None)
    kind = str(getattr(getattr(cfg, "llm", None), "backend", "") or "").strip().lower()
    if kind in DELEGATE_BACKENDS or getattr(getattr(agent, "client", None), "delegate", False):
        raise EvalError(f"the {kind or 'configured'} backend is a CLI agent that runs its own "
                        "tools, outside the implementer policy an eval runs under; use a "
                        "local or API backend")
    agent.cwd = str(workspace)
    make_unattended(agent)
    if hasattr(agent, "auto_all"):
        agent.auto_all = False
    agent.tools = {t.name: t for t in eval_tools(case.mode or default_mode)}
    allowed = checks.allowlist(workspace, cfg, True) if cfg is not None else []
    pol = policy.implementer_policy(workspace, checks=allowed, trusted=True)
    agent.policy = pol
    return pol


def _json_object(text: Any) -> dict[str, Any]:
    if isinstance(text, dict):
        return dict(text)
    try:
        data = json.loads(text) if isinstance(text, str) else None
    except (json.JSONDecodeError, RecursionError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def report_calls(tool_calls: Sequence[Mapping[str, Any]],
                 raw_args: Sequence[str] = ()) -> list[dict[str, Any]]:
    """The run report's tool calls as the assertions see them: name, status
    and arguments (a dict). `raw_args` are the calls' own argument texts in
    call order (the report keeps them redacted and cut short); a report
    entry with no arguments is a result with no call."""
    raw = iter(raw_args)
    out = []
    for rec in tool_calls:
        args: dict[str, Any] = {}
        if rec.get("args") is not None:
            text = next(raw, None)
            args = _json_object(text if text is not None else rec.get("args"))
        out.append({"name": str(rec.get("name") or ""), "status": rec.get("status"),
                    "args": args})
    return out


def _redact(text: str) -> str:
    """`text` (a failure reason: a call's arguments, a command's output) with
    its secrets replaced (`evi.redact`), before a report keeps it."""
    from evi.redact import Redactor

    try:
        return Redactor()(text)
    except Exception:  # noqa: BLE001 — never keep what couldn't be checked
        return "[a failure reason that could not be redacted]"


def _subsequence(want: Sequence[str], got: Sequence[str]) -> bool:
    it = iter(got)
    return all(any(g == w for g in it) for w in want)


def _arg_text(value: Any) -> str:
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)


def arg_matches(m: ToolArgMatch, args: Mapping[str, Any]) -> bool:
    """Whether a call with `args` meets `m` (its `arg` present and matching)."""
    if m.arg not in args:
        return False
    text = _arg_text(args[m.arg]).strip()
    if m.glob is not None:
        a, b = (text.lower(), m.glob.lower()) if m.ignore_case else (text, m.glob)
        return fnmatch.fnmatchcase(a, b)
    if m.regex is not None:
        return re.search(m.regex, text, re.I if m.ignore_case else 0) is not None
    return True


def check_tools(case: EvalCase, calls: Sequence[Mapping[str, Any]],
                offered: Iterable[str] | None = None) -> list[str]:
    """Failures of the case's tool assertions against `calls` (`report_calls`).

    `offered` (the tools the run offered; None: no filter) keeps a call to
    any other tool (it came back an error) from meeting `expect_tools`,
    `tool_order` or a `tool_args` that must match; it still counts against
    `forbid_tools` and an ``absent`` `tool_args` (the model tried)."""
    fails: list[str] = []
    names = [str(c.get("name") or "") for c in calls]
    called = set(names)
    known = None if offered is None else set(offered)
    real = calls if known is None else [c for c in calls if c.get("name") in known]
    real_names = [str(c.get("name") or "") for c in real]
    stray = sorted(called - set(real_names))

    def unoffered(tools: Iterable[str]) -> str:
        hit = [t for t in tools if t in stray]
        return f" ({', '.join(hit)} called, but the run doesn't offer it)" if hit else ""

    for want in case.expect_tools:
        alts = [want] if isinstance(want, str) else list(want)
        if not set(real_names).intersection(alts):
            fails.append(f"expected a call to {' or '.join(alts)}{unoffered(alts)}")
    for bad in case.forbid_tools:
        if bad in called:
            statuses = sorted({str(c.get("status")) for c in calls if c.get("name") == bad})
            fails.append(f"called the forbidden tool {bad} ({names.count(bad)}x, "
                         f"{', '.join(statuses)})")
    if case.tool_order and not _subsequence(case.tool_order, real_names):
        fails.append(f"tools not called in the order {' -> '.join(case.tool_order)} "
                     f"(called: {' -> '.join(names) or 'none'}){unoffered(case.tool_order)}")
    for m in case.tool_args:
        hits = [c for c in (calls if m.absent else real) if c.get("name") == m.tool
                and (m.status is None or c.get("status") == m.status)
                and arg_matches(m, c.get("args") or {})]
        if m.absent and hits:
            value = _redact(_arg_text((hits[0].get("args") or {}).get(m.arg)))[:200]
            fails.append(f"{m.tool} was called with {m.arg}={value!r}, matching "
                         f"{m.describe()}")
        elif not m.absent and not hits:
            fails.append(f"no {m.tool} call with {m.arg} matching {m.describe()}"
                         f"{unoffered([m.tool])}")
    return fails


def check_files(case: EvalCase, workspace: Path,
                files_changed: Sequence[Mapping[str, Any]] | None) -> list[str]:
    """Failures of `file_contains` and `diff_touches_only` after the run."""
    fails: list[str] = []
    root = Path(workspace).resolve()
    for fm in case.file_contains:
        p = _inside(root, fm.path)
        if p is None or not p.is_file():
            fails.append(f"{fm.path} does not exist")
            continue
        try:
            text = p.read_bytes().decode("utf-8", errors="replace")
        except OSError as exc:
            fails.append(f"{fm.path} can't be read: {exc}")
            continue
        if fm.contains is not None and fm.contains not in text:
            fails.append(f"{fm.path} does not contain {fm.contains!r}")
        if fm.regex is not None and re.search(fm.regex, text) is None:
            fails.append(f"{fm.path} has no match for /{fm.regex}/")
    if case.diff_touches_only is not None:
        if files_changed is None:
            fails.append("the files the run changed could not be listed (diff_touches_only)")
        else:
            stray = [str(f.get("path")) for f in files_changed
                     if not any(fnmatch.fnmatchcase(str(f.get("path")), g)
                                for g in case.diff_touches_only)]
            if stray:
                allowed = ", ".join(case.diff_touches_only) or "nothing"
                fails.append(f"changed {', '.join(stray[:10])}, outside {allowed}")
    return fails


def command_argv(argv: Sequence[str]) -> list[str]:
    """A `command_succeeds` command as run: a bare python / python3 / py is
    the interpreter running eVi (`sys.executable`), as the checks use it."""
    from evi import shellsplit

    words = [str(w) for w in argv]
    if words and not re.search(r"[\\/]", words[0]) and _PYTHON.match(
            shellsplit._base(words[0])) and sys.executable:
        words[0] = sys.executable
    return words


def run_command_gates(case: EvalCase, workspace: Path, pol: Any,
                      timeout: float | None = None) -> list[str]:
    """Run the case's `command_succeeds` commands in `workspace`, each first
    checked by the policy `pol` (`Policy.allows_argv`); the failures."""
    from evi import checks

    fails: list[str] = []
    for argv in case.command_succeeds:
        shown = checks.display(argv)
        words = command_argv(argv)
        verdict = pol.allows_argv(words, run_in=Path(workspace))
        if not verdict.allowed:
            fails.append(_redact(f"command_succeeds `{shown}` is refused by the {pol.name} "
                                 f"policy: {verdict.reason}")[:_MAX_REASON])
            continue
        check = checks.Check(name=Path(words[0]).name or words[0], argv=tuple(words),
                             kind="test", source="eval")
        run = checks.run_check(check, workspace, timeout=timeout or checks.DEFAULT_TIMEOUT,
                               tail_lines=20)
        if run.exit != 0:
            status = f"exit {run.exit}" if run.exit is not None else (run.note or "did not run")
            fails.append(_redact(f"command_succeeds `{shown}`: {status}\n{run.tail}")
                         [:_MAX_REASON])
    return fails


def evaluate(case: EvalCase, res: Any, workspace: Path, pol: Any, *,
             raw_args: Sequence[str] = (), judge: Judge | None = None,
             timeout: float | None = None, offered: Iterable[str] | None = None) -> list[str]:
    """Every assertion of `case` against a finished run: its report `res`
    (an `evi.headless.HeadlessResult`), its workspace and policy (`offered`:
    the tools the run offered, `check_tools`). The failures, redacted; []
    when it passed."""
    output = res.text or (f"ERROR: {res.error}" if res.error else "")
    _, fails = check_case(case, output)
    if case.judge:
        if judge is None:
            fails.append("judge rubric set but no grader available")
        else:
            jok, reason = judge(case, output)
            if not jok:
                fails.append(f"judge: {reason}")
    if case.stop_reason:
        if res.stop_reason != case.stop_reason:
            fails.append(f"stopped with {res.stop_reason!r}, expected {case.stop_reason!r}")
    elif res.stop_reason in _FAILED_STOPS:
        fails.append(f"the run stopped with {res.stop_reason}"
                     + (f": {res.error}" if res.error else ""))
    fails += check_tools(case, report_calls(res.tool_calls, raw_args), offered)
    fails += check_files(case, workspace, res.files_changed)
    fails += run_command_gates(case, workspace, pol, timeout)
    return [_redact(f)[:_MAX_REASON] for f in fails]


def run_once(case: EvalCase, ctx: EvalContext) -> RunOutcome:
    """One run of `case`: make its workspace, run the agent there under the
    implementer policy, check every assertion, and remove the workspace
    (also when something fails or the run is interrupted). EvalError only
    for a backend no policy can hold (a CLI agent, as the [llm] backend or a
    ``[models] *_backend``: `evi.llm.cli_agent.check_unattended`)."""
    from evi import checks
    from evi import worktree as _wt
    from evi.headless import run_headless
    from evi.llm.agent import ChecksSkipped, ToolCall
    from evi.llm.cli_agent import CliUnavailable, check_unattended, unattended

    started = time.monotonic()

    def failed(why: str) -> RunOutcome:
        return RunOutcome(False, [_redact(why)[:_MAX_REASON]],
                          duration_s=round(time.monotonic() - started, 3))

    parent = Path(tempfile.mkdtemp(prefix="evi-eval-", dir=ctx.tmp_root))
    workspace = parent / ("repo" if case.workspace == "repo" else "ws")
    added = False
    try:
        try:
            if case.workspace == "repo":
                _wt.add_detached(workspace, start=ctx.repo)
                added = True
                workspace = workspace.resolve()
            else:
                workspace = make_fixture(case, workspace)
        except (OSError, EvalError, _wt.WorktreeError) as exc:
            return failed(f"the workspace could not be made: {exc}")
        try:
            agent = ctx.make_agent(case, workspace)
            pol = hold(agent, case, workspace, ctx.default_mode)
        except EvalError:
            raise
        except Exception as exc:  # noqa: BLE001 — a failed build is the run's failure
            return failed(f"the agent could not be built: {type(exc).__name__}: {exc}")
        cfg = getattr(agent, "config", None)
        if not ctx.fake and cfg is not None:
            # A delegate CLI agent anywhere in the config (a [models] backend
            # a tool builds on its own) is refused up front: a usage error,
            # not one failed run per case.
            try:
                check_unattended(cfg, ENTRY)
            except CliUnavailable as exc:
                raise EvalError(str(exc)) from exc
        offered = set(getattr(agent, "tools", None) or ())
        unscripted = sorted({str(t["tool"]) for t in case.fake or [] if "tool" in t} - offered)
        if ctx.fake and unscripted:
            return failed(f"the fake script calls {', '.join(unscripted)}, which this run "
                          "doesn't offer: the script and the case disagree")
        raw_args: list[str] = []
        verify_skipped = False

        def on_event(event: Any) -> None:
            nonlocal verify_skipped
            if isinstance(event, ToolCall):
                raw_args.append(event.arguments)
            elif isinstance(event, ChecksSkipped):
                verify_skipped = True

        with unattended(ENTRY, None if ctx.fake else cfg):
            res = run_headless(agent, case.prompt, max_turns=case.max_turns, on_event=on_event,
                               track_files=workspace, report_errors=True)
        fails = evaluate(case, res, workspace, pol, raw_args=raw_args, judge=ctx.judge,
                         timeout=checks.settings(cfg).timeout, offered=offered)
        client = getattr(agent, "client", None)
        if getattr(client, "exhausted", False):
            fails.append(f"the fake script ran out after {client.scripted} turn(s): the "
                         "script and the case disagree")
        return RunOutcome(not fails, fails, turns=res.turns_used,
                          duration_s=round(time.monotonic() - started, 3),
                          stop_reason=res.stop_reason, verify_skipped=verify_skipped)
    finally:
        if added:
            _wt.remove_detached(workspace, start=ctx.repo)
        remove_tree(parent)


def case_row(case: EvalCase, outcomes: Sequence[RunOutcome],
             skipped: str | None = None) -> dict[str, Any]:
    """The report's row for `case` from its runs (none when `skipped`)."""
    row: dict[str, Any] = {
        "name": case.name, "runs": len(outcomes), "passes": 0, "pass_rate": None,
        "threshold": case.pass_rate, "passed": False, "skipped": skipped is not None,
        "skip_reason": skipped, "expected_fail": case.expected_fail, "failures": [],
        "mean_turns": None, "mean_duration_s": None, "stop_reasons": {},
        "tags": list(case.tags), "baseline": None, "verify_skipped": 0,
    }
    if skipped is not None or not outcomes:
        return row
    n = len(outcomes)
    passes = sum(1 for o in outcomes if o.passed)
    rate = passes / n
    stops: dict[str, int] = {}
    for o in outcomes:
        key = o.stop_reason or "none"
        stops[key] = stops.get(key, 0) + 1
    row.update(
        passes=passes, pass_rate=round(rate, 4), passed=rate + 1e-9 >= case.pass_rate,
        failures=[f"run {i}: {'; '.join(o.failures[:3])}"[:_MAX_REASON]
                  for i, o in enumerate(outcomes, 1) if not o.passed][:_MAX_LISTED_RUNS],
        mean_turns=round(sum(o.turns for o in outcomes) / n, 2),
        mean_duration_s=round(sum(o.duration_s for o in outcomes) / n, 3),
        stop_reasons=stops,
        verify_skipped=sum(1 for o in outcomes if o.verify_skipped),
    )
    return row


def run_case(case: EvalCase, ctx: EvalContext, runs: int | None = None) -> dict[str, Any]:
    """Run `case` `runs` times (default its own `runs`) and return its row."""
    why = skip_reason(case, ctx)
    if why is not None:
        return case_row(case, [], skipped=why)
    n = max(1, int(runs or case.runs))
    return case_row(case, [run_once(case, ctx) for _ in range(n)])


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def hardware_summary(**overrides: Any) -> dict[str, Any]:
    """{gpu, vram_gb, cpu, ram_gb, os} of this machine, best effort (psutil,
    nvidia-smi when present, `evi.hardware`); a value given in `overrides`
    (not None or "") wins."""
    hw: dict[str, Any] = {"gpu": "none", "vram_gb": None, "cpu": "", "ram_gb": None,
                          "os": platform.platform()}
    try:
        from evi import hardware

        gpus = hardware.detect_gpus()
        if gpus:
            g = max(gpus, key=lambda x: x.vram_total_mb)
            hw["gpu"], hw["vram_gb"] = g.name, round(g.vram_total_mb / 1024, 1)
        ram = hardware.detect_ram_bytes()
        if ram:
            hw["ram_gb"] = round(ram / 1024 ** 3, 1)
    except Exception:  # noqa: BLE001 — a summary without them beats no report
        pass
    hw["cpu"] = platform.processor() or platform.machine() or "unknown"
    for key, value in overrides.items():
        if key in hw and value not in (None, ""):
            hw[key] = value
    return hw


def summarize(report: dict[str, Any]) -> dict[str, Any]:
    """Recount `report`'s totals from its rows (after `apply_baseline`)."""
    rows = report.get("cases") or []
    ran = [r for r in rows if not r.get("skipped")]
    passed = [r for r in ran if r.get("passed")]

    def excused(r: Mapping[str, Any]) -> bool:
        # An expected failure, unless it fell below the baseline too.
        return bool(r.get("expected_fail")) and not (r.get("baseline") or {}).get("regressed")

    report.update(
        total=len(rows), ran=len(ran), passed=len(passed), skipped=len(rows) - len(ran),
        failed=sum(1 for r in ran if not r.get("passed") and not excused(r)),
        expected_failures=sum(1 for r in ran if not r.get("passed") and excused(r)),
        pass_rate=round(len(passed) / len(ran), 4) if ran else 0.0,
    )
    return report


def report_ok(report: Mapping[str, Any]) -> bool:
    """Every case that ran passed, but an expected failure (exit 0), and
    nothing fell below the baseline the report was held to (`apply_baseline`:
    a case whose pass rate fell, expected_fail or not, or a baseline case
    that didn't run)."""
    base = report.get("baseline")
    if isinstance(base, Mapping) and base.get("missing"):
        return False
    return all((r.get("passed") or r.get("expected_fail") or r.get("skipped"))
               and not (r.get("baseline") or {}).get("regressed")
               for r in report.get("cases") or [])


def run_suite(
    suite: EvalSuite,
    ctx: EvalContext,
    *,
    runs: int | None = None,
    hardware: Mapping[str, Any] | None = None,
    on_case: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Run every case of `suite` (`run_case`; `runs` overrides each case's
    own) and return the report (`REPORT_SCHEMA`). `on_case(row)` is called
    as each case finishes. `hardware` (default `hardware_summary()`) is
    the report's machine."""
    from evi import __version__

    started_at, started = _now(), time.monotonic()
    rows = []
    for case in suite.cases:
        row = run_case(case, ctx, runs)
        rows.append(row)
        if on_case is not None:
            on_case(row)
    report: dict[str, Any] = {
        "schema_version": REPORT_SCHEMA_VERSION, "kind": REPORT_KIND,
        "suite": suite.name, "model": ctx.model or "unknown", "backend": ctx.backend or "unknown",
        "n": int(runs) if runs else None, "evi_version": __version__,
        "started_at": started_at, "finished_at": _now(),
        "duration_s": round(time.monotonic() - started, 3),
        "hardware": dict(hardware) if hardware is not None else hardware_summary(),
        "baseline": None, "cases": rows,
    }
    return summarize(report)


# ---- report and baseline schemas ------------------------------------------------

_HARDWARE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["gpu", "vram_gb", "cpu", "ram_gb", "os"],
    "properties": {
        "gpu": {"type": "string"},
        "vram_gb": {"type": ["number", "null"], "minimum": 0},
        "cpu": {"type": "string"},
        "ram_gb": {"type": ["number", "null"], "minimum": 0},
        "os": {"type": "string"},
    },
}
_RATE = {"type": "number", "minimum": 0, "maximum": 1}
_TIME = {"type": "string", "pattern": r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$"}

REPORT_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "eVi eval report",
    "type": "object",
    "required": ["schema_version", "kind", "suite", "model", "backend", "n", "evi_version",
                 "started_at", "finished_at", "duration_s", "hardware", "baseline", "total",
                 "ran", "passed", "failed", "expected_failures", "skipped", "pass_rate",
                 "cases"],
    "properties": {
        "schema_version": {"const": REPORT_SCHEMA_VERSION},
        "kind": {"const": REPORT_KIND},
        "suite": {"type": "string", "minLength": 1},
        "model": {"type": "string", "minLength": 1},
        "backend": {"type": "string", "minLength": 1},
        "n": {"type": ["integer", "null"], "minimum": 1},
        "evi_version": {"type": "string"},
        "started_at": _TIME,
        "finished_at": _TIME,
        "duration_s": {"type": "number", "minimum": 0},
        "hardware": _HARDWARE_SCHEMA,
        "baseline": {"type": ["object", "null"]},
        "total": {"type": "integer", "minimum": 0},
        "ran": {"type": "integer", "minimum": 0},
        "passed": {"type": "integer", "minimum": 0},
        "failed": {"type": "integer", "minimum": 0},
        "expected_failures": {"type": "integer", "minimum": 0},
        "skipped": {"type": "integer", "minimum": 0},
        "pass_rate": _RATE,
        "cases": {"type": "array", "items": {"$ref": "#/$defs/case"}},
    },
    "$defs": {
        "case": {
            "type": "object",
            "required": ["name", "runs", "passes", "pass_rate", "threshold", "passed",
                         "skipped", "skip_reason", "expected_fail", "failures", "mean_turns",
                         "mean_duration_s", "stop_reasons", "tags", "baseline",
                         "verify_skipped"],
            "properties": {
                "name": {"type": "string", "minLength": 1},
                "runs": {"type": "integer", "minimum": 0},
                "passes": {"type": "integer", "minimum": 0},
                "pass_rate": {"type": ["number", "null"], "minimum": 0, "maximum": 1},
                "threshold": _RATE,
                "passed": {"type": "boolean"},
                "skipped": {"type": "boolean"},
                "skip_reason": {"type": ["string", "null"]},
                "expected_fail": {"type": "boolean"},
                "failures": {"type": "array", "items": {"type": "string"}},
                "mean_turns": {"type": ["number", "null"], "minimum": 0},
                "mean_duration_s": {"type": ["number", "null"], "minimum": 0},
                "stop_reasons": {"type": "object",
                                 "additionalProperties": {"type": "integer", "minimum": 1}},
                "tags": {"type": "array", "items": {"type": "string"}},
                "baseline": {"type": ["object", "null"]},
                # Runs whose verify gate was skipped (an untrusted folder).
                "verify_skipped": {"type": "integer", "minimum": 0},
            },
        },
    },
}

# evals/self/baseline.json: the reference model's pass rates, the gate's
# entry criterion. Written only by `evi eval baseline record` (the director).
BASELINE_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "eVi eval baseline",
    "type": "object",
    "additionalProperties": False,
    "required": ["schema_version", "suite", "model", "backend", "quantization", "hardware",
                 "n", "recorded_at", "evi_version", "cases"],
    "properties": {
        "schema_version": {"const": BASELINE_SCHEMA_VERSION},
        "suite": {"type": "string", "minLength": 1},
        "model": {"type": "string", "minLength": 1},
        "backend": {"type": "string", "minLength": 1},
        "quantization": {"type": "string", "minLength": 1},
        "hardware": {**_HARDWARE_SCHEMA, "additionalProperties": False},
        "n": {"type": "integer", "minimum": 1},
        "recorded_at": _TIME,
        "evi_version": {"type": "string", "minLength": 1},
        "cases": {"type": "object", "minProperties": 1, "additionalProperties": _RATE},
    },
}


def schema_errors(doc: Any, schema: Mapping[str, Any]) -> list[str]:
    """Why `doc` doesn't match `schema` (`REPORT_SCHEMA`, `BASELINE_SCHEMA`);
    [] when it does, or when jsonschema (an optional dependency) isn't
    installed (`evi.evidence.schema_errors`)."""
    from evi.evidence import schema_errors as _errors

    if not isinstance(doc, Mapping):
        return ["(top): not a JSON object"]
    return _errors(doc, schema)


# ---- writing and reading reports -----------------------------------------------


def _write_text(path: Path, text: str) -> None:
    """`text` into `path` through a temp file of this call's own beside it
    (two writers never share one), then os.replace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.urandom(8).hex()}.tmp")
    fh = open(tmp, "x", encoding="utf-8", newline="\n")
    try:
        with fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _write_new(folder: Path, stem: str, text: str) -> Path:
    """`text` as a new ``<stem>.json`` in `folder` (``<stem>-2.json``, ...
    when the name is taken), created exclusively: two reports of one suite
    and model that finish in the same second (the web Run button and a
    scheduled task, in one process) never pick the same name."""
    folder.mkdir(parents=True, exist_ok=True)
    k = 1
    while True:
        path = folder / (f"{stem}.json" if k == 1 else f"{stem}-{k}.json")
        try:
            fh = open(path, "x", encoding="utf-8", newline="\n")
        except FileExistsError:
            k += 1
            continue
        try:
            with fh:
                fh.write(text)
        except BaseException:
            try:
                path.unlink()
            except OSError:
                pass
            raise
        return path


def write_report(report: dict[str, Any], root: Path | None = None) -> tuple[Path, Path]:
    """Keep `report` as ``<eVi home>/evals/reports/<suite>/<UTC time>-<model>.json``
    (a ``-2``, ``-3``, ... suffix when the name is taken) and its ``.html``
    rendering; returns both paths. EvalError when the report doesn't match
    `REPORT_SCHEMA` (nothing is written)."""
    bad = schema_errors(report, REPORT_SCHEMA)
    if bad:
        raise EvalError(f"the eval report does not match its schema: {bad[0][:300]}")
    folder = reports_dir(root) / fs_slug(report["suite"])
    stamp = str(report["finished_at"]).replace("-", "").replace(":", "")
    stem = f"{stamp}-{fs_slug(report['model'])}"
    path = _write_new(folder, stem, json.dumps(report, indent=2, ensure_ascii=False) + "\n")
    page = path.with_suffix(".html")
    _write_text(page, render_html(report))
    return path, page


def _pct(rate: Any) -> str:
    return "skipped" if rate is None else f"{round(float(rate) * 100)}%"


def verdict(row: Mapping[str, Any]) -> str:
    """A report row's verdict as reports and logs show it: BELOW BASELINE,
    SKIP, PASS, XPASS (an expected failure that passed), XFAIL or FAIL."""
    if (row.get("baseline") or {}).get("regressed"):
        return "BELOW BASELINE"
    if row.get("skipped"):
        return "SKIP"
    if row.get("passed"):
        return "XPASS" if row.get("expected_fail") else "PASS"
    return "XFAIL" if row.get("expected_fail") else "FAIL"


def render_html(report: Mapping[str, Any]) -> str:
    """`report` as a static HTML page: every value escaped, no scripts."""
    e = html.escape
    hw = report.get("hardware") or {}
    head = (f"{report.get('passed')}/{report.get('ran')} cases passed "
            f"({_pct(report.get('pass_rate'))}), {report.get('skipped')} skipped")
    rows = []
    for r in report.get("cases") or []:
        mark = verdict(r)
        why = "\n".join([*([r["skip_reason"]] if r.get("skip_reason") else []),
                         *(r.get("failures") or [])])
        if r.get("verify_skipped"):
            why += f"\n(verify gate skipped in {r['verify_skipped']} run(s))"
        rows.append(
            f"<tr><td>{e(mark)}</td><td>{e(str(r.get('name')))}</td>"
            f"<td>{e(str(r.get('passes')))}/{e(str(r.get('runs')))}</td>"
            f"<td>{e(_pct(r.get('pass_rate')))}</td><td>{e(_pct(r.get('threshold')))}</td>"
            f"<td>{e(str(r.get('mean_turns')))}</td><td><pre>{e(why)}</pre></td></tr>")
    title = f"eVi eval: {report.get('suite')} on {report.get('model')}"
    runs = f"{report['n']} runs per case" if report.get("n") else "each case's own runs"
    gpu = (f"{hw.get('gpu')} ({hw.get('vram_gb')} GB VRAM)" if hw.get("vram_gb") is not None
           else str(hw.get("gpu")))
    machine = f"{gpu} · {hw.get('cpu')} · {hw.get('ram_gb')} GB RAM · {hw.get('os')}"
    when = (f"backend {report.get('backend')} · {runs} · eVi {report.get('evi_version')} · "
            f"{report.get('started_at')} to {report.get('finished_at')}")
    base = report.get("baseline") if isinstance(report.get("baseline"), Mapping) else None
    held = ""
    if base:
        bits = [f"held to the baseline {base.get('path')} ({base.get('model')}, tolerance "
                f"{base.get('tolerance')})"]
        for key, label in (("regressed", "below it"), ("missing", "not in this run"),
                           ("excluded", "left out by a tag filter (a partial run)")):
            if base.get(key):
                bits.append(f"{label}: {', '.join(map(str, base[key]))}")
        if base.get("mismatch"):
            bits.append(f"differs: {base['mismatch']}")
        held = f"<p>{e(' · '.join(bits))}</p>\n"
    return (
        "<!doctype html>\n<html lang=\"en\"><head><meta charset=\"utf-8\">\n"
        "<meta http-equiv=\"Content-Security-Policy\" content=\"default-src 'none'; "
        "style-src 'unsafe-inline'\">\n"
        f"<title>{e(title)}</title>\n<style>body{{font:14px system-ui,sans-serif;"
        "margin:24px}table{border-collapse:collapse}td,th{border:1px solid #8884;"
        "padding:4px 8px;vertical-align:top;text-align:left}pre{margin:0;"
        "white-space:pre-wrap;max-width:60ch}</style></head><body>\n"
        f"<h1>{e(title)}</h1>\n<p>{e(head)}</p>\n<p>{e(when)}</p>\n<p>{e(machine)}</p>\n"
        + held +
        "<table><tr><th></th><th>case</th><th>passes</th><th>pass rate</th>"
        "<th>threshold</th><th>mean turns</th><th>failures</th></tr>\n"
        + "\n".join(rows) + "\n</table></body></html>\n"
    )


def read_json(path: Path) -> dict[str, Any]:
    """A report or baseline file as a dict; EvalError when it isn't one."""
    p = Path(path)
    try:
        if p.stat().st_size > _MAX_REPORT_BYTES:
            raise EvalError(f"{p} is too big to be an eval report")
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError) as exc:
        raise EvalError(f"could not read {p}: {exc}") from exc
    if not isinstance(data, dict):
        raise EvalError(f"{p} is not a JSON object")
    return data


def _summary(report: Mapping[str, Any], path: Path) -> dict[str, Any]:
    keep = ("suite", "model", "backend", "n", "finished_at", "total", "ran", "passed",
            "skipped", "failed", "pass_rate")
    out = {k: report.get(k) for k in keep}
    out["file"] = path.name
    out["cases"] = [
        {k: r.get(k) for k in ("name", "runs", "passes", "pass_rate", "passed", "skipped",
                               "expected_fail")}
        for r in report.get("cases") or [] if isinstance(r, dict)
    ]
    return out


def latest_reports(root: Path | None = None) -> list[dict[str, Any]]:
    """The newest report of each (suite, model, backend) under ``<eVi
    home>/evals/reports``, summarized (header totals and per-case pass
    rates), by suite, model, then backend. The backend is part of the key: a
    fake run labelled with a real model's id (``--backend fake --model M``,
    `evi models recommend --verify --backend fake`) never stands in for that
    model's real results. It reads at most the newest `_MAX_REPORT_FILES`
    of each suite's folder, so a suite run often (a scheduled one) never
    pushes another suite's latest report out."""
    folder = reports_dir(root)
    if not folder.is_dir():
        return []

    def newest_first(p: Path) -> tuple[str, int]:
        # The name starts with the UTC time; two in one second: the later file.
        try:
            mtime = p.stat().st_mtime_ns
        except OSError:
            mtime = 0
        return p.name.split("-", 1)[0], mtime

    files: list[Path] = []
    for sub in sorted(p for p in folder.iterdir() if p.is_dir()):
        files += sorted(sub.glob("*.json"), key=newest_first, reverse=True)[:_MAX_REPORT_FILES]
    files.sort(key=newest_first, reverse=True)
    seen: dict[tuple[str, str, str], dict[str, Any]] = {}
    for p in files:
        try:
            data = read_json(p)
        except EvalError:
            continue
        if data.get("kind") != REPORT_KIND or not isinstance(data.get("cases"), list):
            continue
        key = (str(data.get("suite")), str(data.get("model")), str(data.get("backend")))
        if key not in seen:
            seen[key] = _summary(data, p)
    return [seen[k] for k in sorted(seen)]


# ---- comparing and gating ----------------------------------------------------------


def rates_of(doc: Mapping[str, Any]) -> dict[str, float | None]:
    """{case: pass rate} of a report (None for a skipped case) or a baseline."""
    cases = doc.get("cases")
    if isinstance(cases, dict):
        return {str(k): float(v) for k, v in cases.items() if isinstance(v, (int, float))}
    out: dict[str, float | None] = {}
    for r in cases or []:
        if isinstance(r, dict) and r.get("name"):
            rate = r.get("pass_rate")
            out[str(r["name"])] = None if r.get("skipped") or rate is None else float(rate)
    return out


def compare(a: Mapping[str, Any], b: Mapping[str, Any],
            tolerance: float = 0.0) -> dict[str, Any]:
    """Per-case pass-rate deltas from `a` to `b` (reports or baselines).
    A case `a` ran regressed when `b` didn't run it or its pass rate fell
    by more than `tolerance`. {rows: [{name, a, b, delta, status}],
    regressed: [names]}; status is "regressed", "improved", "same", "new",
    "missing" (not run in `b`) or "skipped" (not run in `a`)."""
    ra, rb = rates_of(a), rates_of(b)
    rows, regressed = [], []
    for name in list(ra) + [n for n in rb if n not in ra]:
        x, y = ra.get(name), rb.get(name)
        if name not in ra:
            status = "new"
        elif x is None:
            status = "skipped"
        elif y is None:
            status = "missing"
        elif y + 1e-9 < x - tolerance:
            status = "regressed"
        elif y > x + 1e-9:
            status = "improved"
        else:
            status = "same"
        if status in ("regressed", "missing"):
            regressed.append(name)
        delta = round(y - x, 4) if x is not None and y is not None else None
        rows.append({"name": name, "a": x, "b": y, "delta": delta, "status": status})
    return {"rows": rows, "regressed": regressed}


def apply_baseline(report: dict[str, Any], baseline: Mapping[str, Any],
                   tolerance: float = 0.1, path: str | None = None,
                   excluded: Iterable[str] = (), partial: bool = False) -> dict[str, Any]:
    """Hold `report` to `baseline`, as `compare` would: a case below the
    baseline's pass rate minus `tolerance` fails (also an expected_fail
    one: the baseline already holds what it reached), and so does a
    baseline case that didn't run, skipped (its row's ``baseline`` says so)
    or missing from the report (``baseline.missing``: deleted, renamed, or
    in `excluded`, the cases a ``--tag`` / ``--skip-tag`` filter left out:
    a baseline holds the whole suite). Only a `partial` run (``evi eval run
    --partial``) excuses the `excluded` cases, listed under
    ``baseline.excluded`` instead. `report_ok` is then False. The report's
    ``baseline`` names the file, the cases that regressed, went missing or
    were left out, whether the run was partial, and how it differs from the
    baseline's model and backend (``mismatch``). Recounts the totals."""
    rates = rates_of(baseline)
    rows = {r.get("name"): r for r in report.get("cases") or []}
    left_out = set(excluded or ()) if partial else set()
    regressed: list[str] = []
    missing: list[str] = []
    for name, floor in rates.items():
        row = rows.get(name)
        if row is None:
            if name not in left_out:
                missing.append(name)
            continue
        if row.get("skipped"):
            row["baseline"] = {"pass_rate": floor, "tolerance": tolerance, "regressed": True}
            row["failures"] = [*row.get("failures", []),
                               f"skipped ({row.get('skip_reason')}), but the baseline holds "
                               f"it at {floor:.2f}"]
            regressed.append(name)
            continue
        rate = float(row.get("pass_rate") or 0.0)
        below = rate + 1e-9 < floor - tolerance
        row["baseline"] = {"pass_rate": floor, "tolerance": tolerance, "regressed": below}
        if below:
            regressed.append(name)
            row["passed"] = False
            row["failures"] = [*row.get("failures", []),
                               f"pass rate {rate:.2f} is below the baseline {floor:.2f} "
                               f"minus {tolerance:.2f}"]
    mismatch = []
    for key in ("model", "backend"):
        want, got = baseline.get(key), report.get(key)
        if want is not None and str(want) != str(got):
            mismatch.append(f"{key} {got} (the baseline's is {want})")
    report["baseline"] = {
        "path": path, "tolerance": tolerance, "model": baseline.get("model"),
        "backend": baseline.get("backend"), "regressed": regressed, "missing": missing,
        "excluded": sorted(left_out.intersection(rates)), "partial": bool(partial),
        "mismatch": "; ".join(mismatch) or None,
    }
    return summarize(report)


def load_baseline(path: Path) -> dict[str, Any]:
    """A baseline file, checked against `BASELINE_SCHEMA`; EvalError if not."""
    data = read_json(path)
    bad = schema_errors(data, BASELINE_SCHEMA)
    if bad or not isinstance(data.get("cases"), dict):
        raise EvalError(f"{path} is not an eval baseline: {(bad or ['no cases'])[0][:300]}")
    return data


def build_baseline(report: Mapping[str, Any], *, quantization: str,
                   hardware: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """The baseline a recorded `report` makes: its model, backend, hardware
    and N, and the pass rate of every case that ran."""
    from evi import __version__

    cases = {r["name"]: float(r["pass_rate"]) for r in report.get("cases") or []
             if not r.get("skipped") and r.get("pass_rate") is not None}
    hw = dict(hardware if hardware is not None else report.get("hardware") or {})
    return {
        "schema_version": BASELINE_SCHEMA_VERSION,
        "suite": str(report.get("suite")),
        "model": str(report.get("model")),
        "backend": str(report.get("backend")),
        "quantization": str(quantization or "unknown"),
        "hardware": {k: hw.get(k) for k in ("gpu", "vram_gb", "cpu", "ram_gb", "os")},
        "n": int(report.get("n") or max((r.get("runs") or 0 for r in report.get("cases") or []),
                                        default=1) or 1),
        "recorded_at": _now(),
        "evi_version": __version__,
        "cases": cases,
    }


def write_baseline(path: Path, baseline: Mapping[str, Any]) -> Path:
    """Write `baseline` to `path` once it matches `BASELINE_SCHEMA`
    (EvalError otherwise, nothing written)."""
    bad = schema_errors(baseline, BASELINE_SCHEMA)
    if bad:
        raise EvalError(f"the baseline does not match its schema: {bad[0][:300]}")
    target = Path(path)
    _write_text(target, json.dumps(dict(baseline), indent=2, ensure_ascii=False) + "\n")
    return target


# ---- agents for a run -------------------------------------------------------------


def _bare_agent(cfg: Any, **kw: Any) -> Any:
    from evi.sdk.builder import build_agent

    # No project context, hooks, guardrails, memory, skills or transcripts:
    # a hook that vetoes or rewrites a call, or this machine's notes, would
    # make the scores depend on the machine.
    return build_agent(config=cfg, enable_memory=False, enable_skills=False,
                       enable_project=False, enable_hooks=False, enable_guardrails=False,
                       transcripts=None, **kw)


def fake_agent_factory(config_loader: Callable[[], Any] | None = None,
                       model: str = "fake") -> AgentFactory:
    """make_agent for the fake backend: the case's `fake` script
    (`evi.evals_fake.ScriptedClient`) behind the real Agent loop."""
    from evi.evals_fake import ScriptedClient

    def make(case: EvalCase, workspace: Path) -> Any:
        from evi.config import Config

        cfg = (config_loader or Config.load)()
        cfg = replace(cfg, llm=replace(cfg.llm, backend="openai_compat", model=model or "fake"))
        return _bare_agent(cfg, client=ScriptedClient(case.fake or []),
                           tools=eval_tools(case.mode))

    return make


def model_agent_factory(config_loader: Callable[[], Any] | None = None, *,
                        backend: Any = None, model: str = "") -> AgentFactory:
    """make_agent for a real model: the configured [llm] backend, or the
    saved `backend` (an `evi.backends.registry.BackendEntry`), with `model`
    overriding the model id."""

    def make(case: EvalCase, workspace: Path) -> Any:
        from evi.config import Config

        cfg = (config_loader or Config.load)()
        return _bare_agent(cfg, backend=backend, model=model or None,
                           tools=eval_tools(case.mode))

    return make


def model_judge(config_loader: Callable[[], Any] | None = None, *, backend: Any = None,
                model: str = "") -> Judge:
    """The `judge` grader on the same model."""

    def factory() -> Any:
        from evi.config import Config

        cfg = (config_loader or Config.load)()
        return _bare_agent(cfg, backend=backend, model=model or None, tools=[])

    return make_judge(factory)


def configured_context(config: Any = None, *, default_mode: str = "",
                       repo: Path | None = None) -> EvalContext:
    """The EvalContext of a run on the configured [llm] backend and model,
    as ``evi eval run`` builds it with no --backend / --model: what the web
    Evals panel's Run button (``POST /api/evals/run``) and scheduled eval
    tasks (`evi.scheduler`) run `run_suite` with, so all three give one
    verdict. `config` (default `Config.load()`) names the backend and model
    the report shows; each run builds its agent from a fresh `Config.load()`,
    as the CLI's do. `repo` (default: the git repository of the process's
    folder, as the CLI without --repo) is what a ``workspace = "repo"`` case
    gets a worktree of, and only when it is trusted (`skip_reason`). Never
    the fake backend: EvalError when [llm] names it (it replays scripts and
    is only ``evi eval run --backend fake``'s)."""
    from evi.config import Config

    cfg = config if config is not None else Config.load()
    backend = str(cfg.llm.backend or "").strip()
    if backend.lower() == FAKE_BACKEND:
        raise EvalError(f"[llm] backend = {backend!r} is not a backend: the fake backend "
                        "replays eval scripts and only `evi eval run --backend fake` runs it")
    return EvalContext(
        make_agent=model_agent_factory(), backend=backend, model=str(cfg.llm.model or ""),
        judge=model_judge(), repo=repo if repo is not None else find_repo(),
        default_mode=str(default_mode or "").strip(),
    )


def filter_cases(suite: EvalSuite, tags: Iterable[str] = (),
                 skip_tags: Iterable[str] = ()) -> EvalSuite:
    """`suite` with only the cases carrying one of `tags` (all when none)
    and none of `skip_tags`."""
    want, drop = set(tags or ()), set(skip_tags or ())
    cases = [c for c in suite.cases
             if (not want or want & set(c.tags)) and not drop & set(c.tags)]
    return replace(suite, cases=cases)


_TEMPLATE = '''\
name = "{name}"
description = "What this suite checks"

[[case]]
name = "math"
prompt = "What is 2 + 2? Reply with just the number."
contains = ["4"]

[[case]]
name = "no-refusal"
prompt = "List three uses for a paperclip."
not_contains = ["I cannot", "I can't"]
ignore_case = true

[[case]]
name = "tone"
prompt = "Explain recursion to a five-year-old."
judge = "The explanation is simple, friendly, and uses an everyday analogy."
'''


def create_suite(name: str, root: Path | None = None, overwrite: bool = False) -> Path:
    """Write a starter eval suite and return its path. ``self`` is reserved
    (`evi eval run self` runs the repository's evals/self)."""
    slug = _slug(name)
    if is_self_name(slug):
        raise EvalError(f"{SELF!r} is reserved: `evi eval run self` runs the repository's "
                        "evals/self suites; pick another name")
    d = evals_dir(root)
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{slug}.toml"
    if path.exists() and not overwrite:
        raise EvalError(f"eval suite {slug!r} already exists (pass --overwrite to replace)")
    path.write_text(_TEMPLATE.format(name=slug), encoding="utf-8")
    return path
