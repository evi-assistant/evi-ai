"""The Companion: a proactive, safe-scoped personal watcher (Duet-style).

Opt-in (``[companion] enabled = true`` in your own config.toml; a
repository's .evi.toml can never switch it on or widen it). When on, the
scheduler (`evi scheduler`, and `evi web`'s lifespan) runs it on its own:
a check-in every ``check_every_hours`` and a morning brief at ``brief_at``
(`evi.scheduler.companion_jobs`). It reads only the sources you connect, treats
everything it reads as untrusted, speaks up only when it matters, and changes
nothing unless you approve an exact action. How: `evi.companion.engine`;
its files: `evi.companion.store`; what the model is told:
`evi.companion.prompts`; the policy its check-ins run under:
`evi.policy.watcher_policy`.

Public API (each call loads the config afresh; `now` is for tests and
defaults to this moment, local time):

- ``status() -> dict``: whether it is on, its sources and actions, the last
  check-in, the next one due, the day of the last brief, pending proposals
  and today's notification count.
- ``check_in(now=None) -> CheckIn``: run a check-in now (then the brief if
  it is due); ``CheckIn.skipped`` says why one didn't run ("disabled",
  "busy", "no sources", "unchanged").
- ``brief(now=None, force=False) -> dict | None``: the brief when due (or
  now, with `force`); the feed item, or None.
- ``feed(limit=50, since=None) -> list[dict]``: the newest feed items,
  oldest first. An item: id, at, ts, kind (brief | alert | suggestion |
  working_on | needs_you, or "result" for an approved action's outcome),
  title, body, priority, why_now, dedupe_key, source, notified, held (why
  it wasn't notified), and proposal_id or proposal_refused.
- ``proposals(state=None) -> list[dict]``: proposals (id, tool, args,
  args_hash, summary, state, created_at, expires_at, ...), only those in
  `state` when given: an engine state (`engine.PROPOSAL_STATES`), or
  "approved" (running, executed or failed) or "done" (executed);
  ``PROPOSAL_FILTERS`` lists them all.
- ``approve(id) -> dict`` / ``reject(id) -> dict``: decide a pending
  proposal (KeyError: no such id; ProposalError: it can't be). For the
  owner's own callers only: never wire either to a remote channel.
- ``read_instructions() -> str`` / ``write_instructions(text)``: the
  owner's steering file, instructions.md. Owner-only callers.
- ``set_enabled(on) -> None``: switch the Companion on or off in the USER
  config layer (config.toml) only.
- ``printable(value) -> str``: text with its control and format characters
  escaped, for a terminal (`evi companion`).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from evi.companion.engine import (
    PROPOSAL_STATES,
    CheckIn,
    Companion,
    ProposalError,
    Settings,
    settings,
)
from evi.companion.store import CompanionBusy, CompanionStore

__all__ = [
    "CheckIn", "Companion", "CompanionBusy", "CompanionStore", "PROPOSAL_FILTERS",
    "ProposalError", "Settings", "approve", "brief", "check_in", "feed", "printable", "proposals", "read_instructions",
    "reject", "set_enabled", "settings", "status", "write_instructions",
]

# Unicode categories a terminal must not get raw: controls (escape sequences,
# CR, backspace), format characters (bidi overrides, zero-width), surrogates,
# private-use and unassigned code points, line and paragraph separators.
_UNSHOWN = frozenset({"Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp"})


def printable(value: Any) -> str:
    """`value` as text that shows as it is in a terminal: every control or
    format character (an escape sequence, CR, a bidi override) becomes its
    ``\\x..`` / ``\\u....`` escape, so feed text (the model's and the
    sources') can't move the cursor, repaint the screen or reorder what is
    shown, and an escaped character is still seen. Tab and newline stay."""
    import unicodedata

    out = []
    for ch in str(value):
        if ch in "\n\t" or unicodedata.category(ch) not in _UNSHOWN:
            out.append(ch)
        else:
            code = ord(ch)
            out.append(f"\\x{code:02x}" if code < 0x100 else
                       f"\\u{code:04x}" if code < 0x10000 else f"\\U{code:08x}")
    return "".join(out)


def _engine() -> Companion:
    return Companion()


def status() -> dict[str, Any]:
    return _engine().status()


def check_in(now: datetime | None = None) -> CheckIn:
    return _engine().check_in(now)


def brief(now: datetime | None = None, force: bool = False) -> dict[str, Any] | None:
    return _engine().brief(now, force=force)


def feed(limit: int = 50, since: float | datetime | None = None) -> list[dict[str, Any]]:
    return _engine().feed(limit, since)


# Filter names besides the engine's states: every proposal the owner approved
# (whatever came of it), and the ones that ran.
_STATE_ALIASES = {"approved": ("running", "executed", "failed"), "done": ("executed",)}
PROPOSAL_FILTERS = PROPOSAL_STATES + tuple(_STATE_ALIASES)


def proposals(state: str | None = None) -> list[dict[str, Any]]:
    if state in _STATE_ALIASES:
        return [p for p in _engine().proposals() if p.get("state") in _STATE_ALIASES[state]]
    return _engine().proposals(state)


def approve(proposal_id: str) -> dict[str, Any]:
    return _engine().approve(proposal_id)


def reject(proposal_id: str) -> dict[str, Any]:
    return _engine().reject(proposal_id)


def read_instructions() -> str:
    return CompanionStore().read_instructions()


def write_instructions(text: str) -> None:
    store = CompanionStore()
    store.write_instructions(text)
    store.audit("instructions", chars=len(str(text or "")))


def set_enabled(on: bool) -> None:
    """Write ``[companion] enabled`` to config.toml (the user layer only:
    `Config.save` never copies a profile's or a repository's values)."""
    from evi.config import Config

    cfg = Config.load()
    snapshot = getattr(cfg, "_snapshot", None)
    if isinstance(snapshot, dict) and isinstance(snapshot.get("companion"), dict):
        # Write the value even when a profile already gives it: only the
        # user layer's own value switches the Companion on (engine.user_enabled).
        snapshot["companion"]["enabled"] = not on
    cfg.companion.enabled = bool(on)
    cfg.save()
    CompanionStore().audit("set_enabled", enabled=bool(on))
