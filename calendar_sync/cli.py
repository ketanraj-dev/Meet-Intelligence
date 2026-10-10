"""Read-only verification tool for the calendar sync — run this before
trusting the background poller against a real Google Workspace domain.

    python -m calendar_sync.cli preview --employee it@fincelerate.com --limit 10

Lists the next N upcoming events matching EVENT_TITLE_PREFIX (if set), with
their extracted Meet link and client. Never writes to meetings,
calendar_sync_state, or anything else — it reads bot_accounts (to exclude
those emails from client extraction) but makes no writes at all.
"""
import argparse
from datetime import datetime, timezone

from sqlalchemy import select

from calendar_sync.client import build_calendar_service
from calendar_sync.mapping import extract_client, extract_meet_url, matches_title_prefix, parse_expected_languages
from common.config import get_settings
from common.db import SessionLocal
from common.models import BotAccount


def preview(employee_email: str, calendar_id: str, limit: int, service=None) -> None:
    settings = get_settings()
    service = service or build_calendar_service(employee_email)

    db = SessionLocal()
    try:
        bot_emails = set(db.scalars(select(BotAccount.email)).all())
    finally:
        db.close()

    matching: list[dict] = []
    page_token = None
    while len(matching) < limit:
        response = service.events().list(
            calendarId=calendar_id,
            timeMin=datetime.now(timezone.utc).isoformat(),
            singleEvents=True,
            orderBy="startTime",
            maxResults=min(limit, 50),
            pageToken=page_token,
        ).execute()

        for event in response.get("items", []):
            if not matches_title_prefix(event, settings.event_title_prefix):
                continue
            matching.append(event)
            if len(matching) >= limit:
                break

        page_token = response.get("nextPageToken")
        if not page_token:
            break

    prefix_note = f" matching EVENT_TITLE_PREFIX={settings.event_title_prefix!r}" if settings.event_title_prefix else ""
    print(f"Found {len(matching)} upcoming event(s){prefix_note} for {employee_email} (calendar={calendar_id}):\n")

    for event in matching:
        start = (event.get("start") or {}).get("dateTime") or (event.get("start") or {}).get("date")
        meet_url = extract_meet_url(event)
        client = extract_client(event, employee_email, bot_emails)
        languages = parse_expected_languages(event.get("description"))

        print(f"- {event.get('summary')!r}")
        print(f"    start:     {start}")
        print(f"    meet_url:  {meet_url}")
        print(f"    client:    {client}")
        print(f"    languages: {languages}")
        print()


def main() -> None:
    parser = argparse.ArgumentParser(description="Preview upcoming calendar events (read-only)")
    sub = parser.add_subparsers(dest="command", required=True)

    preview_p = sub.add_parser("preview", help="list upcoming matching events without writing to the DB")
    preview_p.add_argument("--employee", required=True, help="calendar to read, e.g. it@fincelerate.com")
    preview_p.add_argument("--calendar-id", default=None, help="defaults to CALENDAR_ID from settings")
    preview_p.add_argument("--limit", type=int, default=10)

    args = parser.parse_args()
    if args.command == "preview":
        settings = get_settings()
        preview(args.employee, args.calendar_id or settings.calendar_id, args.limit)


if __name__ == "__main__":
    main()
