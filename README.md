# icloud-calendar-mcp

A remote [MCP](https://modelcontextprotocol.io) server that gives Claude
read/write access to your iCloud calendars over CalDAV. It runs on
[Modal](https://modal.com) and is added to Claude as a custom connector, so it
works in Claude on web, desktop, mobile and in cloud tasks.

It authenticates to iCloud with an Apple
[app-specific password](https://support.apple.com/en-us/102654), and Claude
authenticates to the server with a bearer token.

## Tools

| Tool             | What it does                                              |
| ---------------- | --------------------------------------------------------- |
| `list_calendars` | Names of your calendars                                   |
| `list_events`    | Events in a time range, optionally filtered by text       |
| `create_event`   | New timed, all-day or recurring event                     |
| `update_event`   | Change the title, time, location or description           |
| `delete_event`   | Delete an event                                           |

Recurring events are listed one occurrence at a time, but `update_event` and
`delete_event` act on the whole series. Invitations, attendees and alerts are
not supported.

One `list_events` call covers at most 366 days and 1000 events; past that it
fails and says so, rather than returning part of the list. New recurring
events repeat daily or less often. A stored series that repeats too often to
expand safely (every few seconds, say) is listed once and marked
`occurrences_omitted`.

## Setup

Requires [uv](https://docs.astral.sh/uv/) and a Modal account.

```bash
uv sync
```

```bash
cp .env.example .env
```

Fill in `.env`:

- `ICLOUD_USERNAME`: your Apple ID email
- `ICLOUD_APP_PASSWORD`: an app-specific password generated at
  [account.apple.com](https://account.apple.com) under Sign-In and Security.
  Create one just for this server so you can revoke it on its own: an
  app-specific password also works for iCloud mail and contacts.
- `MCP_AUTH_TOKEN`: the token Claude will send, which is a Modal proxy auth
  token. Create one:

  ```bash
  uv run modal workspace proxy-tokens create --name icloud-calendar-mcp
  ```

  Modal prints an `Authorization: Bearer wk-....ws-...` line. Use the part
  after `Bearer ` (the token ID and secret joined by a period) as the value.
  The secret is shown only this once.

## Run locally

```bash
uv run --env-file .env python server.py
```

The server listens on `http://127.0.0.1:8000/mcp`. To try the tools, point
the [MCP Inspector](https://modelcontextprotocol.io/docs/tools/inspector) at
that URL using the Streamable HTTP transport and an `Authorization` header of
`Bearer <your MCP_AUTH_TOKEN>`.

Run the tests (no iCloud account needed):

```bash
uv run pytest
```

## Deploy to Modal

Store the three values from `.env` as a Modal Secret:

```bash
uv run modal secret create icloud-calendar-mcp --from-dotenv .env
```

Deploy:

```bash
uv run modal deploy modal_app.py
```

Modal prints the endpoint URL, ending in `.modal.run`. The MCP server is at
that URL plus `/mcp`.

If the deploy fails with "Unsupported Python version: '3.14'", your Modal
workspace is on an old image builder version. Raise it in the Modal dashboard
under Settings > Image Config, or override it for this deploy:

```bash
MODAL_IMAGE_BUILDER_VERSION=2026.10 uv run modal deploy modal_app.py
```

The deployment scales to zero when idle and is capped at one small CPU-only
container, so it costs little or nothing on Modal's free credits. The first
request after an idle period takes a few seconds while a container starts.

## Add it to Claude

In Claude, open Customize > Connectors > Add custom connector:

- URL: your Modal URL plus `/mcp`
- Transport: Streamable HTTP
- Authentication: No sign-in
- Request headers: name `authorization`, value `Bearer <your MCP_AUTH_TOKEN>`
  (type the `Bearer ` prefix yourself)

The request headers option is in beta and not on every account. Claude does
not let you edit a connector's authentication afterwards, so to change the
token: create a new proxy token, put it in `.env`, recreate the Modal Secret
(add `--force`), redeploy, remove and re-add the connector, then delete the
old token with `uv run modal workspace proxy-tokens delete <token-id>`.

## Security

The Modal URL is public, and two layers keep everyone else out. Both check
the same `Authorization` header:

1. Modal's proxy auth rejects a request without a valid proxy token before
   it reaches the server, so strangers can't start a container.
2. The server itself then compares the header to `MCP_AUTH_TOKEN` and
   rejects anything else before doing any work. This keeps the endpoint
   closed even if the Modal setting is ever turned off.

Anyone who has the token can read and change your calendars: keep it out of
chats, logs and screenshots, and replace it if it leaks. Secrets live only in
`.env` (ignored by git) and the Modal Secret.

Past the token check, the server:

- accepts only requests addressed to its own host (`*.modal.run` when
  deployed; edit `ALLOWED_HOSTS` in `modal_app.py` if you use your own
  domain) and refuses requests that come from a web page;
- gives up on iCloud after 20 seconds per request and 45 per tool call;
- reports errors without calendar names, event text or iCloud's replies, in
  both the response and the logs;
- deletes an event only if it hasn't changed since it was looked up.

Titles and descriptions on shared calendars and invitations are written by
other people. Claude is told to treat them as data, but keep your client's
confirmation prompts on for the tools that change things.
