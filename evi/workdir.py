"""Per-session working directory — the agent's "working folder".

Tools resolve relative paths against a session-scoped working directory rather
than the process cwd, so multiple sessions (e.g. several web tabs) can operate
in different folders WITHOUT ``os.chdir`` (which is process-global and would
make sessions collide). It's a ``ContextVar`` so each async request/turn gets
its own value; when unset, everything falls back to the process cwd — so the
single-session CLI and the test suite behave exactly as before.

The Agent sets it from ``Agent.cwd`` at the start of a turn; the web server
sets it per request from the session's working dir.
"""

from __future__ import annotations

import contextvars
from pathlib import Path

_CWD: contextvars.ContextVar[str] = contextvars.ContextVar("evi_cwd", default="")


def set_cwd(path: str | Path | None) -> contextvars.Token:
    """Set the session working dir (``""``/None → fall back to process cwd).
    Returns a token for :func:`reset`."""
    return _CWD.set(str(path) if path else "")


def reset(token: contextvars.Token | None) -> None:
    if token is not None:
        try:
            _CWD.reset(token)
        except (ValueError, LookupError):
            pass


def get_cwd() -> Path:
    """The active session working dir, or the process cwd when unset."""
    v = _CWD.get()
    return Path(v) if v else Path.cwd()


def resolve(path: str | Path) -> Path:
    """Resolve `path` for a file tool: ``~`` expands; absolute paths pass
    through; a relative path is taken against the session working dir."""
    p = Path(path).expanduser()
    return p if p.is_absolute() else (get_cwd() / p)
