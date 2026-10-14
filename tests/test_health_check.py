from datetime import datetime, timedelta, timezone

from common.models import BotAccount, BotAccountStatus
from scheduler.health_check import accounts_due_for_health_check

WEEK = 7 * 24 * 3600


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def test_never_checked_account_is_due(db_session):
    account = BotAccount(email="a@example.com", profile_path="/p/a")
    db_session.add(account)
    db_session.commit()

    due = accounts_due_for_health_check(db_session, WEEK)

    assert [a.id for a in due] == [account.id]


def test_recently_checked_account_is_not_due(db_session):
    account = BotAccount(email="a@example.com", profile_path="/p/a", last_health_check_at=_now())
    db_session.add(account)
    db_session.commit()

    due = accounts_due_for_health_check(db_session, WEEK)

    assert due == []


def test_stale_checked_account_is_due_again(db_session):
    account = BotAccount(
        email="a@example.com", profile_path="/p/a", last_health_check_at=_now() - timedelta(days=10)
    )
    db_session.add(account)
    db_session.commit()

    due = accounts_due_for_health_check(db_session, WEEK)

    assert [a.id for a in due] == [account.id]


def test_in_use_account_is_never_due_even_if_never_checked(db_session):
    """Launching a health-check Chromium against an account's profile
    directory while a real meeting bot already has it open would collide
    on Chrome's own SingletonLock — must only ever check idle accounts."""
    account = BotAccount(
        email="a@example.com", profile_path="/p/a", status=BotAccountStatus.IN_USE
    )
    db_session.add(account)
    db_session.commit()

    assert accounts_due_for_health_check(db_session, WEEK) == []


def test_disabled_account_is_never_due(db_session):
    account = BotAccount(email="a@example.com", profile_path="/p/a", status=BotAccountStatus.DISABLED)
    db_session.add(account)
    db_session.commit()

    assert accounts_due_for_health_check(db_session, WEEK) == []
