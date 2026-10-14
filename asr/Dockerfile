# ASR (transcription) worker — Phase 4. Deliberately separate from
# worker/Dockerfile (Phase 3's diarization): pyannote and the ASR backends
# here must never be loaded in the same container/process at once — see
# asr/transcription.py's module docstring.
FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    libsndfile1 \
    libgomp1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY asr/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY common/ ./common/
COPY asr/ ./asr/

ENV PYTHONUNBUFFERED=1

CMD ["python", "-u", "-m", "asr.main"]
