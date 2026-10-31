"""One-time Google sign-in. Opens a browser, asks you to allow calendar access,
and saves the result to token.json so the MCP server can create events.

    python scripts/google_auth.py

Needs credentials.json (an OAuth "Desktop app" client) in the project folder;
see the README for the five-minute Google Cloud setup.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from google_auth_oauthlib.flow import InstalledAppFlow  # noqa: E402
from googleapiclient.discovery import build  # noqa: E402

from deadline_mcp.calendars import GOOGLE_SCOPES  # noqa: E402
from deadline_mcp.config import load_settings  # noqa: E402


def main() -> int:
    settings = load_settings()
    if not settings.google_credentials_file.exists():
        print(f"Missing {settings.google_credentials_file.name}. Download the OAuth client file from Google Cloud "
              "(APIs & Services -> Credentials -> your Desktop client -> Download JSON) and save it as "
              f"{settings.google_credentials_file}.")
        return 1

    flow = InstalledAppFlow.from_client_secrets_file(str(settings.google_credentials_file), GOOGLE_SCOPES)
    creds = flow.run_local_server(port=0, prompt="consent")
    settings.google_token_file.write_text(creds.to_json(), encoding="utf-8")
    print(f"Saved sign-in to {settings.google_token_file.name}.")

    service = build("calendar", "v3", credentials=creds, cache_discovery=False)
    page = service.events().list(calendarId=settings.google_calendar_id, maxResults=1).execute()
    print(f"Calendar '{page.get('summary', settings.google_calendar_id)}' is reachable. You're ready to go.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
