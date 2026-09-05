"""Per-user queue manager: song selection, rotation, played tracking."""
from __future__ import annotations

import asyncio
import logging
import os
import time

from . import storage
from .downloader import get_audio_source, max_song_seconds, prefetch_for_user, resolve_song

logger = logging.getLogger("personal_radio.queue")


def _no_repeat_seconds() -> float:
    """
    Addon option: Spanne in Stunden, in der kein Song doppelt gespielt wird.
    0 = klassische Vollrotation (erst wiederholen, wenn alle Titel des
    Senders gespielt wurden). Gemessen wird die tatsächlich vergangene
    Zeit (Wanduhr), nicht die Abspieldauer.
    """
    try:
        hours = float(os.environ.get("NO_REPEAT_HOURS", "0") or 0)
    except (TypeError, ValueError):
        hours = 0.0
    return max(0.0, hours) * 3600.0

_user_locks:  dict[str, asyncio.Lock] = {}
_user_errors: dict[str, str | None]   = {}


def get_user_lock(uid: str) -> asyncio.Lock:
    if uid not in _user_locks:
        _user_locks[uid] = asyncio.Lock()
    return _user_locks[uid]


def get_user_error(uid: str) -> str | None:
    return _user_errors.get(uid)


def set_user_error(uid: str, msg: str | None) -> None:
    _user_errors[uid] = msg


# ---------------------------------------------------------------------------
# Song selection — newest-first, duplicate-aware
# ---------------------------------------------------------------------------

def _pick_song(uid: str, station: str,
               exclude_ids: set[str] | None = None) -> dict | None:
    """
    Walk the station pool from NEWEST to OLDEST (reverse insertion order).

    For duplicate artist+song entries, only the LAST (newest) occurrence is
    considered — earlier occurrences are skipped via seen_keys.

    A song is "played" if its yt_id is in the user's played-set for this
    station.  Permanently-failed songs are treated as played (skip them).

    When every unique song has been played, the played-set is reset and we
    start the cycle over from the newest entry again.
    """
    pool = storage.read_station_pool(station)
    if not pool:
        return None

    exclude_ids = exclude_ids or set()
    cache       = storage.read_yt_cache()
    played      = storage.read_played_ids(uid, station)      # {yt_id: played_at}
    no_repeat_s = _no_repeat_seconds()
    # Sperrfrist gilt PRO TITEL über alle Sender hinweg → globaler Index
    played_g    = storage.read_played_global(uid) if no_repeat_s > 0 else {}
    max_sec     = max_song_seconds()
    now         = time.time()

    def _candidates():
        """Newest → oldest; duplicates and over-long songs filtered out."""
        seen: set[tuple[str, str]] = set()
        for entry in reversed(pool):                   # newest → oldest
            key = (entry["artist"].lower(), entry["song"].lower())
            if key in seen:
                continue                               # earlier duplicate — skip
            seen.add(key)
            if exclude_ids:
                cached = cache.get(storage.yt_cache_key(entry["artist"], entry["song"]))
                if cached and cached.get("yt_id") in exclude_ids:
                    continue    # Titel läuft gerade / ist schon eingereiht
            if max_sec > 0:
                cached = cache.get(storage.yt_cache_key(entry["artist"], entry["song"]))
                dur    = (cached or {}).get("duration")
                if dur and dur > max_sec:
                    continue                           # Spieldauer zu lang — nie spielen
            yield entry

    def _status(entry: dict) -> str:
        """'unplayed' | 'played' | 'failed'"""
        key    = storage.yt_cache_key(entry["artist"], entry["song"])
        cached = cache.get(key)
        if not cached:
            return "unplayed"          # not yet resolved → treat as unplayed
        if cached.get("failed"):
            return "failed"
        yt_id = cached.get("yt_id")
        if not yt_id:
            return "unplayed"
        if no_repeat_s > 0:
            # Fenster-Modus: pro Titel, senderübergreifend
            ts = played_g.get(yt_id)
            if ts is None or now - ts >= no_repeat_s:
                return "unplayed"
            return "played"
        # Modus 0: klassische Vollrotation pro Sender
        return "played" if yt_id in played else "unplayed"

    # ── 1st pass: first unplayed / out-of-window song ──────────────────────
    for entry in _candidates():
        if _status(entry) == "unplayed":
            return entry

    # ── All eligible songs are blocked ─────────────────────────────────────
    if no_repeat_s > 0:
        # Alle Titel wurden innerhalb der Sperrfrist bereits gespielt.
        # Laut Anforderung darf dann früher wiederholt werden:
        # wir nehmen den am längsten zurückliegenden Titel.
        best: dict | None = None
        best_ts = float("inf")
        for entry in _candidates():
            if _status(entry) == "failed":
                continue
            key    = storage.yt_cache_key(entry["artist"], entry["song"])
            yt_id  = (cache.get(key) or {}).get("yt_id", "")
            ts     = played_g.get(yt_id, 0.0)
            if ts < best_ts:
                best, best_ts = entry, ts
        if best:
            logger.info(
                "Station '%s' (uid %s): all songs within %.1fh window — "
                "repeating oldest", station, uid, no_repeat_s / 3600,
            )
        return best

    # ── Mode 0: full cycle complete — reset played set ─────────────────────
    logger.info("Cycle complete for station '%s' (uid %s)", station, uid)
    storage.write_played_ids(uid, station, {})
    played.clear()                       # _status re-evaluates against empty set
    for entry in _candidates():
        if _status(entry) == "unplayed":
            return entry
    return None


# ---------------------------------------------------------------------------
# Played tracking (Zeitstempel pro Sender + global pro Titel)
# ---------------------------------------------------------------------------

def _mark_played(uid: str, station: str, yt_id: str, ts: float | None = None) -> None:
    """Titel als gespielt markieren — pro Sender und im globalen Index."""
    if not yt_id:
        return
    ts = ts if ts is not None else time.time()
    if station:
        played = storage.read_played_ids(uid, station)
        played[yt_id] = ts
        storage.write_played_ids(uid, station, played)
    played_g = storage.read_played_global(uid)
    played_g[yt_id] = ts
    storage.write_played_global(uid, played_g)


def mark_song_played(uid: str, song: dict) -> None:
    """
    Vom Stream-Producer beim tatsächlichen Song-Start aufgerufen: setzt den
    Zeitstempel auf den echten Abspielbeginn (statt des Queue-Zeitpunkts),
    damit die No-Repeat-Spanne ab dem Abspielen zählt.
    """
    _mark_played(uid, song.get("station", ""), song.get("yt_id", ""))


def unmark_unplayed(uid: str, entries: list[dict]) -> None:
    """
    Entfernt Einträge, die eingereiht aber nie gespielt wurden (z.B. nach
    einem Senderwechsel oder Neustart), wieder aus den Played-Sets, damit
    sie für Rotation und No-Repeat-Spanne verfügbar bleiben.
    """
    if not entries:
        return
    cache: dict | None = None
    ids_by_station: dict[str, set[str]] = {}
    all_ids: set[str] = set()
    for item in entries:
        yt_id = item.get("yt_id") or ""
        if not yt_id:
            if cache is None:
                cache = storage.read_yt_cache()
            key   = storage.yt_cache_key(item.get("artist", ""), item.get("song", ""))
            yt_id = (cache.get(key) or {}).get("yt_id", "")
        if not yt_id:
            continue
        all_ids.add(yt_id)
        st = item.get("station", "")
        if st:
            ids_by_station.setdefault(st, set()).add(yt_id)

    for st, ids in ids_by_station.items():
        played  = storage.read_played_ids(uid, st)
        changed = False
        for i in ids:
            if i in played:
                del played[i]
                changed = True
        if changed:
            storage.write_played_ids(uid, st, played)

    if all_ids:
        played_g = storage.read_played_global(uid)
        changed  = False
        for i in all_ids:
            if i in played_g:
                del played_g[i]
                changed = True
        if changed:
            storage.write_played_global(uid, played_g)


# ---------------------------------------------------------------------------
# Queue population
# ---------------------------------------------------------------------------

async def _build_next_song_entry(uid: str, station: str,
                                 exclude_ids: set[str] | None = None) -> dict | None:
    """
    Pick and resolve the next song for a station.
    Tries up to 5 candidates per call to skip permanently-failed ones.
    *exclude_ids*: yt_ids, die gerade laufen oder schon eingereiht sind —
    verhindert, dass derselbe Titel direkt hintereinander kommt, wenn er
    in mehreren ausgewählten Sendern vorkommt.
    """
    pool = storage.read_station_pool(station)

    loop = asyncio.get_running_loop()
    for _ in range(min(5, max(1, len(pool)))):
        # _pick_song liest die (potentiell großen) Pool-Dateien synchron —
        # im Executor, damit der Audio-Producer nicht blockiert (Stottern).
        entry = await loop.run_in_executor(None, _pick_song, uid, station, exclude_ids)
        if not entry and exclude_ids:
            # Fallback: lieber wiederholen als Stille (z.B. Mini-Pools)
            entry = await loop.run_in_executor(None, _pick_song, uid, station, None)
        if not entry:
            return None

        result = await resolve_song(entry["artist"], entry["song"])
        if result:
            # Spieldauer-Limit erst nach dem Resolve prüfbar. Zu lange Titel
            # werden NICHT als gespielt markiert — die gecachte Dauer sorgt
            # dafür, dass _pick_song sie ab jetzt gar nicht mehr anbietet.
            max_sec = max_song_seconds()
            dur     = result.get("duration")
            if max_sec and dur and dur > max_sec:
                logger.info(
                    "Titel zu lang (%ds > %ds): %s — %s — wird nicht gespielt",
                    dur, max_sec, entry["artist"], entry["song"],
                )
                continue

            yt_id = result.get("yt_id", "")
            # Mark as played (Queue-Zeitpunkt; beim tatsächlichen Song-Start
            # wird der Zeitstempel via mark_song_played aktualisiert).
            _mark_played(uid, station, yt_id)

            return {
                "artist":    entry["artist"],
                "song":      entry["song"],
                "yt_id":     yt_id,
                "thumbnail": result.get("thumbnail", ""),
                "cover_url": result.get("cover_url", ""),
                "duration":  result.get("duration"),
                "station":   station,
                "queued_at": int(time.time()),
            }
        # resolve_song already marked this as failed in the cache

    return None


async def ensure_queue_has_entries(uid: str, count: int = 1) -> None:
    """Ensure at least *count* songs after current_index are queued."""
    async with get_user_lock(uid):
        state    = storage.read_user_state(uid)
        stations = state.get("selected_stations", [])
        if not stations:
            return

        queue         = state.get("queue", [])
        current_index = state.get("current_index", 0)
        ahead         = len(queue) - current_index - 1
        needed        = count - ahead
        if needed <= 0:
            return

        station_idx = state.get("current_station_index", 0)
        all_failed  = True

        # Titel, die gerade laufen oder schon eingereiht sind, nicht erneut
        # einreihen (derselbe Song kann in mehreren Sender-Pools stehen).
        exclude_ids = {
            e.get("yt_id") for e in queue[max(0, current_index - 1):]
            if e.get("yt_id")
        }

        for _ in range(needed):
            station     = stations[station_idx % len(stations)]
            station_idx = (station_idx + 1) % len(stations)
            item        = await _build_next_song_entry(uid, station, exclude_ids)
            if item:
                queue.append(item)
                if item.get("yt_id"):
                    exclude_ids.add(item["yt_id"])
                all_failed = False
            else:
                logger.warning("[%s] No song available from '%s'", uid, station)

        # Nur die beiden geänderten Felder schreiben: zwischen dem Lesen oben
        # und hier liegen lange Auflösungen (yt-dlp) — ein kompletter
        # Rückschreiber würde zwischenzeitliche Änderungen (Lautstärke,
        # Senderauswahl) wieder auf den alten Stand setzen.
        storage.update_user_state(
            uid, queue=queue, current_station_index=station_idx,
        )

        if all_failed and needed > 0:
            set_user_error(uid, "All songs in all stations failed to resolve.")
        else:
            set_user_error(uid, None)


async def apply_station_change(uid: str) -> None:
    """
    Senderauswahl wurde geändert: alle noch nicht gespielten Queue-Einträge
    verwerfen, damit die neue Auswahl ab dem NÄCHSTEN Song greift. Der
    aktuell laufende Song wird nicht angefasst (sein PCM ist bereits im
    Stream-Producer) und läuft ungestört zu Ende.
    """
    async with get_user_lock(uid):
        state = storage.read_user_state(uid)
        idx   = state.get("current_index", 0)
        queue = state.get("queue", [])
        # current_index zeigt (während der Wiedergabe) bereits auf den
        # NÄCHSTEN Song — alles ab idx wird verworfen und neu befüllt.
        dropped        = queue[idx:]
        state["queue"] = queue[:idx]
        storage.write_user_state(uid, state)
        # Verworfene (nie gespielte) Titel wieder freigeben, damit sie in
        # Rotation und No-Repeat-Spanne nicht fälschlich als gespielt zählen.
        unmark_unplayed(uid, dropped)

    # Prefetch des Streams verwerfen, damit nicht doch noch der alte
    # "nächste" Song gespielt wird.
    from .stream_server import _streams
    stream = _streams.get(uid)
    if stream and not stream.stopped:
        stream.invalidate_upcoming()

    # Queue im Hintergrund mit der neuen Auswahl auffüllen
    # (nicht innerhalb des Locks — ensure_queue_has_entries lockt selbst).
    await ensure_queue_has_entries(uid, count=1)


def _same_song(a: dict | None, b: dict | None) -> bool:
    if not a or not b:
        return False
    if a.get("station") and b.get("station") and a["station"] != b["station"]:
        return False
    if a.get("yt_id") and b.get("yt_id"):
        return a["yt_id"] == b["yt_id"]
    return (
        (a.get("artist", "").lower(), a.get("song", "").lower())
        == (b.get("artist", "").lower(), b.get("song", "").lower())
    )


async def advance_queue(uid: str, played_song: dict | None = None) -> dict | None:
    """
    Den gespielten Song in die History übernehmen und den Queue-Zeiger
    weiterschieben.

    *played_song* ist der Song, den der Producer tatsächlich abspielt.
    Weitergeschoben wird nur, wenn queue[current_index] noch derselbe Song
    ist — wurde die Queue zwischenzeitlich umgebaut (Senderwechsel während
    der Dekodier-Phase), zeigt current_index bereits auf den richtigen
    neuen Song und darf nicht "verbraucht" werden.
    """
    async with get_user_lock(uid):
        state = storage.read_user_state(uid)
        queue = state.get("queue", [])
        idx   = state.get("current_index", 0)
        cur   = queue[idx] if 0 <= idx < len(queue) else None

        if played_song is None:
            played_song = cur

        if played_song:
            # Beim Fortsetzen eines pausierten Songs steht er schon ganz oben
            # in der History (er wurde beim ersten Start eingetragen) — dann
            # keinen doppelten Eintrag anlegen.
            history = storage.read_user_history(uid)
            if not (history and _same_song(history[0], played_song)):
                storage.add_to_history(uid, played_song)

        if cur is not None and _same_song(cur, played_song):
            idx += 1
            state["current_index"] = idx
        storage.write_user_state(uid, state)
        return queue[idx] if 0 <= idx < len(queue) else None


async def restore_paused_song(uid: str) -> dict | None:
    """
    Beim Start der Wiedergabe den pausierten Song wieder an die aktuelle
    Queue-Position setzen, damit der Producer ihn (ab der gemerkten Stelle)
    fortsetzt statt mit dem nächsten Titel zu beginnen.
    """
    from .stream_server import song_key

    async with get_user_lock(uid):
        state  = storage.read_user_state(uid)
        paused = state.get("paused_song") or {}
        if not paused:
            return None

        queue = state.get("queue", [])
        idx   = max(0, min(state.get("current_index", 0), len(queue)))
        cur   = queue[idx] if idx < len(queue) else None
        if cur is not None and song_key(cur) == song_key(paused):
            return cur                       # steht bereits an der Reihe

        entry = {k: v for k, v in paused.items() if k != "position"}
        queue.insert(idx, entry)
        state["queue"]         = queue
        state["current_index"] = idx
        storage.write_user_state(uid, state)
        logger.info("[%s] Pausierten Titel fortsetzen: %s — %s (ab %.0fs)",
                    uid, entry.get("artist", ""), entry.get("song", ""),
                    float(paused.get("position", 0) or 0))
        return entry


async def go_to_previous(uid: str) -> dict | None:
    """
    Zum tatsächlich VORHERIGEN Titel springen.

    history[0] ist der gerade laufende Song (er wird beim Song-Start in die
    History eingetragen) — der vorherige Titel ist daher history[1]. Er wird
    aus der History entfernt und vor den aktuellen Queue-Zeiger eingefügt;
    beim Neustart trägt ihn der Producer wieder als history[0] ein.
    """
    async with get_user_lock(uid):
        history = storage.read_user_history(uid)
        if len(history) < 2:
            return None
        prev = history[1]
        del history[1]
        storage.write_user_history(uid, history)
        state   = storage.read_user_state(uid)
        queue   = state.get("queue", [])
        idx     = state.get("current_index", 0)
        queue.insert(idx, prev)
        state["queue"] = queue
        storage.write_user_state(uid, state)
        return prev


def get_current_song(uid: str) -> dict | None:
    state = storage.read_user_state(uid)
    queue = state.get("queue", [])
    idx   = state.get("current_index", 0)
    return queue[idx] if 0 <= idx < len(queue) else None


def get_next_song(uid: str) -> dict | None:
    state = storage.read_user_state(uid)
    queue = state.get("queue", [])
    idx   = state.get("current_index", 0) + 1
    return queue[idx] if 0 <= idx < len(queue) else None


def next_is_ready(uid: str) -> bool:
    nxt = get_next_song(uid)
    if not nxt:
        return False
    # Ready if local file exists OR a valid stream URL is in cache
    cache_key = storage.yt_cache_key(
        nxt.get("artist", ""), nxt.get("song", "")
    )
    cached = storage.read_yt_cache().get(cache_key, {})
    if cached.get("failed"):
        return False
    yt_id = cached.get("yt_id", "")
    if yt_id and (storage.LIBRARY_DIR / f"{yt_id}.mp3").exists():
        return True
    import time
    return bool(
        cached.get("stream_url")
        and cached.get("stream_url_expires_at", 0) > time.time() + 60
    )
