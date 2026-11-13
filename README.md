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

## What you need

- **An Apple account** with iCloud calendars and two-factor authentication
  turned on. Apple only issues app-specific passwords to accounts that
  have it.
- **A Modal account.** The free Starter plan includes $30 of credits a
  month, and this server is sized to use a small fraction of that.
- **A Claude account that can send request headers to a custom
  connector.** That option is in beta and not on every account, so check
  before you start: in Claude, open Customize > Connectors > Add custom
  connector and look for a "Request headers" section. Without it, Claude
  has no way to send this server its token.
- **A terminal on macOS or Linux.** The commands below are written for
  one.

You don't need Python installed. uv, installed in step 2, fetches it.

## Setup

Nine short steps. Step 7 is optional.

### 1. Get the code

Download the repository:

```bash
git clone https://github.com/samkorn/icloud-calendar-mcp.git
```

Move into it. Every later command is run from this folder:

```bash
cd icloud-calendar-mcp
```

### 2. Install uv and the dependencies

[uv](https://docs.astral.sh/uv/) is the Python package manager this project
uses. If you don't have it, install it (the
[uv docs](https://docs.astral.sh/uv/getting-started/installation/) list other
ways, such as `brew install uv`):

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

Then install the project:

```bash
uv sync
```

This downloads Python 3.14 if you don't have it, creates a `.venv` folder
here, and installs the exact package versions recorded in `uv.lock`,
including Modal's command-line tool. Nothing is installed outside this
folder. Every command from here on starts with `uv run`, which runs it
inside that environment.

### 3. Connect to Modal

If you don't have a Modal account, create one at
[modal.com](https://modal.com). Then log this computer in:

```bash
uv run modal setup
```

This opens your browser and asks you to sign in to Modal and approve a new
API token, which it saves to `~/.modal.toml`. When the terminal prints
"Token verified successfully!", you're done. That is all the Modal setup
there is: the remaining steps create everything else from the command line.

### 4. Create an app-specific password for iCloud

This is the password the server uses to reach your calendars, in place of
your real Apple password.

1. Sign in at [account.apple.com](https://account.apple.com).
2. Open Sign-In and Security > App-Specific Passwords.
3. Add one and name it `icloud-calendar-mcp`.
4. Copy the password Apple shows (`xxxx-xxxx-xxxx-xxxx`). You'll paste it
   into `.env` in step 6.

Make a new one just for this server and don't reuse it. An app-specific
password also works for iCloud mail and contacts, so you want to be able to
revoke this one on its own.

### 5. Create the token Claude will send

The server only answers requests that carry a secret token. That token is a
Modal proxy auth token. Create one:

```bash
uv run modal workspace proxy-tokens create --name icloud-calendar-mcp
```

Modal prints a small table. Find the line that reads
`Authorization: Bearer wk-....ws-...` and copy everything after `Bearer `:
the token ID and the token secret, joined by a period. You'll paste it into
`.env` in the next step. The secret is shown only this once; if you lose
it, delete the token and create another (see "Change the token").

A proxy token is not tied to one app. Unless your workspace uses Modal's
[RBAC](https://modal.com/docs/guide/rbac), where tokens are limited to the
environments you pick, it is accepted by every proxy-protected endpoint
in the workspace. If you run other endpoints that rely on proxy auth
alone, this token opens those too, so think about where you paste it.

### 6. Fill in `.env`

`.env` is the file that holds your three secrets on this computer. Git
ignores it, so it can't be committed by accident. Create it from the
template:

```bash
cp .env.example .env
```

Open `.env` in a text editor and put each value after its `=`, with no
quotes or spaces:

| Key                   | Value                                             |
| --------------------- | ------------------------------------------------- |
| `ICLOUD_USERNAME`     | Your Apple ID email                               |
| `ICLOUD_APP_PASSWORD` | The app-specific password from step 4             |
| `MCP_AUTH_TOKEN`      | The `wk-....ws-...` token from step 5             |

### 7. Try it locally (optional)

This checks your iCloud login before anything is deployed. Start the
server on your own machine:

```bash
uv run --env-file .env python server.py
```

`--env-file .env` makes uv load your three secrets into the server's
environment. The server listens on `http://127.0.0.1:8000/mcp`, reachable
only from this computer, and keeps running until you press Ctrl+C.

In a second terminal, in the same folder, ask it for your calendars:

```bash
uv run --env-file .env sh -c 'curl -s http://127.0.0.1:8000/mcp \
  -H "Authorization: Bearer $MCP_AUTH_TOKEN" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d "{\"jsonrpc\": \"2.0\", \"id\": 1, \"method\": \"tools/call\", \"params\": {\"name\": \"list_calendars\", \"arguments\": {}}}"'
```

This sends one `list_calendars` call with your token, which uv reads from
`.env` so you never type it. The reply is a line of JSON containing your
calendar names. If it holds an error message instead, check the Apple ID
email and app-specific password in `.env`, restart the server and try
again.

To explore the other tools by hand, the
[MCP Inspector](https://modelcontextprotocol.io/docs/tools/inspector) can
connect to the same URL over Streamable HTTP with an `Authorization` header
of `Bearer <your MCP_AUTH_TOKEN>`.

### 8. Deploy to Modal

Copy the three values from `.env` into a Modal Secret named
`icloud-calendar-mcp`, which is where the deployed server reads them from:

```bash
uv run modal secret create icloud-calendar-mcp --from-dotenv .env
```

Build and deploy the server:

```bash
uv run modal deploy modal_app.py
```

The first deploy is the slow one, because Modal builds the image. When it
finishes, Modal prints the URL of the `web` function, ending in
`.modal.run`. The MCP server is at that URL plus `/mcp`. Keep it for the
next step.

To confirm the endpoint is locked, request it without the token (put your
own URL in):

```bash
curl -i https://YOUR-URL.modal.run/mcp
```

The first line of the reply should be a `401`. That is Modal refusing a
request that has no token.

If the deploy fails with "Unsupported Python version: '3.14'", your Modal
workspace is on an old image builder version. Raise it in the Modal dashboard
under Settings > Image Config, or override it for this deploy:

```bash
MODAL_IMAGE_BUILDER_VERSION=2026.10 uv run modal deploy modal_app.py
```

The deployment scales to zero when idle and is capped at one small CPU-only
container, so it costs little or nothing on Modal's free credits. The first
request after an idle period takes a few seconds while a container starts.

### 9. Add it to Claude

In Claude, open Customize > Connectors > Add custom connector:

- URL: your Modal URL plus `/mcp`
- Transport: Streamable HTTP
- Authentication: No sign-in
- Request headers: name `authorization`, value `Bearer <your MCP_AUTH_TOKEN>`
  (type the `Bearer ` prefix yourself, then paste the token from `.env`)

Then ask Claude something like "What's on my calendar this week?" to see it
work.

Once it is added, the connector's page in the same settings lists the five
tools, and each can be set to always allowed, needing approval, or blocked.
To let Claude see your calendars without being able to change them, block
`create_event`, `update_event` and `delete_event`. "Needs approval" is the
default for those three, but the note on that page says Claude does not ask
in every mode, so block a tool you never want used rather than count on
being asked.

## After setup

### Update the server

Fetch the latest code:

```bash
git pull
```

Deploy it. This replaces the running version and keeps the same URL, so
nothing changes in Claude:

```bash
uv run modal deploy modal_app.py
```

### See the logs

Print the deployed server's recent log lines, which record each tool call's
name and how long it took, never calendar contents:

```bash
uv run modal app logs icloud-calendar-mcp
```

### Change the token

Claude does not let you edit a connector's authentication afterwards, so
replacing the token means going back through a few of the setup steps:

1. Create a new proxy token (step 5) and put it in `.env` as
   `MCP_AUTH_TOKEN`.
2. Overwrite the Modal Secret with the new values:

   ```bash
   uv run modal secret create icloud-calendar-mcp --from-dotenv .env --force
   ```

3. Redeploy so the server picks up the new secret:

   ```bash
   uv run modal deploy modal_app.py
   ```

4. In Claude, remove the connector and add it again with the new token
   (step 9).
5. Find the old token's ID (it starts with `wk-`):

   ```bash
   uv run modal workspace proxy-tokens list
   ```

6. Delete the old token, so it stops working:

   ```bash
   uv run modal workspace proxy-tokens delete <token-id>
   ```

### Take it down

Stop the server for good. Its URL stops answering:

```bash
uv run modal app stop icloud-calendar-mcp
```

Delete the stored secrets from Modal:

```bash
uv run modal secret delete icloud-calendar-mcp
```

Then delete the proxy token as in "Change the token", remove the connector
in Claude, and revoke the app-specific password at
[account.apple.com](https://account.apple.com).

## Development

Run the tests. They use no network and need no iCloud account or `.env`:

```bash
uv run pytest
```

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
other people. Claude is told to treat them as data, but keep approval on for
the tools that change things, or block them (see step 9).

To report a security problem, see [SECURITY.md](SECURITY.md).
