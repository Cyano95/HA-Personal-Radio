"""
Suche auf YouTube über yt-dlp — Titel und Playlists.

Bewusst ohne YouTube Data API: kein Schlüssel, kein Kontingent. Dafür gibt
es keine echten Empfehlungen; "Vorschläge" leiten sich aus dem ab, was
tatsächlich gehört wurde (siehe `suggestions`).

Alle Aufrufe laufen über ``extract_flat``: yt-dlp holt damit nur die
Trefferliste und löst keine Stream-URLs auf. Das ist um Größenordnungen
schneller und schont das Kontingent, das YouTube pro IP toleriert.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from urllib.parse import parse_qs, quote, urlparse

import yt_dlp

from . import storage

logger = logging.getLogger("personal_radio.ytsearch")

# YouTube-Suchfilter "nur Playlists" (sp-Parameter der Ergebnisseite)
_SP_PLAYLISTS = "EgIQAw%3D%3D"

SEARCH_TIMEOUT   = 45.0
PLAYLIST_TIMEOUT = 120.0
MAX_PLAYLIST     = 500

# Zusätze, die in YouTube-Titeln stehen, aber nicht zum Songtitel gehören.
# Bewusst eine feste Liste: alles in Klammern pauschal zu entfernen würde
# auch Sinnvolles treffen ("(Live in Berlin)", "(feat. …)").
_NOISE_WORDS = r"""
      official\s*(?:music\s*)?(?:video|audio|lyric\s*video|visuali[sz]er)?
    | video\s*oficial | clip\s*officiel | lyrics? | lyric\s*video
    | audio | hq | hd | uhd | 4k | 8k | mv | m/v
    | remaster(?:ed)?(?:\s*\d{4})? | full\s*album | visuali[sz]er
    | explicit | clean | radio\s*edit
"""
_FLAGS   = re.IGNORECASE | re.VERBOSE
# … in Klammern: "(Official Video)", "[Remastered 2011]"
_BRACKET = re.compile(r"\s*[\(\[\{]\s*(?:" + _NOISE_WORDS +
                      r")\s*[^\)\]\}]*[\)\]\}]", _FLAGS)
# … oder hinter einem senkrechten Strich am Ende: "… | HD"
_PIPE    = re.compile(r"\s*\|\s*(?:" + _NOISE_WORDS + r")\s*$", _FLAGS)

_DASHES = (" - ", " – ", " — ", " ‒ ", " − ")


def _clean_title(title: str) -> str:
    out = str(title or "")
    for _ in range(4):                      # mehrere Zusätze hintereinander
        new = _PIPE.sub("", _BRACKET.sub("", out))
        if new == out:
            break
        out = new
    return re.sub(r"\s{2,}", " ", out).strip(" -–—|·")


def parse_track(title: str, uploader: str = "") -> tuple[str, str]:
    """
    "Interpret - Titel (Official Video)" → ("Interpret", "Titel").

    Ohne Trennstrich wird der Kanal als Interpret genommen; bei
    Musikkanälen hängt YouTube dort " - Topic" an.
    """
    clean = _clean_title(title)
    for dash in _DASHES:
        if dash in clean:
            artist, song = clean.split(dash, 1)
            artist, song = artist.strip(), song.strip()
            if artist and song:
                return artist, song
    channel = re.sub(r"\s*-\s*Topic$", "", str(uploader or "")).strip()
    return channel, clean or str(title or "").strip()


def playlist_id_from(value: str) -> str:
    """Playlist-ID aus einer URL oder direkt durchgereichter ID."""
    v = str(value or "").strip()
    if not v:
        return ""
    if "://" in v or v.startswith("www."):
        q = parse_qs(urlparse(v if "://" in v else "https://" + v).query)
        if q.get("list"):
            return q["list"][0]
    return v if re.fullmatch(r"[A-Za-z0-9_-]{10,}", v) else ""


# ── yt-dlp-Aufrufe (synchron, laufen im Threadpool) ──────────────────────────

def _flat_opts(limit: int | None = None) -> dict:
    opts = {
        "quiet": True, "no_warnings": True, "skip_download": True,
        "extract_flat": "in_playlist", "ignoreerrors": True,
    }
    if limit:
        opts["playlistend"] = limit
    return opts


def _extract(url: str, limit: int | None = None) -> dict | None:
    try:
        with yt_dlp.YoutubeDL(_flat_opts(limit)) as ydl:
            return ydl.extract_info(url, download=False)
    except Exception as e:
        logger.warning("yt-dlp '%s' fehlgeschlagen: %s", url[:80], e)
        return None


def _search_tracks_sync(query: str, limit: int) -> list[dict]:
    info = _extract(f"ytsearch{limit}:{query}", limit)
    out  = []
    for e in (info or {}).get("entries") or []:
        if not e or not e.get("id"):
            continue
        artist, song = parse_track(e.get("title", ""),
                                   e.get("uploader") or e.get("channel") or "")
        out.append({
            "yt_id":    e["id"],
            "title":    e.get("title", ""),
            "artist":   artist,
            "song":     song,
            "duration": e.get("duration"),
            "uploader": e.get("uploader") or e.get("channel") or "",
            "thumbnail": (e.get("thumbnails") or [{}])[-1].get("url", ""),
        })
    return out


def _search_playlists_sync(query: str, limit: int) -> list[dict]:
    url  = (f"https://www.youtube.com/results?search_query={quote(query)}"
            f"&sp={_SP_PLAYLISTS}")
    info = _extract(url, limit)
    out  = []
    for e in (info or {}).get("entries") or []:
        if not e:
            continue
        pl_id = e.get("id") or playlist_id_from(e.get("url", ""))
        # Die gefilterte Ergebnisseite liefert gelegentlich doch Videos mit —
        # Playlist-IDs beginnen mit PL/OL/RD/UU/LL/FL.
        if not pl_id or not re.match(r"^(PL|OL|RD|UU|LL|FL)", pl_id):
            continue
        out.append({
            "id":    pl_id,
            "name":  e.get("title", pl_id),
            "url":   f"https://www.youtube.com/playlist?list={pl_id}",
            "count": e.get("playlist_count") or e.get("n_entries") or 0,
            "uploader": e.get("uploader") or e.get("channel") or "",
        })
        if len(out) >= limit:
            break
    return out


def _fetch_playlist_sync(pl_id: str, limit: int) -> dict | None:
    info = _extract(f"https://www.youtube.com/playlist?list={pl_id}", limit)
    if not info:
        return None
    tracks, seen = [], set()
    for e in info.get("entries") or []:
        if not e or not e.get("id"):
            continue
        artist, song = parse_track(e.get("title", ""),
                                   e.get("uploader") or e.get("channel") or "")
        if not song:
            continue
        key = (artist.lower(), song.lower())
        if key in seen:
            continue
        seen.add(key)
        tracks.append({
            "artist":   artist,
            "song":     song,
            "yt_id":    e["id"],
            "duration": e.get("duration"),
        })
    return {
        "id":         pl_id,
        "name":       info.get("title") or pl_id,
        "url":        f"https://www.youtube.com/playlist?list={pl_id}",
        "uploader":   info.get("uploader") or info.get("channel") or "",
        "fetched_at": int(time.time()),
        "tracks":     tracks,
    }


# ── Öffentliche, asynchrone Schnittstelle ────────────────────────────────────

async def _run(fn, *args, timeout: float):
    loop = asyncio.get_running_loop()
    return await asyncio.wait_for(loop.run_in_executor(None, fn, *args), timeout)


async def search_tracks(query: str, limit: int = 15) -> list[dict]:
    if not query.strip():
        return []
    try:
        return await _run(_search_tracks_sync, query, max(1, min(30, limit)),
                          timeout=SEARCH_TIMEOUT)
    except asyncio.TimeoutError:
        logger.warning("Titelsuche '%s' überschritt %.0fs", query, SEARCH_TIMEOUT)
        return []


async def search_playlists(query: str, limit: int = 10) -> list[dict]:
    if not query.strip():
        return []
    try:
        return await _run(_search_playlists_sync, query, max(1, min(25, limit)),
                          timeout=SEARCH_TIMEOUT)
    except asyncio.TimeoutError:
        logger.warning("Playlistsuche '%s' überschritt %.0fs", query, SEARCH_TIMEOUT)
        return []


async def fetch_playlist(value: str, limit: int = MAX_PLAYLIST) -> dict | None:
    """Playlist auflösen und als Titelvorrat speichern."""
    pl_id = playlist_id_from(value)
    if not pl_id:
        return None
    try:
        data = await _run(_fetch_playlist_sync, pl_id, limit,
                          timeout=PLAYLIST_TIMEOUT)
    except asyncio.TimeoutError:
        logger.warning("Playlist %s überschritt %.0fs", pl_id, PLAYLIST_TIMEOUT)
        return None
    if not data or not data["tracks"]:
        return None
    await asyncio.get_running_loop().run_in_executor(
        None, storage.write_playlist, pl_id, data)
    logger.info("Playlist '%s' übernommen: %d Titel", data["name"], len(data["tracks"]))
    return data


async def suggestions(limit: int = 8) -> list[dict]:
    """
    Vorschläge ohne YouTube-API: aus den meistgehörten Interpreten werden
    Suchanfragen nach passenden Playlists gebaut.

    Das sind ausdrücklich keine YouTube-Empfehlungen — die gäbe es nur mit
    einem API-Schlüssel.
    """
    from . import listened
    counts: dict[str, int] = {}
    for e in listened.snapshot()["songs"].values():
        a = str(e.get("a", "")).strip()
        if a:
            counts[a] = counts.get(a, 0) + int(e.get("n", 1) or 1)
    top = [a for a, _ in sorted(counts.items(), key=lambda kv: -kv[1])[:4]]
    if not top:
        return []

    out: list[dict] = []
    seen: set[str] = set()
    for artist in top:
        for pl in await search_playlists(f"{artist} mix", limit=3):
            if pl["id"] in seen:
                continue
            seen.add(pl["id"])
            pl["because"] = artist
            out.append(pl)
            if len(out) >= limit:
                return out
    return out
