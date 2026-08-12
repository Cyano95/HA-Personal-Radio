#!/usr/bin/env bash
set -e

OPTIONS=/data/options.json

get_option() {
    python3 -c "import json; d=json.load(open('$OPTIONS')); print(d.get('$1','') or '')" 2>/dev/null || echo ""
}

export STATION_API_URL="$(get_option station_api_url)"
export STATION_API_TOKEN="$(get_option station_api_token)"
export STATION_API_USER="$(get_option station_api_user)"
export FANART_API_KEY="$(get_option fanart_api_key)"
export MEDIA_PORT="$(get_option media_port)"
export STREAM_PORT="$(get_option stream_port)"
export MEDIA_HOST="$(get_option media_host)"
export MEDIA_CONTENT_TYPE="$(get_option media_content_type)"
export MAX_SONG_MINUTES="$(get_option max_song_minutes)"
export NO_REPEAT_HOURS="$(get_option no_repeat_hours)"
export MEDIA_PORT="${MEDIA_PORT:-8788}"
export STREAM_PORT="${STREAM_PORT:-8789}"
export MEDIA_CONTENT_TYPE="${MEDIA_CONTENT_TYPE:-music}"
export MAX_SONG_MINUTES="${MAX_SONG_MINUTES:-0}"
export NO_REPEAT_HOURS="${NO_REPEAT_HOURS:-0}"

echo "[Personal Radio] media_port=${MEDIA_PORT}  stream_port=${STREAM_PORT}  host='${MEDIA_HOST:-auto}'  content_type='${MEDIA_CONTENT_TYPE}'  max_song_minutes=${MAX_SONG_MINUTES}  no_repeat_hours=${NO_REPEAT_HOURS}"

exec python3 -m uvicorn backend.main:app \
    --host 0.0.0.0 \
    --port 8787 \
    --log-level info
