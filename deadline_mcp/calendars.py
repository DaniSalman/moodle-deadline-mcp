"""Where the Action tool writes: Google Calendar, or a local JSON stand-in.

Both backends do the same three things: find an event this tool created
before, create one event with a single popup reminder, and update such an
event when Moodle moves the deadline.

Every event carries three private keys so it can be found again:
  dedupKey  the Moodle event UID (or a hash for deadlines added by hand)
  dueAt     the due instant in UTC, e.g. 20261102T215900Z
  titleKey  the title squashed to lowercase letters and digits
  origin    "moodle" or "manual"
When the dedupKey is not found, a second lookup on dueAt + titleKey stops a
deadline added by hand and the same deadline synced from Moodle from becoming
two events. A Moodle deadline only matches events added by hand that way, so
two different Moodle events with the same title and time stay separate.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

from .config import Settings
from .errors import CalendarError

# Least privilege: manage events only, no access to calendar settings or sharing.
GOOGLE_SCOPES = ["https://www.googleapis.com/auth/calendar.events"]
AUTH_HELP = "Run `python scripts/google_auth.py` once to sign in to Google Calendar, then try again."


@dataclass(frozen=True)
class NewEvent:
    summary: str
    description: str
    start: datetime
    end: datetime
    timezone: str
    reminder_minutes: int
    dedup_key: str
    due_key: str
    title_key: str
    origin: str  # "moodle" or "manual"


@dataclass(frozen=True)
class StoredEvent:
    event_id: str
    summary: str
    start: datetime
    reminder_minutes: int
    link: str | None
    dedup_key: str


class CalendarBackend(Protocol):
    label: str
    lock: Any  # a context manager held around find + create/update

    def find(self, dedup_key: str) -> StoredEvent | None: ...

    def find_similar(self, due_key: str, title_key: str, manual_only: bool) -> StoredEvent | None: ...

    def create(self, event: NewEvent) -> StoredEvent: ...

    def update(self, event_id: str, event: NewEvent) -> StoredEvent: ...


def open_calendar(settings: Settings) -> CalendarBackend:
    if settings.calendar_backend == "local":
        return LocalCalendar(settings.local_calendar_file)
    return GoogleCalendar.from_settings(settings)


class LocalCalendar:
    """A JSON file that behaves like a calendar. Used by tests and offline rehearsal."""

    lock = threading.RLock()

    def __init__(self, path: Path):
        self.path = path
        self.label = f"local:{path.name}"

    def _load(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise CalendarError(f"The local calendar file {self.path.name} is unreadable ({exc}).") from exc

    def _save(self, rows: list[dict[str, Any]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(rows, indent=2, ensure_ascii=False), encoding="utf-8")

    def find(self, dedup_key: str) -> StoredEvent | None:
        with self.lock:
            rows = self._load()
        for row in rows:
            if row["dedup_key"] == dedup_key:
                return _stored_from_row(row)
        return None

    def find_similar(self, due_key: str, title_key: str, manual_only: bool) -> StoredEvent | None:
        with self.lock:
            rows = self._load()
        for row in rows:
            if row.get("due_key") == due_key and row.get("title_key") == title_key:
                if not manual_only or row.get("origin") == "manual":
                    return _stored_from_row(row)
        return None

    def create(self, event: NewEvent) -> StoredEvent:
        with self.lock:
            rows = self._load()
            row = {"id": f"local-{len(rows) + 1}", **_row_fields(event)}
            rows.append(row)
            self._save(rows)
        return _stored_from_row(row)

    def update(self, event_id: str, event: NewEvent) -> StoredEvent:
        with self.lock:
            rows = self._load()
            for row in rows:
                if row["id"] == event_id:
                    row.update(_row_fields(event))
                    self._save(rows)
                    return _stored_from_row(row)
        raise CalendarError(f"Event {event_id} is no longer in the local calendar.")


def _row_fields(event: NewEvent) -> dict[str, Any]:
    return {
        "summary": event.summary,
        "description": event.description,
        "start": event.start.isoformat(),
        "end": event.end.isoformat(),
        "timezone": event.timezone,
        "reminder_minutes": event.reminder_minutes,
        "dedup_key": event.dedup_key,
        "due_key": event.due_key,
        "title_key": event.title_key,
        "origin": event.origin,
    }


def _stored_from_row(row: dict[str, Any]) -> StoredEvent:
    return StoredEvent(
        event_id=row["id"],
        summary=row["summary"],
        start=datetime.fromisoformat(row["start"]),
        reminder_minutes=int(row["reminder_minutes"]),
        link=None,
        dedup_key=row["dedup_key"],
    )


class GoogleCalendar:
    """Google Calendar API v3 through an authorised `service` object."""

    lock = threading.RLock()

    def __init__(self, service: Any, calendar_id: str = "primary"):
        self.service = service
        self.calendar_id = calendar_id
        self.label = f"google:{calendar_id}"

    @classmethod
    def from_settings(cls, settings: Settings) -> GoogleCalendar:
        from google.auth.exceptions import RefreshError, TransportError
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build

        token_file = settings.google_token_file
        if not token_file.exists():
            raise CalendarError(f"Not signed in to Google Calendar yet. {AUTH_HELP}")
        try:
            creds = Credentials.from_authorized_user_file(str(token_file), GOOGLE_SCOPES)
        except (ValueError, OSError) as exc:
            raise CalendarError(f"The saved Google sign-in ({token_file.name}) is unreadable. {AUTH_HELP}") from exc
        if not creds.valid:
            if not (creds.expired and creds.refresh_token):
                raise CalendarError(f"The saved Google sign-in is no longer valid. {AUTH_HELP}")
            try:
                creds.refresh(Request())
            except RefreshError as exc:
                raise CalendarError(f"Google refused to renew the sign-in (it may have been revoked). {AUTH_HELP}") from exc
            except TransportError as exc:
                raise CalendarError("Could not reach Google to renew the sign-in. Check the internet connection.") from exc
            try:
                token_file.write_text(creds.to_json(), encoding="utf-8")
            except OSError:
                pass  # the refreshed token still works for this call; it is saved next time
        service = build("calendar", "v3", credentials=creds, cache_discovery=False)
        return cls(service, settings.google_calendar_id)

    def find(self, dedup_key: str) -> StoredEvent | None:
        return self._first([f"dedupKey={dedup_key}"])

    def find_similar(self, due_key: str, title_key: str, manual_only: bool) -> StoredEvent | None:
        constraints = [f"dueAt={due_key}", f"titleKey={title_key}"]
        return self._first(constraints + ["origin=manual"] if manual_only else constraints)

    def _first(self, constraints: list[str]) -> StoredEvent | None:
        # privateExtendedProperty is a repeated parameter: an event must match all of them.
        response = self._execute(
            self.service.events().list(
                calendarId=self.calendar_id,
                privateExtendedProperty=constraints,
                showDeleted=False,
                singleEvents=True,
                maxResults=1,
            )
        )
        items = response.get("items", [])
        return _stored_from_google(items[0]) if items else None

    def create(self, event: NewEvent) -> StoredEvent:
        created = self._execute(self.service.events().insert(calendarId=self.calendar_id, body=_google_body(event)))
        return _stored_from_google(created)

    def update(self, event_id: str, event: NewEvent) -> StoredEvent:
        updated = self._execute(
            self.service.events().patch(calendarId=self.calendar_id, eventId=event_id, body=_google_body(event))
        )
        return _stored_from_google(updated)

    def _execute(self, request: Any) -> dict[str, Any]:
        from google.auth.exceptions import RefreshError, TransportError
        from googleapiclient.errors import HttpError
        from httplib2 import HttpLib2Error

        try:
            return request.execute(num_retries=1)
        except HttpError as exc:
            status = getattr(exc, "status_code", None) or exc.resp.status
            reason = exc.reason if hasattr(exc, "reason") else exc._get_reason()
            hint = {
                400: "Google rejected the event as invalid; check the title and the date.",
                401: AUTH_HELP,
                403: f"The sign-in may lack calendar permission. {AUTH_HELP}",
                404: f"Calendar '{self.calendar_id}' was not found; check GOOGLE_CALENDAR_ID in .env.",
                429: "Google's rate limit was hit; wait a minute and try again.",
            }.get(int(status), "Try again in a minute.")
            raise CalendarError(f"Google Calendar refused the request (HTTP {status}: {reason}). {hint}") from exc
        except RefreshError as exc:
            raise CalendarError(f"The Google sign-in expired or was revoked. {AUTH_HELP}") from exc
        except (TransportError, HttpLib2Error, OSError) as exc:
            raise CalendarError(f"Could not reach Google Calendar ({type(exc).__name__}). Check the internet connection.") from exc


def _google_body(event: NewEvent) -> dict[str, Any]:
    return {
        "summary": event.summary,
        "description": event.description,
        "start": {"dateTime": event.start.isoformat(), "timeZone": event.timezone},
        "end": {"dateTime": event.end.isoformat(), "timeZone": event.timezone},
        "reminders": {
            "useDefault": False,
            "overrides": [{"method": "popup", "minutes": event.reminder_minutes}],
        },
        "extendedProperties": {
            "private": {
                "dedupKey": event.dedup_key,
                "dueAt": event.due_key,
                "titleKey": event.title_key,
                "origin": event.origin,
                "createdBy": "moodle-deadline-mcp",
            }
        },
    }


def _stored_from_google(item: dict[str, Any]) -> StoredEvent:
    start_raw = item.get("start", {}).get("dateTime") or f"{item.get('start', {}).get('date')}T00:00:00+00:00"
    overrides = item.get("reminders", {}).get("overrides") or [{"minutes": 0}]
    return StoredEvent(
        event_id=item["id"],
        summary=item.get("summary", ""),
        start=datetime.fromisoformat(start_raw.replace("Z", "+00:00")),
        reminder_minutes=int(overrides[0].get("minutes", 0)),
        link=item.get("htmlLink"),
        dedup_key=item.get("extendedProperties", {}).get("private", {}).get("dedupKey", ""),
    )
