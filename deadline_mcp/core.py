"""The three capabilities as plain functions, independent of MCP.

server.py exposes each one as an MCP tool. The orchestration function reuses
the data and action functions directly, so all three share one code path.

All time arithmetic is done in UTC and only converted to the student's time
zone for display, so durations stay right across daylight-saving changes.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from .calendars import CalendarBackend, NewEvent, StoredEvent
from .config import MAX_DAYS_AHEAD, MAX_REMIND_HOURS, Settings, describe_source
from .errors import AssistantError, InputError
from .models import CalendarAddResult, Deadline, DeadlineList, SyncFailure, SyncReport
from .moodle import classify, parse_events, read_feed, utc_now

UTC = timezone.utc
EVENT_LENGTH = timedelta(minutes=15)
MAX_YEARS_AHEAD = 5
_DATE_ONLY = re.compile(r"^\d{4}-\d{2}-\d{2}$")


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
    now_utc = (now or utc_now()).astimezone(UTC)
    end_utc = now_utc + timedelta(days=days_ahead)

    text = feed_text if feed_text is not None else read_feed(settings.moodle_ical_url, settings.http_timeout_seconds)
    wanted_course = _squash(course) if course and course.strip() else None
    events, unreadable = parse_events(text, tz)

    found: list[tuple[datetime, Deadline]] = []
    ignored = 0
    for event in events:
        due_utc = event.start.astimezone(UTC)
        if not now_utc <= due_utc <= end_utc:
            continue
        if wanted_course and wanted_course not in _squash(event.course or ""):
            continue
        match = classify(event.summary, has_course=event.course is not None)
        if match is None:
            ignored += 1
            continue
        activity, kind = match
        found.append(
            (
                due_utc,
                Deadline(
                    uid=event.uid,
                    course=event.course,
                    title=event.summary,
                    activity=activity,
                    kind=kind,
                    due_at=due_utc.astimezone(tz).isoformat(),
                    due_in_hours=round((due_utc - now_utc).total_seconds() / 3600, 1),
                ),
            )
        )

    found.sort(key=lambda pair: pair[0])
    return DeadlineList(
        source=describe_source(settings.moodle_ical_url) if feed_text is None else "provided text",
        timezone=settings.timezone,
        window_start=now_utc.astimezone(tz).isoformat(timespec="minutes"),
        window_end=end_utc.astimezone(tz).isoformat(timespec="minutes"),
        count=len(found),
        deadlines=[deadline for _, deadline in found],
        ignored_events=ignored,
        unreadable_events=unreadable,
    )


# --------------------------------------------------------------------------- Action


@dataclass(frozen=True)
class PreparedDeadline:
    """A validated request to put one deadline in the calendar."""

    title: str
    summary: str
    due_utc: datetime
    reminder_minutes: int | None  # None: default for a new event, keep the old one for an existing event
    dedup_key: str
    due_key: str
    title_key: str
    from_moodle: bool
    source_note: str


def prepare_deadline(
    settings: Settings,
    title: str,
    due_at: str,
    remind_hours_before: float | None = None,
    course: str | None = None,
    moodle_uid: str | None = None,
    *,
    now: datetime | None = None,
) -> PreparedDeadline:
    """Check every argument before anything is opened or written. Raises InputError."""
    title = " ".join((title or "").split())
    if not title:
        raise InputError("title must not be empty.")
    reminder_minutes = None
    if remind_hours_before is not None:
        hours = float(remind_hours_before)
        if not 0 <= hours <= MAX_REMIND_HOURS:
            raise InputError(f"remind_hours_before must be between 0 and {MAX_REMIND_HOURS} (Google's 4-week limit).")
        reminder_minutes = round(hours * 60)

    due_utc = parse_due(due_at, settings).astimezone(UTC)
    now_utc = (now or utc_now()).astimezone(UTC)
    if due_utc <= now_utc:
        raise InputError(
            f"That deadline ({_human(due_utc, settings)}) has already passed, so nothing was added to the calendar."
        )
    if due_utc > now_utc + timedelta(days=365 * MAX_YEARS_AHEAD):
        raise InputError(f"due_at is more than {MAX_YEARS_AHEAD} years away; check the date.")

    course = course.strip() if course and course.strip() else None
    uid = moodle_uid.strip() if moodle_uid and moodle_uid.strip() else None
    return PreparedDeadline(
        title=title,
        summary=f"[{course}] {title}" if course else title,
        due_utc=due_utc,
        reminder_minutes=reminder_minutes,
        dedup_key=uid or _manual_key(title, due_utc),
        due_key=due_utc.strftime("%Y%m%dT%H%M%SZ"),
        title_key=title_key(title),
        from_moodle=uid is not None,
        source_note=f"Moodle event {uid}" if uid else "added by request",
    )


def write_deadline(
    settings: Settings, calendar: CalendarBackend, deadline: PreparedDeadline, *, now: datetime | None = None
) -> CalendarAddResult:
    """Create the event, or bring an existing one in line with the deadline. Never duplicates."""
    tz = settings.tz
    now_utc = (now or utc_now()).astimezone(UTC)
    with calendar.lock:
        existing = calendar.find(deadline.dedup_key) or calendar.find_similar(
            deadline.due_key, deadline.title_key, manual_only=deadline.from_moodle
        )
        if existing is None:
            minutes = settings_minutes(settings) if deadline.reminder_minutes is None else deadline.reminder_minutes
            stored = calendar.create(_new_event(settings, deadline, minutes, deadline.dedup_key))
            return _result("created", stored, calendar, now_utc, tz, [])

        minutes = existing.reminder_minutes if deadline.reminder_minutes is None else deadline.reminder_minutes
        old_due = existing.start.astimezone(UTC)
        changes = []
        if old_due != deadline.due_utc:
            changes.append(f"moved from {_human(old_due, settings)} to {_human(deadline.due_utc, settings)}")
        if minutes != existing.reminder_minutes:
            changes.append(
                f"reminder changed from {_hours_text(existing.reminder_minutes)} to {_hours_text(minutes)} before"
            )
        # A deadline first added by hand gets its Moodle UID once it is synced; a UID is never replaced.
        key = deadline.dedup_key if deadline.from_moodle else existing.dedup_key
        if not changes and key == existing.dedup_key:
            return _result("already_exists", existing, calendar, now_utc, tz, [])
        stored = calendar.update(existing.event_id, _new_event(settings, deadline, minutes, key))
        return _result("updated" if changes else "already_exists", stored, calendar, now_utc, tz, changes)


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
    deadline = prepare_deadline(settings, title, due_at, remind_hours_before, course, moodle_uid, now=now)
    return write_deadline(settings, calendar, deadline, now=now)


def parse_due(due_at: str, settings: Settings) -> datetime:
    """ISO 8601 → aware datetime. No offset means the student's time zone; a date alone means 23:59."""
    text = (due_at or "").strip()
    if _DATE_ONLY.match(text):
        text += "T23:59"
    try:
        value = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise InputError(
            f"due_at '{due_at}' is not an ISO 8601 date-time. Use a form like '2026-11-05T23:59' "
            f"(read in {settings.timezone}) or '2026-11-05T23:59:00+02:00'."
        ) from exc
    if value.tzinfo is None:
        value = value.replace(tzinfo=settings.tz)
    try:
        return value.astimezone(settings.tz)
    except OverflowError as exc:
        raise InputError(f"due_at '{due_at}' is out of range; check the date.") from exc


def settings_minutes(settings: Settings) -> int:
    return round(settings.default_remind_hours * 60)


def title_key(title: str) -> str:
    """'C5 - Agent Tools is due' and 'C5 - Agent Tools' both give 'c5agenttools'."""
    found = classify(title)
    base = found[0] if found else title
    return re.sub(r"[^0-9a-z]+", "", base.lower())[:200] or "untitled"


def _new_event(settings: Settings, deadline: PreparedDeadline, minutes: int, dedup_key: str) -> NewEvent:
    tz = settings.tz
    return NewEvent(
        summary=deadline.summary,
        description=(
            f"Deadline: {_human(deadline.due_utc, settings)}.\n"
            f"Source: {deadline.source_note}.\n"
            "Added by Moodle Deadline Assistant (MCP)."
        ),
        start=deadline.due_utc.astimezone(tz),
        end=(deadline.due_utc + EVENT_LENGTH).astimezone(tz),
        timezone=settings.timezone,
        reminder_minutes=minutes,
        dedup_key=dedup_key,
        due_key=deadline.due_key,
        title_key=deadline.title_key,
        origin="manual" if dedup_key.startswith("manual-") else "moodle",
    )


def _result(
    status: str, event: StoredEvent, calendar: CalendarBackend, now_utc: datetime, tz, changes: list[str]
) -> CalendarAddResult:
    due_utc = event.start.astimezone(UTC)
    reminder_utc = due_utc - timedelta(minutes=event.reminder_minutes)
    passed = reminder_utc <= now_utc
    due_local, reminder_local = due_utc.astimezone(tz), reminder_utc.astimezone(tz)
    if status == "created":
        message = (
            f"Added '{event.summary}' to {calendar.label} on {_fmt(due_local)} with a reminder "
            f"{_hours_text(event.reminder_minutes)} before ({_fmt(reminder_local)})."
        )
    elif status == "updated":
        message = f"Updated '{event.summary}' in {calendar.label}: {'; '.join(changes)}. Reminder at {_fmt(reminder_local)}."
    else:
        message = f"'{event.summary}' is already in {calendar.label} with the same time and reminder; nothing was changed."
    if passed:
        message += " Its reminder time has already passed, so no notification will fire for it."
    return CalendarAddResult(
        status=status,
        event_id=event.event_id,
        event_link=event.link,
        calendar=calendar.label,
        summary=event.summary,
        due_at=due_local.isoformat(),
        reminder_at=reminder_local.isoformat(),
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
            updated=[],
            already_in_calendar=[],
            failed=[],
            message=f"No deadlines in the next {days_ahead} days{scope}; nothing to add.",
        )

    # Step 2 (Action), once per deadline. One bad item does not stop the rest.
    calendar = open_calendar()
    buckets: dict[str, list[CalendarAddResult]] = {"created": [], "updated": [], "already_exists": []}
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
        buckets[result.status].append(result)

    message = (
        f"Checked {found.count} deadline(s) in the next {days_ahead} days{scope}: "
        f"{len(buckets['created'])} added, {len(buckets['updated'])} updated, "
        f"{len(buckets['already_exists'])} already in the calendar, {len(failed)} failed."
    )
    return SyncReport(
        checked=found.count,
        created=buckets["created"],
        updated=buckets["updated"],
        already_in_calendar=buckets["already_exists"],
        failed=failed,
        message=message,
    )


# --------------------------------------------------------------------------- helpers


def _squash(text: str) -> str:
    return "".join(text.lower().split())


def _manual_key(title: str, due_utc: datetime) -> str:
    digest = hashlib.sha1(f"{title.lower()}|{due_utc.isoformat()}".encode()).hexdigest()[:20]
    return f"manual-{digest}"


def _human(moment: datetime, settings: Settings) -> str:
    return _fmt(moment.astimezone(settings.tz))


def _fmt(moment: datetime) -> str:
    return moment.strftime("%a %d %b %Y %H:%M")


def _hours_text(minutes: int) -> str:
    hours = minutes / 60
    return f"{hours:g} hour{'s' if hours != 1 else ''}"
