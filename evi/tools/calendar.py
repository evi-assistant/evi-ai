"""Calendar tools — surface configured iCal + CalDAV sources to the LLM.

All four tools share a common shape:

1. Load the source list from `~/.evi/calendars.json`.
2. Filter by `source=` if the caller specified one.
3. Compute the time window for the query.
4. Call `evi.calendar.read_all` to fetch + parse.
5. Render to human-readable text the model can quote back to the user.

Errors from individual sources are kept inline (rendered as
`[source] ERROR: ...` lines) so a single broken feed doesn't blank out
the whole tool call.
"""

from __future__ import annotations

from evi.calendar import (
    CalendarStore,
    days_window,
    format_events,
    read_all,
    today_window,
    week_window,
)
from evi.tools.base import tool


def _resolve_sources(name_filter: str = ""):
    store = CalendarStore()
    sources = store.load()
    if name_filter.strip():
        sources = [s for s in sources if s.name == name_filter.strip()]
    return sources


def _format(events, errors) -> str:
    parts: list[str] = []
    if events:
        parts.append(format_events(events))
    if errors:
        if parts:
            parts.append("")
        parts.append("Errors:")
        for err in errors:
            parts.append(f"  - {err}")
    return "\n".join(parts) if parts else "(no events in this window)"


@tool(
    name="calendar_today",
    description=(
        "List today's calendar events from configured iCal / CalDAV sources. "
        "Pass `source` to restrict to one named source; omit for all."
    ),
    category="calendar",
)
def calendar_today(source: str = "") -> str:
    sources = _resolve_sources(source)
    if not sources:
        return (
            "(no calendar sources configured. Add one with "
            "`evi calendar add <name> --url <ical-url>`)"
        )
    start, end = today_window()
    events, errors = read_all(sources, start=start, end=end)
    return _format(events, errors)


@tool(
    name="calendar_week",
    description=(
        "List the next 7 days of calendar events from configured sources. "
        "Pass `source` to restrict to one named source."
    ),
    category="calendar",
)
def calendar_week(source: str = "") -> str:
    sources = _resolve_sources(source)
    if not sources:
        return "(no calendar sources configured)"
    start, end = week_window()
    events, errors = read_all(sources, start=start, end=end)
    return _format(events, errors)


@tool(
    name="calendar_search",
    description=(
        "Search upcoming calendar events for a substring in the summary "
        "or location. Case-insensitive. `days` caps the lookahead window "
        "(default 30). `source` restricts to one named source."
    ),
    category="calendar",
)
def calendar_search(query: str, days: int = 30, source: str = "") -> str:
    needle = query.strip().lower()
    if not needle:
        return "(empty query — pass something to search for)"
    sources = _resolve_sources(source)
    if not sources:
        return "(no calendar sources configured)"
    start, end = days_window(days)
    events, errors = read_all(sources, start=start, end=end)
    hits = [
        ev for ev in events
        if needle in ev.summary.lower() or needle in ev.location.lower()
    ]
    if not hits and not errors:
        return f"(no events matching {query!r} in the next {days} days)"
    return _format(hits, errors)


@tool(
    name="calendar_next",
    description=(
        "Return the next upcoming calendar event from configured sources. "
        "Looks up to 30 days out. Pass `source` to restrict to one."
    ),
    category="calendar",
)
def calendar_next(source: str = "") -> str:
    sources = _resolve_sources(source)
    if not sources:
        return "(no calendar sources configured)"
    start, end = days_window(30)
    events, errors = read_all(sources, start=start, end=end)
    if not events:
        if errors:
            return "(no events found)\n\nErrors:\n  - " + "\n  - ".join(errors)
        return "(no events in the next 30 days)"
    return format_events(events[:1])
