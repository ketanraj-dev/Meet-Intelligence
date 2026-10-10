"""Builds a Google Calendar API client for a given employee's calendar."""
from googleapiclient.discovery import build

from calendar_sync.auth import CalendarAuthProvider, get_auth_provider


def build_calendar_service(employee_email: str, auth_provider: CalendarAuthProvider | None = None):
    auth_provider = auth_provider or get_auth_provider()
    creds = auth_provider.get_credentials(employee_email)
    return build("calendar", "v3", credentials=creds, cache_discovery=False)
