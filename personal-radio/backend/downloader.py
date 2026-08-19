"""
Audio resolver: yt-dlp stream-URL resolution + MusicBrainz cover art.

Strategy (hybrid — no downloads):
  1. If a local MP3 already exists from a previous run → use it (backwards compat).
  2. If a non-expired stream URL is in the cache          → use it directly.
  3. Otherwise → call yt-dlp with download=False to resolve a fresh stream URL
     and cache it for STREAM_URL_TTL seconds.

Stream URLs from YouTube expire (~6 h).  We refresh them 5 min before expiry.
yt-dlp URL resolution takes ~2-5 s, which is fine inside the prefetch window.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any

import httpx
import yt_dlp

from . import storage

logger = logging.getLogger("personal_radio.downloader")

LIBRARY_DIR      = storage.LIBRARY_DIR
STREAM_URL_TTL   = 4 * 3600   # seconds before we refresh the stream URL
STREAM_URL_MARGIN = 5 * 60    # refresh this many seconds before expiry

# Default upper duration bound for yt-dlp search results (avoid full
# albums / hour-long mixes). Raised if the user allows longer songs.
DEFAULT_MAX_SEARCH_DURATION = 600


def max_song_seconds() -> int:
    """
    Addon option: maximale Spieldauer eines Titels in Minuten.
    0 = unbegrenzt. Titel, die länger sind, werden nicht gespielt.
    """
    try:
        minutes = int(os.environ.get("MAX_SONG_MINUTES", "0") or 0)
    except (TypeError, ValueError):
        minutes = 0
    return max(0, minutes) * 60

MB_SEMAPHORE: asyncio.Semaphore | None = None
_MB_LAST_REQUEST: float = 0.0
_resolve_locks: dict[str, asyncio.Lock] = {}
_user_prefetch_locks: dict[str, asyncio.Lock] = {}


def get_user_prefetch_lock(uid: str) -> asyncio.Lock:
    if uid not in _user_prefetch_locks:
        _user_prefetch_locks[uid] = asyncio.Lock()
    return _user_prefetch_locks[uid]


def _mb_semaphore() -> asyncio.Semaphore:
    global MB_SEMAPHORE
    if MB_SEMAPHORE is None:
        MB_SEMAPHORE = asyncio.Semaphore(1)
    return MB_SEMAPHORE


# ---------------------------------------------------------------------------
# yt-dlp stream URL resolution (sync, runs in executor)
# ---------------------------------------------------------------------------

def _resolve_stream_url_sync(artist: str, song: str) -> dict | None:
    """
    Use yt-dlp to find the best audio stream URL — no download.
    Returns dict with yt_id, stream_url, thumbnail, duration or None on failure.
    """
    query = f"ytsearch1:{artist} {song}"
    # Suchfilter: Obergrenze anheben, wenn der Nutzer längere Titel erlaubt.
    # Die eigentliche Durchsetzung von max_song_seconds passiert NACH dem
    # Resolve anhand der gecachten Dauer (nicht-destruktiv: wird das Limit
    # später erhöht, ist der Titel sofort wieder spielbar).
    limit = max_song_seconds()
    upper = max(DEFAULT_MAX_SEARCH_DURATION, limit) if limit else DEFAULT_MAX_SEARCH_DURATION
    opts = {
        "format":        "bestaudio[ext=webm]/bestaudio[ext=m4a]/bestaudio/best",
        "quiet":         True,
        "no_warnings":   True,
        "noplaylist":    True,
        "match_filter":  yt_dlp.utils.match_filter_func(
                             f"duration >= 60 & duration <= {upper}"
                         ),
    }
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(query, download=False)
            if not info:
                return None
            entry = info["entries"][0] if "entries" in info else info
            if not entry:
                return None

            # Prefer a direct URL on the entry; fall back to formats list
            stream_url:   str | None  = entry.get("url")
            http_headers: dict | None = entry.get("http_headers")
            if not stream_url:
                fmts = entry.get("formats") or []
                # Audio-only first, then any with audio
                audio_only = [f for f in fmts
                              if f.get("url")
                              and f.get("acodec") != "none"
                              and f.get("vcodec") in (None, "none")]
                all_audio  = [f for f in fmts
                              if f.get("url") and f.get("acodec") != "none"]
                candidates = audio_only or all_audio
                if candidates:
                    best = max(candidates, key=lambda f: f.get("abr") or 0)
                    stream_url   = best["url"]
                    http_headers = best.get("http_headers") or http_headers

            if not stream_url:
                return None

            # Nur unkritische Standard-Header übernehmen (kein Cookie o.ä.)
            if http_headers:
                http_headers = {
                    k: v for k, v in http_headers.items()
                    if k.lower() in ("user-agent", "accept", "accept-language",
                                     "origin", "referer")
                }

            return {
                "yt_id":      entry["id"],
                "stream_url": stream_url,
                "thumbnail":  entry.get("thumbnail", ""),
                "duration":   entry.get("duration"),
                "stream_url_expires_at": int(time.time()) + STREAM_URL_TTL,
                "http_headers": http_headers or None,
            }
    except Exception as e:
        logger.debug("URL resolve failed for '%s — %s': %s", artist, song, e)
        return None


# ---------------------------------------------------------------------------
# MusicBrainz cover art
# ---------------------------------------------------------------------------

async def _mb_request(client: httpx.AsyncClient, url: str, params: dict) -> Any | None:
    global _MB_LAST_REQUEST
    async with _mb_semaphore():
        elapsed = time.monotonic() - _MB_LAST_REQUEST
        if elapsed < 1.0:
            await asyncio.sleep(1.0 - elapsed)
        try:
            resp = await client.get(
                url, params=params, timeout=10,
                headers={"User-Agent": "PersonalRadioHA/1.0"},
            )
            _MB_LAST_REQUEST = time.monotonic()
            if resp.status_code == 429:
                await asyncio.sleep(60)
                return None
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            logger.debug("MusicBrainz request failed: %s", e)
            _MB_LAST_REQUEST = time.monotonic()
            return None


async def _fetch_cover_url(artist: str, song: str, yt_thumbnail: str) -> str:
    async with httpx.AsyncClient() as client:
        data = await _mb_request(
            client,
            "https://musicbrainz.org/ws/2/recording/",
            {"query": f'artist:"{artist}" AND recording:"{song}"',
             "limit": 1, "inc": "releases", "fmt": "json"},
        )
        if not data:
            return yt_thumbnail
        recordings = data.get("recordings", [])
        if not recordings:
            return yt_thumbnail
        releases = recordings[0].get("releases", [])
        if not releases:
            return yt_thumbnail
        mbid = releases[0].get("id")
        if not mbid:
            return yt_thumbnail
        try:
            resp = await client.get(
                f"https://coverartarchive.org/release/{mbid}",
                timeout=10,
                headers={"User-Agent": "PersonalRadioHA/1.0"},
                follow_redirects=True,
            )
            if resp.status_code == 200:
                images = resp.json().get("images", [])
                front  = next((i for i in images if i.get("front")), None)
                if front:
                    thumbs = front.get("thumbnails", {})
                    url = thumbs.get("500") or thumbs.get("250") or front.get("image", "")
                    if url:
                        return url
        except Exception:
            pass
    return yt_thumbnail


# ---------------------------------------------------------------------------
# Main entry points
# ---------------------------------------------------------------------------

async def resolve_song(artist: str, song: str) -> dict | None:
    """
    Resolve a song to a playable source, returning full metadata for the queue.
    Does NOT download — resolves a stream URL if no local file exists.

    Returns:
        {yt_id, artist, song, thumbnail, cover_url, duration, stream_url,
         stream_url_expires_at, failed, downloaded}
        or None if the song cannot be resolved at all.
    """
    loop      = asyncio.get_running_loop()
    cache_key = storage.yt_cache_key(artist, song)

    cache = await loop.run_in_executor(None, storage.read_yt_cache)
    entry = cache.get(cache_key)

    if entry and entry.get("failed"):
        return None

    # ── 1. Local MP3 still exists ─────────────────────────────────────────
    if entry and entry.get("yt_id"):
        mp3_path = LIBRARY_DIR / f"{entry['yt_id']}.mp3"
        if mp3_path.exists():
            if not entry.get("cover_url") and entry.get("thumbnail"):
                entry["cover_url"] = await _fetch_cover_url(
                    artist, song, entry["thumbnail"]
                )
                cache[cache_key] = entry
                await loop.run_in_executor(None, storage.write_yt_cache, cache)
            return dict(entry)

    # ── 2. Cached stream URL still valid ──────────────────────────────────
    now = int(time.time())
    if (entry and entry.get("stream_url")
            and entry.get("stream_url_expires_at", 0) > now + STREAM_URL_MARGIN):
        return dict(entry)

    # ── 3. Resolve fresh stream URL via yt-dlp ────────────────────────────
    lock_key = cache_key
    if lock_key not in _resolve_locks:
        _resolve_locks[lock_key] = asyncio.Lock()

    async with _resolve_locks[lock_key]:
        # Re-read cache after acquiring lock — another coroutine may have done it
        cache = await loop.run_in_executor(None, storage.read_yt_cache)
        entry = cache.get(cache_key)
        now   = int(time.time())
        if (entry and not entry.get("failed")
                and entry.get("stream_url")
                and entry.get("stream_url_expires_at", 0) > now + STREAM_URL_MARGIN):
            return dict(entry)

        logger.info("Resolving stream URL: %s — %s", artist, song)
        result = await loop.run_in_executor(None, _resolve_stream_url_sync, artist, song)

        if result is None:
            logger.warning("Resolve permanently failed: %s — %s", artist, song)
            cache[cache_key] = {
                "artist": artist, "song": song,
                "failed": True, "downloaded": False,
                "downloaded_at": now,
            }
            await loop.run_in_executor(None, storage.write_yt_cache, cache)
            return None

        # Cover ist reine Kosmetik — ein Fehler hier (Netz, API, Bibliothek)
        # darf die Songauflösung NIE mitreißen.
        try:
            cover_url = await _fetch_cover_url(
                artist, song, result.get("thumbnail", "")
            )
        except Exception as e:
            logger.debug("Cover fetch failed for '%s — %s': %s", artist, song, e)
            cover_url = result.get("thumbnail", "")

        new_entry: dict = {
            **(entry or {}),
            "yt_id":                  result["yt_id"],
            "artist":                 artist,
            "song":                   song,
            "stream_url":             result["stream_url"],
            "stream_url_expires_at":  result["stream_url_expires_at"],
            "http_headers":           result.get("http_headers"),
            "thumbnail":              result.get("thumbnail", ""),
            "cover_url":              cover_url,
            "duration":               result.get("duration"),
            "failed":                 False,
            "downloaded":             False,
            "downloaded_at":          now,
        }
        cache[cache_key] = new_entry
        await loop.run_in_executor(None, storage.write_yt_cache, cache)
        return new_entry


async def get_source_headers(artist: str, song: str) -> dict | None:
    """
    HTTP-Header (v.a. User-Agent), mit denen yt-dlp die Stream-URL angefragt
    hat. ffmpeg soll dieselben verwenden — YouTube prüft zunehmend, ob der
    Abrufer zum Resolver passt, sonst gibt es 403.
    """
    loop  = asyncio.get_running_loop()
    cache = await loop.run_in_executor(None, storage.read_yt_cache)
    entry = cache.get(storage.yt_cache_key(artist, song))
    headers = (entry or {}).get("http_headers")
    return dict(headers) if isinstance(headers, dict) and headers else None


async def invalidate_stream_url(artist: str, song: str) -> None:
    """
    Gecachte Stream-URL verwerfen (z.B. weil ffmpeg damit nichts oder nur
    hängend liefert). Der nächste Zugriff resolved frisch; yt_id, Cover
    und Dauer bleiben erhalten.
    """
    def _sync() -> None:
        cache_key = storage.yt_cache_key(artist, song)
        cache     = storage.read_yt_cache()
        entry     = cache.get(cache_key)
        if entry and entry.get("stream_url"):
            entry["stream_url"]            = None
            entry["stream_url_expires_at"] = 0
            storage.write_yt_cache(cache)
            logger.info("Stream-URL invalidiert: %s — %s", artist, song)
    await asyncio.get_running_loop().run_in_executor(None, _sync)


async def get_audio_source(artist: str, song: str) -> str | None:
    """
    Return the string path or URL the ffmpeg decoder should open.
    Refreshes expired stream URLs transparently.
    Returns None if the song cannot be resolved.
    """
    loop      = asyncio.get_running_loop()
    cache_key = storage.yt_cache_key(artist, song)
    cache     = await loop.run_in_executor(None, storage.read_yt_cache)
    entry     = cache.get(cache_key)

    if entry and entry.get("failed"):
        return None

    # Local file (backwards compat with old downloads)
    if entry and entry.get("yt_id"):
        mp3_path = LIBRARY_DIR / f"{entry['yt_id']}.mp3"
        if mp3_path.exists():
            return str(mp3_path)

    # Valid cached stream URL
    now = int(time.time())
    if (entry and entry.get("stream_url")
            and entry.get("stream_url_expires_at", 0) > now + STREAM_URL_MARGIN):
        return entry["stream_url"]

    # Need a fresh URL — resolve_song handles locking + caching
    resolved = await resolve_song(artist, song)
    if not resolved:
        return None

    # Local file may have been created by a parallel task
    if resolved.get("yt_id"):
        mp3_path = LIBRARY_DIR / f"{resolved['yt_id']}.mp3"
        if mp3_path.exists():
            return str(mp3_path)

    return resolved.get("stream_url")


# ---------------------------------------------------------------------------
# Per-user prefetch (resolve URLs for upcoming songs)
# ---------------------------------------------------------------------------

async def prefetch_for_user(uid: str, queue: list[dict], current_index: int) -> None:
    """Pre-resolve the stream URL for the NEXT song only (one song ahead)."""
    lock_file = storage.user_dir(uid) / "prefetch.lock"
    if lock_file.exists():
        try:
            if time.time() - float(lock_file.read_text()) < 600:
                return
        except Exception:
            pass

    async with get_user_prefetch_lock(uid):
        lock_file.write_text(str(time.time()))
        try:
            for i in range(current_index + 1,
                           min(current_index + 2, len(queue))):
                item = queue[i]
                result = await resolve_song(item["artist"], item["song"])
                if result:
                    item["yt_id"]     = result.get("yt_id", "")
                    item["thumbnail"] = result.get("thumbnail", "")
                    item["cover_url"] = result.get("cover_url", "")
                    logger.info("[%s] Pre-resolved: %s — %s", uid,
                                item["artist"], item["song"])
        finally:
            lock_file.unlink(missing_ok=True)


async def clear_stream_urls(yt_ids: list[str] | None = None) -> None:
    """
    Remove cached stream URLs after a stream ends so they don't linger.
    If *yt_ids* is given, only clear entries for those IDs.
    They'll be re-resolved quickly on next play.
    """
    loop  = asyncio.get_running_loop()
    cache = await loop.run_in_executor(None, storage.read_yt_cache)
    id_set = set(yt_ids) if yt_ids else None
    changed = False
    for key, entry in cache.items():
        if id_set and entry.get("yt_id") not in id_set:
            continue
        if entry.get("stream_url"):
            entry.pop("stream_url", None)
            entry.pop("stream_url_expires_at", None)
            changed = True
    if changed:
        await loop.run_in_executor(None, storage.write_yt_cache, cache)
        logger.debug("Cleared stream URLs (%s entries)", "all" if not id_set else len(id_set))
