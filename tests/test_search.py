"""Tests for evi/search.py — conversation grep across transcripts."""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from evi.search import _make_snippet, collect


def _write_entry(path: Path, *, session, role, content, ts) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps({
            "session": session,
            "ts": ts,
            "role": role,
            "content": content,
        }) + "\n")


def _now_minus(days: int) -> float:
    return (datetime.now() - timedelta(days=days)).timestamp()


# ----- basic matching ------------------------------------------------------


def test_search_finds_substring_case_insensitive(tmp_path: Path) -> None:
    day = datetime.now().strftime("%Y-%m-%d")
    f = tmp_path / day / "abc123.jsonl"
    _write_entry(f, session="abc123", role="user", content="Need to FIX the deploy bug", ts=time.time())
    hits = collect("fix the", root=tmp_path)
    assert len(hits) == 1
    assert hits[0].session == "abc123"
    assert "FIX" in hits[0].snippet


def test_search_returns_no_results_when_query_misses(tmp_path: Path) -> None:
    day = datetime.now().strftime("%Y-%m-%d")
    _write_entry(
        tmp_path / day / "s.jsonl",
        session="s", role="user", content="hello world", ts=time.time(),
    )
    assert collect("nothing matches", root=tmp_path) == []


def test_search_empty_query_yields_nothing(tmp_path: Path) -> None:
    day = datetime.now().strftime("%Y-%m-%d")
    _write_entry(
        tmp_path / day / "s.jsonl",
        session="s", role="user", content="hello", ts=time.time(),
    )
    assert collect("   ", root=tmp_path) == []


# ----- regex mode ----------------------------------------------------------


def test_search_regex_matches(tmp_path: Path) -> None:
    day = datetime.now().strftime("%Y-%m-%d")
    _write_entry(
        tmp_path / day / "s.jsonl",
        session="s", role="user", content="TODO before FIXME", ts=time.time(),
    )
    hits = collect("TODO|FIXME", regex=True, root=tmp_path)
    # The single message contains both; we yield it ONCE (first match suffices).
    assert len(hits) == 1


def test_search_regex_invalid_raises(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="invalid regex"):
        collect("(unclosed", regex=True, root=tmp_path)


# ----- filtering -----------------------------------------------------------


def test_search_role_filter(tmp_path: Path) -> None:
    day = datetime.now().strftime("%Y-%m-%d")
    f = tmp_path / day / "s.jsonl"
    _write_entry(f, session="s", role="user", content="please deploy", ts=time.time())
    _write_entry(f, session="s", role="assistant", content="ok, deploy started", ts=time.time())
    user_hits = collect("deploy", role="user", root=tmp_path)
    assert len(user_hits) == 1
    assert user_hits[0].role == "user"


def test_search_session_filter(tmp_path: Path) -> None:
    day = datetime.now().strftime("%Y-%m-%d")
    _write_entry(
        tmp_path / day / "alpha.jsonl",
        session="alpha", role="user", content="hello", ts=time.time(),
    )
    _write_entry(
        tmp_path / day / "beta.jsonl",
        session="beta", role="user", content="hello", ts=time.time(),
    )
    hits = collect("hello", session="alpha", root=tmp_path)
    assert all(h.session == "alpha" for h in hits)
    assert len(hits) == 1


def test_search_days_window_excludes_old(tmp_path: Path) -> None:
    """A 200-day-old entry should be excluded by --days 30."""
    today = datetime.now().strftime("%Y-%m-%d")
    old_day = (datetime.now() - timedelta(days=200)).strftime("%Y-%m-%d")
    _write_entry(
        tmp_path / today / "new.jsonl",
        session="new", role="user", content="match", ts=time.time(),
    )
    _write_entry(
        tmp_path / old_day / "old.jsonl",
        session="old", role="user", content="match", ts=_now_minus(200),
    )
    hits = collect("match", days=30, root=tmp_path)
    sessions = [h.session for h in hits]
    assert "new" in sessions
    assert "old" not in sessions


def test_search_limit_caps_results(tmp_path: Path) -> None:
    day = datetime.now().strftime("%Y-%m-%d")
    for i in range(20):
        _write_entry(
            tmp_path / day / f"sess{i:02d}.jsonl",
            session=f"sess{i:02d}", role="user", content="match", ts=time.time(),
        )
    hits = collect("match", limit=5, root=tmp_path)
    assert len(hits) == 5


# ----- ordering ------------------------------------------------------------


def test_search_yields_newest_first(tmp_path: Path) -> None:
    today = datetime.now().strftime("%Y-%m-%d")
    yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    _write_entry(
        tmp_path / yesterday / "old.jsonl",
        session="old", role="user", content="match", ts=_now_minus(1),
    )
    _write_entry(
        tmp_path / today / "new.jsonl",
        session="new", role="user", content="match", ts=time.time(),
    )
    hits = collect("match", root=tmp_path)
    assert hits[0].session == "new"


# ----- snippet builder -----------------------------------------------------


def test_make_snippet_collapses_newlines() -> None:
    class M:
        def start(self): return 8
        def end(self): return 13
    snippet = _make_snippet("hello\n\nworld\nthere", M(), before=10, after=10)
    assert "\n" not in snippet
    # The matched region "world" must be in there.
    assert "world" in snippet


def test_make_snippet_marks_truncation() -> None:
    text = "x" * 200 + "MATCH" + "y" * 200
    class M:
        def start(self): return 200
        def end(self): return 205
    snippet = _make_snippet(text, M(), before=20, after=20)
    assert snippet.startswith("…")
    assert snippet.endswith("…")
    assert "MATCH" in snippet


# ----- corrupt-line tolerance ---------------------------------------------


def test_search_skips_malformed_lines(tmp_path: Path) -> None:
    day = datetime.now().strftime("%Y-%m-%d")
    f = tmp_path / day / "s.jsonl"
    f.parent.mkdir(parents=True)
    f.write_text(
        json.dumps({"session": "s", "ts": time.time(), "role": "user", "content": "match"})
        + "\nthis is not json\n",
        encoding="utf-8",
    )
    hits = collect("match", root=tmp_path)
    assert len(hits) == 1
