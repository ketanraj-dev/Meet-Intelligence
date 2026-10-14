import inspect
from datetime import datetime, timedelta, timezone

import docker
import pytest
from docker.api.container import ContainerApiMixin
from docker.types import HostConfig

from common.config import get_settings
from common.models import Meeting, MeetingStatus
from scheduler.bot_launcher import (
    MAX_LAUNCH_ATTEMPTS,
    _handle_launch_failure,
    claim_meeting,
    due_meetings,
    launch_container,
    launch_health_check_container,
)


@pytest.fixture(autouse=True)
def _reset_settings_cache():
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _make_meeting(db, **kwargs) -> Meeting:
    meeting = Meeting(
        meet_url=kwargs.pop("meet_url", "https://meet.google.com/abc-defg-hij"),
        scheduled_at=kwargs.pop("scheduled_at", _now()),
        status=kwargs.pop("status", MeetingStatus.SCHEDULED),
        **kwargs,
    )
    db.add(meeting)
    db.commit()
    return meeting


def test_claim_meeting_flips_scheduled_to_joining_exactly_once(db_session):
    meeting = _make_meeting(db_session)

    assert claim_meeting(db_session, meeting.id) is True
    db_session.refresh(meeting)
    assert meeting.status == MeetingStatus.JOINING

    # a second claim attempt (e.g. a concurrent/overlapping launcher tick) is a no-op
    assert claim_meeting(db_session, meeting.id) is False


def test_due_meetings_only_returns_scheduled_within_lead_window(db_session):
    due_soon = _make_meeting(
        db_session, meet_url="https://meet.google.com/aaa-0001-aaa", scheduled_at=_now() + timedelta(seconds=30)
    )
    _make_meeting(  # too far in the future
        db_session, meet_url="https://meet.google.com/aaa-0002-aaa", scheduled_at=_now() + timedelta(hours=1)
    )
    _make_meeting(  # already past the grace cutoff
        db_session, meet_url="https://meet.google.com/aaa-0003-aaa", scheduled_at=_now() - timedelta(minutes=30)
    )
    _make_meeting(  # right time window, but not scheduled anymore
        db_session,
        meet_url="https://meet.google.com/aaa-0004-aaa",
        scheduled_at=_now() + timedelta(seconds=10),
        status=MeetingStatus.JOINING,
    )

    due = due_meetings(db_session, lead_seconds=60)

    assert [m.id for m in due] == [due_soon.id]


def test_launch_container_raises_without_host_project_dir(monkeypatch):
    """Must raise, not silently return — a silent return here combined
    with claim_meeting's status flip is exactly the old bug: the meeting
    is left stuck `joining` with no container ever started and nothing to
    revert it. Never touches the Docker SDK (would raise anyway — no real
    docker.sock in this test environment)."""
    monkeypatch.setenv("HOST_PROJECT_DIR", "")
    get_settings.cache_clear()
    settings = get_settings()

    with pytest.raises(RuntimeError, match="HOST_PROJECT_DIR"):
        launch_container(settings, "00000000-0000-0000-0000-000000000000")


# --- launch_container's Docker SDK call vs. the real docker-py signatures --------
# This is what would have caught the actual production bug: containers.run()
# silently rejects a `stop_timeout` kwarg (TypeError at call time, not an
# APIError — the old code's `except docker.errors.APIError` never even caught
# it). Validates every kwarg launch_container actually passes against the
# REAL installed docker-py's signatures, with no live Docker daemon needed.


class _FakeAPIClient:
    def __init__(self):
        self.create_host_config_kwargs = None
        self.create_container_kwargs = None
        self.started_container_id = None

    def create_host_config(self, **kwargs):
        # create_host_config() itself is a thin `(self, *args, **kwargs)`
        # wrapper (confirmed via inspect.getsource) — the real kwarg
        # contract lives on HostConfig.__init__, so validate against that.
        inspect.signature(HostConfig.__init__).bind_partial(None, "1.41", **kwargs)
        self.create_host_config_kwargs = kwargs
        return {"fake": "host_config", **kwargs}

    def create_container(self, **kwargs):
        inspect.signature(ContainerApiMixin.create_container).bind_partial(None, **kwargs)
        self.create_container_kwargs = kwargs
        return {"Id": "fake-container-id"}

    def start(self, **kwargs):
        inspect.signature(ContainerApiMixin.start).bind_partial(None, **kwargs)
        self.started_container_id = kwargs.get("container")


class _FakeDockerClient:
    def __init__(self):
        self.api = _FakeAPIClient()


def test_launch_container_kwargs_are_valid_against_real_docker_py_signatures(monkeypatch):
    """The regression test for the actual production bug: every kwarg
    launch_container passes to create_host_config/create_container/start is
    bound against the real installed docker-py method signatures — this
    fails with the same TypeError a real call would raise if any kwarg
    (e.g. a stop_timeout on the wrong method) isn't actually accepted."""
    monkeypatch.setenv("HOST_PROJECT_DIR", "/srv/meet-intelligence")
    get_settings.cache_clear()
    settings = get_settings()

    fake_client = _FakeDockerClient()
    monkeypatch.setattr(docker, "from_env", lambda: fake_client)

    launch_container(settings, "00000000-0000-0000-0000-000000000000")

    assert fake_client.api.create_container_kwargs["stop_timeout"] == settings.bot_stop_timeout_seconds
    assert fake_client.api.create_host_config_kwargs["shm_size"] == "2g"
    assert fake_client.api.started_container_id == "fake-container-id"


def test_launch_health_check_container_kwargs_are_also_valid(monkeypatch):
    """launch_health_check_container shares _run_bot_container with
    launch_container — same real-signature validation, different
    environment (BOT_MODE=health_check, BOT_ACCOUNT_ID instead of
    MEETING_ID)."""
    monkeypatch.setenv("HOST_PROJECT_DIR", "/srv/meet-intelligence")
    get_settings.cache_clear()
    settings = get_settings()

    fake_client = _FakeDockerClient()
    monkeypatch.setattr(docker, "from_env", lambda: fake_client)

    launch_health_check_container(settings, "11111111-1111-1111-1111-111111111111")

    env = fake_client.api.create_container_kwargs["environment"]
    assert env["BOT_MODE"] == "health_check"
    assert env["BOT_ACCOUNT_ID"] == "11111111-1111-1111-1111-111111111111"
    assert "MEETING_ID" not in env
    assert fake_client.api.started_container_id == "fake-container-id"


def test_launch_container_stop_timeout_via_run_would_genuinely_fail():
    """Documents *why* the fix is necessary, independent of the fake above:
    containers.run()'s own kwarg allowlists (what the old code used) really
    don't include stop_timeout, on the actual installed docker-py."""
    import docker.models.containers as containers_module

    assert "stop_timeout" not in containers_module.RUN_CREATE_KWARGS
    assert "stop_timeout" not in containers_module.RUN_HOST_CONFIG_KWARGS
    # ...but the low-level API genuinely does accept it — confirming
    # launch_container's fix (using create_container directly) is valid,
    # not just "avoids the crash by also dropping the feature".
    assert "stop_timeout" in inspect.signature(ContainerApiMixin.create_container).parameters


# --- _handle_launch_failure ---------------------------------------------------


def test_handle_launch_failure_reverts_to_scheduled_under_the_attempt_cap(db_session):
    meeting = _make_meeting(db_session, status=MeetingStatus.JOINING)

    _handle_launch_failure(db_session, meeting, "boom")

    assert meeting.status == MeetingStatus.SCHEDULED
    assert meeting.launch_attempts == 1
    assert meeting.error == "boom"


def test_handle_launch_failure_gives_up_after_max_attempts(db_session):
    meeting = _make_meeting(db_session, status=MeetingStatus.JOINING, launch_attempts=MAX_LAUNCH_ATTEMPTS - 1)

    _handle_launch_failure(db_session, meeting, "still broken")

    assert meeting.status == MeetingStatus.FAILED
    assert meeting.launch_attempts == MAX_LAUNCH_ATTEMPTS
    assert meeting.error == "still broken"


def test_handle_launch_failure_persists_across_sessions(db_session):
    """Not just an in-memory mutation — same class of bug as the reaper's
    missing commit from an earlier phase: confirm it's actually committed,
    not left for the caller to forget."""
    from tests.conftest import TestingSessionLocal

    meeting = _make_meeting(db_session, status=MeetingStatus.JOINING)
    meeting_id = meeting.id
    _handle_launch_failure(db_session, meeting, "boom")

    verify_session = TestingSessionLocal()
    try:
        refreshed = verify_session.get(Meeting, meeting_id)
        assert refreshed.status == MeetingStatus.SCHEDULED
        assert refreshed.launch_attempts == 1
    finally:
        verify_session.close()
