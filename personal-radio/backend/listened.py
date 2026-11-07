"""
Dauerhafte Liste gehörter Titel.

Warum eigen und nicht die vorhandenen Played-Dateien?

Die bisherigen Listen (`played/<sender>.json`, `played_global.json`) sind an
die YouTube-ID gebunden, werden nach 30 Tagen beschnitten und liegen nur im
Addon-Verzeichnis. Nach einer Neuinstallation ist alles weg, und selbst ein
Backup half wenig: beim erneuten Auflösen kann derselbe Titel eine andere
YouTube-ID bekommen.

Diese Liste ist deshalb
  • nach **Interpret + Titel** geschlüsselt (die ID wird nur mitgeführt),
  • unbefristet (kein automatisches Beschneiden),
  • eine einzelne, portable Datei, die sich per WebDAV abgleichen,
    exportieren und importieren lässt.

Beim Start werden daraus die Rotationslisten wieder aufgebaut — die
Auswahl-Logik in `queue_manager` bleibt dadurch unverändert, überlebt aber
Neustart und Neuinstallation.

Dateiformat (`/data/listened.json`):

    {
      "version": 1,
      "updated_at": 1730000000,
      "cycles": {"swr3": 1729000000},        # Beginn der laufenden Runde
      "songs": {
        "artist|title": {
          "a": "Artist", "s": "Title",       # Originalschreibweise
          "y": "ytid", "st": "swr3",         # zuletzt benutzte ID / Sender
          "t": 1730000000,                   # zuletzt gehört
          "n": 3                             # wie oft gehört
        }
      }
    }
"""
from __future__ import annotations

import logging
import re
import threading
import time
from typing import Any

from . import storage

logger = logging.getLogger("personal_radio.listened")

FILE_VERSION = 1
_lock = threading.Lock()
_cache: dict | None = None
# {yt_id: zuletzt gehört} — damit zählt dieselbe Aufnahme auch dann als
# gehört, wenn sie unter abweichender Schreibweise eingereiht wird
# ("Song" vs. "Song (Remastered)"), was die Senderlogs regelmäßig liefern.
_by_id: dict[str, float] | None = None

_WS = re.compile(r"\s+")


def path():
    return storage.DATA_ROOT / "listened.json"


def key_for(artist: str, song: str) -> str:
    """
    Schlüssel aus Interpret und Titel.

    Bewusst nur Kleinschreibung und vereinheitlichte Leerzeichen: aggressiveres
    Normalisieren (Klammerzusätze entfernen o.ä.) würde unterschiedliche
    Fassungen desselben Titels zusammenwerfen.
    """
    a = _WS.sub(" ", str(artist or "").strip().lower())
    s = _WS.sub(" ", str(song or "").strip().lower())
    return f"{a}|{s}"


def _empty() -> dict:
    return {"version": FILE_VERSION, "updated_at": 0, "cycles": {}, "songs": {}}


def _ids() -> dict[str, float]:
    """Index nach Video-ID, bei Bedarf aufgebaut."""
    global _by_id
    if _by_id is None:
        idx: dict[str, float] = {}
        for e in _load().get("songs", {}).values():
            yt_id = str(e.get("y", "") or "")
            if not yt_id:
                continue
            try:
                ts = float(e.get("t", 0) or 0)
            except (TypeError, ValueError):
                continue
            if ts > idx.get(yt_id, 0):
                idx[yt_id] = ts
        _by_id = idx
    return _by_id


def _load() -> dict:
    global _cache
    if _cache is None:
        data = storage.read_json(path(), default=None)
        if not isinstance(data, dict) or "songs" not in data:
            data = _empty()
        data.setdefault("cycles", {})
        data.setdefault("songs", {})
        data.setdefault("version", FILE_VERSION)
        _cache = data
    return _cache


def _save() -> None:
    data = _load()
    data["updated_at"] = int(time.time())
    storage.write_json(path(), data)


def reload_from_disk() -> None:
    """Cache verwerfen — nach einem Import oder Abgleich von außen."""
    global _cache, _by_id
    with _lock:
        _cache = None
        _by_id = None


# ── Schreiben ────────────────────────────────────────────────────────────────

def record(song: dict, ts: float | None = None) -> None:
    """Einen tatsächlich abgespielten Titel vermerken."""
    artist = song.get("artist", "")
    title  = song.get("song", "")
    if not artist and not title:
        return
    k  = key_for(artist, title)
    ts = int(ts if ts is not None else time.time())
    with _lock:
        data  = _load()
        entry = data["songs"].get(k) or {"n": 0}
        entry.update({
            "a": artist,
            "s": title,
            "y": song.get("yt_id") or entry.get("y", ""),
            "st": song.get("station") or entry.get("st", ""),
            "t": max(int(entry.get("t", 0) or 0), ts),
            "n": int(entry.get("n", 0) or 0) + 1,
        })
        data["songs"][k] = entry
        yt_id = entry.get("y") or ""
        if yt_id and _by_id is not None and ts > _by_id.get(yt_id, 0):
            _by_id[yt_id] = float(ts)
        _save()


def start_cycle(station: str, ts: float | None = None) -> None:
    """
    Eine neue Rotationsrunde für einen Sender beginnt.

    Alles, was davor gehört wurde, zählt für diesen Sender nicht mehr als
    "schon gespielt" — so funktioniert die Vollrotation auch dann noch,
    wenn die Liste von einem anderen Gerät übernommen wurde.
    """
    if not station:
        return
    with _lock:
        data = _load()
        # Mit Nachkommastellen: bei ganzen Sekunden galt ein Titel, der in
        # derselben Sekunde lief wie der Rundenstart, noch als gespielt —
        # dann lieferte die Quelle für den Rest der Sekunde gar nichts.
        data["cycles"][station] = round(float(ts if ts is not None else time.time()), 3)
        _save()


# ── Lesen ────────────────────────────────────────────────────────────────────

def last_played(artist: str, song: str) -> float | None:
    """Wann wurde dieser Titel zuletzt gehört? None = noch nie."""
    entry = _load()["songs"].get(key_for(artist, song))
    if not entry:
        return None
    try:
        return float(entry.get("t", 0)) or None
    except (TypeError, ValueError):
        return None


def cycle_start(station: str) -> float:
    try:
        return float(_load()["cycles"].get(station, 0) or 0)
    except (TypeError, ValueError):
        return 0.0


def ensure_cycle(station: str) -> float:
    """
    Rundenbeginn einer Quelle — fehlt er, beginnt die Runde jetzt.

    Ohne das stünde eine frisch hinzugefügte Quelle bei 0: alles jemals
    Gehörte zählte für sie als gespielt, der erste Zugriff löste sofort
    einen Rundenneustart aus und die dauerhafte Liste war für diese Quelle
    damit wirkungslos.
    """
    if not station:
        return 0.0
    cur = cycle_start(station)
    if cur > 0:
        return cur
    now = round(time.time(), 3)
    with _lock:
        data = _load()
        if not data["cycles"].get(station):
            data["cycles"][station] = now
            _save()
        return float(data["cycles"][station])


def last_played_id(yt_id: str) -> float | None:
    """Wann lief diese Aufnahme (Video-ID) zuletzt? None = noch nie."""
    if not yt_id:
        return None
    return _ids().get(yt_id) or None


def stats() -> dict:
    data  = _load()
    songs = data["songs"]
    last  = max((int(e.get("t", 0) or 0) for e in songs.values()), default=0)
    return {
        "count":      len(songs),
        "plays":      sum(int(e.get("n", 0) or 0) for e in songs.values()),
        "last_at":    last,
        "updated_at": int(data.get("updated_at", 0) or 0),
    }


def snapshot() -> dict:
    """Kopie des gesamten Bestands — für Export und WebDAV-Upload."""
    with _lock:
        data = _load()
        return {
            "version":    FILE_VERSION,
            "updated_at": int(data.get("updated_at", 0) or 0),
            "cycles":     dict(data.get("cycles", {})),
            "songs":      {k: dict(v) for k, v in data.get("songs", {}).items()},
        }


# ── Zusammenführen ───────────────────────────────────────────────────────────

def merge(remote: Any) -> dict:
    """
    Fremdbestand einarbeiten (WebDAV oder Import).

    Zusammengeführt wird additiv: bei jedem Titel gewinnt der jüngere
    Zeitstempel und die höhere Zählung. Dadurch ist die Reihenfolge der
    Abgleiche egal und nichts geht verloren, auch wenn zwei Installationen
    parallel gehört haben.
    """
    if not isinstance(remote, dict):
        raise ValueError("Kein gültiger Datensatz")
    rsongs = remote.get("songs")
    if not isinstance(rsongs, dict):
        raise ValueError("Feld 'songs' fehlt")

    added = updated = 0
    with _lock:
        data  = _load()
        songs = data["songs"]
        for k, rv in rsongs.items():
            if not isinstance(rv, dict):
                continue
            try:
                rt = int(rv.get("t", 0) or 0)
                rn = int(rv.get("n", 0) or 0)
            except (TypeError, ValueError):
                continue
            cur = songs.get(k)
            if cur is None:
                songs[k] = {
                    "a": str(rv.get("a", "")), "s": str(rv.get("s", "")),
                    "y": str(rv.get("y", "")), "st": str(rv.get("st", "")),
                    "t": rt, "n": max(rn, 1),
                }
                added += 1
                continue
            ct = int(cur.get("t", 0) or 0)
            cn = int(cur.get("n", 0) or 0)
            if rt > ct or rn > cn:
                updated += 1
            if rt > ct:
                cur["t"] = rt
                # Die Angaben der jüngeren Wiedergabe sind die aktuelleren
                for src, dst in (("a", "a"), ("s", "s"), ("y", "y"), ("st", "st")):
                    if rv.get(src):
                        cur[dst] = str(rv[src])
            cur["n"] = max(cn, rn)

        rcycles = remote.get("cycles")
        if isinstance(rcycles, dict):
            for st, ts in rcycles.items():
                try:
                    ts = int(ts)
                except (TypeError, ValueError):
                    continue
                if ts > int(data["cycles"].get(st, 0) or 0):
                    data["cycles"][st] = ts

        _save()
    global _by_id
    _by_id = None
    return {"added": added, "updated": updated, "total": len(data["songs"])}


# ── Rotationslisten wiederherstellen ─────────────────────────────────────────

def rebuild_played_sets(uid: str) -> dict:
    """
    Aus der dauerhaften Liste die Rotationslisten neu aufbauen.

    Wird beim Start und nach jedem Abgleich aufgerufen. Danach sieht die
    Auswahl-Logik genau das, was sie vor einem Neustart gesehen hat — und
    nach einer Neuinstallation das, was die abgeglichene Liste mitbringt.
    """
    data   = _load()
    songs  = data.get("songs", {})
    cycles = data.get("cycles", {})
    if not songs:
        return {"stations": 0, "songs": 0}

    cache   = storage.read_yt_cache()
    global_ids: dict[str, float] = {}
    by_station: dict[str, dict[str, float]] = {}

    for k, e in songs.items():
        ts = int(e.get("t", 0) or 0)
        if not ts:
            continue
        yt_id = str(e.get("y", "") or "")
        if not yt_id:
            # ID unbekannt (z.B. von einem anderen Gerät) — aus dem
            # Auflösungs-Cache nachschlagen, sonst zählt nur der Titel.
            entry = cache.get(storage.yt_cache_key(e.get("a", ""), e.get("s", "")))
            yt_id = (entry or {}).get("yt_id", "")
        if not yt_id:
            continue
        if ts > global_ids.get(yt_id, 0):
            global_ids[yt_id] = ts
        station = str(e.get("st", "") or "")
        if station and ts >= int(cycles.get(station, 0) or 0):
            by_station.setdefault(station, {})[yt_id] = ts

    storage.write_played_global(uid, global_ids)
    for station, ids in by_station.items():
        merged = storage.read_played_ids(uid, station)
        merged.update(ids)
        storage.write_played_ids(uid, station, merged)

    logger.info("[%s] Rotationslisten aus der dauerhaften Liste aufgebaut: "
                "%d Titel, %d Sender", uid, len(global_ids), len(by_station))
    return {"stations": len(by_station), "songs": len(global_ids)}
