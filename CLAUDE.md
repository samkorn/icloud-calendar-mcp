# icloud-calendar-mcp

## Purpose

A remote MCP server that gives Claude read/write access to a person's iCloud
calendars. Each user deploys their own copy to Modal and adds it to their
Claude account as a custom connector, so every Claude surface (web, desktop,
mobile, cloud scheduled tasks) can use it, not only ones with shell access.

When this was written (November 2026), Claude's connector directory had no
iCloud calendar integration; this fills that gap.

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
- `icloud.py`: all CalDAV work, and the limits on it. No FastMCP imports.
- `server.py`: the FastMCP tools (thin wrappers over `icloud.py`), the
  error boundary they all go through (`_run`), and `build_app()`.
  `python server.py` serves on 127.0.0.1:8000.
- `modal_app.py`: Modal image and the `web` ASGI function.
- `tests/`: pytest, no network beyond a loopback server one fixture
  starts. `test_safety.py` holds the regression tests for the limits,
  timeouts and error handling, and the helpers the other test files
  borrow. `test_events.py` covers single occurrences, busy times, reading
  one event and event details. `conftest.py` stops FastMCP from reading
  `.env` when the tests import it.

- `.github/workflows/checks.yml`: runs the tests and audits the locked
  packages on every push to `main`, every pull request, and weekly. GitHub
  switches a scheduled workflow off after 60 days without activity in a
  public repository, so the weekly run stops if the repo goes quiet;
  Dependabot alerts don't.
- `SECURITY.md`: how to report a vulnerability privately.

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
- **Tools:** `list_calendars`, `list_events`, `list_busy_times`,
  `get_event`, `create_event`, `update_event`, `delete_event`. Text search
  is the `query` argument of `list_events`, filtered in Python, because
  iCloud's server-side text search is unreliable. It looks at the title,
  location, description and the invitees' names and addresses.
- **`list_busy_times` answers "when am I free" without event text.** It
  returns merged spans of time and nothing else, so a question about
  free time puts no titles or descriptions, which other people can write,
  in front of the model. Events marked free (`TRANSP:TRANSPARENT`) and
  cancelled ones don't count. A series too costly to expand is left out
  and the result is marked `incomplete`.
- **`get_event` reads one event by calendar and UID.** It adds what a
  listing leaves out (organizer, invitees and their replies, the rule) and
  is how the model gets a fresh `etag` after a refusal. For a series it
  describes the event that repeats, not an occurrence.
- **Events are addressed by calendar name + UID.** Never accept a URL from a
  tool argument: the client would send the iCloud credentials to it.
- **Times** are ISO 8601 strings. Date-only means all-day. A date-time needs
  a UTC offset or the `timezone` argument (IANA name); there is no default
  timezone setting.
- **Recurring events:** listed expanded per occurrence, created with an
  `rrule` argument. Each listed occurrence carries `occurrence`, the
  start its rule gave it (its RECURRENCE-ID), which names it however far
  it has been moved since.
- **A recurring event is never changed on a guess.** `update_event` and
  `delete_event` refuse one until the call says what it applies to:
  `scope` is `'series'`, `'occurrence'` or `'following'`, the last two
  with `occurrence`. Before this, a call naming only the event changed
  or deleted every occurrence, which is not what "delete Tuesday's
  standup" means.
- **How each scope is stored.** Changing one occurrence adds a component
  with that RECURRENCE-ID, copied from the event that repeats, or edits
  the one already there. Deleting one adds an EXDATE and removes such a
  component. Deleting an occurrence and every later one ends each rule
  with an UNTIL just before it (in UTC when the start has a timezone,
  and in place of any COUNT) and removes the later components. Each is
  one conditional save of the stored event. The occurrence has to exist:
  it is looked for among the stored components, then in a two-day
  expansion run under the same budget as a listing, so a series too
  dense to expand can only be changed whole.
- **Not done: changing an occurrence and every later one.** Apple's apps
  do it by splitting the series in two, which takes two writes that
  can't be made atomic: a failure between them leaves the later half
  doubled or missing. The other way, `RANGE=THISANDFUTURE`, is one write,
  but Apple's apps don't write it and how they show it is untested.
  Also not done: deleting from a series that lists dates one by one
  (`RDATE`) with `'following'`; that is refused.
- **Details an event can have:** `url`, `alerts` (minutes before the
  start, stored as DISPLAY alarms, replacing whatever alarms were
  there), `free` (TRANSP) and `latitude`/`longitude`. The coordinates go
  with `location` into `X-APPLE-STRUCTURED-LOCATION`, where Apple's apps
  keep them, and a new `location` drops the old ones. Reading that
  property back from events Apple's apps made works; that Calendar draws
  the map for one this server wrote has not been looked at.
- **No invitations.** Adding attendees, or answering an invitation,
  makes iCloud send mail to other people. A model misled by event text
  could then send calendar details to any address, and the server has
  nothing to hold that back but a prompt. Left out for that reason, and
  because it can't be tested on an account with no invitations.
- **An event that already has invitees needs a second call.** iCloud does
  the scheduling, so saving or deleting such an event may send its
  invitees an update, a cancellation or a decline. That has not been
  tested, and user reports are the only evidence, hence "may". The tools
  can't make Claude show a prompt: a connector reaches the model only
  through the server instructions, the tool descriptions and what the
  tools return. So all three say it. `list_events` marks the event
  `has_invitees`, `update_event` and `delete_event` refuse it with a
  message telling the model to ask the user, and they go ahead only when
  called again with `invitees_confirmed`. The model can set that without
  asking, so this is a prompt to ask, not a lock. MCP's elicitation, where
  the server itself asks the user, needs a connection that stays open,
  which stateless JSON responses don't have.
- **`list_events` expands recurrences itself, not through `caldav`.** The
  library underneath walks a rule from the event's first date, one step
  per interval, and offers no way to stop it part-way: an hourly rule that
  began decades ago takes seconds, a per-second one minutes. Events can
  come from shared calendars, so a rule over budget is listed once,
  unexpanded, marked `occurrences_omitted`. `caldav` is asked for
  `expand=False, post_filter=False` because either option expands
  everything with no limit.
- **How many occurrences an expansion can produce is limited before it
  starts** (`_too_costly`). The library hands back one list it has
  already built, so cutting it off afterwards would be too late. The
  count takes in every component (the event and its overrides), every
  explicit date (`RDATE`, in the range or not) and an upper estimate of
  what each rule puts in the range, counting how long an occurrence
  lasts. Over 1000, the event is listed once, unexpanded. Measured on
  synthetic data: an occurrence shares its text with the event rather
  than copying it, so a long description is not multiplied.
- **How long an expansion can run is enforced while it runs.** The same
  estimate turns away the plainly dense rules, but no estimate of the
  steps covers every rule: one that can never match again
  (`BYMONTH=2;BYMONTHDAY=30`) is walked to the year 9999. So `_bounded`
  runs the expansion under `sys.settrace`, which
  calls back on every Python function call the libraries make, and raises
  from inside them after 2 seconds of the thread's processor time or at
  the tool call's deadline. Processor time, so a slow or busy container
  doesn't cut off an ordinary event. The exception is a `BaseException`,
  so no `except Exception` in a library swallows it. This works because
  `icalendar`, `recurring-ical-events` and `dateutil` are pure Python; it
  would not stop a single long call into C. A subprocess that can be
  killed was the alternative, and was left out for what it costs in
  memory and start-up on the smallest container.
- **Limits are constants at the top of `icloud.py`:** a 366-day range, 1000
  events and 300,000 characters per listing, a length cap per text
  argument, 8 MiB per answer from iCloud, 2 seconds of processor time per
  expansion, 20 seconds of silence per iCloud request and 45 seconds per
  tool call. Over a limit the call fails with a message saying how to
  narrow it; it never returns part of a list as if it were all of it.
- **New rules are checked strictly** (`_parse_rrule`): daily or less
  often, positive `INTERVAL` and `COUNT`, not both `COUNT` and `UNTIL`,
  plain weekdays in `WKST`, and a numbered `BYDAY` only where it can
  match (up to 5 monthly, 53 yearly). `icalendar` alone accepts
  `INTERVAL=0`, `COUNT=-1` and `BYDAY=99MO`. A rule that passes can still
  never match (February 30th); reading it back is what `_bounded` is for.
- **Arguments are checked before anything is written,** and the reply is
  built before the write. An error after a successful write would invite a
  retry and a duplicate event. When a write fails without an answer from
  iCloud, the error says the outcome is unknown.
- **Every tool call has one deadline, set on entry and passed down.** Each
  call to iCloud waits no longer than what is left of it. A write is not
  sent once it has passed: `_write` checks first, and a write still queued
  at the deadline is taken out of the queue, so the caller is never told
  "too slow" about a change that then happens. One already on the wire is
  reported as unconfirmed.
- **Changes and deletes are conditional** (`If-Match` with the ETag from
  the lookup), so an event another client changed in between is not
  overwritten or deleted. `_version` requires a strong ETag and refuses
  the write without one. Left alone, `caldav` falls back to a
  Schedule-Tag, which RFC 6638 lets a server keep through some changes,
  and sends a save with no condition at all when there is neither.
- **The caller names the version it read.** Every event a tool returns
  carries `etag`, and `update_event` and `delete_event` require it and
  do nothing unless it is the version the lookup finds. The condition
  above only covers the moment between lookup and write; this covers
  the time since the model read the event, during which someone else,
  or the user on another device, may have changed what it is about to
  change. The reply to a write carries the new `etag` when iCloud gives
  one, added after the write by code that can't fail. The ETag is shown
  without its quotes and accepted either way, to spare the model the
  escaping.
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
- **Every call to iCloud runs on one pool of 8 threads** shared by the
  whole process (`_ask`, `_per_calendar` and `_write` in `icloud.py`), so
  simultaneous tool calls can't multiply the requests in flight. Nothing
  submitted to the pool may itself wait on the pool: with all 8 threads
  doing that, none would be left to do the work. Per-calendar requests
  run concurrently, because iCloud needs one request per calendar. On an
  account with many calendars, a week's listing took about 5 seconds run in
  sequence and 1 to 2 run concurrently. A year-wide `query` search took
  between 3 and 12 seconds on that account, depending on the year: it
  still downloads and parses every event in the range, but expands only
  the series whose text matches.
- **Modal sizing:** `cpu=0.125`, `memory=256`, `max_containers=1`,
  `scaledown_window=60`, `timeout=90`. The single-container cap bounds what
  a flood of requests could cost. uv is pinned in the image build
  (`uv_version`) so a rebuild doesn't pick up whatever uv is newest.
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
- **A proxy token is wider than this app.** Without Modal RBAC it is valid
  for every proxy-protected web endpoint in the workspace; with RBAC, for
  the environments it was given. The README warns about this. The app's
  own comparison against `MCP_AUTH_TOKEN` is why another token from the
  same workspace doesn't open this server.
- **Read-only use is a client setting, not a server one.** Claude's
  connector settings let each tool be allowed, gated on approval, or
  blocked, grouped by the `readOnlyHint` annotations in `server.py`, so
  keep those annotations right. A server-side read-only switch was
  considered and left out.
- **The server has no boundary between calendars.** The token reaches
  every calendar and every tool, so nothing in the server stops a
  model that event text has misled from reading another calendar or
  changing an event. What stands in the way is on the client: the tools
  that write need approval by default, and can be blocked. Claude's
  settings page says it does not ask in every mode, so blocking is the
  only setting to count on. A server-side limit on which calendars can be
  written to would be a real boundary. It was weighed after a security
  review raised this and left out, as an accepted risk: writes need
  approval by default, blocking the write tools is reliable, and the
  setting would be one more thing to configure. The risk is still there
  for anyone who sets the write tools to always allowed, or uses a mode
  in which Claude does not ask.
- **Secrets live in a Modal Secret**, never in the repo or the image. `.env`
  is for local runs only and is gitignored.
- **App-specific password blast radius.** Apple's app-specific passwords are
  not scoped to calendars: one also works for iCloud mail and contacts. Use a
  password dedicated to this project so it can be revoked on its own at
  account.apple.com.
- **Never log** the token, the app password, request headers, or calendar
  contents.
- **Python warnings are printed by kind and origin only** (`_show_warning`
  in `icloud.py`, installed as `warnings.showwarning` for the process).
  A warning's text can quote what was being parsed: `icalendar` warns
  about an unusual timezone name by repeating it, and the name comes from
  the event. Warnings don't pass through `_run` or any logger setting, so
  they need their own rule. `warnings.catch_warnings` around the parsing
  was not an option: it is not safe across threads.
- **Errors go through one boundary** (`_run` in `server.py`). The client
  gets a message written for it and the log gets the tool name, the
  exception's class and the time taken. Never put `str(error)` in either:
  `caldav` errors carry URLs and response bodies, and an error that lists
  calendar names puts them in the logs. `icloud.CalendarError` is the one
  exception whose message may be shown, so its messages must not quote
  input or calendar data. `mask_error_details=True` is only the backstop.
- **Arguments of the wrong type never reach `_run`.** FastMCP refuses
  them itself, and its reply quotes the value that was sent, whatever
  `mask_error_details` says. It goes back to the caller who sent it and
  not into the logs, so nothing is exposed, but "errors never quote
  input" holds only for errors raised in `icloud.py`.
- **Whatever `caldav` logs is reduced to where it came from**
  (`_safe_record` in `icloud.py`, installed as the log record factory for
  the process). `caldav` logs exceptions that carry request URLs and
  iCloud's replies, and about ten of those calls go through the root
  logger, which the level set on the `caldav` logger doesn't reach.
- **A search that couldn't load every event is an error**
  (`_load_every_object` in `icloud.py`, which replaces
  `caldav.Calendar._batch_load_objects`). When iCloud names an event
  without its data, `caldav` fetches it in a second step, and if that
  fails it leaves the event out and returns the rest as if complete.
  Replacing a private method of a library is fragile; the test that
  covers it runs `caldav`'s real search, so a rename shows up there.
- **FastMCP logs tool arguments at DEBUG.** `server.py` holds that one
  logger at INFO, because the arguments include event text.
- **Host and Origin are checked** (FastMCP's `host_origin_protection`).
  Locally only loopback hosts are accepted; the Modal app adds
  `*.modal.run` (`ALLOWED_HOSTS` in `modal_app.py`). A request carrying an
  `Origin` header is refused unless it is the server's own origin; Claude's
  connector calls from a server and sends none. The bearer check stays
  outermost, so a request without the token learns nothing about any of
  this.
- **Request bodies are capped at 256 KiB** (`BodyLimitMiddleware`), after
  the token check and before any parsing.
- **Calendar names and event text are someone else's input.** A
  collaborator on a shared calendar, or anyone who sends an invitation,
  can put text in an event, and whoever shares a calendar names it. The
  server instructions tell the model to treat everything the tools
  return as data, and not to act on requests found in it. That is advice
  to the model, not a boundary (see above).
- **Verified against a real iCloud account (November 2026):** iCloud
  gives looked-up events strong ETags, refuses a conditional save or
  delete of a version that has since changed, and accepts either for the
  current version.
  The listing matched what `caldav`'s own expansion returned, event for
  event, over a two-week and a ninety-day range.
- **Verified read-only against a real iCloud account (November 2026),
  for the tools and arguments added then:** a search returns a strong
  ETag for every event; `get_event` returns the same one; an occurrence
  named by a listing is found again in its series; the listing matched
  the earlier code's, event for event, apart from the new keys.
- **Verified by writing to a real iCloud account (November 2026),** on
  throwaway events that were deleted afterwards: a series created with a
  link, an alert, the free mark and coordinates reads back with all of
  them; changing one occurrence changes only that one, which keeps its
  `occurrence` value and its copy of the alert; deleting one occurrence,
  and an occurrence with every later one, leaves exactly the others; a
  change to the series leaves a changed occurrence as it was; a call
  without `scope`, with a version that is not the current one, or for
  an occurrence the series lacks is refused and changes nothing. iCloud
  answers a create and a save with the new ETag, so the reply to a
  write carries it. Not verified: that Calendar draws the map for
  coordinates this server wrote, and that the alerts sound on a device.
- **Verified on a real deployment (November 2026):** calls from Claude's
  connector pass the Host and Origin checks on Modal with
  `ALLOWED_HOSTS = ['*.modal.run']`, the over-limit error reaches the
  client as written, and a year-wide `query` search finishes inside the
  time budget on the smallest container. Not checked there: that a wrong
  `Host` is refused, since that needs the token, and Modal routes by host
  before the app sees a request anyway.
- **The iCloud password goes only to iCloud.** `caldav` puts it on
  every request, and the addresses it asks come out of iCloud's answers.
  `_LimitedSession.send` refuses any that isn't HTTPS to
  `caldav.icloud.com` or one of the numbered `pNN-caldav.icloud.com`
  servers (`_is_icloud`). Every request leaves through `send`, each step
  of a redirect included, so a redirect out of iCloud is not followed.
  No tool takes a URL, so this is a second line, for an answer from
  iCloud that points somewhere it shouldn't. A read-only run against a
  real account asked only those two kinds of host. If Apple ever serves
  an account from a host named differently, every call fails with
  "somewhere other than iCloud" until the pattern is widened.
- **Answers from iCloud are read through `_LimitedSession`,** which
  replaces the HTTP session `caldav` creates. It reads each answer in
  16 KiB pieces and stops at 8 MiB after decompression, or once the
  request has gone on for the length of a tool call. `caldav` alone
  buffers an answer of any size, and its timeout only limits the silence
  between two pieces. The session sets a private attribute of the
  `niquests` response (`_content`) to hand the body on; a test sends real
  requests through `caldav` to a loopback server, so an upgrade that
  breaks this fails there.
- **What isn't bounded:** memory across requests. The cap is per answer,
  up to 8 answers can be in flight, and a listing holds every calendar's
  results until it has gone through them, so an account with many very
  full calendars can still use more memory than the smallest container
  has. Parsing an event is bounded only by the size of the answer it came
  in, not by `_bounded`.
- **One `caldav` client is shared by all threads.** The HTTP library under
  it (niquests) documents its sessions as thread-safe, and a live run of
  concurrent requests across many calendars returned exactly what the
  sequential code did.

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
- `SECURITY.md` sends reports to GitHub's private vulnerability
  reporting, which exists only on public repositories and has to be
  switched on in the repository settings when this one goes public.
  Secret scanning with push protection is the same.
- The licence is MIT (`LICENSE`): permissive, keeps the copyright notice
  on every copy, and disclaims warranty. Changing it is the maintainer's
  decision.

## Environment

- **uv** manages everything. Use `uv sync`, `uv add`, `uv run`. Never raw
  `pip`.
- Python **3.14**, pinned in `.python-version`. Modal's `debian_slim` image
  supports 3.14 only on image builder version `2025.06` or later. A
  workspace on an older builder (`2023.12` tops out at 3.12) fails the
  deploy with "Unsupported Python version". Raise the setting in the Modal
  dashboard, or run the deploy with `MODAL_IMAGE_BUILDER_VERSION=2026.10`.
- Dependencies: `modal`, `fastmcp`, `caldav`, plus `pytest` in the dev
  group. `icalendar`, `icalendar-searcher`, `niquests`, `starlette` and
  `uvicorn` come in with those but are declared too, because the code
  imports them. Add more only when the code needs them.
- CI audits the locked packages for known vulnerabilities on every push
  and once a week. To run it by hand: export with `uv export --frozen
  --no-dev --no-emit-project`, then run `uv tool run pip-audit
  --require-hashes --disable-pip -r` on the result.
- CI pins its actions to commits and uv to the version the Modal image
  uses. Raise those on purpose. The workflow uses no secrets, so it can run
  on pull requests from forks; keep it that way.
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
