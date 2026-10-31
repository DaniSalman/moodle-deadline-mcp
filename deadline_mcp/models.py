"""Typed outputs of the three tools. FastMCP turns these into output schemas."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

DeadlineKind = Literal["due", "closes", "deadline", "completion"]


class Deadline(BaseModel):
    uid: str = Field(description="Moodle event ID, e.g. '123456@lms.aub.edu.lb'. Pass it as moodle_uid when adding to the calendar.")
    course: str | None = Field(description="Moodle course short name, e.g. 'EECE503P'.")
    title: str = Field(description="Event title exactly as Moodle shows it, e.g. 'C5 - Agent Tools is due'.")
    activity: str = Field(description="Title without Moodle's suffix, e.g. 'C5 - Agent Tools'.")
    kind: DeadlineKind = Field(description="Which Moodle wording marked this as a deadline.")
    due_at: str = Field(description="Due time, ISO 8601 in the configured time zone.")
    due_in_hours: float = Field(description="Hours from now until the deadline.")


class DeadlineList(BaseModel):
    source: str = Field(description="Where the deadlines came from (host name only, never the token).")
    timezone: str
    window_start: str
    window_end: str
    count: int
    deadlines: list[Deadline]
    ignored_events: int = Field(description="Events in the window that are not deadlines (e.g. 'Quiz opens', lectures).")


class CalendarAddResult(BaseModel):
    status: Literal["created", "already_exists"]
    event_id: str
    event_link: str | None = Field(description="Link to open the event in Google Calendar (None for the local backend).")
    calendar: str = Field(description="Which calendar was written, e.g. 'google:primary'.")
    summary: str
    due_at: str
    reminder_at: str = Field(description="When the reminder notification fires.")
    reminder_already_passed: bool = Field(description="True if the reminder time is already in the past, so no notification will fire.")
    message: str


class SyncFailure(BaseModel):
    title: str
    due_at: str
    error: str


class SyncReport(BaseModel):
    checked: int = Field(description="Deadlines found in Moodle for the window.")
    created: list[CalendarAddResult]
    already_in_calendar: list[CalendarAddResult]
    failed: list[SyncFailure]
    message: str
