"""Settings read from the environment (and the project's .env file)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import load_dotenv

from .errors import ConfigError

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Load .env once, without overriding variables that are already set
# (tests and the stdio launcher set their own).
load_dotenv(PROJECT_ROOT / ".env", override=False)

DEFAULT_PORT = 8765
MAX_REMIND_HOURS = 672  # Google Calendar allows reminders up to 4 weeks before.
MAX_DAYS_AHEAD = 60  # Moodle's "recent and upcoming" export covers the next 60 days.


@dataclass(frozen=True)
class Settings:
    moodle_ical_url: str
    calendar_backend: str
    google_calendar_id: str
    google_credentials_file: Path
    google_token_file: Path
    local_calendar_file: Path
    timezone: str
    default_remind_hours: float
    http_timeout_seconds: float

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)


def _path(value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else PROJECT_ROOT / path


def load_settings() -> Settings:
    """Read settings fresh from the environment on every call."""
    timezone = os.getenv("TIMEZONE", "Asia/Beirut").strip() or "Asia/Beirut"
    try:
        ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ConfigError(f"TIMEZONE '{timezone}' is not a valid IANA time zone.") from exc

    backend = os.getenv("CALENDAR_BACKEND", "google").strip().lower() or "google"
    if backend not in {"google", "local"}:
        raise ConfigError("CALENDAR_BACKEND must be 'google' or 'local'.")

    raw_hours = os.getenv("DEFAULT_REMIND_HOURS", "24").strip() or "24"
    try:
        remind_hours = float(raw_hours)
    except ValueError as exc:
        raise ConfigError("DEFAULT_REMIND_HOURS must be a number.") from exc
    if not 0 <= remind_hours <= MAX_REMIND_HOURS:
        raise ConfigError(f"DEFAULT_REMIND_HOURS must be between 0 and {MAX_REMIND_HOURS}.")

    try:
        timeout = float(os.getenv("MOODLE_TIMEOUT_SECONDS", "15").strip() or 15)
    except ValueError as exc:
        raise ConfigError("MOODLE_TIMEOUT_SECONDS must be a number.") from exc

    return Settings(
        moodle_ical_url=os.getenv("MOODLE_ICAL_URL", "").strip(),
        calendar_backend=backend,
        google_calendar_id=os.getenv("GOOGLE_CALENDAR_ID", "primary").strip() or "primary",
        google_credentials_file=_path(os.getenv("GOOGLE_CREDENTIALS_FILE", "credentials.json")),
        google_token_file=_path(os.getenv("GOOGLE_TOKEN_FILE", "token.json")),
        local_calendar_file=_path(os.getenv("LOCAL_CALENDAR_FILE", "data/local_calendar.json")),
        timezone=timezone,
        default_remind_hours=remind_hours,
        http_timeout_seconds=timeout,
    )


def describe_source(url_or_path: str) -> str:
    """Name the feed without leaking its secret token (host or file name only)."""
    if not url_or_path:
        return "(not configured)"
    if url_or_path.lower().startswith(("http://", "https://")):
        return urlsplit(url_or_path).hostname or "(unknown host)"
    return f"file:{Path(url_or_path).name}"
