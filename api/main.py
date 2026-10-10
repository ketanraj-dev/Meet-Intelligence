"""FastAPI app: meeting CRUD for Phase 1."""
from contextlib import asynccontextmanager
from uuid import UUID

from fastapi import Depends, FastAPI, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from common.config import get_settings
from common.db import get_db
from common.logging import configure_logging, get_logger
from common.models import BotAccount, BotAccountStatus, Meeting, MeetingStatus
from common.storage import StorageClient
from api.schemas import (
    BotAccountCreate,
    BotAccountRead,
    BotAccountStatusUpdate,
    MeetingCreate,
    MeetingRead,
)

settings = get_settings()
configure_logging(settings.log_level)
logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        StorageClient(settings).ensure_bucket()
    except Exception:
        logger.warning("storage_bucket_ensure_failed", exc_info=True)
    yield


app = FastAPI(title="Meet Intelligence API", version="0.1.0", lifespan=lifespan)


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.post("/meetings", response_model=MeetingRead, status_code=201)
def create_meeting(payload: MeetingCreate, db: Session = Depends(get_db)) -> Meeting:
    meeting = Meeting(
        meet_url=payload.meet_url,
        scheduled_at=payload.scheduled_at,
        expected_languages=payload.expected_languages,
        status=MeetingStatus.SCHEDULED,
    )
    db.add(meeting)
    db.commit()
    db.refresh(meeting)
    logger.info("meeting_created", meeting_id=str(meeting.id))
    return meeting


@app.get("/meetings/{meeting_id}", response_model=MeetingRead)
def get_meeting(meeting_id: UUID, db: Session = Depends(get_db)) -> Meeting:
    meeting = db.get(Meeting, meeting_id)
    if meeting is None:
        raise HTTPException(status_code=404, detail="meeting not found")
    return meeting


@app.get("/meetings", response_model=list[MeetingRead])
def list_meetings(
    status: MeetingStatus | None = None,
    limit: int = Query(default=50, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
) -> list[Meeting]:
    stmt = select(Meeting).order_by(Meeting.scheduled_at.desc()).limit(limit).offset(offset)
    if status is not None:
        stmt = stmt.where(Meeting.status == status)
    return list(db.scalars(stmt))


@app.post("/bot-accounts", response_model=BotAccountRead, status_code=201)
def create_bot_account(payload: BotAccountCreate, db: Session = Depends(get_db)) -> BotAccount:
    account = BotAccount(
        email=payload.email,
        profile_path=payload.profile_path,
        status=BotAccountStatus.FREE,
    )
    db.add(account)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="a bot account with this email already exists")
    db.refresh(account)
    logger.info("bot_account_created", account_id=str(account.id), email=account.email)
    return account


@app.get("/bot-accounts", response_model=list[BotAccountRead])
def list_bot_accounts(
    status: BotAccountStatus | None = None, db: Session = Depends(get_db)
) -> list[BotAccount]:
    stmt = select(BotAccount).order_by(BotAccount.email)
    if status is not None:
        stmt = stmt.where(BotAccount.status == status)
    return list(db.scalars(stmt))


@app.patch("/bot-accounts/{account_id}", response_model=BotAccountRead)
def update_bot_account_status(
    account_id: UUID, payload: BotAccountStatusUpdate, db: Session = Depends(get_db)
) -> BotAccount:
    account = db.get(BotAccount, account_id)
    if account is None:
        raise HTTPException(status_code=404, detail="bot account not found")
    account.status = payload.status
    if payload.status in (BotAccountStatus.FREE, BotAccountStatus.DISABLED):
        account.current_meeting_id = None
        account.last_heartbeat_at = None
    db.commit()
    db.refresh(account)
    logger.info("bot_account_status_updated", account_id=str(account_id), status=payload.status.value)
    return account
