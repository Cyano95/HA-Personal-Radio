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
# Boolean separat lesen: get_option macht aus false einen leeren String,
# den der :- Default wieder auf "an" drehen würde.
export ICY_METADATA="$(python3 -c "import json; print('1' if json.load(open('$OPTIONS')).get('icy_metadata', True) else '0')" 2>/dev/null || echo 1)"

echo "[Personal Radio] media_port=${MEDIA_PORT}  stream_port=${STREAM_PORT}  host='${MEDIA_HOST:-auto}'  content_type='${MEDIA_CONTENT_TYPE}'  max_song_minutes=${MAX_SONG_MINUTES}  no_repeat_hours=${NO_REPEAT_HOURS}  icy_metadata=${ICY_METADATA}"

# ── yt-dlp bei jedem Start aktualisieren ──────────────────────────────────
# YouTube ändert regelmäßig seine URL-Signaturen; eine veraltete yt-dlp-
# Version liefert dann Stream-URLs, die beim Abruf sofort 403 zurückgeben
# (Symptom: jeder Titel wird nach Sekunden übersprungen, Radio bleibt stumm).
# Schlägt das Update fehl (z.B. offline), läuft die vorhandene Version weiter.
echo "[Personal Radio] Aktualisiere yt-dlp…"
# Nightly-Kanal, wie von yt-dlp bei YouTube-Breakages empfohlen.
# WICHTIG: nur das nackte Paket "yt-dlp" (ohne Extras) — yt-dlp hat
# keine Pflicht-Abhängigkeiten, --pre kann so kein anderes Paket
# (z.B. httpx) auf eine inkompatible Vorabversion ziehen.
if timeout 120 pip install --no-cache-dir --upgrade --pre --quiet yt-dlp 2>/dev/null; then
    echo "[Personal Radio] yt-dlp aktuell: $(python3 -c 'import yt_dlp; print(yt_dlp.version.__version__)' 2>/dev/null || echo 'unbekannt')"
else
    echo "[Personal Radio] yt-dlp-Update fehlgeschlagen — verwende vorhandene Version: $(python3 -c 'import yt_dlp; print(yt_dlp.version.__version__)' 2>/dev/null || echo 'unbekannt')"
fi

exec python3 -m uvicorn backend.main:app \
    --host 0.0.0.0 \
    --port 8787 \
    --log-level info
