"""Per-user queue manager: song selection, rotation, played tracking."""
from __future__ import annotations

import asyncio
import logging
import os
import time
import zlib

from . import listened, sources, storage
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

def _min_repeat_seconds() -> float:
    """
    Harte Untergrenze: derselbe Titel kommt innerhalb dieser Spanne nicht
    erneut — unabhängig vom Rotationsmodus.

    Die Vollrotation (no_repeat_hours = 0) arbeitet pro Quelle und mit
    Video-IDs; sie kann einen Titel daher nicht erkennen, der über eine
    andere Quelle, unter abweichender Schreibweise oder auf einem anderen
    Gerät lief. Diese Sperre schiebt sich als zusätzliche Bedingung davor
    und macht die dauerhafte Hörliste in jedem Modus wirksam.
    """
    try:
        hours = float(os.environ.get("MIN_REPEAT_HOURS", "4") or 0)
    except (TypeError, ValueError):
        hours = 4.0
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

def _as_source(source) -> dict:
    """Bequemlichkeit: ein blanker Sendername zählt als Sender-Quelle."""
    if isinstance(source, str):
        return sources.station_source(source)
    return source


def _pick_song(uid: str, source, exclude_ids: set[str] | None = None,
               skip_keys: set | None = None, relaxed: bool = False) -> dict | None:
    """
    Walk the station pool from NEWEST to OLDEST (reverse insertion order).

    For duplicate artist+song entries, only the LAST (newest) occurrence is
    considered — earlier occurrences are skipped via seen_keys.

    A song is "played" if its yt_id is in the user's played-set for this
    station.  Permanently-failed songs are treated as played (skip them).

    When every unique song has been played, the played-set is reset and we
    start the cycle over from the newest entry again.
    """
    source  = _as_source(source)
    station = source.get("key", "")
    pool    = sources.pool_entries(source)
    if not pool:
        return None

    exclude_ids  = exclude_ids or set()
    skip_keys    = skip_keys or set()
    cache        = storage.read_yt_cache()
    played       = storage.read_played_ids(uid, station)     # {yt_id: played_at}
    no_repeat_s  = _no_repeat_seconds()
    # relaxed: letzte Rettung, wenn KEINE Quelle etwas liefern konnte —
    # dann sind Sperrfrist und Rotation ausgesetzt, die geschützten Titel
    # (laufender/nächster) bleiben über exclude_ids trotzdem außen vor.
    min_repeat_s = 0.0 if relaxed else _min_repeat_seconds()
    # Sperrfrist gilt PRO TITEL über alle Sender hinweg → globaler Index
    played_g     = (storage.read_played_global(uid)
                    if (no_repeat_s > 0 or min_repeat_s > 0) else {})
    max_sec      = max_song_seconds()
    now          = time.time()
    cycle_at     = listened.ensure_cycle(station) if no_repeat_s <= 0 else 0.0

    def _candidates():
        """Newest → oldest; duplicates and over-long songs filtered out."""
        seen: set[tuple[str, str]] = set()
        for entry in reversed(pool):                   # newest → oldest
            key = (entry["artist"].lower(), entry["song"].lower())
            if key in seen or key in skip_keys:
                continue          # frühere Dublette oder bereits verworfen
            seen.add(key)
            if exclude_ids:
                cached = cache.get(storage.yt_cache_key(entry["artist"], entry["song"]))
                known  = (cached or {}).get("yt_id") or entry.get("yt_id")
                if known and known in exclude_ids:
                    continue    # Titel läuft gerade / ist schon eingereiht
            if max_sec > 0:
                cached = cache.get(storage.yt_cache_key(entry["artist"], entry["song"]))
                dur    = (cached or {}).get("duration")
                if dur and dur > max_sec:
                    continue                           # Spieldauer zu lang — nie spielen
            yield entry

    def _last_heard(entry: dict, yt_id: str) -> float | None:
        """
        Wann lief dieser Titel zuletzt? Drei Quellen, die jüngste gewinnt:
        der Played-Index (Video-ID), die dauerhafte Liste nach Interpret+
        Titel und die dauerhafte Liste nach Video-ID. Letztere fängt die
        Fälle ab, in denen dieselbe Aufnahme unter anderer Schreibweise
        eingereiht wird.
        """
        best = played_g.get(yt_id)
        for ts in (listened.last_played(entry["artist"], entry["song"]),
                   listened.last_played_id(yt_id)):
            if ts and (best is None or ts > best):
                best = ts
        return best

    def _status(entry: dict) -> str:
        """'unplayed' | 'recent' (Mindestsperre) | 'played' | 'failed'"""
        key    = storage.yt_cache_key(entry["artist"], entry["song"])
        cached = cache.get(key)
        # Playlist-Titel bringen ihre Video-ID mit — damit zählt auch ein
        # noch nicht aufgelöster Titel korrekt als gehört/ungehört.
        yt_id  = (cached or {}).get("yt_id") or entry.get("yt_id")
        if not cached and not yt_id:
            return "unplayed"          # not yet resolved → treat as unplayed
        if cached and cached.get("failed"):
            return "failed"
        if not yt_id:
            return "unplayed"
        last = _last_heard(entry, yt_id)

        # Harte Untergrenze zuerst — gilt in jedem Modus und ist der
        # eigentliche Schutz gegen Wiederholungen über Quellen, Geräte und
        # Neuinstallationen hinweg.
        if min_repeat_s > 0 and last is not None and now - last < min_repeat_s:
            return "recent"

        if no_repeat_s > 0:
            # Fenster-Modus: pro Titel, senderübergreifend
            if last is None or now - last >= no_repeat_s:
                return "unplayed"
            return "played"

        # Modus 0: klassische Vollrotation pro Quelle
        if yt_id in played:
            return "played"
        # Echt GRÖSSER: ein Titel, der exakt im Moment des Rundenstarts
        # lief, darf die neue Runde nicht blockieren.
        if last is not None and last > cycle_at:
            return "played"
        return "unplayed"

    # ── 1st pass: first unplayed / out-of-window song ──────────────────────
    for entry in _candidates():
        if _status(entry) == "unplayed":
            return entry

    # ── Nichts frei: woran liegt es? ───────────────────────────────────────
    states = [(_status(e), e) for e in _candidates()]
    blocked_by_cycle = any(st == "played" for st, _ in states)

    if no_repeat_s > 0:
        if not blocked_by_cycle:
            # Alles steckt in der Mindestsperre — das ist eine harte Grenze,
            # hier wird nicht gelockert. Die Quelle liefert diesmal nichts.
            return None
        # Alle Titel wurden innerhalb der Sperrfrist bereits gespielt.
        # Laut Anforderung darf dann früher wiederholt werden: der am
        # längsten zurückliegende Titel, aber nie einer aus der Sperre.
        best: dict | None = None
        best_ts = float("inf")
        for st, entry in states:
            if st in ("failed", "recent"):
                continue
            key   = storage.yt_cache_key(entry["artist"], entry["song"])
            yt_id = (cache.get(key) or {}).get("yt_id", "") or entry.get("yt_id", "")
            ts    = _last_heard(entry, yt_id) or 0.0
            if ts < best_ts:
                best, best_ts = entry, ts
        if best:
            logger.info(
                "Quelle '%s' (uid %s): alle Titel innerhalb der %.1f-h-Spanne — "
                "ältester wird wiederholt", station, uid, no_repeat_s / 3600,
            )
        return best

    # ── Modus 0: Runde abgeschlossen → Played-Liste zurücksetzen ───────────
    if not blocked_by_cycle:
        # Nur die Mindestsperre greift — keine abgeschlossene Runde. Ohne
        # diese Unterscheidung würde bei jedem Aufruf ein Rundenneustart
        # ausgelöst und die Rotation damit entwertet.
        return None

    logger.info("Runde abgeschlossen für Quelle '%s' (uid %s)", station, uid)
    storage.write_played_ids(uid, station, {})
    # Auch dauerhaft festhalten: alles davor zählt für diese Quelle nicht
    # mehr als gespielt — sonst stünde nach einem Neustart sofort wieder
    # die komplette Sperre.
    listened.start_cycle(station)
    played.clear()                       # _status re-evaluates against empty set
    cycle_at = listened.cycle_start(station)
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

    Nur hier — nicht beim Einreihen — wandert der Titel in die dauerhafte
    Liste: eingereihte, aber nie gehörte Titel sollen dort nicht auftauchen.
    """
    _mark_played(uid, song.get("station", ""), song.get("yt_id", ""))
    try:
        listened.record(song)
    except Exception:
        logger.exception("Konnte Titel nicht in die dauerhafte Liste eintragen")


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

async def _build_next_song_entry(uid: str, source,
                                 exclude_ids: set[str] | None = None,
                                 protect_ids: set[str] | None = None,
                                 relaxed: bool = False) -> dict | None:
    """
    Pick and resolve the next song for a source.
    Tries up to 5 candidates per call to skip permanently-failed ones.

    *exclude_ids*: yt_ids, die gerade laufen oder schon eingereiht sind.
    *protect_ids*: Titel, die auch in der Rückfallebene tabu bleiben
    (laufender und unmittelbar nächster Titel) — sonst stünde derselbe
    Titel bei kleinen Quellen direkt zweimal hintereinander.
    *relaxed*: letzte Rettung, wenn keine Quelle etwas liefern konnte.
    """
    source  = _as_source(source)
    station = source.get("key", "")
    pool    = sources.pool_entries(source)

    exclude_ids = set(exclude_ids or ())
    protect_ids = set(protect_ids or ())
    skip_keys: set[tuple[str, str]] = set()

    loop = asyncio.get_running_loop()
    for _ in range(min(5, max(1, len(pool)))):
        # _pick_song liest die (potentiell großen) Pool-Dateien synchron —
        # im Executor, damit der Audio-Producer nicht blockiert (Stottern).
        entry = await loop.run_in_executor(
            None, _pick_song, uid, source, exclude_ids, skip_keys, relaxed)
        if not entry and exclude_ids - protect_ids:
            # Rückfallebene: lieber wiederholen als Stille — aber nie den
            # laufenden oder den unmittelbar nächsten Titel.
            entry = await loop.run_in_executor(
                None, _pick_song, uid, source, protect_ids, skip_keys, relaxed)
        if not entry:
            return None

        key = (entry["artist"].lower(), entry["song"].lower())
        # Playlist-Titel bringen die Video-ID mit — dann entfällt die Suche.
        result = await resolve_song(entry["artist"], entry["song"],
                                    yt_id=entry.get("yt_id"))
        if not result:
            # resolve_song hat den Titel bereits als fehlgeschlagen vermerkt
            skip_keys.add(key)
            continue

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
            skip_keys.add(key)
            continue

        yt_id = result.get("yt_id", "")
        # Erst JETZT ist die Video-ID sicher bekannt. Vorher konnte die
        # Ausschlussliste nicht greifen: ein Senderpool-Eintrag hat keine
        # ID, und zwei Schreibweisen desselben Titels landen auf derselben
        # Aufnahme. Genau hier entstanden die Dubletten.
        if yt_id and (yt_id in exclude_ids or yt_id in protect_ids):
            logger.info(
                "[%s] Dublette verhindert: %s — %s läuft bereits oder ist "
                "eingereiht (Video %s)", uid, entry["artist"], entry["song"], yt_id,
            )
            skip_keys.add(key)
            continue

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
            # "station" bleibt der Quellen-Schlüssel — daran hängen die
            # Played-Dateien und die dauerhafte Hörliste.
            "station":     station,
            "source_kind": source.get("kind", sources.KIND_STATION),
            "source_name": sources.display_name(source),
            "queued_at": int(time.time()),
        }

    return None


async def ensure_queue_has_entries(uid: str, count: int = 1) -> None:
    """Ensure at least *count* songs after current_index are queued."""
    async with get_user_lock(uid):
        state    = storage.read_user_state(uid)
        active   = sources.enabled_sources(state)
        if not active:
            return

        queue         = state.get("queue", [])
        current_index = state.get("current_index", 0)
        ahead         = len(queue) - current_index - 1
        needed        = count - ahead
        if needed <= 0:
            return

        # Reihum über die Quellen in der vom Nutzer festgelegten Reihenfolge
        station_idx = int(state.get("current_station_index", 0) or 0)
        all_failed  = True

        # Titel, die gerade laufen oder schon eingereiht sind, nicht erneut
        # einreihen (derselbe Song kann in mehreren Quellen stehen).
        exclude_ids = {
            e.get("yt_id") for e in queue[max(0, current_index - 1):]
            if e.get("yt_id")
        }
        # Diese beiden bleiben auch in jeder Rückfallebene tabu: der gerade
        # laufende Titel und der unmittelbar nächste.
        protect_ids = {
            e.get("yt_id")
            for e in queue[max(0, current_index - 1):current_index + 1]
            if e.get("yt_id")
        }

        for _ in range(needed):
            item = None
            # Normale Runde: jede Quelle einmal versuchen, damit eine
            # erschöpfte Quelle die Warteschlange nicht blockiert.
            for _try in range(len(active)):
                source      = active[station_idx % len(active)]
                station_idx = (station_idx + 1) % len(active)
                item        = await _build_next_song_entry(
                    uid, source, exclude_ids, protect_ids)
                if item:
                    break
                logger.info("[%s] Keine Auswahl aus '%s'", uid,
                            sources.display_name(source))

            if not item:
                # Letzte Rettung: keine einzige Quelle konnte etwas liefern
                # (alles innerhalb der Mindestsperre). Lieber wiederholen
                # als Stille — der laufende Titel bleibt trotzdem tabu.
                for _try in range(len(active)):
                    source      = active[station_idx % len(active)]
                    station_idx = (station_idx + 1) % len(active)
                    item        = await _build_next_song_entry(
                        uid, source, exclude_ids, protect_ids, relaxed=True)
                    if item:
                        logger.warning(
                            "[%s] Alle Quellen innerhalb der Mindestsperre — "
                            "wiederhole '%s — %s'", uid,
                            item.get("artist", ""), item.get("song", ""))
                        break

            if item:
                queue.append(item)
                if item.get("yt_id"):
                    exclude_ids.add(item["yt_id"])
                all_failed = False
            else:
                logger.warning("[%s] Kein Titel verfügbar — Warteschlange "
                               "bleibt kurz", uid)

        # Nur die beiden geänderten Felder schreiben: zwischen dem Lesen oben
        # und hier liegen lange Auflösungen (yt-dlp) — ein kompletter
        # Rückschreiber würde zwischenzeitliche Änderungen (Lautstärke,
        # Quellenauswahl) wieder auf den alten Stand setzen.
        storage.update_user_state(
            uid, queue=queue, current_station_index=station_idx,
        )

        if all_failed and needed > 0:
            set_user_error(uid, "Kein Titel aus den gewählten Quellen abspielbar.")
        else:
            set_user_error(uid, None)


async def apply_source_change(uid: str) -> None:
    """
    Quellenauswahl wurde geändert: alle noch nicht gespielten Queue-Einträge
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


# Alter Name — Webhooks und HA-Integration rufen ihn weiterhin auf.
apply_station_change = apply_source_change


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


async def advance_queue(uid: str, played_song: dict | None = None,
                        resumed: bool = False) -> dict | None:
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
            # Beim FORTSETZEN eines pausierten Songs steht er schon ganz oben
            # in der History (er wurde beim ersten Start eingetragen) — dann
            # keinen doppelten Eintrag anlegen. Nur dann: eine echte
            # Wiederholung desselben Titels muss sichtbar bleiben, sonst
            # verschweigt die History genau die Fälle, die man sucht.
            skip = False
            if resumed:
                history = storage.read_user_history(uid)
                skip = bool(history) and _same_song(history[0], played_song)
            if not skip:
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

        entry = {k: v for k, v in paused.items()
                 if k not in ("position", "lead_trim")}
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


# ---------------------------------------------------------------------------
# Warteschlange ansehen und bearbeiten
# ---------------------------------------------------------------------------
#
# Während der Wiedergabe zeigt current_index bereits auf den NÄCHSTEN Titel
# (der Producer schiebt den Zeiger beim Songstart weiter). Die Warteschlange
# ist daher queue[current_index:]; alles davor ist Vergangenheit und wird von
# diesen Funktionen nie angefasst. Positionen nach außen zählen ab 0 = als
# nächstes.

def _invalidate_prefetch(uid: str) -> None:
    """
    Der Producer hat den nächsten Titel womöglich schon vordekodiert.
    Nach einer Änderung an der Warteschlange muss er das verwerfen, sonst
    liefe trotzdem noch der alte "nächste" Titel.
    """
    try:
        from .stream_server import _streams
        stream = _streams.get(uid)
        if stream and not stream.stopped:
            stream.invalidate_upcoming()
    except Exception:
        logger.debug("[%s] Prefetch konnte nicht verworfen werden", uid)


def queue_fingerprint(uid: str) -> int:
    """
    Kennzahl, die sich bei jeder Änderung an den kommenden Titeln ändert.
    Die Oberfläche lädt die Warteschlange nur dann neu — das spart bei
    einem Sekundentakt eine Menge überflüssiger Abfragen.
    """
    state = storage.read_user_state(uid)
    queue = state.get("queue", [])
    idx   = max(0, min(int(state.get("current_index", 0) or 0), len(queue)))
    raw   = "|".join(f'{e.get("yt_id","")}:{e.get("song","")}' for e in queue[idx:])
    return zlib.crc32(raw.encode("utf-8", "replace"))


def queue_view(uid: str) -> dict:
    """Kommende Titel (0 = als nächstes) plus der gerade laufende."""
    state = storage.read_user_state(uid)
    queue = state.get("queue", [])
    idx   = max(0, min(int(state.get("current_index", 0) or 0), len(queue)))
    upcoming = [
        {
            "position":    i,
            "artist":      e.get("artist", ""),
            "song":        e.get("song", ""),
            "yt_id":       e.get("yt_id", ""),
            "duration":    e.get("duration"),
            "thumbnail":   e.get("thumbnail", ""),
            "source_name": e.get("source_name") or e.get("station", ""),
            "source_kind": e.get("source_kind", "station"),
        }
        for i, e in enumerate(queue[idx:])
    ]
    current = None
    try:
        from .stream_server import _streams
        stream = _streams.get(uid)
        if stream and not stream.stopped and stream.current_song:
            current = dict(stream.current_song)
    except Exception:
        pass
    return {"current": current, "upcoming": upcoming, "count": len(upcoming)}


async def queue_remove(uid: str, position: int) -> bool:
    async with get_user_lock(uid):
        state = storage.read_user_state(uid)
        queue = state.get("queue", [])
        idx   = int(state.get("current_index", 0) or 0)
        at    = idx + int(position)
        if not (idx <= at < len(queue)):
            return False
        removed = queue.pop(at)
        storage.update_user_state(uid, queue=queue)
    # Nie gespielt → für Rotation und Sperrfrist wieder freigeben
    unmark_unplayed(uid, [removed])
    if int(position) == 0:
        _invalidate_prefetch(uid)
    logger.info("[%s] Aus der Warteschlange entfernt: %s — %s",
                uid, removed.get("artist", ""), removed.get("song", ""))
    return True


async def queue_move(uid: str, position: int, to: int) -> bool:
    async with get_user_lock(uid):
        state = storage.read_user_state(uid)
        queue = state.get("queue", [])
        idx   = int(state.get("current_index", 0) or 0)
        n     = len(queue) - idx
        if n <= 1:
            return False
        frm = int(position)
        dst = max(0, min(int(to), n - 1))
        if not (0 <= frm < n) or frm == dst:
            return False
        item = queue.pop(idx + frm)
        queue.insert(idx + dst, item)
        storage.update_user_state(uid, queue=queue)
    if 0 in (int(position), int(to)):
        _invalidate_prefetch(uid)
    return True


async def queue_clear(uid: str) -> int:
    async with get_user_lock(uid):
        state   = storage.read_user_state(uid)
        queue   = state.get("queue", [])
        idx     = int(state.get("current_index", 0) or 0)
        dropped = queue[idx:]
        storage.update_user_state(uid, queue=queue[:idx])
    unmark_unplayed(uid, dropped)
    _invalidate_prefetch(uid)
    # Sofort wieder auffüllen, damit nach dem laufenden Titel etwas folgt
    await ensure_queue_has_entries(uid, count=1)
    return len(dropped)


async def queue_add(uid: str, artist: str, song: str,
                    yt_id: str | None = None, play_next: bool = False) -> dict | None:
    """
    Einen Titel von Hand einreihen (aus der Suche).

    Die Quelle bleibt leer: ein von Hand gewählter Titel gehört zu keinem
    Sender und keiner Playlist, soll also auch keine Rotationsliste
    beeinflussen. In der dauerhaften Hörliste landet er trotzdem.
    """
    result = await resolve_song(artist, song, yt_id=yt_id)
    if not result:
        return None
    item = {
        "artist":      artist,
        "song":        song,
        "yt_id":       result.get("yt_id", yt_id or ""),
        "thumbnail":   result.get("thumbnail", ""),
        "cover_url":   result.get("cover_url", ""),
        "duration":    result.get("duration"),
        "station":     "",
        "source_kind": "manual",
        "source_name": "Von Hand",
        "queued_at":   int(time.time()),
    }
    async with get_user_lock(uid):
        state = storage.read_user_state(uid)
        queue = state.get("queue", [])
        idx   = max(0, min(int(state.get("current_index", 0) or 0), len(queue)))
        queue.insert(idx if play_next else len(queue), item)
        storage.update_user_state(uid, queue=queue)
    if play_next:
        _invalidate_prefetch(uid)
    logger.info("[%s] Eingereiht (%s): %s — %s", uid,
                "als nächstes" if play_next else "ans Ende", artist, song)
    return item
