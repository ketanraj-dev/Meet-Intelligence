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

    # Meeting bot (Phase 2)
    bot_display_name: str = "Fincelerate Notetaker (recording)"
    bot_consent_message: str = (
        "Fincelerate Notetaker is recording and transcribing this meeting for internal analysis."
    )
    admission_timeout_seconds: int = 120
    alone_timeout_seconds: int = 60
    max_meeting_duration_seconds: int = 7200
    speaker_poll_interval_ms: int = 250
    # Raw per-tile speaking-indicator reads flicker a lot (confirmed on a
    # real call: dozens of sub-second start/stop pairs from normal speech
    # pauses). Debounced before being written to speakers.jsonl: a
    # stop->start gap shorter than this is merged into one continuous
    # speaking period (no stop/start emitted for the gap).
    speaking_debounce_gap_seconds: float = 1.0
    # A speaking burst shorter than this is dropped entirely (treated as
    # noise, not a real speaking_start/stop pair).
    speaking_min_burst_seconds: float = 0.3
    audio_segment_seconds: int = 300
    recordings_dir: str = "/recordings"
    # How long to wait for the page to settle (networkidle, best-effort) and
    # a recognizable pre-join screen to appear, before even trying the name
    # field. Longer than the old implicit ~5s so slow-loading pages (or ones
    # on an unexpected screen) get logged clearly instead of silently racing.
    pre_join_wait_seconds: int = 30
    # "signed_in" = the checked-out pool account's saved persistent profile
    # (default — guest mode is blocked outright by Google: "You can't join
    # this video call", confirmed on a real test). "guest" = fresh temp
    # profile, no sign-in, types the name field; kept as an option.
    bot_join_mode: str = "signed_in"
    bot_user_agent: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    )
    # Grace period before Docker SIGKILLs a bot container — must be long
    # enough for the SIGTERM handler to leave the call, stop ffmpeg, and
    # upload the recording.
    bot_stop_timeout_seconds: int = 30
    # Periodic (~60s) in-call debug captures (screenshot + HTML + console)
    # were essential for fixing selectors against real Meet DOM during
    # development, but are unneeded overhead once those selectors are
    # stable. Off by default; set true to re-enable while diagnosing a new
    # issue. Failure-triggered and key-moment captures (join click,
    # admission, exceptions) are unconditional regardless of this flag.
    bot_debug_capture: bool = False

    # Scheduler bot-launcher (docker-outside-of-docker)
    bot_launch_lead_seconds: int = 60
    bot_image: str = "meet-intelligence-bot"
    docker_network: str = "meet-intelligence_default"
    host_project_dir: str | None = None

    # Processing queue (Phase 3 adds the consumer)
    processing_queue_name: str = "meet-intelligence:processing"

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    @property
    def employee_email_list(self) -> list[str]:
        return [e.strip() for e in self.employee_emails.split(",") if e.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
