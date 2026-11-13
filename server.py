"""MCP server exposing iCloud calendars to Claude.

Run locally with:
    uv run --env-file .env python server.py
"""

import os

from fastmcp import FastMCP

import icloud
from auth import BearerAuthMiddleware

INSTRUCTIONS = """\
Read and write the user's iCloud calendars.

Times are ISO 8601 strings. A date-only value such as '2026-11-13' means an
all-day event, and an all-day end date is exclusive (a one-day event on the
13th ends on the 14th). Date-times need either a UTC offset or the
`timezone` argument (an IANA name such as 'America/New_York'); prefer
passing `timezone` with local times, which also returns times in that zone.

Events are identified by `calendar` and `uid`, both returned by list_events.
"""

mcp = FastMCP('iCloud Calendar', instructions=INSTRUCTIONS)


def build_app():
    """Return the ASGI app: the MCP server behind the bearer-token gate."""
    # Stateless, so it works when the host scales to zero between requests.
    app = mcp.http_app(stateless_http=True, json_response=True)
    return BearerAuthMiddleware(app, os.environ['MCP_AUTH_TOKEN'])


@mcp.tool(annotations={'readOnlyHint': True})
def list_calendars() -> list[str]:
    """List the names of the user's iCloud calendars."""
    return icloud.list_calendars()


@mcp.tool(annotations={'readOnlyHint': True})
def list_events(
    start: str,
    end: str,
    calendar: str | None = None,
    query: str | None = None,
    timezone: str | None = None
) -> list[dict]:
    """List events between start (inclusive) and end (exclusive).

    Recurring events come back as one entry per occurrence. Searches every
    calendar unless `calendar` names one. `query` keeps only events whose
    summary, location or description contains that text (case-insensitive),
    so use it with a wide range to search for an event.
    """
    return icloud.list_events(start, end, calendar, query, timezone)


@mcp.tool(annotations={'destructiveHint': False})
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
    """Create an event in the named calendar.

    Without `end`, a timed event lasts one hour and an all-day event one
    day. `rrule` makes it recurring: an RFC 5545 rule such as
    'FREQ=WEEKLY;BYDAY=MO,WE' or 'FREQ=MONTHLY;COUNT=6'. Pass `timezone`
    for recurring events so they follow daylight saving.
    """
    return icloud.create_event(
        calendar,
        summary,
        start,
        end,
        timezone,
        location,
        description,
        rrule
    )


@mcp.tool(annotations={'destructiveHint': True, 'idempotentHint': True})
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
    """Change an existing event; omitted fields stay as they are.

    Giving `start` without `end` moves the event and keeps its duration.
    An empty string clears `location` or `description`. For a recurring
    event this changes the whole series, not a single occurrence.
    """
    return icloud.update_event(
        calendar,
        uid,
        summary,
        start,
        end,
        timezone,
        location,
        description
    )


@mcp.tool(annotations={'destructiveHint': True, 'idempotentHint': True})
def delete_event(calendar: str, uid: str) -> dict:
    """Permanently delete an event.

    For a recurring event this deletes the whole series, not a single
    occurrence.
    """
    return icloud.delete_event(calendar, uid)


if __name__ == '__main__':
    import uvicorn

    uvicorn.run(build_app(), host='127.0.0.1', port=8000)
