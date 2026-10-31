"""Data tool: get_upcoming_deadlines (requirements D1-D8)."""

from __future__ import annotations

from dataclasses import replace
from zoneinfo import ZoneInfo

import httpx
import pytest

from conftest import NOW, SECRET_URL
from deadline_mcp import moodle
from deadline_mcp.core import get_upcoming_deadlines
from deadline_mcp.errors import FeedError, InputError
from deadline_mcp.moodle import classify, parse_events, read_feed

BEIRUT = ZoneInfo("Asia/Beirut")


def test_d1_parses_moodle_export(feed_text):
    events = parse_events(feed_text, BEIRUT)
    assert len(events) == 10
    c5 = next(e for e in events if e.summary == "C5 - Agent Tools is due")
    assert c5.uid.endswith("@lms.aub.edu.lb")
    assert c5.course == "EECE503P"
    assert c5.start.astimezone(BEIRUT).strftime("%Y-%m-%d %H:%M") == "2026-11-02 23:59"
    # A title longer than 75 characters is folded over several lines in the file.
    assert any(e.summary.startswith("Final project proposal") and e.summary.endswith("is due") for e in events)
    # Events without a course have no CATEGORIES line.
    assert any(e.course is None for e in events)


@pytest.mark.parametrize(
    ("summary", "expected"),
    [
        ("C5 - Agent Tools is due", ("C5 - Agent Tools", "due")),
        ("Quiz 3 closes", ("Quiz 3", "closes")),
        ("Reading response 5 should be completed", ("Reading response 5", "completion")),
        ("Peer review (submissions deadline)", ("Peer review", "deadline")),
        ("Lab 7 report due", ("Lab 7 report", "deadline")),
        ("Quiz 3 opens", None),
        ("Assignment 2 is due to be graded", None),
        ("Lab session: FPGA timing", None),
        ("Course overdue fees reminder", None),
    ],
)
def test_d1_recognises_deadline_titles(summary, expected):
    assert classify(summary) == expected


def test_d2_only_deadlines_inside_the_window(settings, feed_text):
    week = get_upcoming_deadlines(settings, 7, now=NOW, feed_text=feed_text)
    titles = [d.title for d in week.deadlines]
    assert titles == ["C5 - Agent Tools is due", "Quiz 3 closes", "Lab 6 report is due"]
    # Quiz opens, the lab session and the advising appointment are skipped, not listed.
    assert week.ignored_events == 3
    # The C4 deadline three days ago and the reading response in 20 days are outside.
    assert "C4 - Agent Personas and Thinking Mechanisms is due" not in titles
    month = get_upcoming_deadlines(settings, 30, now=NOW, feed_text=feed_text)
    assert month.count == 6
    assert [d.due_in_hours for d in month.deadlines] == sorted(d.due_in_hours for d in month.deadlines)


def test_d2_times_are_in_the_student_time_zone(settings, feed_text):
    result = get_upcoming_deadlines(settings, 7, now=NOW, feed_text=feed_text)
    assert result.timezone == "Asia/Beirut"
    assert result.deadlines[0].due_at == "2026-11-02T23:59:00+02:00"
    # Sat 10:00 -> Mon 23:59 is 61 h 59 min.
    assert result.deadlines[0].due_in_hours == 62.0


def test_d3_course_filter_ignores_case_and_spaces(settings, feed_text):
    result = get_upcoming_deadlines(settings, 14, "eece 503p", now=NOW, feed_text=feed_text)
    assert [d.course for d in result.deadlines] == ["EECE503P", "EECE503P"]


def test_d4_missing_feed_url_is_a_clear_error(settings):
    with pytest.raises(FeedError, match="MOODLE_ICAL_URL"):
        get_upcoming_deadlines(replace(settings, moodle_ical_url=""), 7)


def _fake_get(response=None, raises=None):
    def fake(url, **kwargs):
        if raises:
            raise raises
        return response

    return fake


@pytest.mark.parametrize(
    ("fake", "expected"),
    [
        (_fake_get(raises=httpx.ConnectError("getaddrinfo failed")), "Could not reach Moodle at lms.aub.edu.lb"),
        (_fake_get(raises=httpx.ReadTimeout("timed out")), "did not answer within"),
        (_fake_get(response=httpx.Response(503, text="Service Unavailable")), "HTTP 503"),
    ],
)
def test_d5_unreachable_moodle_is_a_clear_error(monkeypatch, fake, expected):
    monkeypatch.setattr(moodle.httpx, "get", fake)
    with pytest.raises(FeedError, match=expected) as caught:
        read_feed(SECRET_URL, 5)
    assert "SECRET0TOKEN0123" not in str(caught.value)


def test_d6_expired_token_is_a_clear_error(monkeypatch):
    monkeypatch.setattr(moodle.httpx, "get", _fake_get(response=httpx.Response(200, text="Invalid authentication")))
    with pytest.raises(FeedError) as caught:
        read_feed(SECRET_URL, 5)
    message = str(caught.value)
    assert "Invalid authentication" in message
    assert "expired" in message and "Export calendar" in message


def test_d8_token_never_leaks_even_if_moodle_echoes_the_url(monkeypatch):
    page = f"<html><body>Bad link: {SECRET_URL} or authtoken=SECRET0TOKEN0123</body></html>"
    monkeypatch.setattr(moodle.httpx, "get", _fake_get(response=httpx.Response(200, text=page)))
    with pytest.raises(FeedError) as caught:
        read_feed(SECRET_URL, 5)
    assert "SECRET0TOKEN0123" not in str(caught.value)


def test_d8_source_shows_host_only(settings, monkeypatch, feed_text):
    monkeypatch.setattr(moodle.httpx, "get", _fake_get(response=httpx.Response(200, text=feed_text)))
    result = get_upcoming_deadlines(replace(settings, moodle_ical_url=SECRET_URL), 7, now=NOW)
    assert result.source == "lms.aub.edu.lb"
    assert "SECRET0TOKEN0123" not in result.model_dump_json()


def test_d7_days_ahead_out_of_range(settings, feed_text):
    for bad in (0, 61):
        with pytest.raises(InputError, match="between 1 and 60"):
            get_upcoming_deadlines(settings, bad, now=NOW, feed_text=feed_text)


def test_html_entities_and_date_only_events():
    text = (
        "BEGIN:VCALENDAR\r\nVERSION:2.0\r\n"
        "BEGIN:VEVENT\r\nUID:1@x\r\nSUMMARY:Essay &amp; outline is due\r\nDTSTART;VALUE=DATE:20261105\r\n"
        "CATEGORIES:ENGL&amp;206\r\nEND:VEVENT\r\n"
        "BEGIN:VEVENT\r\nUID:2@x\r\nSUMMARY:Floating time is due\r\nDTSTART:20261106T120000\r\nEND:VEVENT\r\n"
        "END:VCALENDAR\r\n"
    )
    first, second = parse_events(text, BEIRUT)
    assert first.summary == "Essay & outline is due"
    assert first.course == "ENGL&206"
    assert first.start.strftime("%H:%M") == "23:59" and first.start.tzinfo is not None
    assert second.start.utcoffset().total_seconds() == 2 * 3600


def test_local_file_missing_is_a_clear_error(tmp_path):
    with pytest.raises(FeedError, match="does not exist"):
        read_feed(str(tmp_path / "nope.ics"), 5)
