"""Limits, timeouts and error handling (no network, synthetic data)."""

import logging
import threading
import time
from datetime import UTC, date, datetime, timedelta
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


def test_requested_data_is_still_returned(client, monkeypatch):
    calendar = FakeCalendar(stored=[stored_event(summary="Dentist")])
    monkeypatch.setattr(icloud, '_find_calendar', lambda name: calendar)
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
    monkeypatch.setattr(icloud, '_find_calendar', lambda name: calendar)

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
    monkeypatch.setattr(icloud, '_find_calendar', lambda name: calendar)
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
    monkeypatch.setattr(icloud, '_find_calendar', lambda name: calendar)
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
    monkeypatch.setattr(icloud, '_find_calendar', lambda name: calendar)
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
    monkeypatch.setattr(icloud, '_find_calendar', lambda name: calendar)
    with pytest.raises(icloud.CalendarError, match="too much text"):
        icloud.list_events('2026-11-13', '2026-11-14', calendar='Home')


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
    monkeypatch.setattr(icloud, '_find_calendar', lambda name: calendar)
    monkeypatch.setattr(icloud, 'OPERATION_SECONDS', -1)
    with pytest.raises(icloud.CalendarError, match="too long"):
        icloud.list_events('2026-11-13', '2026-11-14', calendar='Home')


# --- Deletes don't destroy someone else's change ----------------------------

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


@pytest.mark.parametrize('etag', [None, 'W/"weak-version"'])
def test_delete_without_a_usable_version_does_nothing(monkeypatch, etag):
    requests = stub_event(monkeypatch, etag=etag, status=204)
    with pytest.raises(icloud.CalendarError, match="can't be deleted safely"):
        icloud.delete_event('Home', 'abc')
    assert requests == []


# --- Nothing is written before the arguments are known to be good -----------

def test_invalid_timezone_writes_nothing(monkeypatch):
    calendar = FakeCalendar()
    monkeypatch.setattr(icloud, '_find_calendar', lambda name: calendar)
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
    monkeypatch.setattr(icloud, '_find_calendar', lambda name: calendar)
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
    monkeypatch.setattr(icloud, '_find_calendar', lambda name: calendar)
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


def stored_event(
    summary: str,
    start: date | datetime = datetime(2026, 11, 13, 15, tzinfo=UTC),
    rrule: str | None = None,
    description: str | None = None,
    check: bool = True
) -> icalendar.Calendar:
    """Build an event as another client might have stored it."""
    if isinstance(start, datetime):
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
    return icloud._wrap(event)


def record_expansions(expanded: list[str]):
    """Wrap Searcher.check_component to note which events it is given."""
    original = icloud.icalendar_searcher.Searcher.check_component

    def check_component(self, component, *arguments, **keywords):
        expanded.extend(str(event['SUMMARY']) for event in component.events)
        return original(self, component, *arguments, **keywords)

    return check_component


def stub_event(monkeypatch, etag: str | None, status: int) -> list[dict]:
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
    event = caldav.Event(
        client=connection,
        url=f'{icloud.ICLOUD_URL}/synthetic/abc.ics'
    )
    if etag is not None:
        event.props['{DAV:}getetag'] = etag
    calendar = SimpleNamespace(get_event_by_uid=lambda uid: event)
    monkeypatch.setattr(icloud, '_find_calendar', lambda name: calendar)
    return requests
