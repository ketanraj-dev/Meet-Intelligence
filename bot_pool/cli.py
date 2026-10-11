"""Admin CLI for the bot account pool.

    python -m bot_pool.cli add --email bot1@gmail.com --profile-path /bot-profiles/bot1
    python -m bot_pool.cli list
    python -m bot_pool.cli disable --email bot1@gmail.com
    python -m bot_pool.cli enable --email bot1@gmail.com

--profile-path is the path AS SEEN INSIDE THE BOT CONTAINER (e.g.
/bot-profiles/bot1, matching the ./bot-profiles bind mount in
docker-compose.yml's `bot` service), not a path on the host.

See README "Bot account setup" for how to log an account in and capture its
Chrome profile before adding it here.
"""
import argparse

from common.db import SessionLocal
from common.models import BotAccount, BotAccountStatus


def add(email: str, profile_path: str) -> None:
    db = SessionLocal()
    try:
        db.add(BotAccount(email=email, profile_path=profile_path, status=BotAccountStatus.FREE))
        db.commit()
        print(f"added {email}")
    finally:
        db.close()


def list_accounts() -> None:
    db = SessionLocal()
    try:
        for account in db.query(BotAccount).order_by(BotAccount.email).all():
            print(
                f"{account.email}\t{account.status}\t"
                f"meeting={account.current_meeting_id}\t"
                f"last_heartbeat={account.last_heartbeat_at}"
            )
    finally:
        db.close()


def set_status(email: str, status: BotAccountStatus) -> None:
    db = SessionLocal()
    try:
        account = db.query(BotAccount).filter(BotAccount.email == email).first()
        if account is None:
            print(f"no such account: {email}")
            return
        account.status = status
        account.current_meeting_id = None
        db.commit()
        print(f"{email} -> {status.value}")
    finally:
        db.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Manage the bot account pool")
    sub = parser.add_subparsers(dest="command", required=True)

    add_p = sub.add_parser("add")
    add_p.add_argument("--email", required=True)
    add_p.add_argument("--profile-path", required=True)

    sub.add_parser("list")

    disable_p = sub.add_parser("disable")
    disable_p.add_argument("--email", required=True)

    enable_p = sub.add_parser("enable")
    enable_p.add_argument("--email", required=True)

    args = parser.parse_args()

    if args.command == "add":
        add(args.email, args.profile_path)
    elif args.command == "list":
        list_accounts()
    elif args.command == "disable":
        set_status(args.email, BotAccountStatus.DISABLED)
    elif args.command == "enable":
        set_status(args.email, BotAccountStatus.FREE)


if __name__ == "__main__":
    main()
