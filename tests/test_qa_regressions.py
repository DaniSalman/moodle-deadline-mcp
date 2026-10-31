"""Regression tests for the issues found in the QA review (numbers match the review)."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import httplib2
import httpx
import pytest
from fastmcp import Client
from googleapiclient.errors import HttpError

from conftest import NOW, FakeGoogleService
from deadline_mcp import moodle
from deadline_mcp.calendars import GoogleCalendar, LocalCalendar
from deadline_mcp.chat import connection_failure
from deadline_mcp.config import describe_source
from deadline_mcp.core import add_deadline_to_calendar, get_upcoming_deadlines, sync_deadlines_to_calendar
from deadline_mcp.errors import CalendarError, FeedError, InputError
from deadline_mcp.moodle import classify, parse_events, read_feed
from deadline_mcp.offline_model import _choose_tool
from deadline_mcp.sample_feed import build_sample_ics
from deadline_mcp.server import mcp

TOKEN = "SECRETTOKEN999"
WEBCAL = f"webcal://lms.aub.edu.lb/calendar/export_execute.php?userid=4242&authtoken={TOKEN}&preset_what=all"


# 1. The token must not leak, whatever the link's scheme -------------------------------------


def test_1_webcal_link_is_fetched_over_https_and_named_by_host(monkeypatch, feed_text):
    seen = []
    monkeypatch.setattr(moodle.httpx, "get", lambda url, **kw: seen.append(url) or httpx.Response(200, text=feed_text))
    assert describe_source(WEBCAL) == "lms.aub.edu.lb"
    read_feed(WEBCAL, 5)
    assert seen[0].startswith("https://lms.aub.edu.lb/calendar/export_execute.php")


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        (f"ftp://lms.aub.edu.lb/export?authtoken={TOKEN}", "not supported"),
        (f"export_execute.php?userid=1&authtoken={TOKEN}", "does not exist"),
        (f"https://lms.aub.edu.lb:abc/export?authtoken={TOKEN}", "not a valid web address"),
    ],
)
def test_1_bad_links_never_echo_the_token(source, expected):
    with pytest.raises(FeedError, match=expected) as caught:
        read_feed(source, 5)
    assert TOKEN not in str(caught.value)
    assert TOKEN not in describe_source(source)


async def test_1_token_stays_out_of_mcp_errors_and_the_resource(env, monkeypatch):
    monkeypatch.setenv("MOODLE_ICAL_URL", WEBCAL)
    monkeypatch.setattr(moodle.httpx, "get", lambda url, **kw: httpx.Response(200, text=f"Invalid authentication {url}"))
    async with Client(mcp) as client:
        result = await client.call_tool("get_upcoming_deadlines", {}, raise_on_error=False)
        resource = await client.read_resource("deadline-assistant://settings")
    assert result.is_error and "Invalid authentication" in result.content[0].text
    assert TOKEN not in result.content[0].text
    assert TOKEN not in resource[0].text


def test_1_file_url_is_read_as_a_file(tmp_path, feed_text):
    feed = tmp_path / "feed.ics"
    feed.write_text(feed_text, encoding="utf-8")
    assert read_feed(feed.as_uri(), 5).startswith("BEGIN:VCALENDAR")


# 2. A deadline Moodle moved is updated, not left stale ----------------------------------------


def _feed_with_c5_moved(days_later: int) -> str:
    original = build_sample_ics(NOW)
    moved = build_sample_ics(NOW + timedelta(days=days_later))
    c5 = [block for block in moved.split("BEGIN:VEVENT") if "C5 - Agent Tools is due" in block][0]
    old = [block for block in original.split("BEGIN:VEVENT") if "C5 - Agent Tools is due" in block][0]
    return original.replace(old, c5)


@pytest.mark.parametrize("backend", ["local", "google"])
def test_2_moved_deadline_updates_the_same_event(settings, env, feed_text, backend):
    calendar = LocalCalendar(env["calendar"]) if backend == "local" else GoogleCalendar(FakeGoogleService())
    sync_deadlines_to_calendar(settings, lambda: calendar, 14, 6, now=NOW, feed_text=feed_text)
    report = sync_deadlines_to_calendar(settings, lambda: calendar, 14, now=NOW, feed_text=_feed_with_c5_moved(2))
    assert [r.summary for r in report.updated] == ["[EECE503P] C5 - Agent Tools is due"]
    moved = report.updated[0]
    assert moved.due_at == "2026-11-04T23:59:00+02:00"
    assert "moved from Mon 02 Nov 2026 23:59 to Wed 04 Nov 2026 23:59" in moved.message
    # No reminder was asked for this time, so the 6-hour reminder chosen before is kept.
    assert moved.reminder_at == "2026-11-04T17:59:00+02:00"
    assert len(report.already_in_calendar) == 4 and "1 updated" in report.message
    if backend == "local":
        assert len(json.loads(env["calendar"].read_text())) == 5


# 3. Adding by hand and then syncing does not duplicate ----------------------------------------


@pytest.mark.parametrize("backend", ["local", "google"])
def test_3_manual_add_then_sync_is_one_event(settings, env, feed_text, backend):
    service = FakeGoogleService()
    calendar = LocalCalendar(env["calendar"]) if backend == "local" else GoogleCalendar(service)
    manual = add_deadline_to_calendar(settings, calendar, "C5 - Agent Tools", "2026-11-02T23:59", now=NOW)
    report = sync_deadlines_to_calendar(settings, lambda: calendar, 14, now=NOW, feed_text=feed_text)
    assert len(report.created) == 4
    assert [r.event_id for r in report.already_in_calendar] == [manual.event_id]
    # The hand-made event now carries the Moodle UID, so the next sync finds it directly.
    again = sync_deadlines_to_calendar(settings, lambda: calendar, 14, now=NOW, feed_text=feed_text)
    assert len(again.already_in_calendar) == 5 and not again.created and not again.updated
    if backend == "google":
        assert len(service.events_store) == 5


def test_3_sync_then_manual_add_is_one_event(settings, env, feed_text):
    calendar = LocalCalendar(env["calendar"])
    sync_deadlines_to_calendar(settings, lambda: calendar, 14, now=NOW, feed_text=feed_text)
    again = add_deadline_to_calendar(settings, calendar, "C5 - Agent Tools is due", "2026-11-02T23:59", now=NOW)
    assert again.status == "already_exists"
    assert len(json.loads(env["calendar"].read_text())) == 5


def test_3_two_moodle_deadlines_with_same_title_and_time_stay_separate(settings, env):
    calendar = LocalCalendar(env["calendar"])
    first = add_deadline_to_calendar(settings, calendar, "Quiz 1 closes", "2026-11-05T23:59", 24, "EECE430", "1@x", now=NOW)
    second = add_deadline_to_calendar(settings, calendar, "Quiz 1 closes", "2026-11-05T23:59", 24, "MATH218", "2@x", now=NOW)
    assert (first.status, second.status) == ("created", "created")
    assert first.event_id != second.event_id


# 4. Square brackets in text cannot break the logs ---------------------------------------------


async def test_4_brackets_in_arguments_are_just_text(env):
    async with Client(mcp) as client:
        result = await client.call_tool(
            "add_deadline_to_calendar",
            {"title": "notes [/x] due", "due_at": "2026-12-01T10:00", "course": "[/oops]"},
            raise_on_error=False,
        )
    assert result.is_error is False
    assert result.structured_content["summary"] == "[[/oops]] notes [/x] due"


async def test_4_brackets_in_a_moodle_error_page_keep_the_clear_message(env, monkeypatch):
    monkeypatch.setenv("MOODLE_ICAL_URL", "https://lms.aub.edu.lb/export?authtoken=x")
    page = "<html><body>[/script] Maintenance mode</body></html>"
    monkeypatch.setattr(moodle.httpx, "get", lambda url, **kw: httpx.Response(200, text=page))
    async with Client(mcp) as client:
        result = await client.call_tool("get_upcoming_deadlines", {}, raise_on_error=False)
    assert result.is_error and "did not return calendar data" in result.content[0].text


# 5. Input is checked before signing in to Google ----------------------------------------------


async def test_5_bad_input_is_reported_even_when_not_signed_in(env, monkeypatch):
    monkeypatch.setenv("CALENDAR_BACKEND", "google")
    async with Client(mcp) as client:
        past = await client.call_tool(
            "add_deadline_to_calendar", {"title": "Old essay is due", "due_at": "2020-01-01T10:00"}, raise_on_error=False
        )
        good = await client.call_tool(
            "add_deadline_to_calendar", {"title": "Essay is due", "due_at": "2026-12-01T10:00"}, raise_on_error=False
        )
    assert "already passed" in past.content[0].text
    assert "google_auth.py" in good.content[0].text


# 6-7. Dates and bad data -------------------------------------------------------------------------


def test_6_date_only_means_end_of_day(settings, env):
    result = add_deadline_to_calendar(settings, LocalCalendar(env["calendar"]), "Essay is due", "2026-11-05", now=NOW)
    assert result.due_at == "2026-11-05T23:59:00+02:00"


@pytest.mark.parametrize("due", ["9999-12-31T23:59", "2040-01-01T10:00"])
def test_7_absurd_dates_are_clear_errors(settings, env, due):
    with pytest.raises(InputError, match="years away|out of range"):
        add_deadline_to_calendar(settings, LocalCalendar(env["calendar"]), "Essay is due", due, now=NOW)


def test_7_one_bad_event_does_not_spoil_the_feed(settings, feed_text):
    broken = feed_text.replace("DTSTART:", "DTSTART:notadate-", 1)
    events, unreadable = parse_events(broken, ZoneInfo("Asia/Beirut"))
    assert unreadable == 1 and len(events) == 9
    result = get_upcoming_deadlines(settings, 14, now=NOW, feed_text=broken)
    assert result.unreadable_events == 1 and result.count == 5


def test_7_non_utf8_feed_file_is_still_read(tmp_path, feed_text):
    feed = tmp_path / "feed.ics"
    feed.write_bytes(feed_text.replace("Problem Set 4", "Probl\xe8me Set 4").encode("latin-1"))
    assert "BEGIN:VCALENDAR" in read_feed(str(feed), 5)


# 8. Asking for a different reminder changes it ----------------------------------------------------


def test_8_new_reminder_updates_the_event(settings, env):
    calendar = LocalCalendar(env["calendar"])
    add_deadline_to_calendar(settings, calendar, "Lab 6 report is due", "2026-11-05T17:00", 24, "EECE437", "48006@x", now=NOW)
    changed = add_deadline_to_calendar(settings, calendar, "Lab 6 report is due", "2026-11-05T17:00", 2, "EECE437", "48006@x", now=NOW)
    assert changed.status == "updated"
    assert "reminder changed from 24 hours to 2 hours before" in changed.message
    assert changed.reminder_at == "2026-11-05T15:00:00+02:00"
    same = add_deadline_to_calendar(settings, calendar, "Lab 6 report is due", "2026-11-05T17:00", None, "EECE437", "48006@x", now=NOW)
    assert same.status == "already_exists" and same.reminder_at == changed.reminder_at


# 9. Durations are right across a daylight-saving change ---------------------------------------


def test_9_dst_change_in_beirut(settings, env):
    # Lebanon moves clocks forward at 00:00 on Sunday 28 March 2027.
    now = datetime(2027, 3, 27, 10, 0, tzinfo=ZoneInfo("Asia/Beirut"))  # 08:00 UTC
    due_local = "2027-03-28T12:00:00+03:00"  # 09:00 UTC, 25 real hours later
    result = add_deadline_to_calendar(settings, LocalCalendar(env["calendar"]), "DST check is due", due_local, 24, now=now)
    # 24 real hours before 12:00 (+03) is 11:00 (+02) the day before, not 12:00.
    assert result.reminder_at == "2027-03-27T11:00:00+02:00"
    feed = (
        "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nBEGIN:VEVENT\r\nUID:9@x\r\nSUMMARY:DST check is due\r\n"
        "DTSTART:20270328T090000Z\r\nDTEND:20270328T090000Z\r\nCATEGORIES:EECE503P\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
    )
    found = get_upcoming_deadlines(settings, 2, now=now, feed_text=feed)
    assert found.deadlines[0].due_in_hours == 25.0
    assert found.deadlines[0].due_at == due_local


# 10. A server that stops mid-session gives a useful message ------------------------------------


def test_10_connection_failure_message_names_the_fix():
    text = connection_failure(None, ConnectionError("refused"))
    assert "python -m deadline_mcp.server" in text and "ConnectionError: refused" in text


# 11. Offline model phrasing ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "tool", "args"),
    [
        ("sync all my deadlines for the next two weeks to my calendar", "sync_deadlines_to_calendar", {"days_ahead": 14}),
        ("anything due in the next 100 days?", "get_upcoming_deadlines", {"days_ahead": 60}),
        ("sync everything, remind me 1 day before", "sync_deadlines_to_calendar", {"days_ahead": 14, "remind_hours_before": 24.0}),
        ("what's due for eece 503p this week", "get_upcoming_deadlines", {"days_ahead": 7, "course": "EECE503P"}),
    ],
)
def test_11_offline_model_reads_common_phrasings(text, tool, args):
    assert _choose_tool(text, []) == (tool, args)


# 12. Deadline titles -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("summary", "has_course", "expected"),
    [
        ("Library closes", False, None),
        ("Quiz 3 closes", True, ("Quiz 3", "closes")),
        ("Deadline: Project report", False, ("Project report", "deadline")),
        ("Homework 3 is due.", True, ("Homework 3", "due")),
        ("Lab 4 due date", True, ("Lab 4", "deadline")),
    ],
)
def test_12_title_edge_cases(summary, has_course, expected):
    assert classify(summary, has_course) == expected


# 14. Google's 400 gets a useful hint ---------------------------------------------------------------


def test_14_google_bad_request_hint(settings):
    bad = HttpError(httplib2.Response({"status": 400}), b'{"error": {"message": "Invalid value"}}')
    with pytest.raises(CalendarError, match="check the title and the date"):
        add_deadline_to_calendar(settings, GoogleCalendar(FakeGoogleService(fail_with=bad)), "Quiz closes", "2026-11-03T12:00", now=NOW)
