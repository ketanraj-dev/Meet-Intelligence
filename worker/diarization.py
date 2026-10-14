"""Phase 3: diarization + speaker naming.

process_meeting() is the orchestrator worker/main.py and worker/cli.py both
call: download + concatenate a meeting's audio segments, run pyannote
diarization, resolve cross-cluster overlapping speech to a single dominant
pyannote cluster per region, then map onto REAL participant identity by
time: a single pyannote cluster commonly spans periods where different real
participants were actually speaking (per speakers.jsonl's debounced
speaking_start/stop intervals, same "t" clock as the WAV) — naming the
whole cluster after whichever participant has the most overlap OVERALL
mislabels every turn during the other participant's actual turns. Instead
each cluster's time range is split at the points where the "active" known
speaker changes (assign_participants), gaps between known intervals filled
by nearest-in-time speaker rather than left unattributed, then merged/
absorbed/split into Phase-4-ready turns and written to `speaker_turns`
(replacing any existing rows for that meeting — idempotent, not additive).

Only _get_pipeline/run_pyannote_diarization import pyannote.audio/torch,
and only inside the function body — every other function in this module is
plain stdlib + the project's own common/ code, importable and unit-testable
without those (large, CPU/GPU-sensitive) dependencies installed at all.

Convention matches bot_pool/service.py: functions here only db.flush();
callers (worker/main.py, worker/cli.py) are responsible for db.commit().
"""
import array
import json
import math
import tempfile
import time
import wave
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from sqlalchemy import delete
from sqlalchemy.orm import Session

from common.config import Settings
from common.logging import get_logger
from common.models import Meeting, SpeakerTurn
from common.audio import download_and_concatenate_audio
from common.storage import StorageClient

logger = get_logger(__name__)

# (start_seconds, end_seconds, cluster_label) — pyannote's raw per-track
# output, before overlap resolution / gap-merge / long-turn splitting.
RawSegment = tuple[float, float, str]
# RawSegment plus the resolved overlap flag, after resolve_overlaps() —
# still keyed by raw pyannote cluster_label, before per-time-region
# participant assignment (see assign_participants / Turn below).
ResolvedTurn = tuple[float, float, str, bool]


@dataclass
class Turn:
    """A turn attributed to a real participant identity (not just a raw
    pyannote cluster) — the unit assign_participants onward operates on,
    all the way through to the final speaker_turns row. mapping_confidence
    starts unset (None) and is filled in last, once start/end are final —
    see _compute_turn_confidence."""

    start: float
    end: float
    cluster_label: str
    overlap: bool
    participant_id: str | None
    speaker_name: str
    mapping_confidence: float | None = None


# --- speakers.jsonl -----------------------------------------------------------


@dataclass
class SpeakerEventData:
    names: dict[str, str]
    # (start_seconds, end_seconds, participant_id) — paired from debounced
    # speaking_start/speaking_stop events, same "t" clock as the WAV.
    speaking_intervals: list[tuple[float, float, str]]


def parse_speaker_events(path: Path) -> SpeakerEventData:
    """Never raises on a missing/empty/malformed file — just returns no
    data, which map_clusters_to_speakers treats as "selector broken,
    fall back to Speaker A/B naming" (item 7)."""
    names: dict[str, str] = {}
    intervals: list[tuple[float, float, str]] = []
    if not path.exists():
        return SpeakerEventData(names=names, speaking_intervals=intervals)

    open_starts: dict[str, float] = {}
    with path.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            pid = event.get("participant_id")
            name = event.get("name")
            if pid and name:
                names[pid] = name
            kind, t = event.get("event"), event.get("t")
            if kind == "speaking_start" and pid is not None and t is not None:
                open_starts[pid] = t
            elif kind == "speaking_stop" and pid is not None and t is not None:
                start = open_starts.pop(pid, None)
                if start is not None and t > start:
                    intervals.append((start, t, pid))
    return SpeakerEventData(names=names, speaking_intervals=intervals)


# --- cluster -> speaker name mapping -------------------------------------------


@dataclass
class ClusterMapping:
    participant_id: str | None
    speaker_name: str
    mapping_confidence: float | None


def _letter_for_index(i: int) -> str:
    """0->A, 1->B, ..., 25->Z, 26->AA, ... (spreadsheet-column style, in
    case a meeting somehow has more than 26 pyannote clusters)."""
    i += 1
    letters = ""
    while i > 0:
        i, rem = divmod(i - 1, 26)
        letters = chr(65 + rem) + letters
    return letters


def map_clusters_to_speakers(
    pyannote_segments: list[RawSegment], event_data: SpeakerEventData
) -> tuple[dict[str, ClusterMapping], bool]:
    """Returns (cluster_label -> ClusterMapping, used_fallback_naming).

    used_fallback_naming is True only when speakers.jsonl had literally no
    speaking intervals at all (item 7 — e.g. a broken speaking_indicator
    selector) — every cluster gets "Speaker A"/"Speaker B"/... in order of
    first appearance, confidence=None. That's different from a single
    cluster simply not matching anyone despite real data existing, which
    becomes "Unknown speaker N" (item 3) with confidence=0.0 — still
    real data, just an unmatched cluster (e.g. background noise pyannote
    treated as a distinct speaker).

    Confidence is the matched participant's overlap time as a fraction of
    the CLUSTER's own total speaking time — "of everything this pyannote
    cluster said, how much of it lines up with this participant's known
    speaking intervals"."""
    cluster_order: list[str] = []
    seen: set[str] = set()
    for _, _, label in pyannote_segments:
        if label not in seen:
            seen.add(label)
            cluster_order.append(label)

    if not event_data.speaking_intervals:
        return {
            label: ClusterMapping(participant_id=None, speaker_name=f"Speaker {_letter_for_index(i)}", mapping_confidence=None)
            for i, label in enumerate(cluster_order)
        }, True

    cluster_totals: dict[str, float] = {}
    overlap_by_cluster: dict[str, dict[str, float]] = {}
    for start, end, label in pyannote_segments:
        cluster_totals[label] = cluster_totals.get(label, 0.0) + (end - start)
        per_participant = overlap_by_cluster.setdefault(label, {})
        for p_start, p_end, pid in event_data.speaking_intervals:
            overlap = min(end, p_end) - max(start, p_start)
            if overlap > 0:
                per_participant[pid] = per_participant.get(pid, 0.0) + overlap

    mapping: dict[str, ClusterMapping] = {}
    unknown_count = 0
    for label in cluster_order:
        totals = overlap_by_cluster.get(label, {})
        cluster_total = cluster_totals.get(label, 0.0)
        best = max(totals.items(), key=lambda kv: kv[1]) if totals else None
        if best is not None and best[1] > 0 and cluster_total > 0:
            pid, overlap_seconds = best
            mapping[label] = ClusterMapping(
                participant_id=pid,
                speaker_name=event_data.names.get(pid, pid),
                mapping_confidence=overlap_seconds / cluster_total,
            )
        else:
            unknown_count += 1
            mapping[label] = ClusterMapping(
                participant_id=None, speaker_name=f"Unknown speaker {unknown_count}", mapping_confidence=0.0
            )
    return mapping, False


# --- overlap resolution ---------------------------------------------------------


def resolve_overlaps(segments: list[RawSegment]) -> list[ResolvedTurn]:
    """Where two+ pyannote segments from different clusters overlap in
    time, the dominant one (longer ORIGINAL segment duration) wins that
    time region (overlap=True); the other's conflicting portion is
    dropped, but any part of it OUTSIDE the overlapping region is kept as
    its own normal (overlap=False) turn — boundary sweep, not a blunt
    "discard the whole shorter segment", so real exclusive speech right
    next to a brief overlap isn't lost too.

    Adjacent sub-intervals that resolve to the same (cluster_label,
    overlap) are merged back together."""
    if not segments:
        return []

    boundaries = sorted({point for seg in segments for point in (seg[0], seg[1])})
    raw: list[ResolvedTurn] = []
    for lo, hi in zip(boundaries, boundaries[1:]):
        if hi - lo <= 0:
            continue
        active = [seg for seg in segments if seg[0] <= lo and seg[1] >= hi]
        if not active:
            continue
        if len(active) == 1:
            raw.append((lo, hi, active[0][2], False))
        else:
            dominant = max(active, key=lambda s: s[1] - s[0])
            raw.append((lo, hi, dominant[2], True))

    merged: list[list[Any]] = []
    for start, end, label, overlap in raw:
        if merged and merged[-1][2] == label and merged[-1][3] == overlap and abs(merged[-1][1] - start) < 1e-6:
            merged[-1][1] = end
        else:
            merged.append([start, end, label, overlap])
    return [tuple(t) for t in merged]


# --- participant assignment (per-time-region, not per-cluster) -------------------
# This is what fixes the real bug a live run surfaced: naming a whole
# pyannote cluster after whichever participant has the MOST overlap across
# its ENTIRE span mislabels every turn during a period where a *different*
# known participant was actually speaking, if both happen to fall inside
# the same cluster (which pyannote's clustering doesn't guarantee lines up
# with real speaker-change boundaries). Confirmed live on meeting
# e140849a: a single dominant cluster spanning ~41s-166s got named
# "Information Technology" (slightly more overlap overall) even though
# "Ketan Raj" has 50 of its own logged speaking_start/stop events later in
# that exact span. Splitting at the real speaker-change boundaries fixes
# this without needing pyannote to cluster any better.


def _nearest_participant(t_mid: float, speaking_intervals: list[tuple[float, float, str]]) -> str | None:
    """The participant_id whose speaking interval is closest in time to
    t_mid (0 distance if t_mid falls inside it). None only when
    speaking_intervals is completely empty — otherwise this always
    resolves to *someone*, which is the point: a brief gap between two
    known speakers' intervals still gets a real name instead of
    "Unknown", at the cost of confidence (computed separately) correctly
    reflecting that it wasn't a direct overlap."""
    if not speaking_intervals:
        return None

    def _distance(interval: tuple[float, float, str]) -> float:
        s, e, _pid = interval
        if s <= t_mid <= e:
            return 0.0
        return min(abs(s - t_mid), abs(e - t_mid))

    return min(speaking_intervals, key=_distance)[2]


def split_turn_by_participant(
    start: float, end: float, speaking_intervals: list[tuple[float, float, str]]
) -> list[tuple[float, float, str | None, bool]]:
    """Splits [start, end] into contiguous sub-ranges by which participant
    was actually speaking, instead of leaving the whole range attributed
    to one name. Each piece is (lo, hi, participant_id, direct_overlap):
    direct_overlap=True means that piece sits wholly inside a known
    speaking interval; False means it was filled in via
    _nearest_participant (a gap between two known intervals, or outside
    all of them) — _compute_turn_confidence uses this distinction later.
    participant_id is None only when speaking_intervals is empty overall;
    callers route that case through the existing Speaker A/B fallback
    (map_clusters_to_speakers / used_fallback) instead, not here."""
    relevant = [(max(s, start), min(e, end), pid) for s, e, pid in speaking_intervals if e > start and s < end]
    boundaries = sorted({start, end} | {edge for s, e, _pid in relevant for edge in (s, e)})

    pieces: list[tuple[float, float, str | None, bool]] = []
    for lo, hi in zip(boundaries, boundaries[1:]):
        if hi - lo <= 0:
            continue
        active = [iv for iv in relevant if iv[0] <= lo and iv[1] >= hi]
        if active:
            dominant = max(active, key=lambda iv: iv[1] - iv[0])
            pieces.append((lo, hi, dominant[2], True))
        else:
            pieces.append((lo, hi, _nearest_participant((lo + hi) / 2, speaking_intervals), False))

    merged: list[list[Any]] = []
    for lo, hi, pid, direct in pieces:
        if merged and merged[-1][2] == pid and merged[-1][3] == direct and abs(merged[-1][1] - lo) < 1e-9:
            merged[-1][1] = hi
        else:
            merged.append([lo, hi, pid, direct])
    return [tuple(p) for p in merged]


def assign_participants(
    meeting_id: Any,
    resolved_turns: list[ResolvedTurn],
    event_data: SpeakerEventData,
    cluster_mapping: dict[str, ClusterMapping],
    used_fallback: bool,
) -> list[Turn]:
    """Converts cluster-labeled ResolvedTurns into participant-identified
    Turns. When used_fallback is True there's no speaking data to split
    by at all, so each resolved turn stays whole under the existing
    Speaker A/B/... mapping (confidence left None — filled in by the
    caller). Otherwise every resolved turn is split via
    split_turn_by_participant; logs a `cluster_split` line (with each
    participant's total seconds) for any pyannote cluster whose pieces
    ended up naming more than one real participant — worth knowing even
    though it's no longer a problem."""
    if used_fallback:
        return [
            Turn(start, end, label, overlap, None, cluster_mapping[label].speaker_name)
            for start, end, label, overlap in resolved_turns
        ]

    turns: list[Turn] = []
    cluster_participant_seconds: dict[str, dict[str, float]] = {}
    for start, end, label, overlap in resolved_turns:
        per_cluster = cluster_participant_seconds.setdefault(label, {})
        for lo, hi, pid, _direct in split_turn_by_participant(start, end, event_data.speaking_intervals):
            name = event_data.names.get(pid, pid)
            turns.append(Turn(lo, hi, label, overlap, pid, name))
            per_cluster[pid] = per_cluster.get(pid, 0.0) + (hi - lo)

    for label, seconds_by_pid in cluster_participant_seconds.items():
        if len(seconds_by_pid) > 1:
            logger.info(
                "cluster_split",
                meeting_id=str(meeting_id),
                cluster_label=label,
                participants={
                    event_data.names.get(pid, pid): round(seconds, 2) for pid, seconds in seconds_by_pid.items()
                },
            )

    return turns


def _compute_turn_confidence(turn: Turn, event_data: SpeakerEventData) -> float:
    """Fraction of THIS turn's own duration directly corroborated by its
    assigned participant's known speaking intervals — computed per final
    turn (after all merging/splitting), not once for the whole cluster. A
    turn built entirely from nearest-neighbor-filled gaps (no direct
    overlap anywhere) correctly computes to 0.0 — an honest signal, not a
    guessed one."""
    duration = turn.end - turn.start
    if duration <= 0:
        return 0.0
    overlap_seconds = sum(
        max(0.0, min(turn.end, e) - max(turn.start, s))
        for s, e, pid in event_data.speaking_intervals
        if pid == turn.participant_id
    )
    return min(1.0, overlap_seconds / duration)


# --- gap merge + micro-turn absorption + long-turn splitting ---------------------


def merge_turns_by_speaker(turns: list[Turn], max_gap_seconds: float) -> list[Turn]:
    """Like the old cluster-label-based merge, but keyed on the FINAL
    assigned speaker identity (participant_id, speaker_name) — after
    split_turn_by_participant, a single pyannote cluster can legitimately
    represent multiple real speakers, so merging must not blend them back
    together just because they share a cluster_label. cluster_label on
    the merged turn is whichever piece came first (informational only by
    this point)."""
    if not turns:
        return []
    ordered = sorted(turns, key=lambda t: t.start)
    merged: list[Turn] = [ordered[0]]
    for turn in ordered[1:]:
        last = merged[-1]
        same_speaker = (turn.participant_id, turn.speaker_name) == (last.participant_id, last.speaker_name)
        if same_speaker and turn.start - last.end < max_gap_seconds:
            merged[-1] = replace(last, end=max(last.end, turn.end), overlap=last.overlap or turn.overlap)
        else:
            merged.append(turn)
    return merged


def absorb_micro_turns(turns: list[Turn], min_turn_seconds: float) -> list[Turn]:
    """A turn shorter than min_turn_seconds is treated as clustering
    noise, not a meaningful separately-attributable utterance — absorbed
    into the previous turn (extending its end to cover it), or the next
    turn if there is no previous (the very first turn is a micro-turn).
    The micro-turn's own identity is discarded entirely; the survivor
    keeps its own. Runs to a fixed point since absorbing one micro-turn
    can occasionally leave its neighbor still under threshold (e.g. two
    adjacent micro-turns)."""
    if not turns:
        return []
    result = sorted(turns, key=lambda t: t.start)
    changed = True
    while changed and len(result) > 1:
        changed = False
        for i, turn in enumerate(result):
            if turn.end - turn.start >= min_turn_seconds:
                continue
            if i > 0:
                result[i - 1] = replace(result[i - 1], end=turn.end)
            else:
                result[i + 1] = replace(result[i + 1], start=turn.start)
            del result[i]
            changed = True
            break
    return result


def _rms_envelope(wav_path: Path, start_s: float, end_s: float, window_s: float = 0.05) -> list[tuple[float, float]]:
    """[(window_start_seconds, rms_dbfs), ...] for consecutive windows
    across [start_s, end_s]. Pure stdlib (wave + array) — 16-bit PCM,
    whatever channel count the file actually has (only the first channel
    is sampled, which is all that matters for a mono recording)."""
    with wave.open(str(wav_path), "rb") as wf:
        rate = wf.getframerate()
        sampwidth = wf.getsampwidth()
        nchannels = wf.getnchannels()
        if sampwidth != 2:
            raise ValueError(f"expected 16-bit PCM, got sampwidth={sampwidth}")
        start_frame = max(0, int(start_s * rate))
        end_frame = min(wf.getnframes(), int(end_s * rate))
        total_frames = end_frame - start_frame
        if total_frames <= 0:
            return []
        wf.setpos(start_frame)
        window_frames = max(1, int(window_s * rate))

        envelope: list[tuple[float, float]] = []
        frames_read = 0
        t = start_frame / rate
        while frames_read < total_frames:
            to_read = min(window_frames, total_frames - frames_read)
            raw = wf.readframes(to_read)
            if not raw:
                break
            samples = array.array("h", raw)
            if nchannels > 1:
                samples = samples[::nchannels]
            if samples:
                mean_sq = sum(s * s for s in samples) / len(samples)
                rms = math.sqrt(mean_sq)
                db = 20 * math.log10(rms / 32768) if rms > 0 else -120.0
            else:
                db = -120.0
            envelope.append((t, db))
            frames_read += to_read
            t += to_read / rate
        return envelope


def _find_split_point(
    wav_path: Path, start_s: float, end_s: float, max_chunk_s: float, min_silence_s: float, silence_threshold_db: float
) -> float:
    """A split point within (start_s, start_s + max_chunk_s] — the
    midpoint of the LAST qualifying silence run (>= min_silence_s,
    quieter than silence_threshold_db) found in that window, so the chunk
    stays as close to max_chunk_s as possible while still cutting in
    silence. Falls back to a hard cut at the window limit if no
    qualifying silence exists anywhere in it."""
    limit = min(end_s, start_s + max_chunk_s)
    envelope = _rms_envelope(wav_path, start_s, limit)
    if len(envelope) < 2:
        return limit
    window_s = envelope[1][0] - envelope[0][0]
    min_windows = max(1, round(min_silence_s / window_s)) if window_s > 0 else 1

    best_split = None
    run_start = None
    for i, (_, db) in enumerate(envelope):
        quiet = db < silence_threshold_db
        if quiet:
            if run_start is None:
                run_start = i
        else:
            if run_start is not None and (i - run_start) >= min_windows:
                best_split = (envelope[run_start][0] + envelope[i][0]) / 2
            run_start = None
    if run_start is not None and (len(envelope) - run_start) >= min_windows:
        run_end_t = envelope[-1][0] + window_s
        best_split = (envelope[run_start][0] + run_end_t) / 2

    return best_split if best_split is not None else limit


def split_long_turns(
    turns: list[Turn],
    wav_path: Path,
    max_chunk_s: float,
    min_silence_s: float,
    silence_threshold_db: float,
) -> list[Turn]:
    result: list[Turn] = []
    for turn in turns:
        result.extend(_split_one(turn, wav_path, max_chunk_s, min_silence_s, silence_threshold_db))
    return result


def _split_one(
    turn: Turn,
    wav_path: Path,
    max_chunk_s: float,
    min_silence_s: float,
    silence_threshold_db: float,
) -> list[Turn]:
    if turn.end - turn.start <= max_chunk_s:
        return [turn]
    split_at = _find_split_point(wav_path, turn.start, turn.end, max_chunk_s, min_silence_s, silence_threshold_db)
    split_at = min(max(split_at, turn.start + 0.01), turn.end - 0.01)
    rest = _split_one(replace(turn, start=split_at), wav_path, max_chunk_s, min_silence_s, silence_threshold_db)
    return [replace(turn, end=split_at)] + rest


# --- pyannote (the only part that needs torch/pyannote.audio installed) ---------

_PIPELINE_CACHE: dict[str, Any] = {}


def _get_pipeline(settings: Settings) -> Any:
    """Loaded once per worker process (module-level cache), not once per
    job — pyannote model load is the expensive part, there's no reason to
    pay it again for every meeting a long-running worker processes.

    torch.set_num_threads(1) on CPU: confirmed live that leaving PyTorch's
    default (one thread per core) caused a short multi-minute meeting's
    diarization to spike from ~0.8GB to 5GB+ resident memory within two
    seconds and get OOM-killed (each BLAS thread allocates its own working
    buffers — this compounds fast on a many-core host with no explicit
    memory limit). Single-threaded is slower, not faster, per job — the
    device is CPU specifically because this is "slow is fine" per the
    current testing setup — but it actually completes instead of dying.
    GPU mode doesn't hit this (no CPU thread pool involved), so this is
    gated on the device actually being "cpu"."""
    cache_key = f"{settings.diarization_model}:{settings.diarization_device}"
    if cache_key not in _PIPELINE_CACHE:
        from pyannote.audio import Pipeline  # local import: heavy, CPU/GPU-sensitive — see module docstring
        import torch

        if settings.diarization_device == "cpu":
            torch.set_num_threads(1)

        logger.info("diarization_loading_model", model=settings.diarization_model, device=settings.diarization_device)
        pipeline = Pipeline.from_pretrained(settings.diarization_model, use_auth_token=settings.hf_token)
        pipeline.to(torch.device(settings.diarization_device))
        # See diarization_batch_size's config comment — pyannote's own
        # default (32) OOM-killed a real run; this is a real fix, not a
        # cautious guess.
        pipeline.segmentation_batch_size = settings.diarization_batch_size
        pipeline.embedding_batch_size = settings.diarization_batch_size
        _PIPELINE_CACHE[cache_key] = pipeline
        logger.info("diarization_model_loaded", model=settings.diarization_model)
    return _PIPELINE_CACHE[cache_key]


def run_pyannote_diarization(wav_path: Path, settings: Settings) -> list[RawSegment]:
    pipeline = _get_pipeline(settings)
    annotation = pipeline(str(wav_path))
    return [(segment.start, segment.end, label) for segment, _, label in annotation.itertracks(yield_label=True)]


# --- orchestration ---------------------------------------------------------------


def process_meeting(db: Session, meeting: Meeting, settings: Settings) -> int:
    """Downloads + concatenates audio, runs diarization, maps clusters to
    speaker names, resolves overlaps, merges/splits into turns, and
    REPLACES meeting's speaker_turns (delete-then-insert — idempotent,
    re-running never duplicates). Returns the number of turns written.

    Raises on failure (missing audio, pyannote error, etc.) — callers
    (worker/main.py, worker/cli.py) own status transitions and retry
    policy. Only flushes; caller commits."""
    if not meeting.audio_key:
        raise RuntimeError("meeting has no audio_key — nothing to diarize")

    storage = StorageClient(settings)
    with tempfile.TemporaryDirectory(prefix=f"diarize-{meeting.id}-") as tmp:
        tmp_dir = Path(tmp)
        wav_path = tmp_dir / "full.wav"
        download_and_concatenate_audio(storage, meeting.audio_key, wav_path, tmp_dir)

        event_data = SpeakerEventData(names={}, speaking_intervals=[])
        if meeting.speaker_events_key:
            speakers_path = tmp_dir / "speakers.jsonl"
            try:
                storage.download_file(meeting.speaker_events_key, speakers_path)
                event_data = parse_speaker_events(speakers_path)
            except Exception:
                logger.warning("diarization_speakers_jsonl_download_failed", meeting_id=str(meeting.id), exc_info=True)

        started = time.time()
        pyannote_segments = run_pyannote_diarization(wav_path, settings)
        elapsed = time.time() - started
        logger.info(
            "diarization_pyannote_complete",
            meeting_id=str(meeting.id),
            raw_segments=len(pyannote_segments),
            seconds=round(elapsed, 1),
            device=settings.diarization_device,
        )

        cluster_mapping, used_fallback = map_clusters_to_speakers(pyannote_segments, event_data)
        resolved = resolve_overlaps(pyannote_segments)
        assigned = assign_participants(meeting.id, resolved, event_data, cluster_mapping, used_fallback)

        # Merge twice, absorption in between: absorbing a micro-turn can
        # leave its surviving neighbor newly adjacent to another turn with
        # the same speaker identity (e.g. [A][tiny stray][A] -> after
        # absorption, two separate-but-now-touching A turns) that the
        # first merge pass had no reason to join.
        gap_s = settings.diarization_merge_gap_seconds
        merged = merge_turns_by_speaker(assigned, gap_s)
        absorbed = absorb_micro_turns(merged, settings.diarization_min_turn_seconds)
        remerged = merge_turns_by_speaker(absorbed, gap_s)
        chunked = split_long_turns(
            remerged,
            wav_path,
            settings.diarization_max_turn_seconds,
            settings.diarization_min_silence_seconds,
            settings.diarization_silence_threshold_db,
        )

        db.execute(delete(SpeakerTurn).where(SpeakerTurn.meeting_id == meeting.id))
        for turn in chunked:
            confidence = None if used_fallback else _compute_turn_confidence(turn, event_data)
            db.add(
                SpeakerTurn(
                    meeting_id=meeting.id,
                    cluster_label=turn.cluster_label,
                    participant_id=turn.participant_id,
                    speaker_name=turn.speaker_name,
                    mapping_confidence=confidence,
                    start_ms=int(round(turn.start * 1000)),
                    end_ms=int(round(turn.end * 1000)),
                    overlap=turn.overlap,
                )
            )

        meeting.diarization_name_mapping_suspect = used_fallback
        if used_fallback:
            logger.warning(
                "diarization_name_mapping_fallback",
                meeting_id=str(meeting.id),
                hint="speakers.jsonl had no speaking_start/stop events — used Speaker A/B/... labels",
            )

        db.flush()
        return len(chunked)
