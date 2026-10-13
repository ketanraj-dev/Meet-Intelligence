import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

import bot.bot as bot_module
from bot.bot import (
    AdmissionDenied,
    AdmissionTimeout,
    CAMERA_TOGGLE_NAME_RE,
    DebugContext,
    GracefulShutdown,
    MIC_TOGGLE_NAME_RE,
    PageCrashed,
    _SpeakingDebouncer,
    _bot_build_version,
    _check_audio_level,
    _click_join_button,
    _collect_participants,
    _detect_screen,
    _detect_speaking,
    _is_admitted,
    _json_line,
    _log_sink_inputs,
    _monitor_call,
    _now_naive_utc,
    _post_consent_message,
    _save_debug_artifacts,
    _toggle_media_off,
    _wait_for_admission,
    _wait_for_pre_join_ready,
    load_selectors,
)


def _debug_ctx_stub(tmp_path: Path) -> DebugContext:
    """A DebugContext whose .capture() is cheap and never raises — none of
    the _monitor_call tests run long enough to hit the periodic 60s capture
    or need it to actually succeed, but the parameter has to be something."""
    return DebugContext(_settings_stub_with_storage(), tmp_path, "00000000-0000-0000-0000-000000000000", [])


SELECTORS = {
    "pre_join": {
        "name_input": "#name",
        "mic_toggle": "#mic",
        "camera_toggle": "#camera",
    },
    "in_call": {
        "leave_call_button": "#leave",
        "participants_list_item": ".participant",
        "participant_name_label": ".pin-label",
        "chat_panel_toggle": "#chat_toggle",
        "chat_message_input": "#chat_input",
        "chat_send_button": "#chat_send",
        # speaking_indicator intentionally absent — mirrors the real
        # selectors.yaml default (unverified; _detect_speaking no-ops).
    },
    "admission": {"denied_text": "text=denied", "removed_text": "text=removed", "waiting_text": "text=waiting"},
}


class FakeLocator:
    """Also doubles as the `.first` result of itself (for the
    participant-name-label lookup, which calls `.first.get_attribute(...)`),
    and optionally carries a fixed attribute value to return."""

    def __init__(self, count: int, attr: str | None = None):
        self._count = count
        self._attr = attr

    def count(self) -> int:
        return self._count

    @property
    def first(self) -> "FakeLocator":
        return self

    def get_attribute(self, name: str, timeout: int | None = None) -> str | None:
        return self._attr


class FakeTile:
    """A single participant video tile, as returned by
    page.locator(participants_list_item).nth(i) — what _collect_participants
    and _detect_speaking actually operate on."""

    def __init__(self, participant_id: str, name_label: str | None = None, speaking: bool = False):
        self.participant_id = participant_id
        self._name_label = name_label  # None = self/bot tile (no "Pin ..." control)
        self._speaking = speaking

    def get_attribute(self, name: str, timeout: int | None = None) -> str | None:
        if name == "data-participant-id":
            return self.participant_id
        return None

    def locator(self, selector: str) -> FakeLocator:
        if selector == SELECTORS["in_call"]["participant_name_label"]:
            if self._name_label is None:
                return FakeLocator(0)
            return FakeLocator(1, attr=self._name_label)
        # Any other selector is treated as a speaking-indicator lookup —
        # whatever string the test configures as in_call.speaking_indicator.
        return FakeLocator(1 if self._speaking else 0)


class FakeTilesLocator:
    def __init__(self, tiles: list[FakeTile]):
        self._tiles = tiles

    def count(self) -> int:
        return len(self._tiles)

    def nth(self, i: int) -> FakeTile:
        return self._tiles[i]


class FakeRoleLocator:
    """Stand-in for what page.get_by_role(...) returns — wraps a single
    optional element dict, e.g. {"text": "Ask to join", "aria-label": "...",
    "enabled": True}. None means "nothing matched"."""

    def __init__(self, item: dict | None):
        self._item = item
        self.click_calls = 0

    @property
    def first(self) -> "FakeRoleLocator":
        return self

    def count(self) -> int:
        return 1 if self._item else 0

    def wait_for(self, state: str = "visible", timeout: int | None = None) -> None:
        if not self._item:
            raise TimeoutError(f"no element to wait for (state={state})")

    def is_enabled(self) -> bool:
        return bool(self._item) and self._item.get("enabled", True)

    def inner_text(self) -> str:
        if not self._item:
            raise RuntimeError("no element")
        return self._item.get("text", "")

    def get_attribute(self, name: str, timeout: int | None = None) -> str | None:
        if not self._item:
            return None
        return self._item.get(name)

    def click(self, timeout: int | None = None) -> None:
        self.click_calls += 1
        if not self._item:
            raise RuntimeError("no element to click")


class FakeKeyboard:
    def __init__(self):
        self.pressed: list[str] = []

    def press(self, key: str) -> None:
        self.pressed.append(key)


class FakePage:
    """Minimal stand-in for Playwright's sync Page — only what bot.py calls."""

    def __init__(
        self,
        counts: dict | None = None,
        click_fails: set | None = None,
        url: str = "https://meet.google.com/abc-defg-hij",
        title: str = "Meet",
        html: str = "<html></html>",
        join_button: dict | None = None,
        mic_toggle_role: dict | None = None,
        camera_toggle_role: dict | None = None,
        tiles_selector: str = ".participant",
        tiles_sequence: "list[list[FakeTile]] | None" = None,
        leave_button_selector: str = "#leave",
        leave_button_sequence: "list[int] | None" = None,
    ):
        self._counts = counts or {}
        self._click_fails = click_fails or set()
        self.clicked: list[str] = []
        self.url = url
        self._title = title
        self._html = html
        self.load_state_calls: list[str] = []
        self.keyboard = FakeKeyboard()
        self._join_button = join_button
        self._mic_toggle_role = mic_toggle_role
        self._camera_toggle_role = camera_toggle_role
        # One FakeTilesLocator per call to page.locator(tiles_selector) — lets
        # a single _monitor_call test simulate participants changing across
        # successive polls. The last entry repeats once exhausted.
        self._tiles_selector = tiles_selector
        self._tiles_sequence = tiles_sequence
        # Same idea for the leave-button count, so a test can let a couple
        # of polls run (to observe event logging) before the call ends.
        self._leave_button_selector = leave_button_selector
        self._leave_button_sequence = leave_button_sequence

    def locator(self, selector: str):
        if self._leave_button_sequence is not None and selector == self._leave_button_selector:
            count = (
                self._leave_button_sequence[0]
                if len(self._leave_button_sequence) == 1
                else self._leave_button_sequence.pop(0)
            )
            return FakeLocator(count)
        if self._tiles_sequence is not None and selector == self._tiles_selector:
            tiles = self._tiles_sequence[0] if len(self._tiles_sequence) == 1 else self._tiles_sequence.pop(0)
            return FakeTilesLocator(tiles)
        return FakeLocator(self._counts.get(selector, 0))

    def click(self, selector: str, timeout: int | None = None) -> None:
        self.clicked.append(selector)
        if selector in self._click_fails:
            raise RuntimeError(f"could not click {selector}")

    def get_by_role(self, role: str, name=None) -> FakeRoleLocator:
        pattern_str = getattr(name, "pattern", str(name)).lower()
        if "ask to join" in pattern_str:
            return FakeRoleLocator(self._join_button)
        if "microphone" in pattern_str:
            return FakeRoleLocator(self._mic_toggle_role)
        if "camera" in pattern_str:
            return FakeRoleLocator(self._camera_toggle_role)
        return FakeRoleLocator(None)

    def wait_for_load_state(self, state: str, timeout: int | None = None) -> None:
        self.load_state_calls.append(state)

    def wait_for_selector(self, selector: str, state: str = "visible", timeout: int | None = None) -> None:
        if self._counts.get(selector, 0) <= 0:
            raise TimeoutError(f"selector not visible: {selector}")

    def fill(self, selector: str, value: str, timeout: int | None = None) -> None:
        self.clicked.append(f"fill:{selector}")
        if selector in self._click_fails:
            raise RuntimeError(f"could not fill {selector}")

    def title(self) -> str:
        return self._title

    def content(self) -> str:
        return self._html

    def screenshot(self, path: str, full_page: bool = True, timeout: int | None = None) -> None:
        Path(path).write_bytes(b"fake-png")


def _settings_stub(**overrides):
    base = dict(
        speaker_poll_interval_ms=10,
        max_meeting_duration_seconds=3600,
        alone_timeout_seconds=60,
        bot_consent_message="This meeting is being recorded.",
        bot_debug_capture=False,
        speaking_debounce_gap_seconds=1.0,
        speaking_min_burst_seconds=0.3,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def test_load_selectors_has_expected_sections():
    selectors = load_selectors()
    assert "pre_join" in selectors
    assert "in_call" in selectors
    assert "admission" in selectors
    assert "leave_call_button" in selectors["in_call"]
    assert "name_input" in selectors["pre_join"]
    # mic/camera toggles are now fallback-only (primary is keyboard shortcuts)
    assert "mic_toggle" in selectors["pre_join"]
    assert "camera_toggle" in selectors["pre_join"]
    # the join button is role-based now (JOIN_BUTTON_NAME_RE in bot.py), not
    # selector-based — nothing to assert here for it anymore.


def test_json_line_is_valid_single_line_json():
    line = _json_line({"event": "test", "count": 2})
    assert line.endswith("\n")
    assert line.count("\n") == 1


def test_now_naive_utc_has_no_tzinfo():
    assert _now_naive_utc().tzinfo is None


# --- _bot_build_version -------------------------------------------------------
# Regression coverage for a real incident: a live test was run against a
# stale bot image because it hadn't been rebuilt after a code change, and
# that was only discovered after the fact. The startup log line exists so
# the running build is visible up front instead.


def test_bot_build_version_reads_the_baked_in_file(tmp_path, monkeypatch):
    version_file = tmp_path / "BUILD_VERSION"
    version_file.write_text("20261013T000000Z\n")
    monkeypatch.setattr(bot_module, "BUILD_VERSION_PATH", version_file)
    assert _bot_build_version() == "20261013T000000Z"


def test_bot_build_version_is_unknown_when_file_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(bot_module, "BUILD_VERSION_PATH", tmp_path / "does-not-exist")
    assert _bot_build_version() == "unknown"


# --- _wait_for_admission -----------------------------------------------------


def test_wait_for_admission_succeeds_when_in_call_ui_appears():
    # leave button AND another participant's tile — leave_call_button alone
    # is NOT enough (also present during the waiting room), and neither is
    # a single tile (the bot's own renders even before admission); see
    # _is_admitted.
    page = FakePage(counts={"#leave": 1, ".participant": 2})
    _wait_for_admission(page, SELECTORS, timeout_seconds=5, crashed=threading.Event())


def test_wait_for_admission_leave_button_alone_is_not_admission():
    """The actual bug reported live: "Leave call" is present during the
    waiting room too, so it can't be sufficient on its own — this used to
    report in_call falsely within 1s of clicking "Ask to join". Uses a short
    *positive* timeout so the loop body genuinely runs and evaluates the
    page state at least once, rather than timing out before any check."""
    page = FakePage(counts={"#leave": 1})  # no participant tile yet
    with pytest.raises(AdmissionTimeout):
        _wait_for_admission(page, SELECTORS, timeout_seconds=2, crashed=threading.Event())


def test_wait_for_admission_waiting_room_text_blocks_admission_even_with_leave_button():
    page = FakePage(counts={"#leave": 1, ".participant": 1, "text=waiting": 1})
    with pytest.raises(AdmissionTimeout):
        _wait_for_admission(page, SELECTORS, timeout_seconds=2, crashed=threading.Event())


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


def test_wait_for_admission_logs_waiting_room_even_without_a_matching_waiting_text(monkeypatch):
    """Regression test for a real bug: selectors.yaml's waiting_text never
    actually matched Meet's real DOM across three separate live tests, so
    the waiting_room phase was never logged at all — admission_phase_changed
    went straight from "pending" to "in_call" (or to AdmissionTimeout),
    making a stuck wait indistinguishable from a bot that hadn't even
    clicked join yet. "Not yet admitted" must now log waiting_room on its
    own, without depending on that text."""
    page = FakePage(counts={"#leave": 1})  # not admitted (no participant tile), no waiting text either
    calls: list[tuple[str, dict]] = []

    def recording_info(event, **kwargs):
        calls.append((event, kwargs))

    monkeypatch.setattr(bot_module.logger, "info", recording_info)
    with pytest.raises(AdmissionTimeout):
        _wait_for_admission(page, SELECTORS, timeout_seconds=2, crashed=threading.Event())

    phase_changes = [kwargs for event, kwargs in calls if event == "admission_phase_changed"]
    assert any(kwargs.get("to_phase") == "waiting_room" for kwargs in phase_changes)


# --- _monitor_call ------------------------------------------------------------


def test_monitor_call_returns_removed_or_left_when_leave_button_gone(tmp_path):
    page = FakePage(counts={"#leave": 0})
    speaker_log = tmp_path / "speakers.jsonl"
    reason, suspect = _monitor_call(
        page,
        SELECTORS,
        _settings_stub(),
        speaker_log,
        threading.Event(),
        threading.Event(),
        _debug_ctx_stub(tmp_path),
        tmp_path,
        0.0,
    )
    assert reason == "removed_or_left"
    assert suspect is False  # no audio check ever ran (call ended immediately)


def test_monitor_call_returns_max_duration_reached(tmp_path):
    page = FakePage(counts={"#leave": 1, ".participant": 2})
    speaker_log = tmp_path / "speakers.jsonl"
    reason, suspect = _monitor_call(
        page,
        SELECTORS,
        _settings_stub(max_meeting_duration_seconds=-1),
        speaker_log,
        threading.Event(),
        threading.Event(),
        _debug_ctx_stub(tmp_path),
        tmp_path,
        0.0,
    )
    assert reason == "max_duration_reached"
    assert suspect is False


def test_monitor_call_raises_page_crashed_not_removed_or_left(tmp_path):
    """A crash also makes the leave button disappear — the crash check must
    win, or this would be silently misreported as a normal leave."""
    page = FakePage(counts={"#leave": 0})
    crashed = threading.Event()
    crashed.set()
    speaker_log = tmp_path / "speakers.jsonl"
    with pytest.raises(PageCrashed):
        _monitor_call(
            page,
            SELECTORS,
            _settings_stub(),
            speaker_log,
            crashed,
            threading.Event(),
            _debug_ctx_stub(tmp_path),
            tmp_path,
            0.0,
        )


def test_monitor_call_raises_graceful_shutdown_and_clicks_leave_button(tmp_path):
    page = FakePage(counts={"#leave": 1, ".participant": 2})
    shutdown_requested = threading.Event()
    shutdown_requested.set()
    speaker_log = tmp_path / "speakers.jsonl"
    with pytest.raises(GracefulShutdown):
        _monitor_call(
            page,
            SELECTORS,
            _settings_stub(),
            speaker_log,
            threading.Event(),
            shutdown_requested,
            _debug_ctx_stub(tmp_path),
            tmp_path,
            0.0,
        )
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


def test_detect_screen_recognizes_join_button_role_based():
    page = FakePage(join_button={"text": "Ask to join", "enabled": True})
    assert _detect_screen(page, SELECTORS) == "pre_join:join_button"


def test_detect_screen_in_call_wins_over_stale_join_button():
    """The actual bug this round: detection found a stale "Ask to join"
    match even though the bot had already been admitted, which then fed a
    join-button click attempt that correctly found nothing and raised — a
    false failure after a real success. in_call must always win."""
    page = FakePage(counts={"#leave": 1}, join_button={"text": "Ask to join", "enabled": True})
    assert _detect_screen(page, SELECTORS) == "in_call"


def test_wait_for_pre_join_ready_returns_detected_screen_immediately():
    page = FakePage(join_button={"text": "Ask to join", "enabled": True})
    screen = _wait_for_pre_join_ready(page, SELECTORS, timeout_seconds=5)
    assert screen == "pre_join:join_button"
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


def test_save_debug_artifacts_writes_all_local_files_under_step_subdir(tmp_path):
    page = FakePage(url="https://meet.google.com/abc-defg-hij", title="Some Screen")
    console_logs = ["[log] hello", "[error] something broke"]

    _save_debug_artifacts(
        _settings_stub_with_storage(), page, console_logs, tmp_path, "11111111-1111-1111-1111-111111111111", "RuntimeError"
    )

    debug_dir = tmp_path / "debug" / "RuntimeError"
    assert (debug_dir / "screenshot.png").read_bytes() == b"fake-png"
    assert (debug_dir / "page.html").read_text() == "<html></html>"
    assert "Some Screen" in (debug_dir / "page_info.json").read_text()
    assert "something broke" in (debug_dir / "console.log").read_text()


def test_save_debug_artifacts_different_steps_do_not_clobber_each_other(tmp_path):
    """Detection-time capture and a later failure capture must coexist, not
    overwrite one another."""
    page1 = FakePage(title="Detected screen")
    page2 = FakePage(title="Failure screen")
    meeting_id = "11111111-1111-1111-1111-111111111111"

    _save_debug_artifacts(_settings_stub_with_storage(), page1, [], tmp_path, meeting_id, "pre_join_detected_unknown")
    _save_debug_artifacts(_settings_stub_with_storage(), page2, [], tmp_path, meeting_id, "RuntimeError")

    assert "Detected screen" in (tmp_path / "debug" / "pre_join_detected_unknown" / "page_info.json").read_text()
    assert "Failure screen" in (tmp_path / "debug" / "RuntimeError" / "page_info.json").read_text()


def test_save_debug_artifacts_does_not_raise_when_upload_fails(tmp_path):
    """Storage is unreachable in this stub on purpose — must degrade to a
    logged warning, never take down the caller's exception handling."""
    page = FakePage()
    _save_debug_artifacts(
        _settings_stub_with_storage(), page, [], tmp_path, "11111111-1111-1111-1111-111111111111", "RuntimeError"
    )


# --- _click_join_button / _toggle_media_off ------------------------------------


def test_click_join_button_succeeds_when_visible_and_enabled():
    page = FakePage(join_button={"text": "Ask to join", "enabled": True})
    _click_join_button(page)  # must not raise


def test_click_join_button_raises_after_retries_when_never_found():
    page = FakePage(join_button=None)
    with pytest.raises(RuntimeError, match="could not click a join button"):
        _click_join_button(page)


def test_toggle_media_off_presses_keyboard_shortcut_first():
    page = FakePage(mic_toggle_role={"text": "Turn on microphone", "aria-label": "Turn on microphone", "enabled": True})
    _toggle_media_off(page, SELECTORS, "Control+d", MIC_TOGGLE_NAME_RE, "mic_toggle", "mic")
    assert "Control+d" in page.keyboard.pressed


def test_toggle_media_off_confirms_without_fallback_click_when_already_off():
    page = FakePage(mic_toggle_role={"text": "Turn on microphone", "aria-label": "Turn on microphone", "enabled": True})
    _toggle_media_off(page, SELECTORS, "Control+d", MIC_TOGGLE_NAME_RE, "mic_toggle", "mic")
    # "Turn on microphone" means mic is already off — no CSS fallback needed
    assert "#mic" not in page.clicked


def test_toggle_media_off_never_raises_even_with_nothing_found():
    page = FakePage(mic_toggle_role=None)  # role-based lookup finds nothing at all
    _toggle_media_off(page, SELECTORS, "Control+d", MIC_TOGGLE_NAME_RE, "mic_toggle", "mic")  # must not raise
    assert "#mic" in page.clicked  # falls all the way back to the CSS selector


# --- _is_admitted ---------------------------------------------------------------


def test_is_admitted_false_while_waiting_even_with_leave_button_present():
    """The actual bug: "Leave call" is present during the waiting room too."""
    page = FakePage(counts={"#leave": 1, ".participant": 2})
    assert _is_admitted(page, SELECTORS, waiting=True) is False


def test_is_admitted_false_without_a_participant_tile():
    page = FakePage(counts={"#leave": 1})  # leave button alone, no participant tile
    assert _is_admitted(page, SELECTORS, waiting=False) is False


def test_is_admitted_false_with_only_the_bots_own_tile():
    """The real bug this round: the bot's own video tile renders (and
    matches participants_list_item) even while still in the waiting room,
    before any host has admitted it — confirmed live, count stayed at 1
    through the whole wait and only jumped to 3 on real admission. A single
    tile must not be enough."""
    page = FakePage(counts={"#leave": 1, ".participant": 1})
    assert _is_admitted(page, SELECTORS, waiting=False) is False


def test_is_admitted_true_with_leave_button_and_another_participant_tile_and_not_waiting():
    page = FakePage(counts={"#leave": 1, ".participant": 2})
    assert _is_admitted(page, SELECTORS, waiting=False) is True


# --- _post_consent_message -------------------------------------------------------


def test_post_consent_message_opens_waits_sends_and_closes_panel():
    page = FakePage(counts={"#chat_input": 1})
    _post_consent_message(page, SELECTORS, _settings_stub())
    # opened (#chat_toggle), filled, sent, then closed (#chat_toggle again)
    assert page.clicked.count("#chat_toggle") == 2
    assert "fill:#chat_input" in page.clicked
    assert "#chat_send" in page.clicked


def test_post_consent_message_does_not_raise_when_panel_never_becomes_visible():
    page = FakePage(counts={})  # chat_input selector never satisfies wait_for_selector
    _post_consent_message(page, SELECTORS, _settings_stub())  # must not raise


def test_post_consent_message_still_closes_panel_on_failure():
    """Regression test for a real bug found live: the panel-close click used
    to live in a second try block only reached on success, so any failure
    (e.g. the open animation not finishing in time) left the "In-call
    messages" panel stuck open for the rest of the call — confirmed via
    debug captures at 60s and 120s still showing it open. The close click
    must run from `finally` whenever the panel was actually opened."""
    page = FakePage(counts={})  # open succeeds, but chat_input never becomes visible
    _post_consent_message(page, SELECTORS, _settings_stub())
    assert page.clicked.count("#chat_toggle") == 2  # opened once, closed once despite the failure


def test_post_consent_message_does_not_attempt_close_if_open_itself_failed():
    page = FakePage(counts={}, click_fails={"#chat_toggle"})
    _post_consent_message(page, SELECTORS, _settings_stub())
    assert page.clicked.count("#chat_toggle") == 1  # only the failed open attempt, no close


# --- _collect_participants / _detect_speaking ------------------------------------
# Real-DOM-evidence-backed: a 3-person test capture showed every non-self
# tile carries a `[aria-label^="Pin "]` descendant reading "Pin {name} to
# your main screen", while the bot's own tile has no such descendant at
# all (it had "Backgrounds and effects" there instead) — that absence is
# what identifies/excludes the bot's own tile below.


def test_collect_participants_excludes_the_bots_own_tile():
    tiles = [
        FakeTile("spaces/x/devices/1", name_label="Pin Information Technology to your main screen"),
        FakeTile("spaces/x/devices/2", name_label="Pin Ketan Raj to your main screen"),
        FakeTile("spaces/x/devices/3", name_label=None),  # the bot's own tile — no "Pin ..." control
    ]
    page = FakePage(tiles_sequence=[tiles])
    participants = _collect_participants(page, SELECTORS)
    assert participants == {
        "spaces/x/devices/1": "Information Technology",
        "spaces/x/devices/2": "Ketan Raj",
    }


def test_collect_participants_returns_empty_when_no_tiles():
    page = FakePage(tiles_sequence=[[]])
    assert _collect_participants(page, SELECTORS) == {}


def test_collect_participants_skips_a_tile_missing_participant_id():
    tiles = [FakeTile(None, name_label="Pin Someone to your main screen")]
    page = FakePage(tiles_sequence=[tiles])
    assert _collect_participants(page, SELECTORS) == {}


def test_detect_speaking_is_a_safe_no_op_when_selector_unset():
    """speakers.yaml ships with speaking_indicator unset — no real capture
    has caught a participant mid-speech yet. Must return empty, not guess."""
    tiles = [FakeTile("p1", name_label="Pin A to your main screen", speaking=True)]
    page = FakePage(tiles_sequence=[tiles])
    assert _detect_speaking(page, SELECTORS) == set()


def test_detect_speaking_returns_ids_of_tiles_matching_configured_indicator():
    selectors = {**SELECTORS, "in_call": {**SELECTORS["in_call"], "speaking_indicator": ".speaking-ring"}}
    tiles = [
        FakeTile("p1", name_label="Pin A to your main screen", speaking=True),
        FakeTile("p2", name_label="Pin B to your main screen", speaking=False),
    ]
    page = FakePage(tiles_sequence=[tiles])
    assert _detect_speaking(page, selectors) == {"p1"}


# --- _monitor_call: speakers.jsonl event redesign ---------------------------------


def test_monitor_call_logs_join_event_once_with_relative_and_wallclock_time(tmp_path):
    """Core of the redesign: events carry "t" (seconds since the audio
    recording started — same clock as the WAV) and "ts" (wall-clock), and a
    join is logged exactly once, not once per 250ms poll tick."""
    tile = FakeTile("spaces/x/devices/1", name_label="Pin Ketan Raj to your main screen")
    # Present (and re-read) for two polls, then the leave button disappears.
    page = FakePage(tiles_sequence=[[tile], [tile]], leave_button_sequence=[1, 1, 0])
    speaker_log = tmp_path / "speakers.jsonl"

    import time as time_module

    recording_start = time_module.time() - 5  # pretend recording started 5s ago

    reason, _suspect = _monitor_call(
        page,
        SELECTORS,
        _settings_stub(),
        speaker_log,
        threading.Event(),
        threading.Event(),
        _debug_ctx_stub(tmp_path),
        tmp_path,
        recording_start,
    )

    assert reason == "removed_or_left"
    lines = [line for line in speaker_log.read_text().splitlines() if line.strip()]
    join_events = [l for l in lines if '"event": "participant_joined"' in l]
    assert len(join_events) == 1
    assert '"participant_id": "spaces/x/devices/1"' in join_events[0]
    assert '"name": "Ketan Raj"' in join_events[0]
    assert '"t":' in join_events[0]
    assert '"ts":' in join_events[0]
    import json as json_module

    event = json_module.loads(join_events[0])
    assert event["t"] >= 4  # ~5s after a recording_start 5s in the past


def test_monitor_call_alone_timeout_based_on_other_participants_not_raw_tile_count(tmp_path):
    """known_participants already excludes the bot's own tile (see
    _collect_participants) — alone now means zero *other* participants,
    checked directly, rather than a raw tile count <= 1."""
    page = FakePage(counts={"#leave": 1}, tiles_sequence=[[]])
    speaker_log = tmp_path / "speakers.jsonl"
    reason, _suspect = _monitor_call(
        page,
        SELECTORS,
        _settings_stub(alone_timeout_seconds=0),
        speaker_log,
        threading.Event(),
        threading.Event(),
        _debug_ctx_stub(tmp_path),
        tmp_path,
        0.0,
    )
    assert reason == "alone_timeout"


# --- _SpeakingDebouncer ------------------------------------------------------------
# Pure logic tests against a controlled fake clock (not real wall-clock sleeps) — the
# real call this was built from (meeting e140849a-6c26-4130-828d-b47a9990e7a1) showed
# dozens of sub-second raw start/stop pairs from ordinary speech pauses.


def test_speaking_debouncer_drops_a_burst_shorter_than_min_burst():
    d = _SpeakingDebouncer(min_burst_seconds=0.3, gap_seconds=1.0)
    assert d.update({"p1"}, now=0.0) == []
    assert d.update(set(), now=0.1) == []  # stopped after only 0.1s — never confirmed
    assert d.update(set(), now=5.0) == []  # nothing left to say


def test_speaking_debouncer_confirms_a_burst_at_least_min_burst_long():
    d = _SpeakingDebouncer(min_burst_seconds=0.3, gap_seconds=1.0)
    assert d.update({"p1"}, now=0.0) == []
    assert d.update({"p1"}, now=0.2) == []
    events = d.update({"p1"}, now=0.35)
    assert events == [("p1", "speaking_start", 0.0)]  # backdated to the real burst start, not 0.35


def test_speaking_debouncer_merges_a_gap_shorter_than_gap_seconds():
    d = _SpeakingDebouncer(min_burst_seconds=0.3, gap_seconds=1.0)
    d.update({"p1"}, now=0.0)
    d.update({"p1"}, now=0.35)  # confirmed, speaking_start already emitted
    assert d.update(set(), now=0.5) == []  # gap starts
    assert d.update({"p1"}, now=0.9) == []  # resumed within 1.0s — merged, no stop/start pair
    assert d.update(set(), now=5.0) == []  # a new, later gap starts
    events = d.update(set(), now=6.1)
    assert events == [("p1", "speaking_stop", 5.0)]  # backdated to when THIS gap actually started


def test_speaking_debouncer_force_stop_only_emits_for_a_confirmed_participant():
    d = _SpeakingDebouncer(min_burst_seconds=0.3, gap_seconds=1.0)
    d.update({"p1"}, now=0.0)
    d.update({"p1"}, now=0.35)  # p1 confirmed
    assert d.force_stop("p1", at=10.0) == ("p1", "speaking_stop", 10.0)
    assert d.force_stop("p1", at=11.0) is None  # already stopped — no-op
    assert d.force_stop("p2", at=12.0) is None  # never was confirmed (e.g. burst too short, or never spoke)


# --- _monitor_call: debounce + raw flicker log wiring -------------------------------


def test_monitor_call_debounces_speaking_events_and_also_logs_raw_flicker(tmp_path):
    """Confirms _monitor_call actually wires _detect_speaking through
    _SpeakingDebouncer into speakers.jsonl, and separately logs the
    pre-debounce raw signal to a sibling speakers_raw.jsonl. Thresholds are
    zeroed out here so a single poll confirms instantly — the debounce
    *timing* itself is covered precisely by the _SpeakingDebouncer tests
    above; this only checks the wiring."""
    import json as json_module

    selectors = {**SELECTORS, "in_call": {**SELECTORS["in_call"], "speaking_indicator": ".speaking-ring"}}
    tile = FakeTile("p1", name_label="Pin A to your main screen", speaking=True)
    page = FakePage(tiles_sequence=[[tile]], leave_button_sequence=[1, 1, 0])
    speaker_log = tmp_path / "speakers.jsonl"
    settings = _settings_stub(speaking_min_burst_seconds=0.0, speaking_debounce_gap_seconds=0.0)

    reason, _suspect = _monitor_call(
        page,
        selectors,
        settings,
        speaker_log,
        threading.Event(),
        threading.Event(),
        _debug_ctx_stub(tmp_path),
        tmp_path,
        0.0,
    )
    assert reason == "removed_or_left"

    debounced = [json_module.loads(l) for l in speaker_log.read_text().splitlines() if l.strip()]
    assert any(e["event"] == "speaking_start" and e["participant_id"] == "p1" for e in debounced)

    raw_log = tmp_path / "speakers_raw.jsonl"
    raw_events = [json_module.loads(l) for l in raw_log.read_text().splitlines() if l.strip()]
    assert any(e["event"] == "speaking_start" and e["participant_id"] == "p1" for e in raw_events)


# --- _monitor_call: end-of-call speaking_indicator self-check (item 3) --------------


def test_monitor_call_flags_suspect_when_audio_had_speech_but_no_speaking_events(tmp_path, monkeypatch):
    """If audio_level_check found real speech but the call produced zero
    confirmed speaking_start events, that's a strong sign the
    speaking_indicator selector stopped matching (e.g. a Meet UI change) —
    must be flagged rather than failing silently."""
    clock = {"t": 1000.0}
    monkeypatch.setattr(bot_module.time, "time", lambda: clock["t"])
    monkeypatch.setattr(bot_module.time, "sleep", lambda _s: clock.__setitem__("t", clock["t"] + 31))
    monkeypatch.setattr(bot_module, "_check_audio_level", lambda wav_path: True)

    (tmp_path / "audio_00000.wav").write_bytes(b"x")
    # No speaking_indicator configured, and no participant tiles at all — the
    # point is that speaking never gets confirmed, regardless of why.
    page = FakePage(leave_button_sequence=[1, 1, 0])
    speaker_log = tmp_path / "speakers.jsonl"

    reason, suspect = _monitor_call(
        page,
        SELECTORS,
        _settings_stub(),
        speaker_log,
        threading.Event(),
        threading.Event(),
        _debug_ctx_stub(tmp_path),
        tmp_path,
        1000.0,
    )
    assert reason == "removed_or_left"
    assert suspect is True


def test_monitor_call_does_not_flag_suspect_when_nobody_spoke(tmp_path, monkeypatch):
    """Silent audio + zero speaking events is the normal/expected case (an
    empty call, or everyone muted) — not a broken selector."""
    clock = {"t": 1000.0}
    monkeypatch.setattr(bot_module.time, "time", lambda: clock["t"])
    monkeypatch.setattr(bot_module.time, "sleep", lambda _s: clock.__setitem__("t", clock["t"] + 31))
    monkeypatch.setattr(bot_module, "_check_audio_level", lambda wav_path: False)

    (tmp_path / "audio_00000.wav").write_bytes(b"x")
    page = FakePage(leave_button_sequence=[1, 1, 0])
    speaker_log = tmp_path / "speakers.jsonl"

    reason, suspect = _monitor_call(
        page,
        SELECTORS,
        _settings_stub(),
        speaker_log,
        threading.Event(),
        threading.Event(),
        _debug_ctx_stub(tmp_path),
        tmp_path,
        1000.0,
    )
    assert reason == "removed_or_left"
    assert suspect is False


# --- audio diagnostics (_log_sink_inputs / _check_audio_level) -------------------
# Neither ffmpeg nor pactl are installed in this test environment (confirmed
# live in the Docker image instead — see README) — these only verify the
# "never breaks the caller" contract when the tool itself is unavailable.


def test_log_sink_inputs_does_not_raise_when_pactl_missing():
    _log_sink_inputs()


def test_check_audio_level_does_not_raise_when_ffmpeg_missing(tmp_path):
    wav = tmp_path / "audio_00000.wav"
    wav.write_bytes(b"not actually a wav file")
    assert _check_audio_level(wav) is None  # inconclusive, not a crash


def _fake_ffmpeg_result(mean_db: str, max_db: str):
    stderr = f"[Parsed_volumedetect_0 @ 0x0] mean_volume: {mean_db} dB\n[Parsed_volumedetect_0 @ 0x0] max_volume: {max_db} dB\n"
    return SimpleNamespace(stdout="", stderr=stderr)


def test_check_audio_level_returns_true_for_real_speech(tmp_path, monkeypatch):
    """The end-of-call self-check (item 3) hinges on this being a real
    bool, not just a log line — confirm the parsing actually yields True
    for a loud-enough recording."""
    wav = tmp_path / "audio_00000.wav"
    wav.write_bytes(b"irrelevant, subprocess.run is mocked")
    monkeypatch.setattr(bot_module.subprocess, "run", lambda *a, **k: _fake_ffmpeg_result("-20.0", "-16.4"))
    assert _check_audio_level(wav) is True


def test_check_audio_level_returns_false_when_silent(tmp_path, monkeypatch):
    wav = tmp_path / "audio_00000.wav"
    wav.write_bytes(b"irrelevant, subprocess.run is mocked")
    monkeypatch.setattr(bot_module.subprocess, "run", lambda *a, **k: _fake_ffmpeg_result("-91.0", "-91.0"))
    assert _check_audio_level(wav) is False


# --- DebugContext -----------------------------------------------------------------


def test_debug_context_capture_delegates_to_save_debug_artifacts(tmp_path):
    page = FakePage(title="Captured via DebugContext")
    ctx = DebugContext(_settings_stub_with_storage(), tmp_path, "11111111-1111-1111-1111-111111111111", ["[log] hi"])
    ctx.capture(page, "some_step")
    assert "Captured via DebugContext" in (tmp_path / "debug" / "some_step" / "page_info.json").read_text()
