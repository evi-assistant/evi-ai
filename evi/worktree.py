"""Thin git-worktree wrapper.

Pairs nicely with `evi chat`: spin up a worktree for a branch, drop into
chat there, work on it in isolation, then either fold the branch back into
main or `evi worktree remove` it. The path layout is
`<repo>/.worktrees/<branch>/` by default so worktrees are siblings of the
main checkout rather than scattered across the filesystem.

The functions in this module raise `WorktreeError` on git failures; the
CLI layer translates them into clean error output.

git runs hardened (evi.gitsafe): a hostile `.git/config` can't run programs
through fsmonitor, diff drivers, the filter drivers and `hook.<name>.*`
hooks the repository's (or a submodule's) own config defines, or the
programs it names for Git LFS, and `core.hooksPath` points at an empty file,
so `git worktree add` here does NOT run the repo's (or your global, or Git
LFS's) post-checkout hook file. Run it by hand in the new worktree if you
rely on one. Git LFS files are checked out as pointer files (no download);
run `git lfs pull` in the new worktree for their contents. Branch and base
names are validated before they reach git, and a repository whose own
`core.worktree` moves its work tree away from the directory holding its
`.git` (somewhere else, or up to an ancestor) is refused.

Isolated runs (`evi run --worktree`, `evi team run --worktree`, a workflow
step or subagent profile with `isolate = "worktree"`) open their worktree with
`open_isolated` — a branch you name, or "auto" for a fresh `fix-<task>[-n]` —
which keeps it `git worktree lock`ed until the run calls `release`, and
describe what they left there with `worktree_report`. Nothing is ever merged
back: the branch waits for review. `prune_worktrees` removes only managed
worktrees whose work is safe elsewhere (clean, unlocked, merged, holding no
other worktree), and a locked worktree is never deleted, by `remove_worktree`
either. Managed worktrees always live under the repository's MAIN worktree
(`managed_root`), also when started from inside a linked one, so they never
nest.
"""

from __future__ import annotations

import os
import re
import shutil
import threading
from dataclasses import dataclass, field
from pathlib import Path, PurePath
from typing import Any

from evi import gitsafe


class WorktreeError(RuntimeError):
    """Surfaced when a git command fails or git itself is missing."""


@dataclass(frozen=True)
class WorktreeEntry:
    path: Path
    branch: str | None  # detached HEAD entries have no branch
    head: str           # short sha
    locked: bool = False  # `git worktree lock`ed (as git lists it)


# ---- helpers ------------------------------------------------------------


def _git(*args: str, cwd: Path | None = None) -> str:
    try:
        proc = gitsafe.run_git(args, cwd=cwd)
    except gitsafe.GitNotFoundError as exc:
        raise WorktreeError("git not found on PATH") from exc
    except gitsafe.UnsafeRepoError as exc:
        raise WorktreeError(str(exc)) from exc
    if proc.returncode != 0:
        raise WorktreeError(
            (proc.stderr or proc.stdout or f"git {' '.join(args)} failed").strip()
        )
    return proc.stdout


# An MSYS2/Cygwin/Git-Bash git on Windows prints POSIX paths instead of native
# ones, and Path() keeps them verbatim — the result blows up as a subprocess
# cwd with NotADirectoryError (WinError 267). Users hit this whenever such a
# git shadows Git for Windows on PATH (devkitPro, Cygwin, an msys2 setup).
#
# String rewriting alone CANNOT fix this: msys maps drives through a
# user-editable mount table, so C:\proj may print as "/c/proj" but
# C:\Users\me\proj prints as "/home/me/proj". So we rewrite the common drive
# forms, then VERIFY the result exists and fall back to walking up for .git —
# which is always native and needs no translation.
_MSYS_DRIVE = re.compile(r"^/(?:cygdrive/)?([a-zA-Z])(?:/(.*))?$")


def _on_windows() -> bool:
    """Indirection so tests can exercise the Windows branch on any platform.

    (Patching os.name directly would also change what pathlib constructs, and
    instantiating a WindowsPath on Linux raises UnsupportedOperation.)
    """
    return os.name == "nt"


def _normalize_git_path(raw: str) -> str:
    """Rewrite the common msys drive forms. Pure string work, testable anywhere."""
    raw = raw.strip()
    if not _on_windows():
        # Left alone off Windows: "/c/foo" is a legitimate POSIX path there.
        return raw
    m = _MSYS_DRIVE.match(raw)
    if not m:
        return raw
    drive, rest = m.group(1), m.group(2) or ""
    return f"{drive.upper()}:/{rest}"


def _git_path(raw: str) -> Path:
    """A path as git printed it; may still be non-native (see _to_native)."""
    return Path(_normalize_git_path(raw))


def _walk_up_for_git(start: Path) -> Path | None:
    """Nearest ancestor containing `.git` — a dir in a clone, a FILE in a worktree."""
    try:
        here = start.resolve()
    except OSError:
        return None
    for cand in (here, *here.parents):
        if (cand / ".git").exists():
            return cand
    return None


def _same_dir(a: Path, b: Path) -> bool:
    """Whether `a` and `b` are the same directory (both resolved)."""
    try:
        return a.resolve() == b.resolve()
    except OSError:
        return False


def repo_root(start: Path | None = None) -> Path:
    """Top-level dir of the git repo containing `start` (default cwd)."""
    base = Path(start) if start is not None else Path.cwd()
    out = _git("rev-parse", "--show-toplevel", cwd=base)
    p = _git_path(out)
    if p.is_dir():
        own = _walk_up_for_git(base)
        if own is None or not _same_dir(own, p):
            # The repository's own config (core.worktree) moved its work
            # tree away from the directory holding its .git — somewhere
            # else, or up to an ancestor such as the home directory;
            # worktrees would land under that directory.
            raise WorktreeError(
                f"{base}: {gitsafe.NOT_IN_WORK_TREE} (git reports the work tree as {p})"
            )
        return p
    # git answered with a path this platform can't use (an msys mount point).
    found = _walk_up_for_git(base)
    if found is None:
        raise WorktreeError(
            f"git reported the repo root as {out.strip()!r}, which is not a "
            "usable path here, and no .git was found walking up from "
            f"{base} — is a POSIX-style git (msys2/Cygwin) shadowing "
            "Git for Windows on PATH?"
        )
    return found


def _to_native(raw: str, *, git_prefix: str = "", native_root: Path | None = None) -> Path:
    """A path from git output, made native.

    Falls back to swapping git's spelling of the repo root for the real one —
    worktree paths live under the root, so learning that single mapping covers
    them all without having to model msys's mount table.
    """
    p = _git_path(raw)
    if p.is_dir() or native_root is None or not git_prefix:
        return p
    stripped = raw.strip()
    if stripped == git_prefix:
        return native_root
    if stripped.startswith(git_prefix.rstrip("/") + "/"):
        rest = stripped[len(git_prefix.rstrip("/")) + 1:]
        return native_root / rest
    return p


# ---- public API ---------------------------------------------------------


def list_worktrees(start: Path | None = None) -> list[WorktreeEntry]:
    """Parse `git worktree list --porcelain` into structured entries."""
    raw = _git("worktree", "list", "--porcelain", cwd=start)
    # Learn how this git spells the repo root vs. what it really is, so
    # non-native entry paths can be remapped (see _to_native).
    git_prefix = ""
    native_root: Path | None = None
    try:
        git_prefix = _git(
            "rev-parse", "--show-toplevel", cwd=start or Path.cwd()
        ).strip()
        native_root = repo_root(start)
    except WorktreeError:
        pass
    entries: list[WorktreeEntry] = []
    cur_path: Path | None = None
    cur_head = ""
    cur_branch: str | None = None
    cur_locked = False
    for line in raw.splitlines() + [""]:  # trailing blank to flush last entry
        if line.startswith("worktree "):
            cur_path = _to_native(
                line[len("worktree "):],
                git_prefix=git_prefix,
                native_root=native_root,
            )
            cur_head = ""
            cur_branch = None
            cur_locked = False
        elif line.startswith("HEAD "):
            cur_head = line[len("HEAD "):].strip()[:12]
        elif line.startswith("branch "):
            ref = line[len("branch "):].strip()
            cur_branch = ref.removeprefix("refs/heads/")
        elif line == "locked" or line.startswith("locked "):
            cur_locked = True
        elif line.strip() == "":
            if cur_path is not None:
                entries.append(WorktreeEntry(
                    path=cur_path, branch=cur_branch, head=cur_head, locked=cur_locked,
                ))
                cur_path = None
    return entries


def managed_root(start: Path | None = None) -> Path:
    """The checkout whose `.worktrees/` holds the managed worktrees: the
    repository's main worktree (git lists it first), also when `start` is
    inside a linked one. Rooted at the linked one they would nest, and the
    shared `.git/info/exclude` hides a nested `.worktrees/` from the outer
    worktree's status, so removing that "clean" outer worktree would delete
    the inner one with it. A bare repository has no main work tree: then the
    top of `start`'s own checkout (prune still keeps a worktree that holds
    another)."""
    root = repo_root(start)
    try:
        entries = list_worktrees(root)
    except WorktreeError:
        return root
    main = entries[0].path if entries else None
    if main is None or _same_dir(main, root) or not main.is_dir():
        return root
    try:
        return repo_root(main)
    except WorktreeError:  # a bare repository (or one git can't use)
        return root


def _managed_path(root: Path, name: str) -> Path:
    """`<root>/.worktrees/<name with / as __>`, refusing names that would land
    anywhere else (a drive or root prefix, a backslash separator, `..`)."""
    flat = name.replace("/", "__")
    pure = PurePath(flat)
    if not flat or flat in (".", "..") or pure.anchor or len(pure.parts) != 1:
        raise WorktreeError(f"not a usable worktree name: {name!r}")
    return root / ".worktrees" / flat


def create_worktree(
    branch: str,
    *,
    start: Path | None = None,
    create_branch: bool = True,
    base: str | None = None,
    lock_reason: str | None = None,
) -> Path:
    """Create `<repo>/.worktrees/<branch>/` checking out `branch` (`<repo>`:
    the main worktree, `managed_root`).

    `create_branch=True` (the default) means we'll create the branch if it
    doesn't already exist. Pass `base="main"` to fork from a non-HEAD base.
    With `create_branch=False`, `branch` may be any existing branch or
    commit-ish. Names git could read as an option are refused. A failed add
    takes back the branch it made (git makes it before the worktree). With
    `lock_reason` the worktree is created `git worktree lock`ed.
    """
    try:
        branch = (gitsafe.validate_branch_name(branch) if create_branch
                  else gitsafe.validate_ref(branch, what="branch"))
        base = gitsafe.validate_ref(base, what="base") if base else None
    except gitsafe.UnsafeRefError as exc:
        raise WorktreeError(str(exc)) from exc
    root = managed_root(start)
    dest = _managed_path(root, branch)
    if dest.exists():
        raise WorktreeError(f"worktree path already exists: {dest}")

    lock_args = _add_lock_args(lock_reason) if lock_reason else []
    args: list[str] = ["worktree", "add", *(lock_args or [])]
    fork = None
    if create_branch:
        args += ["-b", branch, *gitsafe.end_of_options(), str(dest)]
        if base:
            args += [base]
        try:
            if branch not in _branches(root):
                fork = _git("rev-parse", "--verify", "--quiet",
                            f"{base or 'HEAD'}^{{commit}}", cwd=root).strip()
        except WorktreeError:
            pass  # a bad base: the add below says so
    else:
        args += [*gitsafe.end_of_options(), str(dest), branch]
    # Before the add, so not even a `git status` run meanwhile lists it.
    _exclude_worktrees_dir(root)
    try:
        _git(*args, cwd=root)
    except WorktreeError:
        if fork:
            _drop_unused_branch(root, branch, fork)
        raise
    if lock_reason and lock_args is None:  # a git too old for `add --lock`
        _git("worktree", "lock", "--reason", lock_reason, str(dest), cwd=root)
    return dest


_ADD_LOCK_VERSION = (2, 13)     # `git worktree add --lock`
_ADD_REASON_VERSION = (2, 36)   # ... `--reason <text>` with it (2.33+; a margin)


def _add_lock_args(reason: str) -> list[str] | None:
    """`git worktree add` options that create the worktree locked, so no
    prune can take it between the add and a separate lock; None for a git
    too old for them (lock right after the add instead)."""
    v = gitsafe.git_version()
    if v is not None and v < _ADD_LOCK_VERSION:
        return None
    return ["--lock", "--reason", reason] if v is not None and v >= _ADD_REASON_VERSION \
        else ["--lock"]


def _drop_unused_branch(root: Path, branch: str, fork: str) -> None:
    """Delete the branch a failed `git worktree add -b` left behind: only one
    that did not exist before, still points at the commit it was made from
    and is checked out nowhere, and only while it still does (update-ref
    checks the old value). Best effort."""
    try:
        if _branches(root).get(branch) != fork or _holders(root, branch):
            return
        _git("update-ref", "-d", f"refs/heads/{branch}", fork, cwd=root)
    except WorktreeError:
        pass


_EXCLUDE_LINE = "/.worktrees/"


def _exclude_worktrees_dir(root: Path) -> None:
    """Keep `.worktrees/` out of `git add -A` in the user's repo.

    The managed worktrees live inside the checkout, so without an ignore entry
    a `git add -A` there stages each one as an embedded repository. Written to
    `.git/info/exclude` (local, never committed) rather than the repo's own
    .gitignore. Best effort: a failure here must not undo a created worktree.
    """
    try:
        raw = _git("rev-parse", "--git-common-dir", cwd=root).strip()
        common = _git_path(raw)
        if not common.is_absolute():
            common = root / common
        exclude = common / "info" / "exclude"
        existing = exclude.read_text(encoding="utf-8") if exclude.is_file() else ""
        lines = {ln.strip() for ln in existing.splitlines()}
        if lines & {_EXCLUDE_LINE, ".worktrees/", ".worktrees", "/.worktrees"}:
            return
        exclude.parent.mkdir(parents=True, exist_ok=True)
        sep = "" if not existing or existing.endswith("\n") else "\n"
        with exclude.open("a", encoding="utf-8", newline="\n") as f:
            f.write(f"{sep}# eVi managed worktrees (evi worktree create)\n{_EXCLUDE_LINE}\n")
    except (OSError, WorktreeError):
        pass


def resolve_worktree_path(branch_or_path: str, *, start: Path | None = None) -> Path:
    """Map a branch name or path to the worktree directory it refers to.

    A bare branch name resolves to `<repo>/.worktrees/<branch with / as __>`
    (`<repo>`: the main worktree, `managed_root`); an absolute path is taken
    as-is.
    """
    candidate = Path(branch_or_path)
    if candidate.is_absolute():
        return candidate
    return _managed_path(managed_root(start), branch_or_path)


def dirty_files(path: Path) -> list[str] | None:
    """Porcelain status lines for uncommitted work in `path`, newest git first.

    Returns [] when the worktree is clean and None when that can't be
    determined (missing directory, not a worktree, no git). Callers should
    treat None as "assume there may be work at risk" — it is the fail-safe
    reading, since the only use for this is deciding whether to warn.

    Untracked files and submodule changes are always looked at, whatever the
    config says: `status.showUntrackedFiles=no` (common in big repositories)
    or a submodule's `ignore = all` would otherwise make new work look clean,
    and prune removes clean worktrees.
    """
    if not path.is_dir():
        return None
    try:
        out = _git("status", "--porcelain", "--untracked-files=normal",
                   "--ignore-submodules=none", cwd=path)
    except WorktreeError:
        return None
    return [ln for ln in out.splitlines() if ln.strip()]


def orphaned_head(path: Path) -> str | None:
    """Short sha of HEAD when removing this worktree would strand commits.

    A worktree on a branch is safe — its commits live on that branch. One on a
    DETACHED head whose commit no branch contains is not: delete the directory
    and those commits are only reachable via the reflog. Returns None when
    there is nothing to lose or the question can't be answered.
    """
    if not path.is_dir():
        return None
    try:
        if _git("rev-parse", "--abbrev-ref", "HEAD", cwd=path).strip() != "HEAD":
            return None  # on a branch
        raw = _git("branch", "--contains", "HEAD", cwd=path)
        # git lists the detached HEAD itself here as a pseudo-entry —
        # "* (no branch)", or "* (HEAD detached at abc1234)" on some versions.
        # Those are not branches; anything parenthesised is git talking about
        # the very worktree we are asking about.
        real = [
            ln.lstrip("*+ \t")
            for ln in raw.splitlines()
            if ln.strip() and not ln.lstrip("*+ \t").startswith("(")
        ]
        if real:
            return None  # a real branch already contains it
        return _git("rev-parse", "--short", "HEAD", cwd=path).strip()
    except WorktreeError:
        return None


def _lacks_worktree_remove(exc: WorktreeError) -> bool:
    """Whether `git worktree remove` failed because this git predates it (2.17).

    Only git's own "I don't know that subcommand" answer counts (its usage
    text, or "unknown subcommand"), and only from a git older than 2.17 or
    one whose version can't be told. Sniffing the message for "remove" also
    matched "cannot remove a locked working tree" and any path containing the
    word, and handed those to the rmtree fallback.
    """
    message = str(exc).lower()
    if "usage:" not in message and "unknown subcommand" not in message:
        return False
    version = gitsafe.git_version()
    return version is None or version < (2, 17)


def _is_link(p: Path) -> bool:
    return p.is_symlink() or os.path.isjunction(p)


def _rmtree_managed(root: Path, candidate: Path) -> None:
    """Delete a worktree directory ourselves (old gits only), but only a real
    directory directly under `<root>/.worktrees/` that git lists as a
    worktree, and never through a symlink or junction."""
    base = root / ".worktrees"
    if not os.path.lexists(candidate):
        return
    try:
        inside = (
            candidate.parent == base
            and not _is_link(base) and not _is_link(candidate)
            and candidate.is_dir()
            and candidate.resolve().parent == base.resolve()
            and base.resolve().parent == root.resolve()
        )
        entry = next(
            (e for e in list_worktrees(root) if e.path.resolve() == candidate.resolve()),
            None,
        ) if inside else None
    except OSError:
        entry = None
    if entry is None:
        raise WorktreeError(
            f"refusing to delete {candidate}: only a registered worktree "
            f"directly under {base} (not a link) is removed this way"
        )
    if entry.locked or _has_lock_file(candidate):
        raise WorktreeError(
            f"refusing to delete {candidate}: the worktree is locked "
            "(`git worktree unlock` it first)"
        )
    shutil.rmtree(candidate, ignore_errors=True)


def _admin_dir(path: Path) -> Path | None:
    """A linked worktree's admin dir (`<git dir>/worktrees/<name>`), read
    from its `.git` file; None when `path` is not a linked worktree."""
    dotgit = path / ".git"
    try:
        if not dotgit.is_file() or dotgit.is_symlink():
            return None
        with dotgit.open("rb") as f:
            line = f.read(4096).decode("utf-8").splitlines()[0].strip()
    except (OSError, ValueError, IndexError):
        return None
    if not line.startswith("gitdir:"):
        return None
    admin = _git_path(line[len("gitdir:"):])
    return admin if admin.is_absolute() else path / admin


def _has_lock_file(path: Path) -> bool:
    """`git worktree lock` leaves a `locked` file in the admin dir. Older gits
    don't list it, so it is checked here as well."""
    admin = _admin_dir(path)
    try:
        return admin is not None and (admin / "locked").exists()
    except OSError:
        return True  # can't tell: count it as locked (never delete)


def remove_worktree(
    branch_or_path: str, *, start: Path | None = None, force: bool = True,
) -> None:
    """Remove a worktree by branch name or path. Force-removes by default.

    On git ≥ 2.17 this uses `git worktree remove --force`, which only ever
    removes a registered worktree. Older gits lack that subcommand, so we
    delete the directory ourselves, confined to `<repo>/.worktrees/` (see
    `_rmtree_managed`), and run `git worktree prune` to clean the admin
    entry. A directory that is already gone just gets the prune.

    By default this forces: callers that want to protect uncommitted work
    should check `dirty_files()` and confirm with the user first (the CLI
    does). `force=False` leaves out `--force`, so git itself refuses a
    worktree with uncommitted or untracked files (prune does that).
    """
    root = managed_root(start)
    candidate = resolve_worktree_path(branch_or_path, start=start)

    try:
        _git("worktree", "remove", *(["--force"] if force else []), str(candidate), cwd=root)
        return
    except WorktreeError as exc:
        # Older git lacks `worktree remove` entirely, and some early versions
        # refuse to remove a worktree whose directory was deleted by hand.
        # Anything else is a real failure we should surface.
        gone = not os.path.lexists(candidate) and "does not exist" in str(exc)
        if not gone and not _lacks_worktree_remove(exc):
            raise

    _rmtree_managed(root, candidate)
    # Drop the stale admin entry under .git/worktrees/<name>/.
    _git("worktree", "prune", cwd=root)


def find_worktree_for(branch: str, start: Path | None = None) -> Path | None:
    for e in list_worktrees(start):
        if e.branch == branch:
            return e.path
    return None


# ---- isolated runs ------------------------------------------------------

AUTO = "auto"  # open_isolated: a fresh fix-<task>[-n] branch
_AUTO_STEM = "fix"
_SLUG = re.compile(r"[^a-z0-9]+")
_MAX_SLUG = 32
_AUTO_ATTEMPTS = 5  # auto names tried when another process takes one first
_MAX_LISTED = 200  # commits / dirty files a report lists
_MAX_COUNTED_BYTES = 8 * 1024 * 1024  # an untracked file bigger than this counts no lines

# One worktree opened at a time per process: parallel team tasks and workflow
# steps each open one, and two of them must not pick the same fix-<n> name.
_OPEN_LOCK = threading.Lock()


@dataclass(frozen=True)
class IsolatedWorktree:
    """The worktree an isolated run works in (`open_isolated`)."""

    path: Path
    branch: str
    base: str      # the commit checked out when the run started (full sha)
    created: bool  # this run made the worktree (else it reused one)
    root: Path     # the main checkout whose .worktrees/ holds it (managed_root)
    # The run's lock as git recorded it (its `locked` file), which `release`
    # takes off only while it is still that one; None: the run holds none.
    lock: str | None = None


def _task_slug(task: str) -> str:
    slug = _SLUG.sub("-", (task or "").lower()).strip("-")
    return slug[:_MAX_SLUG].rstrip("-")


def _git_rc(*args: str, cwd: Path) -> int:
    """git's exit code (for the commands that answer with it)."""
    try:
        return gitsafe.run_git(args, cwd=cwd).returncode
    except gitsafe.GitNotFoundError as exc:
        raise WorktreeError("git not found on PATH") from exc
    except gitsafe.UnsafeRepoError as exc:
        raise WorktreeError(str(exc)) from exc


def _branches(root: Path) -> dict[str, str]:
    """{name: sha} of the local branches, each name exactly as git stores it.

    Never ask git about one name (`show-ref refs/heads/<name>`): on a
    case-insensitive filesystem (Windows, macOS) the loose ref file of `main`
    answers for `Main` too, and a worktree on `Main` would then move the main
    checkout's branch."""
    out = _git("for-each-ref", "--format=%(objectname) %(refname)", "refs/heads/", cwd=root)
    found: dict[str, str] = {}
    for line in out.splitlines():
        sha, _, ref = line.strip().partition(" ")
        if ref.startswith("refs/heads/"):
            found[ref[len("refs/heads/"):]] = sha
    return found


def _case_twin(branches: dict[str, str], name: str) -> str | None:
    """An existing branch whose name differs from `name` only in case."""
    folded = name.casefold()
    return next((b for b in branches if b != name and b.casefold() == folded), None)


def _holders(root: Path, name: str) -> list[WorktreeEntry]:
    """The worktrees whose HEAD is on branch `name`, in any case. On a
    case-insensitive filesystem `git checkout MAIN` leaves HEAD saying
    refs/heads/MAIN while the ref (and for-each-ref) says main; an exact
    compare, git's own checked-out check among them, misses that the main
    checkout holds main."""
    folded = name.casefold()
    return [e for e in list_worktrees(root)
            if e.branch is not None and e.branch.casefold() == folded]


def _name_taken(root: Path, name: str) -> bool:
    """A branch of that name in any case, or a `.worktrees/<name>` directory."""
    folded = name.casefold()
    return (any(b.casefold() == folded for b in _branches(root))
            or _managed_path(root, name).exists())


def _is_managed(root: Path, path: Path) -> bool:
    """`path` is a worktree directory directly under `<root>/.worktrees/`."""
    try:
        return path.resolve().parent == (root / ".worktrees").resolve()
    except OSError:
        return False


def auto_branch_name(task: str = "", *, start: Path | None = None) -> str:
    """A fresh branch name for an isolated run: `fix-<task>`, then
    `fix-<task>-2`, ... (`task` cut to lower-case letters, digits and dashes),
    or `fix-1`, `fix-2`, ... without one. Fresh: no branch of that name (in
    any case) and no `.worktrees/<name>` directory yet."""
    root = managed_root(start)
    taken = {b.casefold() for b in _branches(root)}
    slug = _task_slug(task)
    stem = f"{_AUTO_STEM}-{slug}" if slug else _AUTO_STEM
    for n in range(1, 10_000):
        name = stem if slug and n == 1 else f"{stem}-{n}"
        if name.casefold() not in taken and not _managed_path(root, name).exists():
            return name
    raise WorktreeError(f"no free branch name like {stem}-<n> in {root}")


def _run_lock_reason() -> str:
    return f"eVi isolated run (pid {os.getpid()})"


def _lock_reason_of(path: Path) -> str | None:
    """The reason in a worktree's `locked` file ("" for none); None when it
    is not locked or that can't be read."""
    admin = _admin_dir(path)
    try:
        return (admin / "locked").read_text(encoding="utf-8").strip() if admin else None
    except (OSError, ValueError):
        return None


def _lock_for_run(root: Path, path: Path, branch: str, reason: str) -> None:
    """Lock a reused worktree for the run; refused when it is locked already
    (by another run using it, or by someone keeping it)."""
    problem = "it is locked" if _has_lock_file(path) else ""
    if not problem:
        try:
            _git("worktree", "lock", "--reason", reason, str(path), cwd=root)
        except WorktreeError as exc:
            problem = str(exc)
    if problem:
        raise WorktreeError(
            f"the worktree of branch {branch} ({path}) can't be locked for this run "
            f"({problem}): another run may be using it; if none is, "
            f"`git worktree unlock {path}`"
        )


def open_isolated(
    branch: str = AUTO,
    *,
    task: str = "",
    start: Path | None = None,
    base: str | None = None,
) -> IsolatedWorktree:
    """Open the worktree an isolated run works in, locked until `release`.

    `branch` "auto" makes a fresh `fix-<task>[-n]` branch (`auto_branch_name`)
    from `base` (default HEAD) in `<repo>/.worktrees/<name>/` (`<repo>`: the
    main worktree, also from inside a linked one). Any other name must be a
    valid branch name (check-ref-format rules); then a worktree under
    `.worktrees/` already on that branch is reused (`base` is ignored), an
    existing branch is checked out in a new one, and otherwise the branch is
    created from `base`. A branch checked out anywhere else (the main
    checkout above all, also when its HEAD spells the branch in another
    case), or one whose name differs from an existing branch's only in case,
    is refused: an isolated run never edits a checkout someone may be
    working in. The worktree is `git worktree lock`ed for the run, so
    prune and remove leave it alone and no second run shares it (a locked
    one is refused); call `release` when the run is over. Opening is
    serialized within the process; an auto name another process took
    meanwhile moves on to the next one.
    """
    name = (branch or "").strip()
    reason = _run_lock_reason()
    with _OPEN_LOCK:
        root = managed_root(start)
        if name == AUTO:
            for attempt in range(_AUTO_ATTEMPTS):
                name = auto_branch_name(task, start=root)
                try:
                    path = create_worktree(name, start=root, base=base, lock_reason=reason)
                    created = True
                    break
                except WorktreeError:
                    # Taken meanwhile (by another process): the next name.
                    # Anything else is a real failure, and create_worktree
                    # took back the branch its failed add made.
                    if not _name_taken(root, name) or attempt == _AUTO_ATTEMPTS - 1:
                        raise
        else:
            try:
                name = gitsafe.validate_branch_name(name)
            except gitsafe.UnsafeRefError as exc:
                raise WorktreeError(str(exc)) from exc
            branches = _branches(root)
            twin = _case_twin(branches, name)
            if twin is not None:
                raise WorktreeError(
                    f"branch {name} differs from the existing branch {twin} only in case; "
                    "a case-insensitive filesystem (Windows, macOS) makes them the same "
                    "branch, so pick another name"
                )
            # Checked out anywhere, also under a HEAD that spells it in
            # another case (`_holders`): only one managed worktree may hold it.
            holders = _holders(root, name)
            elsewhere = next((e.path for e in holders if not _is_managed(root, e.path)),
                             holders[1].path if len(holders) > 1 else None)
            if elsewhere is not None:
                raise WorktreeError(
                    f"branch {name} is checked out at {elsewhere}; an isolated run needs "
                    f"a worktree of its own under {root / '.worktrees'}, so pick another branch"
                )
            existing = holders[0].path if holders else None
            if existing is not None and not existing.is_dir():
                raise WorktreeError(
                    f"the worktree of branch {name} ({existing}) is missing; "
                    "`git worktree prune` drops its record"
                )
            if existing is not None:
                _lock_for_run(root, existing, name, reason)
                path, created = existing, False
            else:
                have = name in branches
                path = create_worktree(
                    name, start=root, create_branch=not have, base=None if have else base,
                    lock_reason=reason,
                )
                created = True
        # What git wrote: our reason, or its own words for a git too old to
        # take one with `add --lock`.
        iso = IsolatedWorktree(path=path, branch=name, base="", created=created, root=root,
                               lock=_lock_reason_of(path))
        try:
            head = _git("rev-parse", "--verify", "HEAD", cwd=path).strip()
        except WorktreeError:
            release(iso)
            raise
    return IsolatedWorktree(path=path, branch=name, base=head, created=created, root=root,
                            lock=iso.lock)


def release(wt: IsolatedWorktree) -> None:
    """Unlock the worktree `open_isolated` locked, once its run is over (from
    then on prune may remove it, when its work is merged). Only the run's own
    lock is taken off: one someone put on meanwhile stays. Best effort:
    never raises."""
    if wt.lock is None or _lock_reason_of(wt.path) != wt.lock:
        return
    try:
        _git("worktree", "unlock", str(wt.path), cwd=wt.root)
    except WorktreeError:
        pass


# Tools whose work runs in eVi's process folder, not the session's: monitor
# (kind="command" runs its command there, kind="file" reads a relative path
# there), the git tools when given no path, and MCP tools (a stdio server is
# spawned in the process folder, and any server acts wherever it was started
# or its own config points it, never in the session's folder). A run held to
# a worktree on a thread of its own (a team task, a workflow step, a
# subagent) goes without them, or they would reach the very checkout it is
# kept away from (`confined_tools`). `evi run --worktree` moves the process
# (its stdio servers spawn in the worktree), so it keeps them.
PROCESS_FOLDER_TOOLS: frozenset[str] = frozenset({"monitor"})
PROCESS_FOLDER_CATEGORIES: frozenset[str] = frozenset({"git", "mcp"})


def _in_session_folder(tool: Any) -> bool:
    return (getattr(tool, "name", None) not in PROCESS_FOLDER_TOOLS
            and getattr(tool, "category", None) not in PROCESS_FOLDER_CATEGORIES)


def confined_tools(tools: Any) -> Any:
    """`tools` ({name: Tool}, or Tools in a list) without the ones that work
    in eVi's process folder (PROCESS_FOLDER_TOOLS, PROCESS_FOLDER_CATEGORIES),
    for a run held to a worktree on a thread of its own."""
    if isinstance(tools, dict):
        return {n: t for n, t in tools.items() if _in_session_folder(t)}
    return [t for t in tools if _in_session_folder(t)]


def check_thread_isolation(settings: Any) -> None:
    """Raise WorktreeError when a worktree run on a thread of its own (a team
    task, a workflow step, a subagent) could not be held to its worktree: a
    delegate CLI-agent [llm] backend runs its own tools in eVi's process
    folder, not in the session's (evi.llm.cli_agent.DELEGATE_BACKENDS).
    `evi run --worktree` moves the whole process, so it needs no check."""
    from evi.llm.cli_agent import DELEGATE_BACKENDS

    llm = getattr(settings, "llm", settings)
    kind = str(getattr(llm, "backend", "") or "").strip().lower()
    if kind in DELEGATE_BACKENDS:
        raise WorktreeError(
            f"the {kind} backend is a CLI agent that runs its own tools in eVi's "
            "process folder, so it can't be held to a worktree here; use "
            "`evi run --worktree`, or a local or API backend"
        )


def _line_count(path: Path) -> int:
    """Lines in an untracked file (0 for a binary or very large one)."""
    try:
        if path.stat().st_size > _MAX_COUNTED_BYTES:
            return 0
        data = path.read_bytes()
    except OSError:
        return 0
    if not data or b"\0" in data:
        return 0
    return data.count(b"\n") + (0 if data.endswith(b"\n") else 1)


def _diffstat(path: Path, base: str) -> dict[str, int]:
    """{files, insertions, deletions} of the work tree at `path` against
    `base`: tracked changes (committed or not) plus untracked files."""
    files = insertions = deletions = 0
    out = _git("diff", "--numstat", "-z", "--no-renames", "--no-ext-diff",
               *gitsafe.rev_args(base, what="base"), "--", cwd=path)
    for entry in out.split("\0"):
        bits = entry.split("\t", 2)
        if len(bits) != 3 or not bits[2]:
            continue
        files += 1
        insertions += int(bits[0]) if bits[0].isdigit() else 0
        deletions += int(bits[1]) if bits[1].isdigit() else 0
    untracked = _git("ls-files", "--others", "--exclude-standard", "-z", cwd=path)
    for rel in (p for p in untracked.split("\0") if p):
        files += 1
        insertions += _line_count(path / rel)
    return {"files": files, "insertions": insertions, "deletions": deletions}


def worktree_report(wt: IsolatedWorktree) -> dict[str, Any]:
    """What an isolated run left in its worktree, for the run report.

    {path, branch, created, base, head, commits, dirty, dirty_files,
    diffstat}: the branch as checked out now (None on a detached HEAD), the
    commit the run started from and the one checked out now, the commits
    between them (oldest first, [{sha, subject}], at most 200), whether
    uncommitted or untracked files are left (and their `git status
    --porcelain` lines, at most 200), and {files, insertions, deletions} of
    the work tree against `base`, untracked files included. What git can't
    tell is None. Nothing is merged: the branch waits for review."""
    path = wt.path
    report: dict[str, Any] = {
        "path": str(path), "branch": wt.branch, "created": wt.created, "base": wt.base,
        "head": None, "commits": None, "dirty": None, "dirty_files": None, "diffstat": None,
    }
    if not path.is_dir():
        return report
    try:
        report["head"] = _git("rev-parse", "--verify", "HEAD", cwd=path).strip()
        on = _git_rc("symbolic-ref", "--quiet", "HEAD", cwd=path) == 0
        report["branch"] = (
            _git("symbolic-ref", "--quiet", "--short", "HEAD", cwd=path).strip() if on else None
        )
    except WorktreeError:
        return report
    try:
        log = _git("log", f"--max-count={_MAX_LISTED}", "--reverse", "--format=%H%x00%s",
                   *gitsafe.rev_args(f"{wt.base}..HEAD", what="base"), "--", cwd=path)
        report["commits"] = [
            {"sha": sha, "subject": subject}
            for line in log.splitlines() if "\0" in line
            for sha, _, subject in [line.partition("\0")]
        ]
    except WorktreeError:
        pass
    dirty = dirty_files(path)
    if dirty is not None:
        report["dirty"] = bool(dirty)
        report["dirty_files"] = dirty[:_MAX_LISTED]
    try:
        report["diffstat"] = _diffstat(path, wt.base)
    except WorktreeError:
        pass
    return report


def summary_line(report: dict[str, Any]) -> str:
    """`worktree_report` in one line for people (and for the model a
    subagent reports to): where the work is and what it amounts to."""
    bits = [f"worktree {report.get('path')}"]
    if report.get("branch"):
        bits.append(f"branch {report['branch']}")
    commits = report.get("commits")
    if commits is not None:
        bits.append(f"{len(commits)} commit{'' if len(commits) == 1 else 's'}")
    if report.get("dirty"):
        bits.append("uncommitted changes")
    elif report.get("dirty") is False:
        bits.append("clean")
    stat = report.get("diffstat")
    if stat:
        bits.append(
            f"{stat['files']} file{'' if stat['files'] == 1 else 's'} changed "
            f"(+{stat['insertions']} -{stat['deletions']})"
        )
    bits.append("not merged")
    return "; ".join(bits)


@dataclass
class PruneResult:
    """What `prune_worktrees` removed (or, on a dry run, would remove) and
    what it kept, with the reason."""

    removed: list[Path] = field(default_factory=list)
    kept: list[tuple[Path, str]] = field(default_factory=list)


def _held_worktree(path: Path, entries: list[WorktreeEntry]) -> Path | None:
    """Another worktree inside `path`: a registered one, or anything in its
    own `.worktrees/`. The shared `.git/info/exclude` hides that from `path`'s
    status, and removing `path` would delete it too."""
    here = path.resolve()
    for e in entries:
        other = e.path.resolve()
        if other != here and other.is_relative_to(here):
            return e.path
    nested = path / ".worktrees"
    if nested.is_dir() and any(nested.iterdir()):
        return nested
    return None


def _keep_reason(
    entry: WorktreeEntry, into_sha: str, into: str, entries: list[WorktreeEntry],
) -> str | None:
    """Why a managed worktree must stay; None when its work is safe elsewhere."""
    if entry.locked or _has_lock_file(entry.path):
        return "locked"
    if not entry.path.is_dir():
        return "its directory is missing (`git worktree prune` drops the record)"
    if entry.branch is None:
        return "detached HEAD"
    try:
        inner = _held_worktree(entry.path, entries)
    except OSError:
        return "can't inspect it"
    if inner is not None:
        return f"it holds another worktree ({inner})"
    dirty = dirty_files(entry.path)
    if dirty is None:
        return "can't inspect it"
    if dirty:
        return f"{len(dirty)} uncommitted or untracked file(s)"
    try:
        tip = _git("rev-parse", "--verify", "HEAD", cwd=entry.path).strip()
        merged = _git_rc("merge-base", "--is-ancestor", tip, into_sha, cwd=entry.path)
    except WorktreeError:
        return "can't tell whether it is merged"
    if merged == 1:
        return f"branch {entry.branch} is not merged into {into}"
    if merged != 0:
        return "can't tell whether it is merged"
    return None


def prune_worktrees(
    *, start: Path | None = None, into: str | None = None, dry_run: bool = False,
) -> PruneResult:
    """Remove the managed worktrees (`<repo>/.worktrees/`, `<repo>` being the
    main worktree) whose work is safe elsewhere: not locked (a live isolated
    run keeps its worktree locked), on a branch, holding no other worktree,
    clean (no uncommitted or untracked files, whatever the status config
    says) and merged, i.e. the branch tip is an ancestor of `into` (default:
    HEAD of the checkout at `start`). Every other one is kept, with the
    reason, and worktrees outside `.worktrees/` are never looked at. The
    removal itself goes without `--force`, so git checks once more. Branches
    stay (delete a merged one with `git branch -d`), and nothing is merged.
    With `dry_run` nothing is removed: `removed` lists what would be."""
    here = repo_root(start)
    root = managed_root(here)
    try:
        target = gitsafe.validate_ref(into, what="into") if into else "HEAD"
    except gitsafe.UnsafeRefError as exc:
        raise WorktreeError(str(exc)) from exc
    # A validated ref can't start with "-", so no --end-of-options (rev-parse
    # took it only later than the other commands).
    into_sha = _git("rev-parse", "--verify", "--quiet", f"{target}^{{commit}}", cwd=here).strip()
    result = PruneResult()
    entries = list_worktrees(root)
    for entry in entries:
        if not _is_managed(root, entry.path):
            continue
        why = _keep_reason(entry, into_sha, target, entries)
        if why is not None:
            result.kept.append((entry.path, why))
            continue
        if not dry_run:
            try:
                remove_worktree(str(entry.path), start=root, force=False)
            except WorktreeError as exc:
                result.kept.append((entry.path, f"git did not remove it: {exc}"))
                continue
        result.removed.append(entry.path)
    return result
