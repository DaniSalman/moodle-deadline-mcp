"""FastMCP server: the three tools and one resource.

Run it over Streamable HTTP (default, http://127.0.0.1:8765/mcp):
    python -m deadline_mcp.server
or over stdio (the client launches it as a subprocess):
    python -m deadline_mcp.server --stdio
"""

from __future__ import annotations

import argparse
import json
import time
from typing import Annotated, Any

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.middleware import Middleware, MiddlewareContext
from pydantic import Field
from rich.console import Console
from rich.markup import escape

from . import __version__, core
from .calendars import open_calendar
from .config import DEFAULT_PORT, MAX_DAYS_AHEAD, MAX_REMIND_HOURS, describe_source, load_settings
from .errors import AssistantError
from .models import CalendarAddResult, DeadlineList, SyncReport

# stdout carries the protocol under stdio, so every log line goes to stderr.
log = Console(stderr=True, highlight=False)

TOOL_CATEGORIES = {
    "get_upcoming_deadlines": "data",
    "add_deadline_to_calendar": "action",
    "sync_deadlines_to_calendar": "orchestration",
}


class RoundTripLogger(Middleware):
    """Prints every request the server receives and what it sends back."""

    async def on_list_tools(self, context: MiddlewareContext, call_next):
        tools = await call_next(context)
        log.print(f"[cyan]<- tools/list[/]  client asked what tools exist; sent {len(tools)}")
        return tools

    async def on_read_resource(self, context: MiddlewareContext, call_next):
        log.print(f"[cyan]<- resources/read[/] {escape(str(context.message.uri))}")
        return await call_next(context)

    async def on_call_tool(self, context: MiddlewareContext, call_next):
        name = context.message.name
        args = json.dumps(context.message.arguments or {}, ensure_ascii=False)
        category = TOOL_CATEGORIES.get(name, "?")
        log.print(f"[bold cyan]<- tools/call[/] [bold]{escape(name)}[/] [dim]({category})[/] {escape(args)}")
        started = time.perf_counter()
        try:
            result = await call_next(context)
        except Exception as exc:
            elapsed = time.perf_counter() - started
            log.print(f"[bold red]-> error[/] after {elapsed:.2f}s: {escape(str(exc))}")
            raise
        elapsed = time.perf_counter() - started
        log.print(f"[bold green]-> result[/] after {elapsed:.2f}s: {escape(_describe(result.structured_content))}")
        return result


def _describe(payload: dict[str, Any] | None) -> str:
    if not payload:
        return "(no structured content)"
    if "deadlines" in payload:
        return f"{payload['count']} deadline(s), {payload['ignored_events']} other event(s) skipped"
    if "checked" in payload:
        return payload["message"]
    if "status" in payload:
        return f"{payload['status']}: {payload['summary']}"
    return json.dumps(payload)[:160]


mcp = FastMCP(
    name="moodle-deadlines",
    version=__version__,
    instructions=(
        "Tools for a university student's Moodle deadlines. "
        "get_upcoming_deadlines only reads. add_deadline_to_calendar writes one event. "
        "sync_deadlines_to_calendar reads Moodle and writes every upcoming deadline in one step. "
        "Only write to the calendar when the user asks for it."
    ),
    middleware=[RoundTripLogger()],
    # Unexpected crashes are reported generically; expected problems use ToolError and keep their message.
    mask_error_details=True,
)


def _run(fn, *args, **kwargs):
    """Call a core function, turning expected failures into MCP tool errors."""
    try:
        return fn(*args, **kwargs)
    except AssistantError as exc:
        raise ToolError(str(exc)) from exc


DaysAhead = Annotated[
    int,
    Field(ge=1, le=MAX_DAYS_AHEAD, description="How many days ahead of now to look (1-60; Moodle exports 60 days)."),
]
RemindHours = Annotated[
    float | None,
    Field(
        ge=0,
        le=MAX_REMIND_HOURS,
        description="How many hours before the deadline the phone notification should fire (0-672). "
        "Leave empty to use the user's default.",
    ),
]
CourseFilter = Annotated[
    str | None,
    Field(description="Optional course short name, e.g. 'EECE503P' (case and spaces ignored). Empty means all courses."),
]


@mcp.tool(
    annotations={
        "title": "Get upcoming Moodle deadlines",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
    meta={"category": "data"},
    tags={"data"},
)
def get_upcoming_deadlines(days_ahead: DaysAhead = 14, course: CourseFilter = None) -> DeadlineList:
    """Look up the student's upcoming Moodle deadlines (assignments due, quizzes closing, expected completions).

    Read-only: changes nothing. Reads the student's Moodle calendar export and returns only deadlines due
    between now and `days_ahead` days from now, soonest first, in the student's time zone. Events that are
    not deadlines (a quiz opening, a lecture) are counted in `ignored_events` but not listed.
    """
    return _run(core.get_upcoming_deadlines, _run(load_settings), days_ahead, course)


@mcp.tool(
    annotations={
        "title": "Add one deadline to Google Calendar",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
    meta={"category": "action"},
    tags={"action"},
)
def add_deadline_to_calendar(
    title: Annotated[str, Field(min_length=1, max_length=300, description="What is due, e.g. 'C5 - Agent Tools is due'.")],
    due_at: Annotated[
        str,
        Field(
            max_length=40,
            description="When it is due, ISO 8601, e.g. '2026-11-05T23:59' (student's time zone) or with an offset. "
            "A date alone means 23:59 that day.",
        ),
    ],
    remind_hours_before: RemindHours = None,
    course: Annotated[
        str | None, Field(max_length=60, description="Course short name to show in the event title, e.g. 'EECE503P'.")
    ] = None,
    moodle_uid: Annotated[
        str | None,
        Field(
            max_length=200,
            description="The deadline's `uid` from get_upcoming_deadlines, if it came from Moodle. Prevents duplicates.",
        ),
    ] = None,
) -> CalendarAddResult:
    """Create ONE Google Calendar event at a deadline, with a phone notification X hours before it.

    The event starts at the deadline (15 minutes long) and has a popup reminder `remind_hours_before`
    hours earlier. Safe to repeat: if this deadline is already in the calendar, no second event is created
    (status 'already_exists'); if its time moved in Moodle or a different reminder is asked for, that same
    event is changed (status 'updated'). Refuses deadlines that have already passed.
    """
    settings = _run(load_settings)
    # Validate first, so a bad request is reported as such even before signing in to Google.
    deadline = _run(core.prepare_deadline, settings, title, due_at, remind_hours_before, course, moodle_uid)
    calendar = _run(open_calendar, settings)
    return _run(core.write_deadline, settings, calendar, deadline)


@mcp.tool(
    annotations={
        "title": "Sync all upcoming Moodle deadlines into Google Calendar",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
    meta={"category": "orchestration"},
    tags={"orchestration"},
)
def sync_deadlines_to_calendar(
    days_ahead: DaysAhead = 14,
    remind_hours_before: RemindHours = None,
    course: CourseFilter = None,
) -> SyncReport:
    """Copy every upcoming Moodle deadline into Google Calendar in one step, each with a reminder.

    Runs get_upcoming_deadlines once, then add_deadline_to_calendar for each deadline found. Deadlines
    already in the calendar are skipped, ones Moodle moved are updated, and if one deadline fails the rest
    still go through; the report lists what was created, updated, already there, and failed (with the reason).
    Without remind_hours_before, existing events keep their reminder and new ones get the default.
    """
    settings = _run(load_settings)
    return _run(
        core.sync_deadlines_to_calendar,
        settings,
        lambda: _run(open_calendar, settings),
        days_ahead,
        remind_hours_before,
        course,
    )


@mcp.resource(
    "deadline-assistant://settings",
    name="settings",
    description="The assistant's non-secret settings: time zone, default reminder, which calendar and Moodle host.",
    mime_type="application/json",
)
def settings_resource() -> dict[str, Any]:
    settings = _run(load_settings)
    return {
        "server_version": __version__,
        "timezone": settings.timezone,
        "default_remind_hours": settings.default_remind_hours,
        "calendar": f"{settings.calendar_backend}:{settings.google_calendar_id}"
        if settings.calendar_backend == "google"
        else f"local:{settings.local_calendar_file.name}",
        "moodle_source": describe_source(settings.moodle_ical_url),
        "tools": TOOL_CATEGORIES,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Moodle Deadline Assistant MCP server")
    parser.add_argument("--stdio", action="store_true", help="talk over stdin/stdout instead of HTTP")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = parser.parse_args()

    try:
        settings = load_settings()
    except AssistantError as exc:
        log.print(f"[bold red]Configuration problem:[/] {exc}")
        raise SystemExit(1) from exc
    log.print(
        f"[bold]moodle-deadlines MCP server v{__version__}[/]  "
        f"Moodle: {describe_source(settings.moodle_ical_url)} | calendar: {settings.calendar_backend} | "
        f"tz: {settings.timezone}"
    )
    if args.stdio:
        mcp.run(transport="stdio", show_banner=False)
    else:
        log.print(f"Listening on http://{args.host}:{args.port}/mcp  (Ctrl+C to stop)")
        mcp.run(transport="http", host=args.host, port=args.port, path="/mcp", show_banner=False)


if __name__ == "__main__":
    main()
