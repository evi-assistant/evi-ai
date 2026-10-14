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
"""

from __future__ import annotations

import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path, PurePath

from evi import gitsafe


class WorktreeError(RuntimeError):
    """Surfaced when a git command fails or git itself is missing."""


@dataclass(frozen=True)
class WorktreeEntry:
    path: Path
    branch: str | None  # detached HEAD entries have no branch
    head: str           # short sha


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
) -> Path:
    """Create `<repo>/.worktrees/<branch>/` checking out `branch`.

    `create_branch=True` (the default) means we'll create the branch if it
    doesn't already exist. Pass `base="main"` to fork from a non-HEAD base.
    With `create_branch=False`, `branch` may be any existing branch or
    commit-ish. Names git could read as an option are refused.
    """
    try:
        branch = (gitsafe.validate_branch_name(branch) if create_branch
                  else gitsafe.validate_ref(branch, what="branch"))
        base = gitsafe.validate_ref(base, what="base") if base else None
    except gitsafe.UnsafeRefError as exc:
        raise WorktreeError(str(exc)) from exc
    root = repo_root(start)
    dest = _managed_path(root, branch)
    if dest.exists():
        raise WorktreeError(f"worktree path already exists: {dest}")

    args: list[str] = ["worktree", "add"]
    if create_branch:
        args += ["-b", branch, *gitsafe.end_of_options(), str(dest)]
        if base:
            args += [base]
    else:
        args += [*gitsafe.end_of_options(), str(dest), branch]
    _git(*args, cwd=root)
    _exclude_worktrees_dir(root)
    return dest


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

    A bare branch name resolves to `<repo>/.worktrees/<branch with / as __>`;
    an absolute path is taken as-is.
    """
    candidate = Path(branch_or_path)
    if candidate.is_absolute():
        return candidate
    return _managed_path(repo_root(start), branch_or_path)


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

    Decided by version, not by sniffing the message for "remove": that also
    matched "cannot remove a locked working tree" and any path containing the
    word, and handed those to the rmtree fallback.
    """
    version = gitsafe.git_version()
    if version is not None:
        return version < (2, 17)
    return "usage:" in str(exc).lower()


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
        registered = inside and any(
            e.path.resolve() == candidate.resolve() for e in list_worktrees(root)
        )
    except OSError:
        registered = False
    if not registered:
        raise WorktreeError(
            f"refusing to delete {candidate}: only a registered worktree "
            f"directly under {base} (not a link) is removed this way"
        )
    shutil.rmtree(candidate, ignore_errors=True)


def remove_worktree(branch_or_path: str, *, start: Path | None = None) -> None:
    """Remove a worktree by branch name or path. Force-removes.

    On git ≥ 2.17 this uses `git worktree remove --force`, which only ever
    removes a registered worktree. Older gits lack that subcommand, so we
    delete the directory ourselves, confined to `<repo>/.worktrees/` (see
    `_rmtree_managed`), and run `git worktree prune` to clean the admin
    entry. A directory that is already gone just gets the prune.

    This always forces: callers that want to protect uncommitted work should
    check `dirty_files()` and confirm with the user first (the CLI does).
    """
    root = repo_root(start)
    candidate = resolve_worktree_path(branch_or_path, start=start)

    try:
        _git("worktree", "remove", "--force", str(candidate), cwd=root)
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
