"""
Personal Radio — FastAPI application.

Two servers:
  • Port 8787 (ingress): Web UI + REST API + Webhooks
  • Port MEDIA_PORT:     Stream server + MP3 file serving (direct host access)

Stream architecture (replaces per-file MP3 playback):
  • Each user has a persistent HTTP audio/mpeg stream at /stream/{uid}?token=…
  • The stream producer handles crossfading, song advancement, and queue management.
  • HA media player connects to the stream URL once; playback is continuous.
  • The producer stops automatically when the last HTTP client disconnects.
"""
from __future__ import annotations

import asyncio
import pathlib
import logging
import os
import time
from pathlib import Path

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.applications import Starlette
from starlette.routing import Route

from . import storage
from .api_client import StationAPIClient
from .downloader import prefetch_for_user
from .mp3_server import serve_mp3, health as mp3_health
from .player import (
    _fire_event,
    get_ha_host,
    get_media_players,
    get_player_state,
    play_stream,
    set_volume as ha_set_volume,
    stop_playback as ha_stop_playback,
)
from .queue_manager import (
    advance_queue,
    apply_station_change,
    ensure_queue_has_entries,
    get_current_song,
    get_user_error,
    get_user_lock,
    go_to_previous,
    mark_song_played,
    next_is_ready,
    set_user_error,
    unmark_unplayed,
)
from .icy_server import start_icy_server
from .stream_server import (
    get_or_create_stream,
    set_stream_callbacks,
    stop_stream,
    stream_routes,
)
from .webhooks import router as webhook_router, set_playback_engine

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger("personal_radio.main")

app = FastAPI(title="Personal Radio", docs_url=None, redoc_url=None)
app.include_router(webhook_router)

FRONTEND_DIR = Path("/app/frontend")

# ---------------------------------------------------------------------------
# index.html with injected <base href> for HA Ingress path rewriting
# ---------------------------------------------------------------------------

_INDEX_TEMPLATE: str | None = None


def _get_index_html() -> str:
    global _INDEX_TEMPLATE
    if _INDEX_TEMPLATE is None:
        _INDEX_TEMPLATE = (FRONTEND_DIR / "index.html").read_text(encoding="utf-8")
    return _INDEX_TEMPLATE


@app.get("/")
async def index(request: Request):
    ingress_path = request.headers.get("X-Ingress-Path", "").rstrip("/")
    base = f"{ingress_path}/" if ingress_path else "./"
    html = _get_index_html().replace(
        "<head>",
        f'<head>\n  <base href="{base}" />',
        1,
    )
    return HTMLResponse(html)


if FRONTEND_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(FRONTEND_DIR)), name="static")


# ---------------------------------------------------------------------------
# Single instance — es gibt keine Benutzer mehr.
# Alle Zugriffe (Ingress, direkter Port, Webhooks) teilen sich eine Instanz.
# ---------------------------------------------------------------------------

INSTANCE_ID = "local"


def get_user_id(request: Request) -> str | None:
    return INSTANCE_ID


def require_user(request: Request) -> str:
    return INSTANCE_ID


# ---------------------------------------------------------------------------
# Playback engine
# ---------------------------------------------------------------------------

_engine_play_locks: dict[str, asyncio.Lock] = {}


def _engine_play_lock(uid: str) -> asyncio.Lock:
    if uid not in _engine_play_locks:
        _engine_play_locks[uid] = asyncio.Lock()
    return _engine_play_locks[uid]


class PlaybackEngine:
    async def start_playback(self, uid: str) -> bool:
        lock = _engine_play_lock(uid)
        if lock.locked():
            logger.debug("[%s] start_playback already in progress, skipping duplicate", uid)
            return False

        async with lock:
            state = storage.read_user_state(uid)
            if state.get("is_playing"):
                logger.debug("[%s] Already playing, ignoring duplicate start_playback", uid)
                return True

            # Resolve just 1 song so playback starts immediately;
            # the rest are filled in the background while the first plays.
            await ensure_queue_has_entries(uid, count=1)
            song = get_current_song(uid)
            if not song:
                set_user_error(uid, "No songs available in queue.")
                return False

            # Create/resume the stream producer (it starts on first subscriber)
            await get_or_create_stream(uid)

            # Tell HA to play the stream URL; HA connects → subscriber count rises
            ok = await play_stream(uid)
            if ok:
                async with get_user_lock(uid):
                    state = storage.read_user_state(uid)
                    state["is_playing"] = True
                    storage.write_user_state(uid, state)
                # Genau einen Song im Voraus bereithalten
                asyncio.create_task(ensure_queue_has_entries(uid, count=1))
                asyncio.create_task(self._prefetch(uid))
            return ok

    async def stop_playback(self, uid: str) -> None:
        # Stop the stream producer (also fires when last HA client disconnects)
        stop_stream(uid)
        # Tell HA to stop playing
        await ha_stop_playback(uid)

    async def skip_song(self, uid: str) -> bool:
        """Skip the current song; the stream crossfades immediately to the next."""
        from .stream_server import _streams
        stream = _streams.get(uid)
        if stream and not stream.stopped:
            stream.skip()
        # is_playing stays True — stream continues
        await _fire_event("personal_radio_skipped", {"ha_user_id": uid})
        return True

    async def prev_song(self, uid: str) -> bool:
        """Jump back to the previous song."""
        prev = await go_to_previous(uid)
        if not prev:
            return False
        # Force-skip current → stream will pick up the re-inserted previous song
        from .stream_server import _streams
        stream = _streams.get(uid)
        if stream and not stream.stopped:
            stream.skip()
        return True

    async def set_volume(self, uid: str, volume: float) -> None:
        await ha_set_volume(uid, volume)

    async def _prefetch(self, uid: str) -> None:
        state = storage.read_user_state(uid)
        queue = state.get("queue", [])
        idx   = state.get("current_index", 0)
        await prefetch_for_user(uid, queue, idx)
        async with get_user_lock(uid):
            state2 = storage.read_user_state(uid)
            for i, item in enumerate(queue):
                if i < len(state2["queue"]):
                    state2["queue"][i] = item
            storage.write_user_state(uid, state2)


engine = PlaybackEngine()
set_playback_engine(engine)

api_client = StationAPIClient()


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------

def _cleanup_user_dirs() -> None:
    """
    Es gibt nur noch eine Instanz ('local'). Alte Benutzerverzeichnisse aus
    Multi-User-Installationen werden entfernt — inkl. ihrer Media-Tokens,
    die sonst weiterhin gültig wären. Existiert 'local' noch nicht, wird
    das zuletzt genutzte alte Profil (Senderauswahl, History, Token)
    dorthin migriert.
    """
    import shutil

    if not storage.USERS_DIR.exists():
        return
    dirs   = [d for d in storage.USERS_DIR.iterdir() if d.is_dir()]
    others = [d for d in dirs if d.name != INSTANCE_ID]
    if not others:
        return

    local = storage.USERS_DIR / INSTANCE_ID
    if not local.exists():
        def _mtime(d: Path) -> float:
            try:
                return (d / "state.json").stat().st_mtime
            except OSError:
                return d.stat().st_mtime
        newest = max(others, key=_mtime)
        logger.info("Migrating user profile '%s' → '%s'", newest.name, INSTANCE_ID)
        newest.rename(local)
        others.remove(newest)

    for d in others:
        logger.info("Removing stale user dir: %s", d.name)
        shutil.rmtree(d, ignore_errors=True)


@app.on_event("startup")
async def startup() -> None:
    storage.ensure_dirs()
    logger.info("Personal Radio starting up…")
    asyncio.create_task(_install_companion())

    # Alte Multi-User-Verzeichnisse entfernen / migrieren (Einzelinstanz)
    try:
        _cleanup_user_dirs()
    except Exception:
        logger.exception("User-dir cleanup failed")

    # Bestehende Pools einmalig von HTML-Entity-Fehlsplits bereinigen
    try:
        await asyncio.get_running_loop().run_in_executor(None, _migrate_pool_entities)
    except Exception:
        logger.exception("Pool entity migration failed")

    # Clear all queues on startup so songs are rebuilt from current station
    # selection. is_playing is also reset (stream can't survive a restart).
    if storage.USERS_DIR.exists():
        for sf in storage.USERS_DIR.glob("*/state.json"):
            try:
                s = storage.read_json(sf, {})
                if s:
                    # Eingereihte, aber nie gespielte Titel wieder freigeben,
                    # bevor die Queue verworfen wird.
                    try:
                        dropped = s.get("queue", [])[s.get("current_index", 0):]
                        unmark_unplayed(sf.parent.name, dropped)
                    except Exception:
                        pass
                    s["queue"] = []
                    s["current_index"] = 0
                    s["is_playing"] = False
                    storage.write_json(sf, s)
            except Exception:
                pass
    logger.info("User queues cleared for fresh start")

    # Wire stream server callbacks into queue / event infrastructure
    set_stream_callbacks(
        get_current_song=get_current_song,
        advance_queue=advance_queue,
        ensure_queue=ensure_queue_has_entries,
        fire_event=_fire_event,
        mark_played=mark_song_played,
    )

    asyncio.create_task(api_client.run_poll_loop(get_active_stations=_active_stations))

    media_port  = int(os.environ.get("MEDIA_PORT",  "8788"))
    stream_port = int(os.environ.get("STREAM_PORT", "8789"))
    asyncio.create_task(_run_media_server(media_port))
    asyncio.create_task(_run_icy_server(stream_port))


def _active_stations() -> list[str]:
    """
    Sender, die gerade abgespielt werden (= ausgewählte Sender, solange der
    Player läuft). Leere Liste, wenn nichts spielt — dann greift nur der
    stündliche Voll-Poll.
    """
    try:
        state = storage.read_user_state(INSTANCE_ID)
        from .stream_server import _streams
        stream = _streams.get(INSTANCE_ID)
        playing = bool(state.get("is_playing")) or (stream is not None and not stream.stopped)
        if not playing:
            return []
        return list(state.get("selected_stations", []))
    except Exception:
        return []


def _migrate_pool_entities() -> None:
    """
    Einmalige Reparatur bestehender Sender-Pools: Alte Einträge wurden am
    ersten Semikolon getrennt — auch wenn das mitten in einer HTML-Entity
    lag ("Kool &amp; The Gang;…" → Artist "Kool &amp"). Wir setzen die
    Original-Zeile aus Artist+";"+Titel wieder zusammen und parsen sie mit
    dem korrigierten Parser (Unescape zuerst) neu. Für korrekt gespeicherte
    Einträge ist das ein No-Op.
    """
    from .api_client import _parse_entry

    marker = storage.CACHE_DIR / "pool_entities_migrated"
    if marker.exists() or not storage.STATIONS_DIR.exists():
        return
    fixed = 0
    for f in storage.STATIONS_DIR.glob("*.json"):
        try:
            pool    = storage.read_station_pool(f.stem)
            changed = False
            for e in pool:
                artist, song = e.get("artist", ""), e.get("song", "")
                if not artist or not song:
                    continue
                # "; " stellt das beim damaligen Split verlorene Leerzeichen
                # hinter dem Entity-Semikolon wieder her ("Kool &amp| The …").
                reparsed = _parse_entry(f"{artist}; {song}")
                if reparsed and (reparsed["artist"] != artist or reparsed["song"] != song):
                    e["artist"], e["song"] = reparsed["artist"], reparsed["song"]
                    changed = True
                    fixed  += 1
            if changed:
                storage.write_json(storage.station_pool_path(f.stem), pool)
        except Exception:
            logger.exception("Pool migration failed for '%s'", f.stem)
    marker.write_text("1")
    if fixed:
        logger.info("Pool migration: %d Einträge von HTML-Entities/Fehlsplits bereinigt", fixed)


async def _install_companion() -> None:
    """
    Versucht die Companion-Integration nach /config/custom_components/ zu kopieren.
    Falls config:rw nicht verfügbar ist, wird eine HA-Benachrichtigung mit
    manuellen Installationsanweisungen gesendet.

    Die neue Companion-Integration (v1.2) löst das Admin-Problem durch:
    - Eigener HTTP View /api/personal_radio/app (non-admin)
    - JavaScript erstellt Ingress-Session (POST /api/hassio/ingress/session)
    - Kein Admin für Session-Erstellung nötig!
    """
    import shutil

    src_dir = pathlib.Path("/app/companion_integration")
    dst_dir = pathlib.Path("/config/custom_components/personal_radio")
    marker  = pathlib.Path("/data/.companion_installed")

    if not src_dir.exists():
        return

    # ── Auto-Install versuchen (wenn config:rw vorhanden) ─────────────────
    if pathlib.Path("/config").exists():
        try:
            dst_dir.parent.mkdir(parents=True, exist_ok=True)
            src_mtime = max(p.stat().st_mtime for p in src_dir.rglob("*") if p.is_file())
            dst_mtime = max(
                (p.stat().st_mtime for p in dst_dir.rglob("*") if p.is_file()),
                default=0,
            ) if dst_dir.exists() else 0

            if src_mtime > dst_mtime:
                if dst_dir.exists():
                    shutil.rmtree(dst_dir)
                shutil.copytree(src_dir, dst_dir)
                logger.info("Companion-Integration installiert: %s", dst_dir)
                marker.write_text("auto")

                # Benachrichtigung: Integration über HA-UI einrichten
                try:
                    async with httpx.AsyncClient(timeout=5) as client:
                        await client.post(
                            f"{HA_BASE}/services/persistent_notification/create",
                            headers=_headers(),
                            json={
                                "title": "Personal Radio — Einmalige Einrichtung",
                                "message": (
                                    "Die Companion-Integration wurde installiert.\n\n"
                                    "**Jetzt einmalig einrichten:**\n"
                                    "**Einstellungen → Integrationen → + → "
                                    "nach \'Personal Radio\' suchen → Hinzufügen**\n\n"
                                    "Danach erscheint Personal Radio in der Seitenleiste "
                                    "für alle Benutzer — kein Admin nötig, kein Neustart.\n\n"
                                    "*(Diese Meldung erscheint nur einmalig.)*"
                                ),
                                "notification_id": "personal_radio_setup",
                            },
                        )
                except Exception:
                    pass
            return
        except Exception as exc:
            logger.warning("Auto-Install fehlgeschlagen: %s", exc)

    # ── Manuelle Anweisungen senden ────────────────────────────────────────
    if marker.exists():
        return   # Schon gemeldet

    FILES = {
        "__init__.py":   (src_dir / "__init__.py").read_text(),
        "manifest.json": (src_dir / "manifest.json").read_text(),
        "config_flow.py":(src_dir / "config_flow.py").read_text(),
        "strings.json":  (src_dir / "strings.json").read_text(),
    }

    files_text = "\n".join(
        f"**{name}:**\n```\n{content[:300]}{'...' if len(content)>300 else ''}\n```"
        for name, content in FILES.items()
    )

    MESSAGE = (
        "## Personal Radio — Manuelle Installation\n\n"
        "Da `config:rw` nicht verfügbar ist, bitte manuell installieren "
        "(File Editor Addon oder SSH):\n\n"
        "**Ordner erstellen:** `/config/custom_components/personal_radio/`\n\n"
        "**Dateien aus dem Addon kopieren** "
        "(im Addon-Container unter `/app/companion_integration/`).\n\n"
        "**Danach:** Einstellungen → Integrationen → + → Personal Radio\n\n"
        "Alternativ: Addon-Option `config:rw` in der config.yaml aktivieren "
        "und Addon neu installieren."
    )

    try:
        async with httpx.AsyncClient(timeout=5) as client:
            await client.post(
                f"{HA_BASE}/services/persistent_notification/create",
                headers=_headers(),
                json={
                    "title": "Personal Radio — Manuelle Installation erforderlich",
                    "message": MESSAGE,
                    "notification_id": "personal_radio_manual_install",
                },
            )
        marker.write_text("notified")
    except Exception as exc:
        logger.debug("Benachrichtigung fehlgeschlagen: %s", exc)
async def _run_media_server(port: int) -> None:
    """
    Combined media server on MEDIA_PORT:
      /stream/{uid}   — live radio stream (stream_server)
      /media/{yt_id}  — static MP3 file serving (mp3_server, kept for diagnostics)
      /health         — health check
    """
    media_app = Starlette(
        routes=stream_routes + [
            Route("/media/{yt_id}", serve_mp3, methods=["GET", "HEAD"]),
            Route("/health", mp3_health, methods=["GET"]),
        ]
    )
    config = uvicorn.Config(
        media_app,
        host="0.0.0.0",
        port=port,
        log_level="info",
    )
    server = uvicorn.Server(config)
    server.install_signal_handlers = False
    logger.info("Media/stream server starting on 0.0.0.0:%d", port)
    try:
        await server.serve()
        logger.warning("Media server exited unexpectedly — restarting in 5s")
    except Exception as e:
        logger.error("Media server crashed: %s — restarting in 5s", e)
    await asyncio.sleep(5)
    asyncio.create_task(_run_media_server(port))



async def _run_icy_server(port: int) -> None:
    """
    ICY / HTTP-1.0 stream server.  Media players and VLC connect here.
    Uses raw asyncio TCP (no chunked encoding) for universal compatibility.
    """
    logger.info("ICY server starting on 0.0.0.0:%d", port)
    try:
        await start_icy_server(port)
    except asyncio.CancelledError:
        pass
    except Exception as e:
        logger.error("ICY server crashed: %s — restarting in 5s", e)
    await asyncio.sleep(5)
    asyncio.create_task(_run_icy_server(port))


# ---------------------------------------------------------------------------
# REST API
# ---------------------------------------------------------------------------

# Songanzahl pro Sender wird nach Datei-mtime gecacht — die Pools können
# mehrere MB groß sein und dürfen NICHT bei jedem UI-Poll synchron im
# Event-Loop geparst werden (das ließ die Wiedergabe kurz stocken).
_station_count_cache: dict[str, tuple[float, int]] = {}


def _list_stations_sync() -> list[dict]:
    cursors = storage.read_cursors()
    result  = []
    for f in sorted(storage.STATIONS_DIR.glob("*.json")):
        try:
            mtime  = f.stat().st_mtime
            cached = _station_count_cache.get(f.stem)
            if cached and cached[0] == mtime:
                count = cached[1]
            else:
                count = len(storage.read_station_pool(f.stem))
                _station_count_cache[f.stem] = (mtime, count)
            result.append({
                "station":     f.stem,
                "song_count":  count,
                "cursor":      cursors.get(f.stem, 0),
                "modified_at": mtime,
            })
        except OSError:
            continue
    return result


@app.get("/api/stations")
async def api_stations(request: Request):
    uid = require_user(request)
    try:
        if not storage.STATIONS_DIR.exists():
            return []
        # Im Thread-Executor: blockiert den Event-Loop (und damit den
        # Audio-Producer) nicht.
        return await asyncio.get_running_loop().run_in_executor(
            None, _list_stations_sync
        )
    except Exception as e:
        logger.exception("Error reading stations: %s", e)
        return []


@app.get("/api/debug")
async def api_debug(request: Request):
    import os as _os
    env_keys = ["STATION_API_URL", "STATION_API_TOKEN", "STATION_API_USER",
                "FANART_API_KEY", "MEDIA_PORT", "SUPERVISOR_TOKEN"]
    env_info = {k: ("set" if _os.environ.get(k) else "MISSING") for k in env_keys}
    env_info["STATION_API_URL_value"] = _os.environ.get("STATION_API_URL", "")
    station_files = []
    if storage.STATIONS_DIR.exists():
        station_files = [f.name for f in storage.STATIONS_DIR.glob("*.json")]
    return {
        "env": env_info,
        "data_dir_exists": storage.DATA_ROOT.exists(),
        "stations_dir_exists": storage.STATIONS_DIR.exists(),
        "station_files": station_files,
        "cursors": storage.read_cursors(),
        "user_header": get_user_id(request),
        "ingress_path": request.headers.get("X-Ingress-Path"),
        "all_headers": dict(request.headers),
    }


@app.get("/api/user/state")
async def api_get_user_state(request: Request):
    uid = require_user(request)
    return storage.read_user_state(uid)


class StateUpdate(BaseModel):
    selected_stations: list[str] | None = None
    player_entity_id:  str | None = None
    volume:            float | None = None


@app.post("/api/user/state")
async def api_update_user_state(request: Request, body: StateUpdate):
    uid = require_user(request)
    new_volume = None
    stations_changed = False
    async with get_user_lock(uid):
        state = storage.read_user_state(uid)
        if (body.selected_stations is not None
                and body.selected_stations != state.get("selected_stations")):
            state["selected_stations"] = body.selected_stations
            stations_changed = True
        if body.player_entity_id is not None:
            state["player_entity_id"] = body.player_entity_id
        if body.volume is not None:
            new_volume = max(0.0, min(1.0, body.volume))
            state["volume"] = new_volume
        storage.write_user_state(uid, state)
    # Call HA volume service outside the lock
    if new_volume is not None:
        await ha_set_volume(uid, new_volume)
    # Geänderte Senderauswahl greift auch im laufenden Betrieb ab dem
    # nächsten Song — der aktuelle Song wird nicht unterbrochen.
    if stations_changed:
        asyncio.create_task(apply_station_change(uid))
    return state


@app.post("/api/user/play")
async def api_play(request: Request):
    uid = require_user(request)
    ok  = await engine.start_playback(uid)
    if not ok:
        raise HTTPException(status_code=503, detail="Could not start playback")
    return {"ok": True}


@app.post("/api/user/stop")
async def api_stop(request: Request):
    uid = require_user(request)
    await engine.stop_playback(uid)
    return {"ok": True}


@app.post("/api/user/skip")
async def api_skip(request: Request):
    uid = require_user(request)
    ok  = await engine.skip_song(uid)
    return {"ok": ok}


@app.post("/api/user/prev")
async def api_prev(request: Request):
    uid = require_user(request)
    ok  = await engine.prev_song(uid)
    if not ok:
        raise HTTPException(status_code=404, detail="No previous song")
    return {"ok": ok}


@app.get("/api/user/nowplaying")
async def api_nowplaying(request: Request):
    uid   = require_user(request)
    state = storage.read_user_state(uid)

    # Prefer the stream's live current_song for accuracy (reflects crossfade timing)
    from .stream_server import _streams
    stream = _streams.get(uid)
    song   = (stream.current_song if stream and not stream.stopped else None) \
             or get_current_song(uid)

    entity_id   = state.get("player_entity_id")
    player_name = entity_id
    volume      = state.get("volume")
    if entity_id:
        # Parallel abfragen, damit die Antwortzeit niedrig bleibt (Boot/Polling)
        players, pstate = await asyncio.gather(
            get_media_players(),
            get_player_state(entity_id),
            return_exceptions=True,
        )
        if isinstance(players, list):
            for p in players:
                if p["entity_id"] == entity_id:
                    player_name = p["name"]
                    break
        # Tatsächliche Lautstärke des Players — spiegelt auch externe
        # Änderungen (HA-UI, Fernbedienung, andere Apps) wider.
        if isinstance(pstate, dict):
            vl = (pstate.get("attributes") or {}).get("volume_level")
            if isinstance(vl, (int, float)):
                volume = float(vl)

    return {
        "artist":           song.get("artist")    if song else None,
        "song":             song.get("song")      if song else None,
        "thumbnail":        song.get("thumbnail") if song else None,
        "cover_url":        song.get("cover_url") if song else None,
        "station":          song.get("station")   if song else None,
        "player_entity_id": entity_id,
        "player_name":      player_name,
        "is_playing":       state.get("is_playing", False),
        # Live-Lautstärke des Players (None, wenn nicht ermittelbar)
        "volume":           volume,
        # Zählt jeden Song-Start hoch — die UI erkennt daran auch den Wechsel
        # auf denselben Titel (oder einen Neustart des aktuellen Titels).
        "seq":              (stream.song_seq if stream and not stream.stopped else 0),
        "next_ready":       next_is_ready(uid),
        "error":            get_user_error(uid),
    }


@app.get("/api/user/players")
async def api_players(request: Request):
    require_user(request)
    return await get_media_players()


@app.get("/api/user/history")
async def api_history(request: Request):
    uid = require_user(request)
    return storage.read_user_history(uid)[:20]


# ---------------------------------------------------------------------------
# Artist background (fanart.tv)
# ---------------------------------------------------------------------------

_artist_bg_lock = asyncio.Lock()
_MB_LAST: float  = 0.0
_FANART_KEY      = os.environ.get("FANART_API_KEY", "")
_BG_TTL          = 30 * 24 * 3600


@app.get("/api/artist_bg")
async def api_artist_bg(request: Request, artist: str = Query(...)):
    require_user(request)
    if not _FANART_KEY:
        return {"url": None}

    cache = storage.read_artist_bg_cache()
    key   = artist.lower()
    now   = time.time()
    cached = cache.get(key)
    if cached and now - cached.get("fetched_at", 0) < _BG_TTL:
        return {"url": cached.get("url")}

    async with _artist_bg_lock:
        cache  = storage.read_artist_bg_cache()
        cached = cache.get(key)
        if cached and now - cached.get("fetched_at", 0) < _BG_TTL:
            return {"url": cached.get("url")}

        url = None
        try:
            async with httpx.AsyncClient() as client:
                global _MB_LAST
                await asyncio.sleep(max(0, 1.0 - (time.monotonic() - _MB_LAST)))
                resp = await client.get(
                    "https://musicbrainz.org/ws/2/artist/",
                    params={"query": f'artist:"{artist}"', "limit": 1, "fmt": "json"},
                    timeout=10,
                    headers={"User-Agent": "PersonalRadioHA/1.0"},
                )
                _MB_LAST = time.monotonic()
                if resp.status_code == 200:
                    artists = resp.json().get("artists", [])
                    if artists:
                        mbid = artists[0]["id"]
                        r2   = await client.get(
                            f"https://webservice.fanart.tv/v3/music/{mbid}",
                            params={"api_key": _FANART_KEY},
                            timeout=10,
                        )
                        if r2.status_code == 200:
                            bgs = r2.json().get("artistbackground", [])
                            if bgs:
                                best = max(bgs, key=lambda x: int(x.get("likes", 0)))
                                url  = best.get("url")
        except Exception as e:
            logger.debug("Artist bg lookup failed for '%s': %s", artist, e)

        cache[key] = {"url": url, "fetched_at": now}
        storage.write_artist_bg_cache(cache)

    return {"url": url}


# ---------------------------------------------------------------------------
# Debug / maintenance endpoints
# ---------------------------------------------------------------------------

@app.get("/api/debug/media")
async def api_debug_media(request: Request):
    uid         = require_user(request)
    state       = storage.read_user_state(uid)
    song        = get_current_song(uid)
    yt_id       = song.get("yt_id", "?") if song else "?"
    media_token = state.get("media_token", "?")
    media_port  = int(os.environ.get("MEDIA_PORT",  "8788"))
    stream_port = int(os.environ.get("STREAM_PORT", "8789"))
    ha_host     = await get_ha_host()
    # ICY URL (port 8789) — for media players and VLC (HTTP/1.0, no chunked encoding)
    stream_url  = f"http://{ha_host}:{stream_port}/stream/{uid}?token={media_token}"
    # Browser URL (port 8788) — for testing in a browser tab
    browser_url = f"http://{ha_host}:{media_port}/stream/{uid}?token={media_token}"
    health_url  = f"http://{ha_host}:{media_port}/health"

    media_server_ok = False
    try:
        async with httpx.AsyncClient(timeout=3) as client:
            r = await client.get(f"http://localhost:{media_port}/health")
            media_server_ok = r.status_code == 200
    except Exception:
        pass

    from .stream_server import _streams
    stream = _streams.get(uid)

    return {
        "stream_url_icy":              stream_url,
        "stream_url_browser":          browser_url,
        "health_url":                  health_url,
        "ha_host":                     ha_host,
        "media_port":                  media_port,
        "stream_port":                 stream_port,
        "stream_active":               stream is not None and not stream.stopped,
        "media_server_reachable_locally": media_server_ok,
        "yt_id":                       yt_id,
        "player_entity_id":            state.get("player_entity_id"),
        "tip": "Use stream_url_icy in VLC/media players (HTTP/1.0). Use stream_url_browser to test in a browser tab.",
    }


@app.post("/api/debug/reset_cursors")
async def api_reset_cursors(request: Request):
    require_user(request)
    storage.write_cursors({})
    logger.info("Station cursors reset to 0")
    return {"ok": True, "message": "Cursors reset. Songs will be fetched on next poll (within 30s)."}
