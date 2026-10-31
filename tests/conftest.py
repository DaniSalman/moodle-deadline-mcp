from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from deadline_mcp.config import load_settings  # noqa: E402
from deadline_mcp.sample_feed import build_sample_ics  # noqa: E402

# A fixed "now" for tests of the core functions: Saturday 31 Oct 2026, 10:00 in Beirut.
NOW = datetime(2026, 10, 31, 8, 0, tzinfo=timezone.utc)
SECRET_URL = "https://lms.aub.edu.lb/calendar/export_execute.php?userid=4242&authtoken=SECRET0TOKEN0123&preset_what=all&preset_time=recentupcoming"


@pytest.fixture
def env(tmp_path, monkeypatch) -> dict[str, Path]:
    """Point the app at a sample feed and a local calendar file inside tmp_path."""
    feed = tmp_path / "moodle.ics"
    feed.write_text(build_sample_ics(), encoding="utf-8")
    calendar = tmp_path / "calendar.json"
    monkeypatch.setenv("MOODLE_ICAL_URL", str(feed))
    monkeypatch.setenv("CALENDAR_BACKEND", "local")
    monkeypatch.setenv("LOCAL_CALENDAR_FILE", str(calendar))
    monkeypatch.setenv("GOOGLE_TOKEN_FILE", str(tmp_path / "token.json"))
    monkeypatch.setenv("TIMEZONE", "Asia/Beirut")
    monkeypatch.setenv("DEFAULT_REMIND_HOURS", "24")
    return {"feed": feed, "calendar": calendar, "tmp": tmp_path}


@pytest.fixture
def settings(env):
    return load_settings()


@pytest.fixture
def feed_text() -> str:
    return build_sample_ics(NOW)


class FakeRequest:
    def __init__(self, fn):
        self.fn = fn

    def execute(self, num_retries: int = 0):
        return self.fn()


class FakeGoogleService:
    """Just enough of googleapiclient's Calendar service for the GoogleCalendar backend."""

    def __init__(self, fail_with: Exception | None = None):
        self.events_store: list[dict[str, Any]] = []
        self.inserted_bodies: list[dict[str, Any]] = []
        self.patched_bodies: list[dict[str, Any]] = []
        self.list_calls: list[list[str]] = []
        self.fail_with = fail_with

    def events(self):
        return self

    def list(self, calendarId, privateExtendedProperty, **kwargs):
        # Like the real API: every "key=value" constraint must match.
        constraints = [c.split("=", 1) for c in privateExtendedProperty]
        self.list_calls.append(privateExtendedProperty)

        def run():
            if self.fail_with:
                raise self.fail_with
            matches = [
                e for e in self.events_store
                if all(e["extendedProperties"]["private"].get(k) == v for k, v in constraints)
            ]
            return {"items": matches[:1]}

        return FakeRequest(run)

    def patch(self, calendarId, eventId, body):
        def run():
            if self.fail_with:
                raise self.fail_with
            event = next(e for e in self.events_store if e["id"] == eventId)
            event.update(body)
            self.patched_bodies.append(body)
            return event

        return FakeRequest(run)

    def insert(self, calendarId, body):
        def run():
            if self.fail_with:
                raise self.fail_with
            event = dict(body, id=f"g{len(self.events_store) + 1}", htmlLink=f"https://calendar.google.com/event?eid=g{len(self.events_store) + 1}")
            self.inserted_bodies.append(body)
            self.events_store.append(event)
            return event

        return FakeRequest(run)
