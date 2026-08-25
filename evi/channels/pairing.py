"""Who is allowed to talk to eVi through a channel.

An inbound chat channel drives a tool-capable assistant, so "anyone who finds
the bot" must never be the access rule. An unknown sender gets a short pairing
code and **no agent turn at all** until the owner approves it from the machine
eVi runs on — approval requires local access, which is the property that makes
this safe. Same posture as the rest of eVi: deny by default, approve explicitly.

State lives in `~/.evi/channels/<channel>.json` rather than config.toml so the
UI's config round-trip can never clobber an approval list.
"""

from __future__ import annotations

import json
import secrets
import time
from pathlib import Path
from typing import Any

from evi.config import HOME

# Short enough to read off a phone, long enough not to be guessed in the window
# it's alive (and guessing only ever gets you into a queue for manual approval).
CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"   # no I/O/0/1 — read aloud safely
CODE_LEN = 6
PENDING_TTL = 3600.0   # an unapproved request expires after an hour


def _store_path(channel: str) -> Path:
    return HOME / "channels" / f"{channel}.json"


def _load(channel: str) -> dict[str, Any]:
    try:
        return json.loads(_store_path(channel).read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {"paired": {}, "pending": {}}


def _save(channel: str, data: dict[str, Any]) -> None:
    p = _store_path(channel)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _prune(data: dict[str, Any]) -> dict[str, Any]:
    now = time.time()
    data["pending"] = {
        code: v for code, v in (data.get("pending") or {}).items()
        if now - float(v.get("at", 0)) < PENDING_TTL
    }
    return data


def is_paired(channel: str, user_id: Any) -> bool:
    return str(user_id) in (_load(channel).get("paired") or {})


def paired_users(channel: str) -> list[dict[str, Any]]:
    return [{"user_id": k, **v} for k, v in (_load(channel).get("paired") or {}).items()]


def pending_requests(channel: str) -> list[dict[str, Any]]:
    data = _prune(_load(channel))
    return [{"code": k, **v} for k, v in (data.get("pending") or {}).items()]


def request_pairing(channel: str, user_id: Any, name: str = "") -> str:
    """Record an unknown sender and return the code they must have approved.

    Re-requesting returns the SAME code rather than minting a new one, so a
    confused user messaging repeatedly doesn't fill the queue with codes the
    owner then has to disambiguate.
    """
    data = _prune(_load(channel))
    for code, v in (data.get("pending") or {}).items():
        if str(v.get("user_id")) == str(user_id):
            return code
    code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LEN))
    data.setdefault("pending", {})[code] = {
        "user_id": str(user_id), "name": name, "at": time.time(),
    }
    _save(channel, data)
    return code


def approve(channel: str, code: str) -> dict[str, Any] | None:
    """Approve a pending code. Returns the paired entry, or None if the code is
    unknown or expired."""
    data = _prune(_load(channel))
    entry = (data.get("pending") or {}).pop(code.strip().upper(), None)
    if entry is None:
        _save(channel, data)
        return None
    uid = str(entry["user_id"])
    data.setdefault("paired", {})[uid] = {
        "name": entry.get("name", ""), "approved_at": time.time(),
    }
    _save(channel, data)
    return {"user_id": uid, **data["paired"][uid]}


def revoke(channel: str, user_id: Any) -> bool:
    data = _load(channel)
    if str(user_id) in (data.get("paired") or {}):
        data["paired"].pop(str(user_id))
        _save(channel, data)
        return True
    return False
