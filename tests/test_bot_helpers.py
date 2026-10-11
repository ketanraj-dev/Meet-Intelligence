import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from bot.bot import (
    AdmissionDenied,
    AdmissionTimeout,
    GracefulShutdown,
    PageCrashed,
    _detect_screen,
    _json_line,
    _monitor_call,
    _now_naive_utc,
    _save_debug_artifacts,
    _wait_for_admission,
    _wait_for_pre_join_ready,
    load_selectors,
)

SELECTORS = {
    "pre_join": {
        "name_input": "#name",
        "ask_to_join_button": "#ask",
        "join_now_button": "#join",
        "mic_toggle": "#mic",
    },
    "in_call": {"leave_call_button": "#leave", "participants_list_item": ".participant"},
    "admission": {"denied_text": "text=denied", "removed_text": "text=removed"},
}


class FakeLocator:
    def __init__(self, count: int):
        self._count = count

    def count(self) -> int:
        return self._count


class FakePage:
    """Minimal stand-in for Playwright's sync Page — only what bot.py calls."""

    def __init__(
        self,
        counts: dict | None = None,
        click_fails: set | None = None,
        url: str = "https://meet.google.com/abc-defg-hij",
        title: str = "Meet",
        html: str = "<html></html>",
    ):
        self._counts = counts or {}
        self._click_fails = click_fails or set()
        self.clicked: list[str] = []
        self.url = url
        self._title = title
        self._html = html
        self.load_state_calls: list[str] = []

    def locator(self, selector: str) -> FakeLocator:
        return FakeLocator(self._counts.get(selector, 0))

    def click(self, selector: str, timeout: int | None = None) -> None:
        self.clicked.append(selector)
        if selector in self._click_fails:
            raise RuntimeError(f"could not click {selector}")

    def wait_for_load_state(self, state: str, timeout: int | None = None) -> None:
        self.load_state_calls.append(state)

    def title(self) -> str:
        return self._title

    def content(self) -> str:
        return self._html

    def screenshot(self, path: str, full_page: bool = True, timeout: int | None = None) -> None:
        Path(path).write_bytes(b"fake-png")


def _settings_stub(**overrides):
    base = dict(speaker_poll_interval_ms=10, max_meeting_duration_seconds=3600, alone_timeout_seconds=60)
    base.update(overrides)
    return SimpleNamespace(**base)


def test_load_selectors_has_expected_sections():
    selectors = load_selectors()
    assert "pre_join" in selectors
    assert "in_call" in selectors
    assert "admission" in selectors
    assert "leave_call_button" in selectors["in_call"]
    assert "ask_to_join_button" in selectors["pre_join"]
    assert "name_input" in selectors["pre_join"]


def test_json_line_is_valid_single_line_json():
    line = _json_line({"event": "test", "count": 2})
    assert line.endswith("\n")
    assert line.count("\n") == 1


def test_now_naive_utc_has_no_tzinfo():
    assert _now_naive_utc().tzinfo is None


# --- _wait_for_admission -----------------------------------------------------


def test_wait_for_admission_succeeds_when_in_call_ui_appears():
    page = FakePage(counts={"#leave": 1})
    _wait_for_admission(page, SELECTORS, timeout_seconds=5, crashed=threading.Event())


def test_wait_for_admission_times_out_when_never_admitted():
    page = FakePage(counts={})
    with pytest.raises(AdmissionTimeout):
        _wait_for_admission(page, SELECTORS, timeout_seconds=-1, crashed=threading.Event())


def test_wait_for_admission_raises_denied():
    page = FakePage(counts={"text=denied": 1})
    with pytest.raises(AdmissionDenied):
        _wait_for_admission(page, SELECTORS, timeout_seconds=5, crashed=threading.Event())


def test_wait_for_admission_raises_page_crashed():
    page = FakePage(counts={})
    crashed = threading.Event()
    crashed.set()
    with pytest.raises(PageCrashed):
        _wait_for_admission(page, SELECTORS, timeout_seconds=5, crashed=crashed)


# --- _monitor_call ------------------------------------------------------------


def test_monitor_call_returns_removed_or_left_when_leave_button_gone(tmp_path):
    page = FakePage(counts={"#leave": 0})
    speaker_log = tmp_path / "speakers.jsonl"
    reason = _monitor_call(
        page, SELECTORS, _settings_stub(), speaker_log, threading.Event(), threading.Event()
    )
    assert reason == "removed_or_left"


def test_monitor_call_returns_max_duration_reached(tmp_path):
    page = FakePage(counts={"#leave": 1, ".participant": 2})
    speaker_log = tmp_path / "speakers.jsonl"
    reason = _monitor_call(
        page,
        SELECTORS,
        _settings_stub(max_meeting_duration_seconds=-1),
        speaker_log,
        threading.Event(),
        threading.Event(),
    )
    assert reason == "max_duration_reached"


def test_monitor_call_raises_page_crashed_not_removed_or_left(tmp_path):
    """A crash also makes the leave button disappear — the crash check must
    win, or this would be silently misreported as a normal leave."""
    page = FakePage(counts={"#leave": 0})
    crashed = threading.Event()
    crashed.set()
    speaker_log = tmp_path / "speakers.jsonl"
    with pytest.raises(PageCrashed):
        _monitor_call(page, SELECTORS, _settings_stub(), speaker_log, crashed, threading.Event())


def test_monitor_call_raises_graceful_shutdown_and_clicks_leave_button(tmp_path):
    page = FakePage(counts={"#leave": 1, ".participant": 2})
    shutdown_requested = threading.Event()
    shutdown_requested.set()
    speaker_log = tmp_path / "speakers.jsonl"
    with pytest.raises(GracefulShutdown):
        _monitor_call(page, SELECTORS, _settings_stub(), speaker_log, threading.Event(), shutdown_requested)
    assert "#leave" in page.clicked


# --- _detect_screen / _wait_for_pre_join_ready --------------------------------


def test_detect_screen_recognizes_sign_in_interstitial():
    page = FakePage(url="https://accounts.google.com/signin/v2/identifier")
    assert _detect_screen(page, SELECTORS) == "google_sign_in_interstitial"


def test_detect_screen_recognizes_name_input():
    page = FakePage(counts={"#name": 1})
    assert _detect_screen(page, SELECTORS) == "pre_join:name_input"


def test_detect_screen_recognizes_in_call():
    page = FakePage(counts={"#leave": 1})
    assert _detect_screen(page, SELECTORS) == "in_call"


def test_detect_screen_unknown_when_nothing_matches():
    page = FakePage(counts={})
    assert _detect_screen(page, SELECTORS) == "unknown"


def test_wait_for_pre_join_ready_returns_detected_screen_immediately():
    page = FakePage(counts={"#ask": 1})
    screen = _wait_for_pre_join_ready(page, SELECTORS, timeout_seconds=5)
    assert screen == "pre_join:ask_to_join_button"
    assert page.load_state_calls == ["networkidle"]


def test_wait_for_pre_join_ready_returns_unknown_after_timeout():
    page = FakePage(counts={})
    screen = _wait_for_pre_join_ready(page, SELECTORS, timeout_seconds=0)
    assert screen == "unknown"


# --- _save_debug_artifacts -----------------------------------------------------


def _settings_stub_with_storage(**overrides):
    base = dict(
        minio_endpoint="127.0.0.1:1",  # nothing listening -> upload fails fast, caught gracefully
        minio_access_key="x",
        minio_secret_key="x",
        minio_bucket="test-bucket",
        minio_secure=False,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def test_save_debug_artifacts_writes_all_local_files(tmp_path):
    page = FakePage(url="https://meet.google.com/abc-defg-hij", title="Some Screen")
    console_logs = ["[log] hello", "[error] something broke"]

    _save_debug_artifacts(
        _settings_stub_with_storage(), page, console_logs, tmp_path, "11111111-1111-1111-1111-111111111111", "RuntimeError"
    )

    debug_dir = tmp_path / "debug"
    assert (debug_dir / "screenshot.png").read_bytes() == b"fake-png"
    assert (debug_dir / "page.html").read_text() == "<html></html>"
    assert "Some Screen" in (debug_dir / "page_info.json").read_text()
    assert "something broke" in (debug_dir / "console.log").read_text()


def test_save_debug_artifacts_does_not_raise_when_upload_fails(tmp_path):
    """Storage is unreachable in this stub on purpose — must degrade to a
    logged warning, never take down the caller's exception handling."""
    page = FakePage()
    _save_debug_artifacts(
        _settings_stub_with_storage(), page, [], tmp_path, "11111111-1111-1111-1111-111111111111", "RuntimeError"
    )
