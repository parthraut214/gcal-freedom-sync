import os
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

logger = logging.getLogger(__name__)

SCOPES = ['https://www.googleapis.com/auth/calendar.readonly']


class CalendarClient:
    def __init__(self, credentials_path: str, token_path: str):
        self.credentials_path = credentials_path
        self.token_path = token_path
        self._service = None

    def _get_service(self):
        if self._service:
            return self._service

        creds = None
        if os.path.exists(self.token_path):
            creds = Credentials.from_authorized_user_file(self.token_path, SCOPES)

        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
                logger.info("Refreshed Google OAuth token")
            else:
                # Headless-friendly: prints a URL and waits for the user to paste a code
                flow = InstalledAppFlow.from_client_secrets_file(self.credentials_path, SCOPES)
                creds = flow.run_console()

            with open(self.token_path, 'w') as f:
                f.write(creds.to_json())
            logger.info(f"Saved token to {self.token_path}")

        self._service = build('calendar', 'v3', credentials=creds, cache_discovery=False)
        return self._service

    def get_current_event(self, calendar_id: str, lookahead_seconds: int = 30) -> Optional[dict]:
        """Returns the active event (or one starting within lookahead window), or None."""
        service = self._get_service()
        now = datetime.now(timezone.utc)

        # Fetch events in a small window around now
        window_start = now - timedelta(hours=8)   # catch events that started up to 8h ago
        window_end = now + timedelta(seconds=lookahead_seconds + 10)

        result = service.events().list(
            calendarId=calendar_id,
            timeMin=window_start.isoformat(),
            timeMax=window_end.isoformat(),
            maxResults=20,
            singleEvents=True,
            orderBy='startTime',
        ).execute()

        for event in result.get('items', []):
            start = _parse_dt(event['start'])
            end = _parse_dt(event['end'])

            # Event is "current" if it has started (or starts within lookahead) and hasn't ended
            if start <= now + timedelta(seconds=lookahead_seconds) and end > now:
                return {
                    'id': event['id'],
                    'summary': event.get('summary', 'Untitled'),
                    'start': start,
                    'end': end,
                }

        return None

    def get_upcoming_events(self, calendar_id: str, count: int = 5, hours_ahead: int = 48) -> list:
        """Returns future events (start > now) sorted by start time."""
        service = self._get_service()
        now = datetime.now(timezone.utc)
        result = service.events().list(
            calendarId=calendar_id,
            timeMin=now.isoformat(),
            timeMax=(now + timedelta(hours=hours_ahead)).isoformat(),
            maxResults=count + 5,
            singleEvents=True,
            orderBy='startTime',
        ).execute()
        events = []
        for event in result.get('items', []):
            start = _parse_dt(event['start'])
            end = _parse_dt(event['end'])
            if start > now:
                events.append({
                    'id': event['id'],
                    'summary': event.get('summary', 'Untitled'),
                    'start': start,
                    'end': end,
                })
        return events[:count]


def _parse_dt(dt_dict: dict) -> datetime:
    if 'dateTime' in dt_dict:
        dt = datetime.fromisoformat(dt_dict['dateTime'])
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    # All-day event — treat as midnight UTC
    from datetime import date
    d = date.fromisoformat(dt_dict['date'])
    return datetime(d.year, d.month, d.day, tzinfo=timezone.utc)
