import math
import struct
import wave
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from asr.transcription import (
    TurnLanguageDecision,
    choose_backend,
    decide_turn_language,
    transcribe_meeting,
)
from common.models import Meeting, SpeakerTurn, TranscriptSegment


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


def _settings_stub(**overrides):
    base = dict(
        transcription_device="cpu",
        indic_transcribe_model_id="bodhan-ai/indic-transcribe-flex",
        indic_transcribe_mode="mixed",
        indic_transcribe_languages=["hi", "en", "bn", "ta"],
        whisper_model_size="small",
        whisper_compute_type="int8",
        default_language="hi",
        en_lid_confidence_threshold=0.85,
        transcription_silence_threshold_db=-40.0,
        transcription_min_words_per_second=0.3,
        transcription_max_words_per_second=4.0,
        asr_num_threads=8,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


class _FakeWhisper:
    """Fakes WhisperASR's two methods — detect_language_topk returns
    whatever topk list was queued, in order, one per call."""

    def __init__(self, topk_results: list[list[tuple[str, float]]]):
        self._topk_results = list(topk_results)
        self.detect_calls = []
        self.transcribe_calls = []

    def detect_language_topk(self, wav_path):
        self.detect_calls.append(wav_path)
        return self._topk_results.pop(0)

    def transcribe(self, wav_path, language):
        self.transcribe_calls.append((wav_path, language))
        return f"[whisper:{language}] transcribed text", False


class _FakeIndic:
    def __init__(self, texts: list[tuple[str, bool]] | None = None):
        self.transcribe_calls = []
        self._texts = list(texts) if texts is not None else None

    def transcribe(self, wav_path, language):
        self.transcribe_calls.append((wav_path, language))
        if self._texts is not None:
            return self._texts.pop(0)
        return f"[indic:{language}] transcribed text", False


def _turn(meeting_id, start_ms, end_ms, pid="p1", name="A", overlap=False):
    return SpeakerTurn(
        meeting_id=meeting_id,
        cluster_label="SPEAKER_00",
        participant_id=pid,
        speaker_name=name,
        mapping_confidence=1.0,
        start_ms=start_ms,
        end_ms=end_ms,
        overlap=overlap,
    )


# --- decide_turn_language ------------------------------------------------------


def test_decide_turn_language_uses_en_only_above_confidence_threshold(tmp_path):
    meeting = Meeting(meet_url="https://meet.google.com/x", scheduled_at=datetime(2026, 1, 1), expected_languages=None)
    whisper = _FakeWhisper([[("en", 0.9), ("hi", 0.05)]])
    decision = decide_turn_language(meeting, tmp_path / "turn.wav", _settings_stub(), whisper)
    assert decision == TurnLanguageDecision(
        detected="en", detected_probability=0.9, topk=[("en", 0.9), ("hi", 0.05)], used="en", source="lid_en_high_confidence"
    )


def test_decide_turn_language_falls_back_to_default_when_en_confidence_too_low(tmp_path):
    meeting = Meeting(meet_url="https://meet.google.com/x", scheduled_at=datetime(2026, 1, 1), expected_languages=None)
    whisper = _FakeWhisper([[("en", 0.6), ("hi", 0.3)]])
    decision = decide_turn_language(meeting, tmp_path / "turn.wav", _settings_stub(), whisper)
    assert decision.used == "hi"  # settings.default_language, not the raw LID result
    assert decision.detected == "en"
    assert decision.detected_probability == 0.6
    assert decision.source == "default_indic_mixed_mode"


def test_decide_turn_language_falls_back_to_default_for_a_confident_non_english_result(tmp_path):
    """LID is only ever trusted for the binary en/not-en call — a
    confident "ta" result still routes to the Indic default, not "ta"."""
    meeting = Meeting(meet_url="https://meet.google.com/x", scheduled_at=datetime(2026, 1, 1), expected_languages=None)
    whisper = _FakeWhisper([[("ta", 0.95)]])
    decision = decide_turn_language(meeting, tmp_path / "turn.wav", _settings_stub(), whisper)
    assert decision.used == "hi"
    assert decision.detected == "ta"
    assert decision.source == "default_indic_mixed_mode"


def test_decide_turn_language_restricts_en_to_expected_languages(tmp_path):
    meeting = Meeting(
        meet_url="https://meet.google.com/x", scheduled_at=datetime(2026, 1, 1), expected_languages=["ta"]
    )
    whisper = _FakeWhisper([[("en", 0.99)]])
    decision = decide_turn_language(meeting, tmp_path / "turn.wav", _settings_stub(), whisper)
    assert decision.used == "ta"  # "en" not in expected_languages, so excluded even at high confidence
    assert decision.source == "default_indic_mixed_mode"


def test_decide_turn_language_expected_languages_overrides_default_indic_fallback(tmp_path):
    """default_language="hi" isn't in expected_languages, so the Indic
    fallback becomes the first non-English expected language instead."""
    meeting = Meeting(
        meet_url="https://meet.google.com/x", scheduled_at=datetime(2026, 1, 1), expected_languages=["ta", "en"]
    )
    whisper = _FakeWhisper([[("hi", 0.9)]])
    decision = decide_turn_language(meeting, tmp_path / "turn.wav", _settings_stub(), whisper)
    assert decision.used == "ta"


def test_decide_turn_language_allows_en_when_in_expected_languages(tmp_path):
    meeting = Meeting(
        meet_url="https://meet.google.com/x", scheduled_at=datetime(2026, 1, 1), expected_languages=["hi", "en"]
    )
    whisper = _FakeWhisper([[("en", 0.9)]])
    decision = decide_turn_language(meeting, tmp_path / "turn.wav", _settings_stub(), whisper)
    assert decision.used == "en"
    assert decision.source == "lid_en_high_confidence"


# --- choose_backend -----------------------------------------------------------


def test_choose_backend_routes_supported_language_to_indic():
    indic, whisper = _FakeIndic(), _FakeWhisper([])
    settings = _settings_stub()
    assert choose_backend("hi", settings, indic, whisper) is indic


def test_choose_backend_routes_unsupported_language_to_whisper():
    indic, whisper = _FakeIndic(), _FakeWhisper([])
    settings = _settings_stub()
    assert choose_backend("fr", settings, indic, whisper) is whisper


# --- transcribe_meeting orchestration --------------------------------------------


class _FakeStorage:
    def __init__(self, files: dict[str, Path]):
        self._files = files

    def list_objects(self, prefix):
        return [k for k in self._files if k.startswith(prefix)]

    def download_file(self, key, local_path):
        Path(local_path).write_bytes(Path(self._files[key]).read_bytes())


def test_transcribe_meeting_writes_segments_and_replaces_on_rerun(db_session, monkeypatch, tmp_path):
    rate = 8000
    seg_path = tmp_path / "audio_00000.wav"
    _write_wav(seg_path, _tone(20.0, rate), rate)

    fake_storage = _FakeStorage({"meetings/m1/audio/audio_00000.wav": seg_path})
    monkeypatch.setattr("asr.transcription.StorageClient", lambda settings: fake_storage)

    fake_whisper = _FakeWhisper([[("hi", 0.9)], [("hi", 0.9)]])
    fake_indic = _FakeIndic()
    monkeypatch.setattr("asr.transcription.WhisperASR", lambda *a, **k: fake_whisper)
    monkeypatch.setattr("asr.transcription.IndicTranscribeASR", lambda *a, **k: fake_indic)

    meeting = Meeting(
        meet_url="https://meet.google.com/abc-defg-hij",
        scheduled_at=datetime(2026, 1, 1),
        audio_key="meetings/m1/audio/",
        expected_languages=None,
    )
    db_session.add(meeting)
    db_session.commit()
    db_session.add(_turn(meeting.id, 0, 10000, pid="p1", name="Ketan Raj"))
    db_session.add(_turn(meeting.id, 10000, 20000, pid="p1", name="Ketan Raj"))
    db_session.commit()

    settings = _settings_stub()
    count = transcribe_meeting(db_session, meeting, settings)
    db_session.commit()
    assert count == 2

    segments = db_session.query(TranscriptSegment).filter(TranscriptSegment.meeting_id == meeting.id).all()
    assert len(segments) == 2
    assert all(s.language == "hi" for s in segments)
    assert all(s.language_source == "default_indic_mixed_mode" for s in segments)
    assert all(s.lid_topk == [["hi", 0.9]] for s in segments)
    assert all(s.text.startswith("[indic:hi]") for s in segments)
    assert all(s.asr_model.startswith("indic_transcribe:") for s in segments)
    assert all(s.low_confidence is False for s in segments)

    # Re-run: must replace, not duplicate.
    fake_whisper._topk_results = [[("hi", 0.9)], [("hi", 0.9)]]
    transcribe_meeting(db_session, meeting, settings)
    db_session.commit()
    segments_after = db_session.query(TranscriptSegment).filter(TranscriptSegment.meeting_id == meeting.id).all()
    assert len(segments_after) == 2


def test_transcribe_meeting_decides_language_per_turn_not_per_speaker(db_session, monkeypatch, tmp_path):
    """The same speaker's turns can land on different languages — the
    whole point of the per-turn (not per-speaker) redesign."""
    rate = 8000
    seg_path = tmp_path / "audio_00000.wav"
    _write_wav(seg_path, _tone(20.0, rate), rate)

    fake_storage = _FakeStorage({"meetings/m5/audio/audio_00000.wav": seg_path})
    monkeypatch.setattr("asr.transcription.StorageClient", lambda settings: fake_storage)

    # Same speaker, two turns: first is confidently English, second isn't.
    fake_whisper = _FakeWhisper([[("en", 0.95)], [("hi", 0.5)]])
    fake_indic = _FakeIndic()
    monkeypatch.setattr("asr.transcription.WhisperASR", lambda *a, **k: fake_whisper)
    monkeypatch.setattr("asr.transcription.IndicTranscribeASR", lambda *a, **k: fake_indic)

    meeting = Meeting(
        meet_url="https://meet.google.com/abc-defg-hij",
        scheduled_at=datetime(2026, 1, 1),
        audio_key="meetings/m5/audio/",
        expected_languages=None,
    )
    db_session.add(meeting)
    db_session.commit()
    db_session.add(_turn(meeting.id, 0, 10000, pid="p1", name="Ketan Raj"))
    db_session.add(_turn(meeting.id, 10000, 20000, pid="p1", name="Ketan Raj"))
    db_session.commit()

    transcribe_meeting(db_session, meeting, _settings_stub())
    db_session.commit()

    segments = (
        db_session.query(TranscriptSegment).filter(TranscriptSegment.meeting_id == meeting.id).order_by(TranscriptSegment.start_ms).all()
    )
    assert [s.language for s in segments] == ["en", "hi"]


def test_transcribe_meeting_limit_caps_turns_and_replaces_fully(db_session, monkeypatch, tmp_path):
    rate = 8000
    seg_path = tmp_path / "audio_00000.wav"
    _write_wav(seg_path, _tone(20.0, rate), rate)

    fake_storage = _FakeStorage({"meetings/m4/audio/audio_00000.wav": seg_path})
    monkeypatch.setattr("asr.transcription.StorageClient", lambda settings: fake_storage)

    fake_whisper = _FakeWhisper([[("hi", 0.9)], [("hi", 0.9)]])
    fake_indic = _FakeIndic()
    monkeypatch.setattr("asr.transcription.WhisperASR", lambda *a, **k: fake_whisper)
    monkeypatch.setattr("asr.transcription.IndicTranscribeASR", lambda *a, **k: fake_indic)

    meeting = Meeting(
        meet_url="https://meet.google.com/abc-defg-hij",
        scheduled_at=datetime(2026, 1, 1),
        audio_key="meetings/m4/audio/",
        expected_languages=None,
    )
    db_session.add(meeting)
    db_session.commit()
    for i in range(5):
        db_session.add(_turn(meeting.id, i * 1000, i * 1000 + 500, pid="p1", name="Ketan Raj"))
    db_session.commit()

    count = transcribe_meeting(db_session, meeting, _settings_stub(), limit=2)
    db_session.commit()
    assert count == 2
    assert len(fake_indic.transcribe_calls) == 2

    segments = db_session.query(TranscriptSegment).filter(TranscriptSegment.meeting_id == meeting.id).all()
    assert len(segments) == 2


def test_transcribe_meeting_routes_expected_language_outside_the_27_to_whisper(db_session, monkeypatch, tmp_path):
    rate = 8000
    seg_path = tmp_path / "audio_00000.wav"
    _write_wav(seg_path, _tone(10.0, rate), rate)

    fake_storage = _FakeStorage({"meetings/m2/audio/audio_00000.wav": seg_path})
    monkeypatch.setattr("asr.transcription.StorageClient", lambda settings: fake_storage)

    fake_whisper = _FakeWhisper([[("fr", 0.9)]])
    fake_indic = _FakeIndic()
    monkeypatch.setattr("asr.transcription.WhisperASR", lambda *a, **k: fake_whisper)
    monkeypatch.setattr("asr.transcription.IndicTranscribeASR", lambda *a, **k: fake_indic)

    meeting = Meeting(
        meet_url="https://meet.google.com/abc-defg-hij",
        scheduled_at=datetime(2026, 1, 1),
        audio_key="meetings/m2/audio/",
        expected_languages=["fr"],  # explicitly named, and outside indic_transcribe_languages
    )
    db_session.add(meeting)
    db_session.commit()
    db_session.add(_turn(meeting.id, 0, 10000, pid="p1", name="A"))
    db_session.commit()

    transcribe_meeting(db_session, meeting, _settings_stub())
    db_session.commit()

    segments = db_session.query(TranscriptSegment).filter(TranscriptSegment.meeting_id == meeting.id).all()
    assert segments[0].language == "fr"
    assert segments[0].text.startswith("[whisper:fr]")
    assert segments[0].asr_model.startswith("whisper:")
    assert fake_indic.transcribe_calls == []


def test_transcribe_meeting_skips_silent_turn_without_running_lid_or_asr(db_session, monkeypatch, tmp_path):
    rate = 8000
    seg_path = tmp_path / "audio_00000.wav"
    _write_wav(seg_path, _silence(10.0, rate), rate)

    fake_storage = _FakeStorage({"meetings/m6/audio/audio_00000.wav": seg_path})
    monkeypatch.setattr("asr.transcription.StorageClient", lambda settings: fake_storage)

    fake_whisper = _FakeWhisper([])  # must never be called
    fake_indic = _FakeIndic()
    monkeypatch.setattr("asr.transcription.WhisperASR", lambda *a, **k: fake_whisper)
    monkeypatch.setattr("asr.transcription.IndicTranscribeASR", lambda *a, **k: fake_indic)

    meeting = Meeting(
        meet_url="https://meet.google.com/abc-defg-hij",
        scheduled_at=datetime(2026, 1, 1),
        audio_key="meetings/m6/audio/",
        expected_languages=None,
    )
    db_session.add(meeting)
    db_session.commit()
    db_session.add(_turn(meeting.id, 0, 10000, pid="p1", name="A"))
    db_session.commit()

    transcribe_meeting(db_session, meeting, _settings_stub())
    db_session.commit()

    segments = db_session.query(TranscriptSegment).filter(TranscriptSegment.meeting_id == meeting.id).all()
    assert segments[0].text == ""
    assert segments[0].language is None
    assert segments[0].language_source == "skipped_silence_or_noise"
    assert segments[0].low_confidence is True
    assert segments[0].low_confidence_reason == "silence_or_noise"
    assert fake_whisper.detect_calls == []
    assert fake_indic.transcribe_calls == []


def test_transcribe_meeting_flags_repetition_detected_as_low_confidence(db_session, monkeypatch, tmp_path):
    rate = 8000
    seg_path = tmp_path / "audio_00000.wav"
    _write_wav(seg_path, _tone(10.0, rate), rate)

    fake_storage = _FakeStorage({"meetings/m7/audio/audio_00000.wav": seg_path})
    monkeypatch.setattr("asr.transcription.StorageClient", lambda settings: fake_storage)

    fake_whisper = _FakeWhisper([[("hi", 0.9)]])
    # 10s turn, ~2 words/sec is plenty for 10s — text length itself stays
    # proportionate; only the repetition flag should fire here.
    fake_indic = _FakeIndic(texts=[("word " * 20, True)])
    monkeypatch.setattr("asr.transcription.WhisperASR", lambda *a, **k: fake_whisper)
    monkeypatch.setattr("asr.transcription.IndicTranscribeASR", lambda *a, **k: fake_indic)

    meeting = Meeting(
        meet_url="https://meet.google.com/abc-defg-hij",
        scheduled_at=datetime(2026, 1, 1),
        audio_key="meetings/m7/audio/",
        expected_languages=None,
    )
    db_session.add(meeting)
    db_session.commit()
    db_session.add(_turn(meeting.id, 0, 10000, pid="p1", name="A"))
    db_session.commit()

    transcribe_meeting(db_session, meeting, _settings_stub())
    db_session.commit()

    segments = db_session.query(TranscriptSegment).filter(TranscriptSegment.meeting_id == meeting.id).all()
    assert segments[0].low_confidence is True
    assert "repetition_detected" in segments[0].low_confidence_reason


def test_transcribe_meeting_flags_length_disproportionate_as_low_confidence(db_session, monkeypatch, tmp_path):
    rate = 8000
    seg_path = tmp_path / "audio_00000.wav"
    _write_wav(seg_path, _tone(10.0, rate), rate)

    fake_storage = _FakeStorage({"meetings/m8/audio/audio_00000.wav": seg_path})
    monkeypatch.setattr("asr.transcription.StorageClient", lambda settings: fake_storage)

    fake_whisper = _FakeWhisper([[("hi", 0.9)]])
    # 10s turn but only one word — far below transcription_min_words_per_second.
    fake_indic = _FakeIndic(texts=[("ok", False)])
    monkeypatch.setattr("asr.transcription.WhisperASR", lambda *a, **k: fake_whisper)
    monkeypatch.setattr("asr.transcription.IndicTranscribeASR", lambda *a, **k: fake_indic)

    meeting = Meeting(
        meet_url="https://meet.google.com/abc-defg-hij",
        scheduled_at=datetime(2026, 1, 1),
        audio_key="meetings/m8/audio/",
        expected_languages=None,
    )
    db_session.add(meeting)
    db_session.commit()
    db_session.add(_turn(meeting.id, 0, 10000, pid="p1", name="A"))
    db_session.commit()

    transcribe_meeting(db_session, meeting, _settings_stub())
    db_session.commit()

    segments = db_session.query(TranscriptSegment).filter(TranscriptSegment.meeting_id == meeting.id).all()
    assert segments[0].low_confidence is True
    assert "length_disproportionate" in segments[0].low_confidence_reason


def test_transcribe_meeting_raises_with_no_speaker_turns(db_session):
    meeting = Meeting(
        meet_url="https://meet.google.com/x",
        scheduled_at=datetime(2026, 1, 1),
        audio_key="meetings/m3/audio/",
    )
    db_session.add(meeting)
    db_session.commit()

    with pytest.raises(RuntimeError, match="speaker_turns"):
        transcribe_meeting(db_session, meeting, _settings_stub())


def test_transcribe_meeting_raises_without_audio_key(db_session):
    meeting = Meeting(meet_url="https://meet.google.com/x", scheduled_at=datetime(2026, 1, 1), audio_key=None)
    db_session.add(meeting)
    db_session.commit()
    db_session.add(_turn(meeting.id, 0, 1000))
    db_session.commit()

    with pytest.raises(RuntimeError, match="audio_key"):
        transcribe_meeting(db_session, meeting, _settings_stub())
