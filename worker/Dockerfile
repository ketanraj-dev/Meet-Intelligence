# Processing worker (Phase 3 diarization, Phase 4 transcription later).
# No ffmpeg needed here — worker/diarization.py concatenates WAV segments
# with the stdlib `wave` module, not a subprocess. libsndfile1 is needed by
# pyannote.audio's audio-loading backend (soundfile); libgomp1 (GNU OpenMP
# runtime) is needed by torchaudio's compiled extension — confirmed live,
# its import fails with "libgomp.so.1: cannot open shared object file"
# without it (python:3.11-slim doesn't include it by default).
FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    libsndfile1 \
    libgomp1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY worker/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY common/ ./common/
COPY worker/ ./worker/

# Belt-and-suspenders alongside torch.set_num_threads(1) in
# worker/diarization.py — these cap the underlying BLAS (OpenMP/MKL)
# thread pools themselves, which torch's own setting doesn't always fully
# control, and have to be set before those libraries initialize (an
# in-Python os.environ[...] after numpy/torch are already imported is too
# late). Same CPU-memory-spike reasoning — see _get_pipeline's docstring.
ENV OMP_NUM_THREADS=1
ENV MKL_NUM_THREADS=1
ENV PYTHONUNBUFFERED=1

CMD ["python", "-u", "-m", "worker.main"]
