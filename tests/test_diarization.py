import json
import math
import struct
import wave
from pathlib import Path
from types import SimpleNamespace

import pytest

from worker.diarization import (
    ClusterMapping,
    SpeakerEventData,
    Turn,
    _compute_turn_confidence,
    _find_split_point,
    _nearest_participant,
    _rms_envelope,
    absorb_micro_turns,
    assign_participants,
    map_clusters_to_speakers,
    merge_turns_by_speaker,
    parse_speaker_events,
    process_meeting,
    resolve_overlaps,
    split_long_turns,
    split_turn_by_participant,
)


# --- WAV fixture helpers ------------------------------------------------------


def _write_wav(path: Path, samples: list[int], rate: int = 8000) -> None:
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        wf.writeframes(struct.pack(f"<{len(samples)}h", *samples))


def _tone(duration_s: float, rate: int, amplitude: int = 12000, freq: float = 220.0) -> list[int]:
    n = int(duration_s * rate)
    return [int(amplitude * math.sin(2 * math.pi * freq * i / rate)) for i in range(n)]


def _silence(duration_s: float, rate: int) -> list[int]:
    return [0] * int(duration_s * rate)


# --- parse_speaker_events --------------------------------------------------------


def test_parse_speaker_events_pairs_start_stop_into_intervals(tmp_path):
    path = tmp_path / "speakers.jsonl"
    path.write_text(
        "\n".join(
            [
                json.dumps({"t": 1.0, "event": "participant_joined", "participant_id": "p1", "name": "Ketan Raj"}),
                json.dumps({"t": 2.0, "event": "speaking_start", "participant_id": "p1", "name": "Ketan Raj"}),
                json.dumps({"t": 5.0, "event": "speaking_stop", "participant_id": "p1", "name": "Ketan Raj"}),
            ]
        )
    )
    data = parse_speaker_events(path)
    assert data.names == {"p1": "Ketan Raj"}
    assert data.speaking_intervals == [(2.0, 5.0, "p1")]


def test_parse_speaker_events_drops_unterminated_start(tmp_path):
    path = tmp_path / "speakers.jsonl"
    path.write_text(json.dumps({"t": 2.0, "event": "speaking_start", "participant_id": "p1", "name": "A"}) + "\n")
    data = parse_speaker_events(path)
    assert data.speaking_intervals == []


def test_parse_speaker_events_ignores_malformed_lines(tmp_path):
    path = tmp_path / "speakers.jsonl"
    path.write_text("not json\n" + json.dumps({"t": 1.0, "event": "speaking_start", "participant_id": "p1"}) + "\n")
    data = parse_speaker_events(path)  # must not raise
    assert data.speaking_intervals == []  # the start never got a matching stop


def test_parse_speaker_events_missing_file_returns_empty():
    data = parse_speaker_events(Path("/nonexistent/speakers.jsonl"))
    assert data.names == {}
    assert data.speaking_intervals == []


# --- map_clusters_to_speakers -----------------------------------------------------


def test_map_clusters_to_speakers_matches_by_overlap():
    pyannote_segments = [(0.0, 10.0, "SPEAKER_00"), (10.0, 20.0, "SPEAKER_01")]
    event_data = SpeakerEventData(
        names={"p1": "Ketan Raj", "p2": "Information Technology"},
        speaking_intervals=[(0.0, 10.0, "p1"), (10.0, 20.0, "p2")],
    )
    mapping, used_fallback = map_clusters_to_speakers(pyannote_segments, event_data)
    assert used_fallback is False
    assert mapping["SPEAKER_00"] == ClusterMapping("p1", "Ketan Raj", 1.0)
    assert mapping["SPEAKER_01"] == ClusterMapping("p2", "Information Technology", 1.0)


def test_map_clusters_to_speakers_partial_overlap_confidence():
    # SPEAKER_00 speaks 0-10s, but only 0-6s of that lines up with p1 — 60% confidence.
    pyannote_segments = [(0.0, 10.0, "SPEAKER_00")]
    event_data = SpeakerEventData(names={"p1": "A"}, speaking_intervals=[(0.0, 6.0, "p1")])
    mapping, _ = map_clusters_to_speakers(pyannote_segments, event_data)
    assert mapping["SPEAKER_00"].participant_id == "p1"
    assert mapping["SPEAKER_00"].mapping_confidence == pytest.approx(0.6)


def test_map_clusters_to_speakers_unmatched_cluster_becomes_unknown():
    pyannote_segments = [(0.0, 10.0, "SPEAKER_00"), (50.0, 55.0, "SPEAKER_01")]
    event_data = SpeakerEventData(names={"p1": "A"}, speaking_intervals=[(0.0, 10.0, "p1")])
    mapping, used_fallback = map_clusters_to_speakers(pyannote_segments, event_data)
    assert used_fallback is False
    assert mapping["SPEAKER_01"].participant_id is None
    assert mapping["SPEAKER_01"].speaker_name == "Unknown speaker 1"
    assert mapping["SPEAKER_01"].mapping_confidence == 0.0


def test_map_clusters_to_speakers_falls_back_to_letters_when_no_speaking_events():
    """Item 7: a broken speaking_indicator selector (or any reason
    speakers.jsonl has zero speaking events) must not block diarization —
    it falls back to Speaker A/B/... naming and flags the meeting."""
    pyannote_segments = [(0.0, 10.0, "SPEAKER_00"), (10.0, 20.0, "SPEAKER_01")]
    event_data = SpeakerEventData(names={}, speaking_intervals=[])
    mapping, used_fallback = map_clusters_to_speakers(pyannote_segments, event_data)
    assert used_fallback is True
    assert mapping["SPEAKER_00"] == ClusterMapping(None, "Speaker A", None)
    assert mapping["SPEAKER_01"] == ClusterMapping(None, "Speaker B", None)


def test_map_clusters_to_speakers_letter_sequence_past_z():
    segments = [(float(i), float(i) + 1, f"SPEAKER_{i:02d}") for i in range(27)]
    mapping, used_fallback = map_clusters_to_speakers(segments, SpeakerEventData({}, []))
    assert used_fallback is True
    assert mapping["SPEAKER_25"].speaker_name == "Speaker Z"
    assert mapping["SPEAKER_26"].speaker_name == "Speaker AA"


# --- resolve_overlaps --------------------------------------------------------------


def test_resolve_overlaps_passes_through_non_overlapping_segments():
    segments = [(0.0, 5.0, "A"), (5.0, 10.0, "B")]
    assert resolve_overlaps(segments) == [(0.0, 5.0, "A", False), (5.0, 10.0, "B", False)]


def test_resolve_overlaps_dominant_speaker_wins_the_overlap_region():
    # A: 0-10 (10s), B: 8-12 (4s) -- they overlap in [8,10]. A is longer overall.
    segments = [(0.0, 10.0, "A"), (8.0, 12.0, "B")]
    resolved = resolve_overlaps(segments)
    assert resolved == [
        (0.0, 8.0, "A", False),
        (8.0, 10.0, "A", True),
        (10.0, 12.0, "B", False),
    ]


def test_resolve_overlaps_fully_contained_short_segment_is_swallowed_but_edges_kept():
    # B is entirely inside A's span.
    segments = [(0.0, 20.0, "A"), (8.0, 10.0, "B")]
    resolved = resolve_overlaps(segments)
    assert resolved == [
        (0.0, 8.0, "A", False),
        (8.0, 10.0, "A", True),
        (10.0, 20.0, "A", False),
    ]


def test_resolve_overlaps_empty_input():
    assert resolve_overlaps([]) == []


# --- split_turn_by_participant / assign_participants (cluster splitting) --------


def _turn(start, end, label="SPEAKER_00", overlap=False, pid="p1", name="A", conf=None) -> Turn:
    return Turn(start, end, label, overlap, pid, name, conf)


def test_nearest_participant_picks_the_closer_interval():
    intervals = [(0.0, 10.0, "p1"), (50.0, 60.0, "p2")]
    assert _nearest_participant(20.0, intervals) == "p1"
    assert _nearest_participant(45.0, intervals) == "p2"


def test_nearest_participant_zero_distance_when_inside_an_interval():
    intervals = [(0.0, 10.0, "p1"), (50.0, 60.0, "p2")]
    assert _nearest_participant(55.0, intervals) == "p2"


def test_nearest_participant_none_when_no_intervals_at_all():
    assert _nearest_participant(10.0, []) is None


def test_split_turn_by_participant_whole_turn_one_speaker():
    intervals = [(0.0, 20.0, "p1")]
    pieces = split_turn_by_participant(0.0, 10.0, intervals)
    assert pieces == [(0.0, 10.0, "p1", True)]


def test_split_turn_by_participant_splits_at_the_real_speaker_change():
    """The actual bug this round fixes: a single pyannote cluster spanning
    a time range where TWO different participants really spoke must not
    end up labeled as only one of them for its whole span."""
    intervals = [(0.0, 10.0, "p1"), (10.0, 20.0, "p2")]
    pieces = split_turn_by_participant(0.0, 20.0, intervals)
    assert pieces == [(0.0, 10.0, "p1", True), (10.0, 20.0, "p2", True)]


def test_split_turn_by_participant_fills_a_gap_with_nearest_speaker():
    # p1 speaks 0-5, nobody 5-8 (gap), p2 speaks 8-12 — within one turn 0-12.
    intervals = [(0.0, 5.0, "p1"), (8.0, 12.0, "p2")]
    pieces = split_turn_by_participant(0.0, 12.0, intervals)
    assert pieces[0] == (0.0, 5.0, "p1", True)
    assert pieces[-1] == (8.0, 12.0, "p2", True)
    # the 5-8 gap piece is filled (not left unattributed) but flagged as
    # not a direct overlap — the nearest-neighbor call, not an observation.
    assert all(pid is not None for _lo, _hi, pid, _direct in pieces)
    gap_pieces = [p for p in pieces if p[0] >= 5.0 and p[1] <= 8.0]
    assert gap_pieces and all(not p[3] for p in gap_pieces)


def test_split_turn_by_participant_no_overlap_anywhere_uses_nearest():
    intervals = [(100.0, 110.0, "p1")]
    pieces = split_turn_by_participant(0.0, 10.0, intervals)
    assert pieces == [(0.0, 10.0, "p1", False)]


def test_assign_participants_logs_cluster_split(monkeypatch):
    import worker.diarization as diar_module

    logged = []
    monkeypatch.setattr(diar_module.logger, "info", lambda event, **kw: logged.append((event, kw)))

    resolved = [(0.0, 20.0, "SPEAKER_00", False)]
    event_data = SpeakerEventData(
        names={"p1": "Information Technology", "p2": "Ketan Raj"},
        speaking_intervals=[(0.0, 10.0, "p1"), (10.0, 20.0, "p2")],
    )
    cluster_mapping = {}  # unused when used_fallback is False

    turns = assign_participants("m1", resolved, event_data, cluster_mapping, used_fallback=False)

    assert [(t.start, t.end, t.speaker_name) for t in turns] == [
        (0.0, 10.0, "Information Technology"),
        (10.0, 20.0, "Ketan Raj"),
    ]
    split_logs = [kw for event, kw in logged if event == "cluster_split"]
    assert len(split_logs) == 1
    assert split_logs[0]["cluster_label"] == "SPEAKER_00"
    assert split_logs[0]["participants"] == {"Information Technology": 10.0, "Ketan Raj": 10.0}


def test_assign_participants_no_split_logged_for_single_speaker_cluster(monkeypatch):
    import worker.diarization as diar_module

    logged = []
    monkeypatch.setattr(diar_module.logger, "info", lambda event, **kw: logged.append((event, kw)))

    resolved = [(0.0, 10.0, "SPEAKER_00", False)]
    event_data = SpeakerEventData(names={"p1": "A"}, speaking_intervals=[(0.0, 10.0, "p1")])

    assign_participants("m1", resolved, event_data, {}, used_fallback=False)

    assert [event for event, _ in logged if event == "cluster_split"] == []


def test_assign_participants_whole_turn_under_fallback():
    resolved = [(0.0, 10.0, "SPEAKER_00", False)]
    cluster_mapping = {"SPEAKER_00": ClusterMapping(None, "Speaker A", None)}
    turns = assign_participants("m1", resolved, SpeakerEventData({}, []), cluster_mapping, used_fallback=True)
    assert turns == [Turn(0.0, 10.0, "SPEAKER_00", False, None, "Speaker A")]


# --- _compute_turn_confidence ------------------------------------------------------


def test_compute_turn_confidence_full_overlap_is_1():
    turn = _turn(0.0, 10.0, pid="p1")
    event_data = SpeakerEventData({}, [(0.0, 10.0, "p1")])
    assert _compute_turn_confidence(turn, event_data) == pytest.approx(1.0)


def test_compute_turn_confidence_partial_overlap():
    turn = _turn(0.0, 10.0, pid="p1")
    event_data = SpeakerEventData({}, [(0.0, 6.0, "p1")])
    assert _compute_turn_confidence(turn, event_data) == pytest.approx(0.6)


def test_compute_turn_confidence_zero_when_nearest_neighbor_filled():
    """A turn whose identity came purely from _nearest_participant (no
    direct overlap at all) must honestly report 0.0, not a guessed value."""
    turn = _turn(20.0, 25.0, pid="p1")
    event_data = SpeakerEventData({}, [(0.0, 10.0, "p1")])
    assert _compute_turn_confidence(turn, event_data) == 0.0


# --- merge_turns_by_speaker ---------------------------------------------------------


def test_merge_turns_by_speaker_merges_close_same_speaker_turns():
    turns = [_turn(0.0, 5.0), _turn(5.3, 8.0)]
    merged = merge_turns_by_speaker(turns, max_gap_seconds=0.5)
    assert [(t.start, t.end) for t in merged] == [(0.0, 8.0)]


def test_merge_turns_by_speaker_does_not_merge_across_the_threshold():
    turns = [_turn(0.0, 5.0), _turn(5.6, 8.0)]
    merged = merge_turns_by_speaker(turns, max_gap_seconds=0.5)
    assert [(t.start, t.end) for t in merged] == [(0.0, 5.0), (5.6, 8.0)]


def test_merge_turns_by_speaker_does_not_merge_different_speakers():
    turns = [_turn(0.0, 5.0, pid="p1", name="A"), _turn(5.1, 8.0, pid="p2", name="B")]
    merged = merge_turns_by_speaker(turns, max_gap_seconds=0.5)
    assert len(merged) == 2


def test_merge_turns_by_speaker_same_cluster_different_speaker_not_merged():
    """The actual fix: two turns can share a pyannote cluster_label but
    represent different real speakers (post-split) — must not merge."""
    turns = [
        _turn(0.0, 5.0, label="SPEAKER_00", pid="p1", name="A"),
        _turn(5.1, 8.0, label="SPEAKER_00", pid="p2", name="B"),
    ]
    merged = merge_turns_by_speaker(turns, max_gap_seconds=0.5)
    assert len(merged) == 2


def test_merge_turns_by_speaker_ors_the_overlap_flag():
    turns = [_turn(0.0, 5.0, overlap=True), _turn(5.1, 8.0, overlap=False)]
    merged = merge_turns_by_speaker(turns, max_gap_seconds=0.5)
    assert len(merged) == 1
    assert merged[0].overlap is True


# --- absorb_micro_turns -------------------------------------------------------------


def test_absorb_micro_turns_absorbs_into_previous_turn():
    turns = [_turn(0.0, 10.0, pid="p1", name="A"), _turn(10.0, 10.3, pid="p2", name="B")]
    absorbed = absorb_micro_turns(turns, min_turn_seconds=1.0)
    assert len(absorbed) == 1
    assert absorbed[0].start == 0.0
    assert absorbed[0].end == 10.3
    assert absorbed[0].speaker_name == "A"  # survivor keeps its own identity


def test_absorb_micro_turns_absorbs_into_next_when_first_turn_is_micro():
    turns = [_turn(0.0, 0.3, pid="p1", name="A"), _turn(0.3, 10.0, pid="p2", name="B")]
    absorbed = absorb_micro_turns(turns, min_turn_seconds=1.0)
    assert len(absorbed) == 1
    assert absorbed[0].start == 0.0
    assert absorbed[0].speaker_name == "B"


def test_absorb_micro_turns_leaves_normal_turns_alone():
    turns = [_turn(0.0, 10.0, pid="p1", name="A"), _turn(10.0, 20.0, pid="p2", name="B")]
    assert absorb_micro_turns(turns, min_turn_seconds=1.0) == turns


def test_absorb_micro_turns_handles_two_adjacent_micro_turns():
    turns = [
        _turn(0.0, 10.0, pid="p1", name="A"),
        _turn(10.0, 10.2, pid="p2", name="B"),
        _turn(10.2, 10.4, pid="p3", name="C"),
        _turn(10.4, 20.0, pid="p4", name="D"),
    ]
    absorbed = absorb_micro_turns(turns, min_turn_seconds=1.0)
    assert len(absorbed) == 2
    # both micro turns absorb into A (the previous turn), one after another
    assert absorbed[0].end == pytest.approx(10.4)
    assert absorbed[0].speaker_name == "A"
    assert absorbed[1].speaker_name == "D"


def test_absorb_micro_turns_empty_input():
    assert absorb_micro_turns([], min_turn_seconds=1.0) == []


# --- silence detection / long-turn splitting ------------------------------------


def test_rms_envelope_distinguishes_tone_from_silence(tmp_path):
    rate = 8000
    samples = _tone(1.0, rate) + _silence(1.0, rate) + _tone(1.0, rate)
    wav_path = tmp_path / "test.wav"
    _write_wav(wav_path, samples, rate)

    envelope = _rms_envelope(wav_path, 0.0, 3.0, window_s=0.1)
    loud = [db for t, db in envelope if t < 0.9]
    quiet = [db for t, db in envelope if 1.1 < t < 1.9]
    assert min(loud) > max(quiet)
    assert max(quiet) < -40.0


def test_find_split_point_lands_inside_the_silence_gap(tmp_path):
    rate = 8000
    samples = _tone(20.0, rate) + _silence(1.0, rate) + _tone(10.0, rate)
    wav_path = tmp_path / "test.wav"
    _write_wav(wav_path, samples, rate)

    split_at = _find_split_point(
        wav_path, start_s=0.0, end_s=31.0, max_chunk_s=30.0, min_silence_s=0.25, silence_threshold_db=-40.0
    )
    assert 20.0 <= split_at <= 21.0


def test_find_split_point_hard_cuts_when_no_silence_found(tmp_path):
    rate = 8000
    samples = _tone(35.0, rate)  # continuous tone, no silence anywhere
    wav_path = tmp_path / "test.wav"
    _write_wav(wav_path, samples, rate)

    split_at = _find_split_point(
        wav_path, start_s=0.0, end_s=35.0, max_chunk_s=30.0, min_silence_s=0.25, silence_threshold_db=-40.0
    )
    assert split_at == pytest.approx(30.0, abs=0.2)


def test_split_long_turns_leaves_short_turns_alone(tmp_path):
    wav_path = tmp_path / "test.wav"
    _write_wav(wav_path, _tone(5.0, 8000), 8000)
    turns = [_turn(0.0, 5.0)]
    assert split_long_turns(turns, wav_path, 30.0, 0.25, -40.0) == turns


def test_split_long_turns_splits_a_turn_longer_than_the_max(tmp_path):
    rate = 8000
    samples = _tone(20.0, rate) + _silence(1.0, rate) + _tone(10.0, rate)
    wav_path = tmp_path / "test.wav"
    _write_wav(wav_path, samples, rate)

    turns = [_turn(0.0, 31.0, name="A", pid="p1")]
    split = split_long_turns(turns, wav_path, max_chunk_s=30.0, min_silence_s=0.25, silence_threshold_db=-40.0)

    assert len(split) == 2
    assert split[0].start == 0.0
    assert split[1].end == 31.0
    assert split[0].end == split[1].start  # contiguous, no gap/overlap introduced
    assert 20.0 <= split[0].end <= 21.0  # cut landed in the silence
    assert split[0].speaker_name == split[1].speaker_name == "A"  # identity preserved across the split


# concatenate_wav_files moved to common/audio.py — see tests/test_audio.py.


# --- process_meeting orchestration (pyannote itself faked out) -------------------


class _FakeStorage:
    def __init__(self, files: dict[str, Path]):
        self._files = files  # key -> local source path to "download"

    def list_objects(self, prefix):
        return [k for k in self._files if k.startswith(prefix)]

    def download_file(self, key, local_path):
        Path(local_path).write_bytes(Path(self._files[key]).read_bytes())


def _settings_stub(**overrides):
    base = dict(
        diarization_model="pyannote/speaker-diarization-3.1",
        diarization_device="cpu",
        diarization_merge_gap_seconds=0.5,
        diarization_max_turn_seconds=30.0,
        diarization_min_silence_seconds=0.25,
        diarization_silence_threshold_db=-40.0,
        diarization_min_turn_seconds=1.0,
        hf_token="fake-token",
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def test_process_meeting_writes_turns_and_replaces_on_rerun(db_session, monkeypatch, tmp_path):
    from common.models import Meeting, SpeakerTurn

    rate = 8000
    seg_path = tmp_path / "audio_00000.wav"
    _write_wav(seg_path, _tone(10.0, rate), rate)
    speakers_path = tmp_path / "speakers.jsonl"
    speakers_path.write_text(
        "\n".join(
            [
                json.dumps({"t": 0.0, "event": "participant_joined", "participant_id": "p1", "name": "Ketan Raj"}),
                json.dumps({"t": 0.0, "event": "speaking_start", "participant_id": "p1", "name": "Ketan Raj"}),
                json.dumps({"t": 10.0, "event": "speaking_stop", "participant_id": "p1", "name": "Ketan Raj"}),
            ]
        )
    )

    fake_storage = _FakeStorage({"meetings/m1/audio/audio_00000.wav": seg_path, "meetings/m1/speakers.jsonl": speakers_path})
    monkeypatch.setattr("worker.diarization.StorageClient", lambda settings: fake_storage)
    monkeypatch.setattr(
        "worker.diarization.run_pyannote_diarization", lambda wav_path, settings: [(0.0, 10.0, "SPEAKER_00")]
    )

    meeting = Meeting(
        meet_url="https://meet.google.com/abc-defg-hij",
        scheduled_at=__import__("datetime").datetime(2026, 1, 1),
        audio_key="meetings/m1/audio/",
        speaker_events_key="meetings/m1/speakers.jsonl",
    )
    db_session.add(meeting)
    db_session.commit()

    turn_count = process_meeting(db_session, meeting, _settings_stub())
    db_session.commit()
    assert turn_count == 1

    turns = db_session.query(SpeakerTurn).filter(SpeakerTurn.meeting_id == meeting.id).all()
    assert len(turns) == 1
    assert turns[0].speaker_name == "Ketan Raj"
    assert turns[0].participant_id == "p1"
    assert turns[0].mapping_confidence == pytest.approx(1.0)
    assert meeting.diarization_name_mapping_suspect is False

    # Re-run: must replace, not duplicate.
    process_meeting(db_session, meeting, _settings_stub())
    db_session.commit()
    turns_after = db_session.query(SpeakerTurn).filter(SpeakerTurn.meeting_id == meeting.id).all()
    assert len(turns_after) == 1


def test_process_meeting_sets_fallback_flag_when_no_speaking_events(db_session, monkeypatch, tmp_path):
    from common.models import Meeting

    rate = 8000
    seg_path = tmp_path / "audio_00000.wav"
    _write_wav(seg_path, _tone(5.0, rate), rate)

    fake_storage = _FakeStorage({"meetings/m2/audio/audio_00000.wav": seg_path})
    monkeypatch.setattr("worker.diarization.StorageClient", lambda settings: fake_storage)
    monkeypatch.setattr(
        "worker.diarization.run_pyannote_diarization", lambda wav_path, settings: [(0.0, 5.0, "SPEAKER_00")]
    )

    meeting = Meeting(
        meet_url="https://meet.google.com/abc-defg-hij",
        scheduled_at=__import__("datetime").datetime(2026, 1, 1),
        audio_key="meetings/m2/audio/",
        speaker_events_key=None,
    )
    db_session.add(meeting)
    db_session.commit()

    process_meeting(db_session, meeting, _settings_stub())
    db_session.commit()

    assert meeting.diarization_name_mapping_suspect is True


def test_process_meeting_splits_one_dominant_cluster_across_two_real_speakers(db_session, monkeypatch, tmp_path):
    """End-to-end regression test for the actual live bug report: a single
    dominant pyannote cluster spanning a time range where TWO different
    participants really spoke (per speakers.jsonl) must produce two
    correctly-named turns, not one turn mislabeled with whichever
    participant happened to have slightly more overlap overall."""
    from common.models import Meeting, SpeakerTurn

    rate = 8000
    seg_path = tmp_path / "audio_00000.wav"
    _write_wav(seg_path, _tone(40.0, rate), rate)
    speakers_path = tmp_path / "speakers.jsonl"
    speakers_path.write_text(
        "\n".join(
            [
                json.dumps({"t": 0.0, "event": "participant_joined", "participant_id": "p1", "name": "Information Technology"}),
                json.dumps({"t": 0.0, "event": "participant_joined", "participant_id": "p2", "name": "Ketan Raj"}),
                json.dumps({"t": 0.0, "event": "speaking_start", "participant_id": "p1", "name": "Information Technology"}),
                json.dumps({"t": 20.0, "event": "speaking_stop", "participant_id": "p1", "name": "Information Technology"}),
                json.dumps({"t": 20.0, "event": "speaking_start", "participant_id": "p2", "name": "Ketan Raj"}),
                json.dumps({"t": 40.0, "event": "speaking_stop", "participant_id": "p2", "name": "Ketan Raj"}),
            ]
        )
    )

    fake_storage = _FakeStorage(
        {"meetings/m3/audio/audio_00000.wav": seg_path, "meetings/m3/speakers.jsonl": speakers_path}
    )
    monkeypatch.setattr("worker.diarization.StorageClient", lambda settings: fake_storage)
    # One dominant pyannote cluster spans the WHOLE call — exactly the
    # real-world shape that caused the bug (pyannote's own clustering gave
    # no hint that two different people were actually speaking).
    monkeypatch.setattr(
        "worker.diarization.run_pyannote_diarization", lambda wav_path, settings: [(0.0, 40.0, "SPEAKER_00")]
    )

    meeting = Meeting(
        meet_url="https://meet.google.com/abc-defg-hij",
        scheduled_at=__import__("datetime").datetime(2026, 1, 1),
        audio_key="meetings/m3/audio/",
        speaker_events_key="meetings/m3/speakers.jsonl",
    )
    db_session.add(meeting)
    db_session.commit()

    process_meeting(db_session, meeting, _settings_stub())
    db_session.commit()

    turns = db_session.query(SpeakerTurn).filter(SpeakerTurn.meeting_id == meeting.id).order_by(SpeakerTurn.start_ms).all()
    assert [(t.speaker_name, t.start_ms, t.end_ms) for t in turns] == [
        ("Information Technology", 0, 20000),
        ("Ketan Raj", 20000, 40000),
    ]
    assert all(t.mapping_confidence == pytest.approx(1.0) for t in turns)
    assert meeting.diarization_name_mapping_suspect is False


def test_process_meeting_raises_without_audio_key(db_session):
    from common.models import Meeting

    meeting = Meeting(
        meet_url="https://meet.google.com/abc-defg-hij",
        scheduled_at=__import__("datetime").datetime(2026, 1, 1),
        audio_key=None,
    )
    db_session.add(meeting)
    db_session.commit()

    with pytest.raises(RuntimeError):
        process_meeting(db_session, meeting, _settings_stub())
