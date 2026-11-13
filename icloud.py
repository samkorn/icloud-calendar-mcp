"""iCloud calendar operations over CalDAV.

Times cross this module's boundary as ISO 8601 strings. A date-only string
('2026-11-13') means an all-day event; anything else is a timed event.

Failures the caller can act on are raised as CalendarError, whose message
never contains calendar data, the caller's input or anything iCloud sent
back. `safe_message` gives the same guarantee for errors from the libraries
underneath.
"""

import contextlib
import functools
import logging
import math
import os
import time as clock
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import caldav
import icalendar
import icalendar_searcher

ICLOUD_URL = 'https://caldav.icloud.com'
PRODID = '-//icloud-calendar-mcp//EN'

# Seconds iCloud may stay silent on one request before it is abandoned.
REQUEST_TIMEOUT = 20
# Seconds one tool call may take before it stops starting new work.
OPERATION_SECONDS = 45
# iCloud requests in flight at once, across every tool call in the process.
MAX_PARALLEL_REQUESTS = 8

# The widest range one list_events call may cover.
MAX_RANGE_DAYS = 366
# The most events (after expanding recurrences) one call may produce.
MAX_EVENTS = 1000
# The most event text (in characters) one call may return.
MAX_RESPONSE_CHARS = 300_000
# The most steps a stored recurrence rule may cost to expand. The library
# walks a rule from the event's first date, one step per FREQ interval, and
# can't be interrupted, so the cost is estimated before it starts.
MAX_RULE_STEPS = 100_000

TEXT_LIMITS = {
    'calendar': 255,
    'uid': 255,
    'summary': 1000,
    'location': 1000,
    'description': 10_000,
    'query': 255,
    'rrule': 500,
    'start': 40,
    'end': 40,
    'timezone': 64,
}

# Recurrence rules this server will create. Stored rules from other apps
# may use the rest of RFC 5545; those are only bounded (see _too_dense).
RRULE_FREQUENCIES = {'DAILY', 'WEEKLY', 'MONTHLY', 'YEARLY'}
RRULE_NUMBER_RANGES = {
    'INTERVAL': (1, 1000),
    'COUNT': (1, 10_000),
    'BYMONTH': (1, 12),
    'BYMONTHDAY': (-31, 31),
    'BYSETPOS': (-366, 366),
}
RRULE_OTHER_PARTS = {'FREQ', 'UNTIL', 'BYDAY', 'WKST'}
FREQUENCY_SECONDS = {
    'SECONDLY': 1,
    'MINUTELY': 60,
    'HOURLY': 3600,
    'DAILY': 86_400,
    'WEEKLY': 7 * 86_400,
    'MONTHLY': 28 * 86_400,
    'YEARLY': 365 * 86_400,
}

TOO_SLOW = "iCloud took too long to answer. Try again, or ask for less."
UNCONFIRMED = (
    "iCloud did not confirm the change, so it may or may not have been "
    "saved. Check with list_events before trying again."
)
CONFLICT = (
    "The event was changed by someone else in the meantime, so nothing "
    "was done. Look at it again with list_events before retrying."
)

# caldav logs event contents when it repairs malformed data from the server.
# Keep calendar data out of the logs.
logging.getLogger('caldav').setLevel(logging.ERROR)

# One pool for the whole process, so simultaneous tool calls share the
# request budget instead of each bringing its own threads.
_pool = ThreadPoolExecutor(
    max_workers=MAX_PARALLEL_REQUESTS,
    thread_name_prefix='icloud'
)


class CalendarError(Exception):
    """A failure whose message is safe to show the caller and to log."""


def list_calendars() -> list[str]:
    """Return the names of the calendars that can hold events."""
    deadline = _deadline()
    calendars = _principal().get_calendars()
    components = _per_calendar(
        lambda calendar: calendar.get_supported_components(),
        calendars,
        deadline
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

    Recurring events are expanded into one entry per occurrence, except a
    series too dense to expand, which is listed once and marked with
    'occurrences_omitted'. Raises CalendarError rather than return a
    partial list when the range holds more than MAX_EVENTS.
    """
    _check_lengths(
        start=start,
        end=end,
        calendar=calendar,
        query=query,
        timezone=timezone
    )
    deadline = _deadline()
    range_start = _parse_bound(start, timezone)
    range_end = _parse_bound(end, timezone)
    if range_end <= range_start:
        raise CalendarError("end must be after start.")
    if range_end - range_start > timedelta(days=MAX_RANGE_DAYS):
        message = (
            f"The range is longer than {MAX_RANGE_DAYS} days. Search a "
            "longer period in several calls."
        )
        raise CalendarError(message)

    if calendar:
        calendars = [_find_calendar(calendar)]
    else:
        calendars = _principal().get_calendars()

    def search(cal: caldav.Calendar) -> list[caldav.Event]:
        # Neither expanded nor filtered by caldav: both would expand every
        # recurrence with no limit. _occurrences does it within a budget.
        # The extra day each side covers all-day and floating events,
        # which the server can't place in the caller's timezone.
        return cal.search(
            event=True,
            start=range_start - timedelta(days=1),
            end=range_end + timedelta(days=1),
            expand=False,
            post_filter=False
        )

    found = []
    count = 0
    size = 0
    for cal, results in zip(
        calendars,
        _per_calendar(search, calendars, deadline)
    ):
        name = cal.get_display_name()
        for result in results:
            _check_deadline(deadline)
            ical = result.icalendar_instance
            if query and not _any_match(ical.events, query):
                continue
            components, complete = _occurrences(ical, range_start, range_end)
            count += len(components)
            if count > MAX_EVENTS:
                message = (
                    f"More than {MAX_EVENTS} events in that range. Use a "
                    "shorter range, name a calendar, or add a query."
                )
                raise CalendarError(message)
            for component in components:
                if query and not _matches(component, query):
                    continue
                entry = _event_to_dict(component, name, timezone)
                if not complete:
                    entry['occurrences_omitted'] = True
                size += sum(len(str(value)) for value in entry.values())
                found.append((_sort_key(component.start), entry))

    if size > MAX_RESPONSE_CHARS:
        message = (
            "The events in that range hold too much text to return at "
            "once. Use a shorter range, name a calendar, or add a query."
        )
        raise CalendarError(message)
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
    _check_lengths(
        calendar=calendar,
        summary=summary,
        start=start,
        end=end,
        timezone=timezone,
        location=location,
        description=description,
        rrule=rrule
    )
    # Everything that can be wrong with the arguments is found here,
    # before the write: an error afterwards would invite a retry and a
    # duplicate event.
    _zone(timezone)
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
    data = _wrap(event).to_ical().decode()

    cal = _find_calendar(calendar)
    created = _event_to_dict(event, cal.get_display_name(), timezone)
    with _writing():
        cal.add_event(ical=data)
    return created


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
    _check_lengths(
        calendar=calendar,
        uid=uid,
        summary=summary,
        start=start,
        end=end,
        timezone=timezone,
        location=location,
        description=description
    )
    _zone(timezone)
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
    updated = _event_to_dict(component, cal.get_display_name(), timezone)
    # caldav sends the version it fetched with the save, so iCloud refuses
    # it if the event changed in between.
    with _writing():
        event.save()
    return updated


def delete_event(calendar: str, uid: str) -> dict:
    """Delete an event (the whole series, if it recurs)."""
    _check_lengths(calendar=calendar, uid=uid)
    cal = _find_calendar(calendar)
    event = _find_event(cal, uid)
    with _writing():
        try:
            # Only the version that was just looked up: if another client
            # changed the event since, iCloud refuses and nothing is lost.
            event.delete(if_match=True, precondition='etag')
        except ValueError:
            # caldav has no usable version to send. Don't fall back to
            # deleting whatever is there.
            message = (
                "iCloud gave no version for this event, so it can't be "
                "deleted safely."
            )
            raise CalendarError(message) from None
    return {'deleted': uid}


def safe_message(error: Exception) -> str | None:
    """Describe a failure without leaking anything, or return None.

    None means the failure is not a kind this module knows; the caller
    should report it without details. Never use str(error) instead: caldav
    errors carry URLs and whatever iCloud answered.
    """
    if isinstance(error, CalendarError):
        return str(error)
    mismatches = (
        caldav.error.ETagMismatchError,
        caldav.error.ScheduleTagMismatchError,
    )
    if isinstance(error, mismatches):
        return CONFLICT
    if isinstance(error, caldav.error.RateLimitError):
        return "iCloud is limiting requests right now. Try again later."
    if isinstance(error, caldav.error.AuthorizationError):
        return "iCloud refused the request: check the credentials."
    if isinstance(error, caldav.error.NotFoundError):
        return "iCloud could not find that item. It may have been deleted."
    if isinstance(error, caldav.error.DAVError):
        return "iCloud answered with an error."
    # niquests raises OSError subclasses for timeouts and lost connections.
    if isinstance(error, OSError):
        return "Could not reach iCloud, or it stopped answering."
    return None


# --- iCloud lookups ---------------------------------------------------------

@functools.cache
def _principal() -> caldav.Principal:
    """Connect to iCloud once per process and reuse the connection."""
    client = caldav.DAVClient(
        url=ICLOUD_URL,
        username=os.environ['ICLOUD_USERNAME'],
        password=os.environ['ICLOUD_APP_PASSWORD'],
        features='icloud',
        timeout=REQUEST_TIMEOUT,
        # A 429 or 503 fails at once rather than sleeping for as long as
        # the response asks.
        rate_limit_handle=False
    )
    return client.get_principal()


def _per_calendar(
    function,
    calendars: list[caldav.Calendar],
    deadline: float
) -> list:
    """Call function once per calendar, concurrently, keeping order.

    Each call is a separate request to iCloud, so running them one after
    another makes every tool call slower with each calendar on the account.
    """
    futures = [_pool.submit(function, calendar) for calendar in calendars]
    try:
        return [
            future.result(timeout=max(0, deadline - clock.monotonic()))
            for future in futures
        ]
    except TimeoutError:
        raise CalendarError(TOO_SLOW) from None
    finally:
        # Drops the calls still queued. One already talking to iCloud runs
        # on until REQUEST_TIMEOUT stops it.
        for future in futures:
            future.cancel()


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
        "No single calendar has that name. Use list_calendars to see "
        "the names."
    )
    raise CalendarError(message)


def _find_event(calendar: caldav.Calendar, uid: str) -> caldav.Event:
    try:
        return calendar.get_event_by_uid(uid)
    except caldav.error.NotFoundError:
        message = (
            "No event with that UID in that calendar. Use list_events to "
            "find it."
        )
        raise CalendarError(message) from None


def _master(ical: icalendar.Calendar) -> icalendar.Event:
    """Return the event itself, not a per-occurrence override of it."""
    for component in ical.events:
        if 'RECURRENCE-ID' not in component:
            return component
    raise CalendarError("Event has no editable master component.")


# --- Limits -----------------------------------------------------------------

def _deadline() -> float:
    """Return the moment the current tool call runs out of time."""
    return clock.monotonic() + OPERATION_SECONDS


def _check_deadline(deadline: float) -> None:
    if clock.monotonic() > deadline:
        raise CalendarError(TOO_SLOW)


def _check_lengths(**values: str | None) -> None:
    for name, value in values.items():
        limit = TEXT_LIMITS[name]
        if value is not None and len(value) > limit:
            message = f"{name} is too long (limit {limit} characters)."
            raise CalendarError(message)


@contextlib.contextmanager
def _writing():
    """Report a write that failed in a way that leaves its outcome open."""
    try:
        yield
    except OSError:
        raise CalendarError(UNCONFIRMED) from None


# --- Recurrence -------------------------------------------------------------

def _occurrences(
    ical: icalendar.Calendar,
    range_start: datetime,
    range_end: datetime
) -> tuple[list[icalendar.Event], bool]:
    """Return a stored event's occurrences in the range, and whether
    that is all of them.

    A series that would cost too much to expand, or that the library can't
    expand, comes back unexpanded and flagged incomplete.
    """
    events = ical.events
    try:
        dense = any(
            _too_dense(rule, component, range_start, range_end)
            for component in events
            for rule in _rules(component)
        )
        if not dense:
            searcher = icalendar_searcher.Searcher(
                event=True,
                start=range_start,
                end=range_end,
                expand=True
            )
            return list(searcher.check_component(ical) or []), True
    except Exception:
        # Malformed data from another client. Fall through and list the
        # event unexpanded, so one bad event can't hide the others.
        pass
    masters = [event for event in events if 'RECURRENCE-ID' not in event]
    return masters or events, False


def _rules(component: icalendar.Event) -> list[icalendar.vRecur]:
    rules = component.get('RRULE', [])
    return rules if isinstance(rules, list) else [rules]


def _too_dense(
    rule: icalendar.vRecur,
    component: icalendar.Event,
    range_start: datetime,
    range_end: datetime
) -> bool:
    """Estimate whether expanding a stored rule would blow the budget."""
    frequency = str(_single(rule, 'FREQ', '')).upper()
    interval = _single(rule, 'INTERVAL', 1)
    if frequency not in FREQUENCY_SECONDS or not isinstance(interval, int):
        return True
    if interval < 1:
        return True
    step = FREQUENCY_SECONDS[frequency] * interval
    # Each of these multiplies the occurrences every step produces.
    per_step = math.prod(
        max(1, len(rule.get(part, [])))
        for part in ('BYHOUR', 'BYMINUTE', 'BYSECOND')
    )
    first = _sort_key(component.start)
    walked = (range_end - first).total_seconds() / step * per_step
    # A rule coarser than daily can still land on every day (BYDAY).
    window = (range_end - range_start).total_seconds()
    in_range = window / min(step, 86_400) * per_step
    return walked > MAX_RULE_STEPS or in_range > MAX_EVENTS


def _parse_rrule(rrule: str) -> icalendar.vRecur:
    """Parse and check a rule for a new event.

    icalendar parses leniently (it accepts INTERVAL=0 and COUNT=-1), so
    the values are checked here.
    """
    invalid = CalendarError(
        "Invalid rrule. Expected an RFC 5545 rule such as "
        "'FREQ=WEEKLY;BYDAY=MO,WE', repeating daily or less often, with "
        "COUNT or UNTIL but not both."
    )
    try:
        rule = icalendar.vRecur.from_ical(rrule.removeprefix('RRULE:'))
    except ValueError:
        raise invalid from None

    if str(_single(rule, 'FREQ', '')).upper() not in RRULE_FREQUENCIES:
        raise invalid
    if 'COUNT' in rule and 'UNTIL' in rule:
        raise invalid
    for part, values in rule.items():
        if part in RRULE_NUMBER_RANGES:
            low, high = RRULE_NUMBER_RANGES[part]
            for value in values:
                number = _as_int(value)
                if number is None or number == 0:
                    raise invalid
                if not low <= number <= high:
                    raise invalid
        elif part not in RRULE_OTHER_PARTS:
            raise invalid
    for part in ('FREQ', 'INTERVAL', 'COUNT', 'UNTIL', 'WKST'):
        if len(rule.get(part, [])) > 1:
            raise invalid
    return rule


def _single(rule: icalendar.vRecur, part: str, default):
    values = rule.get(part)
    return values[0] if values else default


def _as_int(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


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
        raise CalendarError(message)
    if end <= start:
        raise CalendarError("end must be after start.")


def _default_duration(start: date | datetime) -> timedelta:
    if isinstance(start, datetime):
        return timedelta(hours=1)
    return timedelta(days=1)


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
    zone = _zone(timezone)
    try:
        if len(value) == 10:
            return date.fromisoformat(value)
        moment = datetime.fromisoformat(value)
    except ValueError:
        message = (
            "Invalid date or date-time. Use ISO 8601, such as "
            "'2026-11-13' or '2026-11-13T15:00:00'."
        )
        raise CalendarError(message) from None

    if moment.tzinfo is not None:
        return moment.astimezone(zone or UTC)
    if zone is None:
        message = (
            "A date-time has no UTC offset. Add one, or pass timezone "
            "as an IANA name such as 'America/New_York'."
        )
        raise CalendarError(message)
    return moment.replace(tzinfo=zone)


def _zone(timezone: str | None) -> ZoneInfo | None:
    if not timezone:
        return None
    try:
        return ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError):
        message = (
            "Unknown timezone. Use an IANA name such as "
            "'America/New_York'."
        )
        raise CalendarError(message) from None


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


def _any_match(components: list[icalendar.Event], query: str) -> bool:
    return any(_matches(component, query) for component in components)


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
