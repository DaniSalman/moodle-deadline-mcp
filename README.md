# Moodle Deadline Assistant: an MCP toolkit

**EECE 503P/798S Agentic Systems, C5: Agent Tools.** Dani Salman.

You ask in plain English "what's due this week?" or "put all my deadlines in my
calendar and remind me 6 hours before". The agent reads your AUB Moodle
calendar, creates Google Calendar events, and Google notifies your phone X
hours before each deadline.

It is built from exactly three MCP tools served by **FastMCP** and used by an
agent built with the **OpenAI Agents SDK**:

| Category | Tool | What it does |
|---|---|---|
| **Data** | `get_upcoming_deadlines` | Reads your Moodle calendar export and returns the deadlines due in the next N days. Read-only. |
| **Action** | `add_deadline_to_calendar` | Creates **one** Google Calendar event at a deadline with a popup reminder X hours before. Never duplicates. If Moodle moves the deadline, it moves the same event. |
| **Orchestration** | `sync_deadlines_to_calendar` | Runs the Data step once, then the Action step for every deadline found, and reports what was added, updated, skipped or failed. |

There is also one read-only resource, `deadline-assistant://settings` (time
zone, default reminder, which calendar), which the client reads when it starts.

- **Demo video:** _add the link here after recording_
- **Written summary (Part 1):** [docs/summary.md](docs/summary.md)
- **Reflection:** [docs/reflection.md](docs/reflection.md)
- **Requirements and design (written before the code):** [docs/requirements.md](docs/requirements.md)

## How it fits together

```
 you ──> terminal chat  (deadline_mcp/chat.py)
           OpenAI Agents SDK  Agent + Runner        = MCP host
           MCPServerStreamableHttp / MCPServerStdio = MCP client
              │   JSON-RPC: initialize, tools/list, resources/read, tools/call
              ▼
         FastMCP server  (deadline_mcp/server.py)   = MCP server
           ├─ get_upcoming_deadlines      ──> Moodle calendar export (iCal)
           ├─ add_deadline_to_calendar    ──> Google Calendar API v3
           ├─ sync_deadlines_to_calendar  ──> both, in sequence
           └─ deadline-assistant://settings
```

The agent never sees Python functions. It sees the tool names, descriptions and
JSON schemas the server publishes through `tools/list`, picks a tool, and the
MCP client sends a `tools/call`. The server's typed result (or a clear error)
comes back as the tool output the model reads before answering.

```
deadline_mcp/
  server.py         FastMCP server: the 3 tools, the resource, request logging
  chat.py           Agents SDK host/client: terminal chat that prints each round trip
  core.py           the three capabilities as plain functions (shared by the tools)
  moodle.py         download + parse the Moodle export, recognise deadline titles
  calendars.py      Google Calendar backend + local JSON stand-in
  models.py         typed tool outputs (pydantic -> MCP output schemas)
  config.py         settings from .env
  errors.py         the expected failures that become MCP tool errors
  offline_model.py  keyword stand-in for the LLM (tests / rehearsal without a key)
  sample_feed.py    a Moodle-format sample export with dates relative to today
scripts/
  google_auth.py      one-time Google sign-in (creates token.json)
  try_tools.py        call each tool directly over MCP, failure cases included
  make_sample_feed.py write samples/moodle_sample.ics for offline rehearsal
tests/              95 tests, all offline (no OpenAI, Moodle or Google calls)
```

## Setup

You need Python 3.12 or newer. The project was tested with Python 3.14 on Windows 11.

```bash
python -m venv .venv
.venv\Scripts\activate          # macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
copy .env.example .env          # macOS/Linux: cp .env.example .env
```

Then fill in `.env`:

1. **`OPENAI_API_KEY`**: your OpenAI key. `OPENAI_MODEL` can stay empty to use
   the Agents SDK default model.
2. **`MOODLE_ICAL_URL`**: in Moodle go to **Calendar → Import or export
   calendars → Export calendar**, choose **All events** and **Recent and next
   60 days**, press **Get calendar URL**, and paste the URL. It contains a
   private token, so keep it only in `.env`.
3. **Google Calendar** (one time, about 5 minutes):
   1. Open <https://console.cloud.google.com/>, create a project (any name).
   2. **APIs & Services → Library**, search **Google Calendar API**, press **Enable**.
   3. **Google Auth Platform** (OAuth consent screen): app name and your email.
      Set the audience to **External** and add your own Gmail address as a
      **test user**.
   4. **Clients → Create client → Desktop app**. Download the JSON and save it
      in this folder as **`credentials.json`**.
   5. Run `python scripts/google_auth.py`. A browser opens. Approve access to
      your calendar events, and it saves `token.json`.

   The app asks only for the `calendar.events` scope, so it can manage events
   but cannot touch calendar settings or sharing. While the Google app is in
   "testing" mode, Google expires the sign-in after 7 days. If that happens,
   run `scripts/google_auth.py` again.

## Run it

Terminal 1, the MCP server (it prints every request it receives):

```bash
python -m deadline_mcp.server
```

Terminal 2, the agent:

```bash
python -m deadline_mcp.chat
```

Things to ask:

- `what's due this week?`
- `anything due for EECE 503P in the next 30 days?`
- `add the C5 assignment to my calendar and remind me 6 hours before`
- `sync all my deadlines for the next two weeks to my calendar`
- `add "essay draft" due 2020-01-01 to my calendar` (the failure case: the deadline already passed)

Chat commands: `/tools` (what was discovered), `/schemas` (the JSON schemas the
model sees), `/context` (the conversation items sent to the model next turn),
`/reset`, `/quit`.

For each turn the client prints:

```
* LLM call 1 -> <model>: instructions + 1 conversation item(s) + 3 tool schema(s)
  model asked for a tool call: get_upcoming_deadlines({"days_ahead": 7})
> MCP client -> server: tools/call get_upcoming_deadlines {"days_ahead": 7}
< server -> MCP client: DeadlineList with 3 deadline(s) (C5 - Agent Tools is due, ...)
* LLM call 2 -> <model>: instructions + 3 conversation item(s) + 3 tool schema(s)
  model wrote the final answer
```

Other ways to run it:

- `python -m deadline_mcp.chat --stdio` starts the server itself as a
  subprocess over stdio, so you only need one terminal.
- `python -m deadline_mcp.chat --ask "what's due this week?"` asks one
  question and exits.
- `--no-trace` stops uploading traces to the OpenAI dashboard. Traces are on by
  default when a key is set; see platform.openai.com → Traces.

### Rehearse without any keys

```bash
python scripts/make_sample_feed.py
```

In `.env`, set `MOODLE_ICAL_URL=samples/moodle_sample.ics` and
`CALENDAR_BACKEND=local`. Then:

```bash
python -m deadline_mcp.chat --stdio --offline
```

`--offline` swaps the LLM for a small keyword-matching stand-in
(`offline_model.py`). Everything else is the same pipeline: agent, MCP client,
server, tools. Events go to `data/local_calendar.json` instead of Google.

## Tests

```bash
python -m pytest
```

There are 95 tests, and none of them call OpenAI, Moodle or Google. They are
named after the requirement IDs in [docs/requirements.md](docs/requirements.md):

- `test_data_tool.py` (D1–D8): parsing a Moodle-format export, the time
  window, the course filter, and the failures. Those are a missing URL, Moodle
  unreachable, timeout or HTTP 503, an expired token ("Invalid
  authentication"), and an out-of-range `days_ahead`. It also checks that the
  token never leaks into any message.
- `test_action_tool.py` (A1–A7): the event body sent to Google, no duplicates,
  and the failures. Those are a bad date, a deadline that already passed, not
  signed in, Google refusing (HTTP 403) or unreachable, a reminder time that
  already passed, and out-of-range hours.
- `test_orchestration_tool.py` (O1–O4): sync everything, re-sync adds nothing,
  a Data failure stops the sync before any write, and one failed item doesn't
  stop the rest.
- `test_mcp_server.py`: the real MCP protocol (in-memory). It checks there are
  exactly three tools, one per category, plus their annotations, schemas and
  structured output. Every bad call must come back as `isError` with a clear
  message.
- `test_agent_roundtrip.py`: the Agents SDK agent talks to the server as a
  stdio subprocess. It covers a question, then list → add one → sync all, and
  a tool error reaching the model as text.
- `test_qa_regressions.py`: one or more tests for each issue the QA review
  found. These cover a token leak through `webcal://` links, a moved deadline
  left stale, duplicates after adding by hand, terminal markup injection,
  checking input before Google sign-in, date-only input, DST, unreadable
  events, and the server stopping mid-session.

To call the tools by hand without an LLM (the failure cases included):

```bash
python scripts/try_tools.py            # read-only + failure cases
python scripts/try_tools.py --write    # also the Action and Orchestration tools
```

## Design notes

- **One effect per tool.** "Add it and notify me" sounds like two actions. The
  notification is a property of the calendar event (a popup reminder), so
  Google sends it and the Action tool still does one thing.
- **Idempotent writes.** Every event stores a private `dedupKey` (the Moodle
  event UID) plus the due time and a normalised title. Re-running a sync never
  duplicates events, even when a deadline was first added by hand. When Moodle
  moves a deadline, the same event is moved (`status: updated`), so the
  reminder follows the new date.
- **Partial failure is reported, never hidden.** The sync returns
  `created`, `already_in_calendar` and `failed` (with reasons).
- **The loop lives in the server.** One `sync_deadlines_to_calendar` call
  replaces 1 + N model-driven tool calls: fewer tokens, fixed order, one report.
- **Validate before acting.** Every argument is checked before Google is
  even opened. A date without a time means 23:59, the same rule the Moodle
  parser uses.
- **Secrets stay out.** Only the host name of the Moodle URL is ever shown,
  whatever the link's scheme (`webcal://` links are fetched as `https://`).
  `.env`, `credentials.json` and `token.json` are git-ignored.

## Troubleshooting

| Message | Fix |
|---|---|
| `The Moodle feed is not configured` | Put the export URL in `.env` as `MOODLE_ICAL_URL`. |
| `Moodle did not return calendar data ... Invalid authentication` | The export link expired. Make a new one in Moodle and update `.env`. |
| `Not signed in to Google Calendar yet` | `python scripts/google_auth.py` |
| `Could not connect to the MCP server` | Start `python -m deadline_mcp.server` first, or use `--stdio`. |
| `The MCP call failed ... the server may have stopped` | The server terminal was closed mid-chat; start it again. |
| `OPENAI_API_KEY is not set` | Add it to `.env`, or try `--offline`. |
