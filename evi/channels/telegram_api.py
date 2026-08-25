"""Minimal Telegram Bot API client — stdlib only.

Long polling (`getUpdates`), deliberately not webhooks: a webhook needs a public
HTTPS URL, which for a personal machine means a tunnel, a domain or an open port.
Polling works from behind NAT with nothing exposed, which is the right default
for a local-first assistant.

Stdlib `urllib` + `json` only, so this bundles into the frozen desktop sidecar
with no new dependency (same rule the managed runtime follows).
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

API_ROOT = "https://api.telegram.org"

# Telegram caps a text message at 4096 chars; longer replies are split so a long
# answer arrives in full rather than being rejected outright.
MAX_TEXT = 4096


class TelegramError(RuntimeError):
    """A call the bot API rejected (bad token, revoked bot, rate limit…)."""


def _call(token: str, method: str, params: dict[str, Any] | None = None,
          *, timeout: float = 15.0) -> Any:
    url = f"{API_ROOT}/bot{token}/{method}"
    data = json.dumps(params or {}).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            body = json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = json.loads(exc.read().decode("utf-8", "replace")).get("description", "")
        except Exception:  # noqa: BLE001
            pass
        raise TelegramError(f"{method} failed: HTTP {exc.code} {detail}".strip()) from exc
    except Exception as exc:  # noqa: BLE001
        raise TelegramError(f"{method} failed: {type(exc).__name__}: {exc}") from exc
    if not body.get("ok"):
        raise TelegramError(f"{method} failed: {body.get('description', 'unknown error')}")
    return body.get("result")


def get_me(token: str) -> dict[str, Any]:
    """Identify the bot behind `token`. Used to validate a token before saving it,
    so a typo is reported at setup rather than as silent nothing-happens later."""
    return _call(token, "getMe", timeout=10.0)


def send_message(token: str, chat_id: int | str, text: str) -> None:
    """Send `text`, split across messages if it exceeds Telegram's 4096 limit."""
    body = text if text.strip() else "(no reply)"
    for i in range(0, len(body), MAX_TEXT):
        _call(token, "sendMessage", {"chat_id": chat_id, "text": body[i : i + MAX_TEXT]})


def get_updates(token: str, offset: int | None = None, *, poll: int = 25) -> list[dict[str, Any]]:
    """Long-poll for new updates.

    `poll` is Telegram's server-side wait: the request parks until a message
    arrives or the timeout lapses, so idling costs one open connection rather
    than a busy loop. The HTTP timeout is deliberately longer than the poll so a
    normal empty poll is never mistaken for a network failure.
    """
    params: dict[str, Any] = {"timeout": poll, "allowed_updates": ["message"]}
    if offset is not None:
        params["offset"] = offset
    return _call(token, "getUpdates", params, timeout=poll + 15.0) or []


def parse_message(update: dict[str, Any]) -> dict[str, Any] | None:
    """Flatten an update into {update_id, chat_id, user_id, name, text}, or None
    for anything that isn't a plain text message (joins, photos, edits…)."""
    msg = update.get("message") or {}
    text = msg.get("text")
    chat = msg.get("chat") or {}
    frm = msg.get("from") or {}
    if not text or not chat.get("id"):
        return None
    name = (frm.get("username") or " ".join(
        p for p in (frm.get("first_name"), frm.get("last_name")) if p) or "unknown")
    return {
        "update_id": update.get("update_id"),
        "chat_id": chat["id"],
        "user_id": frm.get("id"),
        "name": name,
        "text": text,
    }
