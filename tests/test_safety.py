"""Limits, timeouts and error handling (no network, synthetic data)."""

import gzip
import http.server
import json
import logging
import os
import socketserver
import subprocess
import sys
import threading
import time
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import caldav
import icalendar
import pytest
from starlette.testclient import TestClient

import icloud
import server

TOKEN = 'test-token-0123456789abcdefghijklmnopqrstuv'
HEADERS = {
    'accept': 'application/json, text/event-stream',
    'authorization': f'Bearer {TOKEN}',
}
INITIALIZE = {
    'jsonrpc': '2.0',
    'id': 1,
    'method': 'initialize',
    'params': {
        'protocolVersion': '2025-06-18',
        'capabilities': {},
        'clientInfo': {'name': 'test', 'version': '0'},
    },
}
# Stands in for anything private: a calendar name, event text, a URL.
SECRET = 'SYNTHETIC_PRIVATE_MARKER'

# Lists one stored event, read from stdin, in a process of its own: only
# there do Python's warnings go where they go on a real server. Logging
# is turned all the way up, to catch anything else that quotes the event.
LISTING = """
import json
import logging
import sys
from types import SimpleNamespace

import icalendar

import icloud
import server

logging.basicConfig(level=logging.DEBUG)
data = sys.stdin.read()


class Stored:
    @property
    def icalendar_instance(self):
        return icalendar.Calendar.from_ical(data)


calendar = SimpleNamespace(search=lambda **arguments: [Stored()])
icloud._find_calendar = lambda name, deadline: (calendar, 'Home')
events = server.list_events('2026-11-13', '2026-11-14', calendar='Home')
print(json.dumps(events))
"""


class FakeCalendar:
    """A calendar holding the given stored events, recording every call."""

    def __init__(self, name='Home', stored=()):
        self.name = name
        self.stored = list(stored)
        self.searches = []
        self.added = []

    def get_display_name(self):
        return self.name

    def search(self, **arguments):
        self.searches.append(arguments)
        return [
            SimpleNamespace(icalendar_instance=ical)
            for ical in self.stored
        ]

    def add_event(self, ical):
        self.added.append(ical)


@pytest.fixture
def no_icloud(monkeypatch):
    """Fail the test if the code tries to reach iCloud at all."""
    def refuse(*arguments, **keywords):
        raise AssertionError("iCloud must not be contacted")

    monkeypatch.setattr(icloud, '_principal', refuse)
    monkeypatch.setattr(icloud, '_find_calendar', refuse)


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv('MCP_AUTH_TOKEN', TOKEN)
    with TestClient(server.build_app()) as test_client:
        yield test_client


class Answers(http.server.BaseHTTPRequestHandler):
    """Stands in for iCloud: each path answers in a different shape."""

    SMALL = b'<multistatus xmlns="DAV:"/>'
    LARGE = b'<a>' + b'x' * 200_000 + b'</a>'

    def do_GET(self):
        body = self.SMALL if self.path == '/small' else self.LARGE
        self.send_response(207)
        self.send_header('Content-Type', 'text/xml')
        if self.path == '/gzip':
            # A few hundred bytes on the wire.
            body = gzip.compress(body)
            self.send_header('Content-Encoding', 'gzip')
        if self.path not in ('/unsized', '/trickle'):
            self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        if self.path != '/trickle':
            self.wfile.write(body)
            return
        try:
            # Never silent for long, and never finished.
            for _ in range(200):
                self.wfile.write(b' ')
                self.wfile.flush()
                time.sleep(0.05)
        except OSError:
            pass

    def log_message(self, *arguments):
        pass


@pytest.fixture
def answers():
    """Send a request through the limited session to a loopback server."""
    # Not http.server's own server class: that one looks its host name up
    # in DNS when it starts.
    httpd = socketserver.ThreadingTCPServer(('127.0.0.1', 0), Answers)
    httpd.daemon_threads = True
    threading.Thread(
        target=httpd.serve_forever,
        kwargs={'poll_interval': 0.01},
        daemon=True
    ).start()
    base = f'http://127.0.0.1:{httpd.server_address[1]}'
    connection = caldav.DAVClient(
        url=base,
        username='someone@example.invalid',
        password='not-a-real-password',
        timeout=5,
        # caldav would otherwise look the address up in DNS first.
        enable_rfc6764=False
    )
    icloud._limit_answers(connection)
    yield lambda path: connection.request(base + path)
    connection.close()
    httpd.shutdown()
    httpd.server_close()


@pytest.fixture
def logs():
    """Everything FastMCP and this server log, tracebacks included."""
    lines = []

    class Capture(logging.Handler):
        def emit(self, record):
            lines.append(self.format(record))

    handler = Capture(level=logging.DEBUG)
    logger = logging.getLogger('fastmcp')
    level = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    yield lines
    logger.removeHandler(handler)
    logger.setLevel(level)


# --- Errors stay out of replies and logs ------------------------------------

def test_unknown_calendar_does_not_list_the_others(
    client,
    logs,
    monkeypatch
):
    principal = SimpleNamespace(
        get_calendars=lambda: [FakeCalendar(name=SECRET)]
    )
    monkeypatch.setattr(icloud, '_principal', lambda: principal)
    reply = call_tool(client, 'delete_event', calendar='nope', uid='x')
    assert 'list_calendars' in reply
    assert SECRET not in reply
    assert SECRET not in '\n'.join(logs)


@pytest.mark.parametrize(
    ('error', 'expected'),
    [
        (
            caldav.error.DAVError(
                url=f'https://caldav.icloud.com/{SECRET}',
                reason=SECRET,
                body=SECRET,
                status=500
            ),
            "iCloud answered with an error.",
        ),
        (
            caldav.error.AuthorizationError(url=SECRET, reason=SECRET),
            "iCloud refused the request",
        ),
        (
            caldav.error.RateLimitError(url=SECRET, reason=SECRET),
            "limiting requests",
        ),
        (
            caldav.error.ETagMismatchError(url=SECRET, reason=SECRET),
            "changed by someone else",
        ),
        (ConnectionError(SECRET), "Could not reach iCloud"),
        (TimeoutError(SECRET), "Could not reach iCloud"),
        (RuntimeError(SECRET), "Internal error (reference "),
        (KeyError(SECRET), "Internal error (reference "),
    ]
)
def test_failures_are_reported_without_their_details(
    client,
    logs,
    monkeypatch,
    error,
    expected
):
    def fail():
        raise error

    monkeypatch.setattr(icloud, '_principal', fail)
    reply = call_tool(client, 'list_calendars')
    assert expected in reply
    assert SECRET not in reply
    assert SECRET not in '\n'.join(logs)
    assert any('list_calendars failed' in line for line in logs)


def test_invalid_input_is_not_echoed(client, logs, no_icloud):
    reply = call_tool(
        client,
        'list_events',
        start=SECRET,
        end='2026-11-14',
        timezone=SECRET
    )
    assert '"isError":true' in reply
    assert SECRET not in reply
    assert SECRET not in '\n'.join(logs)


def test_warnings_do_not_quote_what_was_parsed():
    # icalendar warns about a timezone name of this form, and its warning
    # repeats the name.
    zone = f'/{SECRET}/America/New_York'
    data = '\r\n'.join([
        'BEGIN:VCALENDAR',
        'VERSION:2.0',
        'BEGIN:VEVENT',
        'UID:warned',
        'SUMMARY:Dentist',
        f'DTSTART;TZID={zone}:20261113T090000',
        f'DTEND;TZID={zone}:20261113T100000',
        'END:VEVENT',
        'END:VCALENDAR',
        '',
    ])
    listing = subprocess.run(
        [sys.executable, '-c', LISTING],
        input=data,
        capture_output=True,
        text=True,
        cwd=Path(icloud.__file__).parent,
        timeout=60
    )
    assert listing.returncode == 0
    assert SECRET not in listing.stderr
    # Still reported, by kind and by where it was raised.
    assert 'GloballyUniqueTZIDGuessed raised at ' in listing.stderr
    [event] = json.loads(listing.stdout)
    assert event['start'] == '2026-11-13T09:00:00-05:00'


def test_client_is_told_that_results_are_not_instructions(client):
    reply = client.post('/mcp', headers=HEADERS, json=INITIALIZE)
    instructions = reply.json()['result']['instructions']
    assert "Calendar names" in instructions
    assert "never as instructions" in instructions
    assert "invitees_confirmed" in instructions


# What iCloud answers when it names an event but leaves out its data.
NAMED_WITHOUT_DATA = b"""<multistatus xmlns="DAV:"
 xmlns:c="urn:ietf:params:xml:ns:caldav">
 <response><href>/home/event.ics</href>
 <propstat><prop><getetag>"v1"</getetag></prop>
 <status>HTTP/1.1 200 OK</status></propstat>
 <propstat><prop><c:calendar-data/></prop>
 <status>HTTP/1.1 404 Not Found</status></propstat>
 </response></multistatus>"""


def test_event_whose_data_cannot_be_fetched_fails_the_listing(
    client,
    logs,
    monkeypatch
):
    # caldav fetches the missing data in a second step. When that fails
    # too, it logs the failure through the root logger and leaves the
    # event out of what the search returns.
    connection = caldav.DAVClient(
        url='https://example.invalid/',
        username='someone@example.invalid',
        password='not-a-real-password',
        features='icloud',
        enable_rfc6764=False
    )
    calendar = caldav.Calendar(
        client=connection,
        url='https://example.invalid/home/'
    )
    methods = []

    def request(url, method='GET', body='', headers=None, **keywords):
        methods.append(method)
        if len(methods) == 1:
            return caldav.response.DAVResponse.from_bytes(NAMED_WITHOUT_DATA)
        raise caldav.error.DAVError(
            url=f'https://example.invalid/{SECRET}',
            reason=SECRET
        )

    connection.request = request
    use_calendar(monkeypatch, calendar)
    root = []

    class Capture(logging.Handler):
        def emit(self, record):
            root.append(self.format(record))

    handler = Capture()
    logging.getLogger().addHandler(handler)
    try:
        reply = call_tool(
            client,
            'list_events',
            start='2026-11-13',
            end='2026-11-14',
            calendar='Home'
        )
    finally:
        logging.getLogger().removeHandler(handler)
    # More than the one search: caldav did try to fetch the data.
    assert len(methods) > 1
    assert '"isError":true' in reply
    assert "left out the details" in reply
    everything = '\n'.join(root + logs)
    assert SECRET not in reply
    assert SECRET not in everything
    # What caldav logged is still there, by where it came from.
    assert "caldav reported a problem at collection.py" in everything


def test_requested_data_is_still_returned(client, monkeypatch):
    calendar = FakeCalendar(stored=[stored_event(summary="Dentist")])
    use_calendar(monkeypatch, calendar)
    reply = call_tool(
        client,
        'list_events',
        start='2026-11-13',
        end='2026-11-14',
        calendar='Home'
    )
    assert 'Dentist' in reply
    assert '"isError":true' not in reply


# --- Searches and recurrences have a budget ---------------------------------

def test_range_longer_than_the_limit_is_refused_first(no_icloud):
    with pytest.raises(icloud.CalendarError, match="366 days"):
        icloud.list_events('1900-01-01', '2100-01-01')


@pytest.mark.parametrize(
    'field',
    ['summary', 'location', 'description', 'rrule', 'start', 'timezone']
)
def test_oversized_text_is_refused_first(no_icloud, field):
    arguments = {'summary': "Lunch", 'start': '2026-11-13'}
    arguments[field] = 'x' * (icloud.TEXT_LIMITS[field] + 1)
    with pytest.raises(icloud.CalendarError, match="too long"):
        icloud.create_event('Home', **arguments)


@pytest.mark.parametrize(
    'rule',
    [
        'FREQ=SECONDLY',
        'FREQ=MINUTELY',
        'FREQ=HOURLY',
        'FREQ=DAILY;INTERVAL=0',
        'FREQ=DAILY;COUNT=-1',
        'FREQ=DAILY;COUNT=0',
        'FREQ=DAILY;COUNT=2;UNTIL=20261120T000000Z',
        'FREQ=DAILY;BYSECOND=1,2,3',
        'FREQ=DAILY;BYHOUR=1,2,3',
        'FREQ=MONTHLY;BYMONTH=13',
        'FREQ=MONTHLY;BYMONTHDAY=40',
        'FREQ=DAILY;NONSENSE=1',
        'INTERVAL=2',
        'FREQ=DAILY;BYDAY=0MO',
        'FREQ=WEEKLY;BYDAY=2MO',
        'FREQ=MONTHLY;BYDAY=6MO',
        'FREQ=MONTHLY;BYDAY=99MO',
        'FREQ=DAILY;WKST=1MO',
    ]
)
def test_rrule_values_are_checked(rule):
    with pytest.raises(icloud.CalendarError):
        icloud._parse_rrule(rule)


@pytest.mark.parametrize(
    'rule',
    [
        'FREQ=DAILY',
        'FREQ=WEEKLY;BYDAY=MO,WE;INTERVAL=2',
        'FREQ=MONTHLY;COUNT=6',
        'FREQ=MONTHLY;BYDAY=-1FR',
        'FREQ=YEARLY;BYDAY=20MO',
        'FREQ=MONTHLY;BYDAY=MO,TU,WE,TH,FR;BYSETPOS=-1',
        'FREQ=YEARLY;BYMONTH=11;BYMONTHDAY=13',
        'FREQ=WEEKLY;UNTIL=20261231T000000Z;WKST=SU',
    ]
)
def test_ordinary_rrules_are_accepted(rule):
    assert icloud._parse_rrule(rule)['FREQ']


def test_recurring_event_is_listed_per_occurrence(monkeypatch):
    weekly = stored_event(
        summary="Standup",
        start=datetime(2026, 1, 5, 9, tzinfo=UTC),
        rrule='FREQ=WEEKLY'
    )
    once = stored_event(summary="Dentist")
    elsewhere = stored_event(
        summary="Too late",
        start=datetime(2026, 11, 30, 9, tzinfo=UTC)
    )
    calendar = FakeCalendar(stored=[weekly, once, elsewhere])
    use_calendar(monkeypatch, calendar)

    events = icloud.list_events('2026-11-09', '2026-11-23', calendar='Home')
    assert [(event['summary'], event['start'][:10]) for event in events] == [
        ('Standup', '2026-11-09'),
        ('Dentist', '2026-11-13'),
        ('Standup', '2026-11-16'),
    ]
    assert events[0]['recurring'] is True
    assert 'recurring' not in events[1]
    assert not any('occurrences_omitted' in event for event in events)
    # caldav must not be asked to expand or filter: that has no budget.
    assert calendar.searches[0]['expand'] is False
    assert calendar.searches[0]['post_filter'] is False


def test_all_day_event_is_listed_on_its_day(monkeypatch):
    holiday = stored_event(summary="Holiday", start=date(2026, 11, 13))
    calendar = FakeCalendar(stored=[holiday])
    use_calendar(monkeypatch, calendar)
    listed = icloud.list_events(
        '2026-11-13',
        '2026-11-14',
        calendar='Home',
        timezone='America/New_York'
    )
    assert [event['start'] for event in listed] == ['2026-11-13']
    missed = icloud.list_events(
        '2026-11-14',
        '2026-11-15',
        calendar='Home',
        timezone='America/New_York'
    )
    assert missed == []


@pytest.mark.parametrize(
    'rule',
    [
        # A step per second since 2000: minutes of work if expanded.
        'FREQ=SECONDLY',
        'FREQ=MINUTELY',
        'FREQ=DAILY;BYHOUR=1,2,3,4,5,6;BYMINUTE=0,10,20,30,40,50',
        # dateutil never finishes on this one.
        'FREQ=DAILY;INTERVAL=0',
    ]
)
def test_stored_dense_series_is_listed_once_unexpanded(monkeypatch, rule):
    expanded = []
    dense = stored_event(
        summary="Dense",
        start=datetime(2000, 1, 1, tzinfo=UTC),
        rrule=rule,
        check=False
    )
    calendar = FakeCalendar(stored=[dense, stored_event(summary="Dentist")])
    use_calendar(monkeypatch, calendar)
    monkeypatch.setattr(
        icloud.icalendar_searcher.Searcher,
        'check_component',
        record_expansions(expanded)
    )

    events = icloud.list_events('2026-11-09', '2026-11-23', calendar='Home')
    # Only the ordinary event reached the library that expands rules.
    assert expanded == ['Dentist']
    by_summary = {event['summary']: event for event in events}
    assert by_summary['Dense']['occurrences_omitted'] is True
    assert 'occurrences_omitted' not in by_summary['Dentist']


@pytest.mark.parametrize(
    'rule',
    [
        # None of these ever matches a date, and no estimate says so: the
        # library looks for the next occurrence all the way to year 9999.
        'FREQ=DAILY;BYMONTH=2;BYMONTHDAY=30',
        'FREQ=HOURLY;BYMONTH=2;BYMONTHDAY=30',
        'FREQ=MONTHLY;BYDAY=6MO',
    ]
)
def test_rule_that_never_matches_is_stopped_part_way(monkeypatch, rule):
    barren = stored_event(summary="Barren", rrule=rule, check=False)
    assert not icloud._too_costly(
        barren.events,
        datetime(2026, 11, 13, tzinfo=UTC),
        datetime(2026, 11, 14, tzinfo=UTC)
    )
    calendar = FakeCalendar(stored=[barren, stored_event(summary="Dentist")])
    use_calendar(monkeypatch, calendar)
    # Unstopped, the daily rule alone takes many seconds.
    monkeypatch.setattr(icloud, 'EXPANSION_SECONDS', 0.1)
    tracing = sys.gettrace()

    began = time.monotonic()
    events = icloud.list_events('2026-11-13', '2026-11-14', calendar='Home')
    assert time.monotonic() - began < 2
    by_summary = {event['summary']: event for event in events}
    assert by_summary['Barren']['occurrences_omitted'] is True
    assert 'occurrences_omitted' not in by_summary['Dentist']
    assert sys.gettrace() is tracing


def test_expansion_under_way_stops_at_the_deadline(monkeypatch):
    barren = stored_event(
        summary="Barren",
        rrule='FREQ=DAILY;BYMONTH=2;BYMONTHDAY=30',
        check=False
    )
    # Only the tool call's own deadline can stop it here.
    monkeypatch.setattr(icloud, 'EXPANSION_SECONDS', 60)
    began = time.monotonic()
    components, complete = icloud._occurrences(
        barren,
        datetime(2026, 11, 13, tzinfo=UTC),
        datetime(2026, 11, 14, tzinfo=UTC),
        began + 0.2
    )
    assert time.monotonic() - began < 2
    assert complete is False
    assert [str(event['SUMMARY']) for event in components] == ['Barren']

    # As the last event of a listing, it makes the call fail rather than
    # come back late.
    use_calendar(monkeypatch, FakeCalendar(stored=[barren]))
    monkeypatch.setattr(icloud, 'OPERATION_SECONDS', 0.2)
    with pytest.raises(icloud.CalendarError, match="too long"):
        icloud.list_events('2026-11-13', '2026-11-14', calendar='Home')


def test_long_events_count_for_as_long_as_they_last(monkeypatch):
    # Hourly, each occurrence lasting fifty days: 1200 of them overlap
    # any one hour.
    expanded = []
    overlapping = stored_event(
        summary="Overlapping",
        start=datetime(2026, 9, 24, tzinfo=UTC),
        duration=timedelta(days=50),
        rrule='FREQ=HOURLY',
        check=False
    )
    use_calendar(monkeypatch, FakeCalendar(stored=[overlapping]))
    monkeypatch.setattr(
        icloud.icalendar_searcher.Searcher,
        'check_component',
        record_expansions(expanded)
    )
    events = icloud.list_events(
        '2026-11-13T00:00:00Z',
        '2026-11-13T01:00:00Z',
        calendar='Home'
    )
    assert expanded == []
    assert [event['occurrences_omitted'] for event in events] == [True]


def test_explicit_dates_count_before_anything_is_expanded(monkeypatch):
    # No rule at all: 1500 dates the event is also to occur on.
    start = datetime(2026, 11, 13, tzinfo=UTC)
    dated = stored_event(summary="Dated", start=start)
    dated.events[0].add(
        'rdate',
        [start + timedelta(minutes=minute) for minute in range(1, 1501)]
    )
    expanded = []
    monkeypatch.setattr(
        icloud.icalendar_searcher.Searcher,
        'check_component',
        record_expansions(expanded)
    )
    components, complete = icloud._occurrences(
        dated,
        start,
        start + timedelta(days=2),
        icloud._deadline()
    )
    assert expanded == []
    assert complete is False
    assert [str(event['SUMMARY']) for event in components] == ['Dated']

    calendar = FakeCalendar(stored=[dated, stored_event(summary="Dentist")])
    use_calendar(monkeypatch, calendar)
    events = icloud.list_events('2026-11-13', '2026-11-15', calendar='Home')
    by_summary = {event['summary']: event for event in events}
    assert len(events) == 2
    assert by_summary['Dated']['occurrences_omitted'] is True
    assert 'occurrences_omitted' not in by_summary['Dentist']


def test_overrides_count_before_anything_is_expanded():
    start = datetime(2026, 11, 13, 15, tzinfo=UTC)
    series = stored_event(summary="Series", start=start, rrule='FREQ=DAILY')
    for day in range(1, icloud.MAX_EVENTS + 1):
        moved = icalendar.Event.new(
            uid='uid-Series',
            summary="Moved",
            start=start + timedelta(days=day, hours=1),
            end=start + timedelta(days=day, hours=2)
        )
        moved.add('recurrence-id', start + timedelta(days=day))
        series.add_component(moved)
    components, complete = icloud._occurrences(
        series,
        start,
        start + timedelta(days=1),
        icloud._deadline()
    )
    assert complete is False
    assert [str(event['SUMMARY']) for event in components] == ['Series']


def test_a_few_explicit_dates_are_listed_like_any_occurrence(monkeypatch):
    start = datetime(2026, 11, 13, 15, tzinfo=UTC)
    dated = stored_event(summary="Dated", start=start, rrule='FREQ=WEEKLY')
    dated.events[0].add(
        'rdate',
        [start + timedelta(days=1), start + timedelta(days=40)]
    )
    use_calendar(monkeypatch, FakeCalendar(stored=[dated]))
    events = icloud.list_events('2026-11-13', '2026-11-23', calendar='Home')
    assert [event['start'][:10] for event in events] == [
        '2026-11-13',
        '2026-11-14',
        '2026-11-20',
    ]
    assert not any('occurrences_omitted' in event for event in events)


def test_rules_of_one_event_are_costed_together():
    start = datetime(2026, 11, 13, tzinfo=UTC)
    end = start + timedelta(days=1)
    # About 61,000 steps each: within the budget alone, over it together.
    event = stored_event(
        summary="Twice",
        start=start - timedelta(days=2500),
        rrule='FREQ=HOURLY;COUNT=5',
        check=False
    )
    assert not icloud._too_costly(event.events, start, end)
    event.events[0].add('rrule', icalendar.vRecur.from_ical('FREQ=HOURLY'))
    assert len(icloud._rules(event.events[0])) == 2
    assert icloud._too_costly(event.events, start, end)


def test_too_many_events_is_an_error_not_a_partial_list(monkeypatch):
    daily = [
        stored_event(
            summary=f"Daily {number}",
            start=datetime(2026, 1, 1, 9, tzinfo=UTC),
            rrule='FREQ=DAILY'
        )
        for number in range(4)
    ]
    calendar = FakeCalendar(stored=daily)
    use_calendar(monkeypatch, calendar)
    with pytest.raises(icloud.CalendarError, match="More than 1000"):
        icloud.list_events('2026-01-01', '2027-01-01', calendar='Home')
    # A query that matches one series keeps the others from counting.
    events = icloud.list_events(
        '2026-01-01',
        '2027-01-01',
        calendar='Home',
        query='daily 3'
    )
    assert len(events) == 365


def test_too_much_text_is_an_error(monkeypatch):
    wordy = [
        stored_event(summary=f"Long {number}", description='x' * 9000)
        for number in range(40)
    ]
    calendar = FakeCalendar(stored=wordy)
    use_calendar(monkeypatch, calendar)
    converted = []
    convert = icloud._event_to_dict

    def counting(*arguments):
        converted.append(arguments)
        return convert(*arguments)

    monkeypatch.setattr(icloud, '_event_to_dict', counting)
    with pytest.raises(icloud.CalendarError, match="too much text"):
        icloud.list_events('2026-11-13', '2026-11-14', calendar='Home')
    # Refused at the event that went over, not after converting them all.
    assert len(converted) == icloud.MAX_RESPONSE_CHARS // 9000 + 1


def test_requests_share_one_bounded_pool():
    lock = threading.Lock()
    running = 0
    peak = 0

    def request(calendar):
        nonlocal running, peak
        with lock:
            running += 1
            peak = max(peak, running)
        time.sleep(0.01)
        with lock:
            running -= 1
        return calendar

    def tool_call():
        calendars = list(range(20))
        deadline = icloud._deadline()
        assert icloud._per_calendar(request, calendars, deadline) == calendars

    callers = [threading.Thread(target=tool_call) for _ in range(6)]
    for caller in callers:
        caller.start()
    for caller in callers:
        caller.join()
    assert 1 < peak <= icloud.MAX_PARALLEL_REQUESTS


def test_lookups_and_writes_use_the_pool_too(monkeypatch):
    lock = threading.Lock()
    running = 0
    peak = 0

    def request():
        nonlocal running, peak
        with lock:
            running += 1
            peak = max(peak, running)
        time.sleep(0.02)
        with lock:
            running -= 1

    class SlowCalendar(FakeCalendar):
        def add_event(self, ical):
            request()
            super().add_event(ical)

    calendar = SlowCalendar()

    def get_calendars():
        request()
        return [calendar]

    principal = SimpleNamespace(get_calendars=get_calendars)
    monkeypatch.setattr(icloud, '_principal', lambda: principal)
    callers = [
        threading.Thread(
            target=icloud.create_event,
            args=('Home', "Lunch", '2026-11-13')
        )
        for _ in range(12)
    ]
    for caller in callers:
        caller.start()
    for caller in callers:
        caller.join()
    assert len(calendar.added) == 12
    assert 1 < peak <= icloud.MAX_PARALLEL_REQUESTS


def test_oversized_request_body_is_refused(client):
    padding = 'x' * (server.MAX_BODY_BYTES + 1)
    reply = client.post(
        '/mcp',
        headers=HEADERS,
        json=INITIALIZE | {'padding': padding}
    )
    assert reply.status_code == 413

    def chunks():
        yield b'{"padding": "'
        for _ in range(server.MAX_BODY_BYTES // 1024 + 1):
            yield b'x' * 1024
        yield b'"}'

    # No Content-Length to go by: the limit has to count what arrives.
    reply = client.post(
        '/mcp',
        headers=HEADERS | {'content-type': 'application/json'},
        content=chunks()
    )
    assert reply.status_code == 413


# --- iCloud can't hold a tool call forever ----------------------------------

def test_client_has_a_timeout_and_no_rate_limit_sleep(monkeypatch):
    built = {}

    class FakeClient:
        def __init__(self, **arguments):
            built.update(arguments)
            built['client'] = self
            self.session = SimpleNamespace(close=lambda: None)

        def get_principal(self):
            return 'principal'

    monkeypatch.setenv('ICLOUD_USERNAME', 'someone@example.invalid')
    monkeypatch.setenv('ICLOUD_APP_PASSWORD', 'not-a-real-password')
    monkeypatch.setattr(caldav, 'DAVClient', FakeClient)
    icloud._principal.cache_clear()
    try:
        assert icloud._principal() == 'principal'
    finally:
        icloud._principal.cache_clear()
    assert built['timeout'] == icloud.REQUEST_TIMEOUT
    assert built['rate_limit_handle'] is False
    assert isinstance(built['client'].session, icloud._LimitedSession)

    # The real client keeps both settings where its requests read them.
    monkeypatch.undo()
    real = caldav.DAVClient(
        url=icloud.ICLOUD_URL,
        username='someone@example.invalid',
        password='not-a-real-password',
        features='icloud',
        timeout=icloud.REQUEST_TIMEOUT,
        rate_limit_handle=False
    )
    assert real.timeout == icloud.REQUEST_TIMEOUT
    assert real.rate_limit_handle is False


def test_stalled_calendar_ends_the_call_at_the_deadline():
    release = threading.Event()
    started = []

    def request(calendar):
        started.append(calendar)
        if calendar == 'stalled':
            release.wait(timeout=10)
        return calendar

    calendars = ['fine', 'stalled'] + ['queued'] * 30
    began = time.monotonic()
    try:
        with pytest.raises(icloud.CalendarError, match="too long"):
            icloud._per_calendar(request, calendars, began + 0.2)
        assert time.monotonic() - began < 2
    finally:
        release.set()
    # The pool is usable again once the stalled request lets go.
    deadline = icloud._deadline()
    assert icloud._per_calendar(request, ['next'], deadline) == ['next']


def test_expansion_stops_at_the_deadline(monkeypatch):
    calendar = FakeCalendar(stored=[stored_event(summary="Dentist")])
    use_calendar(monkeypatch, calendar)
    monkeypatch.setattr(icloud, 'OPERATION_SECONDS', -1)
    with pytest.raises(icloud.CalendarError, match="too long"):
        icloud.list_events('2026-11-13', '2026-11-14', calendar='Home')


@pytest.mark.parametrize(
    'call',
    [
        lambda: icloud.list_calendars(),
        lambda: icloud.list_events('2026-11-13', '2026-11-14'),
        lambda: icloud.create_event('Home', "Lunch", '2026-11-13'),
        lambda: icloud.update_event('Home', 'abc', summary="Renamed"),
        lambda: icloud.delete_event('Home', 'abc'),
    ]
)
def test_every_tool_gives_up_at_the_deadline(monkeypatch, call):
    release = threading.Event()

    def get_calendars():
        release.wait(timeout=10)
        return []

    principal = SimpleNamespace(get_calendars=get_calendars)
    monkeypatch.setattr(icloud, '_principal', lambda: principal)
    monkeypatch.setattr(icloud, 'OPERATION_SECONDS', 0.2)
    began = time.monotonic()
    try:
        with pytest.raises(icloud.CalendarError, match="too long"):
            call()
        assert time.monotonic() - began < 2
    finally:
        release.set()


def test_nothing_is_written_once_time_is_up(monkeypatch):
    calendar = FakeCalendar()
    use_calendar(monkeypatch, calendar)
    monkeypatch.setattr(icloud, 'OPERATION_SECONDS', -1)
    with pytest.raises(icloud.CalendarError, match="too long"):
        icloud.create_event('Home', "Lunch", '2026-11-13')
    assert calendar.added == []


def test_write_still_queued_at_the_deadline_is_never_sent():
    release = threading.Event()
    sent = []
    busy = [
        icloud._pool.submit(release.wait, 10)
        for _ in range(icloud.MAX_PARALLEL_REQUESTS)
    ]
    try:
        with pytest.raises(icloud.CalendarError, match="too long"):
            icloud._write(lambda: sent.append(1), time.monotonic() + 0.2)
    finally:
        release.set()
    for future in busy:
        future.result(timeout=10)
    # The pool is free again, and the write did not run late.
    assert icloud._ask(lambda: 'next', icloud._deadline()) == 'next'
    assert sent == []


def test_write_under_way_at_the_deadline_is_unconfirmed():
    release = threading.Event()
    try:
        with pytest.raises(icloud.CalendarError, match="may or may not"):
            icloud._write(lambda: release.wait(10), time.monotonic() + 0.2)
    finally:
        release.set()


# --- iCloud can't send more than the server will hold -----------------------

def test_ordinary_answer_is_read_whole(answers):
    reply = answers('/small')
    assert reply.status == 207
    assert reply.tree.tag == '{DAV:}multistatus'


@pytest.mark.parametrize('path', ['/large', '/unsized', '/gzip'])
def test_oversized_answer_is_cut_off(monkeypatch, answers, path):
    monkeypatch.setattr(icloud, 'MAX_ICLOUD_BYTES', 64 * 1024)
    with pytest.raises(icloud._TooMuchData) as raised:
        answers(path)
    assert "more data" in icloud.safe_message(raised.value)
    # The connection is usable for the next request.
    assert answers('/small').status == 207


def test_answer_that_trickles_in_is_cut_off(monkeypatch, answers):
    monkeypatch.setattr(icloud, 'OPERATION_SECONDS', 0.3)
    began = time.monotonic()
    with pytest.raises(TimeoutError):
        answers('/trickle')
    assert time.monotonic() - began < 3


# --- Changes and deletes don't destroy someone else's change ----------------

def test_delete_names_the_version_it_looked_up(monkeypatch):
    requests = stub_event(monkeypatch, etag='"version-1"', status=204)
    assert icloud.delete_event('Home', 'abc') == {'deleted': 'abc'}
    assert [request['method'] for request in requests] == ['DELETE']
    sent = {
        name.lower(): value
        for name, value in requests[0]['headers'].items()
    }
    assert sent['if-match'] == '"version-1"'


def test_delete_of_a_changed_event_is_a_conflict(monkeypatch):
    requests = stub_event(monkeypatch, etag='"version-1"', status=412)
    with pytest.raises(caldav.error.ETagMismatchError) as raised:
        icloud.delete_event('Home', 'abc')
    assert "changed by someone else" in icloud.safe_message(raised.value)
    # Refused once, and not tried again against the newer version.
    assert len(requests) == 1


def test_delete_goes_by_the_etag_not_the_schedule_tag(monkeypatch):
    requests = stub_event(
        monkeypatch,
        etag='"version-1"',
        status=204,
        schedule_tag='"schedule-1"'
    )
    icloud.delete_event('Home', 'abc')
    assert sent_conditions(requests) == [{'if-match': '"version-1"'}]


# A Schedule-Tag is no substitute: it survives some changes to the event.
@pytest.mark.parametrize('schedule_tag', [None, '"schedule-1"'])
@pytest.mark.parametrize('etag', [None, 'W/"weak-version"'])
def test_delete_without_a_usable_version_does_nothing(
    monkeypatch,
    etag,
    schedule_tag
):
    requests = stub_event(
        monkeypatch,
        etag=etag,
        status=204,
        schedule_tag=schedule_tag
    )
    with pytest.raises(icloud.CalendarError, match="no version"):
        icloud.delete_event('Home', 'abc')
    assert requests == []


def test_update_names_the_version_it_looked_up(monkeypatch):
    requests = stub_event(
        monkeypatch,
        etag='"version-1"',
        status=204,
        schedule_tag='"schedule-1"'
    )
    updated = icloud.update_event('Home', 'abc', summary="Renamed")
    assert updated['summary'] == "Renamed"
    assert [request['method'] for request in requests] == ['PUT']
    assert sent_conditions(requests) == [{'if-match': '"version-1"'}]


@pytest.mark.parametrize('schedule_tag', [None, '"schedule-1"'])
@pytest.mark.parametrize('etag', [None, 'W/"weak-version"'])
def test_update_without_a_usable_version_does_nothing(
    monkeypatch,
    etag,
    schedule_tag
):
    requests = stub_event(
        monkeypatch,
        etag=etag,
        status=204,
        schedule_tag=schedule_tag
    )
    with pytest.raises(icloud.CalendarError, match="no version"):
        icloud.update_event('Home', 'abc', summary="Renamed")
    assert requests == []


# --- An event with invitees is changed only after the user agreed -----------

def test_events_with_invitees_are_marked_in_listings(monkeypatch):
    calendar = FakeCalendar(stored=[
        stored_event(summary="Meeting", invitee=True),
        stored_event(summary="Dentist"),
    ])
    use_calendar(monkeypatch, calendar)
    events = icloud.list_events('2026-11-13', '2026-11-14', calendar='Home')
    by_summary = {event['summary']: event for event in events}
    assert by_summary['Meeting']['has_invitees'] is True
    assert 'has_invitees' not in by_summary['Dentist']


@pytest.mark.parametrize(
    'change',
    [
        lambda **more: icloud.update_event('Home', 'abc', "Renamed", **more),
        lambda **more: icloud.delete_event('Home', 'abc', **more),
    ]
)
def test_event_with_invitees_needs_confirming(monkeypatch, change):
    requests = stub_event(
        monkeypatch,
        etag='"version-1"',
        status=204,
        invitee=True
    )
    with pytest.raises(icloud.CalendarError, match="may notify"):
        change()
    assert requests == []
    change(invitees_confirmed=True)
    assert len(requests) == 1
    assert sent_conditions(requests) == [{'if-match': '"version-1"'}]


def test_confirmation_is_asked_for_through_the_tool(client, monkeypatch):
    requests = stub_event(
        monkeypatch,
        etag='"version-1"',
        status=204,
        invitee=True
    )
    reply = call_tool(client, 'delete_event', calendar='Home', uid='abc')
    assert '"isError":true' in reply
    assert "invitees_confirmed" in reply
    assert requests == []
    reply = call_tool(
        client,
        'delete_event',
        calendar='Home',
        uid='abc',
        invitees_confirmed=True
    )
    assert '"isError":true' not in reply
    assert [request['method'] for request in requests] == ['DELETE']


# --- Nothing is written before the arguments are known to be good -----------

def test_invalid_timezone_writes_nothing(monkeypatch):
    calendar = FakeCalendar()
    use_calendar(monkeypatch, calendar)
    with pytest.raises(icloud.CalendarError, match="Unknown timezone"):
        icloud.create_event(
            'Home',
            "All day",
            '2026-11-13',
            timezone='Mars/Invalid'
        )
    assert calendar.added == []


def test_invalid_timezone_on_update_is_refused_first(no_icloud):
    with pytest.raises(icloud.CalendarError, match="Unknown timezone"):
        icloud.update_event(
            'Home',
            'abc',
            summary="Renamed",
            timezone='Mars/Invalid'
        )


def test_create_writes_once_and_returns_the_event(monkeypatch):
    calendar = FakeCalendar()
    use_calendar(monkeypatch, calendar)
    created = icloud.create_event(
        'Home',
        "All day",
        '2026-11-13',
        timezone='America/New_York'
    )
    assert created['start'] == '2026-11-13'
    assert len(calendar.added) == 1
    assert created['uid'] in calendar.added[0]


def test_unconfirmed_write_says_so(monkeypatch):
    calendar = FakeCalendar()

    def drop(ical):
        raise ConnectionError(SECRET)

    calendar.add_event = drop
    use_calendar(monkeypatch, calendar)
    with pytest.raises(icloud.CalendarError, match="may or may not"):
        icloud.create_event('Home', "Lunch", '2026-11-13')


# --- Host and Origin --------------------------------------------------------

@pytest.mark.parametrize(
    ('extra', 'status'),
    [
        ({}, 200),
        ({'host': 'localhost:8000'}, 200),
        ({'host': 'untrusted.example'}, 421),
        ({'origin': 'https://untrusted.example'}, 403),
        ({'origin': 'null'}, 403),
        ({'host': '127.0.0.1:8000', 'origin': 'http://localhost:6274'}, 200),
    ]
)
def test_local_app_checks_host_and_origin(client, extra, status):
    reply = client.post('/mcp', headers=HEADERS | extra, json=INITIALIZE)
    assert reply.status_code == status


@pytest.mark.parametrize(
    ('extra', 'status'),
    [
        ({'host': 'someone--icloud-calendar-mcp-web.modal.run'}, 200),
        ({'host': 'untrusted.example'}, 421),
        ({'host': 'modal.run.untrusted.example'}, 421),
        (
            {
                'host': 'someone--icloud-calendar-mcp-web.modal.run',
                'origin': 'https://untrusted.example',
            },
            403,
        ),
    ]
)
def test_deployed_app_checks_host_and_origin(monkeypatch, extra, status):
    monkeypatch.setenv('MCP_AUTH_TOKEN', TOKEN)
    app = server.build_app(allowed_hosts=['*.modal.run'])
    with TestClient(app) as deployed:
        reply = deployed.post(
            '/mcp',
            headers=HEADERS | extra,
            json=INITIALIZE
        )
    assert reply.status_code == status


def test_token_is_checked_before_host_and_body(client):
    headers = HEADERS | {
        'authorization': 'Bearer wrong',
        'host': 'untrusted.example',
    }
    padding = 'x' * (server.MAX_BODY_BYTES + 1)
    reply = client.post('/mcp', headers=headers, json={'padding': padding})
    assert reply.status_code == 401


# --- Helpers ----------------------------------------------------------------

def call_tool(client: TestClient, name: str, **arguments) -> str:
    """Call a tool over HTTP and return the raw reply text."""
    reply = client.post(
        '/mcp',
        headers=HEADERS,
        json={
            'jsonrpc': '2.0',
            'id': 2,
            'method': 'tools/call',
            'params': {'name': name, 'arguments': arguments},
        }
    )
    assert reply.status_code == 200
    return reply.text


def use_calendar(monkeypatch, calendar) -> None:
    """Have every calendar name resolve to the given calendar."""
    monkeypatch.setattr(
        icloud,
        '_find_calendar',
        lambda name, deadline: (calendar, 'Home')
    )


def stored_event(
    summary: str,
    start: date | datetime = datetime(2026, 11, 13, 15, tzinfo=UTC),
    rrule: str | None = None,
    description: str | None = None,
    check: bool = True,
    duration: timedelta | None = None,
    invitee: bool = False
) -> icalendar.Calendar:
    """Build an event as another client might have stored it."""
    if duration is not None:
        end = start + duration
    elif isinstance(start, datetime):
        end = start + timedelta(hours=1)
    else:
        end = start + timedelta(days=1)
    event = icalendar.Event.new(
        uid=f'uid-{summary}',
        summary=summary,
        start=start,
        end=end,
        description=description
    )
    if rrule and check:
        event.add('rrule', icloud._parse_rrule(rrule))
    elif rrule:
        event.add('rrule', icalendar.vRecur.from_ical(rrule))
    if invitee:
        event.add('organizer', 'mailto:owner@example.invalid')
        event.add('attendee', 'mailto:guest@example.invalid')
    return icloud._wrap(event)


def record_expansions(expanded: list[str]):
    """Wrap Searcher.check_component to note which events it is given."""
    original = icloud.icalendar_searcher.Searcher.check_component

    def check_component(self, component, *arguments, **keywords):
        expanded.extend(str(event['SUMMARY']) for event in component.events)
        return original(self, component, *arguments, **keywords)

    return check_component


def stub_event(
    monkeypatch,
    etag: str | None,
    status: int,
    schedule_tag: str | None = None,
    invitee: bool = False
) -> list[dict]:
    """Make 'abc' in 'Home' a real caldav event on a fake connection.

    Returns the list that collects the requests caldav sends for it.
    """
    requests = []

    def request(url, method='GET', body='', headers=None, **keywords):
        requests.append({'method': method, 'headers': headers})
        return SimpleNamespace(
            status=status,
            headers={},
            reason='synthetic',
            raw='',
            tree=None
        )

    connection = caldav.DAVClient(
        url=icloud.ICLOUD_URL,
        username='someone@example.invalid',
        password='not-a-real-password',
        features='icloud'
    )
    connection.request = request
    parent = caldav.Calendar(
        client=connection,
        url=f'{icloud.ICLOUD_URL}/synthetic/'
    )
    event = caldav.Event(
        client=connection,
        url=f'{icloud.ICLOUD_URL}/synthetic/abc.ics',
        data=stored_event(
            summary="Dentist",
            invitee=invitee
        ).to_ical().decode(),
        parent=parent
    )
    if etag is not None:
        event.props['{DAV:}getetag'] = etag
    if schedule_tag is not None:
        tag = '{urn:ietf:params:xml:ns:caldav}schedule-tag'
        event.props[tag] = schedule_tag
    calendar = SimpleNamespace(get_event_by_uid=lambda uid: event)
    use_calendar(monkeypatch, calendar)
    return requests


def sent_conditions(requests: list[dict]) -> list[dict]:
    """Return the conditional headers of each request, names lowercased."""
    return [
        {
            name.lower(): value
            for name, value in request['headers'].items()
            if name.lower().startswith('if-')
        }
        for request in requests
    ]
