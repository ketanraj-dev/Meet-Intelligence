"""Shared WAV helpers — pure stdlib (wave + array), no ffmpeg/numpy/torch.

Used by both worker/diarization.py (Phase 3, concatenating a meeting's raw
recorded segments) and asr/transcription.py (Phase 4, slicing out each
speaker turn's audio for transcription). Kept here instead of duplicated
or cross-imported between those two packages, which run in separate
containers with deliberately non-overlapping heavy dependencies (pyannote
vs. transformers/faster-whisper) — see asr/transcription.py's module
docstring for why they're kept apart.
"""
import array
import math
import wave
from pathlib import Path

from common.storage import StorageClient


def concatenate_wav_files(paths: list[Path], output_path: Path) -> None:
    """Pure stdlib concatenation, in the given order. All inputs must share
    sample rate/channels/width — true for this project's segments
    (bot/bot.py's start_ffmpeg always encodes -ac 1 -ar 16000, 16-bit)."""
    if not paths:
        raise ValueError("no WAV files to concatenate")
    with wave.open(str(paths[0]), "rb") as first:
        params = first.getparams()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(output_path), "wb") as out:
        out.setparams(params)
        for path in paths:
            with wave.open(str(path), "rb") as seg:
                fmt = (seg.getnchannels(), seg.getsampwidth(), seg.getframerate())
                if fmt != (params.nchannels, params.sampwidth, params.framerate):
                    raise ValueError(f"{path} has a mismatched WAV format from {paths[0]}: {fmt}")
                out.writeframes(seg.readframes(seg.getnframes()))


def download_and_concatenate_audio(
    storage: StorageClient, audio_key_prefix: str, output_path: Path, tmp_dir: Path
) -> None:
    keys = sorted(storage.list_objects(audio_key_prefix))
    if not keys:
        raise RuntimeError(f"no audio segments found under prefix {audio_key_prefix!r}")
    local_paths = []
    for key in keys:
        local_path = tmp_dir / Path(key).name
        storage.download_file(key, local_path)
        local_paths.append(local_path)
    concatenate_wav_files(local_paths, output_path)


def extract_wav_slice(wav_path: Path, start_s: float, end_s: float, output_path: Path) -> None:
    """Writes [start_s, end_s) of wav_path out as its own WAV file — e.g.
    one speaker turn's audio, ready to hand to an ASR model. Clamped to
    the file's actual bounds; never raises on an out-of-range request."""
    with wave.open(str(wav_path), "rb") as wf:
        rate = wf.getframerate()
        start_frame = max(0, int(start_s * rate))
        end_frame = min(wf.getnframes(), int(end_s * rate))
        wf.setpos(start_frame)
        frames = wf.readframes(max(0, end_frame - start_frame))
        params = wf.getparams()

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(output_path), "wb") as out:
        out.setparams(params)
        out.writeframes(frames)


def wav_energy_db(wav_path: Path) -> float:
    """RMS energy of the whole file, in dBFS (0 dB = full-scale for the
    sample's bit depth). A cheap, dependency-free silence/noise gate for
    asr/transcription.py — stdlib `array`+`math`, not `audioop` (removed
    in Python 3.13, which this project's local dev venv runs — the asr
    container itself is still 3.11, but this needs to import cleanly in
    both)."""
    with wave.open(str(wav_path), "rb") as wf:
        width = wf.getsampwidth()
        frames = wf.readframes(wf.getnframes())
    if not frames:
        return -120.0
    if width == 2:
        samples = array.array("h")
        samples.frombytes(frames)
    elif width == 1:
        samples = array.array("b")
        samples.frombytes(frames)
    else:
        return 0.0  # unexpected width (project only ever produces 16-bit) — don't guess silent
    if not samples:
        return -120.0
    mean_square = sum(s * s for s in samples) / len(samples)
    if mean_square <= 0:
        return -120.0
    full_scale = float(1 << (8 * width - 1))
    return 20 * math.log10(math.sqrt(mean_square) / full_scale)
