"""Named policies for unattended task runs: v0 has one, ``implementer``.

An unattended run (``evi run``, a delegated task) has nobody to approve a
tool call, so without a policy everything outside ``[auto] auto_approve`` is
denied: no shell, so no tests. The escape hatches (``-y``, ``[auto]
unattended_policy = "allow"``) allow everything. The ``implementer`` policy is
the narrow middle: what an implementer needs to change code and prove it works
in the one folder it was given, and nothing else.

Selected by ``evi run --policy implementer`` (``--scope GLOB`` adds the task's
``files_in_scope``) and by ``evi task start`` (`evi.tasks.run_task`), both
through `implementer_policy`. Set as ``Agent.policy``, it replaces the permission
flow: every tool call is allowed or denied, never asked about, whatever
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
  ``apply_patch``, except protected files (below).
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
  cargo's ``--config`` and ``-Z``, and any npm option before ``--`` but
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
a folder argument covers what is in it (``ruff format .`` may rewrite a
protected ``.py`` file under ``.evi/``), and a tool's own cache or temporary
files land where its configuration in the folder says. The environment scrub
(no ``*_KEY`` / ``*_TOKEN`` / ``*_SECRET`` variables for an unattended run,
`evi.proc.scrubbed_env`) limits what it can take; the worktree (``evi run
--worktree``) is the blast radius.
"""

from __future__ import annotations

import fnmatch
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
      globs); they matter for protected files only (`scope_names`).
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

    # -- building ----------------------------------------------------------

    def with_commands(self, commands: Iterable[Sequence[str]]) -> "Policy":
        """This policy with `commands` (argv lists) added to shell_allow."""
        extra = [tuple(str(w) for w in c) for c in commands or () if c]
        allow = tuple(dict.fromkeys([*self.shell_allow, *extra]))
        return replace(self, shell_allow=allow)

    def describe(self) -> str:
        """One line on what the policy allows, for a denial message."""
        if self.trusted:
            return (f"the {self.name} policy allows reading and editing files in {self.root}, "
                    "running tests, linters and read-only git commands there, and nothing else")
        return (f"the {self.name} policy allows reading and editing files in {self.root} and "
                "reading its git history with the git_* tools, and nothing else (no commands: "
                "the folder is not trusted)")

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
            return self._check_writes(path_args, base, protected_paths)
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
                      protected_paths: Iterable[str]) -> Verdict:
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
        written = permissions.protected_write(
            "run_command", "shell", {"command": command, "cwd": str(run_in)},
            list(protected_paths) + list(HARD_PROTECTED), workdir=run_in, path_args=[],
            dialect=dialect, split=sp,
        )
        if written is not None:
            return _deny(f"the line writes the protected path {written!r}")
        return ALLOW

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
        a `SHELL_RULES` entry, its arguments inside the root, and, but for
        read-only git, no argument naming a protected file the task can't
        write (`_may_write_args`). Else why not."""
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
                 abbrev: bool = False) -> str | None:
    """None if an allowlisted tool's `args` use none of its `refused`
    options and stay inside the root (`_args_outside`), else why not."""
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
        return _tool_reason("pytest", rest, root, run_in, _PYTEST_REFUSED, _PYTEST_VALUED)
    if base == "ruff" and bare:
        if rest[:1] not in (["check"], ["format"]):
            return "only ruff check / ruff format"
        return _tool_reason("ruff", rest[1:], root, run_in, _RUFF_REFUSED)
    if _PYTHON.match(base):
        if rest[:2] == ["-m", "pytest"]:
            return _tool_reason("pytest", rest[2:], root, run_in, _PYTEST_REFUSED,
                                _PYTEST_VALUED)
        if rest[:2] == ["-m", "ruff"] and rest[2:3] in (["check"], ["format"]):
            return _tool_reason("ruff", rest[3:], root, run_in, _RUFF_REFUSED)
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
) -> Policy:
    """The ``implementer`` policy for a run in `root` (the worktree, else the
    repository's top folder): writes confined to it, protected files only as
    `files_in_scope` names them (`scope_names`), and `checks` (argv lists,
    or `evi.checks.Check` objects) allowed as exact commands on top of
    `SHELL_RULES`. `trusted`: whether `root` is a trusted folder, so commands
    may run at all (default: `folder_trusted`). Raises PolicyError for
    a bad scope entry (`validate_scope`)."""
    folder = Path(root).expanduser().resolve()
    scope = validate_scope(files_in_scope)
    commands = [tuple(getattr(c, "argv", c)) for c in checks or ()]
    ok = folder_trusted(folder) if trusted is None else bool(trusted)
    return Policy(IMPLEMENTER, folder, (folder,), scope, trusted=ok).with_commands(commands)


# Every named policy: name -> constructor(root, files_in_scope, checks, *, trusted).
POLICIES = {IMPLEMENTER: implementer_policy}


def build(name: str, root: str | Path, files_in_scope: Iterable[str] = (),
          checks: Iterable[Any] = (), *, trusted: bool | None = None) -> Policy:
    """The policy called `name`; PolicyError when there is none (or a scope
    entry is bad). `trusted` as `implementer_policy` takes it."""
    key = str(name or "").strip().lower()
    if key not in POLICIES:
        raise PolicyError(f"unknown policy {name!r} (choose: {' | '.join(POLICIES)})")
    return POLICIES[key](root, files_in_scope, checks, trusted=trusted)
