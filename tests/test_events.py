"""Single occurrences, busy times, one event in full and event details
(no network)."""

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import icalendar
import pytest

import icloud
from test_safety import (
    FakeCalendar,
    no_icloud,
    sent_conditions,
    stored_event,
    stub_event,
    use_calendar
)

NEW_YORK = ZoneInfo('America/New_York')
# Mondays at 09:00 in New York, from 2026-11-09. In UTC that is 14:00.
WEEKLY = 'FREQ=WEEKLY;COUNT=10'
FIRST = '2026-11-09T14:00:00+00:00'
SECOND = '2026-11-16T14:00:00+00:00'
THIRD = '2026-11-23T14:00:00+00:00'


# --- A recurring event is changed only as far as the caller said ------------

@pytest.mark.parametrize(
    'change',
    [
        lambda: icloud.update_event('Home', 'abc', 'version-1', "Renamed"),
        lambda: icloud.delete_event('Home', 'abc', 'version-1'),
    ]
)
def test_recurring_event_is_not_changed_on_a_guess(monkeypatch, change):
    requests = stub_event(monkeypatch, stored=series())
    with pytest.raises(icloud.CalendarError, match="This event repeats"):
        change()
    assert requests == []


@pytest.mark.parametrize(
    ('scope', 'occurrence'),
    [
        ('occurrence', None),
        ('following', None),
        ('series', SECOND),
        (None, SECOND),
        ('everything', None),
        ('occurrence', 'next monday'),
    ]
)
def test_scope_and_occurrence_are_checked_first(no_icloud, scope, occurrence):
    with pytest.raises(icloud.CalendarError):
        icloud.delete_event(
            'Home',
            'abc',
            'version-1',
            occurrence=occurrence,
            scope=scope
        )


def test_event_that_does_not_repeat_has_no_occurrence_to_pick(monkeypatch):
    requests = stub_event(monkeypatch)
    with pytest.raises(icloud.CalendarError, match="does not repeat"):
        icloud.delete_event(
            'Home',
            'abc',
            'version-1',
            occurrence='2026-11-13T15:00:00+00:00',
            scope='occurrence'
        )
    assert requests == []
    icloud.delete_event('Home', 'abc', 'version-1', scope='series')
    assert [request['method'] for request in requests] == ['DELETE']


def test_whole_series_is_deleted_when_asked_for(monkeypatch):
    requests = stub_event(monkeypatch, stored=series())
    deleted = icloud.delete_event('Home', 'abc', 'version-1', scope='series')
    assert deleted == {'deleted': 'abc'}
    assert [request['method'] for request in requests] == ['DELETE']


def test_listing_names_each_occurrence(monkeypatch):
    use_calendar(monkeypatch, FakeCalendar(stored=[series()]))
    events = icloud.list_events('2026-11-09', '2026-11-24', calendar='Home')
    assert [event['occurrence'] for event in events] == [FIRST, SECOND, THIRD]
    # The times themselves come in the timezone that was asked for.
    assert all('occurrence' in event for event in events)
    assert all(event['recurring'] for event in events)


def test_event_that_does_not_repeat_names_no_occurrence(monkeypatch):
    calendar = FakeCalendar(stored=[stored_event(summary="Dentist")])
    use_calendar(monkeypatch, calendar)
    events = icloud.list_events('2026-11-13', '2026-11-14', calendar='Home')
    assert 'occurrence' not in events[0]


# --- One occurrence ---------------------------------------------------------

def test_one_occurrence_is_deleted_by_excluding_it(monkeypatch):
    requests = stub_event(monkeypatch, stored=series())
    deleted = icloud.delete_event(
        'Home',
        'abc',
        'version-1',
        occurrence=SECOND,
        scope='occurrence'
    )
    assert deleted == {
        'deleted': 'abc',
        'scope': 'occurrence',
        'occurrence': SECOND,
    }
    # The series is saved without that occurrence, not deleted.
    assert [request['method'] for request in requests] == ['PUT']
    assert sent_conditions(requests) == [{'if-match': '"version-1"'}]
    saved = sent_event(requests)
    assert len(saved.events) == 1
    excluded = datetime(2026, 11, 16, 9, tzinfo=NEW_YORK)
    assert saved.events[0].exdates == [excluded]
    assert 'RRULE' in saved.events[0]
    assert starts(saved) == ['2026-11-09', '2026-11-23', '2026-11-30']


def test_all_day_occurrence_is_excluded_by_its_date(monkeypatch):
    stored = stored_event(
        summary="Bins",
        start=date(2026, 11, 9),
        rrule='FREQ=WEEKLY'
    )
    requests = stub_event(monkeypatch, stored=stored)
    icloud.delete_event(
        'Home',
        'abc',
        'version-1',
        occurrence='2026-11-16',
        scope='occurrence'
    )
    assert sent_event(requests).events[0].exdates == [date(2026, 11, 16)]


def test_occurrence_of_a_series_without_a_timezone_is_found(monkeypatch):
    stored = stored_event(
        summary="Wake up",
        start=datetime(2026, 11, 9, 7),
        rrule='FREQ=DAILY'
    )
    use_calendar(monkeypatch, FakeCalendar(stored=[stored]))
    events = icloud.list_events('2026-11-10', '2026-11-11', calendar='Home')
    assert [event['occurrence'] for event in events] == ['2026-11-10T07:00:00']

    requests = stub_event(monkeypatch, stored=stored)
    icloud.delete_event(
        'Home',
        'abc',
        'version-1',
        occurrence=events[0]['occurrence'],
        scope='following'
    )
    saved = sent_event(requests).events[0]
    assert saved['RRULE']['UNTIL'] == [datetime(2026, 11, 10, 6, 59, 59)]


def test_one_occurrence_is_changed_on_its_own(monkeypatch):
    requests = stub_event(monkeypatch, stored=series(alert=True))
    updated = icloud.update_event(
        'Home',
        'abc',
        'version-1',
        summary="Standup (moved)",
        start='2026-11-16T11:00:00',
        timezone='America/New_York',
        occurrence=SECOND,
        scope='occurrence'
    )
    assert updated['summary'] == "Standup (moved)"
    assert updated['start'] == '2026-11-16T11:00:00-05:00'
    assert updated['end'] == '2026-11-16T11:30:00-05:00'
    # Still named by where the rule put it.
    assert updated['occurrence'] == SECOND

    assert [request['method'] for request in requests] == ['PUT']
    repeating, changed = sent_event(requests).events
    assert str(repeating['SUMMARY']) == "Standup"
    assert repeating.start == datetime(2026, 11, 9, 9, tzinfo=NEW_YORK)
    assert 'RRULE' in repeating
    assert 'RECURRENCE-ID' not in repeating
    assert str(changed['UID']) == str(repeating['UID'])
    assert changed['RECURRENCE-ID'].dt == datetime(
        2026,
        11,
        16,
        9,
        tzinfo=NEW_YORK
    )
    assert 'RRULE' not in changed
    # It starts as a copy of the event, alerts included.
    assert icloud._alerts(changed) == [15]


def test_changed_occurrence_is_changed_again_in_place(monkeypatch):
    requests = stub_event(monkeypatch, stored=series(moved=True))
    icloud.update_event(
        'Home',
        'abc',
        'version-1',
        summary="Renamed again",
        occurrence=SECOND,
        scope='occurrence'
    )
    repeating, changed = sent_event(requests).events
    assert str(changed['SUMMARY']) == "Renamed again"
    # Where it had been moved to stays.
    assert changed.start == datetime(2026, 11, 17, 9, tzinfo=NEW_YORK)


def test_moved_occurrence_is_deleted_with_its_details(monkeypatch):
    requests = stub_event(monkeypatch, stored=series(moved=True))
    icloud.delete_event(
        'Home',
        'abc',
        'version-1',
        occurrence=SECOND,
        scope='occurrence'
    )
    saved = sent_event(requests)
    assert len(saved.events) == 1
    assert starts(saved) == ['2026-11-09', '2026-11-23', '2026-11-30']


def test_changing_the_series_leaves_changed_occurrences_alone(monkeypatch):
    requests = stub_event(monkeypatch, stored=series(moved=True))
    icloud.update_event(
        'Home',
        'abc',
        'version-1',
        summary="Daily sync",
        scope='series'
    )
    repeating, changed = sent_event(requests).events
    assert str(repeating['SUMMARY']) == "Daily sync"
    assert str(changed['SUMMARY']) == "Standup"


@pytest.mark.parametrize(
    'occurrence',
    [
        # A Tuesday, an hour off, and the wrong kind of value.
        '2026-11-17T14:00:00+00:00',
        '2026-11-16T15:00:00+00:00',
        '2026-11-16',
        '2026-11-16T09:00:00',
    ]
)
def test_occurrence_the_series_does_not_have_is_refused(
    monkeypatch,
    occurrence
):
    requests = stub_event(monkeypatch, stored=series())
    with pytest.raises(icloud.CalendarError, match="no occurrence"):
        icloud.delete_event(
            'Home',
            'abc',
            'version-1',
            occurrence=occurrence,
            scope='occurrence'
        )
    assert requests == []


def test_occurrence_already_deleted_is_not_found_again(monkeypatch):
    stored = series()
    stored.events[0].add('exdate', datetime(2026, 11, 16, 9, tzinfo=NEW_YORK))
    requests = stub_event(monkeypatch, stored=stored)
    with pytest.raises(icloud.CalendarError, match="no occurrence"):
        icloud.update_event(
            'Home',
            'abc',
            'version-1',
            summary="Renamed",
            occurrence=SECOND,
            scope='occurrence'
        )
    assert requests == []


def test_series_too_dense_to_expand_is_changed_only_whole(monkeypatch):
    stored = stored_event(
        summary="Ping",
        start=datetime(2020, 1, 1, tzinfo=UTC),
        rrule='FREQ=SECONDLY',
        check=False
    )
    requests = stub_event(monkeypatch, stored=stored)
    with pytest.raises(icloud.CalendarError, match="Only the whole series"):
        icloud.delete_event(
            'Home',
            'abc',
            'version-1',
            occurrence='2026-11-16T14:00:00+00:00',
            scope='occurrence'
        )
    assert requests == []


def test_occurrence_with_invitees_needs_confirming_too(monkeypatch):
    stored = series()
    stored.events[0].add('attendee', 'mailto:guest@example.invalid')
    requests = stub_event(monkeypatch, stored=stored)
    with pytest.raises(icloud.CalendarError, match="may notify"):
        icloud.delete_event(
            'Home',
            'abc',
            'version-1',
            occurrence=SECOND,
            scope='occurrence'
        )
    assert requests == []


# --- One occurrence and every later one -------------------------------------

def test_series_is_ended_before_an_occurrence(monkeypatch):
    requests = stub_event(monkeypatch, stored=series(moved=True, later=True))
    icloud.delete_event(
        'Home',
        'abc',
        'version-1',
        occurrence=THIRD,
        scope='following'
    )
    assert [request['method'] for request in requests] == ['PUT']
    saved = sent_event(requests)
    rule = saved.events[0]['RRULE']
    # One second before the third occurrence, and no count beside it.
    assert rule['UNTIL'] == [datetime(2026, 11, 23, 13, 59, 59, tzinfo=UTC)]
    assert 'COUNT' not in rule
    # The earlier changed occurrence stays; the later one goes.
    assert [str(event['SUMMARY']) for event in saved.events] == [
        "Standup",
        "Standup",
    ]
    assert starts(saved) == ['2026-11-09', '2026-11-17']


def test_all_day_series_is_ended_the_day_before(monkeypatch):
    stored = stored_event(
        summary="Bins",
        start=date(2026, 11, 9),
        rrule='FREQ=WEEKLY'
    )
    requests = stub_event(monkeypatch, stored=stored)
    icloud.delete_event(
        'Home',
        'abc',
        'version-1',
        occurrence='2026-11-23',
        scope='following'
    )
    rule = sent_event(requests).events[0]['RRULE']
    assert rule['UNTIL'] == [date(2026, 11, 22)]


def test_ending_before_the_first_occurrence_is_refused(monkeypatch):
    requests = stub_event(monkeypatch, stored=series())
    with pytest.raises(icloud.CalendarError, match="first occurrence"):
        icloud.delete_event(
            'Home',
            'abc',
            'version-1',
            occurrence=FIRST,
            scope='following'
        )
    assert requests == []


def test_changing_an_occurrence_and_later_ones_is_refused(no_icloud):
    with pytest.raises(icloud.CalendarError, match="not supported"):
        icloud.update_event(
            'Home',
            'abc',
            'version-1',
            summary="Renamed",
            occurrence=THIRD,
            scope='following'
        )


# --- One event in full ------------------------------------------------------

def test_get_event_adds_the_rule_and_the_people(monkeypatch):
    stored = series()
    event = stored.events[0]
    event.add(
        'organizer',
        'mailto:owner@example.invalid',
        parameters={'CN': 'Owner'}
    )
    event.add(
        'attendee',
        'MAILTO:guest@example.invalid',
        parameters={'CN': 'Guest', 'PARTSTAT': 'ACCEPTED'}
    )
    event.add('attendee', 'mailto:other@example.invalid')
    stub_event(monkeypatch, stored=stored)
    found = icloud.get_event('Home', 'abc', 'America/New_York')
    assert found['summary'] == "Standup"
    assert found['start'] == '2026-11-09T09:00:00-05:00'
    assert found['etag'] == 'version-1'
    assert found['rrule'] == 'FREQ=WEEKLY;COUNT=10'
    assert found['organizer'] == {
        'email': 'owner@example.invalid',
        'name': 'Owner',
    }
    assert found['invitees'] == [
        {
            'email': 'guest@example.invalid',
            'name': 'Guest',
            'status': 'accepted',
        },
        {'email': 'other@example.invalid'},
    ]
    assert found['has_invitees'] is True
    # The series as a whole, so no one occurrence is named.
    assert 'occurrence' not in found


def test_get_event_of_a_plain_event_has_only_what_it_holds(monkeypatch):
    stub_event(monkeypatch)
    found = icloud.get_event('Home', 'abc')
    assert found == {
        'uid': 'uid-Dentist',
        'calendar': 'Home',
        'summary': 'Dentist',
        'start': '2026-11-13T15:00:00+00:00',
        'end': '2026-11-13T16:00:00+00:00',
        'all_day': False,
        'etag': 'version-1',
    }


def test_get_event_refuses_one_too_large_to_return(monkeypatch):
    stub_event(monkeypatch)
    monkeypatch.setattr(icloud, 'MAX_RESPONSE_CHARS', 10)
    with pytest.raises(icloud.CalendarError, match="too much text"):
        icloud.get_event('Home', 'abc')


def test_query_finds_an_event_by_who_is_invited(monkeypatch):
    meeting = stored_event(summary="Review")
    meeting.events[0].add(
        'attendee',
        'mailto:guest@example.invalid',
        parameters={'CN': 'Ada Lovelace'}
    )
    calendar = FakeCalendar(stored=[meeting, stored_event(summary="Dentist")])
    use_calendar(monkeypatch, calendar)
    for query in ('lovelace', 'GUEST@example'):
        events = icloud.list_events(
            '2026-11-13',
            '2026-11-14',
            calendar='Home',
            query=query
        )
        assert [event['summary'] for event in events] == ["Review"]


# --- Busy times -------------------------------------------------------------

def test_busy_times_are_merged_and_hold_no_text(monkeypatch):
    def at(hour, minute=0):
        return datetime(2026, 11, 13, hour, minute, tzinfo=UTC)

    free = stored_event(summary="Lunch hold", start=at(12))
    free.events[0].add('transp', 'TRANSPARENT')
    cancelled = stored_event(summary="Cancelled", start=at(20))
    cancelled.events[0].add('status', 'CANCELLED')
    calendar = FakeCalendar(stored=[
        stored_event(summary="SECRET one", start=at(15)),
        stored_event(summary="SECRET two", start=at(15, 30)),
        # Starts as the one before it ends.
        stored_event(summary="SECRET three", start=at(16, 30)),
        stored_event(summary="SECRET four", start=at(9)),
        free,
        cancelled,
    ])
    use_calendar(monkeypatch, calendar)
    busy = icloud.list_busy_times('2026-11-13', '2026-11-14', calendar='Home')
    assert busy == {
        'busy': [
            {
                'start': '2026-11-13T09:00:00+00:00',
                'end': '2026-11-13T10:00:00+00:00',
            },
            {
                'start': '2026-11-13T15:00:00+00:00',
                'end': '2026-11-13T17:30:00+00:00',
            },
        ],
    }
    assert 'SECRET' not in str(busy)


def test_busy_times_stay_inside_the_range_and_its_timezone(monkeypatch):
    overnight = stored_event(
        summary="Flight",
        start=datetime(2026, 11, 13, 2, tzinfo=UTC),
        duration=timedelta(hours=6)
    )
    all_day = stored_event(summary="Conference", start=date(2026, 11, 14))
    use_calendar(monkeypatch, FakeCalendar(stored=[overnight, all_day]))
    busy = icloud.list_busy_times(
        '2026-11-13',
        '2026-11-15',
        calendar='Home',
        timezone='America/New_York'
    )
    assert busy['busy'] == [
        # Began at 21:00 the evening before, New York time.
        {
            'start': '2026-11-13T00:00:00-05:00',
            'end': '2026-11-13T03:00:00-05:00',
        },
        # An all-day event covers the day where the caller is.
        {
            'start': '2026-11-14T00:00:00-05:00',
            'end': '2026-11-15T00:00:00-05:00',
        },
    ]


def test_busy_times_say_when_a_series_was_left_out(monkeypatch):
    dense = stored_event(
        summary="Ping",
        start=datetime(2020, 1, 1, tzinfo=UTC),
        rrule='FREQ=SECONDLY',
        check=False
    )
    calendar = FakeCalendar(stored=[dense, stored_event(summary="Dentist")])
    use_calendar(monkeypatch, calendar)
    busy = icloud.list_busy_times('2026-11-13', '2026-11-14', calendar='Home')
    assert busy['incomplete'] is True
    assert len(busy['busy']) == 1


def test_busy_times_count_each_occurrence(monkeypatch):
    use_calendar(monkeypatch, FakeCalendar(stored=[series()]))
    busy = icloud.list_busy_times('2026-11-09', '2026-11-24', calendar='Home')
    assert [span['start'][:10] for span in busy['busy']] == [
        '2026-11-09',
        '2026-11-16',
        '2026-11-23',
    ]


def test_busy_times_keep_the_limits_of_a_listing(no_icloud):
    with pytest.raises(icloud.CalendarError, match="366 days"):
        icloud.list_busy_times('2026-01-01', '2028-01-01')


# --- Details: link, alerts, free time, coordinates --------------------------

def test_new_event_takes_a_link_alerts_free_time_and_coordinates(monkeypatch):
    calendar = FakeCalendar()
    use_calendar(monkeypatch, calendar)
    created = icloud.create_event(
        'Home',
        "Dinner",
        '2026-11-13T19:00:00',
        timezone='America/New_York',
        location='Example Diner, 1 "Main" St',
        url='https://example.invalid/menu',
        alerts=[60, 15, 15],
        free=True,
        latitude=40.7128,
        longitude=-74.006
    )
    assert created['url'] == 'https://example.invalid/menu'
    assert created['alerts'] == [15, 60]
    assert created['free'] is True
    assert (created['latitude'], created['longitude']) == (40.7128, -74.006)

    stored = icalendar.Calendar.from_ical(calendar.added[0]).events[0]
    assert str(stored['URL']) == 'https://example.invalid/menu'
    assert str(stored['TRANSP']) == 'TRANSPARENT'
    assert str(stored[icloud.PLACE]) == 'geo:40.712800,-74.006000'
    assert stored[icloud.PLACE].params['X-TITLE'] == (
        "Example Diner, 1 'Main' St"
    )
    alarms = [part for part in stored.subcomponents if part.name == 'VALARM']
    assert [str(alarm['ACTION']) for alarm in alarms] == ['DISPLAY'] * 3
    # What was stored reads back the same.
    assert icloud._event_to_dict(stored, 'Home', 'America/New_York') == created


@pytest.mark.parametrize(
    'details',
    [
        {'url': 'javascript:alert(1)'},
        {'url': 'https://example.invalid/a b'},
        {'url': 'example.invalid'},
        {'alerts': [-5]},
        {'alerts': [1.5]},
        {'alerts': [True]},
        {'alerts': [icloud.MAX_ALERT_MINUTES + 1]},
        {'alerts': [5] * (icloud.MAX_ALERTS + 1)},
        {'latitude': 40.7},
        {'longitude': -74.0},
        {'latitude': 40.7, 'longitude': -74.0},
        {'location': "Diner", 'latitude': 91, 'longitude': 0},
        {'location': "Diner", 'latitude': 0, 'longitude': 181},
        {'location': "Diner", 'latitude': float('nan'), 'longitude': 0},
        {'location': "Diner", 'latitude': '40.7', 'longitude': 0},
    ]
)
def test_unusable_details_write_nothing(monkeypatch, details):
    calendar = FakeCalendar()
    use_calendar(monkeypatch, calendar)
    with pytest.raises(icloud.CalendarError):
        icloud.create_event('Home', "Dinner", '2026-11-13', **details)
    assert calendar.added == []


def test_unusable_details_on_update_are_refused_first(no_icloud):
    with pytest.raises(icloud.CalendarError, match="web address"):
        icloud.update_event('Home', 'abc', 'version-1', url='ftp://x.invalid')


def test_update_replaces_alerts_and_clears_what_is_emptied(monkeypatch):
    stored = stored_event(summary="Dinner")
    event = stored.events[0]
    icloud._edit(
        event,
        location="Old place",
        url='https://example.invalid/old',
        alerts=[30],
        free=True,
        latitude=1.0,
        longitude=2.0
    )
    requests = stub_event(monkeypatch, stored=stored)
    updated = icloud.update_event(
        'Home',
        'abc',
        'version-1',
        location="New place",
        url='',
        alerts=[],
        free=False
    )
    # A new place, so the old one's coordinates are gone with it.
    assert updated['location'] == "New place"
    for gone in ('url', 'alerts', 'free', 'latitude', 'longitude'):
        assert gone not in updated
    saved = sent_event(requests).events[0]
    assert icloud.PLACE not in saved
    assert 'URL' not in saved
    assert str(saved['TRANSP']) == 'OPAQUE'
    assert saved.subcomponents == []


def test_update_leaves_alone_what_it_was_not_given(monkeypatch):
    stored = stored_event(summary="Dinner")
    icloud._edit(
        stored.events[0],
        location="Diner",
        url='https://example.invalid/menu',
        alerts=[30],
        latitude=1.0,
        longitude=2.0
    )
    requests = stub_event(monkeypatch, stored=stored)
    updated = icloud.update_event(
        'Home',
        'abc',
        'version-1',
        summary="Late dinner"
    )
    assert updated['url'] == 'https://example.invalid/menu'
    assert updated['alerts'] == [30]
    assert (updated['latitude'], updated['longitude']) == (1.0, 2.0)
    assert icloud.PLACE in sent_event(requests).events[0]


def test_alerts_set_some_other_way_are_not_described():
    event = stored_event(summary="Dinner").events[0]
    for trigger in (timedelta(minutes=10), datetime(2026, 11, 13, tzinfo=UTC)):
        alarm = icalendar.Alarm()
        alarm.add('action', 'DISPLAY')
        alarm.add('trigger', trigger)
        event.add_component(alarm)
    assert 'alerts' not in icloud._event_to_dict(event, 'Home', None)


def test_tentative_and_cancelled_events_say_so():
    event = stored_event(summary="Dinner").events[0]
    event.add('status', 'TENTATIVE')
    assert icloud._event_to_dict(event, 'Home', None)['status'] == 'tentative'


# --- Helpers ----------------------------------------------------------------

def series(
    alert: bool = False,
    moved: bool = False,
    later: bool = False
) -> icalendar.Calendar:
    """Build a weekly series as another client might have stored it.

    `moved` gives the second occurrence details of its own, a day late;
    `later` does the same for the fourth.
    """
    stored = stored_event(
        summary="Standup",
        start=datetime(2026, 11, 9, 9, tzinfo=NEW_YORK),
        rrule=WEEKLY,
        duration=timedelta(minutes=30)
    )
    event = stored.events[0]
    if alert:
        icloud._edit(event, alerts=[15])
    for wanted, day in ((moved, 16), (later, 30)):
        if not wanted:
            continue
        original = datetime(2026, 11, day, 9, tzinfo=NEW_YORK)
        changed = icalendar.Event.new(
            uid=str(event['UID']),
            summary="Standup",
            start=original + timedelta(days=1),
            end=original + timedelta(days=1, minutes=30)
        )
        changed.add('recurrence-id', original)
        stored.add_component(changed)
    return stored


def sent_event(requests: list[dict]) -> icalendar.Calendar:
    """Return the event data of the one request that was sent."""
    assert len(requests) == 1
    return icalendar.Calendar.from_ical(requests[0]['body'])


def starts(stored: icalendar.Calendar) -> list[str]:
    """Return the days a stored event falls on in the four weeks from
    2026-11-09, as a listing would give them."""
    found, complete = icloud._occurrences(
        stored,
        datetime(2026, 11, 9, tzinfo=UTC),
        datetime(2026, 12, 7, tzinfo=UTC),
        icloud._deadline()
    )
    assert complete
    return sorted(event.start.date().isoformat() for event in found)
