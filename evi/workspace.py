"""Workspace-only file access: `[auto] workspace_only`.

``"off"`` (the default) changes nothing. ``"writes"`` refuses a call that
would write a file outside the workspace; ``"all"`` also refuses one that
names a file or folder to read outside it. It holds in every permission mode,
yolo included, and no allow rule clears it (`evi.permissions.decide` checks
it right after hard_deny and the director's ground).

The **workspace** is the session's working folder, the `[auto] trusted_dirs`
folders (``/add-dir`` adds to them for the session) and, with `[auto]
workspace_allow_temp` (default true), the system temp folder. With ``"all"``
eVi's own folders the model is shown files from (`READ_EXEMPT_DIRS`: spilled
tool output, uploads, skills, images, screenshots) stay readable.

What is checked is what `evi.permissions` already reads from a call: its path
arguments (`evi.tools.base.Tool.path_args`) and, for a shell line, its output
redirects and the files its writing commands name (``tee``, ``cp``,
``Set-Content`` ...). A path is resolved on its real path, so a symlink or
junction inside the workspace that leads out of it is outside; a model-chosen
network path is never opened to find out (`evi.policy._resolve`) and counts
as outside unless it is written under a workspace folder on that share. A
relative write after a ``cd`` eVi can't follow counts as outside.

Like the protected paths it is a guardrail, not a sandbox: code a tool runs
(``run_python``, a script, ``bash -c``) can still reach any file the user can.
Pure apart from resolving paths; `decide` calls `outside_workspace`.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Iterable

from evi import shellsplit

LEVELS = ("off", "writes", "all")
# Under <eVi home>: folders whose files eVi itself hands the model (a spilled
# tool result it pages back with read_file, an uploaded attachment, a skill's
# files, an image or screenshot a tool saved). Readable with "all".
READ_EXEMPT_DIRS = ("tool-output", "uploads", "skills", "images", "screenshots")
# Tool categories whose path arguments name files the call reads.
_FILE_CATEGORIES = ("fs", "code", "code_read")


def level_of(value: object) -> str:
    """`value` as a level; anything unrecognised is the strictest, "all"
    (a typo must not switch the check off)."""
    s = str(value or "off").strip().lower()
    return s if s in LEVELS else "all"


def stricter(a: str, b: str) -> str:
    """The stricter of two levels."""
    return max(level_of(a), level_of(b), key=LEVELS.index)


def _root(value: str | Path) -> list[Path]:
    """`value` as workspace roots: as written (absolute) and on its real path.
    These are folders the user chose, so resolving them is fine."""
    try:
        p = Path(value).expanduser()
        if not p.is_absolute():
            return []
        out = [p]
        try:
            out.append(p.resolve())
        except (OSError, RuntimeError, ValueError):
            pass
        return out
    except (OSError, RuntimeError, ValueError):
        return []


def roots(
    workdir: str | Path | None,
    trusted_dirs: Iterable[str] = (),
    *,
    allow_temp: bool = True,
) -> list[Path]:
    """The workspace folders: `workdir` (default: `evi.workdir.get_cwd()`),
    each trusted dir, and the temp folder when `allow_temp`."""
    from evi import workdir as _workdir

    out: list[Path] = []
    for value in [workdir or _workdir.get_cwd(), *(trusted_dirs or ())]:
        if str(value).strip():
            out += _root(value)
    if allow_temp:
        out += _root(tempfile.gettempdir())
    return out


def read_exempt_roots() -> list[Path]:
    """eVi's own folders the model reads files from (`READ_EXEMPT_DIRS`)."""
    from evi import config as _config

    return [r for name in READ_EXEMPT_DIRS for r in _root(Path(_config.HOME) / name)]


def _inside(value: str, base: Path, roots_: list[Path]) -> bool:
    from evi.policy import _inside as inside
    from evi.policy import _resolve

    p = _resolve(value, base, roots_)
    return p is not None and inside(p, roots_)


def outside_workspace(
    tool_name: str,
    tool_category: str,
    tool_args: str | dict,
    level: str,
    workspace: list[Path],
    *,
    workdir: str | Path | None = None,
    path_args: list[str] | None = None,
    read_only: bool | None = None,
    call_category: str | None = None,
    dialect: str | None = None,
    split: shellsplit.Split | None = None,
    read_exempt: list[Path] | None = None,
) -> str | None:
    """The first path this call reaches outside `workspace` under `level`
    (as written, with why when it isn't a plain path), or None.

    "writes": what `evi.permissions.protected_write` reads as written by the
    call (path arguments of a tool that writes, a shell line's redirects and
    writing commands). "all": also every path argument of an fs/code/code_read
    call, and a glob argument that climbs out with ``..``; `read_exempt`
    (default: `read_exempt_roots`) is readable too."""
    level = level_of(level)
    if level == "off":
        return None
    from evi import permissions as P

    base = P._base_dir(workdir)
    data = P._arg_dict(tool_args) or {}
    if path_args is None:
        from evi.tools.base import path_values

        path_args = path_values(data, tool_category)
    written = P._write_targets(
        tool_name, tool_category, tool_args, ["*"], workdir=workdir, path_args=path_args,
        read_only=read_only, dialect=dialect, split=split,
    )
    if isinstance(written, str):
        return written
    for value, wbase, shown in written:
        if not _inside(value, wbase, workspace):
            return shown
    if level != "all":
        return None
    cat = call_category or P.classify_call(tool_name, tool_category, tool_args)
    if cat not in _FILE_CATEGORIES and tool_category not in _FILE_CATEGORIES:
        return None
    readable = workspace + (read_exempt_roots() if read_exempt is None else read_exempt)
    for value in path_args:
        if value and not _inside(value, base, readable):
            return value
    from evi.tools.base import glob_values

    for g in glob_values(tool_name, data):
        if P._glob_escapes(g):
            return f"{g} (a pattern that climbs out of the folder it searches)"
    return None
