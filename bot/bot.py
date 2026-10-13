"""Meeting bot entrypoint.

BOT_MODE=login: one-time interactive setup. Launches a persistent Chrome
profile + a VNC server so a human can log the bot's Gmail account in over
VNC. Makes no DB calls; stays up until stopped (`docker stop`), which closes
the browser cleanly so the profile is flushed to disk.

BOT_MODE=run (default): the actual meeting bot. Checks out a bot account
(for concurrency control, see BOT_JOIN_MODE below), joins MEET_URL
(Ask-to-join -> waiting room -> admitted is the NORMAL path, since bot
accounts are external Gmail, not Workspace members), records audio, logs
speaker/participant events, and uploads both on exit.

BOT_JOIN_MODE (run mode only), default "signed_in":
  - signed_in: joins using the checked-out bot account's saved, persistent
    Chrome profile. Default — guest mode was found to be blocked outright
    by Google ("You can't join this video call") on a real test.
  - guest: fresh temporary profile, no Google sign-in. Types the display
    name into Meet's "Your name" field on the pre-join screen. Kept as an
    option; not the default while it's blocked.
Either way the bot pool still gates concurrency (one container per
meeting); only the browser profile differs.
"""
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import yaml
from playwright.sync_api import sync_playwright

from bot_pool.service import checkout_or_mark_unavailable, heartbeat, release_account
from common.config import Settings, get_settings
from common.db import SessionLocal
from common.logging import configure_logging, get_logger
from common.models import Meeting, MeetingStatus
from common.queue import enqueue_processing
from common.storage import StorageClient

logger = get_logger(__name__)
SELECTORS_PATH = Path(__file__).parent / "selectors.yaml"

# Role-based locators, used identically for BOTH detection and clicking —
# a real bug came from detection and clicking using different selectors:
# detection (a CSS :has-text selector) found "Ask to join" present, but by
# the time the click loop ran several seconds later (after name/mic/camera
# attempts), the button was gone — because the bot had *already* been
# admitted into the call. Using the exact same locator everywhere removes
# that whole class of mismatch, and role-based locators are more robust to
# DOM restructuring than :has-text CSS selectors in general.
JOIN_BUTTON_NAME_RE = re.compile(r"ask to join|join now", re.I)
MIC_TOGGLE_NAME_RE = re.compile(r"turn (on|off) microphone", re.I)
CAMERA_TOGGLE_NAME_RE = re.compile(r"turn (on|off) camera", re.I)


def _join_button_locator(page):
    return page.get_by_role("button", name=JOIN_BUTTON_NAME_RE)


class AdmissionTimeout(Exception):
    """Waited admission_timeout_seconds in the "Ask to join" waiting room with no response."""


class AdmissionDenied(Exception):
    """Explicitly denied entry, or removed before actually getting into the call."""


class PageCrashed(Exception):
    """The Meet tab's renderer process crashed (Playwright 'crash' event) —
    most commonly an undersized /dev/shm. See shm_size in docker-compose.yml
    / scheduler/bot_launcher.py and --disable-dev-shm-usage below."""


class GracefulShutdown(Exception):
    """SIGTERM/SIGINT received (container stop, redeploy, etc). Handled as a
    deliberate leave — not a failure: whatever was recorded still gets
    uploaded and the meeting still ends up at `recorded`."""


# Chromium args shared by both modes: Docker's default /dev/shm (64MB) is way
# too small for Chromium and causes "Aw, Snap!" tab crashes. shm_size=2g on
# the container (docker-compose.yml / bot_launcher.py) is the real fix;
# --disable-dev-shm-usage (fall back to /tmp for shared memory) is a
# fallback in case a container ever runs without that set.
DISABLE_DEV_SHM_ARG = "--disable-dev-shm-usage"


def load_selectors() -> dict:
    return yaml.safe_load(SELECTORS_PATH.read_text())


BUILD_VERSION_PATH = Path(__file__).parent / "BUILD_VERSION"


def _bot_build_version() -> str:
    """BUILD_VERSION is written by bot/Dockerfile at image build time (there's
    no .git directory to pull a commit hash from in this checkout, so a UTC
    build timestamp is used instead — see the Dockerfile comment). Reading it
    at startup and logging it was added after a real test ran against a
    stale image because a code change hadn't actually been rebuilt yet, and
    that was only discovered after the fact from the bot's behavior, not
    from anything in the logs. "unknown" outside of the built image (e.g.
    running bot.py directly, or in tests)."""
    try:
        return BUILD_VERSION_PATH.read_text().strip() or "unknown"
    except Exception:
        return "unknown"


def _now_naive_utc() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _json_line(obj: dict) -> str:
    return json.dumps(obj) + "\n"


# --- audio recording -------------------------------------------------------


def start_ffmpeg(output_dir: Path, segment_seconds: int) -> subprocess.Popen:
    """Records meet_sink.monitor (whatever Chrome plays to the default sink)
    as 16kHz mono WAV, segmented so a crash never loses the whole call."""
    output_dir.mkdir(parents=True, exist_ok=True)
    pattern = str(output_dir / "audio_%05d.wav")
    cmd = [
        "ffmpeg", "-y",
        "-f", "pulse", "-i", "meet_sink.monitor",
        "-ac", "1", "-ar", "16000",
        "-f", "segment", "-segment_time", str(segment_seconds), "-reset_timestamps", "1",
        pattern,
    ]
    return subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def stop_ffmpeg(proc: subprocess.Popen | None) -> None:
    if proc is None or proc.poll() is not None:
        return
    proc.send_signal(signal.SIGINT)
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()


def _log_sink_inputs() -> None:
    """Diagnostic for silent-audio reports: lists every PulseAudio client
    stream and which sink it's connected to, so we can confirm (or rule out)
    Chromium's audio actually being routed to meet_sink at all."""
    try:
        result = subprocess.run(["pactl", "list", "sink-inputs"], capture_output=True, text=True, timeout=10)
        logger.info("pactl_sink_inputs", output=result.stdout or "(no sink-inputs — nothing is playing audio)")
    except Exception:
        logger.warning("pactl_sink_inputs_failed", exc_info=True)


def _check_audio_level(wav_path: Path) -> bool | None:
    """Runs ffmpeg's volumedetect filter on the in-progress recording and
    warns if it looks silent. Purely diagnostic — never raises; the file may
    still be mid-write, which volumedetect tolerates (reads whatever's
    there). Returns True if the audio looks like real speech (not silent),
    False if it looks silent, None if the check was inconclusive/failed —
    used by _monitor_call's end-of-call self-check to tell "nobody spoke"
    apart from "the speaking_indicator selector stopped working"."""
    try:
        result = subprocess.run(
            ["ffmpeg", "-i", str(wav_path), "-af", "volumedetect", "-f", "null", "-"],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except Exception:
        logger.warning("audio_level_check_failed", exc_info=True)
        return None

    mean_db = max_db = None
    for line in result.stderr.splitlines():
        if "mean_volume:" in line:
            mean_db = line.split("mean_volume:")[1].strip().split(" ")[0]
        elif "max_volume:" in line:
            max_db = line.split("max_volume:")[1].strip().split(" ")[0]

    if mean_db is None and max_db is None:
        logger.warning("audio_level_check_inconclusive", hint="volumedetect produced no readable output")
        return None

    logger.info("audio_level_check", mean_volume_db=mean_db, max_volume_db=max_db)
    try:
        if max_db is not None and float(max_db) < -50:
            logger.warning(
                "audio_appears_silent",
                mean_volume_db=mean_db,
                max_volume_db=max_db,
                hint="check pactl_sink_inputs above, meet_sink routing, and whether the tab/Meet audio is muted",
            )
            return False
        return True
    except ValueError:
        return None


# --- login mode --------------------------------------------------------------


def run_login_mode(settings: Settings) -> None:
    """Launches a PLAIN Chromium process — no Playwright/CDP, no automation
    flags — so Google doesn't block sign-in with "This browser or app may
    not be secure". Playwright's launch_persistent_context always attaches
    CDP and sets navigator.webdriver; Google's login flow actively detects
    that and blocks it, and there's no Playwright API to suppress it. So
    login mode bypasses Playwright's launch() entirely and runs the same
    Chromium binary Playwright downloaded, directly, as a normal subprocess
    pointed at the persistent profile dir — indistinguishable from a human
    double-clicking Chrome.

    Only relevant for BOT_JOIN_MODE=signed_in accounts — guest-mode bots
    never sign in, so there's no profile to log in ahead of time for them.
    """
    profile_path = os.environ["BOT_PROFILE_PATH"]

    with sync_playwright() as pw:
        chromium_path = pw.chromium.executable_path

    logger.info(
        "login_mode_starting", profile_path=profile_path, vnc_port=5900, chromium_path=chromium_path
    )

    proc = subprocess.Popen([
        chromium_path,
        f"--user-data-dir={profile_path}",
        "--no-sandbox",
        DISABLE_DEV_SHM_ARG,
        "--no-first-run",
        "--no-default-browser-check",
        "--window-size=1280,800",
        "--window-position=0,0",
        "https://accounts.google.com/",
    ])
    logger.info("login_mode_ready_connect_via_vnc")

    stop_event = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop_event.set())
    while not stop_event.is_set() and proc.poll() is None:
        time.sleep(1)

    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()

    logger.info("login_mode_stopped_profile_saved")


# --- run mode: join flow -----------------------------------------------------


def _detect_screen(page, selectors: dict) -> str:
    """Best-effort classification of what's currently on screen — purely for
    logging/diagnosis when selectors.yaml doesn't match the normal pre-join
    screen (block page, permission prompt, sign-in interstitial, still
    loading, ...). Checked in a fixed order; "unknown" if nothing recognized.

    in_call is checked FIRST, before any pre-join indicator: a real bug came
    from a pre-join selector still reporting present after the bot had
    already been admitted (stale DOM / race), which then fed a join-button
    click attempt that correctly found nothing and raised — a false
    failure *after* a real success. If we're already in the call, nothing
    else about the page matters."""
    try:
        url = page.url
    except Exception:
        url = ""
    if "accounts.google.com" in url:
        return "google_sign_in_interstitial"

    in_call_leave = selectors.get("in_call", {}).get("leave_call_button")
    if in_call_leave:
        try:
            if page.locator(in_call_leave).count() > 0:
                return "in_call"
        except Exception:
            pass

    try:
        if _join_button_locator(page).count() > 0:
            return "pre_join:join_button"
    except Exception:
        pass

    pre_join = selectors.get("pre_join", {})
    checks = [
        ("pre_join:name_input", pre_join.get("name_input")),
        ("pre_join:mic_toggle", pre_join.get("mic_toggle")),
    ]
    for label, sel in checks:
        if not sel:
            continue
        try:
            if page.locator(sel).count() > 0:
                return label
        except Exception:
            continue

    return "unknown"


def _wait_for_pre_join_ready(page, selectors: dict, timeout_seconds: int) -> str:
    """Waits for the page to settle (networkidle, best-effort — some pages
    never go fully idle) and then polls up to timeout_seconds for a
    recognizable screen. Always returns a label (never raises); logs it
    either way so "which screen did we actually land on" is visible instead
    of only ever seeing a stack of individual selector-not-found warnings."""
    try:
        page.wait_for_load_state("networkidle", timeout=min(timeout_seconds, 10) * 1000)
    except Exception:
        pass

    deadline = time.time() + timeout_seconds
    screen = _detect_screen(page, selectors)
    while screen == "unknown" and time.time() < deadline:
        time.sleep(0.5)
        screen = _detect_screen(page, selectors)

    logger.info("pre_join_screen_detected", screen=screen)
    return screen


class DebugContext:
    """Bundles what _save_debug_artifacts needs so call sites inside the join
    flow (detection, after-click, after-admission, periodic in-call) don't
    each have to thread settings/recordings_dir/meeting_id/console_logs
    through separately."""

    def __init__(self, settings: Settings, recordings_dir: Path, meeting_id: uuid.UUID, console_logs: list[str]):
        self.settings = settings
        self.recordings_dir = recordings_dir
        self.meeting_id = meeting_id
        self.console_logs = console_logs

    def capture(self, page, step: str) -> None:
        _save_debug_artifacts(self.settings, page, self.console_logs, self.recordings_dir, self.meeting_id, step)


def _save_debug_artifacts(
    settings: Settings, page, console_logs: list[str], recordings_dir: Path, meeting_id: uuid.UUID, step: str
) -> None:
    """Captures a full-page screenshot, the actual HTML, the page's
    URL/title, and whatever the browser console logged — on any join/monitor
    failure, AND at the moment the pre-join screen is first detected
    (before any click), so there's visibility into what was on screen
    *before* things went wrong, not just the aftermath. Each call site
    passes a distinct `step` (e.g. "pre_join_detected_pre_join_join_button",
    "RuntimeError") so multiple captures across one run land in their own
    subdirectory instead of overwriting each other. Uploaded to storage too,
    not just left in the bind-mounted recordings/ dir."""
    step_safe = step.replace(":", "_").replace("/", "_")
    debug_dir = recordings_dir / "debug" / step_safe
    debug_dir.mkdir(parents=True, exist_ok=True)

    try:
        page.screenshot(path=str(debug_dir / "screenshot.png"), full_page=True, timeout=10000)
    except Exception:
        logger.warning("debug_screenshot_failed", exc_info=True)

    try:
        (debug_dir / "page.html").write_text(page.content(), encoding="utf-8")
    except Exception:
        logger.warning("debug_html_capture_failed", exc_info=True)

    try:
        info = {"url": page.url, "title": page.title(), "step": step}
        (debug_dir / "page_info.json").write_text(json.dumps(info, indent=2), encoding="utf-8")
    except Exception:
        logger.warning("debug_page_info_failed", exc_info=True)

    try:
        (debug_dir / "console.log").write_text("\n".join(console_logs), encoding="utf-8")
    except Exception:
        logger.warning("debug_console_log_write_failed", exc_info=True)

    logger.info("debug_artifacts_saved", meeting_id=str(meeting_id), step=step, path=str(debug_dir))

    try:
        storage = StorageClient(settings)
        prefix = f"meetings/{meeting_id}/debug/{step_safe}/"
        for debug_file in debug_dir.iterdir():
            storage.upload_file(debug_file, prefix + debug_file.name)
        logger.info("debug_artifacts_uploaded", meeting_id=str(meeting_id), prefix=prefix)
    except Exception:
        logger.warning("debug_artifacts_upload_failed", exc_info=True)


def _toggle_media_off(page, selectors: dict, keyboard_key: str, name_re: re.Pattern, fallback_selector_key: str, label: str) -> None:
    """Primary: Meet's pre-join keyboard shortcut (Ctrl+D mic, Ctrl+E
    camera) — the actual toggle buttons proved unreliable to locate directly.
    Verifies via the toggle's own accessible name afterward ("Turn on X"
    means X is now off — success, since the button now offers to turn it
    back on); falls back to clicking the role-based toggle, then to
    selectors.yaml's CSS selector, if the shortcut didn't visibly take
    effect. Never raises — a failure here shouldn't abort the whole join."""
    try:
        page.keyboard.press(keyboard_key)
        time.sleep(0.5)
    except Exception:
        logger.warning("media_toggle_keyboard_shortcut_failed", label=label, exc_info=True)

    try:
        toggle = page.get_by_role("button", name=name_re)
        aria = toggle.first.get_attribute("aria-label", timeout=2000) or ""
        if aria.lower().startswith("turn on"):
            logger.info("media_toggle_off_confirmed", label=label, aria_label=aria)
            return
        toggle.first.click(timeout=3000)
        time.sleep(0.3)
        aria = toggle.first.get_attribute("aria-label", timeout=2000) or ""
        logger.info("media_toggle_off_via_click", label=label, aria_label=aria)
        return
    except Exception:
        logger.warning("media_toggle_role_based_failed", label=label, exc_info=True)

    sel = selectors["pre_join"].get(fallback_selector_key)
    if sel:
        try:
            page.click(sel, timeout=3000)
            logger.info("media_toggle_selector_fallback_clicked", label=label)
        except Exception:
            logger.warning("media_toggle_selector_fallback_failed", label=label, exc_info=True)


def _click_join_button(page) -> None:
    """Role-based locator shared with detection (_join_button_locator) —
    the exact mismatch that caused a real false failure (see _detect_screen's
    docstring). Waits for visible + enabled before each attempt, retries up
    to 3 times rather than giving up on the first transient failure."""
    locator = _join_button_locator(page)
    last_error: Exception | None = None

    for attempt in range(1, 4):
        try:
            locator.wait_for(state="visible", timeout=10000)
            if not locator.is_enabled():
                raise RuntimeError("join button found but not yet enabled")
            label = locator.first.inner_text().strip()
            locator.first.click(timeout=5000)
            logger.info("join_button_clicked", attempt=attempt, label=label)
            return
        except Exception as exc:
            last_error = exc
            logger.warning("join_button_click_attempt_failed", attempt=attempt, exc_info=True)
            time.sleep(1)

    raise RuntimeError(f"could not click a join button after 3 attempts: {last_error}")




def _join_meeting(
    page,
    meet_url: str,
    selectors: dict,
    settings: Settings,
    join_mode: str,
    crashed: threading.Event,
    debug_ctx: "DebugContext",
) -> None:
    page.goto(meet_url, wait_until="load")
    screen = _wait_for_pre_join_ready(page, selectors, settings.pre_join_wait_seconds)

    # Capture state at detection time too, not only on failure — this is
    # what would have shown the real pre-click DOM the first time this bug
    # happened, instead of only ever seeing the aftermath.
    debug_ctx.capture(page, step=f"pre_join_detected_{screen}")

    if screen == "in_call":
        logger.info("already_in_call_skipping_join_click")
        return

    name_input = selectors["pre_join"].get("name_input")
    if name_input:
        try:
            page.fill(name_input, settings.bot_display_name, timeout=5000 if join_mode == "guest" else 3000)
        except Exception:
            if join_mode == "guest":
                # In guest mode this field should be present — a failure here
                # means the bot likely joined with a default/anonymous name.
                logger.warning("guest_name_field_fill_failed_selectors_may_need_updating", exc_info=True)
            # signed_in: expected — Meet shows the signed-in account's own name instead.

    _toggle_media_off(page, selectors, "Control+d", MIC_TOGGLE_NAME_RE, "mic_toggle", "mic")
    _toggle_media_off(page, selectors, "Control+e", CAMERA_TOGGLE_NAME_RE, "camera_toggle", "camera")

    dismiss = selectors["pre_join"].get("dismiss_got_it")
    if dismiss:
        try:
            page.click(dismiss, timeout=2000)
        except Exception:
            pass

    _click_join_button(page)
    debug_ctx.capture(page, step="after_join_click")

    _wait_for_admission(page, selectors, settings.admission_timeout_seconds, crashed)
    debug_ctx.capture(page, step="after_admission")
    _log_sink_inputs()


def _is_admitted(page, selectors: dict, waiting: bool) -> bool:
    """A real test found "Leave call" is ALSO present during the waiting
    room — Meet shows the full control bar before admission, so that button
    alone caused a false "in_call" just 1s after clicking "Ask to join",
    while the user had not actually admitted the bot yet.

    A follow-up real test then found that requiring `participant_count >= 1`
    on top of that was STILL insufficient: the bot's own video tile renders
    (with a real data-participant-id) even while sitting in the waiting
    room, before any host admits it. Confirmed live: participant_count sat
    at 1 for the entire waiting period, then jumped to 3 the instant the
    host actually let the bot in. Requiring >= 2 means at least one *other*
    participant tile must be present, not just the bot's own — matching
    what was actually observed to distinguish the two states."""
    if waiting:
        return False
    leave_present = page.locator(selectors["in_call"]["leave_call_button"]).count() > 0
    participants_sel = selectors["in_call"].get("participants_list_item")
    participant_count = page.locator(participants_sel).count() if participants_sel else 0
    return leave_present and participant_count >= 2


def _wait_for_admission(page, selectors: dict, timeout_seconds: int, crashed: threading.Event) -> None:
    """External Gmail accounts normally go through Ask-to-join -> waiting
    room -> admitted. Explicitly tracks and logs the waiting_room -> in_call
    phase transition (with timestamps) instead of inferring admission from
    a single ambiguous signal — see _is_admitted's docstring for the bug
    this replaced. Raises AdmissionTimeout if never admitted in time.

    waiting_text (selectors.yaml) was found to never actually match Meet's
    real DOM across three separate live tests — grepped the actual
    captured HTML each time for "Asking to be let in" and every plausible
    variant, zero matches. That silently meant the waiting_room phase was
    never logged at all: the state machine went straight from "pending" to
    "in_call", making a stuck wait indistinguishable from a bot that
    hadn't even clicked join yet — confirmed live. Since Ask-to-join ->
    waiting room -> admitted is the only real path here (see module
    docstring), "not yet admitted" now means waiting_room directly, rather
    than depending on that unverified text to promote it out of "pending".
    waiting_text is still consulted opportunistically on top of that — if
    it ever does match a future Meet build, it's just as valid a signal
    that admission hasn't happened yet."""
    waiting_sel = selectors.get("admission", {}).get("waiting_text")
    denied_sel = selectors.get("admission", {}).get("denied_text")
    removed_sel = selectors.get("admission", {}).get("removed_text")

    deadline = time.time() + timeout_seconds
    phase = "pending"

    def _set_phase(new_phase: str) -> None:
        nonlocal phase
        if new_phase != phase:
            logger.info(
                "admission_phase_changed", from_phase=phase, to_phase=new_phase, ts=_now_naive_utc().isoformat()
            )
            phase = new_phase

    while time.time() < deadline:
        if crashed.is_set():
            raise PageCrashed("tab crashed while waiting for admission")
        if denied_sel and page.locator(denied_sel).count() > 0:
            raise AdmissionDenied("denied")
        if removed_sel and page.locator(removed_sel).count() > 0:
            raise AdmissionDenied("removed_before_admission")

        waiting = bool(waiting_sel) and page.locator(waiting_sel).count() > 0
        if _is_admitted(page, selectors, waiting):
            _set_phase("in_call")
            logger.info("admitted_to_call")
            return
        _set_phase("waiting_room")

        time.sleep(1)

    raise AdmissionTimeout(f"no admission after {timeout_seconds}s (last phase: {phase})")


def _post_consent_message(page, selectors: dict, settings: Settings) -> None:
    """Open the chat panel, wait for it to actually finish animating open
    (the textarea isn't interactable immediately on click — a real test
    showed this racing and failing), type + send, then close the panel
    again so it doesn't sit open for the rest of the call.

    The chat selectors themselves were confirmed correct against a real
    capture (see selectors.yaml) — the real bug was structural: the
    panel-close click lived in a second try block only reached after
    success, so any failure (e.g. the open animation not finishing within
    the old 5s timeout) left the "In-call messages" panel stuck open for
    the rest of the call, confirmed live via debug captures at 60s and
    120s. The close click now runs in `finally` whenever we got as far as
    opening the panel, regardless of what happened after."""
    chat_toggle = selectors["in_call"]["chat_panel_toggle"]
    chat_input = selectors["in_call"]["chat_message_input"]
    opened = False
    try:
        page.click(chat_toggle, timeout=5000)
        opened = True
        time.sleep(1)  # let the panel's open animation settle before probing it
        page.wait_for_selector(chat_input, state="visible", timeout=8000)
        page.fill(chat_input, settings.bot_consent_message, timeout=5000)
        page.click(selectors["in_call"]["chat_send_button"], timeout=5000)
        logger.info("consent_message_posted")
    except Exception:
        logger.warning("consent_message_post_failed_selectors_may_need_updating", exc_info=True)
    finally:
        if opened:
            try:
                page.click(chat_toggle, timeout=3000)
            except Exception:
                logger.warning("chat_panel_close_failed", exc_info=True)


PIN_LABEL_RE = re.compile(r"^Pin (.+) to your main screen$")


def _collect_participants(page, selectors: dict) -> dict[str, str]:
    """Maps data-participant-id -> display name for every *other*
    participant tile, excluding the bot's own. Confirmed against a real
    3-person capture: every non-self tile carries a descendant matching
    selectors.yaml's `participant_name_label`, reading "Pin {name} to your
    main screen"; the bot's own tile has no such descendant at all (it had
    "Backgrounds and effects" there instead) — that absence is what
    excludes it, without needing to know the bot's own name/id ahead of
    time. Never raises; returns {} on any locator failure."""
    tiles_sel = selectors["in_call"].get("participants_list_item")
    name_sel = selectors["in_call"].get("participant_name_label")
    if not tiles_sel or not name_sel:
        return {}

    participants: dict[str, str] = {}
    try:
        tiles = page.locator(tiles_sel)
        count = tiles.count()
    except Exception:
        return {}

    for i in range(count):
        try:
            tile = tiles.nth(i)
            pid = tile.get_attribute("data-participant-id", timeout=1000)
            if not pid:
                continue
            name_label = tile.locator(name_sel)
            if name_label.count() == 0:
                continue  # no "Pin ..." control on this tile — it's the bot's own
            aria = name_label.first.get_attribute("aria-label", timeout=1000) or ""
            match = PIN_LABEL_RE.match(aria)
            name = match.group(1) if match else aria
            if name:
                participants[pid] = name
        except Exception:
            continue

    return participants


def _detect_speaking(page, selectors: dict) -> set[str]:
    """Returns the data-participant-ids currently showing Meet's
    active-speaker indicator — a per-tile `.DYfzY` avatar ring that Meet's
    own CSS pulses through 5 scale steps (5 modifier classes) as that
    participant's live mic amplitude rises; see selectors.yaml's
    speaking_indicator comment for the real page/CSS this was pulled from.
    Returns an empty set — a safe no-op, not a guess — whenever
    `in_call.speaking_indicator` is unset."""
    sel = selectors["in_call"].get("speaking_indicator")
    if not sel:
        return set()

    tiles_sel = selectors["in_call"].get("participants_list_item")
    speaking: set[str] = set()
    try:
        tiles = page.locator(tiles_sel)
        count = tiles.count()
    except Exception:
        return set()

    for i in range(count):
        try:
            tile = tiles.nth(i)
            if tile.locator(sel).count() > 0:
                pid = tile.get_attribute("data-participant-id", timeout=1000)
                if pid:
                    speaking.add(pid)
        except Exception:
            continue

    return speaking


class _SpeakingDebouncer:
    """The raw per-tile speaking_indicator reading flickers heavily —
    confirmed on a real call: ordinary speech pauses produced dozens of
    sub-second start/stop pairs. This confirms a speaking_start only once
    the raw signal has held continuously for `min_burst_seconds` (shorter
    flickers are dropped — never emitted at all), and defers a
    speaking_stop until the raw signal has been gone for `gap_seconds`
    (a resumption within that window merges into the same speaking period,
    no stop/start pair emitted for the gap). Emitted events are backdated
    to when the raw transition actually happened, not when the debounce
    window confirmed it, so timestamps reflect real speech timing."""

    def __init__(self, min_burst_seconds: float, gap_seconds: float):
        self._min_burst = min_burst_seconds
        self._gap = gap_seconds
        self._confirmed: set[str] = set()
        self._burst_started_at: dict[str, float] = {}
        self._gap_started_at: dict[str, float] = {}

    def update(self, raw_active: set[str], now: float) -> list[tuple[str, str, float]]:
        """raw_active: participant_ids whose raw indicator is on right now.
        Returns [(participant_id, event, at), ...] for every CONFIRMED
        transition this tick — `at` is the backdated real transition time."""
        events: list[tuple[str, str, float]] = []
        tracked = raw_active | self._burst_started_at.keys() | self._gap_started_at.keys() | self._confirmed

        for pid in tracked:
            if pid in raw_active:
                self._gap_started_at.pop(pid, None)  # resumed inside the gap window — merge, no stop
                if pid not in self._confirmed:
                    started_at = self._burst_started_at.setdefault(pid, now)
                    if now - started_at >= self._min_burst:
                        self._confirmed.add(pid)
                        self._burst_started_at.pop(pid, None)
                        events.append((pid, "speaking_start", started_at))
            else:
                self._burst_started_at.pop(pid, None)  # burst never sustained long enough — drop it
                if pid in self._confirmed:
                    stopped_at = self._gap_started_at.setdefault(pid, now)
                    if now - stopped_at >= self._gap:
                        self._confirmed.discard(pid)
                        self._gap_started_at.pop(pid, None)
                        events.append((pid, "speaking_stop", stopped_at))

        return events

    def force_stop(self, pid: str, at: float) -> tuple[str, str, float] | None:
        """A participant's tile disappeared (they left) — don't leave them
        dangling as confirmed-speaking forever with no matching stop. A
        no-op (returns None) if they were never actually confirmed."""
        was_confirmed = pid in self._confirmed
        self._confirmed.discard(pid)
        self._burst_started_at.pop(pid, None)
        self._gap_started_at.pop(pid, None)
        return (pid, "speaking_stop", at) if was_confirmed else None


def _monitor_call(
    page,
    selectors: dict,
    settings: Settings,
    speaker_log_path: Path,
    crashed: threading.Event,
    shutdown_requested: threading.Event,
    debug_ctx: "DebugContext",
    recordings_dir: Path,
    recording_start_time: float,
) -> tuple[str, bool]:
    """Logs participant join/leave and debounced active-speaker events to
    speaker_log_path (only on confirmed change — never one line per poll
    tick) and returns (reason, speaking_indicator_suspect):
    reason is removed_or_left | alone_timeout | max_duration_reached.
    speaking_indicator_suspect is True when the end-of-call self-check
    found real speech in the recording but zero confirmed speaking_start
    events the whole call — a strong sign selectors.yaml's
    speaking_indicator stopped matching (e.g. a Meet UI change), which
    would otherwise fail silently (empty-but-valid-looking speaker data).

    Raises PageCrashed if the tab's renderer process crashes mid-call —
    callers must not treat that as a normal leave (it would otherwise look
    like "removed_or_left" since the leave button also disappears). Raises
    GracefulShutdown on SIGTERM/SIGINT, after attempting to click "Leave
    call" so the host sees a normal departure rather than the bot just
    vanishing. Neither exception path runs the self-check (it returns a
    tuple, not raises) — it's only meaningful for a call that actually ran
    its course.

    Every event carries both "t" (seconds since the audio recording
    started — the same clock as the WAV filenames/segments, for Phase 3 to
    align against) and "ts" (wall-clock, for human debugging). The bot's
    own tile is excluded from every event — see _collect_participants.
    Raw (pre-debounce) speaking flicker is also logged to a sibling
    speakers_raw.jsonl, useful for re-tuning the debounce thresholds.

    Also runs a one-time audio level check ~30s in (warns if the recording
    looks silent) and, when settings.bot_debug_capture is enabled, saves
    debug artifacts every 60s so in-call selectors (and audio routing) can
    be diagnosed against the real DOM/recording without needing a failure
    to trigger a capture. Off by default once selectors are stable."""
    leave_button = selectors["in_call"]["leave_call_button"]
    raw_log_path = speaker_log_path.with_name("speakers_raw.jsonl")

    alone_since: float | None = None
    start_time = time.time()
    poll_seconds = settings.speaker_poll_interval_ms / 1000
    audio_checked = False
    audio_showed_speech: bool | None = None
    any_confirmed_speaking = False
    last_debug_capture = start_time
    known_participants: dict[str, str] = {}
    raw_speaking_prev: set[str] = set()
    debouncer = _SpeakingDebouncer(settings.speaking_min_burst_seconds, settings.speaking_debounce_gap_seconds)

    def _at(event: dict, at: float) -> dict:
        return {"t": round(at - recording_start_time, 2), "ts": _now_naive_utc().isoformat(), **event}

    def _emit(log_file, event: dict, at: float | None = None) -> None:
        log_file.write(_json_line(_at(event, at if at is not None else time.time())))
        log_file.flush()

    def _self_check() -> bool:
        suspect = bool(audio_showed_speech) and not any_confirmed_speaking
        if suspect:
            logger.warning(
                "speaking_indicator_selector_likely_broken",
                hint="Meet UI changed? audio_level_check showed real speech but zero speaking_start events were logged this call",
            )
        return suspect

    with speaker_log_path.open("a") as log_file, raw_log_path.open("a") as raw_log_file:
        while True:
            if crashed.is_set():
                raise PageCrashed("tab crashed during the call")

            if shutdown_requested.is_set():
                logger.info("graceful_shutdown_leaving_call")
                try:
                    page.click(leave_button, timeout=3000)
                except Exception:
                    pass
                raise GracefulShutdown("shutdown requested")

            if page.locator(leave_button).count() == 0:
                return "removed_or_left", _self_check()

            elapsed = time.time() - start_time
            if elapsed > settings.max_meeting_duration_seconds:
                return "max_duration_reached", _self_check()

            if not audio_checked and elapsed > 30:
                audio_checked = True
                wav_files = sorted(recordings_dir.glob("audio_*.wav"))
                if wav_files:
                    audio_showed_speech = _check_audio_level(wav_files[0])

            if settings.bot_debug_capture and time.time() - last_debug_capture > 60:
                last_debug_capture = time.time()
                debug_ctx.capture(page, step=f"in_call_{int(elapsed)}s")

            now = time.time()
            participants = _collect_participants(page, selectors)

            for pid, name in participants.items():
                if pid not in known_participants:
                    _emit(log_file, {"event": "participant_joined", "participant_id": pid, "name": name}, now)
            for pid, name in known_participants.items():
                if pid not in participants:
                    forced = debouncer.force_stop(pid, now)
                    if forced:
                        _emit(log_file, {"event": "speaking_stop", "participant_id": pid, "name": name}, now)
                    _emit(log_file, {"event": "participant_left", "participant_id": pid, "name": name}, now)
            known_participants = participants

            raw_speaking = _detect_speaking(page, selectors)
            for pid in raw_speaking - raw_speaking_prev:
                _emit(raw_log_file, {"event": "speaking_start", "participant_id": pid, "name": known_participants.get(pid, "")}, now)
            for pid in raw_speaking_prev - raw_speaking:
                _emit(raw_log_file, {"event": "speaking_stop", "participant_id": pid, "name": known_participants.get(pid, "")}, now)
            raw_speaking_prev = raw_speaking

            for pid, event, at in debouncer.update(raw_speaking, now):
                _emit(log_file, {"event": event, "participant_id": pid, "name": known_participants.get(pid, "")}, at)
                if event == "speaking_start":
                    any_confirmed_speaking = True

            if not known_participants:
                alone_since = alone_since or time.time()
                if time.time() - alone_since > settings.alone_timeout_seconds:
                    return "alone_timeout", _self_check()
            else:
                alone_since = None

            time.sleep(poll_seconds)


# --- run mode: heartbeat + storage -------------------------------------------


def _heartbeat_loop(account_id: uuid.UUID, interval_seconds: int, stop_event: threading.Event) -> None:
    db = SessionLocal()
    try:
        while not stop_event.wait(interval_seconds):
            heartbeat(db, account_id)
            db.commit()
    finally:
        db.close()


def _upload_artifacts(
    settings: Settings, meeting_id: uuid.UUID, recordings_dir: Path, speaker_log_path: Path
) -> tuple[str, str]:
    storage = StorageClient(settings)
    audio_prefix = f"meetings/{meeting_id}/audio/"
    for wav_file in sorted(recordings_dir.glob("audio_*.wav")):
        storage.upload_file(wav_file, audio_prefix + wav_file.name)

    speaker_events_key = f"meetings/{meeting_id}/speakers.jsonl"
    if speaker_log_path.exists():
        storage.upload_file(speaker_log_path, speaker_events_key)

    return audio_prefix, speaker_events_key


def _fail_and_cleanup(db, meeting: Meeting, status: MeetingStatus, error: str, ffmpeg_proc, stop_heartbeat, account_id) -> None:
    meeting.status = status
    meeting.error = error
    db.commit()
    stop_ffmpeg(ffmpeg_proc)
    stop_heartbeat.set()
    if account_id is not None:
        release_account(db, account_id)
        db.commit()


def _finalize_and_release(
    db,
    meeting: Meeting,
    meeting_id: uuid.UUID,
    settings: Settings,
    recordings_dir: Path,
    speaker_log_path: Path,
    ffmpeg_proc,
    stop_heartbeat: threading.Event,
    account_id: uuid.UUID,
) -> None:
    """The "it ended reasonably" path — a normal leave OR a graceful
    shutdown. Either way: finalize the recording, upload whatever exists,
    and release the account immediately. Only the upload step can still
    fail this into `failed`."""
    stop_ffmpeg(ffmpeg_proc)
    meeting.ended_at = _now_naive_utc()

    try:
        audio_key, speaker_events_key = _upload_artifacts(settings, meeting_id, recordings_dir, speaker_log_path)
        meeting.audio_key = audio_key
        meeting.speaker_events_key = speaker_events_key
        meeting.status = MeetingStatus.RECORDED
        db.commit()
        enqueue_processing(meeting_id)
    except Exception as exc:
        logger.error("artifact_upload_failed", meeting_id=str(meeting_id), exc_info=True)
        meeting.status = MeetingStatus.FAILED
        meeting.error = f"upload failed: {exc}"
        db.commit()

    stop_heartbeat.set()
    release_account(db, account_id)
    db.commit()


# --- run mode: main flow ------------------------------------------------------


def run_bot_mode(settings: Settings) -> None:
    meeting_id = uuid.UUID(os.environ["MEETING_ID"])
    selectors = load_selectors()
    db = SessionLocal()

    meeting = db.get(Meeting, meeting_id)
    if meeting is None:
        logger.error("meeting_not_found", meeting_id=str(meeting_id))
        return

    # The pool still gates concurrency (one container per meeting) in guest
    # mode — only the browser profile below differs.
    account = checkout_or_mark_unavailable(db, meeting)
    db.commit()
    if account is None:
        logger.error("no_bot_account_available", meeting_id=str(meeting_id))
        return

    meeting.status = MeetingStatus.JOINING
    db.commit()

    join_mode = settings.bot_join_mode
    temp_profile_dir: str | None = None
    if join_mode == "guest":
        temp_profile_dir = tempfile.mkdtemp(prefix="bot-guest-")
        profile_dir = temp_profile_dir
    else:
        profile_dir = account.profile_path

    logger.info(
        "bot_starting", meeting_id=str(meeting_id), account_email=account.email, join_mode=join_mode
    )

    stop_heartbeat = threading.Event()
    threading.Thread(
        target=_heartbeat_loop,
        args=(account.id, settings.bot_heartbeat_interval_seconds, stop_heartbeat),
        daemon=True,
        name="heartbeat",
    ).start()

    shutdown_requested = threading.Event()

    def _on_signal(signum, _frame) -> None:
        logger.warning("shutdown_signal_received", signal=signum)
        shutdown_requested.set()

    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)

    recordings_dir = Path(settings.recordings_dir) / str(meeting_id)
    speaker_log_path = recordings_dir / "speakers.jsonl"
    recordings_dir.mkdir(parents=True, exist_ok=True)
    ffmpeg_proc = start_ffmpeg(recordings_dir, settings.audio_segment_seconds)
    recording_start_time = time.time()

    crashed = threading.Event()

    def _on_crash(_crashed_page) -> None:
        logger.error(
            "page_crashed",
            meeting_id=str(meeting_id),
            hint="renderer process crashed — check /dev/shm size (shm_size) on this container",
        )
        crashed.set()

    try:
        try:
            with sync_playwright() as pw:
                context = pw.chromium.launch_persistent_context(
                    profile_dir,
                    headless=False,
                    locale="en-US",
                    args=[
                        "--start-maximized",
                        # --use-fake-ui-for-media-stream auto-accepts the
                        # getUserMedia prompt; --use-fake-device-for-media-stream
                        # provides the actual fake camera/mic *devices* so
                        # getUserMedia succeeds at all in a container with no
                        # real hardware — missing this was a likely culprit
                        # behind the pre-join screen never settling.
                        "--use-fake-ui-for-media-stream",
                        "--use-fake-device-for-media-stream",
                        # A real test recorded complete silence despite
                        # audio being spoken. The console had already logged
                        # "The AudioContext was not allowed to start. It
                        # must be resumed (or created) after a user gesture"
                        # — Chrome's autoplay policy blocking the page's own
                        # AudioContext (so Meet never actually plays
                        # incoming audio, meaning there's genuinely nothing
                        # for ffmpeg to capture from meet_sink). This
                        # disables that restriction outright.
                        "--autoplay-policy=no-user-gesture-required",
                        "--lang=en-US",
                        DISABLE_DEV_SHM_ARG,
                        # Reduces automation fingerprinting — relevant in both
                        # modes, but especially for BOT_JOIN_MODE=signed_in,
                        # where an already-authenticated Google account is at
                        # stake if it gets flagged as bot-driven.
                        "--disable-blink-features=AutomationControlled",
                    ],
                    ignore_default_args=["--enable-automation"],
                    user_agent=settings.bot_user_agent,
                    permissions=["microphone", "camera"],
                    viewport={"width": 1280, "height": 800},
                )
                # Belt-and-suspenders on top of the context-level `permissions`
                # above: grant explicitly for meet.google.com specifically, so
                # a permission prompt can't block the pre-join screen.
                context.grant_permissions(["microphone", "camera"], origin="https://meet.google.com")

                page = context.new_page()
                page.on("crash", _on_crash)
                console_logs: list[str] = []
                page.on("console", lambda msg: console_logs.append(f"[{msg.type}] {msg.text}"))
                debug_ctx = DebugContext(settings, recordings_dir, meeting_id, console_logs)

                try:
                    _join_meeting(page, meeting.meet_url, selectors, settings, join_mode, crashed, debug_ctx)
                    meeting.status = MeetingStatus.IN_CALL
                    meeting.started_at = _now_naive_utc()
                    db.commit()

                    _post_consent_message(page, selectors, settings)
                    reason, speaking_indicator_suspect = _monitor_call(
                        page,
                        selectors,
                        settings,
                        speaker_log_path,
                        crashed,
                        shutdown_requested,
                        debug_ctx,
                        recordings_dir,
                        recording_start_time,
                    )
                    meeting.speaking_indicator_suspect = speaking_indicator_suspect
                    logger.info("leaving_meeting", reason=reason)
                except GracefulShutdown:
                    raise
                except Exception as exc:
                    debug_ctx.capture(page, step=type(exc).__name__)
                    raise
                finally:
                    context.close()

        except AdmissionTimeout as exc:
            logger.warning("admission_timeout", meeting_id=str(meeting_id))
            _fail_and_cleanup(db, meeting, MeetingStatus.BOT_REJECTED, str(exc), ffmpeg_proc, stop_heartbeat, account.id)
            return
        except AdmissionDenied as exc:
            logger.warning("admission_denied", meeting_id=str(meeting_id))
            _fail_and_cleanup(db, meeting, MeetingStatus.BOT_REJECTED, str(exc), ffmpeg_proc, stop_heartbeat, account.id)
            return
        except PageCrashed as exc:
            logger.error("page_crashed_failing_meeting", meeting_id=str(meeting_id))
            _fail_and_cleanup(db, meeting, MeetingStatus.FAILED, str(exc), ffmpeg_proc, stop_heartbeat, account.id)
            return
        except GracefulShutdown:
            logger.info("graceful_shutdown_finalizing", meeting_id=str(meeting_id))
            _finalize_and_release(
                db, meeting, meeting_id, settings, recordings_dir, speaker_log_path, ffmpeg_proc, stop_heartbeat, account.id
            )
            return
        except Exception as exc:
            logger.error("bot_run_failed", meeting_id=str(meeting_id), exc_info=True)
            _fail_and_cleanup(db, meeting, MeetingStatus.FAILED, str(exc), ffmpeg_proc, stop_heartbeat, account.id)
            return

        _finalize_and_release(
            db, meeting, meeting_id, settings, recordings_dir, speaker_log_path, ffmpeg_proc, stop_heartbeat, account.id
        )
    finally:
        if temp_profile_dir is not None:
            shutil.rmtree(temp_profile_dir, ignore_errors=True)

    db.close()
    logger.info("bot_finished", meeting_id=str(meeting_id))


def main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    logger.info("bot_code_version", build_version=_bot_build_version())
    mode = os.environ.get("BOT_MODE", "run")

    if mode == "login":
        run_login_mode(settings)
    elif mode == "run":
        run_bot_mode(settings)
    else:
        print(f"unknown BOT_MODE: {mode}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
