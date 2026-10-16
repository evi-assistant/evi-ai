"""Session transcript persistence.

After every chat turn we append the new messages to
`~/.evi/transcripts/<YYYY-MM-DD>/<session>.jsonl`. One JSON object per line,
one file per session per day so the dreaming engine can read "the last 24
hours" without parsing huge logs.

Why JSONL and not a database: simple, append-only, hand-greppable. We're
not querying these at scale; the dream engine reads sequentially.

Most lines are messages. A line with a ``type`` other than "message" is a meta
record (a compaction, an edit, a fork, say) that replay applies rather than
shows; it has role "system" and empty content, so readers that predate meta
records skip it like any system line. A line without ``type`` is a message.

A session that runs past midnight has a file in each day's directory; the
readers that rebuild a session (evi.sessions) read them all, in the order the
lines were written, and retention (prune) keeps or drops a session whole.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterator

from evi.config import TRANSCRIPTS_DIR


# Record types. MESSAGE is the default and is never written out, so message
# lines look exactly as they always have.
MESSAGE = "message"
# compact_history replaced older messages with a summary. Fields: `summary`
# (the summary text), `collapsed` (how many messages it stands for), `cut` (the
# index in Agent.history, system prompt at 0, where the kept tail started) and
# `kept` (how many of that tail's messages were logged, which is all of them:
# notes from outside the chat are logged too, as user lines). Replay keeps the
# last `kept` messages rebuilt so far and puts the summary before them
# (evi.sessions.history_from_transcript with replay=True).
COMPACTION = "compaction"

# The records below describe the history in replay positions: messages are
# counted from 0 with the system prompt left out, as replay rebuilds them.
# Each carries `length`, how many messages the history held just before the
# change, which replay uses to line its own rebuild up with the writer's
# (evi.sessions, _aligned).
#
# The history was cut back (truncate_history, a re-roll's rewind, /reset).
# Fields: `keep` (how many messages are left) and `length`.
TRUNCATE = "truncate"
# A message was rewritten and everything after it dropped (edit_message).
# Fields: `index` (its position), `message_role` (its role), `text` (the new
# content) and `length`.
EDIT = "edit"
# The session starts as a copy of another one's history (a web branch,
# `evi sessions fork`). Fields: `source` (that session's id), `cut` (how many
# of its messages were copied) and `messages` (the copies, in the form replay
# rebuilds). The copies live in the record, not as message lines, because
# they were logged once already, in the source: stats and fine-tune export
# count them there.
FORK = "fork_of"

_FIELDS = ("session", "ts", "role", "content", "tool_name", "tool_calls", "tool_call_id", "type")


def transcript_message(message: dict[str, Any]) -> dict[str, Any] | None:
    """`message` from a live history in the form replay rebuilds it: role,
    text content, and the tool-call fields its role carries. Multimodal
    content becomes its text plus a count of the attachments, as Agent.chat
    logs it, so no image or audio data is written. None for a message replay
    never rebuilds (the system prompt)."""
    role = message.get("role")
    if role not in ("user", "assistant", "tool"):
        return None
    content = message.get("content")
    if isinstance(content, list):
        text = "".join(
            str(p.get("text", "")) for p in content
            if isinstance(p, dict) and p.get("type") == "text"
        )
        kinds = [p.get("type") for p in content if isinstance(p, dict)]
        bits = [
            f"+{n} {label}"
            for label, n in (("audio", kinds.count("input_audio")),
                             ("image", kinds.count("image_url")))
            if n
        ]
        content = f"{text}\n\n[{', '.join(bits)} attachment(s)]" if bits else text
    out: dict[str, Any] = {"role": role, "content": "" if content is None else str(content)}
    if role == "assistant" and message.get("tool_calls"):
        out["tool_calls"] = message["tool_calls"]
    if role == "tool":
        out["name"] = message.get("name") or ""
        out["tool_call_id"] = message.get("tool_call_id")
    return out


@dataclass
class TranscriptEntry:
    session: str
    timestamp: float
    role: str           # "user" | "assistant" | "tool" | "system"
    content: str        # text for chat roles; tool output for "tool"
    tool_name: str | None = None   # only set when role == "tool"
    tool_calls: list[dict] | None = None  # only on assistant messages with calls
    tool_call_id: str | None = None  # on "tool": the id of the call it answers
    type: str = MESSAGE  # anything else is a meta record (COMPACTION, …)
    meta: dict[str, Any] | None = None  # a meta record's own fields, written flat

    @property
    def is_message(self) -> bool:
        return self.type == MESSAGE

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "session": self.session,
            "ts": self.timestamp,
            "role": self.role,
            "content": self.content,
        }
        if self.tool_name is not None:
            d["tool_name"] = self.tool_name
        if self.tool_calls is not None:
            d["tool_calls"] = self.tool_calls
        if self.tool_call_id is not None:
            d["tool_call_id"] = self.tool_call_id
        if self.type != MESSAGE:
            d["type"] = self.type
            for k, v in (self.meta or {}).items():
                if k not in _FIELDS:
                    d[k] = v
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "TranscriptEntry":
        kind = str(d.get("type") or MESSAGE)
        return cls(
            session=str(d.get("session", "")),
            timestamp=float(d.get("ts", 0)),
            role=str(d.get("role", "")),
            content=str(d.get("content", "")),
            tool_name=d.get("tool_name"),
            tool_calls=d.get("tool_calls"),
            tool_call_id=d.get("tool_call_id"),
            type=kind,
            meta=(
                {k: v for k, v in d.items() if k not in _FIELDS} if kind != MESSAGE else None
            ),
        )


class TranscriptStore:
    """Append-only JSONL store partitioned by day + session."""

    def __init__(self, root: Path | None = None) -> None:
        self.root = Path(root) if root is not None else TRANSCRIPTS_DIR

    def write(self, entry: TranscriptEntry) -> None:
        day = datetime.fromtimestamp(entry.timestamp).strftime("%Y-%m-%d")
        d = self.root / day
        d.mkdir(parents=True, exist_ok=True)
        path = d / f"{entry.session}.jsonl"
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry.to_dict()) + "\n")

    def write_message(
        self,
        session: str,
        role: str,
        content: str,
        *,
        tool_name: str | None = None,
        tool_calls: list[dict] | None = None,
        tool_call_id: str | None = None,
        timestamp: float | None = None,
    ) -> None:
        self.write(
            TranscriptEntry(
                session=session,
                timestamp=timestamp if timestamp is not None else time.time(),
                role=role,
                content=content,
                tool_name=tool_name,
                tool_calls=tool_calls,
                tool_call_id=tool_call_id,
            )
        )

    def write_record(
        self,
        session: str,
        kind: str,
        fields: dict[str, Any],
        *,
        timestamp: float | None = None,
    ) -> None:
        """Append a meta record of type `kind` with `fields` (see MESSAGE)."""
        self.write(
            TranscriptEntry(
                session=session,
                timestamp=timestamp if timestamp is not None else time.time(),
                role="system",
                content="",
                type=kind,
                meta=dict(fields),
            )
        )

    def write_compaction(
        self,
        session: str,
        summary: str,
        *,
        cut: int,
        kept: int,
        collapsed: int,
        timestamp: float | None = None,
    ) -> None:
        """Record a compaction (see COMPACTION)."""
        self.write_record(
            session,
            COMPACTION,
            {"summary": summary, "cut": int(cut), "kept": int(kept), "collapsed": int(collapsed)},
            timestamp=timestamp,
        )

    def write_truncate(
        self, session: str, *, keep: int, length: int, timestamp: float | None = None
    ) -> None:
        """Record a cut back to the first `keep` of `length` messages (see TRUNCATE)."""
        self.write_record(
            session, TRUNCATE, {"keep": int(keep), "length": int(length)}, timestamp=timestamp
        )

    def write_edit(
        self,
        session: str,
        *,
        index: int,
        role: str,
        text: str,
        length: int,
        timestamp: float | None = None,
    ) -> None:
        """Record an edit of the message at `index` (see EDIT)."""
        self.write_record(
            session,
            EDIT,
            {"index": int(index), "message_role": role, "text": text, "length": int(length)},
            timestamp=timestamp,
        )

    def write_fork(
        self,
        session: str,
        *,
        source: str,
        messages: list[dict[str, Any]],
        timestamp: float | None = None,
    ) -> None:
        """Record that `session` starts as a copy of `messages` from session
        `source` (see FORK)."""
        copied = [m for m in map(transcript_message, messages) if m is not None]
        self.write_record(
            session,
            FORK,
            {"source": source, "cut": len(copied), "messages": copied},
            timestamp=timestamp,
        )

    def message_count(self, session: str, role: str) -> int:
        """How many `role` messages `session` has logged, over all its day
        files. Meta records are not messages."""
        if not session or Path(session).name != session or not self.root.is_dir():
            return 0
        n = 0
        for day_dir in self.root.iterdir():
            path = day_dir / f"{session}.jsonl"
            if not path.is_file():
                continue
            try:
                with path.open("r", encoding="utf-8") as f:
                    for line in f:
                        try:
                            d = json.loads(line)
                        except ValueError:
                            continue
                        if (
                            isinstance(d, dict) and d.get("role") == role
                            and (d.get("type") or MESSAGE) == MESSAGE
                        ):
                            n += 1
            except OSError:
                continue
        return n

    def iter_since(self, cutoff: datetime) -> Iterator[TranscriptEntry]:
        """Yield every transcript entry with timestamp >= cutoff, oldest first."""
        if not self.root.is_dir():
            return
        cutoff_ts = cutoff.timestamp()
        # Days are sortable lexicographically as YYYY-MM-DD.
        for day_dir in sorted(p for p in self.root.iterdir() if p.is_dir()):
            try:
                day_dt = datetime.strptime(day_dir.name, "%Y-%m-%d")
            except ValueError:
                continue
            # Skip whole days that ended before the cutoff.
            if (day_dt + timedelta(days=1)).timestamp() < cutoff_ts:
                continue
            for path in sorted(day_dir.glob("*.jsonl")):
                try:
                    with path.open("r", encoding="utf-8") as f:
                        for line in f:
                            if not line.strip():
                                continue
                            try:
                                entry = TranscriptEntry.from_dict(json.loads(line))
                            except (json.JSONDecodeError, ValueError):
                                continue
                            if entry.timestamp >= cutoff_ts:
                                yield entry
                except OSError:
                    continue

    def prune(self, keep_days: int = 30) -> int:
        """Delete day-directories older than `keep_days`. Returns count removed.

        A session goes by its last day: one that ran on into a day that is
        kept keeps its files in the older days too, and their directories
        stay. Half a session would replay wrong, as the records a replay
        applies count messages from the session's start (evi.sessions)."""
        if not self.root.is_dir():
            return 0
        cutoff = datetime.now() - timedelta(days=keep_days)
        old: list[Path] = []
        ongoing: set[str] = set()
        for day_dir in self.root.iterdir():
            if not day_dir.is_dir():
                continue
            try:
                day_dt = datetime.strptime(day_dir.name, "%Y-%m-%d")
            except ValueError:
                continue
            if day_dt < cutoff:
                old.append(day_dir)
            else:
                ongoing.update(f.stem for f in day_dir.glob("*.jsonl"))
        removed = 0
        for day_dir in old:
            for f in day_dir.glob("*.jsonl"):
                if f.stem not in ongoing:
                    f.unlink()
            if not any(day_dir.iterdir()):
                day_dir.rmdir()
                removed += 1
        return removed
