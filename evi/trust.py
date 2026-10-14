"""Folder trust — what a repository may change about eVi before you say so.

A repository you clone arrives with files eVi would otherwise act on: a
`.evi.toml` (per-project config) today, and project commands, hooks, MCP
servers, skills and agents as those land. None of that takes effect until you
trust the folder:

- `evi chat` asks the first time it meets a folder with a project config;
- `evi trust [path]` trusts one up front after you confirm (never from
  inside an eVi session, so the agent can't do it for you); `--list` /
  `--revoke` / `--status` manage the list;
- surfaces that can't ask — `evi run`, `evi batch`, the web server, the
  scheduler, subagents — never prompt: there an untrusted folder's project
  layer is simply off (fail closed), with a one-line warning.

EVI.md / AGENTS.md text is NOT gated: it is prompt content and can't run
anything or change a setting.

Identity. Trust is recorded per repository, not per checkout: the key is the
real path of the git common dir, so every linked worktree of a trusted repo
is trusted too. A `.git` FILE counts as a linked worktree only when its
admin dir sits in that git dir's `worktrees/` and points back at it —
otherwise any folder could name a trusted repository's git dir (or ship its
own admin dir that does) and borrow its trust — so such a folder (and a
submodule, or a `.git` symlink) is keyed by its own path. Outside git the key
is the folder's real path, and trust covers that folder only — not its
subfolders, so nothing unpacked into a trusted folder later rides along on
it. Project discovery stops at the git top level (evi/project.py), so a parent
folder's files never ride along on a repository's trust.

Pinning. Trusting also approves the current `.evi.toml` content (sha256 of
its bytes). Editing the file withdraws that approval until you trust again;
instruction-file (EVI.md) edits don't, they are prompt content. An entry
keeps the last few approved versions, so worktrees on branches with
different `.evi.toml` files don't keep invalidating each other.

Even a trusted `.evi.toml` goes through the allowlist in evi/project.py: it
can set model/sampling preferences and only tighten tools/permissions.

Loaders of anything else a repository ships that can execute or change
settings must check `is_trusted(root)` first.

The list lives in `~/.evi/trusted_folders.json` (EVI_HOME), a file only you
write — a repository can't trust itself.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from evi.project import (
    ProjectConfigFile,
    _printable,
    filter_project_overlay,
    format_key_list,
    git_toplevel,
    project_config_keys,
    read_project_config,
)

logger = logging.getLogger(__name__)

TRUST_FILENAME = "trusted_folders.json"
_VERSION = 1
_MAX_APPROVED = 16  # approved .evi.toml versions kept per entry
_MAX_STORE_BYTES = 4 * 1024 * 1024
_MAX_POINTER_BYTES = 4096  # a .git / commondir / gitdir pointer file

_UNSET: Any = object()


def store_path() -> Path:
    """`<eVi home>/trusted_folders.json`, read at call time (EVI_HOME)."""
    from evi import config

    return config.HOME / TRUST_FILENAME


# --- identity ---------------------------------------------------------------


@dataclass(frozen=True)
class Identity:
    """What trust is recorded against.

    `key` is the store key (case-normalised real path); `root` is what the
    user sees — the work tree's top level, or the folder; `kind` is "git" or
    "folder". `shared` is True when the key is the repository's git common
    dir, i.e. every worktree of the repository shares the trust."""

    key: str
    root: Path
    kind: str
    shared: bool = False


def _norm(path: Path | str) -> str:
    return os.path.normcase(os.path.realpath(path))


def _read_pointer(path: Path) -> str:
    """The first line of a small git pointer file, stripped ("" if empty)."""
    with path.open("rb") as f:
        lines = f.read(_MAX_POINTER_BYTES).decode("utf-8").splitlines()
    return lines[0].strip() if lines else ""


def _git_common_dir(top: Path) -> Path | None:
    """The git common dir of the work tree at `top`, or None when it can't be
    verified to belong to `top` (then trust is keyed by `top` itself)."""
    dotgit = top / ".git"
    try:
        # A symlink or (Windows) directory junction could point at any
        # repository's git dir, so it never shares that repository's trust.
        if dotgit.is_symlink() or dotgit.is_junction():
            return None
        if dotgit.is_dir():
            return dotgit
        line = _read_pointer(dotgit)
        if not line.startswith("gitdir:"):
            return None
        gitdir = Path(line[len("gitdir:"):].strip())
        if not gitdir.is_absolute():
            gitdir = top / gitdir
        commondir = gitdir / "commondir"
        if not commondir.is_file():
            return None  # a submodule or --separate-git-dir: nothing to verify
        # A linked worktree's admin dir records where its `.git` file is
        # (relative to the admin dir with git's worktree.useRelativePaths).
        back = Path(_read_pointer(gitdir / "gitdir"))
        if not back.parts:
            return None
        if not back.is_absolute():
            back = gitdir / back
        if _norm(back) != _norm(dotgit):
            return None
        common = Path(_read_pointer(commondir))
        if not common.is_absolute():
            common = gitdir / common
        # git keeps a linked worktree's admin dir at <common>/worktrees/<name>.
        # One anywhere else — e.g. shipped inside the folder itself, with a
        # `commondir` naming a trusted repository and a back-pointer to the
        # folder — is a forgery: a folder can't write inside another
        # repository's git dir, so this is what ties the two together.
        if _norm(gitdir.parent) != _norm(common / "worktrees"):
            return None
        if not ((common / "HEAD").is_file() and (common / "objects").is_dir()):
            return None  # not a git dir (e.g. a trusted plain folder)
        return common
    except (OSError, ValueError):  # ValueError covers UnicodeDecodeError
        return None


def identify(start: Path | None = None) -> Identity:
    """The trust identity of the folder `start` (default cwd)."""
    folder = (start or Path.cwd()).resolve()
    top = git_toplevel(folder)
    if top is None:
        return Identity(_norm(folder), folder, "folder")
    common = _git_common_dir(top)
    if common is None:
        return Identity(_norm(top), top, "git")
    return Identity(_norm(common), top, "git", shared=True)


# --- the store --------------------------------------------------------------


_WARNED: set[str] = set()  # unusable trust lists already reported


def _warn_store(path: Path, problem: str) -> None:
    if str(path) in _WARNED:
        return
    _WARNED.add(str(path))
    logger.warning("eVi: %s %s; treating every folder as untrusted.", path, problem)


def _load() -> dict[str, dict[str, Any]]:
    """The trusted entries by key. {} when there are none or the file can't be
    used — fail closed: then nothing is trusted."""
    path = store_path()
    try:
        with path.open("rb") as f:
            raw = f.read(_MAX_STORE_BYTES + 1)
    except FileNotFoundError:
        return {}
    except OSError:
        _warn_store(path, "can't be read")
        return {}
    data: Any = None
    if len(raw) <= _MAX_STORE_BYTES:
        try:
            data = json.loads(raw.decode("utf-8"))
        except (ValueError, RecursionError):
            data = None
    folders = data.get("folders") if isinstance(data, dict) else None
    if not isinstance(folders, dict):
        _warn_store(path, "is not a valid trust list")
        return {}
    return {k: v for k, v in folders.items() if isinstance(k, str) and isinstance(v, dict)}


def _save(folders: dict[str, dict[str, Any]]) -> None:
    """Write the store atomically (a temp file, then a rename)."""
    path = store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps({"version": _VERSION, "folders": folders}, indent=2, sort_keys=True)
    fd, tmp = tempfile.mkstemp(prefix=".trusted_folders-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(text + "\n")
        for attempt in range(5):
            try:
                os.replace(tmp, path)
                break
            except PermissionError:
                # Windows refuses to replace a file another process (a web
                # request's Config.load) has open this instant; it's brief.
                if attempt == 4:
                    raise
                time.sleep(0.05 * (attempt + 1))
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _approved(entry: dict[str, Any]) -> list[str]:
    hashes = entry.get("config_sha256")
    return [h for h in hashes if isinstance(h, str)] if isinstance(hashes, list) else []


def _match(
    ident: Identity, entries: dict[str, dict[str, Any]]
) -> tuple[str | None, dict[str, Any] | None]:
    """The entry recorded for `ident` itself, of the same kind. Trust never
    spreads to other folders: outside git a trusted folder's subfolders
    (an archive unpacked into it later, say) are not trusted, and a folder
    that turns into a repository is asked about again."""
    entry = entries.get(ident.key)
    if entry is not None and entry.get("kind") == ident.kind:
        return ident.key, entry
    return None, None


# --- status / grant / revoke --------------------------------------------------


@dataclass(frozen=True)
class TrustStatus:
    """Whether the project layer for a folder may load, and why (not)."""

    folder: Path
    identity: Identity
    entry_key: str | None  # the folder's own store entry (never a parent's)
    entry_path: str  # that entry's recorded folder (for messages)
    config: ProjectConfigFile | None  # the folder's .evi.toml, if any
    config_approved: bool  # that exact file version was trusted

    @property
    def trusted(self) -> bool:
        """The folder (its repository, inside git) is on the trust list."""
        return self.entry_key is not None

    @property
    def has_config(self) -> bool:
        """There is a `.evi.toml` with settings in it."""
        return self.config is not None and bool(self.config.data)

    @property
    def project_layer_on(self) -> bool:
        """Trusted, and the current `.evi.toml` (if any) is a trusted version."""
        return self.trusted and (not self.has_config or self.config_approved)

    @property
    def state(self) -> str:
        """"trusted", "changed" (trusted, but the .evi.toml is new or was
        edited since) or "untrusted"."""
        if not self.trusted:
            return "untrusted"
        return "trusted" if self.project_layer_on else "changed"

    @property
    def needs_prompt(self) -> bool:
        """A project config is waiting for the user's decision."""
        return self.has_config and not self.project_layer_on


def status(start: Path | None = None, *, config: Any = _UNSET) -> TrustStatus:
    """The trust status of `start` (default cwd). `config` is the folder's
    already-read `.evi.toml` (a ProjectConfigFile, or None for none) when the
    caller has it — the trust check then pins exactly those bytes."""
    folder = (start or Path.cwd()).resolve()
    ident = identify(folder)
    pc = read_project_config(folder) if config is _UNSET else config
    key, entry = _match(ident, _load())
    approved = entry is not None and pc is not None and pc.sha256 in _approved(entry)
    return TrustStatus(
        folder=folder,
        identity=ident,
        entry_key=key,
        entry_path=str(entry.get("path", "")) if entry is not None else "",
        config=pc,
        config_approved=approved,
    )


def is_trusted(start: Path | None = None) -> bool:
    """May the project layer of `start` (default cwd) load? The gate for
    every loader of repository-supplied settings or executables."""
    return status(start).project_layer_on


def grant(start: Path | None = None, *, config: Any = _UNSET) -> TrustStatus:
    """Trust the folder / repository at `start` (default cwd) and approve its
    current `.evi.toml`. Only ever called on the user's explicit say-so."""
    folder = (start or Path.cwd()).resolve()
    ident = identify(folder)
    pc = read_project_config(folder) if config is _UNSET else config
    entries = _load()
    hashes = _approved(entries.get(ident.key, {}))
    if pc is not None and pc.data:
        hashes = [pc.sha256] + [h for h in hashes if h != pc.sha256]
    entries[ident.key] = {
        "path": str(ident.root),
        "kind": ident.kind,
        "trusted_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "config_sha256": hashes[:_MAX_APPROVED],
    }
    _save(entries)
    return status(folder, config=pc)


def revoke(start: Path | None = None) -> str | None:
    """Stop trusting the folder at `start` (default cwd): remove its entry
    (its repository's, inside git), or the one recorded for that exact path
    (e.g. a deleted repository). Never a parent folder's entry. Returns the
    removed entry's folder, or None."""
    folder = (start or Path.cwd()).resolve()
    entries = _load()
    key, entry = _match(identify(folder), entries)
    if key is None:
        want = os.path.normcase(str(folder))
        for k, e in entries.items():
            if os.path.normcase(str(e.get("path", ""))) == want:
                key, entry = k, e
                break
    if key is None or entry is None:
        return None
    del entries[key]
    _save(entries)
    return str(entry.get("path") or key)


def list_trusted() -> list[dict[str, Any]]:
    """The trust list, sorted by folder: {path, kind, trusted_at, approved}."""
    rows = [
        {
            "path": str(e.get("path", "")),
            "kind": str(e.get("kind", "")),
            "trusted_at": str(e.get("trusted_at", "")),
            "approved": len(_approved(e)),
        }
        for e in _load().values()
    ]
    return sorted(rows, key=lambda r: r["path"].lower())


# --- presentation (CLI prompt, `evi trust`, web) ------------------------------


def scope_text(ident: Identity) -> str:
    """What trusting `ident` covers, in words."""
    root = _printable(str(ident.root))
    if ident.kind == "git" and ident.shared:
        return f"the git repository at {root} (and all its worktrees)"
    if ident.kind == "git":
        return f"the git checkout at {root}"
    return f"the folder {root} (not its subfolders)"


def describe(st: TrustStatus) -> list[str]:
    """Plain-text lines summarising what trusting `st.folder` would turn on.
    Key names only — values are never echoed (they come from the repo)."""
    lines: list[str] = []
    pc = st.config
    if pc is not None and pc.data:
        lines.append(f"Project config: {_printable(str(pc.path))}")
        if st.state == "changed":
            lines.append(
                "  It is new or changed since you trusted this folder, so eVi is not applying it."
            )
        elif st.state == "untrusted":
            lines.append("  This folder is not trusted, so eVi is not applying it.")
        else:
            lines.append("  Applied: this folder is trusted.")
        lines.append(f"  It sets: {format_key_list(project_config_keys(pc.data))}")
        safe, ignored = filter_project_overlay(pc.data)
        applies = project_config_keys(safe)
        if applies:
            lines.append(
                f"  Applied when trusted: {format_key_list(applies)} "
                "([tools] / [auto] only ever tighten yours)"
            )
        if ignored:
            lines.append(f"  Never applied from a .evi.toml: {format_key_list(ignored)}")
    lines.append(f"Trust covers {scope_text(st.identity)}.")
    return lines


def status_dict(st: TrustStatus) -> dict[str, Any]:
    """JSON-ready status (web API)."""
    pc = st.config
    data = pc.data if pc is not None else {}
    safe, ignored = filter_project_overlay(data) if data else ({}, [])
    return {
        "folder": str(st.folder),
        "root": str(st.identity.root),
        "kind": st.identity.kind,
        "shared": st.identity.shared,
        "scope": scope_text(st.identity),
        "state": st.state,
        "trusted": st.trusted,
        "needs_prompt": st.needs_prompt,
        "project_layer_on": st.project_layer_on,
        "config_path": str(pc.path) if pc is not None else None,
        "config_sha256": pc.sha256 if pc is not None else "",
        "config_keys": project_config_keys(data),
        "applies_if_trusted": project_config_keys(safe),
        "never_applied": ignored,
    }
