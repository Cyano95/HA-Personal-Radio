"""
Quellen: Radiosender und YouTube-Playlists als gleichwertige Einträge.

Bisher kannte der Zustand nur ``selected_stations`` — eine Liste von
Sendernamen, die starr reihum abgespielt wurde. Damit ließen sich weder
Playlists einmischen noch die Reihenfolge ändern.

Eine Quelle ist jetzt:

    {"key": "swr3",       "kind": "station",  "name": "swr3",      "enabled": true}
    {"key": "pl:PL123…",  "kind": "playlist", "name": "Chill Mix", "enabled": true,
     "id": "PL123…", "url": "https://…"}

Der **Schlüssel** ist bewusst so gewählt, dass Sender ihren bisherigen Namen
behalten: die vorhandenen Dateien ``played/<sender>.json`` und die Einträge
der dauerhaften Hörliste bleiben damit gültig.

``selected_stations`` wird weiter mitgeführt (abgeleitet aus den
Sender-Quellen), damit Webhooks, die HA-Integration und bestehende
Automationen unverändert funktionieren.
"""
from __future__ import annotations

import logging

from . import storage

logger = logging.getLogger("personal_radio.sources")

KIND_STATION  = "station"
KIND_PLAYLIST = "playlist"
PLAYLIST_PREFIX = "pl:"


# ── Konstruktion ─────────────────────────────────────────────────────────────

def station_source(name: str, enabled: bool = True) -> dict:
    return {"key": name, "kind": KIND_STATION, "name": name, "enabled": bool(enabled)}


def playlist_source(pl_id: str, name: str = "", url: str = "",
                    enabled: bool = True) -> dict:
    return {
        "key":     PLAYLIST_PREFIX + pl_id,
        "kind":    KIND_PLAYLIST,
        "name":    name or pl_id,
        "id":      pl_id,
        "url":     url,
        "enabled": bool(enabled),
    }


def is_playlist(source: dict) -> bool:
    return source.get("kind") == KIND_PLAYLIST


def playlist_id(source: dict) -> str:
    return source.get("id") or str(source.get("key", ""))[len(PLAYLIST_PREFIX):]


def display_name(source: dict) -> str:
    return source.get("name") or source.get("key", "")


# ── Zustand lesen / schreiben ────────────────────────────────────────────────

def from_state(state: dict) -> list[dict]:
    """
    Quellenliste aus dem Zustand — mit Migration aus ``selected_stations``
    für Installationen, die es noch nicht gibt.
    """
    raw = state.get("sources")
    if isinstance(raw, list) and raw:
        return [s for s in (_clean(e) for e in raw) if s]
    return [station_source(n) for n in state.get("selected_stations", []) if n]


def _clean(entry) -> dict | None:
    if not isinstance(entry, dict):
        return None
    key = str(entry.get("key") or "").strip()
    if not key:
        return None
    kind = entry.get("kind")
    if kind not in (KIND_STATION, KIND_PLAYLIST):
        kind = KIND_PLAYLIST if key.startswith(PLAYLIST_PREFIX) else KIND_STATION
    out = {
        "key":     key,
        "kind":    kind,
        "name":    str(entry.get("name") or key),
        "enabled": entry.get("enabled", True) is not False,
    }
    if kind == KIND_PLAYLIST:
        out["id"]  = str(entry.get("id") or key[len(PLAYLIST_PREFIX):])
        out["url"] = str(entry.get("url") or "")
    return out


def enabled_sources(state: dict) -> list[dict]:
    return [s for s in from_state(state) if s.get("enabled", True)]


def to_state_fields(sources: list[dict]) -> dict:
    """
    Felder, die in den Zustand geschrieben werden.

    ``selected_stations`` wird mitgeschrieben, damit alles, was es bisher
    liest (Webhooks, HA-Integration, Sender-Polling), weiter funktioniert.
    """
    clean = [s for s in (_clean(e) for e in sources) if s]
    return {
        "sources": clean,
        "selected_stations": [s["name"] for s in clean
                              if s["kind"] == KIND_STATION and s.get("enabled", True)],
    }


def apply_station_list(state: dict, stations: list[str]) -> list[dict]:
    """
    Senderauswahl (alte Schnittstelle) auf die Quellenliste anwenden:
    Playlists bleiben erhalten, Sender werden abgeglichen.

    Die Reihenfolge der bestehenden Einträge bleibt dabei stehen — nur
    weggefallene Sender verschwinden, neue kommen hinten dran.
    """
    wanted  = [s for s in stations if s]
    current = from_state(state)
    out: list[dict] = []
    for src in current:
        if src["kind"] == KIND_PLAYLIST:
            out.append(src)
        elif src["name"] in wanted:
            out.append({**src, "enabled": True})
    have = {s["name"] for s in out if s["kind"] == KIND_STATION}
    for name in wanted:
        if name not in have:
            out.append(station_source(name))
    return out


# ── Titelvorrat einer Quelle ─────────────────────────────────────────────────

def pool_entries(source: dict) -> list[dict]:
    """
    Titel der Quelle, in derselben Form wie bisher die Sender-Pools:
    ``[{"artist": …, "song": …}]`` — bei Playlists zusätzlich mit ``yt_id``,
    weil die Video-ID dort schon bekannt ist und die Suche entfällt.
    """
    if is_playlist(source):
        data = storage.read_playlist(playlist_id(source)) or {}
        return list(data.get("tracks") or [])
    return storage.read_station_pool(source.get("key", ""))


def pool_size(source: dict) -> int:
    return len(pool_entries(source))
