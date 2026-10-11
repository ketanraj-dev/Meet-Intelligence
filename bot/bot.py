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

BOT_JOIN_MODE (run mode only), default "guest":
  - guest: fresh temporary profile, no Google sign-in. Types the display
    name into Meet's "Your name" field on the pre-join screen. Permitted
    here because the Workspace's Meet "Domain" setting allows non-signed-in
    users to join.
  - signed_in: today's original behavior — joins using the checked-out bot
    account's saved, persistent Chrome profile.
Either way the bot pool still gates concurrency (one container per
meeting); only the browser profile differs.
"""
import json
import os
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
    loading, ...). Checked in a fixed order; "unknown" if nothing recognized."""
    try:
        url = page.url
    except Exception:
        url = ""
    if "accounts.google.com" in url:
        return "google_sign_in_interstitial"

    pre_join = selectors.get("pre_join", {})
    checks = [
        ("pre_join:name_input", pre_join.get("name_input")),
        ("pre_join:ask_to_join_button", pre_join.get("ask_to_join_button")),
        ("pre_join:join_now_button", pre_join.get("join_now_button")),
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

    in_call_leave = selectors.get("in_call", {}).get("leave_call_button")
    if in_call_leave:
        try:
            if page.locator(in_call_leave).count() > 0:
                return "in_call"
        except Exception:
            pass

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


def _save_debug_artifacts(
    settings: Settings, page, console_logs: list[str], recordings_dir: Path, meeting_id: uuid.UUID, step: str
) -> None:
    """Called on any join/monitor failure so there's something to look at
    besides "selector not found": a full-page screenshot, the actual HTML,
    the page's URL/title, and whatever the browser console logged. Uploaded
    to storage too, not just left in the bind-mounted recordings/ dir."""
    debug_dir = recordings_dir / "debug"
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

    logger.error("debug_artifacts_saved", meeting_id=str(meeting_id), step=step, path=str(debug_dir))

    try:
        storage = StorageClient(settings)
        prefix = f"meetings/{meeting_id}/debug/"
        for debug_file in debug_dir.iterdir():
            storage.upload_file(debug_file, prefix + debug_file.name)
        logger.info("debug_artifacts_uploaded", meeting_id=str(meeting_id), prefix=prefix)
    except Exception:
        logger.warning("debug_artifacts_upload_failed", exc_info=True)


def _join_meeting(
    page, meet_url: str, selectors: dict, settings: Settings, join_mode: str, crashed: threading.Event
) -> None:
    page.goto(meet_url, wait_until="load")
    _wait_for_pre_join_ready(page, selectors, settings.pre_join_wait_seconds)

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

    for toggle_key in ("mic_toggle", "camera_toggle"):
        sel = selectors["pre_join"].get(toggle_key)
        if sel:
            try:
                page.click(sel, timeout=3000)
            except Exception:
                logger.warning("pre_join_toggle_click_failed", toggle=toggle_key)

    dismiss = selectors["pre_join"].get("dismiss_got_it")
    if dismiss:
        try:
            page.click(dismiss, timeout=2000)
        except Exception:
            pass

    join_clicked = False
    for button_key in ("ask_to_join_button", "join_now_button"):
        sel = selectors["pre_join"].get(button_key)
        if not sel:
            continue
        try:
            page.click(sel, timeout=5000)
            join_clicked = True
            logger.info("join_button_clicked", button=button_key)
            break
        except Exception:
            continue

    if not join_clicked:
        raise RuntimeError("could not find a join button — selectors.yaml needs updating")

    _wait_for_admission(page, selectors, settings.admission_timeout_seconds, crashed)


def _wait_for_admission(page, selectors: dict, timeout_seconds: int, crashed: threading.Event) -> None:
    """External Gmail accounts normally go through Ask-to-join -> waiting
    room -> admitted. Polls for either success (in-call UI appears) or an
    explicit denial; raises AdmissionTimeout if neither happens in time."""
    in_call_sel = selectors["in_call"]["leave_call_button"]
    denied_sel = selectors.get("admission", {}).get("denied_text")
    removed_sel = selectors.get("admission", {}).get("removed_text")

    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        if crashed.is_set():
            raise PageCrashed("tab crashed while waiting for admission")
        if page.locator(in_call_sel).count() > 0:
            logger.info("admitted_to_call")
            return
        if denied_sel and page.locator(denied_sel).count() > 0:
            raise AdmissionDenied("denied")
        if removed_sel and page.locator(removed_sel).count() > 0:
            raise AdmissionDenied("removed_before_admission")
        time.sleep(1)

    raise AdmissionTimeout(f"no admission after {timeout_seconds}s")


def _post_consent_message(page, selectors: dict, settings: Settings) -> None:
    try:
        page.click(selectors["in_call"]["chat_panel_toggle"], timeout=5000)
        page.fill(selectors["in_call"]["chat_message_input"], settings.bot_consent_message, timeout=5000)
        page.click(selectors["in_call"]["chat_send_button"], timeout=5000)
        logger.info("consent_message_posted")
    except Exception:
        logger.warning("consent_message_post_failed_selectors_may_need_updating", exc_info=True)


def _monitor_call(
    page,
    selectors: dict,
    settings: Settings,
    speaker_log_path: Path,
    crashed: threading.Event,
    shutdown_requested: threading.Event,
) -> str:
    """Logs participant-count snapshots to speaker_log_path and returns the
    reason the bot left: removed_or_left | alone_timeout | max_duration_reached.
    Raises PageCrashed if the tab's renderer process crashes mid-call —
    callers must not treat that as a normal leave (it would otherwise look
    like "removed_or_left" since the leave button also disappears). Raises
    GracefulShutdown on SIGTERM/SIGINT, after attempting to click "Leave
    call" so the host sees a normal departure rather than the bot just
    vanishing."""
    leave_button = selectors["in_call"]["leave_call_button"]
    participants_sel = selectors["in_call"].get("participants_list_item")

    alone_since: float | None = None
    start_time = time.time()
    poll_seconds = settings.speaker_poll_interval_ms / 1000

    with speaker_log_path.open("a") as log_file:
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
                return "removed_or_left"

            if time.time() - start_time > settings.max_meeting_duration_seconds:
                return "max_duration_reached"

            if participants_sel:
                count = page.locator(participants_sel).count()
                log_file.write(_json_line({"ts": _now_naive_utc().isoformat(), "event": "participant_count", "count": count}))
                log_file.flush()

                if count <= 1:
                    alone_since = alone_since or time.time()
                    if time.time() - alone_since > settings.alone_timeout_seconds:
                        return "alone_timeout"
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

                try:
                    _join_meeting(page, meeting.meet_url, selectors, settings, join_mode, crashed)
                    meeting.status = MeetingStatus.IN_CALL
                    meeting.started_at = _now_naive_utc()
                    db.commit()

                    _post_consent_message(page, selectors, settings)
                    reason = _monitor_call(
                        page, selectors, settings, speaker_log_path, crashed, shutdown_requested
                    )
                    logger.info("leaving_meeting", reason=reason)
                except GracefulShutdown:
                    raise
                except Exception as exc:
                    _save_debug_artifacts(
                        settings, page, console_logs, recordings_dir, meeting_id, step=type(exc).__name__
                    )
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
