"""Git intelligence tools — read-only inspection of a local repository.

Wraps the `git` CLI in subprocess calls. All commands are deliberately
read-only (status, diff, log, show, blame). The `worktree` family stays in
`evi.worktree` since it mutates repo state.

Every call goes through `evi.gitsafe.run_git` (hostile repo config can't run
programs), and model-supplied refs go through `gitsafe.rev_args`, so a ref
like `--output=<file>` or `--no-index` is refused instead of reaching git.
The tools that read the work tree (status, diff, blame, info) first check the
directory is inside the work tree git reports for it, and that this is the
repository's own — so a repository whose own `core.worktree` points
elsewhere or at an ancestor (or no repository at all) is refused —
and git_diff refuses `paths` that leave the work tree, which would otherwise
switch git to `--no-index` and diff any two files on disk. That holds for
every caller, `evi mcp serve` clients included.

Category is `git`. Defaults to off; flip `tools.git = true` to enable.
"""

from __future__ import annotations

import json
import os
import posixpath
import subprocess
from pathlib import Path

from evi import gitsafe
from evi.tools.base import tool


_GIT_TIMEOUT = 15.0
_OUTPUT_CAP = 32 * 1024  # 32 KB per tool call


def _git(args: list[str], cwd: str | None = None) -> tuple[int, str, str]:
    """Run `git <args>` and return (code, stdout, stderr). Truncates output
    so a huge diff doesn't blow the context window."""
    try:
        proc = gitsafe.run_git(args, cwd=cwd or None, timeout=_GIT_TIMEOUT)
    except gitsafe.GitNotFoundError:
        return 127, "", "git not found on PATH"
    except subprocess.TimeoutExpired:
        return 124, "", f"git {' '.join(args)} timed out after {_GIT_TIMEOUT}s"
    except OSError as exc:
        return 1, "", f"failed to invoke git: {exc}"
    out = (proc.stdout or "")[:_OUTPUT_CAP]
    err = (proc.stderr or "")[:_OUTPUT_CAP]
    return proc.returncode, out, err


def _resolve_cwd(path: str) -> str | None:
    """Return the directory to use as `cwd` for git, or None for current."""
    if not path or not path.strip():
        return None
    p = Path(path).expanduser()
    return str(p if p.is_dir() else p.parent)


def _work_tree_prefix(cwd: str | None) -> tuple[str | None, str]:
    """Where `cwd` sits in its repository's work tree: `(prefix, "")` with a
    prefix like "" (the top) or "src/", or `(None, "ERROR: ...")` when `cwd`
    is not inside one — no repository, a bare one, or a `core.worktree` in
    the repository's own config that points somewhere else (an ancestor
    such as the home directory included, see gitsafe.is_own_work_tree)."""
    code, out, err = _git(["rev-parse", "--is-inside-work-tree", "--show-prefix"], cwd=cwd)
    if code != 0:
        return None, f"ERROR: {(err or out).strip()}"
    lines = out.split("\n")
    prefix = lines[1].rstrip("\r") if len(lines) > 1 else ""
    if lines[0].strip() != "true" or not gitsafe.is_own_work_tree(cwd, prefix):
        return None, f"ERROR: {gitsafe.NOT_IN_WORK_TREE}"
    return prefix, ""


def _pathspecs_inside(paths: list[str], cwd: str | None, prefix: str) -> list[str]:
    """`paths` as pathspecs relative to `cwd`; ValueError for any that leaves
    the work tree.

    `git diff -- a b` where a or b is outside the repository silently turns
    into `git diff --no-index a b`, which prints any two files on disk. git
    decides that lexically — `<prefix of cwd><path>` with `..` resolved must
    not climb above the top — so this applies the same rule first, and turns
    absolute paths into relative ones so git judges them by that rule too.
    """
    base = os.path.abspath(cwd or os.getcwd())
    out: list[str] = []
    for raw in paths:
        p = raw.replace("\\", "/") if os.name == "nt" else raw
        if p.startswith("/") or os.path.splitdrive(p)[0]:
            if not os.path.isabs(p):
                raise ValueError(
                    f"path {raw!r} is drive- or root-relative; give it relative "
                    "to the repository or as a full absolute path"
                )
            try:
                p = os.path.relpath(p, base)
            except ValueError:  # another drive
                raise ValueError(f"path {raw!r} is outside the repository") from None
            if os.name == "nt":
                p = p.replace("\\", "/")
        rel = posixpath.normpath(prefix + p)
        if rel == ".." or rel.startswith("../"):
            raise ValueError(f"path {raw!r} is outside the repository")
        out.append(p)
    return out


@tool(
    description=(
        "Run `git status --short --branch` and return the working-tree summary."
    ),
    category="git",
)
def git_status(path: str = "") -> str:
    cwd = _resolve_cwd(path)
    prefix, refused = _work_tree_prefix(cwd)
    if prefix is None:
        return refused
    code, out, err = _git(["status", "--short", "--branch"], cwd=cwd)
    if code != 0:
        return f"ERROR: {err or out}"
    return out.strip() or "(clean working tree)"


@tool(
    description=(
        "Show a diff. `ref` is optional: omit for unstaged changes, "
        "`--staged` for the index, or any rev / range like `HEAD~3` or "
        "`main..feature`. `paths` is an optional list-of-paths to limit "
        "the diff scope."
    ),
    category="git",
)
def git_diff(ref: str = "", paths: str = "", path: str = "") -> str:
    args = ["diff", "--stat=200", "--patch", "--no-color"]
    ref = ref.strip()
    if ref in ("--staged", "--cached"):
        # The one option `ref` documents; everything else must be a revision.
        args.append("--cached")
    elif ref:
        try:
            args += gitsafe.rev_args(ref)
        except gitsafe.UnsafeRefError as exc:
            return f"ERROR: {exc}"
    cwd = _resolve_cwd(path)
    # Outside a repository git_diff would run as `git diff --no-index`.
    prefix, refused = _work_tree_prefix(cwd)
    if prefix is None:
        return refused
    if paths.strip():
        try:
            specs = _pathspecs_inside(paths.split(), cwd, prefix)
        except ValueError as exc:
            return f"ERROR: {exc}"
        args.append("--")
        args.extend(specs)
    code, out, err = _git(args, cwd=cwd)
    if code != 0:
        return f"ERROR: {err or out}"
    return out.strip() or "(no diff)"


@tool(
    description=(
        "Show the commit log. `limit` is the max commits (default 20). "
        "`paths` limits to changes touching specific files."
    ),
    category="git",
)
def git_log(limit: int = 20, paths: str = "", path: str = "") -> str:
    n = max(1, min(int(limit) or 20, 200))
    args = [
        "log",
        f"-n{n}",
        "--no-color",
        "--pretty=format:%h %ad %s%n  author: %an <%ae>%n",
        "--date=short",
    ]
    if paths.strip():
        args.append("--")
        args.extend(paths.split())
    code, out, err = _git(args, cwd=_resolve_cwd(path))
    if code != 0:
        return f"ERROR: {err or out}"
    return out.strip() or "(empty history)"


@tool(
    description=(
        "Show a specific commit with its diff. `ref` accepts any rev like "
        "`HEAD`, a short SHA, or a branch name."
    ),
    category="git",
)
def git_show(ref: str, path: str = "") -> str:
    if not ref.strip():
        return "ERROR: ref is required"
    try:
        revs = gitsafe.rev_args(ref)
    except gitsafe.UnsafeRefError as exc:
        return f"ERROR: {exc}"
    code, out, err = _git(["show", "--no-color", *revs], cwd=_resolve_cwd(path))
    if code != 0:
        return f"ERROR: {err or out}"
    return out.strip() or "(empty)"


@tool(
    description=(
        "Per-line blame of a file. Returns one line per source line: "
        "`<short-sha> <author> <date>  <text>`."
    ),
    category="git",
)
def git_blame(file_path: str, path: str = "") -> str:
    if not file_path.strip():
        return "ERROR: file_path is required"
    cwd = _resolve_cwd(path) or _resolve_cwd(file_path)
    target = file_path.strip()
    # If `file_path` is absolute, run blame from its directory.
    if Path(target).is_absolute():
        target_path = Path(target)
        cwd = str(target_path.parent)
        target = target_path.name
    prefix, refused = _work_tree_prefix(cwd)
    if prefix is None:
        return refused
    code, out, err = _git(
        ["blame", "--date=short", "--", target],
        cwd=cwd,
    )
    if code != 0:
        return f"ERROR: {err or out}"
    return out.strip() or "(empty)"


@tool(
    description=(
        "Return repo metadata as JSON: current branch, head commit, "
        "remote URL (if any), counts of dirty + untracked files."
    ),
    category="git",
)
def git_info(path: str = "") -> str:
    cwd = _resolve_cwd(path)
    out: dict[str, object] = {}
    code, branch, err = _git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=cwd)
    if code != 0:
        return f"ERROR: not a git repo: {err.strip()}"
    prefix, refused = _work_tree_prefix(cwd)
    if prefix is None:
        return refused
    out["branch"] = branch.strip()
    _, head, _ = _git(["rev-parse", "--short", "HEAD"], cwd=cwd)
    out["head"] = head.strip()
    _, remote, _ = _git(["remote", "get-url", "origin"], cwd=cwd)
    out["remote"] = remote.strip() or None
    _, dirty, _ = _git(["status", "--porcelain"], cwd=cwd)
    lines = [line for line in dirty.splitlines() if line.strip()]
    out["dirty_files"] = sum(1 for line in lines if not line.startswith("??"))
    out["untracked_files"] = sum(1 for line in lines if line.startswith("??"))
    return json.dumps(out)
