# Requirements and design: Moodle Deadline Assistant (C5)

EECE 503P/798S, C5: Agent Tools. Dani Salman.

This file was written before the code. It fixes what the toolkit must do, where
each tool's boundary sits, and how every failure is reported. The tests in
`tests/` check these requirements one by one (IDs in brackets).

## 1. Problem

Moodle (lms.aub.edu.lb) knows every assignment, quiz and lab deadline, but it
only tells you when you open it. A student wants:

1. to ask "what's due this week?" in plain language,
2. to have those deadlines appear in Google Calendar, and
3. to get a phone notification some hours before each one.

## 2. Scope

In scope: reading the student's own Moodle calendar export, creating Google
Calendar events with a reminder, and an agent that chooses between these.

Out of scope: logging in to Moodle with a password, submitting work, editing or
deleting calendar events, multi-user hosting, background schedulers.

## 3. Architecture

```
 you ──> terminal chat (deadline_mcp/chat.py)
          │  OpenAI Agents SDK: Agent + Runner      = MCP Host
          │  MCPServerStreamableHttp / Stdio        = MCP Client
          ▼  JSON-RPC over Streamable HTTP (or stdio)
        FastMCP server (deadline_mcp/server.py)     = MCP Server
          ├─ get_upcoming_deadlines      (Data)
          ├─ add_deadline_to_calendar    (Action)
          ├─ sync_deadlines_to_calendar  (Orchestration)
          └─ resource deadline-assistant://settings
               │                    │
        Moodle iCal export     Google Calendar API v3
```

The server tools are thin wrappers. The logic sits in plain Python modules
(`moodle.py`, `calendars.py`, `core.py`), so it can be tested without MCP and
the orchestration tool can call the same functions the other two tools use.

## 4. Data sources and side effects

| Thing | How we reach it | Why this way |
|---|---|---|
| Moodle deadlines | The personal calendar export URL (Moodle → Calendar → Import or export calendars → Export calendar → Get calendar URL), stored in `.env` as `MOODLE_ICAL_URL` | Read-only, no password handling, works with AUB's SSO. Moodle's own export code builds the feed with `UID = <event id>@<host>`, `SUMMARY = event name`, `DTSTART = DTEND` for deadlines, and `CATEGORIES = course short name`. |
| Google Calendar | Calendar API v3 with OAuth (desktop app flow), scope `calendar.events` only | Least privilege: the server can manage events but cannot change calendar settings or sharing. |
| Notification | The created event's own reminder (`popup`, X hours before) | Google delivers the push to the phone. No background process has to stay alive, and the Action tool keeps a single effect. |

`MOODLE_ICAL_URL` may also be a path to a local `.ics` file. Tests and the
offline rehearsal use this with a generated sample feed.

## 5. The three tools

### 5.1 Data tool: `get_upcoming_deadlines`

Looks up upcoming Moodle deadlines. Never changes anything.

- Input: `days_ahead: int` (1 to 60, default 14), `course: str | None`
  (case-insensitive match on the course short name).
- Output (`DeadlineList`): `source` (host only), `timezone`, `window_start`,
  `window_end`, `count`, `deadlines[]` (each: `uid`, `course`, `title`,
  `activity`, `kind`, `due_at`, `due_in_hours`), `ignored_events`.
- What counts as a deadline: event titles Moodle generates for closing dates,
  so "... is due", "... closes", "... deadline" and "... should be completed".
  "... opens" events, lectures and other timed events are counted in
  `ignored_events` and left out.
- Sorted by due time, soonest first. Times are shown in `TIMEZONE`
  (default Asia/Beirut).
- [D1] Parses a real Moodle-format feed into deadlines with course and due time.
- [D2] Returns only deadlines inside `[now, now + days_ahead]`.
- [D3] Filters by course when asked.
- [D4] Failure: feed URL missing from `.env` → clear error naming the setting.
- [D5] Failure: Moodle unreachable (network error, timeout, HTTP error) →
  clear error saying Moodle could not be reached, with the host only.
- [D6] Failure: Moodle answers with something that is not a calendar (for
  example "Invalid authentication" from an expired token) → clear error telling
  the user to generate a new export URL.
- [D7] `days_ahead` outside 1 to 60 is rejected by the input schema.
- [D8] The secret export token never appears in any output, error or log line.

### 5.2 Action tool: `add_deadline_to_calendar`

Creates one Google Calendar event for one deadline, with a reminder.

- Input: `title: str`, `due_at: str` (ISO 8601; no offset means `TIMEZONE`),
  `remind_hours_before: float` (0 to 672, default from `.env`, 24),
  `course: str | None`, `moodle_uid: str | None`.
- Output (`CalendarAddResult`): `status` (`created` or `already_exists`),
  `event_id`, `event_link`, `calendar`, `summary`, `due_at`, `reminder_at`,
  `reminder_already_passed`, `message`.
- Event shape: starts at the deadline and lasts 15 minutes, so a reminder
  "X hours before the start" is exactly X hours before the deadline. Title
  `[COURSE] title`. Popup reminder only.
- Idempotent: every event carries a private extended property `dedupKey`
  (the Moodle UID, or a hash of title + due time for manual entries). If an
  event with that key exists, nothing new is created and the existing event
  is returned with `status = already_exists`.
- [A1] Creates an event with the right start, end, title and reminder minutes.
- [A2] Calling it twice with the same deadline creates one event, not two.
- [A3] Failure: `due_at` that is not ISO 8601 → clear error with an example.
- [A4] Failure: `due_at` in the past → clear error, nothing created.
- [A5] Failure: not signed in to Google (no token) or Google rejects the call
  → clear error telling the user to run `scripts/google_auth.py`.
- [A6] A reminder time that has already passed still creates the event but is
  flagged (`reminder_already_passed = true`) so the agent can say so.
- [A7] `remind_hours_before` outside 0 to 672 (Google's 4-week limit) is
  rejected by the input schema.

### 5.3 Orchestration tool: `sync_deadlines_to_calendar`

Copies every upcoming Moodle deadline into Google Calendar in one request by
running the Data step once and the Action step once per deadline.

- Input: `days_ahead` (1 to 60, default 14), `remind_hours_before`
  (0 to 672, default from `.env`), `course: str | None`.
- Output (`SyncReport`): `checked`, `created[]`, `already_in_calendar[]`,
  `failed[]` (title + reason), `message`.
- [O1] Creates events for all deadlines in the window and reports them.
- [O2] Running it twice creates nothing the second time (all
  `already_in_calendar`).
- [O3] Failure: if the Data step fails, the whole sync fails with that error
  and nothing is written.
- [O4] Failure: if one Action step fails, the others still run and the failure
  is listed in `failed[]` with its reason (partial success is reported, never
  hidden).

### Why the line is drawn here

- Reading and writing are separate tools, so the read-only one can be called
  freely and the agent (or a human approval step) can treat writes differently.
  MCP annotations say this too: the Data tool is `readOnlyHint`, the others are
  `idempotentHint` and not destructive.
- "Add to calendar *and* notify me" would be two effects. The notification is a
  property of the calendar event, so it stays one effect.
- The sync loop runs inside the server instead of in the model: one model
  decision instead of 1 + N tool calls, deterministic order, and a single
  report. The agent can still do it step by step with the two smaller tools.
- No `dry_run` flag on the sync tool: the Data tool already is the dry run.

## 6. Resource

`deadline-assistant://settings` (read-only JSON): timezone, default reminder
hours, calendar backend and ID, Moodle host (no token), tool list with
categories. The terminal client reads it at startup and puts the timezone and
reminder default into the agent's instructions.

## 7. Host / client

- Built with the OpenAI Agents SDK. One `Agent` with the MCP server attached
  (`mcp_servers=[...]`), so its tools are discovered with `tools/list` instead
  of being hard-coded.
- Instructions are rebuilt on every turn and include the current local date and
  time, so "this week" and "tomorrow" resolve correctly.
- The client never writes to the calendar on its own: instructions say to call
  the Action or Orchestration tool only when the user asks for it.
- The terminal shows the full round trip of each turn: the model's tool call
  and arguments, the request going to the MCP server, the server's result (or
  error), and the final answer with token usage. `/context` prints the
  conversation items the model receives next turn.
- [H1] Connects over Streamable HTTP by default, or launches the server over
  stdio with `--stdio`.
- [H2] Works end to end without an OpenAI key using `--offline` (a keyword-based
  stand-in model), so the round trip can be tested and rehearsed for free.

## 8. Non-functional

- Python 3.12+; tested on Windows with Python 3.14.
- Secrets (`OPENAI_API_KEY`, `MOODLE_ICAL_URL`, Google `credentials.json` and
  `token.json`) live only in `.env` and git-ignored files.
- The test suite runs offline: no OpenAI, Moodle or Google calls.
- Server logs go to stderr (stdout is the protocol channel under stdio).
