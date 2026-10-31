"""A sample Moodle export, laid out exactly like Moodle's own (Bennu) output.

Dates are relative to `now`, so the sample always has upcoming deadlines.
Used by the tests and by scripts/make_sample_feed.py for offline rehearsal.
"""

from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

# (days from today, local time, duration minutes, course, summary)
SAMPLE_EVENTS: list[tuple[int, time, int, str | None, str]] = [
    (-3, time(23, 59), 0, "EECE503P", "C4 - Agent Personas and Thinking Mechanisms is due"),
    (2, time(23, 59), 0, "EECE503P", "C5 - Agent Tools is due"),
    (3, time(10, 0), 0, "EECE430", "Quiz 3 opens"),
    (3, time(12, 0), 0, "EECE430", "Quiz 3 closes"),
    (4, time(14, 0), 180, "EECE437", "Lab session: FPGA timing"),
    (5, time(17, 0), 0, "EECE437", "Lab 6 report is due"),
    (6, time(11, 0), 30, None, "Academic advising appointment"),
    (9, time(23, 59), 0, "MATH218", "Problem Set 4 is due"),
    (11, time(23, 0), 0, "EECE503P", "Final project proposal and team formation form for the agentic systems course is due"),
    (20, time(23, 59), 0, "CMPS201", "Reading response 5 should be completed"),
]


def build_sample_ics(now: datetime | None = None, host: str = "lms.aub.edu.lb", tz: str = "Asia/Beirut") -> str:
    zone = ZoneInfo(tz)
    today = (now or datetime.now(timezone.utc)).astimezone(zone).date()
    stamp = _utc(datetime.now(timezone.utc))
    lines = [
        "BEGIN:VCALENDAR",
        "METHOD:PUBLISH",
        "PRODID:-//Moodle Pty Ltd//NONSGML Moodle Version 2025041400//EN",
        "VERSION:2.0",
    ]
    for index, (days, clock, minutes, course, summary) in enumerate(SAMPLE_EVENTS, start=1):
        start = datetime.combine(today + timedelta(days=days), clock, tzinfo=zone)
        lines += [
            "BEGIN:VEVENT",
            f"UID:{48000 + index}@{host}",
            f"SUMMARY:{summary}",
            "DESCRIPTION:",
            "CLASS:PUBLIC",
            f"LAST-MODIFIED:{stamp}",
            f"DTSTAMP:{stamp}",
            f"DTSTART:{_utc(start)}",
            f"DTEND:{_utc(start + timedelta(minutes=minutes))}",
        ]
        if course:
            lines.append(f"CATEGORIES:{course}")
        lines.append("END:VEVENT")
    lines.append("END:VCALENDAR")
    return "\r\n".join(_fold(line) for line in lines) + "\r\n"


def _utc(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _fold(line: str, width: int = 75) -> str:
    """RFC 5545 line folding, as Moodle does for long titles."""
    if len(line) <= width:
        return line
    parts = [line[:width]]
    rest = line[width:]
    while rest:
        parts.append(" " + rest[: width - 1])
        rest = rest[width - 1 :]
    return "\r\n".join(parts)
