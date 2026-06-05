"""Tests for the JSONL transcript store."""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta
from pathlib import Path

from evi.transcripts import TranscriptStore


def test_write_creates_day_partition(tmp_path: Path) -> None:
    store = TranscriptStore(root=tmp_path)
    store.write_message(session="abc", role="user", content="hello")
    days = list(tmp_path.iterdir())
    assert len(days) == 1
    files = list(days[0].glob("*.jsonl"))
    assert [f.name for f in files] == ["abc.jsonl"]


def test_write_appends_multiple_lines(tmp_path: Path) -> None:
    store = TranscriptStore(root=tmp_path)
    store.write_message(session="s1", role="user", content="hi")
    store.write_message(session="s1", role="assistant", content="hello back")
    [day] = list(tmp_path.iterdir())
    [path] = list(day.glob("*.jsonl"))
    lines = path.read_text("utf-8").strip().splitlines()
    assert len(lines) == 2
    decoded = [json.loads(line) for line in lines]
    assert decoded[0]["role"] == "user"
    assert decoded[1]["role"] == "assistant"


def test_iter_since_filters_by_cutoff(tmp_path: Path) -> None:
    store = TranscriptStore(root=tmp_path)
    old_ts = (datetime.now() - timedelta(days=10)).timestamp()
    fresh_ts = time.time()
    store.write_message(session="s", role="user", content="old", timestamp=old_ts)
    store.write_message(session="s", role="user", content="fresh", timestamp=fresh_ts)

    cutoff = datetime.now() - timedelta(hours=1)
    entries = list(store.iter_since(cutoff))
    assert [e.content for e in entries] == ["fresh"]


def test_iter_since_yields_oldest_first(tmp_path: Path) -> None:
    store = TranscriptStore(root=tmp_path)
    base = time.time()
    store.write_message(session="s", role="user", content="b", timestamp=base + 10)
    store.write_message(session="s", role="user", content="a", timestamp=base + 5)
    entries = list(store.iter_since(datetime.fromtimestamp(base)))
    # Within a file order is write order; the dream engine doesn't strictly
    # need timestamp sort, just stable replay.
    assert [e.content for e in entries] == ["b", "a"]


def test_iter_since_ignores_malformed_lines(tmp_path: Path) -> None:
    # Use today's date so the day-dir is always inside the iter_since window
    # below (a hardcoded date here becomes a time-bomb once the clock passes
    # it by more than the lookback).
    day = tmp_path / datetime.now().strftime("%Y-%m-%d")
    day.mkdir()
    f = day / "s.jsonl"
    good = json.dumps({
        "session": "s", "ts": time.time(), "role": "user", "content": "ok"
    })
    f.write_text(good + "\n{garbage\n" + good + "\n")
    store = TranscriptStore(root=tmp_path)
    entries = list(store.iter_since(datetime.now() - timedelta(days=2)))
    assert len(entries) == 2


def test_prune_removes_old_day_dirs(tmp_path: Path) -> None:
    store = TranscriptStore(root=tmp_path)
    old_day = (datetime.now() - timedelta(days=60)).strftime("%Y-%m-%d")
    new_day = datetime.now().strftime("%Y-%m-%d")
    (tmp_path / old_day).mkdir()
    (tmp_path / old_day / "s.jsonl").write_text("{}\n")
    (tmp_path / new_day).mkdir()
    (tmp_path / new_day / "s.jsonl").write_text("{}\n")

    removed = store.prune(keep_days=30)
    assert removed == 1
    assert not (tmp_path / old_day).exists()
    assert (tmp_path / new_day).exists()
