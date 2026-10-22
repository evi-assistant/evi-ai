"""The Companion's files: <eVi home>/companion/.

- ``feed.jsonl``: what the Companion found, one item per line, the last
  `FEED_MAX` kept (oldest dropped first).
- ``proposals.json``: proposed actions by id, with their state (pending,
  running, executed, failed, rejected, expired, refused).
- ``state.json``: the last check-in, the sources' fingerprint, the dedupe
  keys seen, today's notification count, the day the brief went out and
  the items held back for quiet hours or the daily limit.
- ``instructions.md``: the owner's steering (what to watch, what to ignore,
  how often to speak up). The Companion reads it; only the owner's own
  callers write it (no tool can).
- ``audit.jsonl``: append-only, redacted (`evi.redact`): every check-in,
  notification, proposal, approval, rejection, expiry and execution.

Every rewrite is atomic (a temporary file, then a rename). A file that can't
be read fails closed and never crashes a caller: a corrupt state.json or
proposals.json is moved aside (``<name>.corrupt-<time>``) and logged, so no
proposal from it can be approved and the check-in that found it notifies
nothing (`load_state` marks the fresh state ``quiet``); a feed line that
doesn't parse is skipped.

Two locks keep processes apart (`evi scheduler` and `evi web` may both run
the Companion): `lock` serialises read-modify-write of these files (a short
wait), `run_lock` lets one check-in or brief run at a time (no wait: a
second one skips). Each is a lock file created exclusively, taken over when
older than its stale age, and re-entrant within a thread.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import secrets
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

FEED_MAX = 500
FEED_FILE = "feed.jsonl"
PROPOSALS_FILE = "proposals.json"
STATE_FILE = "state.json"
INSTRUCTIONS_FILE = "instructions.md"
AUDIT_FILE = "audit.jsonl"
# How long the store lock is waited for, and when a lock file left by a
# process that died is taken over.
LOCK_WAIT_S = 10.0
LOCK_STALE_S = 120.0
RUN_LOCK_STALE_S = 2 * 3600.0
# The most of instructions.md a reader gets.
INSTRUCTIONS_MAX = 16_000


# What `CompanionStore._read_json` returns for a file that can't be read.
_CORRUPT: Any = object()


class CompanionBusy(TimeoutError):
    """The store lock could not be taken in time."""


def default_root() -> Path:
    """<eVi home>/companion, read now (tests move eVi's home)."""
    from evi import config as _config

    return Path(_config.HOME) / "companion"


class _FileLock:
    """An exclusive lock file, re-entrant within the thread that holds it."""

    def __init__(self, path: Path, stale_after: float) -> None:
        self.path = path
        self.stale_after = stale_after
        self._rlock = threading.RLock()
        self._depth = 0

    def acquire(self, wait: float) -> bool:
        ok = (self._rlock.acquire(timeout=wait) if wait > 0
              else self._rlock.acquire(blocking=False))
        if not ok:
            return False
        if self._depth:
            self._depth += 1
            return True
        deadline = time.monotonic() + max(wait, 0.0)
        while True:
            if self._create():
                self._depth = 1
                return True
            if self._stale():
                with contextlib.suppress(OSError):
                    self.path.unlink()
                continue
            if time.monotonic() >= deadline:
                self._rlock.release()
                return False
            time.sleep(0.05)

    def release(self) -> None:
        self._depth -= 1
        if self._depth == 0:
            with contextlib.suppress(OSError):
                self.path.unlink()
        self._rlock.release()

    def _create(self) -> bool:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            return False
        except OSError:
            return False
        try:
            os.write(fd, f"{os.getpid()} {time.time():.0f}\n".encode())
        finally:
            os.close(fd)
        return True

    def _stale(self) -> bool:
        try:
            return time.time() - self.path.stat().st_mtime > self.stale_after
        except OSError:
            return False


# One pair of locks per folder in this process, so every store object on the
# same folder shares them.
_LOCKS: dict[str, tuple[_FileLock, _FileLock]] = {}
_LOCKS_GUARD = threading.Lock()


def _locks_for(root: Path) -> tuple[_FileLock, _FileLock]:
    key = os.path.normcase(str(root))
    with _LOCKS_GUARD:
        pair = _LOCKS.get(key)
        if pair is None:
            pair = (_FileLock(root / "store.lock", LOCK_STALE_S),
                    _FileLock(root / "run.lock", RUN_LOCK_STALE_S))
            _LOCKS[key] = pair
        return pair


def atomic_write(path: Path, text: str) -> None:
    """Write `text` to `path` through a temporary file and a rename."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
    tmp.write_text(text, encoding="utf-8", newline="\n")
    for attempt in range(5):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:  # Windows: a reader has the file open
            if attempt == 4:
                with contextlib.suppress(OSError):
                    tmp.unlink()
                raise
            time.sleep(0.05 * (attempt + 1))


def new_state() -> dict[str, Any]:
    """state.json as it starts."""
    return {"last_check": None, "fingerprint": None, "seen": {},
            "notified": {"day": None, "count": 0}, "brief_day": None, "held": []}


class CompanionStore:
    """The Companion's folder (`default_root` unless `root` is given)."""

    def __init__(self, root: str | os.PathLike[str] | None = None) -> None:
        self.root = Path(root) if root is not None else default_root()
        self._lock, self._run_lock = _locks_for(self.root)
        # Set once this object found state.json unreadable: its caller then
        # notifies nothing (what was already sent can't be told).
        self.state_corrupt = False

    # -- paths ----------------------------------------------------------------

    @property
    def feed_path(self) -> Path:
        return self.root / FEED_FILE

    @property
    def proposals_path(self) -> Path:
        return self.root / PROPOSALS_FILE

    @property
    def state_path(self) -> Path:
        return self.root / STATE_FILE

    @property
    def instructions_path(self) -> Path:
        return self.root / INSTRUCTIONS_FILE

    @property
    def audit_path(self) -> Path:
        return self.root / AUDIT_FILE

    # -- locks ----------------------------------------------------------------

    @contextlib.contextmanager
    def lock(self, wait: float = LOCK_WAIT_S) -> Iterator[None]:
        """Hold the store lock; CompanionBusy when it can't be had in `wait`
        seconds."""
        if not self._lock.acquire(wait):
            raise CompanionBusy("the Companion's store is busy")
        try:
            yield
        finally:
            self._lock.release()

    @contextlib.contextmanager
    def run_lock(self) -> Iterator[bool]:
        """Hold the run lock if it is free: yields whether this caller got
        it (a check-in already running elsewhere: False, skip)."""
        got = self._run_lock.acquire(0.0)
        try:
            yield got
        finally:
            if got:
                self._run_lock.release()

    # -- state ------------------------------------------------------------------

    def load_state(self) -> dict[str, Any]:
        """state.json; a fresh state when there is none. One that can't be
        read is moved aside, and the fresh state is marked ``quiet`` and
        `state_corrupt` set (the caller notifies nothing this time: what was
        already sent can't be told)."""
        data = self._read_json(self.state_path)
        if data is None:
            return new_state()
        if data is _CORRUPT or not isinstance(data, dict):
            if data is not _CORRUPT:
                self._corrupt(self.state_path, "not an object")
            self.state_corrupt = True
            return {**new_state(), "quiet": True}
        state = {**new_state(), **data}
        if not isinstance(state.get("seen"), dict):
            state["seen"] = {}
        if not isinstance(state.get("notified"), dict):
            state["notified"] = {"day": None, "count": 0}
        if not isinstance(state.get("held"), list):
            state["held"] = []
        state.pop("quiet", None)
        return state

    def save_state(self, state: dict[str, Any]) -> None:
        body = {k: v for k, v in state.items() if k != "quiet"}
        atomic_write(self.state_path, json.dumps(body, indent=2, sort_keys=True))

    # -- proposals --------------------------------------------------------------

    def load_proposals(self) -> dict[str, dict[str, Any]]:
        """proposals.json by id; {} when there is none or it can't be read
        (then it is moved aside: none of its proposals can be approved)."""
        data = self._read_json(self.proposals_path)
        if data is None or data is _CORRUPT:
            return {}
        if not isinstance(data, dict) or not all(isinstance(v, dict) for v in data.values()):
            self._corrupt(self.proposals_path, "not an object of proposals")
            return {}
        return data

    def save_proposals(self, proposals: dict[str, dict[str, Any]]) -> None:
        atomic_write(self.proposals_path, json.dumps(proposals, indent=2, sort_keys=True))

    # -- feed ---------------------------------------------------------------------

    def read_feed(self) -> list[dict[str, Any]]:
        """Every feed item, oldest first (a line that doesn't parse is
        skipped and logged)."""
        try:
            text = self.feed_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return []
        except OSError as exc:
            logger.warning("companion: the feed can't be read: %s", exc)
            return []
        out: list[dict[str, Any]] = []
        bad = 0
        for line in text.splitlines():
            if not line.strip():
                continue
            try:
                item = json.loads(line)
            except (json.JSONDecodeError, RecursionError):
                bad += 1
                continue
            if isinstance(item, dict):
                out.append(item)
            else:
                bad += 1
        if bad:
            logger.warning("companion: skipped %d unreadable feed line(s)", bad)
        return out

    def append_feed(self, items: list[dict[str, Any]]) -> None:
        """Add `items` to the feed, keeping the last `FEED_MAX`."""
        if not items:
            return
        feed = self.read_feed() + list(items)
        feed = feed[-FEED_MAX:]
        atomic_write(self.feed_path,
                     "".join(json.dumps(i, sort_keys=True) + "\n" for i in feed))

    # -- instructions -------------------------------------------------------------

    def read_instructions(self) -> str:
        try:
            return self.instructions_path.read_text(encoding="utf-8")[:INSTRUCTIONS_MAX]
        except FileNotFoundError:
            return ""
        except (OSError, UnicodeDecodeError) as exc:
            logger.warning("companion: instructions.md can't be read: %s", exc)
            return ""

    def write_instructions(self, text: str) -> None:
        atomic_write(self.instructions_path, str(text or ""))

    # -- audit --------------------------------------------------------------------

    def audit(self, event: str, **fields: Any) -> None:
        """Append one redacted line to audit.jsonl. Never raises."""
        try:
            from evi.redact import redact_obj

            record = redact_obj({"ts": round(time.time(), 3), "event": event, **fields})
            self.root.mkdir(parents=True, exist_ok=True)
            with self.audit_path.open("a", encoding="utf-8", newline="\n") as fh:
                fh.write(json.dumps(record, sort_keys=True, default=str) + "\n")
        except Exception as exc:  # noqa: BLE001 — an audit line must never break a run
            logger.warning("companion: audit write failed: %s", exc)

    def read_audit(self) -> list[dict[str, Any]]:
        """audit.jsonl's records, oldest first (unreadable lines skipped)."""
        try:
            lines = self.audit_path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        out = []
        for line in lines:
            with contextlib.suppress(json.JSONDecodeError, RecursionError):
                rec = json.loads(line)
                if isinstance(rec, dict):
                    out.append(rec)
        return out

    # -- internals ----------------------------------------------------------------

    def _read_json(self, path: Path) -> Any:
        """The JSON in `path`; None when it doesn't exist, `_CORRUPT` when
        it can't be read (then it is moved aside and logged)."""
        try:
            text = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except (OSError, UnicodeDecodeError) as exc:
            self._corrupt(path, f"unreadable ({type(exc).__name__})")
            return _CORRUPT
        try:
            return json.loads(text)
        except (json.JSONDecodeError, RecursionError):
            self._corrupt(path, "not JSON")
            return _CORRUPT

    def _corrupt(self, path: Path, why: str) -> None:
        aside = path.with_name(f"{path.name}.corrupt-{time.strftime('%Y%m%d-%H%M%S')}")
        try:
            os.replace(path, aside)
        except OSError:
            aside = path
        logger.warning("companion: %s is %s; moved aside to %s", path.name, why, aside.name)
        self.audit("store_corrupt", file=path.name, why=why, moved_to=aside.name)
