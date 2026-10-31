"""The three capabilities as plain functions, independent of MCP.

server.py exposes each one as an MCP tool. The orchestration function reuses
the data and action functions directly, so all three share one code path.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from datetime import datetime, timedelta

from .calendars import CalendarBackend, NewEvent, StoredEvent
from .config import MAX_DAYS_AHEAD, MAX_REMIND_HOURS, Settings, describe_source
from .errors import AssistantError, InputError
from .models import CalendarAddResult, Deadline, DeadlineList, SyncFailure, SyncReport
from .moodle import classify, parse_events, read_feed, utc_now

EVENT_LENGTH = timedelta(minutes=15)


# --------------------------------------------------------------------------- Data


def get_upcoming_deadlines(
    settings: Settings,
    days_ahead: int = 14,
    course: str | None = None,
    *,
    now: datetime | None = None,
    feed_text: str | None = None,
) -> DeadlineList:
    """Deadlines due between now and now + days_ahead, soonest first."""
    if not 1 <= days_ahead <= MAX_DAYS_AHEAD:
        raise InputError(f"days_ahead must be between 1 and {MAX_DAYS_AHEAD} (Moodle exports 60 days ahead).")
    tz = settings.tz
    now = (now or utc_now()).astimezone(tz)
    window_end = now + timedelta(days=days_ahead)

    text = feed_text if feed_text is not None else read_feed(settings.moodle_ical_url, settings.http_timeout_seconds)
    wanted_course = _squash(course) if course and course.strip() else None

    deadlines: list[Deadline] = []
    ignored = 0
    for event in parse_events(text, tz):
        if not now <= event.start <= window_end:
            continue
        if wanted_course and wanted_course not in _squash(event.course or ""):
            continue
        found = classify(event.summary)
        if found is None:
            ignored += 1
            continue
        activity, kind = found
        due = event.start.astimezone(tz)
        deadlines.append(
            Deadline(
                uid=event.uid,
                course=event.course,
                title=event.summary,
                activity=activity,
                kind=kind,
                due_at=due.isoformat(),
                due_in_hours=round((due - now).total_seconds() / 3600, 1),
            )
        )

    deadlines.sort(key=lambda d: datetime.fromisoformat(d.due_at))
    return DeadlineList(
        source=describe_source(settings.moodle_ical_url) if feed_text is None else "provided text",
        timezone=settings.timezone,
        window_start=now.isoformat(timespec="minutes"),
        window_end=window_end.isoformat(timespec="minutes"),
        count=len(deadlines),
        deadlines=deadlines,
        ignored_events=ignored,
    )


# --------------------------------------------------------------------------- Action


def add_deadline_to_calendar(
    settings: Settings,
    calendar: CalendarBackend,
    title: str,
    due_at: str,
    remind_hours_before: float | None = None,
    course: str | None = None,
    moodle_uid: str | None = None,
    *,
    now: datetime | None = None,
) -> CalendarAddResult:
    """Create one calendar event at the deadline with a reminder; never duplicates."""
    title = (title or "").strip()
    if not title:
        raise InputError("title must not be empty.")
    hours = settings.default_remind_hours if remind_hours_before is None else float(remind_hours_before)
    if not 0 <= hours <= MAX_REMIND_HOURS:
        raise InputError(f"remind_hours_before must be between 0 and {MAX_REMIND_HOURS} (Google's 4-week limit).")

    tz = settings.tz
    due = parse_due(due_at, settings)
    now = (now or utc_now()).astimezone(tz)
    if due <= now:
        raise InputError(
            f"That deadline ({_human(due)}) has already passed, so nothing was added to the calendar."
        )

    course = course.strip() if course and course.strip() else None
    summary = f"[{course}] {title}" if course else title
    dedup_key = moodle_uid.strip() if moodle_uid and moodle_uid.strip() else _manual_key(title, due)
    reminder_minutes = round(hours * 60)

    existing = calendar.find(dedup_key)
    if existing is not None:
        return _result("already_exists", existing, calendar, now, tz)

    stored = calendar.create(
        NewEvent(
            summary=summary,
            description=(
                f"Deadline: {_human(due)}.\n"
                f"Source: {'Moodle event ' + moodle_uid.strip() if moodle_uid and moodle_uid.strip() else 'added by request'}.\n"
                "Added by Moodle Deadline Assistant (MCP)."
            ),
            start=due,
            end=due + EVENT_LENGTH,
            timezone=settings.timezone,
            reminder_minutes=reminder_minutes,
            dedup_key=dedup_key,
        )
    )
    return _result("created", stored, calendar, now, tz)


def parse_due(due_at: str, settings: Settings) -> datetime:
    """ISO 8601 → aware datetime. No offset means the configured time zone."""
    text = (due_at or "").strip().replace("Z", "+00:00")
    try:
        value = datetime.fromisoformat(text)
    except ValueError as exc:
        raise InputError(
            f"due_at '{due_at}' is not an ISO 8601 date-time. Use a form like '2026-11-05T23:59' "
            f"(read in {settings.timezone}) or '2026-11-05T23:59:00+02:00'."
        ) from exc
    if value.tzinfo is None:
        value = value.replace(tzinfo=settings.tz)
    return value.astimezone(settings.tz)


def _result(status: str, event: StoredEvent, calendar: CalendarBackend, now: datetime, tz) -> CalendarAddResult:
    due = event.start.astimezone(tz)
    reminder_at = due - timedelta(minutes=event.reminder_minutes)
    passed = reminder_at <= now
    if status == "created":
        message = (
            f"Added '{event.summary}' to {calendar.label} on {_human(due)} with a reminder "
            f"{_hours_text(event.reminder_minutes)} before ({_human(reminder_at)})."
        )
    else:
        message = f"'{event.summary}' is already in {calendar.label}; nothing new was added."
    if passed:
        message += " Its reminder time has already passed, so no notification will fire for it."
    return CalendarAddResult(
        status=status,
        event_id=event.event_id,
        event_link=event.link,
        calendar=calendar.label,
        summary=event.summary,
        due_at=due.isoformat(),
        reminder_at=reminder_at.isoformat(),
        reminder_already_passed=passed,
        message=message,
    )


# --------------------------------------------------------------------------- Orchestration


def sync_deadlines_to_calendar(
    settings: Settings,
    open_calendar: Callable[[], CalendarBackend],
    days_ahead: int = 14,
    remind_hours_before: float | None = None,
    course: str | None = None,
    *,
    now: datetime | None = None,
    feed_text: str | None = None,
) -> SyncReport:
    """Data step once, then the Action step for each deadline. Partial failures are reported."""
    now = now or utc_now()
    # Step 1 (Data). If this fails the whole sync fails and nothing is written.
    found = get_upcoming_deadlines(settings, days_ahead, course, now=now, feed_text=feed_text)
    scope = f" for {course}" if course else ""
    if found.count == 0:
        return SyncReport(
            checked=0,
            created=[],
            already_in_calendar=[],
            failed=[],
            message=f"No deadlines in the next {days_ahead} days{scope}; nothing to add.",
        )

    # Step 2 (Action), once per deadline. One bad item does not stop the rest.
    calendar = open_calendar()
    created: list[CalendarAddResult] = []
    existing: list[CalendarAddResult] = []
    failed: list[SyncFailure] = []
    for deadline in found.deadlines:
        try:
            result = add_deadline_to_calendar(
                settings,
                calendar,
                title=deadline.title,
                due_at=deadline.due_at,
                remind_hours_before=remind_hours_before,
                course=deadline.course,
                moodle_uid=deadline.uid,
                now=now,
            )
        except AssistantError as exc:
            failed.append(SyncFailure(title=deadline.title, due_at=deadline.due_at, error=str(exc)))
            continue
        (created if result.status == "created" else existing).append(result)

    message = (
        f"Checked {found.count} deadline(s) in the next {days_ahead} days{scope}: "
        f"{len(created)} added, {len(existing)} already in the calendar, {len(failed)} failed."
    )
    return SyncReport(
        checked=found.count,
        created=created,
        already_in_calendar=existing,
        failed=failed,
        message=message,
    )


# --------------------------------------------------------------------------- helpers


def _squash(text: str) -> str:
    return "".join(text.lower().split())


def _manual_key(title: str, due: datetime) -> str:
    digest = hashlib.sha1(f"{title.lower()}|{due.isoformat()}".encode()).hexdigest()[:20]
    return f"manual-{digest}"


def _human(moment: datetime) -> str:
    return moment.strftime("%a %d %b %Y %H:%M")


def _hours_text(minutes: int) -> str:
    hours = minutes / 60
    return f"{hours:g} hour{'s' if hours != 1 else ''}"
