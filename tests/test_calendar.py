"""Tests for evi/calendar.py — store, iCal parsing, CalDAV gating, formatting."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest

pytest.importorskip("icalendar")

from evi.calendar import (
    CalendarError,
    CalendarStore,
    Event,
    Source,
    _event_sort_key,
    _parse_ical_bytes,
    days_window,
    format_events,
    read_all,
    read_caldav,
    read_ical,
    today_window,
    week_window,
)


# ----- CalendarStore -------------------------------------------------------


def test_store_load_missing_returns_empty(tmp_path: Path) -> None:
    assert CalendarStore(path=tmp_path / "missing.json").load() == []


def test_store_round_trip(tmp_path: Path) -> None:
    store = CalendarStore(path=tmp_path / "c.json")
    src = Source(name="personal", kind="ical", url="https://example.com/p.ics")
    store.save([src])
    assert store.load() == [src]


def test_store_load_skips_invalid_entries(tmp_path: Path) -> None:
    path = tmp_path / "c.json"
    path.write_text(json.dumps({
        "sources": [
            {"name": "ok", "kind": "ical", "url": "https://x/y.ics"},
            {"name": "", "kind": "ical", "url": "https://x/y.ics"},   # missing name
            {"name": "no-url", "kind": "ical"},                       # missing url
            {"name": "bad-kind", "kind": "exchange", "url": "u"},     # unknown kind
            "not-a-dict",
        ]
    }))
    loaded = CalendarStore(path=path).load()
    assert [s.name for s in loaded] == ["ok"]


def test_store_add_existing_without_overwrite_returns_false(tmp_path: Path) -> None:
    store = CalendarStore(path=tmp_path / "c.json")
    assert store.add(Source(name="a", url="u1"))
    assert not store.add(Source(name="a", url="u2"))


def test_store_add_overwrite(tmp_path: Path) -> None:
    store = CalendarStore(path=tmp_path / "c.json")
    store.add(Source(name="a", url="u1"))
    assert store.add(Source(name="a", url="u2"), overwrite=True)
    assert store.load()[0].url == "u2"


def test_store_remove(tmp_path: Path) -> None:
    store = CalendarStore(path=tmp_path / "c.json")
    store.add(Source(name="a", url="u"))
    assert store.remove("a")
    assert not store.remove("missing")


# ----- iCal parsing --------------------------------------------------------


_SAMPLE_ICS = b"""BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Test//Test//EN
BEGIN:VEVENT
UID:event-1@test
SUMMARY:Team standup
DTSTART:20260527T090000Z
DTEND:20260527T093000Z
LOCATION:Zoom
DESCRIPTION:Daily sync
END:VEVENT
BEGIN:VEVENT
UID:event-2@test
SUMMARY:All-hands
DTSTART:20260527T140000Z
DTEND:20260527T150000Z
END:VEVENT
BEGIN:VEVENT
UID:event-3@test
SUMMARY:Quarterly review
DTSTART;VALUE=DATE:20260528
DTEND;VALUE=DATE:20260529
END:VEVENT
END:VCALENDAR
"""


def test_parse_ical_extracts_timed_and_allday() -> None:
    start = datetime(2026, 5, 27, tzinfo=timezone.utc)
    end = start + timedelta(days=3)
    events = _parse_ical_bytes(_SAMPLE_ICS, "personal", start, end)
    assert len(events) == 3
    summaries = sorted(ev.summary for ev in events)
    assert summaries == ["All-hands", "Quarterly review", "Team standup"]
    allday = [ev for ev in events if ev.all_day]
    assert len(allday) == 1
    assert allday[0].summary == "Quarterly review"


def test_parse_ical_respects_window() -> None:
    start = datetime(2026, 5, 28, tzinfo=timezone.utc)
    end = start + timedelta(days=1)
    events = _parse_ical_bytes(_SAMPLE_ICS, "personal", start, end)
    # Only the all-day on the 28th is in this window.
    assert [ev.summary for ev in events] == ["Quarterly review"]


def test_parse_ical_carries_location_and_description() -> None:
    start = datetime(2026, 5, 27, tzinfo=timezone.utc)
    end = start + timedelta(days=1)
    events = _parse_ical_bytes(_SAMPLE_ICS, "personal", start, end)
    standup = next(ev for ev in events if ev.summary == "Team standup")
    assert standup.location == "Zoom"
    assert standup.description == "Daily sync"


# ----- read_ical: HTTP plumbing -------------------------------------------


def test_read_ical_fetches_and_parses(tmp_path: Path) -> None:
    """Stand up a MockTransport that returns the sample .ics."""
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/cal.ics"
        return httpx.Response(200, content=_SAMPLE_ICS)

    transport = httpx.MockTransport(handler)
    src = Source(name="personal", kind="ical", url="https://example.com/cal.ics")
    start = datetime(2026, 5, 27, tzinfo=timezone.utc)
    end = start + timedelta(days=2)
    events = read_ical(src, start=start, end=end, transport=transport)
    assert len(events) >= 2


def test_read_ical_network_failure_raises() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, content=b"server error")

    transport = httpx.MockTransport(handler)
    src = Source(name="personal", kind="ical", url="https://example.com/cal.ics")
    start = datetime(2026, 5, 27, tzinfo=timezone.utc)
    end = start + timedelta(days=1)
    with pytest.raises(CalendarError):
        read_ical(src, start=start, end=end, transport=transport)


def test_read_ical_empty_url_raises() -> None:
    src = Source(name="personal", kind="ical", url="")
    with pytest.raises(CalendarError, match="empty url"):
        read_ical(
            src,
            start=datetime(2026, 5, 27, tzinfo=timezone.utc),
            end=datetime(2026, 5, 28, tzinfo=timezone.utc),
        )


# ----- CalDAV gating -------------------------------------------------------


def test_caldav_missing_password_env_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("EVI_CAL_TEST_PW", raising=False)
    src = Source(
        name="work",
        kind="caldav",
        url="https://caldav.example.com/",
        username="alice",
        password_env="EVI_CAL_TEST_PW",
    )
    with pytest.raises(CalendarError, match="not set"):
        read_caldav(
            src,
            start=datetime(2026, 5, 27, tzinfo=timezone.utc),
            end=datetime(2026, 5, 28, tzinfo=timezone.utc),
        )


def test_caldav_username_without_env_raises() -> None:
    src = Source(
        name="work",
        kind="caldav",
        url="https://caldav.example.com/",
        username="alice",
        password_env="",
    )
    with pytest.raises(CalendarError, match="password_env"):
        read_caldav(
            src,
            start=datetime(2026, 5, 27, tzinfo=timezone.utc),
            end=datetime(2026, 5, 28, tzinfo=timezone.utc),
        )


def test_caldav_connect_failure_wrapped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EVI_CAL_TEST_PW", "secret")
    src = Source(
        name="work",
        kind="caldav",
        url="https://caldav.example.com/",
        username="alice",
        password_env="EVI_CAL_TEST_PW",
    )
    with patch("caldav.DAVClient") as mocked:
        mocked.return_value.principal.side_effect = RuntimeError("net down")
        with pytest.raises(CalendarError, match="CalDAV connect failed"):
            read_caldav(
                src,
                start=datetime(2026, 5, 27, tzinfo=timezone.utc),
                end=datetime(2026, 5, 28, tzinfo=timezone.utc),
            )


# ----- read_all dispatch + error tolerance ---------------------------------


def test_read_all_collects_errors_without_failing() -> None:
    """One bad source shouldn't black out the others."""
    def good_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=_SAMPLE_ICS)

    transport = httpx.MockTransport(good_handler)
    good = Source(name="g", kind="ical", url="https://x/good.ics")
    bad_kind = Source(name="b", kind="weird", url="x")

    with patch("evi.calendar.read_ical") as reader:
        # Make the real reader hit our MockTransport.
        def shim(src, start, end, **_):
            return read_ical(src, start=start, end=end, transport=transport)
        reader.side_effect = shim
        events, errors = read_all(
            [good, bad_kind],
            start=datetime(2026, 5, 27, tzinfo=timezone.utc),
            end=datetime(2026, 5, 30, tzinfo=timezone.utc),
        )
    assert events  # the good source produced events
    assert any("unknown kind" in e for e in errors)


# ----- windowing + sorting + formatting -----------------------------------


def test_today_window_is_midnight_to_midnight() -> None:
    now = datetime(2026, 5, 27, 14, 30, tzinfo=timezone.utc)
    start, end = today_window(now)
    assert start == datetime(2026, 5, 27, tzinfo=timezone.utc)
    assert end == datetime(2026, 5, 28, tzinfo=timezone.utc)


def test_week_window_is_7_days() -> None:
    now = datetime(2026, 5, 27, tzinfo=timezone.utc)
    start, end = week_window(now)
    assert (end - start).days == 7


def test_days_window_clamps_to_one() -> None:
    now = datetime(2026, 5, 27, tzinfo=timezone.utc)
    start, end = days_window(0, now)
    assert (end - start).days == 1


def test_event_sort_key_allday_first_then_by_time() -> None:
    """All-day events for date D sort BEFORE timed events on D."""
    from datetime import date

    timed = Event(
        source="x",
        summary="late",
        start=datetime(2026, 5, 27, 23, tzinfo=timezone.utc),
        end=datetime(2026, 5, 27, 23, 30, tzinfo=timezone.utc),
    )
    allday = Event(
        source="x", summary="all", start=date(2026, 5, 27), end=date(2026, 5, 28),
        all_day=True,
    )
    keys = sorted([timed, allday], key=_event_sort_key)
    assert keys[0] is allday
    assert keys[1] is timed


def test_format_events_groups_by_day() -> None:
    events = [
        Event(
            source="personal",
            summary="Standup",
            start=datetime(2026, 5, 27, 9, 0, tzinfo=timezone.utc),
            end=datetime(2026, 5, 27, 9, 30, tzinfo=timezone.utc),
            location="Zoom",
        ),
    ]
    out = format_events(events)
    assert "Standup" in out
    assert "[personal]" in out
    assert "Zoom" in out


def test_format_events_empty_message() -> None:
    assert "no events" in format_events([])


def test_format_events_truncates_at_limit() -> None:
    events = [
        Event(
            source="x",
            summary=f"evt {i}",
            start=datetime(2026, 5, 27, 9, 0, tzinfo=timezone.utc) + timedelta(minutes=i),
            end=datetime(2026, 5, 27, 9, 30, tzinfo=timezone.utc) + timedelta(minutes=i),
        )
        for i in range(60)
    ]
    out = format_events(events, limit=10)
    assert "and 50 more" in out
