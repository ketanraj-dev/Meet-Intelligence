# Uses Playwright's bundled Chromium (not Google Chrome) deliberately: Chrome
# ships amd64-only, Chromium has native arm64 builds too, and Meet doesn't
# need anything Chrome-only for basic audio/video. See README Phase 2 notes.
FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    xvfb x11-utils pulseaudio ffmpeg x11vnc dbus-x11 \
    wget gnupg ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY bot/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt
RUN playwright install --with-deps chromium

COPY common/ ./common/
COPY bot_pool/ ./bot_pool/
COPY bot/ ./bot/
RUN chmod +x bot/entrypoint.sh

ENV DISPLAY=:99
ENV PYTHONUNBUFFERED=1

ENTRYPOINT ["bot/entrypoint.sh"]
