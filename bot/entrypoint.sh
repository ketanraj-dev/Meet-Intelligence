#!/bin/sh
set -e

Xvfb "$DISPLAY" -screen 0 1280x800x24 -nolisten tcp &
for i in $(seq 1 20); do
  if xdpyinfo -display "$DISPLAY" >/dev/null 2>&1; then
    break
  fi
  sleep 0.25
done

dbus-daemon --system --fork 2>/dev/null || true

pulseaudio --start --exit-idle-time=-1
pactl load-module module-null-sink sink_name=meet_sink sink_properties=device.description=meet_sink
pactl set-default-sink meet_sink
pactl set-default-source meet_sink.monitor

# VNC is always needed for BOT_MODE=login (watch/drive the one-time Gmail
# sign-in). For the normal run mode it's off by default (saves a process
# and a port) but can be turned on with BOT_VNC=true to watch a real test
# live: `docker compose run --rm -p 5900:5900 -e BOT_VNC=true -e MEETING_ID=... bot`.
if [ "$BOT_MODE" = "login" ] || [ "$BOT_VNC" = "true" ]; then
  x11vnc -display "$DISPLAY" -forever -shared -nopw -rfbport 5900 -bg -o /tmp/x11vnc.log
fi

exec python -u -m bot.bot
