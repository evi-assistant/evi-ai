"""Conversation grep — search across all transcripts.

We already write every chat turn to `~/.evi/transcripts/<YYYY-MM-DD>/<session>.jsonl`
(see `evi/transcripts.py`). This module adds a small grep over those
files. The CLI surface is `evi search "<query>"`; everything else here
is shared with the future web search-UI.

The matcher supports plain substring (default, case-insensitive) and
regex (opt-in via the `regex=True` flag). Results carry enough metadata
to render a snippet with highlighted match plus a `evi sessions show
<id>` jump-target.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterator

from evi.config import TRANSCRIPTS_DIR


@dataclass
class SearchMatch:
    """One hit returned by `search`."""

    session: str
    timestamp: float
    role: str
    content: str
    snippet: str
    line_no: int  # 1-indexed line number in the JSONL file
    file: Path

    @property
    def date(self) -> str:
        return datetime.fromtimestamp(self.timestamp).strftime("%Y-%m-%d %H:%M")


def _make_matcher(query: str, regex: bool) -> "callable[[str], re.Match | None]":
    """Build a function that returns the first match (or None) for `text`."""
    if regex:
        try:
            pat = re.compile(query, re.IGNORECASE)
        except re.error as exc:
            raise ValueError(f"invalid regex: {exc}") from exc
        return pat.search
    needle = query.lower()
    # Wrap substring search to return a Match-like span tuple, so the
    # snippet builder has one code path.
    def find_substr(text: str):
        idx = text.lower().find(needle)
        if idx < 0:
            return None
        # A lightweight stand-in for re.Match — only `start()` and `end()`
        # are used downstream.
        class _Hit:
            def start(self) -> int: return idx
            def end(self) -> int: return idx + len(needle)
        return _Hit()
    return find_substr


def _make_snippet(content: str, m, *, before: int = 40, after: int = 80) -> str:
    """Build a one-line snippet around a match.

    `m` is anything with `start()` / `end()`. The snippet is collapsed
    to a single line (newlines → spaces) and capped at `before + after`
    chars on either side of the match.
    """
    flat = content.replace("\n", " ").replace("\r", " ")
    start = max(0, m.start() - before)
    end = min(len(flat), m.end() + after)
    prefix = "…" if start > 0 else ""
    suffix = "…" if end < len(flat) else ""
    return prefix + flat[start:end] + suffix


def search(
    query: str,
    *,
    days: int = 90,
    role: str | None = None,
    session: str | None = None,
    regex: bool = False,
    limit: int = 100,
    root: Path | None = None,
) -> Iterator[SearchMatch]:
    """Yield matches newest-first across the transcript store.

    Args:
        query: Needle. Substring by default; regex if `regex=True`.
        days: Limit the search window — only files dated within the last
            `days` days are scanned. Defaults to 90 (~3 months).
        role: If set, only match messages with this role.
        session: If set, only scan this session's JSONL.
        regex: Interpret `query` as a regular expression (case-insensitive).
        limit: Stop after this many matches.
        root: Override the transcripts dir (test hook).
    """
    if not query.strip():
        return
    matcher = _make_matcher(query, regex)
    base = Path(root) if root is not None else TRANSCRIPTS_DIR
    if not base.is_dir():
        return

    cutoff = datetime.now() - timedelta(days=max(1, int(days)))
    cutoff_ts = cutoff.timestamp()
    # Day directories sort lexicographically newest-last; reverse so we
    # surface recent matches first.
    day_dirs = sorted(
        (p for p in base.iterdir() if p.is_dir()),
        reverse=True,
    )

    count = 0
    for day_dir in day_dirs:
        try:
            day_dt = datetime.strptime(day_dir.name, "%Y-%m-%d")
        except ValueError:
            continue
        # Whole-day cull: a day that ENDED before the cutoff has no
        # entries we care about.
        if (day_dt + timedelta(days=1)).timestamp() < cutoff_ts:
            continue
        files = sorted(day_dir.glob("*.jsonl"), reverse=True)
        if session is not None:
            files = [f for f in files if f.stem == session]
        for path in files:
            try:
                with path.open("r", encoding="utf-8") as f:
                    for line_no, raw in enumerate(f, start=1):
                        if not raw.strip():
                            continue
                        try:
                            entry = json.loads(raw)
                        except json.JSONDecodeError:
                            continue
                        if not isinstance(entry, dict):
                            continue
                        if entry.get("ts", 0) < cutoff_ts:
                            continue
                        if role is not None and entry.get("role") != role:
                            continue
                        content = str(entry.get("content") or "")
                        if not content:
                            continue
                        m = matcher(content)
                        if m is None:
                            continue
                        yield SearchMatch(
                            session=str(entry.get("session", path.stem)),
                            timestamp=float(entry.get("ts", 0)),
                            role=str(entry.get("role", "?")),
                            content=content,
                            snippet=_make_snippet(content, m),
                            line_no=line_no,
                            file=path,
                        )
                        count += 1
                        if count >= limit:
                            return
            except OSError:
                continue


def collect(query: str, **kwargs) -> list[SearchMatch]:
    """Materialise the iterator into a list. Convenience for tests + CLI."""
    return list(search(query, **kwargs))
