"""Time parsing and event conversion (no network)."""

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import caldav
import icalendar
import pytest

import icloud

NEW_YORK = ZoneInfo('America/New_York')


def test_date_only_is_all_day():
    assert icloud._parse_time('2026-11-13', None) == date(2026, 11, 13)


def test_local_time_takes_the_named_timezone():
    parsed = icloud._parse_time('2026-11-13T15:00:00', 'America/New_York')
    assert parsed == datetime(2026, 11, 13, 15, tzinfo=NEW_YORK)
    assert parsed.tzinfo is NEW_YORK


def test_offset_time_without_timezone_becomes_utc():
    parsed = icloud._parse_time('2026-11-13T15:00:00-05:00', None)
    assert parsed == datetime(2026, 11, 13, 20, tzinfo=UTC)
    assert parsed.tzinfo is UTC


def test_offset_time_is_converted_to_the_named_timezone():
    parsed = icloud._parse_time('2026-11-13T20:00:00Z', 'America/New_York')
    assert parsed.tzinfo is NEW_YORK
    assert parsed.hour == 15


@pytest.mark.parametrize(
    ('value', 'timezone'),
    [
        ('2026-11-13T15:00:00', None),
        ('2026-11-13T15:00:00', 'Mars/Olympus_Mons'),
        ('next tuesday', 'America/New_York'),
    ]
)
def test_unusable_times_are_rejected(value, timezone):
    with pytest.raises(icloud.CalendarError):
        icloud._parse_time(value, timezone)


def test_date_bound_is_midnight_in_the_timezone():
    bound = icloud._parse_bound('2026-11-13', 'America/New_York')
    assert bound == datetime(2026, 11, 13, tzinfo=NEW_YORK)


def test_span_must_be_ordered_and_of_one_kind():
    start = datetime(2026, 11, 13, 15, tzinfo=UTC)
    with pytest.raises(icloud.CalendarError):
        icloud._check_span(start, start)
    with pytest.raises(icloud.CalendarError):
        icloud._check_span(date(2026, 11, 13), start)
    icloud._check_span(start, start + timedelta(hours=1))


def test_event_to_dict_converts_to_the_requested_timezone():
    event = icalendar.Event.new(
        uid='abc',
        summary="Dentist",
        start=datetime(2026, 11, 13, 20, tzinfo=UTC),
        end=datetime(2026, 11, 13, 21, tzinfo=UTC),
        location="Main St"
    )
    assert icloud._event_to_dict(event, 'Home', 'America/New_York') == {
        'uid': 'abc',
        'calendar': 'Home',
        'summary': 'Dentist',
        'start': '2026-11-13T15:00:00-05:00',
        'end': '2026-11-13T16:00:00-05:00',
        'all_day': False,
        'location': 'Main St',
    }


def test_event_to_dict_all_day():
    event = icalendar.Event.new(
        uid='abc',
        summary="Holiday",
        start=date(2026, 11, 13),
        end=date(2026, 11, 14)
    )
    entry = icloud._event_to_dict(event, 'Home', 'America/New_York')
    assert entry['start'] == '2026-11-13'
    assert entry['end'] == '2026-11-14'
    assert entry['all_day'] is True


def test_reschedule_with_only_start_keeps_duration():
    event = icalendar.Event.new(
        uid='abc',
        summary="Call",
        start=datetime(2026, 11, 13, 15, tzinfo=NEW_YORK),
        end=datetime(2026, 11, 13, 15, 45, tzinfo=NEW_YORK)
    )
    icloud._reschedule(event, '2026-11-14T09:00:00', None, 'America/New_York')
    assert event.start == datetime(2026, 11, 14, 9, tzinfo=NEW_YORK)
    assert event.end == datetime(2026, 11, 14, 9, 45, tzinfo=NEW_YORK)


def test_rrule_accepts_optional_prefix_and_rejects_garbage():
    rule = icloud._parse_rrule('RRULE:FREQ=WEEKLY;BYDAY=MO,WE')
    assert rule['FREQ'] == ['WEEKLY']
    with pytest.raises(icloud.CalendarError):
        icloud._parse_rrule('every other week')


def test_stored_event_is_identified_by_its_own_uid():
    event = icalendar.Event.new(
        uid='event-uid',
        summary="Call",
        start=datetime(2026, 11, 13, 15, tzinfo=NEW_YORK),
        end=datetime(2026, 11, 13, 16, tzinfo=NEW_YORK)
    )
    data = icloud._wrap(event).to_ical().decode()
    assert caldav.Event(data=data).id == 'event-uid'
    assert 'BEGIN:VTIMEZONE' in data
