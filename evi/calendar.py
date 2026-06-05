"""Calendar reading — iCal URLs and CalDAV with auth.

Two source kinds are supported:

- **iCal URL** (`kind="ical"`): any publicly fetchable `.ics` endpoint.
  Google Calendar's "Secret address in iCal format", iCloud public
  calendars, Outlook publish, Fastmail/Posteo public links, raw `.ics`
  files in cloud storage, etc.
- **CalDAV** (`kind="caldav"`): a CalDAV-speaking server with
  username / password. Covers Nextcloud, Fastmail private, Posteo,
  iCloud private (with app-specific password), Apple iCloud,
  Mailfence, mailbox.org, Synology, ownCloud, Radicale, Baikal.

Both shapes parse to the same `Event` dataclass so the tool layer
doesn't have to care which backend produced it. Recurring events are
expanded into individual occurrences within the requested window
(via `recurring-ical-events` for iCal; CalDAV servers usually expand
server-side, but we still pass them through `_expand_if_recurring` as
belt-and-braces).

Storage: sources live in `~/.evi/calendars.json` (same JSON-file pattern
as routes — our minimal TOML writer doesn't speak arrays-of-tables).
Passwords are NEVER stored in this file. Each CalDAV source carries a
`password_env` name (e.g. `EVI_CAL_WORK_PASSWORD`); the actual secret
must be set in the environment at run time.

Heavy deps (`icalendar`, `caldav`) are lazy-imported. The tool wrappers
surface a clean install-this-extra error when they're missing.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, asdict
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx

from evi.config import HOME


CALENDARS_PATH = HOME / "calendars.json"


class CalendarError(RuntimeError):
    """Raised when a calendar source can't be reached or parsed."""


@dataclass
class Source:
    """One calendar source. `kind` decides which backend reads it.

    - `kind="ical"`: only `url` is used. Public / signed URL.
    - `kind="caldav"`: `url` + `username` + `password_env`. The actual
      password is read from `os.environ[password_env]` at read time.
      `calendar` optionally narrows to one named calendar on the server
      (empty = include events from ALL calendars the user has access to).
    """

    name: str
    kind: str = "ical"        # "ical" | "caldav"
    url: str = ""
    username: str = ""
    password_env: str = ""
    calendar: str = ""        # CalDAV: filter to this calendar name


@dataclass
class Event:
    """One calendar event, normalised from iCal or CalDAV.

    `start` / `end` are timezone-aware datetimes when the event has a
    time; for all-day events they are dates and `all_day=True`.
    """

    source: str               # the configured Source.name
    summary: str
    start: datetime | date
    end: datetime | date
    all_day: bool = False
    location: str = ""
    description: str = ""
    url: str = ""


# --- store --------------------------------------------------------------


class CalendarStore:
    """JSON-on-disk store of `Source` entries."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path) if path is not None else CALENDARS_PATH

    def load(self) -> list[Source]:
        if not self.path.is_file():
            return []
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        raw = data.get("sources", []) if isinstance(data, dict) else []
        out: list[Source] = []
        for entry in raw:
            if not isinstance(entry, dict):
                continue
            name = str(entry.get("name", "")).strip()
            kind = str(entry.get("kind", "ical")).strip().lower() or "ical"
            url = str(entry.get("url", "")).strip()
            if not name or not url:
                continue
            if kind not in ("ical", "caldav"):
                continue
            out.append(
                Source(
                    name=name,
                    kind=kind,
                    url=url,
                    username=str(entry.get("username", "")),
                    password_env=str(entry.get("password_env", "")),
                    calendar=str(entry.get("calendar", "")),
                )
            )
        return out

    def save(self, sources: list[Source]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"sources": [asdict(s) for s in sources]}
        self.path.write_text(
            json.dumps(payload, indent=2) + "\n", encoding="utf-8"
        )

    def add(self, source: Source, *, overwrite: bool = False) -> bool:
        sources = self.load()
        for i, s in enumerate(sources):
            if s.name == source.name:
                if not overwrite:
                    return False
                sources[i] = source
                self.save(sources)
                return True
        sources.append(source)
        self.save(sources)
        return True

    def remove(self, name: str) -> bool:
        sources = self.load()
        kept = [s for s in sources if s.name != name]
        if len(kept) == len(sources):
            return False
        self.save(kept)
        return True


# --- iCal reader --------------------------------------------------------


def _need_icalendar():
    try:
        import icalendar  # noqa: F401
        import recurring_ical_events  # noqa: F401
    except ImportError as exc:
        raise CalendarError(
            "calendar reading needs icalendar + recurring-ical-events — "
            "install with: pip install 'evi-ai[calendar]'"
        ) from exc


def _coerce_dt(value: Any) -> datetime | date:
    """Normalise an icalendar dtstart/dtend value into our schema.

    `value` is either a `datetime`, a `date`, or an `icalendar.prop.vDDDTypes`-
    wrapped one. All-day events are dates; timed events should be tz-aware.
    """
    if hasattr(value, "dt"):
        value = value.dt  # icalendar property wrapper → raw datetime/date
    if isinstance(value, datetime) and value.tzinfo is None:
        # Floating-time iCal value — assume local. Tests run UTC-naive so
        # we attach the local tz lazily.
        value = value.replace(tzinfo=timezone.utc)
    return value


def _utc_window(start: datetime, end: datetime) -> tuple[datetime, datetime]:
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    if end.tzinfo is None:
        end = end.replace(tzinfo=timezone.utc)
    return start, end


def _parse_ical_bytes(
    raw: bytes,
    source_name: str,
    start: datetime,
    end: datetime,
) -> list[Event]:
    """Parse a raw .ics blob into Event objects within [start, end]."""
    _need_icalendar()
    import icalendar
    import recurring_ical_events as rie

    cal = icalendar.Calendar.from_ical(raw)
    start_utc, end_utc = _utc_window(start, end)
    events: list[Event] = []
    for comp in rie.of(cal).between(start_utc, end_utc):
        if comp.name != "VEVENT":
            continue
        summary = str(comp.get("summary") or "").strip()
        dtstart = comp.get("dtstart")
        dtend = comp.get("dtend") or dtstart  # zero-length events legal
        if dtstart is None:
            continue
        s = _coerce_dt(dtstart)
        e = _coerce_dt(dtend) if dtend is not None else s
        all_day = isinstance(s, date) and not isinstance(s, datetime)
        events.append(
            Event(
                source=source_name,
                summary=summary or "(no title)",
                start=s,
                end=e,
                all_day=all_day,
                location=str(comp.get("location") or "").strip(),
                description=str(comp.get("description") or "").strip(),
                url=str(comp.get("url") or "").strip(),
            )
        )
    return events


def read_ical(
    source: Source,
    *,
    start: datetime,
    end: datetime,
    timeout: float = 15.0,
    transport: httpx.BaseTransport | None = None,
) -> list[Event]:
    """Fetch + parse an iCal URL. Network errors raise `CalendarError`."""
    if not source.url:
        raise CalendarError(f"{source.name}: empty url")
    try:
        with httpx.Client(
            timeout=timeout, transport=transport, follow_redirects=True
        ) as client:
            r = client.get(source.url)
            r.raise_for_status()
            raw = r.content
    except httpx.HTTPError as exc:
        raise CalendarError(f"{source.name}: fetch failed: {exc}") from exc
    return _parse_ical_bytes(raw, source.name, start, end)


# --- CalDAV reader ------------------------------------------------------


def _need_caldav():
    try:
        import caldav  # noqa: F401
    except ImportError as exc:
        raise CalendarError(
            "CalDAV reading needs the caldav package — "
            "install with: pip install 'evi-ai[calendar]'"
        ) from exc


def read_caldav(
    source: Source,
    *,
    start: datetime,
    end: datetime,
) -> list[Event]:
    """Connect to a CalDAV server and pull events in [start, end].

    The password is looked up from `os.environ[source.password_env]` at
    call time. Empty env value or missing var raises `CalendarError`.
    """
    _need_caldav()
    import caldav  # noqa: PLC0415

    if not source.url:
        raise CalendarError(f"{source.name}: empty CalDAV url")
    if source.username and not source.password_env:
        raise CalendarError(
            f"{source.name}: username set but password_env is empty"
        )
    password = ""
    if source.password_env:
        password = os.environ.get(source.password_env, "")
        if not password:
            raise CalendarError(
                f"{source.name}: env var {source.password_env!r} is not set"
            )

    try:
        client = caldav.DAVClient(
            url=source.url,
            username=source.username or None,
            password=password or None,
        )
        principal = client.principal()
        calendars = principal.calendars()
    except Exception as exc:  # noqa: BLE001
        raise CalendarError(
            f"{source.name}: CalDAV connect failed: {type(exc).__name__}: {exc}"
        ) from exc

    if source.calendar:
        calendars = [c for c in calendars if str(c.name) == source.calendar]
        if not calendars:
            raise CalendarError(
                f"{source.name}: no calendar named {source.calendar!r}"
            )

    start_utc, end_utc = _utc_window(start, end)
    out: list[Event] = []
    for cal in calendars:
        try:
            results = cal.search(start=start_utc, end=end_utc, event=True, expand=True)
        except Exception:  # noqa: BLE001
            # Don't fail the whole read if one calendar is broken; skip
            # and continue.
            continue
        for ev in results:
            try:
                comp = ev.icalendar_component  # VEVENT
            except Exception:  # noqa: BLE001
                continue
            if comp.name != "VEVENT":
                continue
            summary = str(comp.get("summary") or "").strip()
            dtstart = comp.get("dtstart")
            dtend = comp.get("dtend") or dtstart
            if dtstart is None:
                continue
            s = _coerce_dt(dtstart)
            e = _coerce_dt(dtend) if dtend is not None else s
            all_day = isinstance(s, date) and not isinstance(s, datetime)
            out.append(
                Event(
                    source=source.name,
                    summary=summary or "(no title)",
                    start=s,
                    end=e,
                    all_day=all_day,
                    location=str(comp.get("location") or "").strip(),
                    description=str(comp.get("description") or "").strip(),
                    url=str(comp.get("url") or "").strip(),
                )
            )
    return out


# --- dispatch + windowing helpers ---------------------------------------


def read_events(
    source: Source,
    *,
    start: datetime,
    end: datetime,
) -> list[Event]:
    """Dispatch to the right backend by source.kind."""
    if source.kind == "ical":
        return read_ical(source, start=start, end=end)
    if source.kind == "caldav":
        return read_caldav(source, start=start, end=end)
    raise CalendarError(f"{source.name}: unknown kind {source.kind!r}")


def read_all(
    sources: list[Source],
    *,
    start: datetime,
    end: datetime,
) -> tuple[list[Event], list[str]]:
    """Read all sources, returning (events_sorted, errors).

    Per-source failures are collected into the `errors` list rather than
    raised so a single bad source doesn't black out the whole query. The
    returned events are sorted by start time.
    """
    events: list[Event] = []
    errors: list[str] = []
    for s in sources:
        try:
            events.extend(read_events(s, start=start, end=end))
        except CalendarError as exc:
            errors.append(str(exc))
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{s.name}: {type(exc).__name__}: {exc}")
    events.sort(key=_event_sort_key)
    return events, errors


def _event_sort_key(ev: Event) -> tuple[int, float, str]:
    """Sort events: all-day first within each day, then by start time."""
    if isinstance(ev.start, datetime):
        ts = ev.start.timestamp()
        return (1, ts, ev.summary)
    # all-day — sort to top of the day, encode the date as a timestamp.
    dt = datetime.combine(ev.start, time.min, tzinfo=timezone.utc)
    return (0, dt.timestamp(), ev.summary)


# --- window helpers (used by the tool layer) ----------------------------


def today_window(now: datetime | None = None) -> tuple[datetime, datetime]:
    """`[00:00 today, 00:00 tomorrow)` in UTC."""
    now = now or datetime.now(tz=timezone.utc)
    start = datetime.combine(now.date(), time.min, tzinfo=timezone.utc)
    end = start + timedelta(days=1)
    return start, end


def week_window(now: datetime | None = None) -> tuple[datetime, datetime]:
    """Today through 7 days out."""
    now = now or datetime.now(tz=timezone.utc)
    start = datetime.combine(now.date(), time.min, tzinfo=timezone.utc)
    end = start + timedelta(days=7)
    return start, end


def days_window(days: int, now: datetime | None = None) -> tuple[datetime, datetime]:
    """Today through `days` days out (always at least 1)."""
    now = now or datetime.now(tz=timezone.utc)
    start = datetime.combine(now.date(), time.min, tzinfo=timezone.utc)
    end = start + timedelta(days=max(1, int(days)))
    return start, end


def format_events(events: list[Event], *, limit: int = 50) -> str:
    """Human-readable text rendering for tool / CLI output.

    Output groups by day, lists `HH:MM-HH:MM Summary [@ location]` per
    timed event and `(all day) Summary` for all-day events.
    """
    if not events:
        return "(no events in this window)"
    lines: list[str] = []
    last_date: date | None = None
    for ev in events[:limit]:
        ev_date = ev.start.date() if isinstance(ev.start, datetime) else ev.start
        if ev_date != last_date:
            lines.append(f"\n{ev_date.strftime('%a %Y-%m-%d')}")
            last_date = ev_date
        if ev.all_day:
            time_part = "(all day)"
        else:
            s_local = _to_local(ev.start)  # type: ignore[arg-type]
            if isinstance(ev.end, datetime):
                e_local = _to_local(ev.end)
                time_part = f"{s_local:%H:%M}-{e_local:%H:%M}"
            else:
                time_part = f"{s_local:%H:%M}"
        loc = f" @ {ev.location}" if ev.location else ""
        lines.append(f"  [{ev.source}] {time_part}  {ev.summary}{loc}")
    if len(events) > limit:
        lines.append(f"\n… and {len(events) - limit} more")
    return "\n".join(line.lstrip() if line.startswith("\n") else line for line in lines).strip()


def _to_local(dt: datetime) -> datetime:
    """Convert to local time; tz-naive values are assumed UTC."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone()
