"""Cross-machine sync of the portable ``~/.evi`` state via a git remote.

Syncs the knowledge that should follow you between machines — memory, skills,
profiles, saved commands, routes, the MCP server list, and hooks — while
deliberately leaving behind anything per-machine, secret, large, or
rebuildable:

  synced:   memory/ skills/ profiles/ commands/ routes.json mcp.json hooks.toml
  ignored:  config.toml (per-machine backend + secrets), tokens/ (OAuth
            secrets), models/ + indices/ (large / rebuildable), logs/ images/
            screenshots/ uploads/ transcripts/ scheduled/ (machine-local).

The git repo lives at ``~/.evi/.git`` so the files stay in place; a managed
``.gitignore`` enforces the include/exclude split. Everything shells out to
``git`` (must be on PATH). All functions take an optional ``root`` so tests can
point at a temp home; the CLI uses the real ``~/.evi``.
"""

from __future__ import annotations

import socket
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import evi.config as config

# Top-level entries (relative to the eVi home) that travel between machines.
SYNCED_PATHS = (
    "memory",
    "skills",
    "profiles",
    "commands",
    "routes.json",
    "mcp.json",
    "hooks.toml",
)

# Ignore everything, then re-include only the portable state. Keeps per-machine
# config, secrets, and large/rebuildable data local even if new files appear.
GITIGNORE = """\
# eVi sync — managed by `evi sync`. Ignore everything by default, then
# re-include only the portable state. Per-machine config, secrets, and
# large/rebuildable data stay local. Edit with care.
/*
!/.gitignore
!/memory/
!/skills/
!/profiles/
!/commands/
!/routes.json
!/mcp.json
!/hooks.toml
# Belt-and-suspenders: never sync key material even if nested under a
# re-included directory.
**/*.key
**/*.pem
**/token*.json
"""


class SyncError(Exception):
    """A git operation failed or sync is misconfigured."""


@dataclass
class GitResult:
    ok: bool
    out: str


def _root(root: Path | None) -> Path:
    return root if root is not None else config.HOME


def _git(root: Path, *args: str, check: bool = False) -> GitResult:
    proc = subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True,
        text=True,
    )
    out = (proc.stdout + proc.stderr).strip()
    if check and proc.returncode != 0:
        raise SyncError(out or f"git {' '.join(args)} failed")
    return GitResult(proc.returncode == 0, out)


def is_initialized(root: Path | None = None) -> bool:
    return (_root(root) / ".git").is_dir()


def current_branch(root: Path | None = None) -> str:
    res = _git(_root(root), "rev-parse", "--abbrev-ref", "HEAD")
    branch = res.out.strip()
    # "HEAD" means no commits yet; fall back to the conventional default.
    return branch if res.ok and branch and branch != "HEAD" else "main"


def remote_url(root: Path | None = None) -> str:
    res = _git(_root(root), "remote", "get-url", "origin")
    return res.out.strip() if res.ok else ""


def write_gitignore(root: Path | None = None) -> None:
    (_root(root) / ".gitignore").write_text(GITIGNORE, encoding="utf-8")


def init(remote: str | None = None, branch: str = "main", root: Path | None = None) -> str:
    """Initialise the sync repo (idempotent). Sets the managed .gitignore and,
    if given, the ``origin`` remote."""
    r = _root(root)
    config.ensure_dirs()
    if not is_initialized(root):
        _git(r, "init", check=True)
        _git(r, "branch", "-M", branch)
    write_gitignore(root)
    if remote:
        if _git(r, "remote", "get-url", "origin").ok:
            _git(r, "remote", "set-url", "origin", remote, check=True)
        else:
            _git(r, "remote", "add", "origin", remote, check=True)
    lines = [f"initialised sync at {r}"]
    url = remote_url(root)
    if url:
        lines.append(f"remote: {url}")
    else:
        lines.append("no remote set — add one with `evi sync init <git-url>`")
    return "\n".join(lines)


def status(root: Path | None = None) -> str:
    if not is_initialized(root):
        raise SyncError("not initialised — run `evi sync init <git-url>` first")
    r = _root(root)
    head = _git(r, "status", "--short", "--branch").out
    url = remote_url(root) or "(none)"
    return f"remote: {url}\n{head}"


def _has_staged_changes(root: Path) -> bool:
    # `git diff --cached --quiet` exits 1 when there are staged changes.
    return not _git(root, "diff", "--cached", "--quiet").ok


def push(message: str | None = None, root: Path | None = None) -> str:
    """Stage everything tracked by the include rules, commit if there are
    changes, and push to origin."""
    if not is_initialized(root):
        raise SyncError("not initialised — run `evi sync init <git-url>` first")
    r = _root(root)
    _git(r, "add", "-A", check=True)
    committed = False
    if _has_staged_changes(r):
        msg = message or f"sync from {socket.gethostname()} at {datetime.now().isoformat(timespec='seconds')}"
        _git(r, "commit", "-m", msg, check=True)
        committed = True
    if not remote_url(root):
        return "committed locally (no remote set)" if committed else "nothing to sync (no remote set)"
    branch = current_branch(root)
    res = _git(r, "push", "-u", "origin", branch)
    if not res.ok:
        raise SyncError("push failed:\n" + res.out)
    if committed:
        return f"pushed to origin/{branch}"
    return f"already up to date (origin/{branch})"


def pull(root: Path | None = None) -> str:
    """Pull and merge remote changes into the local home.

    The first pull on a new machine is special: there are no local commits yet,
    only the freshly-written (untracked) managed .gitignore, which a normal
    merge would refuse to overwrite. In that case we fetch and force-check-out
    the remote branch — adopting the synced state. Subsequent pulls do an
    ordinary merge so locally-committed changes are preserved."""
    if not is_initialized(root):
        raise SyncError("not initialised — run `evi sync init <git-url>` first")
    if not remote_url(root):
        raise SyncError("no remote set — run `evi sync init <git-url>` first")
    r = _root(root)
    branch = current_branch(root)
    has_commits = _git(r, "rev-parse", "--verify", "HEAD").ok
    if not has_commits:
        fetched = _git(r, "fetch", "origin", branch)
        if not fetched.ok:
            raise SyncError(
                f"fetch failed — does origin have a '{branch}' branch yet? "
                f"Run `evi sync push` on another machine first:\n{fetched.out}"
            )
        res = _git(r, "checkout", "-f", "-B", branch, f"origin/{branch}")
        if not res.ok:
            raise SyncError("checkout failed:\n" + res.out)
        return f"pulled origin/{branch} (first sync on this machine)"
    res = _git(r, "pull", "--no-rebase", "origin", branch)
    if not res.ok:
        raise SyncError(
            "pull failed (a merge conflict, or the branch doesn't exist yet):\n" + res.out
        )
    return res.out or f"up to date with origin/{branch}"
