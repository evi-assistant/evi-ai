"""Telegram channel service — talk to eVi from your phone.

Runs a long-polling loop on a daemon thread, hands each approved message to a
one-shot agent turn, and sends the reply back.

Security posture, because this is an inbound channel into a tool-capable agent
and therefore the most dangerous surface eVi has:

  1. OFF unless `[channels] telegram_enabled` is set. No token, no thread.
  2. Direct chats only by default. A group message gets no reply (and an
     unknown sender in a group no pairing code) unless `allow_groups` is set,
     and a sender that doesn't identify one person (an anonymous group admin,
     a message sent on behalf of a channel) is never answered.
  3. An unknown sender gets a pairing code and **no agent turn runs at all** —
     not even a read-only one. Approval requires local access to the machine
     (`evi channel approve <code>`), which is what makes this safe: finding the
     bot is not the same as being allowed to use it.
  4. Approved senders run under the channel's own policy (evi/channels/
     policy.py), not the desktop user's: the `telegram_tools` categories only,
     mode "ask", and only `telegram_auto_approve` (empty by default) plus the
     channel-safe memory and skill reads run. `[auto]` (its auto-approve list,
     yolo, allow rules, trusted dirs) never applies.
  5. Memory is recall-only by default (`memory_mode`); in "staged" mode a
     `remember` waits in the channel inbox (evi/channels/inbox.py) as untrusted
     until the owner applies it.
  6. Permission prompts are auto-DENIED rather than awaited — nobody is at the
     desk to answer, and a hung turn would be a stuck bot. A delegate CLI-agent
     backend (which runs its own tools) is always refused: `[llm]
     allow_cli_agent_unattended` is for the owner's own unattended runs and
     never covers the bot.
  7. The policy of every turn is appended to ~/.evi/logs/channels/telegram.log.

Nothing here is a substitute for the user choosing who to pair with.
"""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Any

from evi.channels import pairing, policy, telegram_api
from evi.config import HOME, Config, ConfigUnreadable

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

# The next update offset per bot token, shared by every poller in this process.
# A restart (stop, then start: /api/channels/telegram/restart) can't wait out a
# thread that is mid-turn, and Telegram confirms updates only on a later
# getUpdates, so a new poller starting from no offset would fetch the batch the
# old one is still handling and run the same message twice.
_OFFSET_LOCK = threading.Lock()
_OFFSETS: dict[str, int] = {}

# The turn log rolls over to telegram.log.1 past this size.
_TURN_LOG_MAX_BYTES = 1_000_000


def channel_policy(cfg: Config) -> policy.ChannelPolicy:
    """The Telegram channel's policy from `[channels]` (never `[auto]`)."""
    ch = cfg.channels
    return policy.make_policy(
        CHANNEL,
        # [] means no tools at all; only a missing value falls back.
        tools=DEFAULT_TOOLS if ch.telegram_tools is None else ch.telegram_tools,
        auto_approve=ch.telegram_auto_approve,
        memory_mode=ch.memory_mode,
        allow_groups=ch.allow_groups,
    )


def turn_log_path() -> Path:
    return HOME / "logs" / "channels" / f"{CHANNEL}.log"


def _log_turn(msg: dict[str, Any], pol: policy.ChannelPolicy) -> None:
    """Record the policy a turn runs under, in the process log and in the
    channel's own turn log (the web app and `evi channel serve` configure no
    logging for eVi's loggers, so the process log alone is often nowhere)."""
    line = (f"turn for user {msg.get('user_id')} in chat {msg.get('chat_id')}: "
            f"policy {pol.describe()}")
    logger.info("telegram: %s", line)
    path = turn_log_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.is_file() and path.stat().st_size > _TURN_LOG_MAX_BYTES:
            path.replace(path.with_name(path.name + ".1"))
        with path.open("a", encoding="utf-8") as fh:
            fh.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S%z')} {line}\n")
    except OSError as exc:
        logger.warning("telegram: could not write %s: %s", path, exc)


def _deny_what_would_ask(agent: Any) -> None:
    """Nobody is at the desk: a call that would ask is denied, not awaited.

    An agent with an unattended policy runs with no permission UI under
    "deny", so the run reports policy "deny" and a denial says nobody could
    approve it. Anything else gets a callback that refuses every prompt."""
    from evi.llm import agent as agent_mod

    make_unattended = getattr(agent_mod, "make_unattended", None)
    if make_unattended is not None and callable(
        getattr(agent, "effective_unattended_policy", None)
    ):
        make_unattended(agent, "deny")
        return
    agent.permission_callback = lambda *a, **k: False
    agent.permission_batch_callback = None


def _reply_for(text: str, msg: dict[str, Any], cfg: Config) -> str:
    """Run one agent turn for an approved sender and return the reply text."""
    from evi.channels.inbox import StagedMemoryStore
    from evi.llm import cli_agent
    from evi.memory import MemoryStore
    from evi.sdk import build_agent, run_headless

    pol = channel_policy(cfg)
    _log_turn(msg, pol)
    chan_cfg = policy.channel_config(cfg, pol)
    try:
        # A delegate CLI agent runs its own tools, outside this policy: refused
        # here and, inside the block, by every client built or called in it.
        # chan_cfg has [llm] allow_cli_agent_unattended off, so the owner's
        # opt-in for their own unattended runs never reaches the bot.
        with cli_agent.unattended(CHANNEL, chan_cfg):
            agent = build_agent(
                config=chan_cfg,
                tools=policy.channel_tools(pol),     # explicit; ignores the local toggles
                enable_memory="memory" in pol.tools,
                enable_skills="skills" in pol.tools,
                enable_project=False,       # a remote sender gets no repo context
                enable_hooks=True,          # ...but the user's hooks guard its turns (off in safe mode)
                session_id=f"telegram-{msg.get('user_id')}",
            )
            # Never block waiting for a human: deny anything that would prompt.
            _deny_what_would_ask(agent)
            agent.auto_approve_categories = set(pol.auto_approve)
            agent.auto_all = False
            if "memory" in pol.tools and pol.memory_mode != "full":
                mem = getattr(agent, "memory", None)
                root = mem.root if isinstance(mem, MemoryStore) else None
                agent.memory = (
                    StagedMemoryStore(
                        root, source=f"channel:{CHANNEL}:{msg.get('chat_id')}",
                        sender=f"{msg.get('name', '')} ({msg.get('user_id')})")
                    if pol.memory_mode == "staged"
                    else MemoryStore(root, read_only=True)
                )
            res = run_headless(agent, text, max_turns=max(1, int(cfg.channels.telegram_max_turns or 6)))
    except cli_agent.CliAgentUnattended as exc:
        logger.warning("telegram: refused a delegate CLI-agent backend "
                       "([llm] allow_cli_agent_unattended doesn't cover channels): %s", exc)
        return (
            "⚠ eVi's model backend here is a delegate CLI agent ("
            + ", ".join(sorted(cli_agent.DELEGATE_BACKENDS))
            + "), which runs its own tools outside the Telegram channel's policy, so "
            "the channel never uses one. Point [llm] backend (and any [models] "
            "*_backend) at a local or API backend."
        )
    if getattr(res, "error", ""):
        return f"⚠ {res.error}"
    return (getattr(res, "text", "") or "").strip() or "(no reply)"


def handle_message(msg: dict[str, Any], cfg: Config | None = None) -> str:
    """Decide what an inbound message deserves, and return what was sent back
    ("" for nothing).

    Split out from the loop so the pairing gate is directly testable without a
    network, a token, or a model. The gates that need only the message run
    before the config is loaded, so a config that fails to load never makes
    the bot speak in a group or to a shared sender.
    """
    uid = msg.get("user_id")
    if uid is None:
        return ""
    if msg.get("anonymous"):
        logger.info("telegram: ignored a message from a shared sender id %s in chat %s",
                    uid, msg.get("chat_id"))
        return ""
    # Fail closed: only Telegram's own "private" marks a direct chat.
    private = msg.get("chat_type") == "private"
    if not pairing.is_paired(CHANNEL, uid):
        if not private:
            # A pairing code posted in a group is readable by everyone there.
            return ""
        code = pairing.request_pairing(CHANNEL, uid, msg.get("name", ""))
        logger.info("telegram: pairing requested by %s (%s) -> %s", msg.get("name"), uid, code)
        return _PAIR_HELP.format(code=code)
    cfg = cfg or Config.load()
    if cfg.load_error:
        # config.toml can't be read, so Config.load fell back to the defaults.
        # A remote surface doesn't run on settings the owner didn't choose:
        # nothing is answered, and (the loop's error path) only a paired
        # sender's direct chat hears why.
        raise ConfigUnreadable(
            f"eVi's config.toml can't be read ({cfg.load_error}); fix it and try again")
    # The policy's value, so the gate, status() and the turn log agree: only a
    # real `true` opens groups (a hand-written "false" is a non-empty string).
    if not private and not channel_policy(cfg).allow_groups:
        logger.info("telegram: ignored %s (%s) in a %s chat (allow_groups is off)",
                    msg.get("name"), uid, msg.get("chat_type") or "non-private")
        return ""
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
            with _OFFSET_LOCK:
                shared = _OFFSETS.get(self.token)
                if shared is not None and (self._offset is None or shared > self._offset):
                    self._offset = shared
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
                with _OFFSET_LOCK:
                    if self._stop.is_set():
                        break
                    # Ack before work, and share it: a poller started after this
                    # one, or running beside it, never takes this update again.
                    nxt = int(upd.get("update_id", 0)) + 1
                    shared = _OFFSETS.get(self.token, 0)
                    self._offset = max(nxt, shared)
                    if nxt <= shared:
                        continue        # another poller already took it
                    _OFFSETS[self.token] = nxt
                msg = telegram_api.parse_message(upd)
                if not msg:
                    continue
                try:
                    reply = handle_message(msg)
                    if reply:
                        telegram_api.send_message(self.token, msg["chat_id"], reply)
                except Exception as exc:  # noqa: BLE001 — one bad message must not kill the loop
                    logger.warning("telegram: handling failed: %s", exc)
                    if not _may_see_errors(msg):
                        continue
                    try:
                        telegram_api.send_message(
                            self.token, msg["chat_id"], f"⚠ {type(exc).__name__}: {exc}"[:400])
                    except Exception:  # noqa: BLE001
                        pass


def _may_see_errors(msg: dict[str, Any]) -> bool:
    """Error text goes back only to a paired sender in a direct chat. In a
    group, or to a stranger or a shared sender, it would hand eVi's internals
    (paths, config errors) to whoever is there."""
    try:
        return (
            msg.get("chat_type") == "private"
            and not msg.get("anonymous")
            and pairing.is_paired(CHANNEL, msg.get("user_id"))
        )
    except Exception:  # noqa: BLE001 — unsure means no
        return False


_ACTIVE: TelegramChannel | None = None
# Held while _ACTIVE is replaced: two restarts at once must not leave a poller
# running that stop_active() can no longer reach.
_ACTIVE_LOCK = threading.Lock()


def active() -> TelegramChannel | None:
    return _ACTIVE


def start_if_configured(cfg: Config | None = None) -> TelegramChannel | None:
    """Start the channel when configured. Called from the web server's lifespan;
    returns None (quietly) when the channel is off or has no token."""
    global _ACTIVE
    cfg = cfg or Config.load()
    if not cfg.channels.telegram_enabled or not cfg.channels.telegram_token.strip():
        return None
    with _ACTIVE_LOCK:
        if _ACTIVE is not None:
            _ACTIVE.stop()      # one poller per process, never an unreachable second
        ch = TelegramChannel(cfg=cfg)
        ch.start()
        _ACTIVE = ch
        return ch


def stop_active() -> None:
    global _ACTIVE
    with _ACTIVE_LOCK:
        if _ACTIVE is not None:
            _ACTIVE.stop()
            _ACTIVE = None


def status(cfg: Config | None = None) -> dict[str, Any]:
    cfg = cfg or Config.load()
    ch = _ACTIVE
    pol = channel_policy(cfg)
    return {
        "enabled": bool(cfg.channels.telegram_enabled),
        "token_set": bool(cfg.channels.telegram_token.strip()),
        "running": bool(ch and ch.started),
        "last_error": (ch.last_error if ch else ""),
        "tools": list(pol.tools),
        "policy": pol.as_dict(),
        "paired": pairing.paired_users(CHANNEL),
        "pending": pairing.pending_requests(CHANNEL),
    }


def owner_chat(cfg: Config | None = None, chat: str = "") -> str | None:
    """The chat an outbound note to the owner goes to: `chat` when it is a
    paired user (a private chat's id is its user's id), else the one paired
    user when there is exactly one; None when the channel is off, has no
    token, or no single owner can be told."""
    cfg = cfg or Config.load()
    if not cfg.channels.telegram_enabled or not cfg.channels.telegram_token.strip():
        return None
    paired = [str(u.get("user_id")) for u in pairing.paired_users(CHANNEL)]
    want = str(chat or "").strip()
    if want:
        return want if want in paired else None
    return paired[0] if len(paired) == 1 else None


def send_to_owner(text: str, cfg: Config | None = None, chat: str = "") -> bool:
    """Send `text` (plain text, outbound only: no reply is read) to the
    owner's chat (`owner_chat`). Returns whether it was sent; never raises."""
    try:
        cfg = cfg or Config.load()
        target = owner_chat(cfg, chat)
        if target is None:
            return False
        telegram_api.send_message(cfg.channels.telegram_token.strip(), target, text)
        return True
    except Exception as exc:  # noqa: BLE001 — a note that can't go out is not fatal
        logger.warning("telegram: outbound note failed: %s", exc)
        return False


def wait_for_stop() -> None:
    """Block the caller until interrupted — for `evi channel serve`."""
    try:
        while True:
            time.sleep(1.0)
    except KeyboardInterrupt:
        pass
