"""Where the Action tool writes: Google Calendar, or a local JSON stand-in.

Both backends do the same two things: find an event by its dedup key, and
create one event with a single popup reminder.
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
DEDUP_PROPERTY = "dedupKey"
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


@dataclass(frozen=True)
class StoredEvent:
    event_id: str
    summary: str
    start: datetime
    reminder_minutes: int
    link: str | None


class CalendarBackend(Protocol):
    label: str

    def find(self, dedup_key: str) -> StoredEvent | None: ...

    def create(self, event: NewEvent) -> StoredEvent: ...


def open_calendar(settings: Settings) -> CalendarBackend:
    if settings.calendar_backend == "local":
        return LocalCalendar(settings.local_calendar_file)
    return GoogleCalendar.from_settings(settings)


class LocalCalendar:
    """A JSON file that behaves like a calendar. Used by tests and offline rehearsal."""

    _lock = threading.Lock()

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

    def find(self, dedup_key: str) -> StoredEvent | None:
        with self._lock:
            for row in self._load():
                if row["dedup_key"] == dedup_key:
                    return _stored_from_row(row)
        return None

    def create(self, event: NewEvent) -> StoredEvent:
        with self._lock:
            rows = self._load()
            row = {
                "id": f"local-{len(rows) + 1}",
                "summary": event.summary,
                "description": event.description,
                "start": event.start.isoformat(),
                "end": event.end.isoformat(),
                "timezone": event.timezone,
                "reminder_minutes": event.reminder_minutes,
                "dedup_key": event.dedup_key,
            }
            rows.append(row)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(rows, indent=2), encoding="utf-8")
        return _stored_from_row(row)


def _stored_from_row(row: dict[str, Any]) -> StoredEvent:
    return StoredEvent(
        event_id=row["id"],
        summary=row["summary"],
        start=datetime.fromisoformat(row["start"]),
        reminder_minutes=int(row["reminder_minutes"]),
        link=None,
    )


class GoogleCalendar:
    """Google Calendar API v3 through an authorised `service` object."""

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
            token_file.write_text(creds.to_json(), encoding="utf-8")
        service = build("calendar", "v3", credentials=creds, cache_discovery=False)
        return cls(service, settings.google_calendar_id)

    def find(self, dedup_key: str) -> StoredEvent | None:
        response = self._execute(
            self.service.events().list(
                calendarId=self.calendar_id,
                privateExtendedProperty=f"{DEDUP_PROPERTY}={dedup_key}",
                showDeleted=False,
                singleEvents=True,
                maxResults=1,
            )
        )
        items = response.get("items", [])
        return _stored_from_google(items[0]) if items else None

    def create(self, event: NewEvent) -> StoredEvent:
        body = {
            "summary": event.summary,
            "description": event.description,
            "start": {"dateTime": event.start.isoformat(), "timeZone": event.timezone},
            "end": {"dateTime": event.end.isoformat(), "timeZone": event.timezone},
            "reminders": {
                "useDefault": False,
                "overrides": [{"method": "popup", "minutes": event.reminder_minutes}],
            },
            "extendedProperties": {
                "private": {DEDUP_PROPERTY: event.dedup_key, "createdBy": "moodle-deadline-mcp"}
            },
        }
        created = self._execute(self.service.events().insert(calendarId=self.calendar_id, body=body))
        return _stored_from_google(created)

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


def _stored_from_google(item: dict[str, Any]) -> StoredEvent:
    start_raw = item.get("start", {}).get("dateTime") or f"{item.get('start', {}).get('date')}T00:00:00+00:00"
    overrides = item.get("reminders", {}).get("overrides") or [{"minutes": 0}]
    return StoredEvent(
        event_id=item["id"],
        summary=item.get("summary", ""),
        start=datetime.fromisoformat(start_raw.replace("Z", "+00:00")),
        reminder_minutes=int(overrides[0].get("minutes", 0)),
        link=item.get("htmlLink"),
    )
