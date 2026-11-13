# icloud-calendar-mcp

## Purpose

A remote MCP server that gives Claude read/write access to a person's iCloud
calendars. Each user deploys their own copy to Modal and adds it to their
Claude account as a custom connector, so every Claude surface (web, desktop,
mobile, cloud scheduled tasks) can use it, not only ones with shell access.

Claude's connector directory has no iCloud calendar integration; this fills
that gap.

## Public and private notes

This file is committed, so treat it as world-readable. It holds what any
contributor needs: how the project works and why it is built this way.

Anything about one person or one deployment goes in `CLAUDE.local.md` at the
repo root instead. Claude Code loads that file automatically, right after
this one, and `.gitignore` keeps it out of the repo. Put there:

- Names, email addresses, Apple IDs, account and workspace names.
- Deployed URLs, token IDs, and anything else that identifies a deployment.
- Anything about calendar contents: calendar names, counts, events.
- Budgets, billing, and which beta features an account has.
- The state of your own deployment and your own to-do list.
- Personal working preferences and plans you haven't made public.

Private files that aren't notes (scan reports, scratch work, anything that
names a deployment) go in `.local/` at the repo root, which is gitignored
the same way.

For Claude, when recording something in this project:

- Pick the file by the rule above. If it is unclear which side a detail
  falls on, put it in `CLAUDE.local.md` and say so.
- If `CLAUDE.local.md` doesn't exist, create it, and confirm that
  `git check-ignore CLAUDE.local.md` succeeds before writing to it.
- The same rule covers every tracked file: README, code, comments, tests
  and commit messages. Describe behaviour generically ("an account with
  many calendars"), not by reference to a particular person's setup.
- Before committing, read the diff for details that belong in the private
  file.

## Layout

Flat modules at the repo root, no package and no build system:

- `auth.py`: `BearerAuthMiddleware`, a plain ASGI wrapper around the whole
  app. Chosen over FastMCP's auth providers so a bad token gets a bare 401
  with no OAuth discovery metadata, and every route is covered.
- `icloud.py`: all CalDAV work. No FastMCP imports.
- `server.py`: the FastMCP tools (thin wrappers over `icloud.py`) and
  `build_app()`. `python server.py` serves on 127.0.0.1:8000.
- `modal_app.py`: Modal image and the `web` ASGI function.
- `tests/`: pytest, no network.

Local run: `uv run --env-file .env python server.py` (uv loads `.env`, so
nothing in the code reads it). Tests: `uv run pytest`.

## Design decisions

- **Keep it simple.** Don't add features, options or abstractions that
  nothing needs yet.
- **iCloud access: CalDAV with an app-specific password.** Endpoint is
  `https://caldav.icloud.com`, username is the Apple ID email.
- **Read and write.** Listing/searching events and creating/updating/deleting
  them are all in scope.
- **Hosting: Modal.** Chosen for convenience and its free credits, not for
  being the ideal MCP host. Free MCP-specific hosts (FastMCP Cloud,
  Cloudflare Workers) have not been evaluated.
- **MCP framework: FastMCP** (`fastmcp`), served over Streamable HTTP (the
  current MCP transport; SSE is legacy).
- **Tools:** `list_calendars`, `list_events`, `create_event`,
  `update_event`, `delete_event`. Text search is the `query` argument of
  `list_events`, filtered in Python, because iCloud's server-side text
  search is unreliable.
- **Events are addressed by calendar name + UID.** Never accept a URL from a
  tool argument: the client would send the iCloud credentials to it.
- **Times** are ISO 8601 strings. Date-only means all-day. A date-time needs
  a UTC offset or the `timezone` argument (IANA name); there is no default
  timezone setting.
- **Recurring events:** listed expanded per occurrence, created with an
  `rrule` argument, updated and deleted as a whole series. Editing a single
  occurrence is not implemented. Attendees and alarms are out of scope.
- **Stateless HTTP with JSON responses** (`stateless_http=True`,
  `json_response=True`), so nothing depends on a container staying alive.
- **`caldav` is used with `features='icloud'`**, which makes the library
  work around iCloud quirks (for example UID searches answering 412).
- **No UID on the VCALENDAR wrapper.** `icalendar.Calendar.new()` adds
  one, and `caldav` then takes it for the event's UID, so events this server
  created could not be found again for update or delete (`_wrap` in
  `icloud.py` removes it; there is a regression test).
- **The `caldav` logger is set to ERROR.** At WARNING it prints event
  contents when it repairs malformed data, which would put calendar data in
  the Modal logs.
- **Per-calendar requests run concurrently** (`_per_calendar` in
  `icloud.py`, 8 threads). iCloud needs one request per calendar. On an
  account with many calendars, a week's listing took about 5 seconds run in
  sequence and 1 to 2 run concurrently. A year-wide `query` search is still
  slow (about 8 seconds on that account), since it downloads every event in
  the range.
- **Modal sizing:** `cpu=0.125`, `memory=256`, `max_containers=1`,
  `scaledown_window=60`. The single-container cap bounds what a flood of
  requests could cost.
- **Modal Secret name:** `icloud-calendar-mcp`.

## Security

A Modal web endpoint is a public `*.modal.run` URL, so the deployment must
reject every request that doesn't come from its owner's Claude account.

- **Bearer token, checked twice.** Modal's proxy auth
  (`requires_proxy_auth=True`) validates the `Authorization` header first,
  then the server compares it to `MCP_AUTH_TOKEN` and rejects anything that
  isn't `Bearer <token>`. Compare in constant time. Reject before touching
  iCloud. Keep both layers: the app's check is what holds if the Modal flag
  is ever dropped.
- **One header, one value.** Both layers read the same header, so
  `MCP_AUTH_TOKEN` is a Modal proxy token in the form
  `<token-id>.<token-secret>` (`wk-....ws-...`), created with `modal
  workspace proxy-tokens create`. Modal also accepts `Modal-Key` and
  `Modal-Secret` headers, but Claude can't send those (see below).
- **What was verified on a real deployment (November 2026):** Modal forwards
  the `Authorization` header to the app. Requests without a valid proxy
  token get a 401 from Modal in about 0.1 seconds and start no container;
  without proxy auth, such a request cold-started a container (several
  seconds) just to be refused. With proxy auth on, the app's own check can't
  be reached from outside without also passing Modal's, so only the unit
  tests exercise it.
- **Connector settings in Claude** (Customize > Connectors > Add custom
  connector). The "Request headers" section is a beta that not every
  account has:
  - Authentication: **No sign-in**
  - Request headers: add `authorization` with value `Bearer <token>`. Claude
    sends the value verbatim, so the `Bearer ` prefix must be typed in.
  - Transport: Streamable HTTP
  - Auth settings can't be edited after adding. Rotating the token means
    a new proxy token, a recreated Modal Secret, a redeploy, and removing
    and re-adding the connector.
  - Custom header names need Anthropic approval; `authorization` is on the
    standard list, so use that rather than inventing one.
- **Secrets live in a Modal Secret**, never in the repo or the image. `.env`
  is for local runs only and is gitignored.
- **App-specific password blast radius.** Apple's app-specific passwords are
  not scoped to calendars: one also works for iCloud mail and contacts. Use a
  password dedicated to this project so it can be revoked on its own at
  account.apple.com.
- **Never log** the token, the app password, request headers, or calendar
  contents.

Alternatives considered and not chosen:

- GitHub OAuth via FastMCP's OAuth proxy, allowlisting the owner's
  username. Works on accounts without the request-headers beta, but needs
  an OAuth app plus state that survives Modal scaling to zero. FastMCP's
  OAuth proxy had a confused-deputy advisory (GHSA-rww4-4w9c-7733, March
  2026), so check the installed version is patched if this is ever used.
- Secret token in the URL path. Works everywhere, but URLs leak into logs.

## Cost

The deployment is meant to cost close to nothing and fit inside Modal's free
credits.

- Scale to zero when idle: no minimum/warm containers, smallest CPU-only
  container, short idle timeout. A cold start on the first calendar request
  is an acceptable trade.
- Develop and test locally against iCloud using `.env`; don't iterate by
  redeploying, and don't run load or stress tests against Modal.

## Working with real accounts

- Writing to a real calendar needs the owner's go-ahead each time. Use a
  clearly labelled throwaway event on a calendar that isn't shared, and
  delete it afterwards.
- Don't print calendar contents or secrets into logs, test output or chat
  beyond what the task needs.

## Open source

The repo is written so that a stranger can deploy their own copy:

- Nothing personal in tracked files (see Public and private notes).
  Everything user-specific comes from secrets and config.
- README should stay accurate for someone setting it up from scratch.
- No license has been chosen yet; don't add one without the maintainer
  deciding.

## Environment

- **uv** manages everything. Use `uv sync`, `uv add`, `uv run`. Never raw
  `pip`.
- Python **3.14**, pinned in `.python-version`. Modal's `debian_slim` image
  supports 3.14 only on image builder version `2025.06` or later. A
  workspace on an older builder (`2023.12` tops out at 3.12) fails the
  deploy with "Unsupported Python version". Raise the setting in the Modal
  dashboard, or run the deploy with `MODAL_IMAGE_BUILDER_VERSION=2026.10`.
- Dependencies: `modal`, `fastmcp`, `caldav`, plus `pytest` in the dev
  group. `icalendar` and `uvicorn` are imported directly but come in with
  `caldav` and `fastmcp`. Add more only when the code needs them.
- Run Modal through the project env: `uv run modal ...`.

## Secrets

`.env` (gitignored) and `.env.example` hold the same three keys:

- `ICLOUD_USERNAME`: Apple ID email
- `ICLOUD_APP_PASSWORD`: app-specific password
- `MCP_AUTH_TOKEN`: bearer token Claude sends (a Modal proxy token,
  `<token-id>.<token-secret>`; any string of 32+ characters works locally)

Never read `.env`. The same three values go into a Modal Secret for the
deployed server.

## Code conventions

- 80-column lines, including comments and docstrings.
- Top-down ordering: public functions first, their helpers below.
- Single quotes for identifiers, keys and paths; double quotes for text a
  person reads (messages, docstrings).
- When a call or signature doesn't fit on one line, one argument per line.
