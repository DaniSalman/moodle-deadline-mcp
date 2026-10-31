"""Read the student's Moodle calendar export and recognise deadline events.

Moodle builds the export in calendar/export_execute.php: one VEVENT per event,
UID "<event id>@<host>", SUMMARY = event name ("C5 - Agent Tools is due"),
DTSTART = DTEND for deadlines, and CATEGORIES = the course short name.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass
from datetime import date, datetime, time, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
from icalendar import Calendar

from .config import PROJECT_ROOT, describe_source
from .errors import FeedError

EXPORT_HELP = (
    "In Moodle open Calendar → Import or export calendars → Export calendar, "
    "choose 'All events' and 'Recent and next 60 days', press 'Get calendar URL', "
    "and put that URL in .env as MOODLE_ICAL_URL."
)

# Endings Moodle gives to closing-date events, most specific first.
# "is due to be graded" (teachers only) and "opens" do not match on purpose.
_DEADLINE_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"^(?P<activity>.+?)\s+is due$", re.IGNORECASE), "due"),
    (re.compile(r"^(?P<activity>.+?)\s+closes$", re.IGNORECASE), "closes"),
    (re.compile(r"^(?P<activity>.+?)\s+should be completed$", re.IGNORECASE), "completion"),
    (re.compile(r"^(?P<activity>.+?)\s*\([^)]*deadline\)$", re.IGNORECASE), "deadline"),
    (re.compile(r"^(?P<activity>.+?)\s*[-:]?\s+(?:deadline|due)$", re.IGNORECASE), "deadline"),
]


@dataclass(frozen=True)
class MoodleEvent:
    uid: str
    summary: str
    course: str | None
    start: datetime  # timezone-aware


def classify(summary: str) -> tuple[str, str] | None:
    """Return (activity, kind) if the event title marks a deadline, else None."""
    text = summary.strip()
    for pattern, kind in _DEADLINE_PATTERNS:
        match = pattern.match(text)
        if match:
            return match.group("activity").strip(), kind
    return None


def read_feed(source: str, timeout_seconds: float) -> str:
    """Download (or read from disk) the iCalendar text. Raises FeedError."""
    if not source:
        raise FeedError(f"The Moodle feed is not configured. {EXPORT_HELP}")

    where = describe_source(source)
    if source.lower().startswith(("http://", "https://")):
        try:
            response = httpx.get(
                source,
                timeout=timeout_seconds,
                follow_redirects=True,
                headers={"User-Agent": "moodle-deadline-mcp/1.0"},
            )
        except httpx.TimeoutException as exc:
            raise FeedError(
                f"Moodle at {where} did not answer within {timeout_seconds:g} seconds. "
                "It may be down or slow; try again in a minute."
            ) from exc
        except httpx.HTTPError as exc:
            raise FeedError(
                f"Could not reach Moodle at {where} ({type(exc).__name__}). "
                "Check the internet connection and try again."
            ) from exc
        if response.status_code != 200:
            raise FeedError(
                f"Moodle at {where} answered with HTTP {response.status_code} instead of a calendar. "
                "The site may be down, or the export link may be wrong."
            )
        text = response.text
    else:
        path = Path(source)
        if not path.is_absolute():
            path = PROJECT_ROOT / path
        if not path.is_file():
            raise FeedError(f"The Moodle feed file '{path.name}' does not exist.")
        text = path.read_text(encoding="utf-8")

    text = text.lstrip("﻿")
    if not text.lstrip().upper().startswith("BEGIN:VCALENDAR"):
        raise FeedError(
            f"Moodle did not return calendar data (it said: \"{_snippet(text, source)}\"). "
            f"The export link has probably expired or was copied incompletely. {EXPORT_HELP}"
        )
    return text


def parse_events(text: str, tz: ZoneInfo) -> list[MoodleEvent]:
    """Parse every VEVENT. Times without a zone are read in `tz`."""
    try:
        calendar = Calendar.from_ical(text)
    except ValueError as exc:
        raise FeedError(f"The Moodle feed is not valid iCalendar data ({exc}).") from exc

    events: list[MoodleEvent] = []
    for component in calendar.walk("VEVENT"):
        if component.get("dtstart") is None:
            continue
        summary = html.unescape(str(component.get("summary", ""))).strip()
        events.append(
            MoodleEvent(
                uid=str(component.get("uid", "")).strip(),
                summary=summary,
                course=_course(component),
                start=_aware(component.decoded("dtstart"), tz),
            )
        )
    return events


def _course(component) -> str | None:
    raw = component.get("categories")
    if raw is None:
        return None
    names: list[str] = []
    for item in raw if isinstance(raw, list) else [raw]:
        names.extend(str(name) for name in getattr(item, "cats", [item]))
    names = [html.unescape(name).strip() for name in names if str(name).strip()]
    return names[0] if names else None


def _aware(value: datetime | date, tz: ZoneInfo) -> datetime:
    if isinstance(value, datetime):
        return value.replace(tzinfo=tz) if value.tzinfo is None else value
    # A date without a time: treat it as due at the end of that day.
    return datetime.combine(value, time(23, 59), tzinfo=tz)


def _snippet(text: str, source: str) -> str:
    plain = re.sub(r"<[^>]+>", " ", text)
    plain = " ".join(plain.split())[:100] or "an empty response"
    if source:
        plain = plain.replace(source, "[export URL]")
    return re.sub(r"(authtoken=)[^&\s\"']+", r"\1[hidden]", plain)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)
