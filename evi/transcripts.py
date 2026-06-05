"""Session transcript persistence.

After every chat turn we append the new messages to
`~/.evi/transcripts/<YYYY-MM-DD>/<session>.jsonl`. One JSON object per line,
one file per session per day so the dreaming engine can read "the last 24
hours" without parsing huge logs.

Why JSONL and not a database: simple, append-only, hand-greppable. We're
not querying these at scale; the dream engine reads sequentially.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterator

from evi.config import TRANSCRIPTS_DIR


@dataclass
class TranscriptEntry:
    session: str
    timestamp: float
    role: str           # "user" | "assistant" | "tool" | "system"
    content: str        # text for chat roles; tool output for "tool"
    tool_name: str | None = None   # only set when role == "tool"
    tool_calls: list[dict] | None = None  # only on assistant messages with calls

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
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "TranscriptEntry":
        return cls(
            session=str(d.get("session", "")),
            timestamp=float(d.get("ts", 0)),
            role=str(d.get("role", "")),
            content=str(d.get("content", "")),
            tool_name=d.get("tool_name"),
            tool_calls=d.get("tool_calls"),
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
            )
        )

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
        """Delete day-directories older than `keep_days`. Returns count removed."""
        if not self.root.is_dir():
            return 0
        cutoff = datetime.now() - timedelta(days=keep_days)
        removed = 0
        for day_dir in self.root.iterdir():
            if not day_dir.is_dir():
                continue
            try:
                day_dt = datetime.strptime(day_dir.name, "%Y-%m-%d")
            except ValueError:
                continue
            if day_dt < cutoff:
                for f in day_dir.glob("*.jsonl"):
                    f.unlink()
                day_dir.rmdir()
                removed += 1
        return removed
