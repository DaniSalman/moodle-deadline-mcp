"""Action tool: add_deadline_to_calendar (requirements A1-A7)."""

from __future__ import annotations

import json
from datetime import timedelta

import httplib2
import pytest
from googleapiclient.errors import HttpError

from conftest import NOW, FakeGoogleService
from deadline_mcp.calendars import GoogleCalendar, LocalCalendar
from deadline_mcp.config import load_settings
from deadline_mcp.core import add_deadline_to_calendar
from deadline_mcp.errors import CalendarError, InputError

DUE = "2026-11-02T23:59:00+02:00"


def test_a1_creates_event_at_deadline_with_reminder(settings, env):
    calendar = LocalCalendar(env["calendar"])
    result = add_deadline_to_calendar(
        settings, calendar, "C5 - Agent Tools is due", DUE, 6, "EECE503P", "48002@lms.aub.edu.lb", now=NOW
    )
    assert result.status == "created"
    assert result.summary == "[EECE503P] C5 - Agent Tools is due"
    assert result.reminder_at == "2026-11-02T17:59:00+02:00"
    assert result.reminder_already_passed is False
    stored = json.loads(env["calendar"].read_text())
    assert len(stored) == 1
    assert stored[0]["start"] == DUE
    assert stored[0]["end"] == "2026-11-03T00:14:00+02:00"
    assert stored[0]["reminder_minutes"] == 360
    assert stored[0]["dedup_key"] == "48002@lms.aub.edu.lb"


def test_a1_google_request_body(settings):
    service = FakeGoogleService()
    result = add_deadline_to_calendar(
        settings, GoogleCalendar(service), "Quiz 3 closes", "2026-11-03T12:00", 24, "EECE430", "48004@lms.aub.edu.lb", now=NOW
    )
    body = service.inserted_bodies[0]
    assert body["start"] == {"dateTime": "2026-11-03T12:00:00+02:00", "timeZone": "Asia/Beirut"}
    assert body["end"]["dateTime"] == "2026-11-03T12:15:00+02:00"
    assert body["reminders"] == {"useDefault": False, "overrides": [{"method": "popup", "minutes": 1440}]}
    assert body["extendedProperties"]["private"]["dedupKey"] == "48004@lms.aub.edu.lb"
    assert result.calendar == "google:primary"
    assert result.event_link.startswith("https://calendar.google.com/")


def test_naive_due_time_is_read_in_the_student_time_zone(settings, env):
    result = add_deadline_to_calendar(settings, LocalCalendar(env["calendar"]), "Essay is due", "2026-11-05T23:59", now=NOW)
    assert result.due_at == "2026-11-05T23:59:00+02:00"
    # Default reminder from .env (24 h).
    assert result.reminder_at == "2026-11-04T23:59:00+02:00"


@pytest.mark.parametrize("backend", ["local", "google"])
def test_a2_same_deadline_twice_creates_one_event(settings, env, backend):
    calendar = LocalCalendar(env["calendar"]) if backend == "local" else GoogleCalendar(FakeGoogleService())
    first = add_deadline_to_calendar(settings, calendar, "Lab 6 report is due", "2026-11-05T17:00", 24, "EECE437", "48006@x", now=NOW)
    second = add_deadline_to_calendar(settings, calendar, "Lab 6 report is due", "2026-11-05T17:00", 24, "EECE437", "48006@x", now=NOW)
    assert (first.status, second.status) == ("created", "already_exists")
    assert first.event_id == second.event_id
    assert "nothing was changed" in second.message


def test_a2_manual_entries_are_deduplicated_too(settings, env):
    calendar = LocalCalendar(env["calendar"])
    add_deadline_to_calendar(settings, calendar, "Essay is due", "2026-11-05T23:59", now=NOW)
    again = add_deadline_to_calendar(settings, calendar, "essay is due", "2026-11-05T21:59:00Z", now=NOW)
    assert again.status == "already_exists"
    assert len(json.loads(env["calendar"].read_text())) == 1


@pytest.mark.parametrize("bad", ["next friday", "05/11/2026", ""])
def test_a3_unparseable_due_time_is_a_clear_error(settings, env, bad):
    with pytest.raises(InputError, match="ISO 8601"):
        add_deadline_to_calendar(settings, LocalCalendar(env["calendar"]), "Essay is due", bad, now=NOW)


def test_a4_past_deadline_is_refused_and_nothing_is_written(settings, env):
    with pytest.raises(InputError, match="already passed"):
        add_deadline_to_calendar(settings, LocalCalendar(env["calendar"]), "C4 is due", "2026-10-28T23:59", now=NOW)
    assert not env["calendar"].exists()


def test_a5_not_signed_in_to_google(env):
    with pytest.raises(CalendarError, match="google_auth.py"):
        GoogleCalendar.from_settings(load_settings())


def test_a5_google_refusal_is_a_clear_error(settings):
    refused = HttpError(httplib2.Response({"status": 403}), b'{"error": {"message": "Insufficient Permission"}}')
    calendar = GoogleCalendar(FakeGoogleService(fail_with=refused))
    with pytest.raises(CalendarError) as caught:
        add_deadline_to_calendar(settings, calendar, "Quiz 3 closes", "2026-11-03T12:00", now=NOW)
    assert "HTTP 403" in str(caught.value) and "google_auth.py" in str(caught.value)


def test_a5_google_unreachable_is_a_clear_error(settings):
    calendar = GoogleCalendar(FakeGoogleService(fail_with=httplib2.ServerNotFoundError("no dns")))
    with pytest.raises(CalendarError, match="Could not reach Google Calendar"):
        add_deadline_to_calendar(settings, calendar, "Quiz 3 closes", "2026-11-03T12:00", now=NOW)


def test_a6_reminder_in_the_past_is_flagged(settings, env):
    due = (NOW + timedelta(hours=2)).isoformat()
    result = add_deadline_to_calendar(settings, LocalCalendar(env["calendar"]), "Quiz closes", due, 24, now=NOW)
    assert result.status == "created"
    assert result.reminder_already_passed is True
    assert "no notification will fire" in result.message


@pytest.mark.parametrize("hours", [-1, 673])
def test_a7_reminder_hours_out_of_range(settings, env, hours):
    with pytest.raises(InputError, match="between 0 and 672"):
        add_deadline_to_calendar(settings, LocalCalendar(env["calendar"]), "Essay is due", DUE, hours, now=NOW)


def test_empty_title_is_refused(settings, env):
    with pytest.raises(InputError, match="title"):
        add_deadline_to_calendar(settings, LocalCalendar(env["calendar"]), "   ", DUE, now=NOW)
