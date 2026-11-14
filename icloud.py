"""iCloud calendar operations over CalDAV.

Times cross this module's boundary as ISO 8601 strings. A date-only string
('2026-11-13') means an all-day event; anything else is a timed event.

Failures the caller can act on are raised as CalendarError, whose message
never contains calendar data, the caller's input or anything iCloud sent
back. `safe_message` gives the same guarantee for errors from the libraries
underneath.
"""

import copy
import functools
import logging
import math
import os
import re
import sys
import time as clock
import uuid
import warnings
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime, time, timedelta
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import caldav
import icalendar
import icalendar_searcher
import niquests

ICLOUD_URL = 'https://caldav.icloud.com'
# Where the iCloud password may be sent: the address above, and the
# numbered servers it hands each account on to.
ICLOUD_HOSTS = re.compile(r'(caldav|p[0-9]+-caldav)\.icloud\.com')
PRODID = '-//icloud-calendar-mcp//EN'

# Seconds iCloud may stay silent on one request before it is abandoned.
REQUEST_TIMEOUT = 20
# Seconds one tool call may take. Past that it starts nothing new, an
# expansion under way is stopped, and no write is sent. It is also the
# longest any one request may go on being read.
OPERATION_SECONDS = 45
# iCloud requests in flight at once, across every tool call in the process.
MAX_PARALLEL_REQUESTS = 8

# The widest range one list_events call may cover.
MAX_RANGE_DAYS = 366
# The most events (after expanding recurrences) one call may produce.
MAX_EVENTS = 1000
# The most event text (in characters) one call may return.
MAX_RESPONSE_CHARS = 300_000
# The most iCloud may send in answer to one request, after decompression.
MAX_ICLOUD_BYTES = 8 * 1024 * 1024
# How much of an answer is read at a time. Small, because one read of
# compressed data can grow about a thousandfold when it is unpacked.
READ_BYTES = 16 * 1024
# The most steps a stored event's recurrence rules may be estimated to
# cost. The library walks a rule from the event's first date, one step per
# FREQ interval. This turns away the plainly dense rules before any work.
MAX_RULE_STEPS = 100_000
# Seconds of processor time expanding one stored event may take. No
# estimate covers every rule (one that never matches again is walked to
# the year 9999), so the expansion is timed as it runs and stopped (see
# _bounded). A rule within the estimate above needs well under a second.
EXPANSION_SECONDS = 2

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
    'etag': 255,
    'occurrence': 40,
    'url': 2000,
}
# The most alerts one event may be given, and the earliest one may sound.
MAX_ALERTS = 10
MAX_ALERT_MINUTES = 28 * 24 * 60
# What a change to a recurring event can apply to.
SCOPES = ('series', 'occurrence', 'following')
WEB_ADDRESS = re.compile(r'https?://[^\s<>"]+', re.IGNORECASE)
# Where Apple's apps keep a place's coordinates, as a 'geo:' address. With
# it the event shows a map.
PLACE = 'X-APPLE-STRUCTURED-LOCATION'
COORDINATES = re.compile(r'geo:(-?[0-9.]+),(-?[0-9.]+)', re.IGNORECASE)

# Recurrence rules this server will create. Stored rules from other apps
# may use the rest of RFC 5545; those are only bounded (see _occurrences).
RRULE_FREQUENCIES = {'DAILY', 'WEEKLY', 'MONTHLY', 'YEARLY'}
RRULE_NUMBER_RANGES = {
    'INTERVAL': (1, 1000),
    'COUNT': (1, 10_000),
    'BYMONTH': (1, 12),
    'BYMONTHDAY': (-31, 31),
    'BYSETPOS': (-366, 366),
}
RRULE_OTHER_PARTS = {'FREQ', 'UNTIL', 'BYDAY', 'WKST'}
# A weekday as BYDAY and WKST write it: 'MO', or numbered, '-1FR'.
WEEKDAY = re.compile(r'([+-]?[1-9][0-9]?)?(MO|TU|WE|TH|FR|SA|SU)')
# How far a numbered BYDAY may count. Other frequencies take no number.
RRULE_WEEKDAY_NUMBERS = {'MONTHLY': 5, 'YEARLY': 53}
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
TOO_MUCH_DATA = (
    "iCloud sent more data than this server accepts at once. Use a "
    "shorter range, or name a calendar."
)
TOO_MUCH_TEXT = (
    "The events in that range hold too much text to return at once. Use "
    "a shorter range, name a calendar, or add a query."
)
INVITEES = (
    "This event has invitees, and iCloud may notify them of the change. "
    "Ask the user whether to go ahead, saying that invitees may be "
    "notified. Only if they agree, call again with invitees_confirmed "
    "set to true."
)
INCOMPLETE = (
    "iCloud left out the details of some events, so the list would be "
    "incomplete. Try again."
)
NO_VERSION = (
    "iCloud gave no version for this event, so it can't be changed or "
    "deleted safely."
)
UNCONFIRMED = (
    "iCloud did not confirm the change, so it may or may not have been "
    "saved. Check with list_events before trying again."
)
CONFLICT = (
    "The event was changed by someone else in the meantime, so nothing "
    "was done. Look at it again with get_event before retrying."
)
STALE = (
    "The event has changed since it was read, so nothing was done. Read "
    "it again with get_event, tell the user what is different, and "
    "retry with the new etag if the change still makes sense."
)
NOT_ICLOUD = (
    "A request was headed somewhere other than iCloud, so it was not "
    "sent."
)
NEEDS_SCOPE = (
    "This event repeats, so say what the change applies to. Pass scope: "
    "'occurrence' for one occurrence, 'following' for one occurrence "
    "and every later one (delete_event only), or 'series' for all of "
    "them. With 'occurrence' or 'following', also pass `occurrence`, "
    "the value list_events gives for that occurrence. Ask the user if "
    "it is not clear which they mean."
)
NOT_REPEATING = (
    "This event does not repeat, so there is no occurrence to pick. "
    "Leave out occurrence and scope."
)
NO_SUCH_OCCURRENCE = (
    "This event has no occurrence starting then. Pass the `occurrence` "
    "value list_events gives for the one you mean."
)
NOT_BY_OCCURRENCE = (
    "This series can't be worked out one occurrence at a time: it "
    "repeats too often, or its data is damaged. Only the whole series "
    "can be changed."
)


class CalendarError(Exception):
    """A failure whose message is safe to show the caller and to log."""


# --- Set once, for the whole process ----------------------------------------
#
# Most of this keeps calendar data out of the logs, or keeps a listing
# from coming back short. None of it can be done per tool call: Python's
# own ways of changing logging or warnings for a stretch of code are not
# safe across threads.

logger = logging.getLogger(__name__)

# caldav logs event contents when it repairs malformed data from the server.
# Below ERROR it says nothing at all; what is left goes through
# _safe_record.
logging.getLogger('caldav').setLevel(logging.ERROR)

CALDAV_DIRECTORY = os.path.dirname(caldav.__file__)
_make_record = logging.getLogRecordFactory()


def _safe_record(
    name,
    level,
    pathname,
    lineno,
    message,
    arguments,
    exc_info,
    *rest
) -> logging.LogRecord:
    """Build a log record, withholding the text of anything caldav logs.

    When a request goes wrong, caldav logs the exception, which carries
    the URL and iCloud's reply. Some of that goes through the root logger,
    which no level set on the 'caldav' logger reaches. So the record is
    changed where every logger gets it made: only where in caldav it came
    from is kept.
    """
    if pathname.startswith(CALDAV_DIRECTORY):
        message = "caldav reported a problem at %s:%s (details withheld)"
        arguments = (os.path.basename(pathname), lineno)
        exc_info = None
    return _make_record(
        name,
        level,
        pathname,
        lineno,
        message,
        arguments,
        exc_info,
        *rest
    )


logging.setLogRecordFactory(_safe_record)


def _show_warning(
    message,
    category,
    filename,
    lineno,
    file=None,
    line=None
) -> None:
    """Report a Python warning by its kind and origin, without its text.

    The text can quote what was being parsed: icalendar's warning about an
    unusual timezone name repeats the name, which comes from the event.
    """
    logger.warning("%s raised at %s:%s", category.__name__, filename, lineno)


warnings.showwarning = _show_warning

_load_objects = caldav.Calendar._batch_load_objects


def _load_every_object(self, objects: list) -> None:
    """Fetch the data of events a search named without it, or fail.

    caldav's own loader carries on when that fetch fails, and the search
    then leaves those events out without a word: the listing comes back
    short and looks complete. An event that was fetched and turned out to
    be empty is not a failure; caldav drops those itself.
    """
    _load_objects(self, objects)
    if not all(found._has_data() for found in objects):
        raise CalendarError(INCOMPLETE)


caldav.Calendar._batch_load_objects = _load_every_object

# One pool for the whole process. Every call to iCloud runs on it, so
# simultaneous tool calls share the request budget instead of each
# bringing its own threads.
_pool = ThreadPoolExecutor(
    max_workers=MAX_PARALLEL_REQUESTS,
    thread_name_prefix='icloud'
)


# --- The operations ---------------------------------------------------------

def list_calendars() -> list[str]:
    """Return the names of the calendars that can hold events."""
    deadline = _deadline()
    calendars = _calendars(deadline)
    components = _per_calendar(
        lambda calendar: calendar.get_supported_components(),
        [calendar for calendar, _ in calendars],
        deadline
    )
    return [
        name
        for (_, name), supported in zip(calendars, components)
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
    series that costs too much to expand, which is listed once and marked
    with 'occurrences_omitted'. Raises CalendarError rather than return a
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
    range_start, range_end = _range(start, end, timezone)
    found = []
    size = 0
    matches = _search(range_start, range_end, calendar, query, deadline)
    for component, name, etag, complete in matches:
        entry = _event_to_dict(component, name, timezone, etag)
        if not complete:
            entry['occurrences_omitted'] = True
        size += _size(entry)
        if size > MAX_RESPONSE_CHARS:
            raise CalendarError(TOO_MUCH_TEXT)
        found.append((_sort_key(component.start), entry))

    # The last event's expansion may be what ran out the clock.
    _check_deadline(deadline)
    found.sort(key=lambda pair: pair[0])
    return [entry for _, entry in found]


def list_busy_times(
    start: str,
    end: str,
    calendar: str | None = None,
    timezone: str | None = None
) -> dict:
    """Return when the calendars are busy within [start, end), merged.

    Only times come back, no titles or other text. Events marked free
    and cancelled events don't count. A series that costs too much to
    expand is left out, and the result is then marked 'incomplete'.
    """
    _check_lengths(
        start=start,
        end=end,
        calendar=calendar,
        timezone=timezone
    )
    deadline = _deadline()
    range_start, range_end = _range(start, end, timezone)
    zone = _zone(timezone) or UTC
    spans = []
    incomplete = False
    matches = _search(range_start, range_end, calendar, None, deadline)
    for component, _, _, complete in matches:
        if not complete:
            incomplete = True
        elif _is_busy(component):
            begins = max(_moment(component.start, zone), range_start)
            ends = min(_moment(component.end, zone), range_end)
            if begins < ends:
                spans.append((begins, ends))

    _check_deadline(deadline)
    result = {
        'busy': [
            {
                'start': begins.astimezone(zone).isoformat(),
                'end': ends.astimezone(zone).isoformat(),
            }
            for begins, ends in _merged(spans)
        ],
    }
    if incomplete:
        result['incomplete'] = True
    return result


def get_event(
    calendar: str,
    uid: str,
    timezone: str | None = None
) -> dict:
    """Return one event in full, with the version to name when changing
    it.

    For a recurring event this is the event that repeats, with its rule,
    not one of its occurrences.
    """
    _check_lengths(calendar=calendar, uid=uid, timezone=timezone)
    deadline = _deadline()
    _zone(timezone)
    cal, name = _find_calendar(calendar, deadline)
    event = _find_event(cal, uid, deadline)
    component = _main(event.icalendar_instance)
    entry = _event_to_dict(component, name, timezone, event.etag)
    entry.update(_people(component))
    rules = _rules(component)
    if rules:
        entry['rrule'] = rules[0].to_ical().decode()
    if _size(entry) > MAX_RESPONSE_CHARS:
        raise CalendarError("This event holds too much text to return.")
    return entry


def create_event(
    calendar: str,
    summary: str,
    start: str,
    end: str | None = None,
    timezone: str | None = None,
    location: str | None = None,
    description: str | None = None,
    rrule: str | None = None,
    url: str | None = None,
    alerts: list[int] | None = None,
    free: bool | None = None,
    latitude: float | None = None,
    longitude: float | None = None
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
        rrule=rrule,
        url=url
    )
    deadline = _deadline()
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
    _check_details(location, url, alerts, latitude, longitude)

    event = icalendar.Event.new(
        uid=str(uuid.uuid4()),
        summary=summary,
        start=dtstart,
        end=dtend
    )
    if rrule:
        event.add('rrule', _parse_rrule(rrule))
    _edit(
        event,
        location=location,
        description=description,
        url=url,
        alerts=alerts,
        free=free,
        latitude=latitude,
        longitude=longitude
    )
    data = _wrap(event).to_ical().decode()

    cal, name = _find_calendar(calendar, deadline)
    created = _event_to_dict(event, name, timezone)
    saved = _write(lambda: cal.add_event(ical=data), deadline)
    return _with_version(created, saved)


def update_event(
    calendar: str,
    uid: str,
    etag: str,
    summary: str | None = None,
    start: str | None = None,
    end: str | None = None,
    timezone: str | None = None,
    location: str | None = None,
    description: str | None = None,
    url: str | None = None,
    alerts: list[int] | None = None,
    free: bool | None = None,
    latitude: float | None = None,
    longitude: float | None = None,
    occurrence: str | None = None,
    scope: str | None = None,
    invitees_confirmed: bool = False
) -> dict:
    """Change the given fields of an event and return the result.

    Fields left as None are untouched; an empty string clears location,
    description or url, and an empty list clears the alerts. `etag` is
    the version the caller read: if the event has changed since, nothing
    is done. A recurring event needs `scope`: the whole series, or the
    one occurrence named by `occurrence`. An event with invitees is
    changed only when invitees_confirmed is true.
    """
    _check_lengths(
        calendar=calendar,
        uid=uid,
        etag=etag,
        summary=summary,
        start=start,
        end=end,
        timezone=timezone,
        location=location,
        description=description,
        url=url,
        occurrence=occurrence
    )
    deadline = _deadline()
    _zone(timezone)
    _check_details(location, url, alerts, latitude, longitude)
    _check_target(scope, occurrence)
    if scope == 'following':
        message = (
            "Changing one occurrence and every later one is not "
            "supported. Change the whole series, or one occurrence at a "
            "time."
        )
        raise CalendarError(message)
    cal, name = _find_calendar(calendar, deadline)
    event = _find_event(cal, uid, deadline)
    _version(event, etag)
    _check_invitees(event, invitees_confirmed)
    with event.edit_icalendar_instance() as ical:
        if _scope(ical, scope) == 'series':
            component = _master(ical)
        else:
            found = _find_occurrence(ical, occurrence, deadline)
            component = _own_details(ical, found)
        if start is not None or end is not None:
            _reschedule(component, start, end, timezone)
        _edit(
            component,
            summary=summary,
            location=location,
            description=description,
            url=url,
            alerts=alerts,
            free=free,
            latitude=latitude,
            longitude=longitude
        )
        ical.add_missing_timezones()
    updated = _event_to_dict(component, name, timezone)
    # 'etag' has caldav send the version it fetched as the condition (it
    # was checked above that there is one, and that it is the one the
    # caller read), so iCloud refuses the save if the event changed in
    # between.
    _write(lambda: event.save(precondition='etag'), deadline)
    return _with_version(updated, event)


def delete_event(
    calendar: str,
    uid: str,
    etag: str,
    occurrence: str | None = None,
    scope: str | None = None,
    invitees_confirmed: bool = False
) -> dict:
    """Delete an event, or part of a recurring one.

    `etag` is the version the caller read: if the event has changed
    since, nothing is deleted. A recurring event needs `scope`: the whole
    series, the one occurrence named by `occurrence`, or that occurrence
    and every later one. An event with invitees is deleted only when
    invitees_confirmed is true.
    """
    _check_lengths(
        calendar=calendar,
        uid=uid,
        etag=etag,
        occurrence=occurrence
    )
    deadline = _deadline()
    _check_target(scope, occurrence)
    cal, _ = _find_calendar(calendar, deadline)
    event = _find_event(cal, uid, deadline)
    # Only the version the caller read, which is also the one just
    # looked up: if another client changes the event after this, iCloud
    # refuses and nothing is lost.
    version = _version(event, etag)
    _check_invitees(event, invitees_confirmed)
    if _scope(event.icalendar_instance, scope) == 'series':
        _write(lambda: event.delete(if_match=version), deadline)
        return {'deleted': uid}

    # The series stays, without some of its occurrences: a save, not a
    # delete.
    with event.edit_icalendar_instance() as ical:
        found = _find_occurrence(ical, occurrence, deadline)
        if scope == 'occurrence':
            _drop_occurrence(ical, found)
        else:
            _end_before(ical, found)
    deleted = {'deleted': uid, 'scope': scope, 'occurrence': occurrence}
    _write(lambda: event.save(precondition='etag'), deadline)
    return _with_version(deleted, event)


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
    if isinstance(error, _TooMuchData):
        return TOO_MUCH_DATA
    # niquests raises OSError subclasses for timeouts and lost connections.
    if isinstance(error, OSError):
        return "Could not reach iCloud, or it stopped answering."
    return None


# --- iCloud lookups ---------------------------------------------------------

def _search(
    range_start: datetime,
    range_end: datetime,
    calendar: str | None,
    query: str | None,
    deadline: float
):
    """Yield what overlaps the range, in one calendar or in all of them.

    Each event or occurrence comes with its calendar's name, the version
    of the stored event it belongs to, and whether its series could be
    expanded. Raises CalendarError past MAX_EVENTS.
    """
    if calendar:
        calendars = [_find_calendar(calendar, deadline)]
    else:
        calendars = _calendars(deadline)

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

    count = 0
    searched = _per_calendar(
        search,
        [cal for cal, _ in calendars],
        deadline
    )
    for (_, name), results in zip(calendars, searched):
        for result in results:
            _check_deadline(deadline)
            ical = result.icalendar_instance
            if query and not _any_match(ical.events, query):
                continue
            components, complete = _occurrences(
                ical,
                range_start,
                range_end,
                deadline
            )
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
                yield component, name, result.etag, complete


def _range(
    start: str,
    end: str,
    timezone: str | None
) -> tuple[datetime, datetime]:
    """Parse and check the two ends of a range to search."""
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
    return range_start, range_end


def _calendars(deadline: float) -> list[tuple[caldav.Calendar, str]]:
    """Return every calendar on the account, each with its name."""
    def fetch():
        return [
            (calendar, calendar.get_display_name() or '')
            for calendar in _principal().get_calendars()
        ]

    return _ask(fetch, deadline)


def _find_calendar(
    name: str,
    deadline: float
) -> tuple[caldav.Calendar, str]:
    """Return the calendar of that name, and the name as iCloud has it."""
    calendars = _calendars(deadline)
    exact = [pair for pair in calendars if pair[1] == name]
    loose = [
        pair
        for pair in calendars
        if pair[1].casefold() == name.casefold()
    ]
    for matches in (exact, loose):
        if len(matches) == 1:
            return matches[0]
    message = (
        "No single calendar has that name. Use list_calendars to see "
        "the names."
    )
    raise CalendarError(message)


def _find_event(
    calendar: caldav.Calendar,
    uid: str,
    deadline: float
) -> caldav.Event:
    try:
        return _ask(lambda: calendar.get_event_by_uid(uid), deadline)
    except caldav.error.NotFoundError:
        message = (
            "No event with that UID in that calendar. Use list_events to "
            "find it."
        )
        raise CalendarError(message) from None


def _version(event: caldav.Event, read: str) -> str:
    """Return the ETag naming exactly the version that was looked up,
    which must be the one the caller read.

    A change or delete is sent on condition that the event still has it.
    Left to itself caldav settles for less when there is no strong ETag:
    a Schedule-Tag, which stays the same through some changes (RFC 6638),
    or for a save no condition at all. Here that is refused instead.

    The caller's version closes the other gap: an event that changed
    between the caller reading it and asking for the change would
    otherwise be changed or deleted on the strength of what it used to
    say.
    """
    etag = event.etag
    current = _shown(etag)
    if current is None:
        raise CalendarError(NO_VERSION)
    if read.strip().strip('"') != current:
        raise CalendarError(STALE)
    return etag


def _shown(etag: str | None) -> str | None:
    """Return a strong ETag as callers see it, without its quotes.

    None for a missing or weak one: a version that can't be made a
    condition is not offered as one.
    """
    if not etag or etag.startswith('W/'):
        return None
    return etag.strip('"')


def _with_version(reply: dict, stored) -> dict:
    """Add to a reply the version iCloud gave what was just saved.

    iCloud doesn't always give one. This runs after the write, so it
    must not be able to fail: an error here would invite a retry.
    """
    etag = _shown(getattr(stored, 'etag', None))
    if etag:
        reply['etag'] = etag
    return reply


def _check_invitees(event: caldav.Event, confirmed: bool) -> None:
    """Refuse to change an event with invitees unless that was confirmed.

    iCloud does the scheduling: saving a change to such an event, or
    deleting it, may send its invitees an update, a cancellation or a
    decline. Whether it does has not been tested here, so the message
    says "may". The answer has to come from the user, and this can only
    make the model ask: it is told how, and has to call a second time.
    """
    if confirmed:
        return
    if any('ATTENDEE' in found for found in event.icalendar_instance.events):
        raise CalendarError(INVITEES)


# --- Requests to iCloud -----------------------------------------------------

def _ask(call, deadline: float):
    """Have iCloud answer one call, on the shared pool, by the deadline."""
    return _wait(_pool.submit(_in_time, call, deadline), deadline)


def _per_calendar(
    function,
    calendars: list[caldav.Calendar],
    deadline: float
) -> list:
    """Call function once per calendar, concurrently, keeping order.

    Each call is a separate request to iCloud, so running them one after
    another makes every tool call slower with each calendar on the account.
    """
    futures = [
        _pool.submit(_in_time, functools.partial(function, calendar), deadline)
        for calendar in calendars
    ]
    try:
        return [_wait(future, deadline) for future in futures]
    finally:
        # Drops the calls still queued. One already talking to iCloud runs
        # on until its request ends or _LimitedSession stops it.
        for future in futures:
            future.cancel()


def _write(change, deadline: float):
    """Make one change at iCloud, or say so if its outcome is unknown.

    A change that has not been sent by the deadline never is: this stops
    waiting for it then, and takes it back out of the queue. Returns
    whatever the change returns.
    """
    _check_deadline(deadline)
    future = _pool.submit(_in_time, change, deadline)
    try:
        return future.result(timeout=max(0, deadline - clock.monotonic()))
    except OSError:
        # Out of time (TimeoutError is an OSError too), a lost connection,
        # or an answer cut short. If the change was still queued, taking
        # it out settles the matter. Otherwise it may have gone through.
        if future.cancel():
            raise CalendarError(TOO_SLOW) from None
        raise CalendarError(UNCONFIRMED) from None


def _wait(future, deadline: float):
    try:
        return future.result(timeout=max(0, deadline - clock.monotonic()))
    except TimeoutError:
        if future.done():
            # The call's own timeout, or an answer that arrived just now.
            return future.result()
        future.cancel()
        raise CalendarError(TOO_SLOW) from None


def _in_time(call, deadline: float):
    """Run a queued call, unless its tool call ran out of time waiting."""
    _check_deadline(deadline)
    return call()


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
    _limit_answers(client)
    return client.get_principal()


def _limit_answers(client: caldav.DAVClient) -> None:
    """Make every request of this client go through _LimitedSession."""
    client.session.close()
    client.session = _LimitedSession()


class _LimitedSession(niquests.Session):
    """Sends requests to iCloud only, and reads its answers a piece at a
    time, up to a size and a time.

    caldav buffers a whole answer however large it is, and REQUEST_TIMEOUT
    only limits the silence between two pieces, not how long an answer
    that keeps trickling in may take.
    """

    def send(self, request, **arguments):
        # Every request leaves through here, each step of a redirect
        # included, and caldav puts the password on all of them. The
        # addresses come from iCloud's own answers, so one outside
        # iCloud should never turn up; if one does, it gets nothing.
        if not _is_icloud(request.url):
            raise CalendarError(NOT_ICLOUD)
        return super().send(request, **arguments)

    def request(self, method, url, **arguments):
        started = clock.monotonic()
        arguments['stream'] = True
        response = super().request(method, url, **arguments)
        body = bytearray()
        try:
            # iter_content undoes any compression, so this counts the
            # size the answer has in memory, not on the wire.
            for piece in response.iter_content(READ_BYTES):
                body += piece
                if len(body) > MAX_ICLOUD_BYTES:
                    raise _TooMuchData()
                if clock.monotonic() - started > OPERATION_SECONDS:
                    raise TimeoutError()
        except BaseException:
            response.close()
            raise
        # caldav reads the body from .content, which niquests fills from
        # this attribute once the stream has been used up.
        response._content = bytes(body)
        return response


def _is_icloud(url: str) -> bool:
    """Whether a request to this address may carry the password."""
    parts = urlsplit(url)
    host = parts.hostname or ''
    return parts.scheme == 'https' and bool(ICLOUD_HOSTS.fullmatch(host))


class _TooMuchData(OSError):
    """iCloud's answer to one request went over MAX_ICLOUD_BYTES.

    An OSError like the other ways a request can break off, so a write
    it happens to is reported as unconfirmed.
    """


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


# --- Recurrence -------------------------------------------------------------

def _occurrences(
    ical: icalendar.Calendar,
    range_start: datetime,
    range_end: datetime,
    deadline: float
) -> tuple[list[icalendar.Event], bool]:
    """Return a stored event's occurrences in the range, and whether
    that is all of them.

    A series that costs too much to expand, or that the library can't
    expand, comes back unexpanded and flagged incomplete.
    """
    events = ical.events
    try:
        if not _too_costly(events, range_start, range_end):
            searcher = icalendar_searcher.Searcher(
                event=True,
                start=range_start,
                end=range_end,
                expand=True
            )
            found = _bounded(
                lambda: list(searcher.check_component(ical) or []),
                deadline
            )
            return found, True
    except (Exception, _OverBudget):
        # Malformed data from another client, or a rule that cost too
        # much. Fall through and list the event unexpanded, so one bad
        # event can't hide the others.
        pass
    masters = [event for event in events if 'RECURRENCE-ID' not in event]
    return masters or events, False


def _bounded(work, deadline: float):
    """Run work(), stopping it once it has cost too much.

    The libraries that expand a rule are plain Python loops with no way
    to interrupt them from outside. So Python is asked to report every
    function call they make, which is where the clocks get looked at:
    after EXPANSION_SECONDS of this thread's processor time, or past the
    deadline, _OverBudget is raised from inside them.
    """
    calls = 0
    enough = clock.thread_time() + EXPANSION_SECONDS

    def check(frame, event, argument):
        # Returns nothing, which tells Python not to trace the call line
        # by line as well.
        nonlocal calls
        calls += 1
        if calls % 1000:
            return
        if clock.thread_time() > enough or clock.monotonic() > deadline:
            raise _OverBudget()

    # Per thread, so other tool calls are not timed or slowed.
    previous = sys.gettrace()
    sys.settrace(check)
    try:
        return work()
    finally:
        sys.settrace(previous)


class _OverBudget(BaseException):
    """Stops the work _bounded is running.

    Not an Exception, so that no handler inside a library takes it for
    one of its own failures and carries on.
    """


def _too_costly(
    events: list[icalendar.Event],
    range_start: datetime,
    range_end: datetime
) -> bool:
    """Estimate whether expanding a stored event would blow the budget.

    Counts everything that can become an occurrence: each component (the
    event and its per-occurrence overrides), each explicit date (RDATE)
    whether or not it is in the range, and what each rule puts in the
    range, rounded up. The library returns the occurrences as one list
    it has already built, so their number has to be limited from here.

    The steps are another matter: for those this only spares starting on
    a rule that is plainly too dense. The limit that holds for every
    rule is the one in _bounded.
    """
    steps = 0
    found = 0
    for component in events:
        found += 1 + len(component.rdates)
        for rule in _rules(component):
            cost = _rule_cost(rule, component, range_start, range_end)
            steps += cost[0]
            found += cost[1] + 1
    return steps > MAX_RULE_STEPS or found > MAX_EVENTS


def _rules(component: icalendar.Event) -> list[icalendar.vRecur]:
    rules = component.get('RRULE', [])
    return rules if isinstance(rules, list) else [rules]


def _rule_cost(
    rule: icalendar.vRecur,
    component: icalendar.Event,
    range_start: datetime,
    range_end: datetime
) -> tuple[float, float]:
    """Estimate the steps a rule takes to walk, and the occurrences it
    puts in the range."""
    frequency = str(_single(rule, 'FREQ', '')).upper()
    interval = _single(rule, 'INTERVAL', 1)
    if frequency not in FREQUENCY_SECONDS or not isinstance(interval, int):
        return math.inf, math.inf
    if interval < 1:
        return math.inf, math.inf
    step = FREQUENCY_SECONDS[frequency] * interval
    # Each of these multiplies the occurrences every step produces.
    per_step = math.prod(
        max(1, len(rule.get(part, [])))
        for part in ('BYHOUR', 'BYMINUTE', 'BYSECOND')
    )
    first = _sort_key(component.start)
    walked = (range_end - first).total_seconds() / step * per_step
    # An occurrence that began before the range still counts while it
    # lasts into it, so a long event widens the range it is found in.
    lasting = _sort_key(component.end) - first
    window = (range_end - range_start + lasting).total_seconds()
    # A rule coarser than daily can still land on every day (BYDAY).
    in_range = window / min(step, 86_400) * per_step
    return walked, in_range


def _parse_rrule(rrule: str) -> icalendar.vRecur:
    """Parse and check a rule for a new event.

    icalendar parses leniently (it accepts INTERVAL=0, COUNT=-1 and
    BYDAY=99MO), so the values are checked here.
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

    frequency = str(_single(rule, 'FREQ', '')).upper()
    if frequency not in RRULE_FREQUENCIES:
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
    numbered = RRULE_WEEKDAY_NUMBERS.get(frequency, 0)
    if not all(_is_weekday(day, numbered) for day in rule.get('BYDAY', [])):
        raise invalid
    if not all(_is_weekday(day, 0) for day in rule.get('WKST', [])):
        raise invalid
    return rule


def _is_weekday(value, highest: int) -> bool:
    """Whether value is a weekday such as 'MO', or a numbered one such
    as '-1FR' that counts no further than highest."""
    match = WEEKDAY.fullmatch(str(value).upper())
    return bool(match) and abs(int(match[1] or 0)) <= highest


def _single(rule: icalendar.vRecur, part: str, default):
    values = rule.get(part)
    return values[0] if values else default


def _as_int(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


# --- One occurrence of a series ---------------------------------------------

def _check_target(scope: str | None, occurrence: str | None) -> None:
    """Check that scope and occurrence make sense together.

    On the arguments alone, before iCloud is asked anything.
    """
    if scope is not None and scope not in SCOPES:
        message = "scope must be 'series', 'occurrence' or 'following'."
        raise CalendarError(message)
    picks_one = scope in ('occurrence', 'following')
    if picks_one != bool(occurrence):
        raise CalendarError(NEEDS_SCOPE)
    if occurrence:
        _parse_occurrence(occurrence)


def _scope(ical: icalendar.Calendar, scope: str | None) -> str:
    """Return what a change to this event applies to.

    An event that repeats is never changed on a guess. Asked to delete
    "Tuesday's meeting", a caller that names only the event would
    otherwise delete every one of them.
    """
    repeats = any(
        name in component
        for component in ical.events
        for name in ('RRULE', 'RDATE', 'RECURRENCE-ID')
    )
    if not repeats:
        if scope not in (None, 'series'):
            raise CalendarError(NOT_REPEATING)
        return 'series'
    if scope is None:
        raise CalendarError(NEEDS_SCOPE)
    return scope


def _find_occurrence(
    ical: icalendar.Calendar,
    occurrence: str,
    deadline: float
) -> icalendar.Event:
    """Return the occurrence of a series that was due to start then.

    Either the stored component that gives the occurrence details of its
    own, or a copy made by expanding the series. An occurrence is named
    by where its rule put it, wherever it has been moved to since.
    """
    wanted = _parse_occurrence(occurrence)
    for component in ical.events:
        if _same_moment(_original(component), wanted):
            return component
    around = _sort_key(wanted)
    found, complete = _occurrences(
        ical,
        around - timedelta(days=1),
        around + timedelta(days=1),
        deadline
    )
    if not complete:
        raise CalendarError(NOT_BY_OCCURRENCE)
    for component in found:
        if _same_moment(_original(component), wanted):
            return component
    raise CalendarError(NO_SUCH_OCCURRENCE)


def _own_details(
    ical: icalendar.Calendar,
    found: icalendar.Event
) -> icalendar.Event:
    """Return the stored component holding one occurrence's own details.

    A series without one for that occurrence is given it: a copy of the
    event that repeats, at the occurrence's time and without the rule.
    """
    if any(found is component for component in ical.events):
        return found
    details = copy.deepcopy(_master(ical))
    for name in ('RRULE', 'RDATE', 'EXDATE', 'EXRULE'):
        details.pop(name, None)
    for name in ('DTSTART', 'DTEND', 'DURATION'):
        details.pop(name, None)
    details.add('recurrence-id', _original(found))
    details.add('dtstart', found.start)
    details.add('dtend', found.end)
    ical.add_component(details)
    return details


def _drop_occurrence(
    ical: icalendar.Calendar,
    found: icalendar.Event
) -> None:
    """Take one occurrence out of a series."""
    master = _master(ical)
    _remove(ical, [found])
    master.add('exdate', _original(found))


def _end_before(
    ical: icalendar.Calendar,
    found: icalendar.Event
) -> None:
    """End a series just before one of its occurrences."""
    master = _master(ical)
    first_gone = _original(found)
    if master.rdates:
        message = (
            "This series lists some of its dates one by one, so it can't "
            "be cut short here. Delete its occurrences one at a time, or "
            "the whole series."
        )
        raise CalendarError(message)
    if _sort_key(first_gone) <= _sort_key(master.start):
        message = (
            "That is the first occurrence. To delete it and every later "
            "one, delete the whole series: scope 'series'."
        )
        raise CalendarError(message)
    if not isinstance(first_gone, datetime):
        last = first_gone - timedelta(days=1)
    elif first_gone.tzinfo is None:
        last = first_gone - timedelta(seconds=1)
    else:
        # RFC 5545 wants UNTIL in UTC when the start has a timezone.
        last = (first_gone - timedelta(seconds=1)).astimezone(UTC)
    for rule in _rules(master):
        # A rule ends by count or by date, not both.
        rule.pop('COUNT', None)
        rule['UNTIL'] = [last]
    later = [
        component
        for component in ical.events
        if _original(component) is not None
        and _sort_key(_original(component)) >= _sort_key(first_gone)
    ]
    _remove(ical, later)


def _original(component: icalendar.Event) -> date | datetime | None:
    """Return when the rule put an occurrence, or None for the event
    that repeats."""
    return getattr(component.get('RECURRENCE-ID'), 'dt', None)


def _same_moment(
    one: date | datetime | None,
    other: date | datetime
) -> bool:
    """Whether two starts are the same.

    Each is a date, a date-time with a zone or one without. Two of
    different kinds are never the same.
    """
    def kind(value):
        timed = isinstance(value, datetime)
        return timed, timed and value.tzinfo is not None

    return one is not None and kind(one) == kind(other) and one == other


def _parse_occurrence(value: str) -> date | datetime:
    """Parse an `occurrence` value as list_events writes it."""
    try:
        if len(value) == 10:
            return date.fromisoformat(value)
        return datetime.fromisoformat(value)
    except ValueError:
        raise CalendarError(NO_SUCH_OCCURRENCE) from None


def _remove(ical: icalendar.Calendar, gone: list[icalendar.Event]) -> None:
    """Take stored components out of an event: those very ones, not
    others that happen to equal them."""
    ical.subcomponents[:] = [
        component
        for component in ical.subcomponents
        if not any(component is one for one in gone)
    ]


# --- Editing ----------------------------------------------------------------

def _wrap(event: icalendar.Event) -> icalendar.Calendar:
    """Put a new event in the calendar object that gets stored."""
    ical = icalendar.Calendar.new(prodid=PRODID, subcomponents=[event])
    # icalendar gives the wrapper a UID of its own. caldav then mistakes it
    # for the event's, and the event can't be found by UID afterwards.
    ical.pop('UID', None)
    ical.add_missing_timezones()
    return ical


def _master(ical: icalendar.Calendar) -> icalendar.Event:
    """Return the event itself, not a per-occurrence override of it."""
    for component in ical.events:
        if 'RECURRENCE-ID' not in component:
            return component
    raise CalendarError("Event has no editable master component.")


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


def _check_details(
    location: str | None,
    url: str | None,
    alerts: list[int] | None,
    latitude: float | None,
    longitude: float | None
) -> None:
    """Check the details an event can be given, before anything is done."""
    if url and not WEB_ADDRESS.fullmatch(url):
        message = "url must be a web address: http:// or https://."
        raise CalendarError(message)
    if alerts is not None:
        usable = len(alerts) <= MAX_ALERTS and all(
            type(minutes) is int and 0 <= minutes <= MAX_ALERT_MINUTES
            for minutes in alerts
        )
        if not usable:
            message = (
                f"alerts must be at most {MAX_ALERTS} whole numbers of "
                f"minutes before the start, from 0 to {MAX_ALERT_MINUTES}."
            )
            raise CalendarError(message)
    if latitude is None and longitude is None:
        return
    numbers = all(
        isinstance(value, (int, float)) and not isinstance(value, bool)
        for value in (latitude, longitude)
    )
    # Written so that a NaN fails it too.
    if not (numbers and -90 <= latitude <= 90 and -180 <= longitude <= 180):
        message = (
            "latitude and longitude go together: latitude from -90 to "
            "90, longitude from -180 to 180."
        )
        raise CalendarError(message)
    if not location:
        message = (
            "Coordinates describe a place: pass its name or address as "
            "location in the same call."
        )
        raise CalendarError(message)


def _edit(
    component: icalendar.Event,
    summary: str | None = None,
    location: str | None = None,
    description: str | None = None,
    url: str | None = None,
    alerts: list[int] | None = None,
    free: bool | None = None,
    latitude: float | None = None,
    longitude: float | None = None
) -> None:
    """Set the given details of an event; None leaves one as it is."""
    if summary is not None:
        _set_text(component, 'SUMMARY', summary)
    if location is not None:
        _set_text(component, 'LOCATION', location)
        # Coordinates there already belong to the place this replaces.
        component.pop(PLACE, None)
        if latitude is not None:
            _set_coordinates(component, location, latitude, longitude)
    if description is not None:
        _set_text(component, 'DESCRIPTION', description)
    if url is not None:
        _set_text(component, 'URL', url)
    if free is not None:
        component.pop('TRANSP', None)
        component.add('transp', 'TRANSPARENT' if free else 'OPAQUE')
    if alerts is not None:
        _set_alerts(component, alerts)


def _set_text(component: icalendar.Event, name: str, value: str) -> None:
    component.pop(name, None)
    if value:
        component.add(name, value)


def _set_coordinates(
    component: icalendar.Event,
    location: str,
    latitude: float,
    longitude: float
) -> None:
    """Record where a place is, the way Apple's apps do."""
    # A parameter can't hold a line break or a double quote.
    title = ' '.join(location.replace('"', "'").split())
    component.add(
        PLACE,
        icalendar.vUri(f'geo:{latitude:.6f},{longitude:.6f}'),
        parameters={'VALUE': 'URI', 'X-TITLE': title}
    )


def _set_alerts(component: icalendar.Event, alerts: list[int]) -> None:
    """Replace an event's alerts with ones the given minutes before it."""
    component.subcomponents[:] = [
        part
        for part in component.subcomponents
        if part.name != 'VALARM'
    ]
    title = str(component.get('SUMMARY', '')) or "Event"
    for minutes in alerts:
        alarm = icalendar.Alarm()
        alarm.add('action', 'DISPLAY')
        alarm.add('trigger', timedelta(minutes=-minutes))
        alarm.add('description', title)
        component.add_component(alarm)


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
    timezone: str | None,
    etag: str | None = None
) -> dict:
    """Describe an event or occurrence, leaving out whatever it lacks.

    `etag` is the version of the stored event it is part of.
    """
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
    for key in ('location', 'description', 'url'):
        value = str(component.get(key.upper(), ''))
        if value:
            entry[key] = value
    place = COORDINATES.match(str(component.get(PLACE, '')))
    if place:
        try:
            entry['latitude'] = float(place[1])
            entry['longitude'] = float(place[2])
        except ValueError:
            pass
    alerts = _alerts(component)
    if alerts:
        entry['alerts'] = alerts
    if str(component.get('TRANSP', '')).upper() == 'TRANSPARENT':
        entry['free'] = True
    status = str(component.get('STATUS', '')).lower()
    if status in ('tentative', 'cancelled'):
        entry['status'] = status
    if 'RRULE' in component or 'RECURRENCE-ID' in component:
        entry['recurring'] = True
    original = _original(component)
    if isinstance(original, datetime) and original.tzinfo is not None:
        original = original.astimezone(UTC)
    if original is not None:
        # Names this occurrence to update_event and delete_event.
        entry['occurrence'] = original.isoformat()
    if 'ATTENDEE' in component:
        entry['has_invitees'] = True
    if _shown(etag):
        entry['etag'] = _shown(etag)
    return entry


def _alerts(component: icalendar.Event) -> list[int]:
    """Return how many minutes before its start an event's alerts sound.

    Alerts set some other way (at a fixed time, or after the start) are
    not described.
    """
    minutes = set()
    for part in component.subcomponents:
        before = getattr(part.get('TRIGGER'), 'dt', None)
        if part.name != 'VALARM' or type(before) is not timedelta:
            continue
        if before <= timedelta(0):
            minutes.add(int(-before.total_seconds() // 60))
    return sorted(minutes)


def _people(component: icalendar.Event) -> dict:
    """Return who organizes an event and who is invited, if anyone."""
    people = {}
    if 'ORGANIZER' in component:
        people['organizer'] = _person(component['ORGANIZER'])
    if 'ATTENDEE' in component:
        people['invitees'] = _invitees(component)
    return people


def _invitees(component: icalendar.Event) -> list[dict]:
    invitees = component.get('ATTENDEE', [])
    if not isinstance(invitees, list):
        invitees = [invitees]
    return [_person(invitee) for invitee in invitees]


def _person(address) -> dict:
    """Describe an organizer or invitee: address, name and reply."""
    person = {'email': re.sub('^mailto:', '', str(address), flags=re.I)}
    parameters = getattr(address, 'params', {})
    if parameters.get('CN'):
        person['name'] = str(parameters['CN'])
    if parameters.get('PARTSTAT'):
        person['status'] = str(parameters['PARTSTAT']).lower()
    return person


def _main(ical: icalendar.Calendar) -> icalendar.Event:
    """Return the event that best stands for a stored event: the one
    that repeats, or failing that the first occurrence held."""
    events = ical.events
    if not events:
        raise CalendarError("iCloud holds no event data under that UID.")
    for component in events:
        if 'RECURRENCE-ID' not in component:
            return component
    return events[0]


def _size(entry: dict) -> int:
    """Return roughly how many characters of text an entry holds."""
    return sum(len(str(value)) for value in entry.values())


def _is_busy(component: icalendar.Event) -> bool:
    """Whether an event takes up the time it covers."""
    free = str(component.get('TRANSP', '')).upper() == 'TRANSPARENT'
    cancelled = str(component.get('STATUS', '')).upper() == 'CANCELLED'
    return not free and not cancelled


def _moment(value: date | datetime, zone) -> datetime:
    """Place a start or end in time: a date or a floating time is taken
    to be in the given zone."""
    if not isinstance(value, datetime):
        return datetime.combine(value, time.min, zone)
    if value.tzinfo is None:
        return value.replace(tzinfo=zone)
    return value


def _merged(
    spans: list[tuple[datetime, datetime]]
) -> list[tuple[datetime, datetime]]:
    """Join spans of time that overlap or touch, earliest first."""
    merged = []
    for begins, ends in sorted(spans):
        if merged and begins <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], ends))
        else:
            merged.append((begins, ends))
    return merged


def _any_match(components: list[icalendar.Event], query: str) -> bool:
    return any(_matches(component, query) for component in components)


def _matches(component: icalendar.Event, query: str) -> bool:
    """Whether the text is in an event's title, place, notes or guest
    list."""
    texts = [
        str(component.get(name, ''))
        for name in ('SUMMARY', 'LOCATION', 'DESCRIPTION')
    ]
    for invitee in _invitees(component):
        texts += [invitee['email'], invitee.get('name', '')]
    return query.casefold() in ' '.join(texts).casefold()


def _sort_key(start: date | datetime) -> datetime:
    """Make all-day, floating and zoned starts comparable."""
    if not isinstance(start, datetime):
        return datetime.combine(start, time.min, UTC)
    if start.tzinfo is None:
        return start.replace(tzinfo=UTC)
    return start
