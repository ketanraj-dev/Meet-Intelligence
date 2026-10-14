from datetime import datetime, timezone

import bot_pool.cli as cli_module
from common.models import BotAccount


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def test_list_accounts_shows_rotation_and_health_check_fields(db_session, monkeypatch, capsys):
    account = BotAccount(
        email="a@example.com",
        profile_path="/p/a",
        last_used_at=_now(),
        last_health_check_at=_now(),
        needs_relogin=True,
    )
    db_session.add(account)
    db_session.commit()

    monkeypatch.setattr(cli_module, "SessionLocal", lambda: db_session)
    # list_accounts() closes the session it's given — give it a throwaway
    # wrapper so the shared db_session fixture survives for any later use.
    monkeypatch.setattr(db_session, "close", lambda: None)

    cli_module.list_accounts()

    out = capsys.readouterr().out
    assert "a@example.com" in out
    assert "last_used=" in out
    assert "last_health_check=" in out
    assert "needs_relogin=True" in out
