"""Orchestration tool: sync_deadlines_to_calendar (requirements O1-O4)."""

from __future__ import annotations

import json
from dataclasses import replace

import pytest

from conftest import NOW
from deadline_mcp.calendars import LocalCalendar
from deadline_mcp.core import sync_deadlines_to_calendar
from deadline_mcp.errors import CalendarError, FeedError


def test_o1_adds_every_deadline_in_the_window(settings, env, feed_text):
    report = sync_deadlines_to_calendar(
        settings, lambda: LocalCalendar(env["calendar"]), 14, 6, now=NOW, feed_text=feed_text
    )
    assert report.checked == 5
    assert len(report.created) == 5 and not report.failed and not report.already_in_calendar
    assert "5 added" in report.message
    stored = json.loads(env["calendar"].read_text())
    assert {row["reminder_minutes"] for row in stored} == {360}


def test_o2_running_twice_adds_nothing_new(settings, env, feed_text):
    open_calendar = lambda: LocalCalendar(env["calendar"])  # noqa: E731
    sync_deadlines_to_calendar(settings, open_calendar, 14, now=NOW, feed_text=feed_text)
    second = sync_deadlines_to_calendar(settings, open_calendar, 14, now=NOW, feed_text=feed_text)
    assert len(second.created) == 0
    assert len(second.already_in_calendar) == 5
    assert len(json.loads(env["calendar"].read_text())) == 5


def test_o3_data_failure_stops_everything_before_any_write(settings):
    opened = []
    with pytest.raises(FeedError):
        sync_deadlines_to_calendar(replace(settings, moodle_ical_url=""), lambda: opened.append(1), 14, now=NOW)
    assert opened == []  # the calendar was never even opened


class FlakyCalendar(LocalCalendar):
    """Refuses to write one specific deadline."""

    def create(self, event):
        if "Quiz 3" in event.summary:
            raise CalendarError("Google Calendar refused the request (HTTP 500: backendError). Try again in a minute.")
        return super().create(event)


def test_o4_one_failure_does_not_stop_the_rest(settings, env, feed_text):
    report = sync_deadlines_to_calendar(
        settings, lambda: FlakyCalendar(env["calendar"]), 14, now=NOW, feed_text=feed_text
    )
    assert len(report.created) == 4
    assert [f.title for f in report.failed] == ["Quiz 3 closes"]
    assert "HTTP 500" in report.failed[0].error
    assert "1 failed" in report.message


def test_course_filter_and_empty_window(settings, env, feed_text):
    open_calendar = lambda: LocalCalendar(env["calendar"])  # noqa: E731
    only_math = sync_deadlines_to_calendar(settings, open_calendar, 14, course="MATH218", now=NOW, feed_text=feed_text)
    assert [r.summary for r in only_math.created] == ["[MATH218] Problem Set 4 is due"]
    nothing = sync_deadlines_to_calendar(settings, open_calendar, 1, now=NOW, feed_text=feed_text)
    assert nothing.checked == 0 and "nothing to add" in nothing.message
