"""Google Calendar auth providers.

Calendar API only — never Meet or Drive. Two implementations behind one
interface, selected by CALENDAR_AUTH_MODE:

- ServiceAccountDWDProvider: a service account with domain-wide delegation,
  impersonating each employee. This is the primary/prod path. Requires the
  Admin console steps documented in the README.
- OAuthRefreshTokenProvider: a single user's stored refresh token. For local
  dev or single-user setups; ignores which employee is asked for since there's
  only one identity behind it.
"""
import abc
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2 import service_account
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

from common.config import Settings, get_settings

CALENDAR_READONLY_SCOPE = "https://www.googleapis.com/auth/calendar.readonly"
CALENDAR_EVENTS_SCOPE = "https://www.googleapis.com/auth/calendar.events"


def scopes_for(settings: Settings) -> list[str]:
    return [CALENDAR_EVENTS_SCOPE] if settings.auto_add_bot_guest else [CALENDAR_READONLY_SCOPE]


class CalendarAuthProvider(abc.ABC):
    """Produces Google API credentials usable to read (and maybe edit) a given
    employee's calendar."""

    @abc.abstractmethod
    def get_credentials(self, employee_email: str):
        ...


class ServiceAccountDWDProvider(CalendarAuthProvider):
    def __init__(self, settings: Settings | None = None):
        self.settings = settings or get_settings()
        if not self.settings.google_service_account_file:
            raise ValueError(
                "GOOGLE_SERVICE_ACCOUNT_FILE is required when CALENDAR_AUTH_MODE=service_account"
            )

    def get_credentials(self, employee_email: str):
        base_creds = service_account.Credentials.from_service_account_file(
            self.settings.google_service_account_file,
            scopes=scopes_for(self.settings),
        )
        return base_creds.with_subject(employee_email)


class OAuthRefreshTokenProvider(CalendarAuthProvider):
    def __init__(self, settings: Settings | None = None):
        self.settings = settings or get_settings()
        self._creds: Credentials | None = None

    def get_credentials(self, employee_email: str):
        if self._creds is None or not self._creds.valid:
            self._creds = self._load_or_refresh()
        return self._creds

    def _load_or_refresh(self) -> Credentials:
        token_path = Path(self.settings.google_oauth_token_file)
        scopes = scopes_for(self.settings)
        creds = None
        if token_path.exists():
            creds = Credentials.from_authorized_user_file(str(token_path), scopes)

        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                if not self.settings.google_oauth_client_file:
                    raise ValueError(
                        "GOOGLE_OAUTH_CLIENT_FILE is required the first time (to run the "
                        "interactive consent flow) when CALENDAR_AUTH_MODE=oauth"
                    )
                flow = InstalledAppFlow.from_client_secrets_file(
                    self.settings.google_oauth_client_file, scopes
                )
                creds = flow.run_local_server(port=0)
            token_path.write_text(creds.to_json())
        return creds


def get_auth_provider(settings: Settings | None = None) -> CalendarAuthProvider:
    settings = settings or get_settings()
    if settings.calendar_auth_mode == "service_account":
        return ServiceAccountDWDProvider(settings)
    if settings.calendar_auth_mode == "oauth":
        return OAuthRefreshTokenProvider(settings)
    raise ValueError(f"Unknown CALENDAR_AUTH_MODE: {settings.calendar_auth_mode!r}")
