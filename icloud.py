"""iCloud calendar operations over CalDAV.

Times cross this module's boundary as ISO 8601 strings. A date-only string
('2026-11-13') means an all-day event; anything else is a timed event.
"""

import functools
import logging
import os
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import caldav
import icalendar

ICLOUD_URL = 'https://caldav.icloud.com'
PRODID = '-//icloud-calendar-mcp//EN'
MAX_PARALLEL_REQUESTS = 8

# caldav logs event contents when it repairs malformed data from the server.
# Keep calendar data out of the logs.
logging.getLogger('caldav').setLevel(logging.ERROR)


def list_calendars() -> list[str]:
    """Return the names of the calendars that can hold events."""
    calendars = _principal().get_calendars()
    components = _per_calendar(
        lambda calendar: calendar.get_supported_components(),
        calendars
    )
    return [
        calendar.get_display_name()
        for calendar, supported in zip(calendars, components)
        if 'VEVENT' in supported
    ]


def list_events(
    start: str,
    end: str,
    calendar: str | None = None,
    query: str | None = None,
    timezone: str | None = None
) -> list[dict]:
    """Return events overlapping [start, end), earliest first.

    Recurring events are expanded into one entry per occurrence.
    """
    range_start = _parse_bound(start, timezone)
    range_end = _parse_bound(end, timezone)
    if range_end <= range_start:
        raise ValueError("end must be after start.")

    if calendar:
        calendars = [_find_calendar(calendar)]
    else:
        calendars = _principal().get_calendars()

    def search(cal: caldav.Calendar) -> list[caldav.Event]:
        return cal.search(
            event=True,
            start=range_start,
            end=range_end,
            expand=True
        )

    found = []
    for cal, results in zip(calendars, _per_calendar(search, calendars)):
        name = cal.get_display_name()
        for result in results:
            component = result.component
            if query and not _matches(component, query):
                continue
            entry = _event_to_dict(component, name, timezone)
            found.append((_sort_key(component.start), entry))

    found.sort(key=lambda pair: pair[0])
    return [entry for _, entry in found]


def create_event(
    calendar: str,
    summary: str,
    start: str,
    end: str | None = None,
    timezone: str | None = None,
    location: str | None = None,
    description: str | None = None,
    rrule: str | None = None
) -> dict:
    """Create an event and return it."""
    cal = _find_calendar(calendar)
    dtstart = _parse_time(start, timezone)
    if end:
        dtend = _parse_time(end, timezone)
    else:
        dtend = dtstart + _default_duration(dtstart)
    _check_span(dtstart, dtend)

    event = icalendar.Event.new(
        uid=str(uuid.uuid4()),
        summary=summary,
        start=dtstart,
        end=dtend,
        location=location or None,
        description=description or None
    )
    if rrule:
        event.add('rrule', _parse_rrule(rrule))

    cal.add_event(ical=_wrap(event).to_ical().decode())
    return _event_to_dict(event, cal.get_display_name(), timezone)


def update_event(
    calendar: str,
    uid: str,
    summary: str | None = None,
    start: str | None = None,
    end: str | None = None,
    timezone: str | None = None,
    location: str | None = None,
    description: str | None = None
) -> dict:
    """Change the given fields of an event and return the result.

    Fields left as None are untouched; an empty string clears location or
    description. A recurring event is changed as a whole series.
    """
    cal = _find_calendar(calendar)
    event = _find_event(cal, uid)
    with event.edit_icalendar_instance() as ical:
        component = _master(ical)
        if start is not None or end is not None:
            _reschedule(component, start, end, timezone)
        if summary is not None:
            _set_text(component, 'SUMMARY', summary)
        if location is not None:
            _set_text(component, 'LOCATION', location)
        if description is not None:
            _set_text(component, 'DESCRIPTION', description)
        ical.add_missing_timezones()
    event.save()
    return _event_to_dict(component, cal.get_display_name(), timezone)


def delete_event(calendar: str, uid: str) -> dict:
    """Delete an event (the whole series, if it recurs)."""
    cal = _find_calendar(calendar)
    _find_event(cal, uid).delete()
    return {'deleted': uid}


# --- iCloud lookups ---------------------------------------------------------

@functools.cache
def _principal() -> caldav.Principal:
    """Connect to iCloud once per process and reuse the connection."""
    client = caldav.DAVClient(
        url=ICLOUD_URL,
        username=os.environ['ICLOUD_USERNAME'],
        password=os.environ['ICLOUD_APP_PASSWORD'],
        features='icloud'
    )
    return client.get_principal()


def _per_calendar(function, calendars: list[caldav.Calendar]) -> list:
    """Call function once per calendar, concurrently, keeping order.

    Each call is a separate request to iCloud, so running them one after
    another makes every tool call slower with each calendar on the account.
    """
    with ThreadPoolExecutor(max_workers=MAX_PARALLEL_REQUESTS) as pool:
        return list(pool.map(function, calendars))


def _find_calendar(name: str) -> caldav.Calendar:
    calendars = _principal().get_calendars()
    names = [calendar.get_display_name() or '' for calendar in calendars]
    pairs = list(zip(calendars, names))
    exact = [cal for cal, cal_name in pairs if cal_name == name]
    loose = [
        cal
        for cal, cal_name in pairs
        if cal_name.casefold() == name.casefold()
    ]
    for matches in (exact, loose):
        if len(matches) == 1:
            return matches[0]
    message = (
        f"No single calendar named {name!r}. "
        f"Available calendars: {', '.join(names)}."
    )
    raise ValueError(message)


def _find_event(calendar: caldav.Calendar, uid: str) -> caldav.Event:
    try:
        return calendar.get_event_by_uid(uid)
    except caldav.error.NotFoundError:
        message = (
            f"No event with UID {uid!r} in calendar "
            f"{calendar.get_display_name()!r}."
        )
        raise ValueError(message) from None


def _master(ical: icalendar.Calendar) -> icalendar.Event:
    """Return the event itself, not a per-occurrence override of it."""
    for component in ical.events:
        if 'RECURRENCE-ID' not in component:
            return component
    raise ValueError("Event has no editable master component.")


# --- Editing ----------------------------------------------------------------

def _wrap(event: icalendar.Event) -> icalendar.Calendar:
    """Put a new event in the calendar object that gets stored."""
    ical = icalendar.Calendar.new(prodid=PRODID, subcomponents=[event])
    # icalendar gives the wrapper a UID of its own. caldav then mistakes it
    # for the event's, and the event can't be found by UID afterwards.
    ical.pop('UID', None)
    ical.add_missing_timezones()
    return ical


def _reschedule(
    component: icalendar.Event,
    start: str | None,
    end: str | None,
    timezone: str | None
) -> None:
    """Move an event, keeping its duration when only start is given."""
    duration = component.end - component.start
    if start is not None:
        dtstart = _parse_time(start, timezone)
    else:
        dtstart = component.start
    if end is not None:
        dtend = _parse_time(end, timezone)
    else:
        dtend = dtstart + duration
    _check_span(dtstart, dtend)

    for name in ('DTSTART', 'DTEND', 'DURATION'):
        component.pop(name, None)
    component.add('dtstart', dtstart)
    component.add('dtend', dtend)


def _set_text(component: icalendar.Event, name: str, value: str) -> None:
    component.pop(name, None)
    if value:
        component.add(name, value)


def _check_span(start: date | datetime, end: date | datetime) -> None:
    if isinstance(start, datetime) != isinstance(end, datetime):
        message = (
            "start and end must both be dates (all-day) "
            "or both be date-times."
        )
        raise ValueError(message)
    if end <= start:
        raise ValueError("end must be after start.")


def _default_duration(start: date | datetime) -> timedelta:
    if isinstance(start, datetime):
        return timedelta(hours=1)
    return timedelta(days=1)


def _parse_rrule(rrule: str) -> icalendar.vRecur:
    try:
        rule = icalendar.vRecur.from_ical(rrule.removeprefix('RRULE:'))
    except ValueError:
        rule = {}
    # icalendar parses leniently, so require the one mandatory part.
    if 'FREQ' not in rule:
        message = (
            f"Invalid rrule {rrule!r}. Expected an RFC 5545 rule "
            "such as 'FREQ=WEEKLY;BYDAY=MO,WE'."
        )
        raise ValueError(message)
    return rule


# --- Time parsing -----------------------------------------------------------

def _parse_bound(value: str, timezone: str | None) -> datetime:
    """Parse a search-range bound; a bare date means its midnight."""
    moment = _parse_time(value, timezone)
    if isinstance(moment, datetime):
        return moment
    return datetime.combine(moment, time.min, _zone(timezone) or UTC)


def _parse_time(value: str, timezone: str | None) -> date | datetime:
    """Parse an ISO 8601 string into a date (all-day) or aware datetime.

    A date-time with a named timezone keeps it, so recurring events follow
    daylight saving. One with only a UTC offset is stored in UTC.
    """
    try:
        if len(value) == 10:
            return date.fromisoformat(value)
        moment = datetime.fromisoformat(value)
    except ValueError:
        message = (
            f"Invalid date or date-time {value!r}. Use ISO 8601, such "
            "as '2026-11-13' or '2026-11-13T15:00:00'."
        )
        raise ValueError(message) from None

    zone = _zone(timezone)
    if moment.tzinfo is not None:
        return moment.astimezone(zone or UTC)
    if zone is None:
        message = (
            f"{value!r} has no UTC offset. Add one, or pass timezone "
            "as an IANA name such as 'America/New_York'."
        )
        raise ValueError(message)
    return moment.replace(tzinfo=zone)


def _zone(timezone: str | None) -> ZoneInfo | None:
    if not timezone:
        return None
    try:
        return ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError):
        message = (
            f"Unknown timezone {timezone!r}. Use an IANA name such as "
            "'America/New_York'."
        )
        raise ValueError(message) from None


# --- Output -----------------------------------------------------------------

def _event_to_dict(
    component: icalendar.Event,
    calendar: str,
    timezone: str | None
) -> dict:
    start, end = component.start, component.end
    all_day = not isinstance(start, datetime)
    zone = _zone(timezone)
    if zone and not all_day and start.tzinfo is not None:
        start, end = start.astimezone(zone), end.astimezone(zone)

    entry = {
        'uid': str(component.get('UID', '')),
        'calendar': calendar,
        'summary': str(component.get('SUMMARY', '')),
        'start': start.isoformat(),
        'end': end.isoformat(),
        'all_day': all_day,
    }
    for key in ('location', 'description'):
        value = str(component.get(key.upper(), ''))
        if value:
            entry[key] = value
    if 'RRULE' in component or 'RECURRENCE-ID' in component:
        entry['recurring'] = True
    return entry


def _matches(component: icalendar.Event, query: str) -> bool:
    text = ' '.join(
        str(component.get(name, ''))
        for name in ('SUMMARY', 'LOCATION', 'DESCRIPTION')
    )
    return query.casefold() in text.casefold()


def _sort_key(start: date | datetime) -> datetime:
    """Make all-day, floating and zoned starts comparable."""
    if not isinstance(start, datetime):
        return datetime.combine(start, time.min, UTC)
    if start.tzinfo is None:
        return start.replace(tzinfo=UTC)
    return start
