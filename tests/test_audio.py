import math
import struct
import wave
from pathlib import Path

import pytest

from common.audio import concatenate_wav_files, download_and_concatenate_audio, extract_wav_slice


def _write_wav(path: Path, samples: list[int], rate: int = 8000) -> None:
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        wf.writeframes(struct.pack(f"<{len(samples)}h", *samples))


def _tone(duration_s: float, rate: int, amplitude: int = 12000, freq: float = 220.0) -> list[int]:
    n = int(duration_s * rate)
    return [int(amplitude * math.sin(2 * math.pi * freq * i / rate)) for i in range(n)]


# --- concatenate_wav_files --------------------------------------------------------


def test_concatenate_wav_files_preserves_order_and_total_duration(tmp_path):
    rate = 8000
    seg1 = tmp_path / "a.wav"
    seg2 = tmp_path / "b.wav"
    _write_wav(seg1, _tone(1.0, rate), rate)
    _write_wav(seg2, _tone(2.0, rate), rate)

    output = tmp_path / "out.wav"
    concatenate_wav_files([seg1, seg2], output)

    with wave.open(str(output), "rb") as wf:
        assert wf.getnframes() == 3 * rate
        assert wf.getframerate() == rate


def test_concatenate_wav_files_rejects_mismatched_format(tmp_path):
    seg1 = tmp_path / "a.wav"
    seg2 = tmp_path / "b.wav"
    _write_wav(seg1, _tone(1.0, 8000), rate=8000)
    _write_wav(seg2, _tone(1.0, 16000), rate=16000)

    with pytest.raises(ValueError):
        concatenate_wav_files([seg1, seg2], tmp_path / "out.wav")


def test_concatenate_wav_files_raises_on_empty_list(tmp_path):
    with pytest.raises(ValueError):
        concatenate_wav_files([], tmp_path / "out.wav")


# --- download_and_concatenate_audio ------------------------------------------------


class _FakeStorage:
    def __init__(self, files: dict[str, Path]):
        self._files = files

    def list_objects(self, prefix):
        return [k for k in self._files if k.startswith(prefix)]

    def download_file(self, key, local_path):
        Path(local_path).write_bytes(Path(self._files[key]).read_bytes())


def test_download_and_concatenate_audio_orders_segments_lexicographically(tmp_path):
    rate = 8000
    seg0 = tmp_path / "audio_00000.wav"
    seg1 = tmp_path / "audio_00001.wav"
    _write_wav(seg0, _tone(1.0, rate), rate)
    _write_wav(seg1, _tone(2.0, rate), rate)
    storage = _FakeStorage({"meetings/m1/audio/audio_00001.wav": seg1, "meetings/m1/audio/audio_00000.wav": seg0})

    output = tmp_path / "out.wav"
    dl_dir = tmp_path / "dl"
    dl_dir.mkdir()
    download_and_concatenate_audio(storage, "meetings/m1/audio/", output, dl_dir)

    with wave.open(str(output), "rb") as wf:
        assert wf.getnframes() == 3 * rate


def test_download_and_concatenate_audio_raises_when_no_segments_found(tmp_path):
    storage = _FakeStorage({})
    with pytest.raises(RuntimeError):
        download_and_concatenate_audio(storage, "meetings/missing/audio/", tmp_path / "out.wav", tmp_path / "dl")


# --- extract_wav_slice --------------------------------------------------------------


def test_extract_wav_slice_pulls_out_the_right_range(tmp_path):
    rate = 8000
    src = tmp_path / "full.wav"
    _write_wav(src, _tone(10.0, rate), rate)

    out = tmp_path / "slice.wav"
    extract_wav_slice(src, 2.0, 5.0, out)

    with wave.open(str(out), "rb") as wf:
        assert wf.getnframes() == pytest.approx(3 * rate, abs=1)
        assert wf.getframerate() == rate


def test_extract_wav_slice_clamps_to_file_bounds(tmp_path):
    rate = 8000
    src = tmp_path / "full.wav"
    _write_wav(src, _tone(5.0, rate), rate)

    out = tmp_path / "slice.wav"
    extract_wav_slice(src, 3.0, 100.0, out)  # end far past the file's actual length

    with wave.open(str(out), "rb") as wf:
        assert wf.getnframes() == pytest.approx(2 * rate, abs=1)
