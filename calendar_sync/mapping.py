"""Pure functions mapping a Google Calendar API event dict to meeting fields.

No network calls here — kept pure so sync.py's tests can feed in plain dicts.
"""
import re
from collections.abc import Iterable

_LANGUAGE_LINE_RE = re.compile(r"^\s*language\s*:\s*(.+?)\s*$", re.IGNORECASE | re.MULTILINE)
_TITLE_CLIENT_NAME_RE = re.compile(r"\(([^()]+)\)\s*$")


def extract_meet_url(event: dict) -> str | None:
    """hangoutLink first, then any video entry point in conferenceData."""
    hangout_link = event.get("hangoutLink")
    if hangout_link:
        return hangout_link

    entry_points = (event.get("conferenceData") or {}).get("entryPoints") or []
    for entry_point in entry_points:
        if entry_point.get("entryPointType") == "video" and entry_point.get("uri"):
            return entry_point["uri"]

    return None


def parse_expected_languages(description: str | None) -> list[str] | None:
    """Parses a `Language: hi, en` line (case-insensitive) from the description."""
    if not description:
        return None
    match = _LANGUAGE_LINE_RE.search(description)
    if not match:
        return None
    codes = [code.strip().lower() for code in match.group(1).split(",") if code.strip()]
    return codes or None


def matches_title_prefix(event: dict, prefix: str | None) -> bool:
    if not prefix:
        return True
    return (event.get("summary") or "").startswith(prefix)


def extract_client_name_from_title(summary: str | None) -> str | None:
    """`30 min with Information (Acme Corp)` -> `Acme Corp`."""
    if not summary:
        return None
    match = _TITLE_CLIENT_NAME_RE.search(summary)
    if not match:
        return None
    name = match.group(1).strip()
    return name or None


def extract_client(
    event: dict, organizer_email: str | None, excluded_emails: Iterable[str] = ()
) -> dict | None:
    """The client is the attendee who is neither the organizer nor a bot
    account. Name comes from the title's trailing `(...)`, falling back to
    that attendee's `displayName`; email comes from the attendee itself.

    Returns {"name": str | None, "email": str} for the first such attendee
    found, or None if there isn't one.
    """
    attendees = event.get("attendees") or []
    organizer_lower = (organizer_email or "").lower()
    excluded_lower = {e.lower() for e in excluded_emails}

    for attendee in attendees:
        email = (attendee.get("email") or "").strip()
        if not email or email.lower() == organizer_lower or email.lower() in excluded_lower:
            continue
        name = extract_client_name_from_title(event.get("summary")) or attendee.get("displayName")
        return {"name": name, "email": email}

    return None
