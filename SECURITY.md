# Security

This server holds an iCloud app-specific password and can read and change
the calendars of whoever deployed it. A flaw here is a flaw in every
deployed copy, so reports are welcome and taken seriously.

## Reporting a problem

Please don't open a public issue for a security problem: that tells
everyone before there is a fix.

Use GitHub's private report instead: open this repository's **Security**
tab and choose **Report a vulnerability**. Only the maintainer sees it.

Helpful things to include:

- what you found and what it lets someone do;
- how to reproduce it, ideally against the test suite or a local run;
- the commit you tested.

Use made-up credentials and events in a report, never real ones, and only
test against a deployment that is yours.

## What counts

Anything that lets someone:

- reach a tool, or any route, without the bearer token;
- get calendar data, credentials or the token into logs or error messages;
- make the server send the iCloud credentials anywhere but iCloud;
- hang or crash the server with a request or with a calendar event;
- get around the limits and checks described in the README.

By design, anyone who holds the token can read and change every calendar
on the account. That alone is not a vulnerability.

## Supported versions

Only the latest commit on `main` is supported. If you deployed your own
copy, pull and redeploy to pick up fixes.

## If your own token or password leaks

That is not a bug in this project, but act quickly: replace the token as
described under "Add it to Claude" in the README, and revoke the
app-specific password at [account.apple.com](https://account.apple.com).
