#!/bin/sh
set -eu
export DIRECT_ONLY=true DISABLE_DHT=true DISABLE_STARTUP_TASKS=true
export FFMPEG_PATH=/app/deny-media-tool.sh FFPROBE_PATH=/app/deny-media-tool.sh
exec /app/remux-server "$@"
