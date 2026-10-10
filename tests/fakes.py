"""Test doubles for the Google Calendar API client.

Shaped like googleapiclient's `service.events().list(...).execute()` /
`.get(...).execute()` / `.patch(...).execute()` so calendar_sync.sync can be
exercised without any real network call or Google credentials.
"""
import httplib2
from googleapiclient.errors import HttpError


def http_error(status: int) -> HttpError:
    resp = httplib2.Response({"status": status})
    return HttpError(resp, b"{}", uri="https://fake.googleapis.com/")


class _Execute:
    def __init__(self, value):
        self._value = value

    def execute(self):
        return self._value


class FakeEventsResource:
    def __init__(self, pages=None, get_responses=None, fail_with_410_on_sync_token=False):
        self._pages = list(pages or [])
        self.list_calls = []
        self.patch_calls = []
        self._get_responses = get_responses or {}
        self._fail_with_410_on_sync_token = fail_with_410_on_sync_token
        self._raised = False

    def list(self, **kwargs):
        self.list_calls.append(kwargs)
        if self._fail_with_410_on_sync_token and not self._raised and kwargs.get("syncToken"):
            self._raised = True
            raise http_error(410)
        page = self._pages.pop(0) if self._pages else {"items": []}
        return _Execute(page)

    def get(self, calendarId, eventId):  # noqa: N803 (matches googleapiclient's kwarg names)
        return _Execute(self._get_responses.get(eventId, {}))

    def patch(self, calendarId, eventId, body):  # noqa: N803
        self.patch_calls.append({"calendarId": calendarId, "eventId": eventId, "body": body})
        return _Execute({})


class FakeCalendarService:
    def __init__(self, pages=None, get_responses=None, fail_with_410_on_sync_token=False):
        self._events = FakeEventsResource(pages, get_responses, fail_with_410_on_sync_token)

    def events(self):
        return self._events


def make_event(
    *,
    event_id="evt-1",
    ical_uid="ical-1",
    summary="Sync up",
    hangout_link="https://meet.google.com/abc-defg-hij",
    conference_entry_points=None,
    description=None,
    start="2026-11-01T10:00:00+05:30",
    etag='"etag-1"',
    status="confirmed",
    attendees=None,
):
    event = {
        "id": event_id,
        "iCalUID": ical_uid,
        "summary": summary,
        "etag": etag,
        "status": status,
        "start": {"dateTime": start},
    }
    if hangout_link:
        event["hangoutLink"] = hangout_link
    if conference_entry_points is not None:
        event["conferenceData"] = {"entryPoints": conference_entry_points}
    if description is not None:
        event["description"] = description
    if attendees is not None:
        event["attendees"] = attendees
    return event
