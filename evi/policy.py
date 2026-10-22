"""Named policies for unattended runs: ``implementer`` for task runs, and
``watcher`` for the Companion's check-ins (`watcher_policy`, at the end of
this module: only the configured read-only sources, nothing else).

An unattended run (``evi run``, a delegated task) has nobody to approve a
tool call, so without a policy everything outside ``[auto] auto_approve`` is
denied: no shell, so no tests. The escape hatches (``-y``, ``[auto]
unattended_policy = "allow"``) allow everything. The ``implementer`` policy is
the narrow middle: what an implementer needs to change code and prove it works
in the one folder it was given, and nothing else.

Selected by ``evi run --policy implementer`` (``--scope GLOB`` adds the task's
``files_in_scope``, ``--confine`` holds writes to it) and by ``evi task start``
(`evi.tasks.run_task`, always confined), both through `implementer_policy`
(see **Scope** below for the difference). Set as ``Agent.policy``, it
replaces the permission flow: every tool call is allowed or denied, never asked about, whatever
``[auto] mode``, ``unattended_policy`` or ``/auto on`` say. ``[auto]
hard_deny``, ``deny`` rules and the director's ground (`evi.permissions`)
still deny first; an ``allow`` rule never widens the policy. The destructive
guard is forced on. A denial's reason goes into the tool result, and so into
the run report (``tool_calls[].reason``, ``denied_tools``).

What the policy allows (`Policy.check`), failing closed on anything else:

- **Reading** inside the run's folder (the *root*): ``read_file``,
  ``list_dir``, ``find_files``, ``search_files``, ``python_symbols``,
  ``check_file`` when its linter runs no project code (in a trusted folder),
  ``monitor`` tailing a file, and the read-only git tools (`evi.gitsafe`:
  the repository's config can't make them run a program). Every path
  argument must resolve (real path: symlinks and junctions followed,
  case-insensitive on Windows) inside the root, and a glob argument can't
  climb out of it. A secret
  (``.env``, a key; `evi.permissions.protected_read`) is never read.
- **Writing** inside the root with ``write_file``, ``edit_file`` and
  ``apply_patch``, except protected files (below); under a confined policy
  only the task's ``files_in_scope`` (**Scope**, below).
- **Shell lines** (``run_command``, ``monitor`` kind=command) whose every
  segment (`evi.shellsplit`) is allowlisted (`SHELL_RULES`): ``pytest``,
  ``ruff check|format``, ``python``/``python3``/``py`` (any path) ``-m
  pytest`` or ``-m ruff check|format``, ``npm test``, ``npm run test``,
  ``cargo check|test|clippy``, and the read-only git subcommands (``status``,
  ``diff``, ``log``, ``show``, ``rev-parse``, ``ls-files``, ``blame``,
  ``grep``, ``branch --show-current|--list``) without ``-c``,
  ``--config-env``, ``--exec-path``, ``--output``, ``--ext-diff``,
  ``-O``/``--open-files-in-pager``, ``--no-index``, ``--contents`` (or an
  abbreviation git would take for one), or a ``-C`` / ``--git-dir`` /
  ``--work-tree`` outside the root. Only in a trusted folder (`evi.trust`,
  ``evi trust``), as the verify checks: in an untrusted one the policy runs
  no command line at all, nor ``check_file``'s linter, since each can run the
  folder's own code. pytest, npm and cargo run it by design; git runs the
  programs the repository's ``.git/config`` names (``core.fsmonitor``,
  ``diff.external``, filter drivers) and ``python -m ruff`` imports a
  ``ruff`` package from the folder; and on Windows the shell (and process
  creation) finds a ``ruff.bat`` or ``git.exe`` in the folder before PATH.
  The ``git_*`` tools still read the history there. The project's check
  commands (`evi.checks.allowlist`) are added at run start (``shell_allow``):
  each runs exactly as given, and words after one must meet the builtin rule
  for that program (else only the command itself). The line must parse; command substitution, backticks,
  process substitution and subshells, an expansion (``$x``), and an
  environment-assignment prefix (``X=1 pytest``) are denied; redirects must
  stay inside the root; the command runs in a folder inside the root. A denied
  line's reason names the segment.
- **A command's own arguments** stay in the root too (`_args_outside`): every
  argument that is no option, the value of a ``--name=value`` option, a short
  option's attached value and the value in a ``key=value`` must resolve
  inside it (``ruff format ../x.py``, ``git diff ../x calc.py`` and
  ``pytest --rootdir=/tmp`` are denied). The options that write a file of
  their choosing, send output over the network or set config that runs a
  program are refused outright: pytest's ``--junitxml``, ``--basetemp``,
  ``--pastebin``, ``-o``/``--override-ini`` and the other report files
  (`_PYTEST_REFUSED`), ruff's ``-o``/``--output-file`` and ``--cache-dir``,
  for both any ``@file`` word (they read more arguments from that file,
  which the check never sees: ``pytest @args.txt`` could hold
  ``--basetemp``), cargo's ``--config`` and ``-Z``, and any npm option before ``--`` but
  ``--silent`` / ``--if-present`` (an npm option sets npm config, such as
  its script shell). But for read-only git, no argument may name a protected
  file the task can't write: ruff rewrites the files it is given (``format``,
  a ``fix`` setting), a test binary takes ``--logfile``.
- ``search_tools``: in-process only (what it surfaces is checked here too).

Everything else is denied, unknown tools included: the network (``web_fetch``,
``web_search``, federation, A2A, channels, browser), MCP tools, ``run_python``,
memory (it lives in eVi's home and outlasts the run), subagents and delegate
backends (v0 does not hand the policy down), computer use, scheduling.

**Protected files.** The repository's own write-protected files, the
relative entries of `evi.permissions.WRITE_PROTECTED_PATHS` (`GRANTABLE`:
``.github/workflows/**``, ``EVI.md``, ``AGENTS.md``, ``.evi.toml``,
``.evi/**``, ``.vscode/tasks.json``), are denied unless a ``files_in_scope``
entry names the file explicitly (`scope_names`): the entry's folder part has
no wildcard and is the file's folder, and its last part is the file's name
or, below the top folder, a wildcard matching it. So
``.github/workflows/ci.yml`` and ``.github/workflows/*.yml`` grant
``.github/workflows/ci.yml``, but ``**``, ``.github/**`` or ``*.md`` grant no
protected file, and a file at the top (``EVI.md``) is granted only by its
exact name. Never granted, named or not (`HARD_PROTECTED` and the rest of
the protected list): ``.evi/tasks/**``, the eval gate's reference
``evals/self/baseline.json`` (`evi.evals`), anything under ``.git``, eVi's home
(``~/.evi/**``, ``EVI_HOME``), the director's key folder, the secret and
start-up files (`evi.permissions.SECRET_PROTECTED_PATHS`) and every other
``[auto] protected_paths`` entry the user configured.

**Scope.** What ``files_in_scope`` means depends on how the policy was built
(`implementer_policy`'s ``confine``):

- *Unconfined* (``evi run --policy implementer --scope GLOB``, the default):
  the scope only grants protected files, as above; any other file in the
  root may be written.
- *Confined* (``confine=True``: ``evi task start`` through
  `evi.tasks.run_task`, and ``evi run --policy implementer --confine``):
  every write must also match a ``files_in_scope`` entry (`in_scope`: the
  file's own path or a folder it is in, taken literally, so ``pages/[id].tsx``
  covers itself; else the entry as an fnmatch glob over the whole
  repo-relative ``/`` path, so ``*`` crosses folders too and ``evi/*.py``
  covers ``evi/apps/x.py``, while a ``**/`` also matches no folder, as in a
  .gitignore; ``.`` is the whole folder; case-insensitive on Windows). That
  holds for ``write_file``, ``edit_file`` and every path an ``apply_patch``
  call names, for an output redirect in a command line, and for the files a
  command rewrites (`_rewritten`): ``ruff format`` (but ``--check`` /
  ``--diff``) and ``ruff check`` with any option not known to rewrite
  nothing: ``--fix``, ``--unsafe-fixes``, ``--fix-only``, ``--add-noqa``,
  ``--add-ignore``, a ``--config`` (an inline ``fix = true``, or a file that
  sets it; but the fixes with ``--diff``), or one a newer ruff adds (it fails
  closed), also as ``python -m ruff``. Such a
  command naming a folder (``ruff format .``),
  or no file at all (it then rewrites the folder it runs in), is denied:
  name the files instead; so is ``cargo check|test|clippy --fix`` (it
  rewrites the whole crate). A denial names the task's scope and tells the
  model to list the file as a concern in its hand-off. A broad entry still
  grants no protected file, and an empty ``files_in_scope`` lets a confined
  run change nothing (``evi task start`` refuses such a task). Reads, and
  every other command, are as unconfined: tests and linters run on any file.
  What a command writes anyway (a ``fix = true`` setting in the folder's own
  ruff config, a test's own output files) is the residual risk the
  backstop catches: `evi.tasks.run_task` records the branch's changed files
  outside the scope (``out_of_scope``), and ``evi task verify`` refuses to
  sign over them without the director's reason.

**Relative paths** resolve against the folder the tool itself uses (`Policy.check`'s
``workdir``; default `evi.workdir.get_cwd()`: the session's working folder,
else the process's), never against the root, so a link in a subfolder the
run started in is followed where the tool would follow it.

**Residual risk.** An allowlisted test or build command runs repository code
(``conftest.py``, a test, ``build.rs``, an npm script, a pytest plugin it
loads with ``-p``): the shell allowlist is a guardrail against obvious
network and destructive commands, not a sandbox, and such code can still
reach the network or write outside the root (that is why no command runs in
an untrusted folder). The argument check reads the words, not the tool's mind:
a folder argument covers what is in it (unconfined, ``ruff format .`` may
rewrite a protected ``.py`` file under ``.evi/``), and a tool's own cache or
temporary files land where its configuration in the folder says. The environment scrub
(no ``*_KEY`` / ``*_TOKEN`` / ``*_SECRET`` variables for an unattended run,
`evi.proc.scrubbed_env`) limits what it can take; the worktree (``evi run
--worktree``) is the blast radius.
"""

from __future__ import annotations

import fnmatch
import itertools
import json
import os
import re
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Iterable, NamedTuple, Sequence

from evi import permissions, shellsplit

IMPLEMENTER = "implementer"

# Never writable under the policy, named in files_in_scope or not (on top of
# every protected pattern outside GRANTABLE). evals/self/baseline.json is the
# eval gate's reference (evi.evals): an agent that could rewrite it could
# move the gate it is measured against.
HARD_PROTECTED: tuple[str, ...] = (
    ".evi/tasks", ".evi/tasks/**", ".git", ".git/**", "~/.evi", "~/.evi/**",
    "evals/self/baseline.json",
)
# The repo-relative write-protected files a task may be granted by naming
# them in files_in_scope; the others (.git internals, eVi's home, the eval
# baseline) never.
GRANTABLE: tuple[str, ...] = tuple(
    p for p in permissions.WRITE_PROTECTED_PATHS
    if not p.startswith(("~", ".git/")) and p not in HARD_PROTECTED
)

# Tools the policy knows, with the category each must have: a tool of another
# category under one of these names (an MCP tool, a plugin's) is unknown.
READ_TOOLS: dict[str, str] = {
    "read_file": "fs", "list_dir": "fs", "find_files": "fs", "search_files": "fs",
    "python_symbols": "code", "check_file": "code",
    "git_blame": "git", "git_diff": "git", "git_info": "git", "git_log": "git",
    "git_show": "git", "git_status": "git",
}
WRITE_TOOLS: dict[str, str] = {"write_file": "fs", "edit_file": "fs", "apply_patch": "fs"}
SHELL_TOOLS: dict[str, str] = {"run_command": "shell", "monitor": "code"}
PURE_TOOLS: dict[str, str] = {"search_tools": "meta"}

# The shell allowlist, as documented above (`_builtin_reason`).
SHELL_RULES: tuple[str, ...] = (
    "pytest", "ruff check|format", "python|python3|py -m pytest", "python -m ruff check|format",
    "npm test", "npm run test", "cargo check|test|clippy",
    "git status|diff|log|show|rev-parse|ls-files|blame|grep",
    "git branch --show-current|--list",
)
_GIT_READ = frozenset({"status", "diff", "log", "show", "rev-parse", "ls-files", "blame", "grep"})
# git's global options that are fine before the subcommand.
_GIT_GLOBAL_FLAGS = frozenset({"--no-pager", "--no-optional-locks", "--literal-pathspecs",
                               "--no-replace-objects", "--bare"})
# git options that take a folder: allowed only inside the root.
_GIT_DIR_OPTIONS = ("-C", "--git-dir", "--work-tree")
# git arguments refused anywhere in the line: they run a program, read config
# from elsewhere, write a file or read outside the repository. git takes an
# unambiguous abbreviation of a long option (--outp=x is --output=x), so a
# long option any of these starts with is refused too.
_GIT_REFUSED = ("--config-env", "--exec-path", "--output", "--ext-diff",
                "--open-files-in-pager", "--no-index", "--contents", "-O")
_GIT_REFUSED_LONG = tuple(o for o in _GIT_REFUSED if o.startswith("--"))
# Options of an allowlisted tool refused anywhere in its arguments: they write
# a file of their choosing (inside .git, or a protected file), send output
# over the network (--pastebin) or set config that runs a program (cargo
# --config, a target runner). A one-letter one counts inside a cluster (-vo)
# up to the first letter that takes a value (the rest is that value).
_PYTEST_REFUSED = ("--junitxml", "--junit-xml", "--basetemp", "--result-log", "--resultlog",
                   "--report-log", "--log-file", "--debug", "--html", "--cov-report",
                   "--pastebin", "--override-ini", "-o")
_PYTEST_VALUED = "kmcprWn"
_RUFF_REFUSED = ("--output-file", "--cache-dir", "-o")
_CARGO_REFUSED = ("--config", "-Z")
_CARGO_VALUED = "pjF"
# The npm options allowed before `--`: any other sets npm config (its
# script shell, a node preload, the registry).
_NPM_FLAGS = frozenset({"-s", "--silent", "--if-present"})
# ruff check's options known to rewrite no file it is given. Any other
# (--fix, --unsafe-fixes, --fix-only, --add-noqa, --add-ignore, a --config
# that may say `fix = true`, an option a newer ruff adds) makes it count as
# rewriting the files it names: it fails closed. With --diff, the fix options
# (_RUFF_DIFFED) write a diff to stdout instead. Then ruff's options that
# take a value as the next word (that word is no file it rewrites). An option
# taking a value that is missing here makes its value count as a file, which
# a confined policy then holds to the scope: it fails closed too (spell it
# --name=value).
_RUFF_CHECK_READS = frozenset({
    "--no-fix", "--no-unsafe-fixes", "--no-fix-only", "--show-fixes", "--no-show-fixes",
    "--ignore-noqa", "--statistics", "--show-files", "--show-settings", "--output-format",
    "--target-version", "--line-length", "--dummy-variable-rgx", "--extension", "--preview",
    "--no-preview", "--select", "--ignore", "--extend-select", "--extend-ignore",
    "--per-file-ignores", "--extend-per-file-ignores", "--fixable", "--unfixable",
    "--extend-fixable", "--extend-unfixable", "--exclude", "--extend-exclude",
    "--respect-gitignore", "--no-respect-gitignore", "--force-exclude", "--no-force-exclude",
    "--stdin-filename", "--no-cache", "--exit-zero", "--exit-non-zero-on-fix", "--isolated",
    "--color", "--watch", "--verbose", "--quiet", "--silent", "--help",
    "-n", "-e", "-w", "-v", "-q", "-s", "-h",
})
_RUFF_DIFFED = frozenset({"--diff", "--fix", "--unsafe-fixes", "--fix-only", "--config"})
_RUFF_VALUED = frozenset({
    "--config", "--select", "--ignore", "--extend-select", "--extend-ignore",
    "--per-file-ignores", "--extend-per-file-ignores", "--exclude", "--extend-exclude",
    "--fixable", "--unfixable", "--extend-fixable", "--extend-unfixable", "--target-version",
    "--line-length", "--dummy-variable-rgx", "--output-format", "--output-file", "-o",
    "--cache-dir", "--stdin-filename", "--extension", "--range", "--color",
})
# How many files_in_scope entries a confined denial lists.
_SCOPE_SHOWN = 10
_PYTHON = re.compile(r"(?:python(?:3(?:\.\d+)?)?|py)\Z")
_ASSIGNMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=")
_SEPARATORS = re.compile(r"[\\/]")
_WILDCARDS = "*?["


class PolicyError(ValueError):
    """A policy that can't be built: an unknown name, a bad scope entry."""


class Verdict(NamedTuple):
    """`Policy.check`'s answer: allowed, or not and why."""

    allowed: bool
    reason: str = ""


ALLOW = Verdict(True)


def _deny(reason: str) -> Verdict:
    return Verdict(False, reason)


# ---- scope -------------------------------------------------------------------


def _norm_entry(entry: str) -> str:
    """A files_in_scope entry as matched: ``/``-separated, no leading ``./``."""
    s = str(entry or "").strip().replace("\\", "/")
    while s.startswith("./"):
        s = s[2:]
    return s.rstrip("/")


def validate_scope(entries: Iterable[str]) -> tuple[str, ...]:
    """`entries` (files_in_scope globs, relative to the run's folder) as the
    policy keeps them. Raises PolicyError for an empty, absolute or
    home-relative one, or one with a ``..`` part: scope can't reach outside
    the folder."""
    out: list[str] = []
    for raw in entries or ():
        s = _norm_entry(raw)
        if not s:
            raise PolicyError(f"empty scope entry {raw!r}")
        if s.startswith(("/", "~")) or re.match(r"[A-Za-z]:", s) or s.startswith("//"):
            raise PolicyError(f"scope entry {raw!r} is absolute: give a path inside the folder")
        if ".." in s.split("/"):
            raise PolicyError(f"scope entry {raw!r} climbs out of the folder ('..')")
        if s not in out:
            out.append(s)
    return tuple(out)


def _key(path: str) -> str:
    """A repo-relative path or scope entry as compared for a grant. On
    Windows each part loses the trailing dots and spaces and the ``:stream``
    suffix Windows ignores (``EVI.md.`` writes EVI.md), and case is folded."""
    parts: list[str] = []
    for part in path.replace("\\", "/").split("/"):
        if os.name == "nt" and part not in (".", ".."):
            part = part.split(":", 1)[0].rstrip(". ")
        if part and part != ".":
            parts.append(part)
    s = "/".join(parts)
    return s.lower() if os.name == "nt" else s


def scope_names(entry: str, rel: str) -> bool:
    """True if files_in_scope `entry` names the repo-relative file `rel`
    explicitly enough to grant it when it is protected (see the module doc):
    the entry's folder part is wildcard-free and equals the file's folder,
    and its last part is the file's name, or a wildcard matching it when the
    file is below the top folder."""
    e_dir, _, e_name = _key(_norm_entry(entry)).rpartition("/")
    r_dir, _, r_name = _key(rel).rpartition("/")
    if not e_name or any(c in e_dir for c in _WILDCARDS) or e_dir != r_dir:
        return False
    if not r_dir:
        return e_name == r_name
    return fnmatch.fnmatchcase(r_name, e_name)


def in_scope(entries: Iterable[str], rel: str) -> bool:
    """True if the repo-relative file `rel` matches a files_in_scope entry
    (what a confined policy may write, and what `evi.tasks.out_of_scope`
    leaves out): the entry as `rel` itself or a folder it is in, taken
    literally (``pages/[id].tsx`` covers itself, ``tests/fixtures`` covers
    ``tests/fixtures/a.json``); else as an fnmatch glob over the whole
    ``/``-separated path or over a folder `rel` is in (``*`` matches across
    folders too: ``evi/*.py`` covers ``evi/apps/x.py``; a ``**/`` also
    matches no folder, `_glob_forms`: ``**/*.py`` covers ``calc.py``). The
    entry ``.`` is the whole folder. Compared as `_key` has them:
    case-insensitive on Windows."""
    key = _key(rel)
    if not key:
        return False
    for entry in entries or ():
        norm = _norm_entry(entry)
        if norm == ".":
            return True  # the run's whole folder
        e = _key(norm)
        if not e:
            continue
        if key == e or key.startswith(e + "/"):
            return True
        if any(fnmatch.fnmatchcase(key, form) or fnmatch.fnmatchcase(key, form + "/*")
               for form in _glob_forms(e)):
            return True
    return False


# The most `**/` in one entry `_glob_forms` takes out one at a time (each
# doubles the forms); past it, all of them at once or none.
_GLOB_FORMS_MAX = 4
# A `**/` at the start of a glob or after a `/`.
_ANY_FOLDERS = re.compile(r"(?:^|(?<=/))\*\*/")


def _glob_forms(pattern: str) -> set[str]:
    """`pattern` and, for each ``**/`` in it (at its start or after a
    ``/``), the form without it as well: fnmatch's ``**/`` needs at least
    one folder, a .gitignore's matches none too (``tests/**/test_*.py``
    covers ``tests/test_x.py``)."""
    parts = _ANY_FOLDERS.split(pattern)
    if len(parts) == 1:
        return {pattern}
    if len(parts) - 1 > _GLOB_FORMS_MAX:
        return {pattern, "".join(parts)}
    return {parts[0] + "".join(k + p for k, p in zip(keep, parts[1:]))
            for keep in itertools.product(("**/", ""), repeat=len(parts) - 1)}


# ---- paths -------------------------------------------------------------------


def _resolve(value: str, base: Path) -> Path | None:
    """`value` (relative to `base`) on its real path, or None when where it
    leads can't be told without opening it (a network or opaque device
    path) or it can't be resolved."""
    local = permissions._local_spelling(str(value))
    if local is None:
        return None
    try:
        p = Path(local or ".").expanduser()
        if not p.is_absolute():
            p = base / p
        if permissions._is_network_path(p):
            return None
        return p.resolve()
    except (OSError, RuntimeError, ValueError):
        return None


def _inside(p: Path, roots: Sequence[Path]) -> bool:
    return any(p == r or p.is_relative_to(r) for r in roots)


def _base(workdir: str | Path | None) -> Path:
    """The folder relative path arguments resolve against: `workdir` (the
    folder the tool resolves them in), else the one a file tool uses when
    none is set (`evi.workdir.get_cwd`: the session's, else the process's).
    Never the root: a tool started in a subfolder resolves there, and a link
    in that subfolder must be followed where the tool would follow it."""
    if not workdir:
        from evi import workdir as _workdir

        workdir = _workdir.get_cwd()
    try:
        return Path(workdir).expanduser()
    except RuntimeError:
        return Path(workdir)


# ---- the policy --------------------------------------------------------------


@dataclass(frozen=True)
class Policy:
    """A named tool policy for an unattended run (see the module doc).

    - name: what the run report's ``policy`` says.
    - root: the run's folder (resolved); reads and writes stay inside it.
    - write_roots: where writes may go (the root).
    - extra_writable: the task's files_in_scope (validated, repo-relative
      globs); unconfined they matter for protected files only
      (`scope_names`).
    - confine: writes stay inside extra_writable too (`in_scope`; the
      module doc's **Scope**): what a task run uses.
    - shell_allow: exact argv commands allowed besides `SHELL_RULES` (the
      project's check commands, `evi.checks.allowlist`); words after one
      must meet the `SHELL_RULES` entry for its program.
    - read_roots: folders readable besides the root (none in v0).
    - deny_effects: what no call may do (documentation; enforced by the tool
      allowlist).
    - guard: the destructive-command guard, always on.
    - trusted: the root is a trusted folder (`evi.trust`); only then may a
      command line (or ``check_file``'s linter) run at all: each can run
      the folder's own code (`untrusted_reason`).
    """

    name: str
    root: Path
    write_roots: tuple[Path, ...] = ()
    extra_writable: tuple[str, ...] = ()
    shell_allow: tuple[tuple[str, ...], ...] = ()
    read_roots: tuple[Path, ...] = ()
    deny_effects: frozenset[str] = field(default_factory=lambda: frozenset({"network"}))
    guard: bool = True
    trusted: bool = False
    confine: bool = False

    # -- building ----------------------------------------------------------

    def with_commands(self, commands: Iterable[Sequence[str]]) -> "Policy":
        """This policy with `commands` (argv lists) added to shell_allow."""
        extra = [tuple(str(w) for w in c) for c in commands or () if c]
        allow = tuple(dict.fromkeys([*self.shell_allow, *extra]))
        return replace(self, shell_allow=allow)

    def describe(self) -> str:
        """One line on what the policy allows, for a denial message."""
        editing = (f"reading files in {self.root} and editing those its files_in_scope names"
                   if self.confine else f"reading and editing files in {self.root}")
        if self.trusted:
            return (f"the {self.name} policy allows {editing}, running tests, linters and "
                    "read-only git commands there, and nothing else")
        return (f"the {self.name} policy allows {editing} and reading its git history with "
                "the git_* tools, and nothing else (no commands: the folder is not trusted)")

    def scope_text(self) -> str:
        """The task's files_in_scope as a denial lists it (at most
        `_SCOPE_SHOWN` entries)."""
        scope = list(self.extra_writable)
        if not scope:
            return "the task names no files_in_scope, so it may change no file"
        shown = ", ".join(scope[:_SCOPE_SHOWN])
        more = len(scope) - _SCOPE_SHOWN
        return f"files_in_scope: {shown}" + (f" and {more} more" if more > 0 else "")

    def scope_reason(self, value: str, p: Path) -> str | None:
        """Why a confined policy refuses to let a call write `p` (resolved;
        `value` as the call names it), or None when files_in_scope covers it
        (`in_scope`) or the policy isn't confined. What to do comes first,
        so a report's capped reason keeps it."""
        if not self.confine:
            return None
        rel = p.relative_to(self.root).as_posix() if _inside(p, (self.root,)) else ""
        if rel and in_scope(self.extra_writable, rel):
            return None
        return (f"{value!r} is not in the task's files_in_scope: change only those files, and "
                f"list this one as a concern in your hand-off instead ({self.scope_text()})")

    def untrusted_reason(self, what: str) -> str:
        """Why `what` (a command line, check_file's linter) can't run in
        the policy's untrusted root; what to do instead comes first, so a
        report's capped reason keeps it."""
        return (f"{self.root} is not a trusted folder, so the {self.name} policy runs no "
                "commands there (read the history with the git_* tools, or `evi trust` the "
                f"folder): {what} can run the folder's own code (git runs the programs its "
                ".git/config names, python -m imports from the folder, the shell finds "
                "programs there first)")

    # -- deciding ------------------------------------------------------------

    def check(
        self,
        tool: Any,
        args_json: str | dict,
        *,
        workdir: str | Path | None = None,
        protected_paths: Iterable[str] | None = None,
        dialect: str | None = None,
    ) -> Verdict:
        """Whether this policy allows `tool` (a Tool, or any object with
        ``name`` and ``category``) to run with `args_json`. `workdir` is the
        folder relative paths resolve against: the one the tool resolves
        them in (the agent's working folder; default `evi.workdir.get_cwd`,
        what a tool uses when none is set, never the root);
        `protected_paths` the protected patterns in force (default:
        `evi.permissions.effective_protected_paths` of the stock list)."""
        name = str(getattr(tool, "name", "") or "")
        category = str(getattr(tool, "category", "") or "")
        if protected_paths is None:
            protected_paths = permissions.effective_protected_paths()
        protected_paths = list(protected_paths)
        try:
            data = json.loads(args_json) if isinstance(args_json, str) else dict(args_json)
        except (json.JSONDecodeError, TypeError, ValueError, RecursionError):
            return _deny(f"{name}: its arguments don't parse")
        if not isinstance(data, dict):
            return _deny(f"{name}: its arguments are not an object")
        path_args = _path_args(tool, data, category)
        base = _base(workdir)
        expected = {**READ_TOOLS, **WRITE_TOOLS, **SHELL_TOOLS, **PURE_TOOLS}.get(name)
        if expected is None or expected != category:
            return _deny(f"{name} is not a tool the {self.name} policy allows "
                         "(no network, MCP, run_python, memory, subagents or other tools)")
        if name in PURE_TOOLS:
            return ALLOW
        call_category = permissions.classify_call(name, category, data)
        if call_category == "shell":
            command = permissions.shell_command_of(name, category, data)
            if command is None:
                return _deny(f"{name}: no command line to check")
            exec_dir = self._exec_dir(name, data, base)
            if exec_dir is None:
                return _deny(f"{name}: the folder the command would run in can't be told")
            return self.check_command(command, exec_dir=exec_dir, dialect=dialect,
                                      protected_paths=protected_paths)
        if name == "check_file" and call_category != permissions.CODE_READ:
            return _deny("check_file would run a linter that runs project code; "
                         "run the project's checks through run_command instead")
        if name == "check_file" and not self.trusted:
            # evi.codeintel starts the linter by its bare name, which Windows
            # looks up in the process's folder first.
            return _deny(self.untrusted_reason("check_file's linter"))
        if name in WRITE_TOOLS:
            return self._check_writes(path_args, base, protected_paths, scoped=True)
        return self._check_reads(name, data, path_args, base, protected_paths)

    def _exec_dir(self, name: str, data: dict, base: Path) -> Path | None:
        """The folder a shell call's line runs in (`permissions.exec_cwd_of`)."""
        cwd = permissions.exec_cwd_of(name, data)
        if cwd is None:
            return None
        return _resolve(cwd, base) if cwd else _resolve(".", base)

    def _check_reads(self, name: str, data: dict, path_args: list[str], base: Path,
                     protected_paths: Iterable[str]) -> Verdict:
        roots = (self.root, *self.read_roots)
        for value in path_args or ["."]:
            p = _resolve(value, base)
            if p is None:
                return _deny(f"{value!r}: where this path leads can't be told")
            if not _inside(p, roots):
                return _deny(f"{value!r} is outside the run's folder {self.root}")
        for g in permissions.glob_values(name, data):
            if permissions._glob_escapes(g):
                return _deny(f"the glob {g!r} reaches outside the folder it searches")
        secret = permissions.protected_read("fs", data, list(protected_paths), workdir=base,
                                            path_args=path_args)
        if secret is not None:
            return _deny(f"{secret!r} is a protected secret file ([auto] protected_paths)")
        return ALLOW

    def _never_patterns(self, protected_paths: Iterable[str]) -> list[str]:
        """Protected patterns no files_in_scope entry can grant."""
        grantable = set(GRANTABLE)
        out = [p for p in (str(x).strip() for x in protected_paths or ()) if p
               and p not in grantable]
        out += [p for p in (*HARD_PROTECTED, *permissions.director_paths()) if p not in out]
        home = permissions._evi_home_pattern()
        if home and home not in out:
            out.append(home)
        return out

    def _check_writes(self, path_args: list[str], base: Path,
                      protected_paths: Iterable[str], *, scoped: bool = False) -> Verdict:
        """Whether every one of `path_args` (relative to `base`) may be
        written: inside the root, no protected file the scope doesn't grant,
        and with `scoped` (a write tool's own paths) inside files_in_scope
        when the policy is confined (`scope_reason`)."""
        if not path_args:
            return _deny("a write with no path argument can't be checked")
        never = self._never_patterns(protected_paths)
        roots = self.write_roots or (self.root,)
        for value in path_args:
            p = _resolve(value, base)
            if p is None:
                return _deny(f"{value!r}: where this path leads can't be told")
            if not _inside(p, roots):
                return _deny(f"{value!r} is outside the run's folder {self.root}")
            target = [(value, base, value)]
            if permissions._first_protected(target, never) is not None:
                return _deny(f"{value!r} is protected and no task may write it "
                             "(.evi/tasks, .git, eVi's home, secrets, [auto] protected_paths)")
            if permissions._first_protected(target, GRANTABLE) is not None:
                rel = p.relative_to(self.root).as_posix() if _inside(p, (self.root,)) else ""
                if not rel or not any(scope_names(e, rel) for e in self.extra_writable):
                    return _deny(f"{value!r} is a protected file the task's files_in_scope "
                                 "doesn't name")
            why = self.scope_reason(value, p) if scoped else None
            if why:
                return _deny(why)
        return ALLOW

    def check_command(
        self,
        command: str,
        *,
        exec_dir: Path | None = None,
        dialect: str | None = None,
        protected_paths: Iterable[str] | None = None,
    ) -> Verdict:
        """Whether the shell line `command`, run in `exec_dir` (default: the
        root), is allowed (see the module doc)."""
        if not self.trusted:
            return _deny(self.untrusted_reason(repr(command)))
        if protected_paths is None:
            protected_paths = permissions.effective_protected_paths()
        run_in = self.root if exec_dir is None else exec_dir
        if not _inside(run_in, (self.root,)):
            return _deny(f"the command would run in {run_in}, outside the run's folder")
        sp = shellsplit.split(command, dialect)
        if not sp.ok:
            return _deny(f"the command line can't be checked: {sp.error}")
        if not sp.commands:
            return _deny("no command to run")
        for seg in sp.commands:
            if seg.depth > 0:
                return _deny(f"{seg.text!r} runs inside a command substitution or subshell")
        protected_paths = list(protected_paths)
        for seg in sp.commands:
            reason = self._segment_reason(seg, sp.dialect, run_in, protected_paths)
            if reason:
                return _deny(reason)
        if not permissions._redirects_inside(sp, self.root, str(run_in)):
            return _deny("a redirect in the line targets a file outside the run's folder")
        reason = self._redirect_scope_reason(sp, run_in)
        if reason:
            return _deny(reason)
        written = permissions.protected_write(
            "run_command", "shell", {"command": command, "cwd": str(run_in)},
            list(protected_paths) + list(HARD_PROTECTED), workdir=run_in, path_args=[],
            dialect=dialect, split=sp,
        )
        if written is not None:
            return _deny(f"the line writes the protected path {written!r}")
        return ALLOW

    def _redirect_scope_reason(self, sp: shellsplit.Split, run_in: Path) -> str:
        """Why a confined policy refuses the line's output redirects (each
        writes the file it names, which must be in files_in_scope), or ""."""
        if not self.confine:
            return ""
        for r in sp.redirects:
            if (">" not in r.op or not r.text or r.dynamic
                    or r.text.lower() in permissions._NULL_SINKS):
                continue
            p = _resolve(r.text, run_in)
            if p is None:
                return f"{r.text!r}: where this redirect leads can't be told"
            why = self.scope_reason(r.text, p)
            if why:
                return f"the line writes {r.text!r} (a redirect): {why}"
        return ""

    def _rewrite_reason(self, words: Sequence[str], run_in: Path) -> str | None:
        """Why a confined policy refuses the command `words`, which rewrites
        the files it names (`_rewritten`), or None: each must be a file in
        files_in_scope, never a folder, and it must name one."""
        if not self.confine:
            return None
        files = _rewritten(words)
        if files is None:
            return None
        if files == [_CRATE]:
            return ("cargo --fix rewrites files across the whole crate, so the task's "
                    "files_in_scope can't hold it: fix them with edit_file instead "
                    f"({self.scope_text()})")
        if not files:
            return ("it rewrites the files it is given and names none, so it would rewrite "
                    "every file in the folder it runs in: name the files instead "
                    f"({self.scope_text()})")
        for value in files:
            if any(c in value for c in _WILDCARDS):
                return (f"it rewrites every file {value!r} matches: name the files instead "
                        f"({self.scope_text()})")
            p = _resolve(value, run_in)
            if p is None:
                return f"where its argument {value!r} leads can't be told"
            if p.is_dir():
                return (f"it rewrites every file in {value!r}, a folder: name the files instead "
                        f"({self.scope_text()})")
            why = self.scope_reason(value, p)
            if why:
                return f"it rewrites {value!r}: {why}"
        return None

    def _segment_reason(self, seg: shellsplit.Segment, dialect: str, run_in: Path,
                        protected_paths: Iterable[str]) -> str:
        """Why this command segment is not allowed, or ""."""
        words = list(seg.words)
        if not words:
            return f"{seg.text!r}: nothing to run"
        if _ASSIGNMENT.match(words[0]):
            return f"{seg.text!r} sets an environment variable before the command"
        if dialect != "cmd" and ("$" in seg.text or "`" in seg.text):
            return f"{seg.text!r} holds a shell expansion"
        why = self._words_reason(words, run_in, protected_paths)
        return "" if why is None else f"{seg.text!r} is not allowed: {why}"

    def _words_reason(self, words: Sequence[str], run_in: Path,
                      protected_paths: Iterable[str]) -> str | None:
        """None if the command `words` is allowed: a `shell_allow` prefix or
        a `SHELL_RULES` entry, its arguments inside the root, but for
        read-only git no argument naming a protected file the task can't
        write (`_may_write_args`), and, confined, the files it rewrites in
        files_in_scope (`_rewrite_reason`). Else why not."""
        n = self._prefix_len(words)
        why = _builtin_reason(words, self.root, run_in)
        if n and why is not None and n == len(words):
            why = None  # an allowlisted command, run as it is
        elif n and why is not None:
            # Words after an allowlisted command would skip the builtin rule's
            # refused options (pytest --pastebin, an npm config flag, a make
            # variable), so they must pass that rule. eVi picked the prefix's
            # program (a full path from which()), so judge it by its name.
            name = shellsplit._base(words[0]).removesuffix(".cmd").removesuffix(".bat")
            rule = _builtin_reason([name, *words[1:]], self.root, run_in)
            why = None if rule is None else (
                f"only {' '.join(words[:n])!r} itself may run; the words after it are "
                f"not allowed ({rule})"
            )
        if why is None and _may_write_args(words):
            values = _arg_values(words[n or 1:])
            if values:
                verdict = self._check_writes(values, run_in, protected_paths)
                if not verdict.allowed:
                    why = f"it may write what its arguments name, and {verdict.reason}"
        if why is None:
            why = self._rewrite_reason(words, run_in)
        return why

    def _prefix_len(self, words: Sequence[str]) -> int:
        """The length of the longest `shell_allow` prefix `words` start with
        (0 for none)."""
        best = 0
        for prefix in self.shell_allow:
            if prefix and len(prefix) > best and tuple(words[:len(prefix)]) == tuple(prefix):
                best = len(prefix)
        return best

    def allows_argv(self, argv: Sequence[str], *, run_in: Path | None = None,
                    protected_paths: Iterable[str] | None = None) -> Verdict:
        """Whether a command given as `argv` (no shell: a check eVi runs
        itself) is allowed, as one segment of a line would be."""
        words = [str(w) for w in argv]
        if not words:
            return _deny("no command to run")
        if not self.trusted:
            return _deny(self.untrusted_reason(repr(" ".join(words))))
        if protected_paths is None:
            protected_paths = permissions.effective_protected_paths()
        why = self._words_reason(words, run_in or self.root, list(protected_paths))
        return ALLOW if why is None else _deny(f"{' '.join(words)!r} is not allowed: {why}")


def _path_args(tool: Any, data: dict, category: str) -> list[str]:
    """The call's path arguments (`evi.tools.base.Tool.path_args`); for
    ``monitor`` tailing a file, its target, whatever the tool object says."""
    if getattr(tool, "name", "") == "monitor":
        target = data.get("target")
        return [target] if isinstance(target, str) and data.get("kind") != "command" else []
    fn = getattr(tool, "path_args", None)
    if callable(fn):
        try:
            return [str(v) for v in fn(data)]
        except Exception:  # noqa: BLE001 — fall back to the names
            pass
    return permissions.path_values(data, category)


def _arg_values(args: Sequence[str]) -> list[str]:
    """The values in a command's arguments that may name a file: each word
    that is no option, the value of a ``--name=value`` option, a short
    option's attached value (``-ofile``), and the value in a ``key=value``
    (``-o cache_dir=x``, ``FILES=x``)."""
    out: list[str] = []
    for a in args:
        if a in ("-", "--"):
            continue
        if a.startswith("--"):
            value = a.partition("=")[2]
        elif a.startswith("-"):
            value = a[2:]
        else:
            value = a
        while value:
            out.append(value)
            value = value.partition("=")[2]
    return out


def _args_outside(args: Sequence[str], root: Path, base: Path) -> str | None:
    """Why `args` reach outside `root` (a value that names a path outside it,
    relative to `base`, the folder the command works in), or None."""
    for value in _arg_values(args):
        p = _resolve(value, base)
        if p is None:
            return f"where its argument {value!r} leads can't be told"
        if not _inside(p, (root,)):
            return f"its argument {value!r} is outside the run's folder"
    return None


def _refused_option(args: Sequence[str], refused: Sequence[str], valued: str = "",
                    abbrev: bool = False) -> str | None:
    """The first of `args` that is one of the `refused` options, or None. A
    long one matches with or without ``=value`` (and, with `abbrev`, as an
    abbreviation of one); a one-letter one also inside a cluster, up to the
    first letter in `valued` (an option taking the rest as its value)."""
    longs = [o for o in refused if o.startswith("--")]
    shorts = "".join(o[1] for o in refused if len(o) == 2 and o[0] == "-" and o[1] != "-")
    for a in args:
        if a == "--":
            return None  # what follows goes to the tests / the script
        if a.startswith("--"):
            name = a.partition("=")[0]
            if name in longs or (abbrev and len(name) > 3
                                 and any(o.startswith(name) for o in longs)):
                return a
        elif a.startswith("-") and len(a) > 1:
            for c in a[1:]:
                if c in shorts:
                    return a
                if c in valued:
                    break
    return None


def _tool_reason(tool: str, args: Sequence[str], root: Path, run_in: Path,
                 refused: Sequence[str] = (), valued: str = "",
                 abbrev: bool = False, argfiles: bool = False) -> str | None:
    """None if an allowlisted tool's `args` use none of its `refused`
    options and stay inside the root (`_args_outside`), else why not. With
    `argfiles` (pytest, ruff: each reads more arguments from the file an
    ``@file`` word names, wherever the word is) no word may start with
    ``@``: what the file holds would get past this check."""
    bad = next((a for a in args if a.startswith("@")), None) if argfiles else None
    if bad is not None:
        return (f"{tool} {bad} is not allowed: {tool} reads more arguments from that file, "
                "which the policy can't check (name the options and files directly)")
    bad = _refused_option(args, refused, valued, abbrev)
    if bad is not None:
        return f"{tool} {bad} is not allowed (it writes a file of its choosing, reaches the " \
               "network or sets config that runs a program)"
    return _args_outside(args, root, run_in)


def _npm_reason(args: Sequence[str], root: Path, run_in: Path) -> str | None:
    """None for ``npm test`` arguments that set no npm config: before
    ``--`` only `_NPM_FLAGS`; the script's own arguments after it."""
    for a in args:
        if a == "--":
            break
        if a.startswith("-") and a not in _NPM_FLAGS:
            return (f"npm test {a} is not allowed: an npm option sets npm config (its script "
                    "shell, a preload); give the script's own arguments after --")
    return _args_outside(args, root, run_in)


def _builtin_reason(words: Sequence[str], root: Path, run_in: Path) -> str | None:
    """None if `words` match a `SHELL_RULES` entry with arguments that stay
    in `root`, else why not (for a trusted root: `Policy.check_command` runs
    nothing in an untrusted one)."""
    prog = words[0]
    base = shellsplit._base(prog)
    rest = list(words[1:])
    bare = _SEPARATORS.search(prog) is None
    if base == "pytest" and bare:
        return _tool_reason("pytest", rest, root, run_in, _PYTEST_REFUSED, _PYTEST_VALUED,
                            argfiles=True)
    if base == "ruff" and bare:
        if rest[:1] not in (["check"], ["format"]):
            return "only ruff check / ruff format"
        return _tool_reason("ruff", rest[1:], root, run_in, _RUFF_REFUSED, argfiles=True)
    if _PYTHON.match(base):
        if rest[:2] == ["-m", "pytest"]:
            return _tool_reason("pytest", rest[2:], root, run_in, _PYTEST_REFUSED,
                                _PYTEST_VALUED, argfiles=True)
        if rest[:2] == ["-m", "ruff"] and rest[2:3] in (["check"], ["format"]):
            return _tool_reason("ruff", rest[3:], root, run_in, _RUFF_REFUSED, argfiles=True)
        return "python runs only -m pytest or -m ruff check|format"
    if base == "npm" and bare:
        if rest[:1] == ["test"] or rest[:2] == ["run", "test"]:
            return _npm_reason(rest[1 if rest[:1] == ["test"] else 2:], root, run_in)
        return "only npm test"
    if base == "cargo" and bare:
        if rest[:1] in (["check"], ["test"], ["clippy"]):
            return _tool_reason("cargo", rest[1:], root, run_in, _CARGO_REFUSED, _CARGO_VALUED,
                                abbrev=True)
        return "only cargo check / test / clippy"
    if base == "git" and bare:
        return _git_reason(rest, root, run_in)
    return f"{prog} is not on the allowlist ({'; '.join(SHELL_RULES)})"


def _may_write_args(words: Sequence[str]) -> bool:
    """Whether the command `words` may write what its arguments name: any
    allowed command but a read-only git one (ruff rewrites the files it is
    given, a test binary takes ``--logfile``, a check command its own)."""
    return bool(words) and shellsplit._base(words[0]) != "git"


# What `_rewritten` answers for a command that rewrites a whole cargo crate.
_CRATE = "<the crate>"


def _rewritten(words: Sequence[str]) -> list[str] | None:
    """The files the command `words` rewrites, as it names them ([] when it
    names none: ruff then rewrites the folder it runs in; [`_CRATE`] for
    ``cargo check|test|clippy --fix``), or None when it rewrites no file it
    is given. The rewriting commands: ``ruff format`` (but ``--check`` /
    ``--diff``) and ``ruff check`` with any option outside
    `_RUFF_CHECK_READS` (``--fix``, ``--add-noqa``, ``--add-ignore``, a
    ``--config``: its inline setting or file may say ``fix = true``; one a
    newer ruff adds; a one-letter one counts inside a cluster, ``-nq``; with
    ``--diff``, the fix options of `_RUFF_DIFFED` rewrite nothing), also as
    ``python -m ruff`` or a check command's full path (judged by the
    program's name). The words after an option in `_RUFF_VALUED` are its
    value, and ``-`` (stdin, written to stdout) no file."""
    if not words:
        return None
    base = shellsplit._base(words[0]).removesuffix(".cmd").removesuffix(".bat")
    rest = list(words[1:])
    if _PYTHON.match(base) and rest[:2] == ["-m", "ruff"]:
        base, rest = "ruff", rest[2:]
    if base == "cargo":
        args = rest[:rest.index("--")] if "--" in rest else rest
        fixes = any(len(n) >= 4 and "--fix".startswith(n)  # cargo takes --fi for --fix
                    for n in (a.partition("=")[0] for a in args))
        return [_CRATE] if fixes and rest[:1] in (["check"], ["test"], ["clippy"]) else None
    if base != "ruff" or rest[:1] not in (["check"], ["format"]):
        return None
    sub, args = rest[0], rest[1:]
    flags: set[str] = set()
    files: list[str] = []
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--":
            files += args[i + 1:]
            break
        if a.startswith("-") and a != "-":
            name = a.partition("=")[0]
            if name.startswith("--"):
                flags.add(name)
            else:  # one-letter options, clustered or not (-nq)
                flags.update(f"-{c}" for c in name[1:])
            if "=" not in a and name in _RUFF_VALUED:
                i += 1  # the option's value
        else:
            files.append(a)
        i += 1
    if sub == "format":
        if flags.intersection(("--check", "--diff")):
            return None
    elif flags <= (_RUFF_CHECK_READS | _RUFF_DIFFED if "--diff" in flags
                   else _RUFF_CHECK_READS):
        return None
    if "-" in files:
        files = [f for f in files if f != "-"]
        if not files:
            return None  # stdin in, stdout out
    return files


def _git_reason(rest: list[str], root: Path, run_in: Path) -> str | None:
    """None for a read-only git invocation that stays in `root`, else why not."""
    here = run_in
    i = 0
    while i < len(rest) and rest[i].startswith("-"):
        opt = rest[i]
        name, eq, value = opt.partition("=")
        if opt in _GIT_GLOBAL_FLAGS:
            i += 1
            continue
        if name in _GIT_DIR_OPTIONS:
            if not eq:
                if i + 1 >= len(rest):
                    return f"git {name} without a folder"
                value = rest[i + 1]
                i += 1
            target = _resolve(value, here)
            if target is None or not _inside(target, (root,)):
                return f"git {name} {value} points outside the run's folder"
            if name == "-C":
                here = target
            i += 1
            continue
        return f"the git option {opt} is not allowed"
    if i >= len(rest):
        return "git without a subcommand"
    sub, args = rest[i], rest[i + 1:]
    for a in args:
        name = a.partition("=")[0]
        if (a == "-c" or a.startswith(_GIT_REFUSED)
                or (name.startswith("--") and len(name) > 3
                    and any(o.startswith(name) for o in _GIT_REFUSED_LONG))):
            return f"git {sub} {a} is not allowed"
    # A path outside the repository reads it (git diff takes two such paths
    # as --no-index on its own); relative to the folder git works in.
    outside = _args_outside(args, root, here)
    if outside is not None:
        return outside
    if sub == "branch":
        if args[:1] == ["--show-current"] and len(args) == 1:
            return None
        if args[:1] == ["--list"] and not any(a.startswith("-") for a in args[1:]):
            return None
        return "only git branch --show-current / --list"
    if sub not in _GIT_READ:
        return f"git {sub} is not a read-only git command"
    return None


# ---- constructors ------------------------------------------------------------


def folder_trusted(folder: str | Path) -> bool:
    """Whether `folder` is a trusted folder (`evi.trust`; its repository,
    inside git). False when that can't be told."""
    try:
        from evi import trust

        return bool(trust.status(Path(folder)).trusted)
    except Exception:  # noqa: BLE001 — fail closed: no commands
        return False


def implementer_policy(
    root: str | Path,
    files_in_scope: Iterable[str] = (),
    checks: Iterable[Any] = (),
    *,
    trusted: bool | None = None,
    confine: bool = False,
) -> Policy:
    """The ``implementer`` policy for a run in `root` (the worktree, else the
    repository's top folder): writes confined to it, protected files only as
    `files_in_scope` names them (`scope_names`), and `checks` (argv lists,
    or `evi.checks.Check` objects) allowed as exact commands on top of
    `SHELL_RULES`. `trusted`: whether `root` is a trusted folder, so commands
    may run at all (default: `folder_trusted`). `confine`: writes must also
    be in `files_in_scope` (`in_scope`; the module doc's **Scope**), as
    `evi.tasks.run_task` builds it; else the scope only grants protected
    files (``evi run --policy implementer --scope`` without ``--confine``).
    Raises PolicyError for a bad scope entry (`validate_scope`)."""
    folder = Path(root).expanduser().resolve()
    scope = validate_scope(files_in_scope)
    commands = [tuple(getattr(c, "argv", c)) for c in checks or ()]
    ok = folder_trusted(folder) if trusted is None else bool(trusted)
    return Policy(IMPLEMENTER, folder, (folder,), scope, trusted=ok,
                  confine=bool(confine)).with_commands(commands)


# Every named policy: name -> constructor(root, files_in_scope, checks, *, trusted, confine).
# The watcher policy is not one of them: only the Companion builds it
# (`watcher_policy`, from [companion] sources), never `evi run --policy`.
POLICIES = {IMPLEMENTER: implementer_policy}


def build(name: str, root: str | Path, files_in_scope: Iterable[str] = (),
          checks: Iterable[Any] = (), *, trusted: bool | None = None,
          confine: bool = False) -> Policy:
    """The policy called `name`; PolicyError when there is none (or a scope
    entry is bad). `trusted` and `confine` as `implementer_policy` takes
    them."""
    key = str(name or "").strip().lower()
    if key not in POLICIES:
        raise PolicyError(f"unknown policy {name!r} (choose: {' | '.join(POLICIES)})")
    return POLICIES[key](root, files_in_scope, checks, trusted=trusted, confine=confine)


# ---- the watcher policy (the Companion's check-ins) --------------------------

WATCHER = "watcher"

# The built-in tools a watcher may call, with the category each must have:
# reads of the owner's own sources that write nothing and run nothing. A
# source group in [companion] sources stands for its tools.
WATCHER_SOURCE_TOOLS: dict[str, str] = {
    "calendar_today": "calendar", "calendar_week": "calendar",
    "calendar_search": "calendar", "calendar_next": "calendar",
    "recall": "memory", "list_memories": "memory", "recall_by_tag": "memory",
}
WATCHER_SOURCE_GROUPS: dict[str, tuple[str, ...]] = {
    "calendar": ("calendar_today", "calendar_week", "calendar_search", "calendar_next"),
    "memory": ("recall", "list_memories", "recall_by_tag"),
}
# An MCP source: "mcp:<server>:<tool>", the tool eVi registers as
# "<server>.<tool>" (evi.mcp.manager).
_MCP_SOURCE = re.compile(r"mcp:([A-Za-z0-9_.-]{1,64}):([^:\s]{1,128})\Z")


def watcher_sources(entries: Iterable[str]) -> dict[str, str]:
    """[companion] `sources` as the tools they allow: tool name -> the
    category it must have. An entry is a group (`WATCHER_SOURCE_GROUPS`), a
    tool of `WATCHER_SOURCE_TOOLS`, or ``mcp:<server>:<tool>`` (the MCP tool
    ``<server>.<tool>``, category "mcp"). Raises PolicyError for anything
    else: a write, shell or network tool can't be made a source."""
    out: dict[str, str] = {}
    for raw in entries or ():
        entry = str(raw or "").strip()
        if not entry:
            continue
        key = entry.lower()
        if key in WATCHER_SOURCE_GROUPS:
            for name in WATCHER_SOURCE_GROUPS[key]:
                out[name] = WATCHER_SOURCE_TOOLS[name]
            continue
        if key in WATCHER_SOURCE_TOOLS:
            out[key] = WATCHER_SOURCE_TOOLS[key]
            continue
        m = _MCP_SOURCE.match(entry)
        if m is not None:
            out[f"{m.group(1)}.{m.group(2)}"] = "mcp"
            continue
        raise PolicyError(
            f"{entry!r} is not a watcher source (choose: "
            f"{' | '.join([*WATCHER_SOURCE_GROUPS, *WATCHER_SOURCE_TOOLS])} | "
            "mcp:<server>:<tool>)")
    return out


@dataclass(frozen=True)
class WatcherPolicy:
    """The ``watcher`` policy: a Companion check-in (evi/companion/) may call
    the read-only source tools in `tools` ((name, category) pairs; a tool of
    another category under one of these names is unknown) and nothing else:
    no shell, code, file reads or writes, web, memory writes, subagents,
    scheduling or unlisted MCP tools. Set as ``Agent.policy`` it decides
    every call, after ``[auto] hard_deny``, deny rules and the director's
    ground (`evi.llm.agent.Agent._policy_verdict`).

    - root: the Companion's folder. No command or check runs there:
      `trusted` is always False and `allows_argv` denies."""

    name: str
    root: Path
    tools: tuple[tuple[str, str], ...] = ()
    trusted: bool = False

    def describe(self) -> str:
        """One line on what the policy allows, for a denial message."""
        names = ", ".join(n for n, _ in self.tools) or "none"
        return (f"the {self.name} policy allows only the configured read-only sources "
                f"({names}), and nothing else")

    def check(
        self,
        tool: Any,
        args_json: str | dict,
        *,
        workdir: str | Path | None = None,
        protected_paths: Iterable[str] | None = None,
        dialect: str | None = None,
    ) -> Verdict:
        """Whether this policy allows `tool` (a Tool, or any object with
        ``name`` and ``category``) to run with `args_json`. The keyword
        arguments are `Policy.check`'s; a watcher needs none of them."""
        name = str(getattr(tool, "name", "") or "")
        category = str(getattr(tool, "category", "") or "")
        try:
            data = json.loads(args_json) if isinstance(args_json, str) else dict(args_json)
        except (json.JSONDecodeError, TypeError, ValueError, RecursionError):
            return _deny(f"{name}: its arguments don't parse")
        if not isinstance(data, dict):
            return _deny(f"{name}: its arguments are not an object")
        expected = dict(self.tools).get(name)
        if expected is None or expected != category:
            return _deny(f"{name} is not a source the {self.name} policy allows (it only "
                         "reads the configured sources: no shell, code, files, web, memory "
                         "writes, subagents or other tools)")
        if permissions.classify_call(name, category, data) == "shell":
            return _deny(f"{name}: the {self.name} policy runs no command line")
        return ALLOW

    def allows_argv(self, argv: Sequence[str], *, run_in: Path | None = None,
                    protected_paths: Iterable[str] | None = None) -> Verdict:
        """A watcher runs no command, a check included."""
        return _deny(f"the {self.name} policy runs no commands")


def watcher_policy(sources: Iterable[str], root: str | Path) -> WatcherPolicy:
    """The ``watcher`` policy for [companion] `sources` (`watcher_sources`),
    with `root` the Companion's folder. Raises PolicyError for an entry that
    is no source."""
    tools = tuple(sorted(watcher_sources(sources).items()))
    return WatcherPolicy(WATCHER, Path(root).expanduser().resolve(), tools)
