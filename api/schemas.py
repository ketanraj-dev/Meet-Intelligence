"""Pydantic request/response models for the API."""
from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, field_validator

from common.models import BotAccountStatus, MeetingStatus


class MeetingCreate(BaseModel):
    meet_url: str
    scheduled_at: datetime
    expected_languages: list[str] | None = None

    @field_validator("meet_url")
    @classmethod
    def validate_meet_url(cls, v: str) -> str:
        if "meet.google.com" not in v:
            raise ValueError("meet_url must be a meet.google.com link")
        return v


class MeetingRead(BaseModel):
    id: UUID
    meet_url: str
    scheduled_at: datetime
    started_at: datetime | None
    ended_at: datetime | None
    status: MeetingStatus
    expected_languages: list[str] | None
    audio_key: str | None
    speaker_events_key: str | None
    speaking_indicator_suspect: bool
    error: str | None
    google_event_id: str | None
    google_ical_uid: str | None
    google_etag: str | None
    owner_email: str | None
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class BotAccountCreate(BaseModel):
    email: str
    profile_path: str


class BotAccountRead(BaseModel):
    id: UUID
    email: str
    profile_path: str
    status: BotAccountStatus
    current_meeting_id: UUID | None
    last_heartbeat_at: datetime | None
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class BotAccountStatusUpdate(BaseModel):
    status: BotAccountStatus
