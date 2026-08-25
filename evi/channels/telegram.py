"""Telegram channel service — talk to eVi from your phone.

Runs a long-polling loop on a daemon thread, hands each approved message to a
one-shot agent turn, and sends the reply back.

Security posture, because this is an inbound channel into a tool-capable agent
and therefore the most dangerous surface eVi has:

  1. OFF unless `[channels] telegram_enabled` is set. No token, no thread.
  2. An unknown sender gets a pairing code and **no agent turn runs at all** —
     not even a read-only one. Approval requires local access to the machine
     (`evi channel approve <code>`), which is what makes this safe: finding the
     bot is not the same as being allowed to use it.
  3. Approved senders run with an explicitly chosen toolset, not the desktop
     user's. `build_agent(tool_categories=...)` bypasses config toggles, so the
     default (memory + skills) holds even if the local user has enabled shell.
  4. Permission prompts are auto-DENIED rather than awaited — nobody is at the
     desk to answer, and a hung turn would be a stuck bot.

Nothing here is a substitute for the user choosing who to pair with.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

from evi.channels import pairing, telegram_api
from evi.config import Config

logger = logging.getLogger(__name__)

CHANNEL = "telegram"

# Categories an approved sender gets unless the config says otherwise. Chosen to
# be useful for chat while touching nothing outside eVi: no shell, no filesystem
# writes, no computer control.
DEFAULT_TOOLS = ("memory", "skills")

_PAIR_HELP = (
    "eVi doesn't know you yet.\n\n"
    "Your pairing code is: {code}\n\n"
    "Ask the person running eVi to approve it on that machine:\n"
    "    evi channel approve {code}\n\n"
    "Until then nothing you send is processed."
)


def _reply_for(text: str, msg: dict[str, Any], cfg: Config) -> str:
    """Run one agent turn for an approved sender and return the reply text."""
    from evi.sdk import build_agent, run_headless

    tools = list(cfg.channels.telegram_tools or DEFAULT_TOOLS)
    agent = build_agent(
        config=cfg,
        tool_categories=tools,      # explicit; ignores the local user's toggles
        enable_project=False,       # a remote sender gets no repo context
        enable_hooks=False,         # ...and triggers no local side effects
        session_id=f"telegram-{msg.get('user_id')}",
    )
    # Never block waiting for a human: deny anything that would prompt.
    agent.permission_callback = lambda *a, **k: False
    agent.permission_batch_callback = None
    res = run_headless(agent, text, max_turns=max(1, int(cfg.channels.telegram_max_turns or 6)))
    if getattr(res, "error", ""):
        return f"⚠ {res.error}"
    return (getattr(res, "text", "") or "").strip() or "(no reply)"


def handle_message(msg: dict[str, Any], cfg: Config | None = None) -> str:
    """Decide what an inbound message deserves, and return what was sent back.

    Split out from the loop so the pairing gate is directly testable without a
    network, a token, or a model.
    """
    cfg = cfg or Config.load()
    uid = msg.get("user_id")
    if uid is None:
        return ""
    if not pairing.is_paired(CHANNEL, uid):
        code = pairing.request_pairing(CHANNEL, uid, msg.get("name", ""))
        logger.info("telegram: pairing requested by %s (%s) -> %s", msg.get("name"), uid, code)
        return _PAIR_HELP.format(code=code)
    return _reply_for(msg.get("text", ""), msg, cfg)


class TelegramChannel:
    """Long-polling supervisor. `start()` is non-blocking; `stop()` is idempotent."""

    def __init__(self, token: str = "", cfg: Config | None = None) -> None:
        self._cfg = cfg or Config.load()
        self.token = (token or self._cfg.channels.telegram_token or "").strip()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._offset: int | None = None
        self.started = False
        self.last_error = ""

    def start(self) -> None:
        if self.started or not self.token:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="evi-telegram", daemon=True)
        self._thread.start()
        self.started = True

    def stop(self) -> None:
        self._stop.set()
        self.started = False
        t, self._thread = self._thread, None
        if t is not None:
            t.join(timeout=2.0)     # the poll can be parked; don't hold shutdown

    def _loop(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            try:
                updates = telegram_api.get_updates(self.token, self._offset, poll=25)
                backoff = 1.0
                self.last_error = ""
            except telegram_api.TelegramError as exc:
                # A bad token or a revoked bot would otherwise spin here; back
                # off so a misconfigured channel costs ~nothing.
                self.last_error = str(exc)
                logger.warning("telegram: %s", exc)
                self._stop.wait(backoff)
                backoff = min(backoff * 2, 300.0)
                continue
            for upd in updates:
                self._offset = int(upd.get("update_id", 0)) + 1   # ack before work
                if self._stop.is_set():
                    break
                msg = telegram_api.parse_message(upd)
                if not msg:
                    continue
                try:
                    reply = handle_message(msg, Config.load())
                    if reply:
                        telegram_api.send_message(self.token, msg["chat_id"], reply)
                except Exception as exc:  # noqa: BLE001 — one bad message must not kill the loop
                    logger.warning("telegram: handling failed: %s", exc)
                    try:
                        telegram_api.send_message(
                            self.token, msg["chat_id"], f"⚠ {type(exc).__name__}: {exc}"[:400])
                    except Exception:  # noqa: BLE001
                        pass


_ACTIVE: TelegramChannel | None = None


def active() -> TelegramChannel | None:
    return _ACTIVE


def start_if_configured(cfg: Config | None = None) -> TelegramChannel | None:
    """Start the channel when configured. Called from the web server's lifespan;
    returns None (quietly) when the channel is off or has no token."""
    global _ACTIVE
    cfg = cfg or Config.load()
    if not cfg.channels.telegram_enabled or not cfg.channels.telegram_token.strip():
        return None
    ch = TelegramChannel(cfg=cfg)
    ch.start()
    _ACTIVE = ch
    return ch


def stop_active() -> None:
    global _ACTIVE
    if _ACTIVE is not None:
        _ACTIVE.stop()
        _ACTIVE = None


def status(cfg: Config | None = None) -> dict[str, Any]:
    cfg = cfg or Config.load()
    ch = _ACTIVE
    return {
        "enabled": bool(cfg.channels.telegram_enabled),
        "token_set": bool(cfg.channels.telegram_token.strip()),
        "running": bool(ch and ch.started),
        "last_error": (ch.last_error if ch else ""),
        "tools": list(cfg.channels.telegram_tools or DEFAULT_TOOLS),
        "paired": pairing.paired_users(CHANNEL),
        "pending": pairing.pending_requests(CHANNEL),
    }


def wait_for_stop() -> None:
    """Block the caller until interrupted — for `evi channel serve`."""
    try:
        while True:
            time.sleep(1.0)
    except KeyboardInterrupt:
        pass
