from datetime import datetime, timedelta, timezone

from common.config import get_settings
from common.models import BotAccount, BotAccountStatus
from scheduler.main import _reap_once
from tests.conftest import TestingSessionLocal


def test_reap_once_commits_so_the_release_actually_persists(db_session):
    """Regression test for a real bug found live: reap_stale() only
    flush()es by design (callers are documented to commit), but the
    scheduler's reaper loop forgot to — every "successful" reap was silently
    rolled back by db.close(), so a crashed bot's account stayed stuck
    `in_use` forever despite the reaper's own logs claiming success every
    30s. Reproduces the exact scenario: reap on one session (mirroring the
    real loop's own fresh SessionLocal()), then verify the change is
    actually visible from a *different* session — proving it was committed,
    not just flushed-then-rolled-back."""
    account = BotAccount(email="reaper-commit-test@example.com", profile_path="/p/x")
    db_session.add(account)
    db_session.commit()

    account.status = BotAccountStatus.IN_USE
    account.last_heartbeat_at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=600)
    db_session.commit()

    settings = get_settings()
    reaper_session = TestingSessionLocal()
    try:
        released = _reap_once(reaper_session, settings)
        assert len(released) == 1
    finally:
        reaper_session.close()

    verify_session = TestingSessionLocal()
    try:
        refreshed = verify_session.get(BotAccount, account.id)
        assert refreshed.status == BotAccountStatus.FREE
        assert refreshed.last_heartbeat_at is None
    finally:
        verify_session.close()
