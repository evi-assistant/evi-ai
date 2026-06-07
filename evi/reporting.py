"""Opt-in, privacy-first crash/error reporting (Phase 52).

eVi is local-first, so reporting is **OFF by default** and inert until you set
both `[telemetry] crash_reports = true` and a `dsn` (a Sentry-compatible
endpoint — self-hosted GlitchTip or hosted Sentry). The DSN is a write-only
ingest key, safe to ship.

Design: one swappable `Reporter` seam (so a GlitchTip/Sentry DSN — or a future
GitHub-issue backend — is a config change, not a code change) and one shared
**scrubber** applied to every event. The scrubber is deliberately aggressive
because exception messages and stack-frame locals in an AI assistant can carry
prompt text, file paths with usernames, and API keys.

`sentry-sdk` is an optional dep (`pip install 'evi-assistant[telemetry]'`); when it's
absent or reporting is off, you get a no-op `NullReporter`.
"""

from __future__ import annotations

import os
import re
from typing import Any, Protocol

# Patterns for obvious secrets that can end up in tracebacks/messages.
_SECRET_RE = re.compile(
    r"(sk-[A-Za-z0-9_\-]{8,}"          # OpenAI-style keys
    r"|gh[pousr]_[A-Za-z0-9]{20,}"      # GitHub tokens
    r"|xox[baprs]-[A-Za-z0-9\-]{10,}"   # Slack tokens
    r"|Bearer\s+[A-Za-z0-9._\-]+)"      # bearer headers
)

# Keys whose VALUES are dropped wholesale wherever they appear in an event —
# these routinely carry user content / secrets in Sentry payloads.
_DROP_KEYS = frozenset({
    "vars",          # stack-frame locals — may hold prompt text
    "env", "environ",
    "headers", "cookies", "authorization",
    "data", "request", "extra",
    "api_key", "token", "password", "secret", "dsn",
})

_REDACTED = "<redacted>"


def _scrub_str(s: str, home: str, user: str) -> str:
    if home:
        s = s.replace(home, "<HOME>")
    if user:
        s = re.sub(re.escape(user), "<USER>", s, flags=re.IGNORECASE)
    return _SECRET_RE.sub(_REDACTED, s)


def _scrub_obj(o: Any, home: str, user: str) -> Any:
    if isinstance(o, dict):
        return {
            k: (_REDACTED if k in _DROP_KEYS else _scrub_obj(v, home, user))
            for k, v in o.items()
        }
    if isinstance(o, (list, tuple)):
        return [_scrub_obj(v, home, user) for v in o]
    if isinstance(o, str):
        return _scrub_str(o, home, user)
    return o


def scrub_event(event: dict, hint: Any = None, *, home: str | None = None,
                user: str | None = None) -> dict:
    """Scrub a Sentry-shaped event dict in place-ish (returns the cleaned copy).

    Drops local variables, env, request/headers/cookies; rewrites the user's
    home dir → ``<HOME>`` and username → ``<USER>``; redacts API-key / token
    patterns; anonymises ``server_name`` and removes the ``user`` (IP/id) block.
    Usable as a sentry ``before_send`` and unit-testable without sentry-sdk.
    """
    home = home if home is not None else os.path.expanduser("~")
    user = user if user is not None else (os.environ.get("USERNAME") or os.environ.get("USER") or "")
    cleaned = _scrub_obj(event, home, user)
    cleaned["server_name"] = "evi"
    cleaned.pop("user", None)
    return cleaned


def make_scrubber():
    """Return a `before_send(event, hint)` closure bound to this machine's
    home dir + username (computed once)."""
    home = os.path.expanduser("~")
    user = os.environ.get("USERNAME") or os.environ.get("USER") or ""
    return lambda event, hint=None: scrub_event(event, hint, home=home, user=user)


class Reporter(Protocol):
    """Crash-reporter seam. Implementations: NullReporter, SentryReporter, and
    (future) a GitHub-issue reporter — all selected by config, never by code."""

    def capture(self, exc: BaseException, context: dict | None = None) -> None: ...

    @property
    def active(self) -> bool: ...


class NullReporter:
    """No-op reporter — the default. Reporting is disabled or sentry-sdk absent."""

    active = False

    def capture(self, exc: BaseException, context: dict | None = None) -> None:
        return None


class SentryReporter:
    """Sends scrubbed exceptions to a Sentry-compatible DSN (GlitchTip/Sentry).

    All PII handling rides on the shared scrubber + `send_default_pii=False`.
    """

    active = True

    def __init__(self, dsn: str, *, release: str) -> None:
        import sentry_sdk

        self._sdk = sentry_sdk
        self._home = os.path.expanduser("~")
        self._user = os.environ.get("USERNAME") or os.environ.get("USER") or ""
        sentry_sdk.init(
            dsn=dsn,
            release=release,
            send_default_pii=False,
            server_name="evi",
            before_send=make_scrubber(),
        )

    def capture(self, exc: BaseException, context: dict | None = None) -> None:
        if context:
            safe = _scrub_obj(context, self._home, self._user)
            self._sdk.set_context("evi", safe)
        self._sdk.capture_exception(exc)


def init_reporting(cfg: Any = None) -> Reporter:
    """Build the configured reporter. Returns a `NullReporter` unless reporting
    is opted-in (config or `EVI_CRASH_REPORTS`), a DSN is present (config or
    `EVI_TELEMETRY_DSN`), the backend isn't `none`, and sentry-sdk imports."""
    if cfg is None:
        from evi.config import Config

        cfg = Config.load()

    enabled = cfg.telemetry.crash_reports
    env_flag = os.environ.get("EVI_CRASH_REPORTS")
    if env_flag is not None:
        enabled = env_flag.strip().lower() not in ("", "0", "false", "no")

    dsn = (os.environ.get("EVI_TELEMETRY_DSN") or cfg.telemetry.dsn or "").strip()

    if not enabled or cfg.telemetry.backend == "none" or not dsn:
        return NullReporter()

    try:
        from evi import __version__

        return SentryReporter(dsn, release=f"evi@{__version__}")
    except Exception:  # noqa: BLE001 — sentry-sdk missing or init failed → degrade
        return NullReporter()


def install_excepthook(reporter: Reporter) -> None:
    """Chain `reporter.capture` into `sys.excepthook` (CLI uncaught errors),
    then defer to the original hook so the user still sees the traceback."""
    import sys

    if not getattr(reporter, "active", False):
        return
    original = sys.excepthook

    def _hook(exc_type, exc, tb):
        try:
            reporter.capture(exc, {"source": "excepthook"})
        except Exception:  # noqa: BLE001 — reporting must never mask the real error
            pass
        original(exc_type, exc, tb)

    sys.excepthook = _hook
