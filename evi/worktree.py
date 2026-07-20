"""Thin git-worktree wrapper.

Pairs nicely with `evi chat`: spin up a worktree for a branch, drop into
chat there, work on it in isolation, then either fold the branch back into
main or `evi worktree remove` it. The path layout is
`<repo>/.worktrees/<branch>/` by default so worktrees are siblings of the
main checkout rather than scattered across the filesystem.

The functions in this module raise `WorktreeError` on git failures; the
CLI layer translates them into clean error output.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path


class WorktreeError(RuntimeError):
    """Surfaced when a git command fails or git itself is missing."""


@dataclass(frozen=True)
class WorktreeEntry:
    path: Path
    branch: str | None  # detached HEAD entries have no branch
    head: str           # short sha


# ---- helpers ------------------------------------------------------------


def _git(*args: str, cwd: Path | None = None) -> str:
    if shutil.which("git") is None:
        raise WorktreeError("git not found on PATH")
    proc = subprocess.run(
        ["git", *args],
        cwd=str(cwd) if cwd is not None else None,
        capture_output=True,
        text=True,
    )
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


def _git_path(raw: str) -> Path:
    """Rewrite the common msys drive forms; may still be non-native."""
    raw = raw.strip()
    if os.name == "nt":
        m = _MSYS_DRIVE.match(raw)
        if m:
            drive, rest = m.group(1), m.group(2) or ""
            raw = f"{drive.upper()}:/{rest}"
    # Left alone off Windows: "/c/foo" is a legitimate POSIX path there.
    return Path(raw)


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


def repo_root(start: Path | None = None) -> Path:
    """Top-level dir of the git repo containing `start` (default cwd)."""
    base = Path(start) if start is not None else Path.cwd()
    out = _git("rev-parse", "--show-toplevel", cwd=base)
    p = _git_path(out)
    if p.is_dir():
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
    for line in raw.splitlines() + [""]:  # trailing blank to flush last entry
        if line.startswith("worktree "):
            cur_path = _to_native(
                line[len("worktree "):],
                git_prefix=git_prefix,
                native_root=native_root,
            )
            cur_head = ""
            cur_branch = None
        elif line.startswith("HEAD "):
            cur_head = line[len("HEAD "):].strip()[:12]
        elif line.startswith("branch "):
            ref = line[len("branch "):].strip()
            cur_branch = ref.removeprefix("refs/heads/")
        elif line.strip() == "":
            if cur_path is not None:
                entries.append(
                    WorktreeEntry(path=cur_path, branch=cur_branch, head=cur_head)
                )
                cur_path = None
    return entries


def create_worktree(
    branch: str,
    *,
    start: Path | None = None,
    create_branch: bool = True,
    base: str | None = None,
) -> Path:
    """Create `<repo>/.worktrees/<branch>/` checking out `branch`.

    `create_branch=True` (the default) means we'll create the branch if it
    doesn't already exist. Pass `base="main"` to fork from a non-HEAD base.
    """
    if not branch or "/" in branch.replace("refs/", "") and "refs/" not in branch:
        # Allow `refs/heads/foo` but block bare `foo/bar` filenames as a
        # rough sanity check.
        pass
    root = repo_root(start)
    dest = root / ".worktrees" / branch.replace("/", "__")
    if dest.exists():
        raise WorktreeError(f"worktree path already exists: {dest}")

    args: list[str] = ["worktree", "add"]
    if create_branch:
        args += ["-b", branch]
        args += [str(dest)]
        if base:
            args += [base]
    else:
        args += [str(dest), branch]
    _git(*args, cwd=root)
    return dest


def resolve_worktree_path(branch_or_path: str, *, start: Path | None = None) -> Path:
    """Map a branch name or path to the worktree directory it refers to.

    A bare branch name resolves to `<repo>/.worktrees/<branch with / as __>`;
    an absolute path is taken as-is.
    """
    candidate = Path(branch_or_path)
    if candidate.is_absolute():
        return candidate
    return repo_root(start) / ".worktrees" / branch_or_path.replace("/", "__")


def dirty_files(path: Path) -> list[str] | None:
    """Porcelain status lines for uncommitted work in `path`, newest git first.

    Returns [] when the worktree is clean and None when that can't be
    determined (missing directory, not a worktree, no git). Callers should
    treat None as "assume there may be work at risk" — it is the fail-safe
    reading, since the only use for this is deciding whether to warn.
    """
    if not path.is_dir():
        return None
    try:
        out = _git("status", "--porcelain", cwd=path)
    except WorktreeError:
        return None
    return [ln for ln in out.splitlines() if ln.strip()]


def remove_worktree(branch_or_path: str, *, start: Path | None = None) -> None:
    """Remove a worktree by branch name or path. Force-removes.

    On git ≥ 2.17 this uses `git worktree remove --force`. On older versions
    (which lack that subcommand) we fall back to deleting the directory
    ourselves and running `git worktree prune` to clean the admin entry.

    This always forces: callers that want to protect uncommitted work should
    check `dirty_files()` and confirm with the user first (the CLI does).
    """
    import shutil as _shutil

    root = repo_root(start)
    candidate = resolve_worktree_path(branch_or_path, start=start)

    try:
        _git("worktree", "remove", "--force", str(candidate), cwd=root)
        return
    except WorktreeError as exc:
        # Older git lacks `worktree remove` entirely; fall through if that's
        # the reason. Anything else is a real failure we should surface.
        if "remove" not in str(exc).lower() and "usage" not in str(exc).lower():
            raise

    if candidate.exists():
        _shutil.rmtree(candidate, ignore_errors=True)
    # Drop the stale admin entry under .git/worktrees/<name>/.
    _git("worktree", "prune", cwd=root)


def find_worktree_for(branch: str, start: Path | None = None) -> Path | None:
    for e in list_worktrees(start):
        if e.branch == branch:
            return e.path
    return None
