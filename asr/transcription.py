"""Phase 4: transcription.

transcribe_meeting() is the orchestrator asr/main.py and asr/cli.py both
call: for each of a diarized meeting's speaker_turns, skip the turn if
it's silence/noise, otherwise decide a language PER TURN (not per speaker
— see decide_turn_language's docstring for why), transcribe the turn's
audio slice with IndicTranscribeASR (if the decided language is one of
its 27) or WhisperASR (fallback, for languages outside that set), and
write transcript_segments (replacing any existing rows for the meeting —
idempotent, not additive).

Runs in its OWN container/queue (asr/, consuming
transcription_queue_name) specifically so this module's models and
worker/diarization.py's pyannote model are never loaded in the same
process at the same time. Phase 3 found that loading just ONE heavy CPU
model with the wrong batch size spiked a container from ~0.8GB to 5GB+
resident memory in two seconds and got it SIGKILLed with 7.75GB
available — loading a second, different heavy model (transformers-based
Indic-Transcribe, or faster-whisper) alongside pyannote in the same
process is exactly the scenario to avoid rather than discover the hard
way again.

Only the ASR backend classes import transformers/torch/faster_whisper,
and only inside lazily-cached loader methods — every other function here
is plain stdlib + the project's own common/ code, importable and
unit-testable without those installed at all (same convention as
worker/diarization.py's pyannote isolation).

Convention matches bot_pool/service.py and worker/diarization.py:
functions here only db.flush(); callers (asr/main.py, asr/cli.py) commit.
"""
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import delete
from sqlalchemy.orm import Session

from common.audio import download_and_concatenate_audio, extract_wav_slice, wav_energy_db
from common.config import Settings
from common.logging import get_logger
from common.models import Meeting, SpeakerTurn, TranscriptSegment
from common.storage import StorageClient

logger = get_logger(__name__)


# --- ASR backends ------------------------------------------------------------
# Both lazily import their heavy deps inside _pipeline()/_model() — never at
# module level — and cache the loaded model in a module-level dict keyed by
# (model_id/size, device), loaded once per worker process, not once per job.

_INDIC_TRANSCRIBE_CACHE: dict[str, Any] = {}
_WHISPER_CACHE: dict[str, Any] = {}


class IndicTranscribeASR:
    """bodhan-ai/indic-transcribe-flex (or -core, via settings.
    indic_transcribe_model_id).

    VERIFIED live against the model repo's own source on first real run
    (fetched README.md/inference.py/indic_transcribe.py/long_form.py from
    the Hub) — this is NOT a standard transformers.pipeline()-compatible
    model. It ships its own `indic_transcribe.py` module (IndicTranscribe
    class, LANGUAGES, MODES) with a custom tokenizer that loads two
    SentencePiece files via os.path.join — it needs a real local
    directory, not a Hub repo id or an AutoTokenizer/pipeline()
    dynamic-class path. So: snapshot_download the whole repo, put it on
    sys.path, import the repo's own classes/helpers directly — including
    `long_form.py`, which ships the repetition-loop guard transcribe()
    below reuses verbatim (same detection, same constants, same
    split-at-pause retry, same n-gram-collapse last resort) rather than
    reimplementing a second, possibly-drifting copy of it.

    mode="mixed" matches MODES["mixed"] = (itn=True, romanized=False) in
    the repo's indic_transcribe.py exactly."""

    def __init__(self, model_id: str, device: str, mode: str, num_threads: int = 1):
        self.model_id = model_id
        self.device = device
        self.mode = mode
        self.num_threads = num_threads

    def _pipeline(self) -> Any:
        cache_key = f"{self.model_id}:{self.device}"
        if cache_key not in _INDIC_TRANSCRIBE_CACHE:
            import sys

            import torch  # local import: heavy — see module docstring
            from huggingface_hub import snapshot_download

            # Capped (not left at torch's default, which spawns one thread
            # per core) per settings.asr_num_threads — confirmed live this
            # container was only using 1 of 10 available cores because the
            # cap was hardcoded to 1, copied from worker/Dockerfile's
            # pyannote-specific OOM fix without rethinking it here.
            if self.device == "cpu":
                torch.set_num_threads(self.num_threads)
            logger.info(
                "asr_thread_pool_configured",
                backend="indic_transcribe",
                num_threads=self.num_threads,
                torch_get_num_threads=torch.get_num_threads(),
            )

            logger.info("asr_loading_model", backend="indic_transcribe", model=self.model_id, device=self.device)
            model_dir = snapshot_download(self.model_id)
            if model_dir not in sys.path:
                sys.path.insert(0, model_dir)
            from indic_transcribe import IndicTranscribe  # the model repo's own module
            import long_form  # the model repo's own repetition-loop guard

            asr = IndicTranscribe.from_pretrained(model_dir, device=self.device)
            _INDIC_TRANSCRIBE_CACHE[cache_key] = (asr, long_form)
            logger.info("asr_model_loaded", backend="indic_transcribe", model=self.model_id)
        return _INDIC_TRANSCRIBE_CACHE[cache_key]

    def transcribe(self, wav_path: Path, language: str) -> tuple[str, bool]:
        """Returns (text, repetition_detected). repetition_detected is True
        if the repo's own repetition guard ever had to intervene on this
        clip — even when a retry split successfully fixed it — since that
        still means the model struggled with this specific audio."""
        asr, long_form = self._pipeline()
        wav = asr._load(str(wav_path))
        text, repetition_detected = self._decode_guarded(asr, long_form, wav, language)
        return text.strip(), repetition_detected

    def _decode_guarded(self, asr: Any, long_form: Any, wav: Any, language: str, depth: int = 0) -> tuple[str, bool]:
        """Mirrors long_form.py's own `_decode_guarded` exactly: decode
        once; if the output has collapsed into a repeated-phrase loop
        (>LOOP_REPETITION 4-gram repetition over >=LOOP_MIN_WORDS words),
        split at a pause (same split_on_silence the repo uses for
        over-length audio) and decode each half, up to 2 levels deep;
        beyond that, collapse the repeats so a stuck loop can't flood the
        transcript. Only difference from the repo's version: this also
        reports whether looping was ever seen, for low_confidence."""
        text = asr.transcribe(wav, lang=language, mode=self.mode)
        text = text if isinstance(text, str) else text[0]
        words = text.split()
        looped = len(words) >= long_form.LOOP_MIN_WORDS and long_form._repetition(words) > long_form.LOOP_REPETITION
        if not looped:
            return text, False
        if depth >= 2 or wav.numel() < 4 * 16000:
            return " ".join(long_form._collapse_repeats(words)), True
        half = wav.numel() / 16000 / 2
        parts = long_form.split_on_silence(wav, target=half, hard_max=half + 2.0)
        if len(parts) < 2:
            mid = wav.numel() // 2
            parts = [(0, mid), (mid, wav.numel())]
        pieces = [
            self._decode_guarded(asr, long_form, wav[x:y], language, depth + 1)[0]
            for x, y in parts
            if y - x >= 1600
        ]
        return " ".join(piece.strip() for piece in pieces if piece and piece.strip()), True


class WhisperASR:
    """faster-whisper — the fallback for languages Indic-Transcribe
    doesn't support, and also used universally for language ID (its
    language-detection API is well-documented/stable; reusing it avoids
    guessing at an unverified Indic-Transcribe LID surface for something
    the task doesn't actually require Indic-Transcribe to do)."""

    def __init__(self, model_size: str, device: str, compute_type: str, num_threads: int = 1):
        self.model_size = model_size
        self.device = device
        self.compute_type = compute_type
        self.num_threads = num_threads

    def _model(self) -> Any:
        cache_key = f"{self.model_size}:{self.device}:{self.compute_type}"
        if cache_key not in _WHISPER_CACHE:
            from faster_whisper import WhisperModel  # local import: heavy — see module docstring

            logger.info("asr_loading_model", backend="whisper", model=self.model_size, device=self.device)
            # CTranslate2 has its own thread pool, unrelated to torch's, so
            # it needs its own explicit cap — same settings.asr_num_threads
            # as IndicTranscribeASR, not the hardcoded 1 this used before.
            cpu_threads = self.num_threads if self.device == "cpu" else 0  # 0 = faster-whisper's own default on GPU
            _WHISPER_CACHE[cache_key] = WhisperModel(
                self.model_size, device=self.device, compute_type=self.compute_type, cpu_threads=cpu_threads
            )
            logger.info("asr_thread_pool_configured", backend="whisper", num_threads=cpu_threads)
            logger.info("asr_model_loaded", backend="whisper", model=self.model_size)
        return _WHISPER_CACHE[cache_key]

    def transcribe(self, wav_path: Path, language: str | None) -> tuple[str, bool]:
        segments, _info = self._model().transcribe(str(wav_path), language=language)
        text = " ".join(segment.text.strip() for segment in segments).strip()
        return text, False  # the repetition guard is IndicTranscribeASR-specific (see its docstring)

    def detect_language_topk(self, wav_path: Path) -> list[tuple[str, float]]:
        """Full LID distribution, best-first. faster-whisper always
        detects language as part of transcribe() unless language= is
        given, so a plain auto-detecting call doubles as LID; its
        TranscriptionInfo.all_language_probs (confirmed present on the
        installed version) is the full ranked list, not just the top-1."""
        _segments, info = self._model().transcribe(str(wav_path), language=None)
        if info.all_language_probs:
            return list(info.all_language_probs)
        return [(info.language, info.language_probability)]


# --- language decision (per turn, not per speaker) ---------------------------


@dataclass
class TurnLanguageDecision:
    detected: str  # raw LID top-1 result for this turn's audio
    detected_probability: float
    topk: list[tuple[str, float]]
    used: str
    source: str  # "lid_en_high_confidence" | "default_indic_mixed_mode" | "restricted_to_expected_languages"


def decide_turn_language(meeting: Meeting, slice_path: Path, settings: Settings, whisper: WhisperASR) -> TurnLanguageDecision:
    """One decision per TURN, not per speaker — a speaker who code-switches
    needs each turn judged on its own audio; deciding once per speaker and
    out-voting their other turns is exactly what produced garbled
    transcripts on turns where the "wrong" language won the speaker-level
    vote.

    LID only ever makes a binary call here: is this turn confidently
    English, or not (settings.en_lid_confidence_threshold). Anything not
    confidently English falls back to settings.default_language with
    mode="mixed" — Indic-Transcribe renders that language in native
    script with English loanwords/numerals in Latin, the right rendering
    for code-mixed speech, regardless of which exact non-English language
    LID guessed. LID is too unreliable at fine-grained Indic-language
    discrimination on short clips to trust for anything beyond the
    English/not-English call (the old per-speaker design's hi/ur/mai/bho
    confusion was exactly this problem one level deeper).

    meeting.expected_languages, when set, restricts the usable
    candidates: "en" is only chosen if it's in that list, and the Indic
    fallback uses the first non-English expected language instead of
    default_language if default_language itself isn't in the list."""
    topk = whisper.detect_language_topk(slice_path)
    top_lang, top_prob = topk[0]

    allowed = set(meeting.expected_languages) if meeting.expected_languages else None

    indic_fallback = settings.default_language
    if allowed is not None and indic_fallback not in allowed:
        non_en_allowed = [lang for lang in meeting.expected_languages if lang != "en"]
        indic_fallback = non_en_allowed[0] if non_en_allowed else next(iter(allowed))

    en_allowed = allowed is None or "en" in allowed

    if top_lang == "en" and top_prob >= settings.en_lid_confidence_threshold and en_allowed:
        used, source = "en", "lid_en_high_confidence"
    else:
        used, source = indic_fallback, "default_indic_mixed_mode"

    if allowed is not None and used not in allowed:
        used, source = next(iter(allowed)), "restricted_to_expected_languages"

    return TurnLanguageDecision(detected=top_lang, detected_probability=top_prob, topk=topk, used=used, source=source)


def choose_backend(language: str, settings: Settings, indic: IndicTranscribeASR, whisper: WhisperASR) -> Any:
    return indic if language in settings.indic_transcribe_languages else whisper


# --- orchestration ------------------------------------------------------------


def transcribe_meeting(db: Session, meeting: Meeting, settings: Settings, limit: int | None = None) -> int:
    """Downloads + concatenates audio (same as Phase 3 — no shared
    artifact between the two containers, see module docstring), and for
    each speaker_turns row: skips it (as silence/noise) if its energy is
    below settings.transcription_silence_threshold_db, otherwise decides
    its language, transcribes it, and flags low_confidence when the ASR
    repetition guard intervened or the output's word rate doesn't match
    its duration. REPLACES meeting's transcript_segments (delete-then-
    insert — idempotent, re-running never duplicates). Returns the number
    of segments written.

    `limit` caps how many turns are actually transcribed (for a quick
    manual check, via the CLI's --limit) — the delete-then-insert above
    still replaces ALL of the meeting's existing segments, so a limited
    run leaves the meeting with a partial transcript, not a merge of old
    and new; a warning is logged when that happens.

    Raises on failure — callers (asr/main.py, asr/cli.py) own status
    transitions and retry policy. Only flushes; caller commits."""
    turns = list(db.query(SpeakerTurn).filter(SpeakerTurn.meeting_id == meeting.id).order_by(SpeakerTurn.start_ms))
    if not turns:
        raise RuntimeError("meeting has no speaker_turns — run diarization (Phase 3) first")
    if not meeting.audio_key:
        raise RuntimeError("meeting has no audio_key — nothing to transcribe")

    storage = StorageClient(settings)
    indic = IndicTranscribeASR(
        settings.indic_transcribe_model_id,
        settings.transcription_device,
        settings.indic_transcribe_mode,
        settings.asr_num_threads,
    )
    whisper = WhisperASR(
        settings.whisper_model_size, settings.transcription_device, settings.whisper_compute_type, settings.asr_num_threads
    )

    with tempfile.TemporaryDirectory(prefix=f"transcribe-{meeting.id}-") as tmp:
        tmp_dir = Path(tmp)
        wav_path = tmp_dir / "full.wav"
        download_and_concatenate_audio(storage, meeting.audio_key, wav_path, tmp_dir)

        db.execute(delete(TranscriptSegment).where(TranscriptSegment.meeting_id == meeting.id))

        turns_to_transcribe = turns[:limit] if limit is not None else turns
        if limit is not None and limit < len(turns):
            logger.warning(
                "transcription_limited_partial_replace",
                meeting_id=str(meeting.id),
                limit=limit,
                total_turns=len(turns),
            )

        written = 0
        total = len(turns_to_transcribe)
        started = time.time()
        for idx, turn in enumerate(turns_to_transcribe, start=1):
            slice_path = tmp_dir / f"turn_{turn.id}.wav"
            extract_wav_slice(wav_path, turn.start_ms / 1000, turn.end_ms / 1000, slice_path)
            turn_started = time.time()

            energy_db = wav_energy_db(slice_path)
            if energy_db < settings.transcription_silence_threshold_db:
                db.add(
                    TranscriptSegment(
                        meeting_id=meeting.id,
                        speaker_turn_id=turn.id,
                        start_ms=turn.start_ms,
                        end_ms=turn.end_ms,
                        detected_language=None,
                        lid_topk=None,
                        language=None,
                        language_source="skipped_silence_or_noise",
                        text="",
                        asr_model=None,
                        low_confidence=True,
                        low_confidence_reason="silence_or_noise",
                    )
                )
                written += 1
                logger.info(
                    "transcription_turn_progress",
                    meeting_id=str(meeting.id),
                    turn=idx,
                    total=total,
                    backend="skipped",
                    seconds=round(time.time() - turn_started, 2),
                    energy_db=round(energy_db, 1),
                )
                continue

            decision = decide_turn_language(meeting, slice_path, settings, whisper)
            backend = choose_backend(decision.used, settings, indic, whisper)
            backend_name = "indic_transcribe" if backend is indic else "whisper"
            model_label = settings.indic_transcribe_model_id if backend is indic else settings.whisper_model_size

            text, repetition_detected = backend.transcribe(slice_path, decision.used)
            turn_elapsed = time.time() - turn_started

            duration_s = (turn.end_ms - turn.start_ms) / 1000
            words_per_second = len(text.split()) / duration_s if duration_s > 0 else 0.0
            length_disproportionate = duration_s >= 2.0 and not (
                settings.transcription_min_words_per_second <= words_per_second <= settings.transcription_max_words_per_second
            )

            reasons = []
            if repetition_detected:
                reasons.append("repetition_detected")
            if length_disproportionate:
                reasons.append("length_disproportionate")

            db.add(
                TranscriptSegment(
                    meeting_id=meeting.id,
                    speaker_turn_id=turn.id,
                    start_ms=turn.start_ms,
                    end_ms=turn.end_ms,
                    detected_language=decision.detected,
                    lid_topk=[list(pair) for pair in decision.topk],
                    language=decision.used,
                    language_source=decision.source,
                    text=text,
                    asr_model=f"{backend_name}:{model_label}",
                    low_confidence=bool(reasons),
                    low_confidence_reason=",".join(reasons) if reasons else None,
                )
            )
            written += 1
            logger.info(
                "transcription_turn_progress",
                meeting_id=str(meeting.id),
                turn=idx,
                total=total,
                backend=backend_name,
                seconds=round(turn_elapsed, 2),
                language=decision.used,
                language_source=decision.source,
                low_confidence=bool(reasons),
            )

        elapsed = time.time() - started
        logger.info(
            "transcription_complete",
            meeting_id=str(meeting.id),
            segments=written,
            seconds=round(elapsed, 1),
            device=settings.transcription_device,
        )

        db.flush()
        return written
