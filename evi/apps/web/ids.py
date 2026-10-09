"""Session-id validation for the web server.

A session id is joined onto on-disk directories (`~/.evi/uploads/<session>/`),
and it arrives as a raw form field or URL segment. pathlib will happily walk
`..` out of the base or, for an absolute id (`C:/x`, `/abs`, `\\\\server\\share`),
replace the base outright — so an id must be checked before it touches a path.

Every id eVi mints or accepts is a plain token:
  - web UI tabs        `crypto.randomUUID()`        (36, hex + `-`)
  - CLI / SDK agents   `secrets.token_hex(6)`       (12, hex)
  - branched sessions  `secrets.token_hex(8)`       (16, hex)
  - VS Code sidebar    `vscode`
  - Telegram channel   `telegram-<user id>`
  - imported sessions  `claude-code-<uuid>` (48) and
                       `codex-rollout-<YYYY-MM-DDTHH-MM-SS>-<uuid>` (70)
so the allowlist is letters, digits, `_` and `-` — no separators, drive colons,
dots, whitespace or NUL — with a length bound that leaves room for the longest
imported id while keeping `uploads/<session>/<file>` well inside Windows'
260-character MAX_PATH.
"""

from __future__ import annotations

import re

SESSION_ID_MAX_LEN = 96
SESSION_ID_RE = re.compile(rf"[A-Za-z0-9_-]{{1,{SESSION_ID_MAX_LEN}}}")

# Windows treats these as devices in any directory (`uploads\nul` is not a
# folder), so an id spelled like one can never be a safe directory name.
_WINDOWS_RESERVED = frozenset(
    {"con", "prn", "aux", "nul"}
    | {f"com{i}" for i in range(10)}
    | {f"lpt{i}" for i in range(10)}
)


def valid_session_id(session_id: object) -> bool:
    """True if `session_id` is safe to use as a single path component."""
    if not isinstance(session_id, str):
        return False
    if SESSION_ID_RE.fullmatch(session_id) is None:
        return False
    return session_id.lower() not in _WINDOWS_RESERVED
