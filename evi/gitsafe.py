"""Hardened git invocations for eVi's own git calls.

Every git subprocess eVi runs for itself — the read-only git tools, `evi
review`, worktrees, the status line, `evi sync`, the anatomy map and plugin /
skill clones — goes through `run_git`. A working copy eVi did not create (a
cloned or downloaded repo, a branch it is asked to fix) can carry a
`.git/config` that turns an ordinary `git status` into code execution, so each
call:

- overrides the repo config that runs programs during a read with `-c`
  (which beats every config file): `core.fsmonitor` (run by status / diff),
  `core.hooksPath` (pointed at an empty *file*, so no hook file runs and
  none can be installed there — git-lfs writes its hooks into whatever
  hooks directory git reports; git can't tell the repo's hooks from yours,
  so this also skips your own global post-checkout hook during
  `evi worktree create`), `diff.external`, `diff.submodule` (kept "short",
  so diff / log / show never start a diff inside a submodule), `core.pager`
  and `log.showSignature` (which runs `gpg.program` on `log` / `show`);
- switches off the filter drivers (`filter.<name>.clean/smudge/process`,
  run whenever git reads or checks out a work-tree file: status, diff,
  blame, worktree add) and config-defined hooks (`hook.<name>.command` /
  `.event`) that the *repository's* config defines — local or worktree
  scope, includes too — and the config of every checked-out submodule (and
  theirs), since status / diff start a git inside each one and the `-c`
  settings reach it. One `git config --show-scope` lookup per repository
  finds them, plus one `ls-files` per repository for its submodules.
  Drivers and hooks from your global or system config (Git LFS, say) are
  left alone; a filter set up only in a repo's `.git/config` (git-crypt,
  `nbstripout --install`, `git lfs install --local`) does NOT run in eVi's
  own git calls — define it globally to keep it. A git older than 2.26
  can't report scopes, so there every driver and hook is switched off,
  global ones too. A name `-c` can't express (one containing "=") makes
  `run_git` refuse instead (UnsafeRepoError). Subcommands that never convert
  work-tree files or run hooks (rev-parse, config, clone, ...) skip the
  lookup;
- keeps a trusted Git LFS from running what the repository names: git-lfs
  reads the repository's config too, and some of its keys are programs.
  The same lookup blanks the ones a repository scope sets —
  `lfs.extension.<n>.clean/smudge` (git then fails instead of running
  them), `lfs.customtransfer.<n>.path`, `lfs.standalonetransferagent`,
  `core.sshCommand`, `core.askPass` — and resets the credential helper list
  when it names a helper. GIT_LFS_SKIP_SMUDGE=1 means a checkout (`evi
  worktree create`) writes LFS pointer files and never downloads, so no
  transfer agent, ssh or credential helper runs at all: run `git lfs pull`
  in the new worktree for the real files. `clone` (plugins, skills) is the
  exception — its new repository's config holds nothing the remote chose;
- adds `--no-ext-diff --no-textconv` to diff / log / show and
  `--no-textconv` to blame, so `diff.<driver>.command` / `.textconv`
  drivers wired up through gitattributes never run;
- drops inherited environment that points git at another repository or at
  a program (GIT_DIR, GIT_WORK_TREE, GIT_INDEX_FILE, GIT_EXTERNAL_DIFF,
  GIT_PAGER, GIT_EXEC_PATH, ... — set when eVi itself runs from a git hook).
  A command the user runs in their own shell (`evi review`) passes
  `inherit_repo_env=True` to keep the repository-locating ones, as plain git
  would: in a pre-commit hook during `git commit -a`, GIT_INDEX_FILE names the
  index actually being committed, and `evi review --staged` must diff that;
- sets GIT_OPTIONAL_LOCKS=0 (inspection never rewrites the index) and, unless
  the caller is an interactive command, GIT_TERMINAL_PROMPT=0 so a credential
  prompt fails fast instead of hanging a server thread;
- runs the `git` found on an absolute PATH entry, never one in the current
  directory (by default Windows process creation and `shutil.which` search
  it before PATH, so a repo shipping its own git.exe would otherwise run).

System config (GIT_CONFIG_NOSYSTEM) is deliberately left alone: it is
admin-owned, not repo-controlled, and on Git for Windows it carries
`core.autocrlf`, `credential.helper` and `http.sslBackend` — dropping it makes
clean files show as modified and breaks authenticated pushes.

The callers that read the work tree (the git tools, `evi review`, worktrees)
also check that the work tree git reports for the directory they were given
is the repository's own — the nearest directory at or above it holding a
`.git` entry (`is_own_work_tree`) — so a hostile `core.worktree` is refused
whether it points elsewhere or at an ancestor such as your home directory,
and git_diff refuses `paths` outside the repository (which would make git
diff them as `--no-index`).

Revisions that come from a model, an MCP client or a user go through
`validate_ref` / `rev_args`: a leading "-" is refused outright (git would read
`--output=<file>` as "write the diff there" and `--no-index` as "diff two
arbitrary files"), and `--end-of-options` is added when git understands it
(2.24+). The validation is what actually protects older gits — and newer
ones too: `git diff` scans for `--no-index` before it honours
`--end-of-options`.
"""

from __future__ import annotations

import atexit
import functools
import os
import re
import stat
import subprocess
import tempfile
import threading
from collections.abc import Sequence
from pathlib import Path

# First git that understands `--end-of-options`.
END_OF_OPTIONS_VERSION = (2, 24)


class GitNotFoundError(FileNotFoundError):
    """No usable `git` executable on PATH."""


class UnsafeRefError(ValueError):
    """A revision / ref / branch argument git could read as something else."""


class UnsafeRepoError(OSError):
    """The repository's config could not be neutralised, so git was not run.

    An OSError so callers' existing "git could not be run" handling applies.
    """


# Shown when a directory is not inside the work tree git reports for it.
NOT_IN_WORK_TREE = (
    "not inside a git work tree: a bare repository, a .git directory, or a "
    "repository whose own config (core.worktree) puts its work tree somewhere "
    "other than the directory holding its .git (an ancestor included)"
)


# ---- executable ---------------------------------------------------------


@functools.lru_cache(maxsize=8)
def _find_git(path_env: str) -> str | None:
    names = ("git.exe",) if os.name == "nt" else ("git",)
    for entry in path_env.split(os.pathsep):
        d = entry.strip().strip('"')
        # Relative entries ("", ".") would resolve against whatever directory
        # eVi happens to be in — exactly the planted-binary case.
        if not d or not os.path.isabs(d):
            continue
        for name in names:
            cand = os.path.join(d, name)
            if os.path.isfile(cand) and os.access(cand, os.X_OK):
                return cand
    return None


def git_executable() -> str | None:
    """Absolute path of the git to run, or None when there is none."""
    return _find_git(os.environ.get("PATH", ""))


# ---- environment --------------------------------------------------------

# Inherited variables that point git at a repository / index / object store.
# A git hook or alias that launches eVi exports several of these.
_REPO_ENV = frozenset({
    "GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_COMMON_DIR", "GIT_NAMESPACE",
    "GIT_PREFIX",
})
# ...and ones that name a program for git to run. Always dropped.
_PROGRAM_ENV = frozenset({"GIT_EXTERNAL_DIFF", "GIT_PAGER", "GIT_EXEC_PATH"})
_SCRUB_ENV = _REPO_ENV | _PROGRAM_ENV


def git_env(*, prompt: bool = False, inherit_repo_env: bool = False) -> dict[str, str]:
    """The environment for a hardened git child process.

    `inherit_repo_env=True` keeps GIT_DIR / GIT_INDEX_FILE / ... — only for a
    command the user runs in their own shell, where they mean "this repo, as
    git sees it here" (e.g. `evi review --staged` from a pre-commit hook).
    eVi's agent and MCP tools and worktree operations never inherit them.
    """
    scrub = _PROGRAM_ENV if inherit_repo_env else _SCRUB_ENV
    env = {k: v for k, v in os.environ.items() if k.upper() not in scrub}
    env["GIT_OPTIONAL_LOCKS"] = "0"
    if not prompt:
        env["GIT_TERMINAL_PROMPT"] = "0"
    return env


# ---- the hooks path that holds no hooks ---------------------------------

_hooks_lock = threading.Lock()
_hooks_path: str | None = None


def _usable_hooks_file(path: str) -> bool:
    """A regular (not linked) file we own.

    As `core.hooksPath` it holds no hooks, and nothing can add any: git-lfs
    installs its hooks into the hooks directory git reports (into an empty
    directory it would, and git would then run its post-checkout), but
    nobody can create files inside a file. Re-checked on every call: if a
    temp cleaner removes it, a directory someone recreates under that name
    must not be used.
    """
    try:
        st = os.lstat(path)
        if not stat.S_ISREG(st.st_mode):
            return False
        return not (hasattr(os, "getuid") and st.st_uid != os.getuid())
    except OSError:
        return False


def _remove_quietly(path: str) -> None:
    try:
        if _usable_hooks_file(path):
            os.remove(path)
    except OSError:
        pass


def no_hooks_path() -> str:
    """Path of a private empty file to use as `core.hooksPath`."""
    global _hooks_path
    with _hooks_lock:
        if _hooks_path is None or not _usable_hooks_file(_hooks_path):
            fd, _hooks_path = tempfile.mkstemp(prefix="evi-nohooks-")
            os.close(fd)
            atexit.register(_remove_quietly, _hooks_path)
        return _hooks_path


# ---- argv ---------------------------------------------------------------

# Flags added right after these subcommands so configured diff drivers
# (diff.<name>.command / .textconv via gitattributes) never run.
_SUBCOMMAND_FLAGS: dict[str, tuple[str, ...]] = {
    "diff": ("--no-ext-diff", "--no-textconv"),
    "log": ("--no-ext-diff", "--no-textconv"),
    "show": ("--no-ext-diff", "--no-textconv"),
    "blame": ("--no-textconv",),
}


def _hardening_config(run_hooks: bool) -> list[str]:
    cfg = [
        "-c", "core.fsmonitor=",
        "-c", "diff.external=",
        "-c", "diff.submodule=short",
        "-c", "core.pager=cat",
        "-c", "log.showSignature=false",
    ]
    if not run_hooks:
        cfg += ["-c", f"core.hooksPath={Path(no_hooks_path()).as_posix()}"]
    return cfg


def _subcommand_index(args: Sequence[str]) -> int:
    """Index of the subcommand in `args` (len(args) when there is none)."""
    i = 0
    # Skip leading global options; -C and -c take a separate value.
    while i < len(args) and args[i].startswith("-"):
        i += 2 if args[i] in ("-C", "-c") else 1
    return i


def _with_subcommand_flags(args: Sequence[str]) -> list[str]:
    i = _subcommand_index(args)
    if i >= len(args):
        return list(args)
    extra = _SUBCOMMAND_FLAGS.get(args[i], ())
    return [*args[: i + 1], *extra, *args[i + 1:]]


def git_argv(
    args: Sequence[str], *, run_hooks: bool = False, extra_config: Sequence[str] = (),
) -> list[str]:
    """The full hardened argv for `git <args>`. Raises GitNotFoundError.

    `extra_config` is more `-c key=value` pairs (see `run_git`, which adds
    the ones that switch off the repository's filter drivers and hooks).
    """
    exe = git_executable()
    if exe is None:
        raise GitNotFoundError("git not found on PATH")
    return [
        exe, *_hardening_config(run_hooks), *extra_config, *_with_subcommand_flags(args),
    ]


# ---- the repository's own filter drivers, hooks and program settings --

# Config keys that make git run a program while it reads / checks out work
# tree files (filter drivers) or on an event (config-defined hooks), plus the
# sections holding the settings a trusted git-lfs, or git's transport and
# credential layer, runs as a program (picked out in `_neutralising_config`:
# git-lfs lowercases the subsection too, which `--get-regexp` can't). There is
# no one key that turns them all off, so each name is looked up per call.
# `.*`, not `.+`: `[filter ""]` is a driver too, picked by `filter=`.
_PROGRAM_KEYS_RE = (
    r"^(filter\..*\.(clean|smudge|process)|hook\..*\.(command|event)"
    r"|lfs\..*|core\.(sshcommand|askpass)|credential\..*)$"
)

# Scopes owned by the user or the machine, not by the repository.
_TRUSTED_SCOPES = frozenset({"system", "global", "command"})

# Subcommands that never convert work-tree files or run hooks, so they skip
# the lookup (rev-parse is the status line's hot path). `clone` makes a new
# repository whose config holds nothing the remote chose.
_NO_REPO_PROGRAMS = frozenset({
    "config", "rev-parse", "version", "clone", "symbolic-ref", "ls-remote",
})

# Most nested repositories (submodules, theirs, ...) looked up for one call;
# beyond that `run_git` refuses rather than leave one unchecked.
_MAX_SUBMODULES = 64


def _config_lookup(
    lead: Sequence[str], scoped: bool, cwd: str | None, timeout: float | None,
    env: dict[str, str],
) -> subprocess.CompletedProcess[bytes]:
    opts = ["--show-scope"] if scoped else []
    return subprocess.run(
        git_argv([*lead, "config", *opts, "-z", "--get-regexp", _PROGRAM_KEYS_RE]),
        cwd=cwd, capture_output=True, timeout=timeout, env=env,
    )


def _repo_program_keys(
    lead: Sequence[str], cwd: str | None, timeout: float | None, env: dict[str, str],
) -> list[tuple[str | None, str]]:
    """`(scope, key)` for every key `_PROGRAM_KEYS_RE` matches that git sees here.

    The scope is None when this git predates `--show-scope` (2.26); such keys
    are all treated as the repository's. Raises UnsafeRepoError when the
    config can't be read or a key can't be overridden from the command line.
    """
    scoped = True
    r = _config_lookup(lead, True, cwd, timeout, env)
    if r.returncode == 129:  # usage error: no --show-scope in this git
        scoped = False
        r = _config_lookup(lead, False, cwd, timeout, env)
    if r.returncode == 1 and not r.stdout:
        return []  # nothing configured
    if r.returncode != 0:
        detail = r.stderr.decode("utf-8", "replace").strip() or f"exit {r.returncode}"
        raise UnsafeRepoError(f"could not read the repository's git config: {detail}")
    try:
        text = r.stdout.decode("utf-8")
    except UnicodeDecodeError as exc:
        # A name that is not UTF-8 can't be passed back to git intact (and
        # not at all on Windows), so it could not be switched off.
        raise UnsafeRepoError(
            "the repository's git config names a filter driver or hook that is "
            "not valid UTF-8; refusing to run git there"
        ) from exc
    fields = text.split("\0")
    if fields and fields[-1] == "":
        fields.pop()
    # -z prints "<scope>\0<key>\n<value>\0" (no scope without --show-scope).
    if scoped:
        pairs = zip(fields[0::2], fields[1::2])
        return [(scope, rec.split("\n", 1)[0]) for scope, rec in pairs]
    return [(None, rec.split("\n", 1)[0]) for rec in fields]


def _lfs_program_key(key: str) -> bool:
    """Whether git-lfs runs `key`'s value as a program (or picks one by it)."""
    low = key.lower()
    return (
        (low.startswith("lfs.extension.") and low.endswith((".clean", ".smudge")))
        or (low.startswith("lfs.customtransfer.") and low.endswith(".path"))
        or low == "lfs.standalonetransferagent"
    )


def _neutralising_config(keys: Sequence[tuple[str | None, str]]) -> list[str]:
    """`-c` pairs switching off every filter driver, config hook and program
    setting that a repository scope (not system / global / command line) sets."""
    filters: set[str] = set()
    hooks: set[str] = set()
    blank: set[str] = set()  # whole keys, overridden with an empty value
    reset_helpers = False
    for scope, key in keys:
        if scope in _TRUSTED_SCOPES:
            continue
        section, _, rest = key.partition(".")
        section = section.lower()
        if section == "filter":
            filters.add(rest.rpartition(".")[0])
        elif section == "hook":
            hooks.add(rest.rpartition(".")[0])
        elif section == "credential":
            reset_helpers = reset_helpers or key.lower().endswith(".helper")
        elif section == "core" or _lfs_program_key(key):
            # core.sshCommand / core.askPass; an emptied lfs.extension makes
            # git-lfs (and so git) fail rather than run anything.
            blank.add(key)
    bad = sorted(n for n in filters | hooks | blank if "=" in n)
    if bad:
        # `-c` splits at the first "=", so such a name can't be overridden.
        raise UnsafeRepoError(
            f"the repository's git config defines a filter driver, hook or "
            f"program setting named {bad[0]!r}, which can't be switched off; "
            "refusing to run git there"
        )
    cfg: list[str] = []
    for d in sorted(filters):
        cfg += [
            "-c", f"filter.{d}.clean=", "-c", f"filter.{d}.smudge=",
            "-c", f"filter.{d}.process=", "-c", f"filter.{d}.required=false",
        ]
    for h in sorted(hooks):
        cfg += ["-c", f"hook.{h}.enabled=false", "-c", f"hook.{h}.event="]
    for k in sorted(blank):
        cfg += ["-c", f"{k}="]
    if reset_helpers:
        # An empty value empties the helper list, the URL-specific
        # `credential.<url>.helper` ones read before it included.
        cfg += ["-c", "credential.helper="]
    return cfg


def _lead_dir(lead: Sequence[str], cwd: str | None) -> str:
    """The directory `git <lead> ...` started in `cwd` works in."""
    here = os.path.abspath(cwd or os.getcwd())
    i = 0
    while i < len(lead):
        if lead[i] in ("-C", "-c") and i + 1 < len(lead):
            if lead[i] == "-C":
                here = os.path.join(here, lead[i + 1])
            i += 2
        else:
            i += 1
    return here


def _checked_out_submodules(
    lead: Sequence[str], cwd: str | None, timeout: float | None, env: dict[str, str],
) -> list[str]:
    """Absolute paths of the gitlinks (submodules, or a repository committed
    inside this one) whose directory holds a `.git`: status / diff start a
    git in each, which reads that repository's config."""
    r = subprocess.run(
        git_argv([*lead, "ls-files", "--stage", "-z", "--", ":/"]),
        cwd=cwd, capture_output=True, timeout=timeout, env=env,
    )
    if r.returncode != 0:
        return []  # no work tree (a bare repository): nothing to start a git in
    here = _lead_dir(lead, cwd)
    found: list[str] = []
    for rec in r.stdout.split(b"\0"):
        meta, tab, path = rec.partition(b"\t")
        if tab and meta.startswith(b"160000 "):
            sub = os.path.normpath(os.path.join(here, os.fsdecode(path)))
            if sub not in found and os.path.lexists(os.path.join(sub, ".git")):
                found.append(sub)
    return found


def repo_program_overrides(
    args: Sequence[str],
    cwd: str | os.PathLike[str] | None = None,
    timeout: float | None = None,
    *,
    inherit_repo_env: bool = False,
) -> list[str]:
    """The `-c` pairs `run_git` adds to `git <args>` (run in `cwd`) to switch
    off the filter drivers, config hooks and program settings of the
    repository and of its checked-out submodules; [] for a subcommand that
    runs none of them. Raises UnsafeRepoError (fail closed)."""
    i = _subcommand_index(args)
    if i >= len(args) or args[i] in _NO_REPO_PROGRAMS:
        return []
    where = os.fspath(cwd) if cwd is not None else None
    env = git_env(inherit_repo_env=inherit_repo_env)
    lead = list(args[:i])
    keys = _repo_program_keys(lead, where, timeout, env)
    # The git that status / diff start inside a submodule reads that
    # submodule's config, not this one's, but inherits these -c pairs
    # (through GIT_CONFIG_PARAMETERS) — so the names every nested repository
    # defines are switched off here too. That git never inherits GIT_DIR /
    # GIT_INDEX_FILE, and neither does its lookup.
    sub_env = git_env()
    todo = _checked_out_submodules(lead, where, timeout, env)
    seen: set[str] = set()
    while todo:
        sub = todo.pop()
        if sub in seen:
            continue
        seen.add(sub)
        if len(seen) > _MAX_SUBMODULES:
            raise UnsafeRepoError(
                f"more than {_MAX_SUBMODULES} nested submodules to check; "
                "refusing to run git there"
            )
        sub_lead = [*lead, "-C", sub]
        keys += _repo_program_keys(sub_lead, where, timeout, sub_env)
        todo += _checked_out_submodules(sub_lead, where, timeout, sub_env)
    return _neutralising_config(keys)


def run_git(
    args: Sequence[str],
    cwd: str | os.PathLike[str] | None = None,
    timeout: float | None = None,
    *,
    prompt: bool = False,
    run_hooks: bool = False,
    inherit_repo_env: bool = False,
) -> subprocess.CompletedProcess[str]:
    """Run `git <args>` hardened; returns the CompletedProcess (text output).

    `prompt=True` keeps git's terminal credential prompt (interactive CLI
    commands only). `run_hooks=True` trusts the repository's own config — its
    hooks, filter drivers and LFS downloads run — so it is only for a
    repository eVi created itself (`evi sync`'s home repo), where the user's
    own global hooks (e.g. a secret scanner) should still run.
    `inherit_repo_env=True` keeps an inherited GIT_DIR / GIT_INDEX_FILE / ...
    (see `git_env`).

    Raises GitNotFoundError, UnsafeRepoError, subprocess.TimeoutExpired or
    OSError; a non-zero exit is left to the caller.
    """
    extra = [] if run_hooks else repo_program_overrides(
        args, cwd, timeout, inherit_repo_env=inherit_repo_env,
    )
    env = git_env(prompt=prompt, inherit_repo_env=inherit_repo_env)
    i = _subcommand_index(args)
    if not run_hooks and not (i < len(args) and args[i] == "clone"):
        # A checkout writes LFS pointer files instead of downloading, so no
        # transfer agent, ssh or credential helper the repository names runs.
        env["GIT_LFS_SKIP_SMUDGE"] = "1"
    return subprocess.run(
        git_argv(args, run_hooks=run_hooks, extra_config=extra),
        cwd=os.fspath(cwd) if cwd is not None else None,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        env=env,
    )


# ---- whose work tree ----------------------------------------------------


def is_own_work_tree(
    cwd: str | os.PathLike[str] | None, prefix: str, *, inherit_repo_env: bool = False,
) -> bool:
    """Whether the work tree git reported for `cwd` is the repository's own.

    `prefix` is git's `rev-parse --show-prefix` there (once
    `--is-inside-work-tree` said "true"). The top of the work tree, that many
    levels above `cwd`, must be the nearest directory at or above `cwd` that
    holds a `.git` entry — as in a clone, a linked worktree or a submodule.
    A repository whose own config sets `core.worktree` to another directory
    fails, an ancestor such as your home directory included (status / diff
    would otherwise reach every file under it). With `inherit_repo_env` and a
    GIT_DIR / GIT_WORK_TREE in the environment, the user picked the
    repository and work tree themselves, so that passes.
    """
    if inherit_repo_env and any(
        k.upper() in ("GIT_DIR", "GIT_WORK_TREE") for k in os.environ
    ):
        return True
    try:
        here = Path(cwd or os.getcwd()).resolve()
    except OSError:
        return False
    levels = len([part for part in prefix.split("/") if part])
    for n, cand in enumerate((here, *here.parents)):
        if os.path.lexists(cand / ".git"):
            return n == levels
    return False


# ---- version ------------------------------------------------------------

_VERSION_RE = re.compile(r"git version (\d+)\.(\d+)(?:\.(\d+))?")


def parse_version(text: str) -> tuple[int, int, int] | None:
    """`git version 2.45.1.windows.1` -> (2, 45, 1); None if unrecognised."""
    m = _VERSION_RE.search(text or "")
    if not m:
        return None
    return int(m.group(1)), int(m.group(2)), int(m.group(3) or 0)


@functools.lru_cache(maxsize=8)
def _version_of(exe: str) -> tuple[int, int, int] | None:
    try:
        r = subprocess.run(
            [exe, "--version"], capture_output=True, text=True,
            timeout=10, env=git_env(),
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return parse_version(r.stdout)


def git_version() -> tuple[int, int, int] | None:
    """(major, minor, patch) of the git `run_git` uses, or None if unknown."""
    exe = git_executable()
    return _version_of(exe) if exe else None


def end_of_options() -> list[str]:
    """`["--end-of-options"]` when git understands it, else `[]`.

    An unknown version gets `[]`: an old git would read the marker itself as
    a bad revision, and `validate_ref` already keeps options out.
    """
    v = git_version()
    return ["--end-of-options"] if v is not None and v >= END_OF_OPTIONS_VERSION else []


# ---- ref validation -----------------------------------------------------

_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


def validate_ref(ref: str, *, what: str = "ref") -> str:
    """Return `ref` stripped, or raise UnsafeRefError.

    Accepts anything git accepts as a revision or range (HEAD~3, main..topic,
    a...b, :/message); refuses an empty value, a leading "-" (an option, e.g.
    `--output=<file>` or `--no-index`) and control characters.
    """
    r = (ref or "").strip()
    if not r:
        raise UnsafeRefError(f"{what} is empty")
    if r.startswith("-"):
        raise UnsafeRefError(
            f"{what} {r!r} starts with '-', so git would read it as an option "
            "(spell a ref that really starts with '-' as refs/heads/<name>)"
        )
    if _CONTROL.search(r):
        raise UnsafeRefError(f"{what} {r!r} contains a control character")
    return r


def rev_args(*refs: str, what: str = "ref") -> list[str]:
    """Validated revisions, preceded by `--end-of-options` where supported."""
    safe = [validate_ref(r, what=what) for r in refs]
    return [*end_of_options(), *safe] if safe else []


_BAD_BRANCH_CHARS = re.compile(r"[\x00-\x20\x7f~^:?*\[\\]")


def validate_branch_name(name: str) -> str:
    """Return `name` if it is a valid new branch name, else raise UnsafeRefError.

    Mirrors `git check-ref-format --branch` without spawning git: no leading
    "-", no "..", "@{", spaces, control or ~^:?*[\\ characters, no empty or
    dot-led path components, no trailing "/", "." or ".lock", and not "HEAD"
    or "@". That also makes the name safe to use as a directory name.
    """
    n = validate_ref(name, what="branch name")
    problem = None
    if n in ("HEAD", "@"):
        problem = "is reserved"
    elif ".." in n or "@{" in n or _BAD_BRANCH_CHARS.search(n):
        problem = "contains '..', '@{', whitespace or one of ~^:?*[\\"
    elif n.startswith("/") or n.endswith(("/", ".")):
        problem = "starts with '/' or ends with '/' or '.'"
    elif any(not part or part.startswith(".") or part.endswith(".lock")
             for part in n.split("/")):
        problem = "has an empty, dot-led or .lock path component"
    if problem:
        raise UnsafeRefError(f"invalid branch name {n!r}: it {problem}")
    return n
