"""Multi-user auth for the web UI (opt-in).

eVi is single-user by default. With ``[web] multi_user = true`` a trusted small
team can each authenticate with their own token from ``~/.evi/users.json``:

    [
      {"name": "alice", "token": "…"},
      {"name": "bob", "token": "…"}
    ]

This is a *shared workspace* — sessions and memory are common to everyone who
authenticates; the win over a single shared `auth_token` is per-user, revocable
logins (drop a user from the file to cut their access). Per-user data isolation
is a separate, larger feature. Tokens compare in constant time.
"""

from __future__ import annotations

import json
import secrets
from dataclasses import dataclass
from pathlib import Path

from evi.config import USERS_PATH


@dataclass(frozen=True)
class User:
    name: str
    token: str


def load_users(path: Path | None = None) -> list[User]:
    """Read ~/.evi/users.json. Missing/malformed → []; bad entries skipped."""
    p = path or USERS_PATH
    if not p.is_file():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(data, list):
        return []
    out: list[User] = []
    for e in data:
        if not isinstance(e, dict):
            continue
        name = str(e.get("name") or "").strip()
        token = str(e.get("token") or "").strip()
        if name and token:
            out.append(User(name=name, token=token))
    return out


def save_users(users: list[User], path: Path | None = None) -> None:
    p = path or USERS_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = [{"name": u.name, "token": u.token} for u in users]
    p.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def add_user(name: str, path: Path | None = None) -> User:
    """Add (or re-issue) a user with a fresh random token. Returns the User."""
    name = name.strip()
    if not name:
        raise ValueError("name is required")
    users = [u for u in load_users(path) if u.name.lower() != name.lower()]
    user = User(name=name, token=secrets.token_urlsafe(24))
    users.append(user)
    save_users(users, path)
    return user


def remove_user(name: str, path: Path | None = None) -> bool:
    users = load_users(path)
    kept = [u for u in users if u.name.lower() != name.strip().lower()]
    if len(kept) == len(users):
        return False
    save_users(kept, path)
    return True


def authenticate(provided: str, users: list[User]) -> User | None:
    """Return the user whose token matches `provided` (constant-time), or None."""
    if not provided:
        return None
    match: User | None = None
    # Compare against every user (no early exit) so timing doesn't leak which
    # token prefix matched.
    for u in users:
        if secrets.compare_digest(provided, u.token):
            match = u
    return match
