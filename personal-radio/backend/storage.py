"""Atomic JSON persistence helpers for Personal Radio."""
from __future__ import annotations

import asyncio
import json
import os
import secrets
import time
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

DATA_ROOT = Path("/data")
LIBRARY_DIR = DATA_ROOT / "library"
CACHE_DIR = DATA_ROOT / "cache"
STATIONS_DIR = DATA_ROOT / "stations"
USERS_DIR = DATA_ROOT / "users"

YT_CACHE_PATH = CACHE_DIR / "yt_ids.json"
ARTIST_BG_CACHE_PATH = CACHE_DIR / "artist_bg.json"
STATION_CURSORS_PATH = DATA_ROOT / "station_cursors.json"


def ensure_dirs() -> None:
    """Create all necessary data directories."""
    for d in [LIBRARY_DIR, CACHE_DIR, STATIONS_DIR, USERS_DIR]:
        d.mkdir(parents=True, exist_ok=True)


def user_dir(uid: str) -> Path:
    p = USERS_DIR / uid
    p.mkdir(parents=True, exist_ok=True)
    (p / "played").mkdir(exist_ok=True)
    return p


# ---------------------------------------------------------------------------
# Atomic read / write
# ---------------------------------------------------------------------------

def read_json(path: Path, default: Any = None) -> Any:
    """Read a JSON file, returning *default* on missing/corrupt files."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def write_json(path: Path, data: Any) -> None:
    """Atomically write *data* as JSON to *path*."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


# ---------------------------------------------------------------------------
# Async wrappers (run blocking I/O in thread pool)
# ---------------------------------------------------------------------------

async def aread_json(path: Path, default: Any = None) -> Any:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, read_json, path, default)


async def awrite_json(path: Path, data: Any) -> None:
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, write_json, path, data)


# ---------------------------------------------------------------------------
# Media token helpers
# ---------------------------------------------------------------------------

def get_or_create_media_token(uid: str) -> str:
    token_path = user_dir(uid) / "media_token"
    if token_path.exists():
        return token_path.read_text().strip()
    token = secrets.token_hex(32)
    token_path.write_text(token)
    return token


# ---------------------------------------------------------------------------
# Station pool helpers
# ---------------------------------------------------------------------------

def station_pool_path(station: str) -> Path:
    return STATIONS_DIR / f"{station}.json"


def read_station_pool(station: str) -> list[dict]:
    return read_json(station_pool_path(station), default=[])


def append_to_station_pool(station: str, entries: list[dict]) -> None:
    """Append new {artist, song} entries to the local station pool."""
    pool = read_station_pool(station)
    existing = {(e["artist"].lower(), e["song"].lower()) for e in pool}
    for entry in entries:
        key = (entry["artist"].lower(), entry["song"].lower())
        if key not in existing:
            pool.append(entry)
            existing.add(key)
    write_json(station_pool_path(station), pool)


# ---------------------------------------------------------------------------
# User state helpers
# ---------------------------------------------------------------------------

def default_state(uid: str) -> dict:
    return {
        "selected_stations": [],
        "current_station_index": 0,
        "queue": [],
        "current_index": 0,
        "player_entity_id": None,
        "is_playing": False,
        "volume": 0.7,
        "media_token": get_or_create_media_token(uid),
    }


def read_user_state(uid: str) -> dict:
    path = user_dir(uid) / "state.json"
    state = read_json(path, default=None)
    if state is None:
        state = default_state(uid)
        write_json(path, state)
    # Ensure media_token always present
    if "media_token" not in state:
        state["media_token"] = get_or_create_media_token(uid)
        write_json(path, state)
    return state


def write_user_state(uid: str, state: dict) -> None:
    write_json(user_dir(uid) / "state.json", state)


def update_user_state(uid: str, **fields: Any) -> dict:
    """
    Nur einzelne Felder des Zustands ändern — der Rest wird FRISCH von der
    Platte gelesen.

    Wichtig gegen verlorene Änderungen: wer einen kompletten state-Dict über
    ein ``await`` hinweg festhält und danach zurückschreibt, überschreibt
    alles, was zwischenzeitlich gespeichert wurde (z.B. eine gerade geänderte
    Senderauswahl, die Sekunden später wieder "zurücksprang"). Diese Funktion
    läuft ohne await, ist also gegenüber anderen Coroutinen atomar.
    """
    state = read_user_state(uid)
    state.update(fields)
    write_user_state(uid, state)
    return state


def read_user_history(uid: str) -> list[dict]:
    return read_json(user_dir(uid) / "history.json", default=[])


def write_user_history(uid: str, history: list[dict]) -> None:
    write_json(user_dir(uid) / "history.json", history)


def add_to_history(uid: str, song_entry: dict) -> None:
    history = read_user_history(uid)
    history.insert(0, song_entry)
    history = history[:50]
    write_user_history(uid, history)


def read_played_ids(uid: str, station: str) -> dict[str, float]:
    """
    Return {yt_id: played_at_unix_ts} for this station.

    Legacy format was a plain list of yt_ids (no timestamps). Those are
    migrated on read: every legacy entry gets the current time as its
    played_at so a configured no-repeat window is honoured conservatively.
    """
    path = user_dir(uid) / "played" / f"{station}.json"
    data = read_json(path, default={})
    if isinstance(data, list):                      # legacy migration
        now = time.time()
        data = {yt_id: now for yt_id in data}
        write_json(path, data)
    return data


def write_played_ids(uid: str, station: str, played: dict[str, float]) -> None:
    write_json(user_dir(uid) / "played" / f"{station}.json", played)


# Global (senderübergreifend) — für die No-Repeat-Spanne pro Titel.
# Einträge älter als 30 Tage werden beim Schreiben automatisch entfernt.
PLAYED_GLOBAL_PRUNE_S = 30 * 24 * 3600


def read_played_global(uid: str) -> dict[str, float]:
    """{yt_id: played_at} über ALLE Sender hinweg."""
    return read_json(user_dir(uid) / "played_global.json", default={})


def write_played_global(uid: str, played: dict[str, float]) -> None:
    cutoff = time.time() - PLAYED_GLOBAL_PRUNE_S
    played = {k: v for k, v in played.items() if v >= cutoff}
    write_json(user_dir(uid) / "played_global.json", played)


# ---------------------------------------------------------------------------
# YT-ID cache
# ---------------------------------------------------------------------------

def read_yt_cache() -> dict:
    return read_json(YT_CACHE_PATH, default={})


def write_yt_cache(cache: dict) -> None:
    write_json(YT_CACHE_PATH, cache)


def yt_cache_key(artist: str, song: str) -> str:
    return f"{artist.lower()}|{song.lower()}"


# ---------------------------------------------------------------------------
# Artist background cache
# ---------------------------------------------------------------------------

def read_artist_bg_cache() -> dict:
    return read_json(ARTIST_BG_CACHE_PATH, default={})


def write_artist_bg_cache(cache: dict) -> None:
    write_json(ARTIST_BG_CACHE_PATH, cache)


# ---------------------------------------------------------------------------
# Station cursors
# ---------------------------------------------------------------------------

def read_cursors() -> dict:
    return read_json(STATION_CURSORS_PATH, default={})


def write_cursors(cursors: dict) -> None:
    write_json(STATION_CURSORS_PATH, cursors)
