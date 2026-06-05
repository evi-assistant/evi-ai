"""Git intelligence tools — read-only inspection of a local repository.

Wraps the `git` CLI in subprocess calls. All commands are deliberately
read-only (status, diff, log, show, blame). The `worktree` family stays in
`evi.worktree` since it mutates repo state.

Category is `git`. Defaults to off; flip `tools.git = true` to enable.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

from evi.tools.base import tool


_GIT_TIMEOUT = 15.0
_OUTPUT_CAP = 32 * 1024  # 32 KB per tool call


def _git(args: list[str], cwd: str | None = None) -> tuple[int, str, str]:
    """Run `git <args>` and return (code, stdout, stderr). Truncates output
    so a huge diff doesn't blow the context window."""
    if shutil.which("git") is None:
        return 127, "", "git not found on PATH"
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=cwd or None,
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT,
        )
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


@tool(
    description=(
        "Run `git status --short --branch` and return the working-tree summary."
    ),
    category="git",
)
def git_status(path: str = "") -> str:
    code, out, err = _git(["status", "--short", "--branch"], cwd=_resolve_cwd(path))
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
    if ref.strip():
        args.append(ref.strip())
    if paths.strip():
        args.append("--")
        args.extend(paths.split())
    code, out, err = _git(args, cwd=_resolve_cwd(path))
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
    code, out, err = _git(["show", "--no-color", ref.strip()], cwd=_resolve_cwd(path))
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
