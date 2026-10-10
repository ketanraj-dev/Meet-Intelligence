"""Centralized app configuration, read from environment variables / .env."""
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_env: str = "development"
    log_level: str = "INFO"

    database_url: str
    redis_url: str = "redis://localhost:6379/0"

    minio_endpoint: str = "localhost:9000"
    minio_access_key: str = "accessKey1"
    minio_secret_key: str = "verySecretKey1"
    minio_bucket: str = "meet-intelligence"
    minio_secure: bool = False

    # Phase 4+
    hf_token: str | None = None

    # Phase 5+
    llm_provider: str = "openai"
    llm_api_key: str | None = None

    # Google Calendar booking trigger
    calendar_auth_mode: str = "oauth"  # "service_account" | "oauth"
    google_service_account_file: str | None = None
    google_oauth_client_file: str | None = None
    google_oauth_token_file: str = "oauth_token.json"
    employee_emails: str = ""  # comma-separated
    calendar_id: str = "primary"
    event_title_prefix: str | None = None
    calendar_poll_seconds: int = 180
    auto_add_bot_guest: bool = False

    # Bot account pool
    bot_heartbeat_interval_seconds: int = 30
    bot_heartbeat_timeout_seconds: int = 180
    bot_reservation_lead_seconds: int = 120
    bot_reservation_tail_seconds: int = 1800

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    @property
    def employee_email_list(self) -> list[str]:
        return [e.strip() for e in self.employee_emails.split(",") if e.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
