"""Bot account pool: reserve / reschedule / cancel / checkout / release / reap.

Future bookings live in `bot_reservations`, one row per meeting, with a
Postgres EXCLUDE constraint preventing two overlapping windows on the same
account — see common/models.py:BotReservation. `bot_accounts.status` only
ever reflects LIVE state (is a bot running as this account right now?); it is
independent of how many future reservations an account holds.

Concurrency notes:
- `reserve_account` relies on the EXCLUDE constraint itself as the race
  arbiter: on conflict the INSERT fails inside a SAVEPOINT (`db.begin_nested`),
  we catch it and try the next candidate account. No row locking needed here
  since the constraint is the actual guard.
- `checkout_account`'s fallback path (no pre-existing reservation) filters out
  overlapping-reservation accounts in SQL and uses `LIMIT 1` together with
  `FOR UPDATE SKIP LOCKED` — using SKIP LOCKED *without* a LIMIT would lock
  every matching row in one statement, including ones this call has no
  intention of using, starving other concurrent callers of rows they could
  otherwise take. Keep that LIMIT.

Each caller is expected to `db.commit()` after these calls within its own
request/transaction boundary; functions here only `flush()`.
"""
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import literal, nulls_first, select
from sqlalchemy.dialects.postgresql import TSTZRANGE, Range
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from common.config import Settings, get_settings
from common.logging import get_logger
from common.models import BotAccount, BotAccountStatus, BotReservation, Meeting, MeetingStatus

logger = get_logger(__name__)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _window_for_meeting(meeting: Meeting, settings: Settings) -> Range:
    scheduled_at = meeting.scheduled_at
    if scheduled_at.tzinfo is None:
        scheduled_at = scheduled_at.replace(tzinfo=timezone.utc)
    else:
        scheduled_at = scheduled_at.astimezone(timezone.utc)
    start = scheduled_at - timedelta(seconds=settings.bot_reservation_lead_seconds)
    end = scheduled_at + timedelta(seconds=settings.bot_reservation_tail_seconds)
    return Range(lower=start, upper=end, bounds="[)")


def get_reservation(db: Session, meeting_id: uuid.UUID) -> BotReservation | None:
    return db.scalars(select(BotReservation).where(BotReservation.meeting_id == meeting_id)).first()


def reserve_account(db: Session, meeting: Meeting) -> BotAccount | None:
    """Reserve a (non-disabled) account for meeting's time window.

    Idempotent: if meeting already has a reservation, returns its account
    without creating another.
    """
    existing = get_reservation(db, meeting.id)
    if existing is not None:
        return db.get(BotAccount, existing.bot_account_id)

    window = _window_for_meeting(meeting, get_settings())
    # Least-recently-*used* first (nulls — never yet checked out — first of
    # all), not a fixed id order, so reservations rotate across every
    # account rather than piling onto whichever one happens to sort first.
    # last_used_at is only touched at checkout (see checkout_account), not
    # here — reserving doesn't itself constitute "use".
    candidates = db.scalars(
        select(BotAccount)
        .where(BotAccount.status != BotAccountStatus.DISABLED)
        .order_by(nulls_first(BotAccount.last_used_at.asc()))
    ).all()

    for account in candidates:
        try:
            with db.begin_nested():
                db.add(BotReservation(bot_account_id=account.id, meeting_id=meeting.id, window=window))
                db.flush()
        except IntegrityError:
            continue
        logger.info("bot_account_reserved", account_id=str(account.id), meeting_id=str(meeting.id))
        return account

    logger.warning("bot_account_reserve_failed_no_capacity", meeting_id=str(meeting.id))
    return None


def reschedule_reservation(db: Session, meeting: Meeting) -> BotAccount | None:
    """Moves meeting's reservation to its current scheduled_at.

    Tries to update the existing reservation's window in place (same
    account). If that would overlap another reservation on that account,
    drops this one and reserves a different account instead. Idempotent:
    calling this again with an unchanged scheduled_at just rewrites the same
    window on the same row — no second reservation is ever created.
    """
    window = _window_for_meeting(meeting, get_settings())
    existing = get_reservation(db, meeting.id)

    if existing is None:
        return reserve_account(db, meeting)

    try:
        with db.begin_nested():
            existing.window = window
            db.flush()
        logger.info(
            "bot_reservation_moved", account_id=str(existing.bot_account_id), meeting_id=str(meeting.id)
        )
        return db.get(BotAccount, existing.bot_account_id)
    except IntegrityError:
        pass

    logger.info(
        "bot_reservation_conflict_on_reschedule_swapping_account",
        account_id=str(existing.bot_account_id),
        meeting_id=str(meeting.id),
    )
    db.delete(existing)
    db.flush()
    return reserve_account(db, meeting)


def cancel_reservation(db: Session, meeting_id: uuid.UUID) -> BotAccount | None:
    """Deletes meeting's reservation (if any) and frees its account's live
    state (if a bot had already checked out for it). Returns the affected
    account so the caller can remove it as a calendar guest."""
    reservation = get_reservation(db, meeting_id)
    account = None
    if reservation is not None:
        account = db.get(BotAccount, reservation.bot_account_id)
        db.delete(reservation)

    live_account = db.scalars(select(BotAccount).where(BotAccount.current_meeting_id == meeting_id)).first()
    if live_account is not None:
        if live_account.status != BotAccountStatus.DISABLED:
            live_account.status = BotAccountStatus.FREE
        live_account.current_meeting_id = None
        live_account.last_heartbeat_at = None
        account = account or live_account

    db.flush()
    if account is not None:
        logger.info("bot_reservation_cancelled", account_id=str(account.id), meeting_id=str(meeting_id))
    return account


def checkout_account(db: Session, meeting: Meeting) -> BotAccount | None:
    """Called by the bot container at start. Prefers meeting's own
    reservation; falls back to any free account with no reservation
    overlapping meeting's window."""
    reservation = get_reservation(db, meeting.id)
    if reservation is not None:
        account = db.get(BotAccount, reservation.bot_account_id)
        if account is not None and account.status != BotAccountStatus.DISABLED:
            account.status = BotAccountStatus.IN_USE
            account.current_meeting_id = meeting.id
            account.last_heartbeat_at = _utcnow()
            account.last_used_at = _utcnow()
            db.flush()
            logger.info(
                "bot_account_checked_out_reserved", account_id=str(account.id), meeting_id=str(meeting.id)
            )
            return account

    window = _window_for_meeting(meeting, get_settings())
    window_literal = literal(window, type_=TSTZRANGE())
    overlap_exists = (
        select(BotReservation.id)
        .where(
            BotReservation.bot_account_id == BotAccount.id,
            BotReservation.window.op("&&")(window_literal),
        )
        .exists()
    )

    stmt = (
        select(BotAccount)
        .where(BotAccount.status == BotAccountStatus.FREE, ~overlap_exists)
        # Same least-recently-used rotation as reserve_account's candidate
        # order — keeps every account's Google session exercised instead
        # of always falling back to the same lowest-id account.
        .order_by(nulls_first(BotAccount.last_used_at.asc()))
        .with_for_update(skip_locked=True)
        .limit(1)
    )
    account = db.scalars(stmt).first()
    if account is None:
        return None

    account.status = BotAccountStatus.IN_USE
    account.current_meeting_id = meeting.id
    account.last_heartbeat_at = _utcnow()
    account.last_used_at = _utcnow()
    db.flush()
    logger.info("bot_account_checked_out_fallback", account_id=str(account.id), meeting_id=str(meeting.id))
    return account


def checkout_or_mark_unavailable(db: Session, meeting: Meeting) -> BotAccount | None:
    """Convenience wrapper for the bot launcher: on exhaustion, sets the
    meeting status instead of just returning None, since that status is the
    externally-visible signal (API, dashboard, outbound webhook in Phase 6)."""
    account = checkout_account(db, meeting)
    if account is None:
        meeting.status = MeetingStatus.NO_BOT_AVAILABLE
        db.flush()
        logger.error("no_bot_available", meeting_id=str(meeting.id))
    return account


def heartbeat(db: Session, account_id: uuid.UUID) -> None:
    account = db.get(BotAccount, account_id)
    if account is not None and account.status == BotAccountStatus.IN_USE:
        account.last_heartbeat_at = _utcnow()
        db.flush()


def release_account(db: Session, account_id: uuid.UUID) -> None:
    """Called on normal bot exit. Only touches live state — any reservation
    for the meeting this account just ran stays in bot_reservations (harmless;
    its window is now in the past)."""
    account = db.get(BotAccount, account_id)
    if account is None:
        return
    if account.status != BotAccountStatus.DISABLED:
        account.status = BotAccountStatus.FREE
    account.current_meeting_id = None
    account.last_heartbeat_at = None
    db.flush()
    logger.info("bot_account_released", account_id=str(account_id))


def reap_stale(db: Session, timeout_seconds: int) -> list[BotAccount]:
    """Release accounts whose heartbeat is older than timeout_seconds and mark
    their meeting failed. Idempotent / safe to call repeatedly or concurrently."""
    cutoff = _utcnow() - timedelta(seconds=timeout_seconds)
    stmt = (
        select(BotAccount)
        .where(
            BotAccount.status == BotAccountStatus.IN_USE,
            BotAccount.last_heartbeat_at.is_not(None),
            BotAccount.last_heartbeat_at < cutoff,
        )
        .with_for_update(skip_locked=True)
    )
    stale = list(db.scalars(stmt))
    for account in stale:
        meeting = db.get(Meeting, account.current_meeting_id) if account.current_meeting_id else None
        if meeting is not None and meeting.status not in (
            MeetingStatus.DONE,
            MeetingStatus.CANCELLED,
            MeetingStatus.FAILED,
        ):
            meeting.status = MeetingStatus.FAILED
            meeting.error = "bot heartbeat timed out"
        logger.warning(
            "bot_account_reaped", account_id=str(account.id), meeting_id=str(account.current_meeting_id)
        )
        account.status = BotAccountStatus.FREE
        account.current_meeting_id = None
        account.last_heartbeat_at = None
    db.flush()
    return stale
