"""iCloud calendar operations over CalDAV.

Times cross this module's boundary as ISO 8601 strings. A date-only string
('2026-11-13') means an all-day event; anything else is a timed event.

Failures the caller can act on are raised as CalendarError, whose message
never contains calendar data, the caller's input or anything iCloud sent
back. `safe_message` gives the same guarantee for errors from the libraries
underneath.
"""

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
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import caldav
import caldav.lib.error
import icalendar
import icalendar_searcher
import niquests

ICLOUD_URL = 'https://caldav.icloud.com'
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
}

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
    "was done. Look at it again with list_events before retrying."
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
#
# caldav sets this level too, to WARNING, in a module it doesn't load
# until something needs it (caldav.lib.error). That is why this file
# imports that module by name at the top: left to load later, on the
# first use of a calendar, it would undo this line.
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


# --- The five operations ----------------------------------------------------

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

    found = []
    count = 0
    size = 0
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
                entry = _event_to_dict(component, name, timezone)
                if not complete:
                    entry['occurrences_omitted'] = True
                size += sum(len(str(value)) for value in entry.values())
                if size > MAX_RESPONSE_CHARS:
                    raise CalendarError(TOO_MUCH_TEXT)
                found.append((_sort_key(component.start), entry))

    # The last event's expansion may be what ran out the clock.
    _check_deadline(deadline)
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

    cal, name = _find_calendar(calendar, deadline)
    created = _event_to_dict(event, name, timezone)
    _write(lambda: cal.add_event(ical=data), deadline)
    return created


def update_event(
    calendar: str,
    uid: str,
    summary: str | None = None,
    start: str | None = None,
    end: str | None = None,
    timezone: str | None = None,
    location: str | None = None,
    description: str | None = None,
    invitees_confirmed: bool = False
) -> dict:
    """Change the given fields of an event and return the result.

    Fields left as None are untouched; an empty string clears location or
    description. A recurring event is changed as a whole series. An event
    with invitees is changed only when invitees_confirmed is true.
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
    deadline = _deadline()
    _zone(timezone)
    cal, name = _find_calendar(calendar, deadline)
    event = _find_event(cal, uid, deadline)
    _version(event)
    _check_invitees(event, invitees_confirmed)
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
    updated = _event_to_dict(component, name, timezone)
    # 'etag' has caldav send the version it fetched as the condition (it
    # was checked above that there is one), so iCloud refuses the save if
    # the event changed in between.
    _write(lambda: event.save(precondition='etag'), deadline)
    return updated


def delete_event(
    calendar: str,
    uid: str,
    invitees_confirmed: bool = False
) -> dict:
    """Delete an event (the whole series, if it recurs).

    An event with invitees is deleted only when invitees_confirmed is
    true.
    """
    _check_lengths(calendar=calendar, uid=uid)
    deadline = _deadline()
    cal, _ = _find_calendar(calendar, deadline)
    event = _find_event(cal, uid, deadline)
    _check_invitees(event, invitees_confirmed)
    # Only the version that was just looked up: if another client changed
    # the event since, iCloud refuses and nothing is lost.
    etag = _version(event)
    _write(lambda: event.delete(if_match=etag), deadline)
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
    if isinstance(error, _TooMuchData):
        return TOO_MUCH_DATA
    # niquests raises OSError subclasses for timeouts and lost connections.
    if isinstance(error, OSError):
        return "Could not reach iCloud, or it stopped answering."
    return None


# --- iCloud lookups ---------------------------------------------------------

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


def _version(event: caldav.Event) -> str:
    """Return the ETag naming exactly the version that was looked up.

    A change or delete is sent on condition that the event still has it.
    Left to itself caldav settles for less when there is no strong ETag:
    a Schedule-Tag, which stays the same through some changes (RFC 6638),
    or for a save no condition at all. Here that is refused instead.
    """
    etag = event.etag
    if not etag or etag.startswith('W/'):
        raise CalendarError(NO_VERSION)
    return etag


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


def _write(change, deadline: float) -> None:
    """Make one change at iCloud, or say so if its outcome is unknown.

    A change that has not been sent by the deadline never is: this stops
    waiting for it then, and takes it back out of the queue.
    """
    _check_deadline(deadline)
    future = _pool.submit(_in_time, change, deadline)
    try:
        future.result(timeout=max(0, deadline - clock.monotonic()))
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
    """Reads iCloud's answers a piece at a time, up to a size and a time.

    caldav buffers a whole answer however large it is, and REQUEST_TIMEOUT
    only limits the silence between two pieces, not how long an answer
    that keeps trickling in may take.
    """

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
    if 'ATTENDEE' in component:
        entry['has_invitees'] = True
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
