"""Session browsing — list, show, and resume past chats.

Transcripts persist as one JSONL file per session under
`~/.evi/transcripts/<YYYY-MM-DD>/<session>.jsonl`. This module reads them
back into structured objects so the CLI (and eventually the web UI) can:

- enumerate sessions across days
- preview a session's contents
- hydrate an `Agent.history` from one and continue the conversation

Resuming is a best-effort reconstruction. We re-emit:

- the user/assistant/tool messages in order
- the assistant's `tool_calls` payload, with each tool result paired to the
  call it answers (by its logged `tool_call_id`, else by position), so strict
  OpenAI-compatible backends accept the resumed history
- on request (`replay=True`, what a resume wants), the meta records: a
  compaction, an edit, a cut back (truncate, a re-roll's rewind, /reset) and
  the copied start of a fork, so the history is the one the user and the
  model last saw. A session that ran past midnight has a file in each day's
  directory, and a replay reads them all, in the order their lines were
  written.

Things we DON'T try to perfectly restore:

- the original Agent's transient flags (goal, plan_mode_once) — the
  caller can re-set them if needed
- the backend's KV cache (it's a fresh prompt to the LLM)
"""

from __future__ import annotations

import heapq
import json
import math
import os
import re
import sys
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterator, TypeVar

from evi.config import HOME, TRANSCRIPTS_DIR
from evi.history import compaction_messages, pair_tool_results
from evi.transcripts import COMPACTION, EDIT, FORK, MESSAGE, TRUNCATE

# Where `evi sessions handoff` puts the copy of a session that `evi sync`
# carries to your other devices (stage_handoff / adopt_handoff).
HANDOFF_DIR = HOME / "handoff"
# adopt_handoff compares, then appends: two at once would both append the same
# lines. This lock covers the threads of one process (the web's first two
# requests for a session); a lock file next to the transcripts covers two
# processes (`evi sessions resume` while the desktop app revives the session).
_ADOPT_LOCK = threading.Lock()
_ADOPT_LOCK_FILE = ".handoff.lock"

_DAY = re.compile(r"\d{4}-\d{2}-\d{2}")

_T = TypeVar("_T")


@dataclass(frozen=True)
class SessionInfo:
    """Lightweight summary of a stored session."""

    session_id: str
    day: str                  # YYYY-MM-DD
    path: Path
    message_count: int
    first_user_message: str   # truncated to 80 chars
    started_at: float | None  # earliest timestamp in the file
    ended_at: float | None    # latest timestamp

    @property
    def duration_seconds(self) -> float | None:
        if self.started_at and self.ended_at:
            return self.ended_at - self.started_at
        return None


def _read_entries(path: Path) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                # A line that isn't an object isn't an entry (a copy handed
                # off from another device holds whatever it was given).
                if isinstance(entry, dict):
                    entries.append(entry)
    except OSError:
        pass
    return entries


def _summarize(path: Path, day: str) -> SessionInfo | None:
    entries = _read_entries(path)
    if not entries:
        return None
    first_user = ""
    # A fork's first prompt is in the copy it started from (its fork_of record).
    copied = [
        m for e in entries if e.get("type") == FORK and isinstance(e.get("messages"), list)
        for m in e["messages"] if isinstance(m, dict)
    ]
    for e in [*copied, *entries]:
        if e.get("role") == "user" and (e.get("type") or MESSAGE) == MESSAGE:
            content = str(e.get("content", "")).strip()
            # Strip the "[ongoing goal: …]" prefix injected by Agent.chat
            # so the summary reflects what the human actually typed.
            if content.startswith("[ongoing goal:"):
                marker = "]\n\n"
                idx = content.find(marker)
                if idx > 0:
                    content = content[idx + len(marker):].strip()
            first_user = content[:80]
            break

    timestamps = [
        float(e["ts"]) for e in entries
        if isinstance(e.get("ts"), (int, float)) and math.isfinite(e["ts"])
    ]
    return SessionInfo(
        session_id=path.stem,
        day=day,
        path=path,
        message_count=sum(1 for e in entries if (e.get("type") or MESSAGE) == MESSAGE),
        first_user_message=first_user or "(no user message)",
        started_at=min(timestamps) if timestamps else None,
        ended_at=max(timestamps) if timestamps else None,
    )


def list_sessions(
    *,
    root: Path | None = None,
    days: int | None = None,
    limit: int = 50,
) -> list[SessionInfo]:
    """Enumerate sessions, newest first.

    A file that holds no message, only meta records (a web branch or
    `evi sessions fork` nobody has written in yet: its fork_of record), isn't
    listed, so it changes no count (`evi stats`, fine-tune export) and isn't
    what `evi sessions continue` picks. It is still a session to resume by id.

    Args:
        root: override `~/.evi/transcripts/` (mostly for tests).
        days: cap how many calendar days back we walk. None = all.
        limit: cap on returned entries.
    """
    base = root or TRANSCRIPTS_DIR
    if not base.is_dir():
        return []

    day_dirs = sorted(
        (p for p in base.iterdir() if p.is_dir()),
        reverse=True,
    )
    if days is not None:
        day_dirs = day_dirs[:days]

    out: list[SessionInfo] = []
    for day_dir in day_dirs:
        for f in sorted(day_dir.glob("*.jsonl"), reverse=True):
            info = _summarize(f, day_dir.name)
            if info is not None and info.message_count:
                out.append(info)
                if len(out) >= limit:
                    return out
    return out


@dataclass(frozen=True)
class SessionMatch:
    """A transcript search hit: the session it's in plus a context snippet."""

    session: SessionInfo
    role: str
    snippet: str


def search_sessions(
    query: str,
    *,
    root: Path | None = None,
    days: int | None = None,
    limit: int = 50,
    max_per_session: int = 3,
) -> list[SessionMatch]:
    """Full-text search across stored transcripts (case-insensitive substring).

    Returns matches newest-session first, each with a trimmed snippet centred
    on the hit. Caps hits per session so one chatty session can't crowd out
    the rest. Mirrors Claude Code's transcript search (Ctrl+R / `--resume`)."""
    q = query.strip().lower()
    if not q:
        return []
    out: list[SessionMatch] = []
    for info in list_sessions(root=root, days=days, limit=100_000):
        hits = 0
        for e in _read_entries(info.path):
            content = str(e.get("content", ""))
            idx = content.lower().find(q)
            if idx < 0:
                continue
            start = max(0, idx - 40)
            end = min(len(content), idx + len(q) + 40)
            snippet = ("…" if start else "") + content[start:end].replace("\n", " ").strip() + ("…" if end < len(content) else "")
            out.append(SessionMatch(session=info, role=str(e.get("role", "")), snippet=snippet))
            hits += 1
            if hits >= max_per_session:
                break
        if len(out) >= limit:
            break
    return out[:limit]


def handoff_info(
    session_id: str, *, base_url: str = "", root: Path | None = None
) -> dict[str, Any] | None:
    """Resume affordances for picking a session up on another device (Phase 87).

    Returns ``{session_id, day, messages, resume_cmd, resume_url}`` or None when
    the session isn't persisted on disk (nothing to hand off yet). `day` is the
    day it started and `messages` counts every day's file. Transcripts are
    written per turn, so the on-disk copy is current as of the last completed
    turn. `evi sync` leaves transcripts on the device: stage_handoff puts a
    copy of this one where sync carries it, and the other device takes it in
    when it resumes the session (adopt_handoff).
    """
    files = session_files(session_id, root=root)
    if not files:
        return None
    base = base_url.rstrip("/")
    return {
        "session_id": session_id,
        "day": files[0].parent.name,
        "messages": sum(
            1 for e in _read_session(files[0]) if (e.get("type") or MESSAGE) == MESSAGE
        ),
        "resume_cmd": f"evi sessions resume {session_id}",
        "resume_url": f"{base}/?session={session_id}",
    }


def _raw_lines(path: Path) -> list[str]:
    """The non-blank lines of `path`. Raises OSError when it can't be read and
    ValueError (UnicodeDecodeError) when it isn't UTF-8 text."""
    with path.open("r", encoding="utf-8") as f:
        return [line.strip() for line in f if line.strip()]


def _in_time_order(per_file: list[list[_T]], stamp: Callable[[_T], Any]) -> list[_T]:
    """The lines of a session's day files (oldest day first), merged in the
    order they were written: by timestamp (`stamp`), each file's own order
    kept, a tie going to the earlier day. Each day's file normally holds one
    stretch of the session, but a device whose time zone moved west across
    midnight files later lines under the day before. A line with no timestamp
    goes with the line before it."""
    if len(per_file) == 1:
        return list(per_file[0])

    def stamped(items: list[_T]) -> Iterator[tuple[float, _T]]:
        last = -math.inf
        for item in items:
            ts = stamp(item)
            if isinstance(ts, (int, float)) and math.isfinite(ts):
                last = float(ts)
            yield last, item

    merged = heapq.merge(*(stamped(items) for items in per_file), key=lambda p: p[0])
    return [item for _, item in merged]


def _object(line: str) -> dict[str, Any] | None:
    """The transcript entry a raw line holds: a JSON object, else None."""
    try:
        entry = json.loads(line)
    except ValueError:
        return None
    return entry if isinstance(entry, dict) else None


def _line_ts(line: str) -> Any:
    return (_object(line) or {}).get("ts")


def _session_lines(files: list[Path]) -> list[str]:
    """The raw lines of a session's files in the order they were written
    (_in_time_order). Raises as _raw_lines does."""
    return _in_time_order([_raw_lines(p) for p in files], _line_ts)


def stage_handoff(
    session_id: str, *, root: Path | None = None, dest: Path | None = None
) -> Path | None:
    """Copy a session's transcript, every day's file in the order its lines
    were written, to `<dest>/<session_id>.jsonl` (by default ~/.evi/handoff/,
    which `evi sync` carries). Transcripts themselves stay on the device, so
    only the sessions you hand off travel. Replaces an earlier copy. Returns
    the copy's path, or None when the session has no transcript. Raises
    OSError, or ValueError for a transcript that isn't UTF-8 text."""
    files = session_files(session_id, root=root)
    if not files:
        return None
    lines = _session_lines(files)
    out_dir = dest if dest is not None else HANDOFF_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"{session_id}.jsonl"
    tmp = out_dir / f".{session_id}.jsonl.tmp"
    with tmp.open("w", encoding="utf-8", newline="\n") as f:
        f.write("".join(line + "\n" for line in lines))
    os.replace(tmp, out)
    return out


def adopt_handoff(
    session_id: str, *, root: Path | None = None, src: Path | None = None
) -> str:
    """Take in the copy of a session another device handed off (stage_handoff,
    arrived with `evi sync pull`), so a resume here finds all of it.

    Returns "adopted" when lines were added here: the session was missing, or
    this device's transcript is how the copy starts (it was handed off from
    here and went on over there). "current" when this device already has
    every line of the copy, "diverged" when both went on since (this device's
    transcript is left as it is), and "none" when there's no copy, or when the
    copy or this device's transcript can't be read (nothing is changed). The
    new lines are appended under the days their timestamps fall on, so
    nothing already here is rewritten.

    Serialised across threads and processes (_adoption_lock). Raises OSError
    when the lock can't be had or the new lines can't be written.
    """
    if not _valid_id(session_id):
        return "none"
    staged = (src if src is not None else HANDOFF_DIR) / f"{session_id}.jsonl"
    if not staged.is_file():
        return "none"
    with _adoption_lock(root or TRANSCRIPTS_DIR):
        # Compared only once the lock is held: whoever held it before may
        # have just taken this very copy in.
        return _adopt(session_id, staged, root)


@contextmanager
def _adoption_lock(base: Path, timeout: float = 15.0) -> Iterator[None]:
    """Hold the handoff lock of the transcripts under `base`: _ADOPT_LOCK for
    this process's threads, and an exclusive lock on `base/.handoff.lock` for
    other processes, which the system lets go of if the process holding it
    dies. Raises TimeoutError (an OSError) after `timeout` seconds."""
    base.mkdir(parents=True, exist_ok=True)
    with _ADOPT_LOCK:
        fd = os.open(base / _ADOPT_LOCK_FILE, os.O_RDWR | os.O_CREAT, 0o600)
        locked = False
        try:
            deadline = time.monotonic() + timeout
            while not (locked := _lock(fd)):
                if time.monotonic() >= deadline:
                    raise TimeoutError("another eVi is taking in a handed-off session")
                time.sleep(0.05)
            yield
        finally:
            if locked:
                _lock(fd, release=True)
            os.close(fd)


def _lock(fd: int, *, release: bool = False) -> bool:
    """Lock the first byte of the open file `fd` (its offset is 0) without
    waiting, or with `release` let it go. False when another open file holds
    the lock (or it couldn't be let go: closing the file does that too)."""
    try:
        if sys.platform == "win32":
            import msvcrt

            msvcrt.locking(fd, msvcrt.LK_UNLCK if release else msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_UN if release else fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    return True


def _adopt(session_id: str, staged: Path, root: Path | None) -> str:
    # Only the lines that hold an entry (a JSON object) count, in the copy and
    # here alike: no reader sees the others (a line a crash cut short, say),
    # and none of them is taken in.
    try:
        theirs = [line for line in _raw_lines(staged) if _object(line) is not None]
    except (OSError, ValueError):
        return "none"  # a copy that can't be read is no copy
    if not theirs:
        return "none"
    files = session_files(session_id, root=root)
    try:
        ours = [line for line in _session_lines(files) if _object(line) is not None]
    except (OSError, ValueError):
        return "none"  # nothing to compare with: leave this device's as it is
    if theirs[:len(ours)] != ours:
        return "current" if ours[:len(theirs)] == theirs else "diverged"
    if len(theirs) == len(ours):
        return "current"
    base = root or TRANSCRIPTS_DIR
    # A line whose timestamp can't be read goes with the line before it.
    day = files[-1].parent.name if files else datetime.now().strftime("%Y-%m-%d")
    by_day: dict[str, list[str]] = {}
    for line in theirs[len(ours):]:
        try:
            day = datetime.fromtimestamp(float(json.loads(line)["ts"])).strftime("%Y-%m-%d")
        except (ValueError, TypeError, KeyError, OverflowError, OSError):
            pass
        by_day.setdefault(day, []).append(line)
    for name, lines in by_day.items():
        (base / name).mkdir(parents=True, exist_ok=True)
        with (base / name / f"{session_id}.jsonl").open("a", encoding="utf-8") as f:
            f.write("".join(line + "\n" for line in lines))
    return "adopted"


def most_recent_session_id(*, root: Path | None = None) -> str | None:
    """The id of the most recently active session (by last timestamp), or None.

    list_sessions orders by day-dir + filename, so we sort by `ended_at` here to
    get the true most-recent rather than the lexically-last filename. A branch
    or fork nobody has written in yet isn't listed, so it isn't picked."""
    items = [s for s in list_sessions(root=root, limit=100_000) if s.ended_at is not None]
    if not items:
        return None
    return max(items, key=lambda s: s.ended_at).session_id


def _valid_id(session_id: str) -> bool:
    """A session id names a file: no path in it."""
    return (
        bool(session_id) and session_id not in (".", "..")
        and Path(session_id).name == session_id
    )


def session_files(session_id: str, *, root: Path | None = None) -> list[Path]:
    """Every transcript file of a session, oldest day first. TranscriptStore
    files each line under the day it was written, so a session that ran past
    midnight has one per day."""
    base = root or TRANSCRIPTS_DIR
    if not _valid_id(session_id) or not base.is_dir():
        return []
    return [
        day_dir / f"{session_id}.jsonl"
        for day_dir in sorted(base.iterdir())
        if day_dir.is_dir() and (day_dir / f"{session_id}.jsonl").is_file()
    ]


def find_session(session_id: str, *, root: Path | None = None) -> Path | None:
    """The transcript of a session: its first day's file, or None. It names the
    whole session: a replay (history_from_transcript), an export and a
    handoff read the session's files for the other days too (session_files)."""
    files = session_files(session_id, root=root)
    return files[0] if files else None


def _session_paths(path: Path) -> list[Path]:
    """`path` and the same session's files in the other day directories,
    oldest day first; just `path` when it isn't in a day directory."""
    if _DAY.fullmatch(path.parent.name):
        return session_files(path.stem, root=path.parent.parent) or [path]
    return [path]


def _read_session(path: Path) -> list[dict[str, Any]]:
    """The entries of the whole session `path` names, every day's file of it,
    in the order they were written (_in_time_order)."""
    return _in_time_order([_read_entries(p) for p in _session_paths(path)], _entry_ts)


def _entry_ts(entry: dict[str, Any]) -> Any:
    return entry.get("ts")


def history_from_transcript(
    path: Path, *, replay: bool = False, apply_compaction: bool = False
) -> list[dict[str, Any]]:
    """Rebuild an Agent.history list (without its system prompt) from a stored
    transcript.

    Order is preserved. Goal-reminder prefixes injected by Agent.chat are
    LEFT IN PLACE so the reconstructed history matches what the model
    originally saw — important for KV cache symmetry on resume. For the same
    reason a resume passes `replay=True` (`apply_compaction=True`, its older
    name, does the same): the session's meta records are applied in order,
    so the result is the history the user and the model last had. A
    compaction replaces what it summarised with the summary
    (evi.history.compaction_messages), an edit rewrites its message and drops
    what followed it, a truncate cuts back, and a fork_of record starts the
    history with the messages it copied (evi.transcripts). A record applied
    twice changes nothing more. A replay reads the whole session: `path`
    names it, and its files in other day directories (it ran past midnight)
    are read too, in the order their lines were written. Without replay only
    `path` is read and every message it logged is kept, meta records
    skipped, which is what a reader of the log (`evi sessions show`,
    fine-tune export) wants.

    Each tool result is paired with the call it answers: by the
    `tool_call_id` it was logged with, or by position for transcripts written
    before that was logged. A call with no logged result (the turn was cut
    short) gets a marked placeholder, and a result with no call is dropped
    (evi.history.pair_tool_results).
    """
    replay = replay or apply_compaction
    history: list[dict[str, Any]] = []
    for raw in _read_session(path) if replay else _read_entries(path):
        if (raw.get("type") or MESSAGE) != MESSAGE:
            if replay:
                history = _apply_record(history, raw)
            continue  # a meta record: part of the log, not of the conversation
        msg = _message(raw)
        if msg is not None:
            history.append(msg)
    return pair_tool_results(history)


def _message(raw: dict[str, Any]) -> dict[str, Any] | None:
    """A history message from a transcript line, or from a message a fork_of
    record copied (whose tool name is `name`, not `tool_name`). None for any
    other role."""
    role = raw.get("role")
    if role not in ("user", "assistant", "tool"):
        return None
    content = raw.get("content", "")
    msg: dict[str, Any] = {"role": role, "content": "" if content is None else content}
    if role == "tool":
        msg["name"] = raw.get("tool_name") or raw.get("name") or ""
        msg["tool_call_id"] = raw.get("tool_call_id")  # None: paired by position
    if role == "assistant" and raw.get("tool_calls"):
        msg["tool_calls"] = raw["tool_calls"]
    return msg


def _int(value: Any) -> int | None:
    """`value` as an int, or None when it isn't one: not a number, or one
    with no int (NaN, and Infinity or 1e999, which JSON reads as infinite)."""
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return None


def _aligned(history: list[dict[str, Any]], length: Any) -> list[dict[str, Any]]:
    """`history` lined up with the one the writer of a record had, which held
    `length` messages. They differ only after a revive or resume: it paired
    the tool calls and results it rebuilt (a placeholder for a call with no
    logged result, a result with no call dropped: pair_tool_results), which a
    log replayed as written hasn't yet. Then the paired form is the writer's.
    Otherwise, or with no `length`, `history` as it is."""
    want = _int(length)
    if want is None or len(history) == want:
        return history
    paired = pair_tool_results(history)
    return paired if len(paired) == want else history


def _apply_record(history: list[dict[str, Any]], record: dict[str, Any]) -> list[dict[str, Any]]:
    """Replay one meta record onto the messages rebuilt so far. A record of a
    type this version doesn't know, or one it can't read, changes nothing."""
    kind = record.get("type")
    if kind == COMPACTION:
        return _apply_compaction(history, record)
    if kind == FORK:
        copied = record.get("messages")
        if not isinstance(copied, list):
            return history
        return [m for m in (_message(c) for c in copied if isinstance(c, dict)) if m is not None]
    if kind == TRUNCATE:
        keep = _int(record.get("keep"))
        if keep is None:
            return history
        return _aligned(history, record.get("length"))[:max(0, keep)]
    if kind == EDIT:
        index = _int(record.get("index"))
        if index is None or index < 0:
            return history
        role = str(record.get("message_role") or "user")
        text = record.get("text")
        history = _aligned(history, record.get("length"))
        old = history[index] if index < len(history) else None
        if old is None or old.get("role") != role:
            old = {"role": role}
        return [*history[:index], {**old, "content": "" if text is None else str(text)}]
    return history


def _apply_compaction(history: list[dict[str, Any]], record: dict[str, Any]) -> list[dict[str, Any]]:
    """Replay one compaction record onto the messages rebuilt so far: keep the
    last `kept` of them (the logged messages the kept tail held) and put the
    summary before them. A tail that would start with a tool result (its call
    was summarised away) drops it."""
    summary = str(record.get("summary") or "").strip()
    if not summary:
        return history
    kept, collapsed = _int(record.get("kept") or 0), _int(record.get("collapsed") or 0)
    if kept is None or collapsed is None:
        return history
    kept, collapsed = max(0, kept), max(0, collapsed)
    cut = _int(record.get("cut"))
    if cut is not None:
        # The writer's history held `cut` messages before the kept tail, the
        # system prompt one of them, and the `kept` of the tail.
        history = _aligned(history, cut - 1 + kept)
    tail = history[max(0, len(history) - kept):] if kept else []
    while tail and tail[0].get("role") == "tool":
        tail = tail[1:]
    if not collapsed:
        collapsed = len(history) - len(tail)
    head = compaction_messages(summary, collapsed, next_role=tail[0].get("role") if tail else None)
    return [*head, *tail]


def fmt_when(ts: float | None) -> str:
    if ts is None:
        return "—"
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")


# ---- export -------------------------------------------------------------


def export_markdown(path: Path) -> str:
    """Render a stored session as a self-contained markdown document: its
    whole log, every day's file of it, meta records left out."""
    info = _summarize(path, path.parent.name)
    title = info.first_user_message[:80] if info else path.stem
    entries = _read_session(path)
    lines = [
        f"# Session {path.stem}",
        "",
        f"- Day: {path.parent.name}",
        f"- Messages: {sum(1 for e in entries if (e.get('type') or MESSAGE) == MESSAGE)}",
        f"- Started: {fmt_when(info.started_at) if info else '—'}",
        f"- Topic: {title}",
        "",
        "---",
        "",
    ]
    for entry in entries:
        role = entry.get("role", "")
        content = entry.get("content", "") or ""
        if role == "system":
            continue
        if role == "tool":
            name = entry.get("tool_name", "tool")
            lines.append(f"**🔧 {name}**\n")
            lines.append(f"```\n{content}\n```")
        elif role == "user":
            lines.append("**🧑 user**\n")
            lines.append(content)
        elif role == "assistant":
            lines.append("**🤖 evi**\n")
            lines.append(content)
            tc = entry.get("tool_calls")
            if tc:
                lines.append("")
                for call in tc:
                    fn = call.get("function", {})
                    lines.append(
                        f"> *called* `{fn.get('name', '?')}({fn.get('arguments', '{}')})`"
                    )
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def export_json(path: Path) -> str:
    """Return the session's raw transcript entries, every day's file of it,
    as a JSON array string."""
    import json as _json

    return _json.dumps(_read_session(path), indent=2, default=str)


def export_html(path: Path) -> str:
    """Wrap the markdown export in a minimal HTML shell for browser viewing.

    No JS — pure styled document. Markdown features beyond the renderer's
    basics (bold, code blocks, headers) won't render here, but the
    transcript is fully readable.
    """
    md = export_markdown(path)

    def _escape(s: str) -> str:
        return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    # Minimal markdown → HTML: handle code fences and bold runs.
    html_parts: list[str] = []
    in_code = False
    code_buf: list[str] = []
    for raw in md.splitlines():
        if raw.startswith("```"):
            if in_code:
                html_parts.append(
                    "<pre><code>" + "\n".join(_escape(line) for line in code_buf) + "</code></pre>"
                )
                code_buf = []
            in_code = not in_code
            continue
        if in_code:
            code_buf.append(raw)
            continue
        if raw.startswith("# "):
            html_parts.append(f"<h1>{_escape(raw[2:])}</h1>")
        elif raw.startswith("> "):
            html_parts.append(f"<blockquote>{_escape(raw[2:])}</blockquote>")
        elif raw.strip() == "---":
            html_parts.append("<hr>")
        else:
            line = _escape(raw)
            # Re-introduce **bold** runs.
            import re as _re
            line = _re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", line)
            html_parts.append(f"<p>{line}</p>")
    body = "\n".join(html_parts)
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        f"<title>eVi session {path.stem}</title>"
        "<style>body{font:14px/1.5 -apple-system,Segoe UI,system-ui,sans-serif;"
        "max-width:720px;margin:30px auto;padding:0 16px;color:#222}"
        "h1{font-size:18px}pre{background:#f4f4f4;padding:8px 10px;border-radius:4px;"
        "overflow-x:auto}blockquote{color:#666;border-left:3px solid #ddd;padding-left:10px;"
        "margin:0}strong{color:#000}</style></head><body>"
        f"{body}</body></html>"
    )


def export_session(
    session_id: str,
    *,
    fmt: str = "md",
    root: Path | None = None,
) -> str:
    """High-level export by session id. `fmt` is 'md', 'html', or 'json'."""
    path = find_session(session_id, root=root)
    if path is None:
        raise FileNotFoundError(f"no session {session_id!r}")
    fmt = fmt.lower()
    if fmt in ("md", "markdown"):
        return export_markdown(path)
    if fmt == "html":
        return export_html(path)
    if fmt == "json":
        return export_json(path)
    raise ValueError(f"unknown format {fmt!r} (use md / html / json)")
