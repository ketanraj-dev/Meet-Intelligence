"""Centralized app configuration, read from environment variables / .env."""
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_env: str = "development"
    log_level: str = "INFO"

    database_url: str
    redis_url: str = "redis://localhost:6379/0"

    minio_endpoint: str = "localhost:9000"
    minio_access_key: str = "accessKey1"
    minio_secret_key: str = "verySecretKey1"
    minio_bucket: str = "meet-intelligence"
    minio_secure: bool = False

    # Phase 3 — diarization (gated HF models; terms must be accepted on the
    # HF model pages for the token's account) and Phase 4 — transcription
    hf_token: str | None = None
    diarization_model: str = "pyannote/speaker-diarization-3.1"
    # "cuda" switches to GPU later — a config change, not a rewrite. CPU
    # default for now per the current testing setup.
    diarization_device: str = "cpu"
    # pyannote's own default (32) OOM-killed a worker container processing
    # a single ~2-minute test meeting — confirmed live, memory spiked from
    # ~0.8GB to 5GB+ within 2 seconds and the container was SIGKILLed.
    # batch_size=1 fixed it (completed the same meeting in ~268s, no
    # crash) — slower per job, but this is CPU/"slow is fine" per the
    # current testing setup; GPU hosts with real memory headroom can raise
    # this via config later without a code change.
    diarization_batch_size: int = 1
    # Consecutive same-speaker turns closer together than this are merged
    # into one turn before Phase 4 transcribes them.
    diarization_merge_gap_seconds: float = 0.5
    # A turn longer than this gets split (at the quietest ~250ms+ silence
    # point found, else a hard cut) — keeps individual Phase 4 ASR calls a
    # bounded size.
    diarization_max_turn_seconds: float = 30.0
    diarization_min_silence_seconds: float = 0.25
    # A turn shorter than this is noise, not a meaningful
    # separately-attributable utterance — absorbed into an adjacent turn
    # instead of being written to speaker_turns on its own. Confirmed live:
    # without this, a real meeting's output had 0.02-0.6s "Unknown speaker"
    # fragments wedged between real turns, pure clustering noise.
    diarization_min_turn_seconds: float = 1.0
    diarization_silence_threshold_db: float = -40.0

    # Phase 4 — transcription. Runs in its own `asr` container/queue, not
    # `worker` — Phase 3 found that loading just ONE heavy CPU model with
    # the wrong batch size OOM-killed a container with 7.75GB available;
    # loading pyannote (diarization) and Indic-Transcribe/Whisper
    # (transcription) in the same process at once is exactly the scenario
    # to avoid rather than discover the hard way again.
    transcription_queue_name: str = "meet-intelligence:transcription"
    transcription_device: str = "cpu"  # "cuda" later, config switch only
    # Diarization (worker) stays pinned to 1 thread (pyannote's own OOM fix
    # from Phase 3 — unrelated model, unrelated container, left alone).
    # This is the ASR container's own thread cap — confirmed live it was
    # only using 1 of 10 available Docker Desktop cores (hardcoded
    # OMP_NUM_THREADS=1/MKL_NUM_THREADS=1 copied over from worker/Dockerfile
    # without rethinking it for this container's own memory headroom).
    # Default 8 leaves 2 cores for macOS + the other containers; set via
    # docker-compose.yml's asr service (OMP_NUM_THREADS/MKL_NUM_THREADS env
    # vars, read by numpy/BLAS) and torch.set_num_threads()/faster-whisper's
    # cpu_threads= (read here, by this field) — both need setting since
    # they're separate thread pools.
    asr_num_threads: int = 8
    # bodhan-ai/indic-transcribe-flex by default; set to the -core variant
    # (or any compatible model id) via this setting, no code change.
    indic_transcribe_model_id: str = "bodhan-ai/indic-transcribe-flex"
    # "mixed": native words in native script, English words/numerals in
    # Latin — matches MODES["mixed"] in the model repo's own
    # indic_transcribe.py (verified live against the real source on first
    # run, see asr/transcription.py's IndicTranscribeASR docstring).
    indic_transcribe_mode: str = "mixed"
    # The real 27-language list, read directly from the model repo's own
    # indic_transcribe.py LANGUAGES tuple (confirmed live on first run —
    # this replaces an earlier unverified guess). Anything not in this set
    # routes to WhisperASR instead.
    indic_transcribe_languages: list[str] = [
        "as", "bgc", "bhb", "bho", "bn", "brx", "doi", "en", "gu", "hi",
        "hne", "kn", "kok", "ks", "mai", "ml", "mni", "mr", "ne", "or",
        "pa", "sa", "sat", "sd", "ta", "te", "ur",
    ]
    # faster-whisper fallback — for languages Indic-Transcribe doesn't
    # cover, and universally for language ID (faster-whisper's language
    # detection is well-documented/stable; reusing it avoids guessing at
    # Indic-Transcribe's own LID API, which isn't part of the task spec
    # anyway). Small model for CPU testing; large-v3 on GPU later is a
    # config change, not a rewrite.
    whisper_model_size: str = "small"
    whisper_compute_type: str = "int8"  # faster-whisper's CPU-friendly default
    # The Indic fallback when a turn's LID doesn't come back confidently
    # "en" (see en_lid_confidence_threshold below) — Indic-Transcribe's
    # mode="mixed" renders this language in native script with English
    # loanwords/numerals in Latin, which is the right call for code-mixed
    # speech. Restricted to meeting.expected_languages when set (see
    # asr/transcription.py's decide_turn_language).
    default_language: str = "hi"
    # LID is unreliable at telling Hindi apart from Urdu/Maithili/Bhojpuri,
    # and more generally at fine-grained Indic-language discrimination on
    # short code-mixed clips — so the per-turn decision only ever trusts
    # LID for a binary "is this actually English" call, not for picking
    # between Indic languages. Only "en" at or above this confidence is
    # used as-is; anything else (including a confident non-English,
    # non-"en" result) falls back to default_language. Lower this if real
    # English turns are getting misrouted to Hindi; raise it if Hindi
    # turns are getting wrongly treated as English.
    en_lid_confidence_threshold: float = 0.85
    # A turn whose whole-clip RMS energy (common/audio.py's wav_energy_db)
    # is below this is treated as silence/noise and skipped — no LID, no
    # ASR call, written with text="" and low_confidence=True rather than
    # risking a hallucinated transcription of near-nothing.
    transcription_silence_threshold_db: float = -40.0
    # Sanity bounds on a turn's (word count / duration) — outside this
    # range (checked only for turns at least 2s long, to avoid noise on
    # very short clips) flags low_confidence even if the ASR repetition
    # guard didn't itself detect a loop. Typical speech across the
    # languages here runs roughly 1.5-3 words/sec.
    transcription_min_words_per_second: float = 0.3
    transcription_max_words_per_second: float = 4.0

    # Phase 5+ (deferred — see README "Plan (current)")
    llm_provider: str = "openai"
    llm_api_key: str | None = None

    # Google Calendar booking trigger
    calendar_auth_mode: str = "oauth"  # "service_account" | "oauth"
    google_service_account_file: str | None = None
    google_oauth_client_file: str | None = None
    google_oauth_token_file: str = "oauth_token.json"
    employee_emails: str = ""  # comma-separated
    calendar_id: str = "primary"
    event_title_prefix: str | None = None
    calendar_poll_seconds: int = 180
    auto_add_bot_guest: bool = False

    # Bot account pool
    bot_heartbeat_interval_seconds: int = 30
    bot_heartbeat_timeout_seconds: int = 180
    bot_reservation_lead_seconds: int = 120
    bot_reservation_tail_seconds: int = 1800
    # Minimum time between health checks of the same (idle) account's saved
    # Google session — default weekly. How often the scheduler's
    # health-check loop actually wakes up to see who's due is a separate,
    # much shorter interval (bot_health_check_poll_seconds) — it has to be
    # shorter than the per-account interval or newly-due accounts could sit
    # waiting for up to a whole extra cycle.
    bot_health_check_interval_seconds: int = 7 * 24 * 3600
    bot_health_check_poll_seconds: int = 3600
    # Hard ceiling on a single health check run (launch + navigate + check)
    # — confirmed live that a stale profile lock can otherwise hang this
    # for a full BOT_HEARTBEAT_TIMEOUT_SECONDS-scale wait with nothing to
    # show for it. Enforced via a background thread join, not a Playwright
    # per-call timeout, since it has to bound the whole run including
    # browser launch itself.
    bot_health_check_run_timeout_seconds: int = 90

    # Meeting bot (Phase 2)
    bot_display_name: str = "Fincelerate Notetaker (recording)"
    bot_consent_message: str = (
        "Fincelerate Notetaker is recording and transcribing this meeting for internal analysis."
    )
    admission_timeout_seconds: int = 120
    alone_timeout_seconds: int = 60
    max_meeting_duration_seconds: int = 7200
    speaker_poll_interval_ms: int = 250
    # Raw per-tile speaking-indicator reads flicker a lot (confirmed on a
    # real call: dozens of sub-second start/stop pairs from normal speech
    # pauses). Debounced before being written to speakers.jsonl: a
    # stop->start gap shorter than this is merged into one continuous
    # speaking period (no stop/start emitted for the gap).
    speaking_debounce_gap_seconds: float = 1.0
    # A speaking burst shorter than this is dropped entirely (treated as
    # noise, not a real speaking_start/stop pair).
    speaking_min_burst_seconds: float = 0.3
    audio_segment_seconds: int = 300
    recordings_dir: str = "/recordings"
    # How long to wait for the page to settle (networkidle, best-effort) and
    # a recognizable pre-join screen to appear, before even trying the name
    # field. Longer than the old implicit ~5s so slow-loading pages (or ones
    # on an unexpected screen) get logged clearly instead of silently racing.
    pre_join_wait_seconds: int = 30
    # "signed_in" = the checked-out pool account's saved persistent profile
    # (default — guest mode is blocked outright by Google: "You can't join
    # this video call", confirmed on a real test). "guest" = fresh temp
    # profile, no sign-in, types the name field; kept as an option.
    bot_join_mode: str = "signed_in"
    bot_user_agent: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    )
    # Grace period before Docker SIGKILLs a bot container — must be long
    # enough for the SIGTERM handler to leave the call, stop ffmpeg, and
    # upload the recording.
    bot_stop_timeout_seconds: int = 30
    # Periodic (~60s) in-call debug captures (screenshot + HTML + console)
    # were essential for fixing selectors against real Meet DOM during
    # development, but are unneeded overhead once those selectors are
    # stable. Off by default; set true to re-enable while diagnosing a new
    # issue. Failure-triggered and key-moment captures (join click,
    # admission, exceptions) are unconditional regardless of this flag.
    bot_debug_capture: bool = False

    # Scheduler bot-launcher (docker-outside-of-docker)
    bot_launch_lead_seconds: int = 60
    bot_image: str = "meet-intelligence-bot"
    docker_network: str = "meet-intelligence_default"
    host_project_dir: str | None = None

    # Processing queue (Phase 3 adds the consumer)
    processing_queue_name: str = "meet-intelligence:processing"

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    @property
    def employee_email_list(self) -> list[str]:
        return [e.strip() for e in self.employee_emails.split(",") if e.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
