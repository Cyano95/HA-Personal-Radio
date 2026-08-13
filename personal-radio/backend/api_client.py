"""Station Log API polling client for Personal Radio."""
from __future__ import annotations

import asyncio
import html
import logging
import os
import time
from typing import Any, Callable

import httpx

from . import storage

logger = logging.getLogger("personal_radio.api_client")

# Alle Sender werden nur noch 1x pro Stunde abgefragt.
FULL_POLL_INTERVAL = 3600
# Läuft der Player, werden die gerade abgespielten (ausgewählten) Sender
# zusätzlich 1x pro Minute abgefragt.
ACTIVE_POLL_INTERVAL = 60
MAX_BACKOFF = 60


def _parse_entry(raw: str) -> dict | None:
    """
    Parse a station log entry.

    Supported formats (all seen in the wild):
        Artist;Song Title;unix_timestamp   ← primary format from the API
        Artist;Song Title                  ← legacy, no timestamp
        Artist — Song Title;unix_timestamp ← em-dash variant with timestamp
        Artist — Song Title                ← em-dash, no timestamp
        Artist - Song Title                ← plain-hyphen variant
    """
    line = raw.strip()
    if not line:
        return None

    # ── HTML-Entities ZUERST auflösen ────────────────────────────────────
    # Die API liefert HTML-escaped Zeilen (&amp;, &#039;, …). Das Semikolon
    # in "&amp;" kollidierte mit dem Feldtrenner und zerlegte z.B.
    # "Kool &amp; The Gang;Kool&#039;s Back Again" an der falschen Stelle.
    # Nach dem Unescape bleiben nur echte Trenner-Semikolons übrig.
    line = html.unescape(line)

    # ── Strip trailing ;unix_timestamp ───────────────────────────────────
    # Use rsplit so we only look at the very last semicolon-separated token.
    if ";" in line:
        parts = line.rsplit(";", 1)
        if parts[-1].strip().isdigit():
            line = parts[0].strip()   # drop the timestamp, keep the rest

    # ── Now split on the separator that is present ────────────────────────
    # Priority: semicolon (Artist;Song), then em-dash, then plain hyphen.
    for sep in [";", " \u2014 ", " - "]:
        if sep in line:
            artist, song = line.split(sep, 1)
            artist, song = artist.strip(), song.strip()
            if artist and song:
                return {"artist": artist, "song": song}

    return None  # unrecognised format — skip


class StationAPIClient:
    def __init__(self) -> None:
        self.url = os.environ.get("STATION_API_URL", "").strip()
        self.token = os.environ.get("STATION_API_TOKEN", "").strip()
        self.user = os.environ.get("STATION_API_USER", "").strip()
        self._backoff = 1
        self._client: httpx.AsyncClient | None = None

    @property
    def configured(self) -> bool:
        return bool(self.url and self.token)

    async def _get(self, params: dict) -> Any | None:
        if not self.configured:
            return None
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=15)

        for encoding in ("utf-8", "latin-1"):
            try:
                # Build query string with explicit encoding.
                # Older PHP servers often expect ISO-8859-1 / Latin-1 encoding
                # (%FC for ü) rather than UTF-8 (%C3%BC).
                # We try UTF-8 first; on 400 we retry with Latin-1.
                import urllib.parse
                qs   = urllib.parse.urlencode(params, encoding=encoding)
                resp = await self._client.get(f"{self.url}?{qs}")

                if resp.status_code == 400:
                    if encoding == "utf-8":
                        logger.debug(
                            "Station API 400 with UTF-8 encoding for station=%s — "
                            "retrying with Latin-1.  Server said: %s",
                            params.get("station", "?"), resp.text[:300],
                        )
                        continue    # retry with latin-1
                    # latin-1 also failed → give up on this station for now
                    logger.warning(
                        "Station API 400 (both encodings) for station=%s — "
                        "skipping.  Server said: %s",
                        params.get("station", "?"), resp.text[:300],
                    )
                    return None

                resp.raise_for_status()
                self._backoff = 1
                return resp.json()

            except httpx.HTTPStatusError as e:
                logger.warning(
                    "Station API HTTP error %s for station=%s: %s",
                    e.response.status_code, params.get("station", "?"),
                    e.response.text[:200],
                )
                await asyncio.sleep(self._backoff)
                self._backoff = min(self._backoff * 2, MAX_BACKOFF)
                return None
            except Exception as e:
                logger.warning(
                    "Station API request failed: %s (backoff %ds)", e, self._backoff
                )
                await asyncio.sleep(self._backoff)
                self._backoff = min(self._backoff * 2, MAX_BACKOFF)
                return None

        return None

    async def list_stations(self) -> list[dict]:
        data = await self._get({"user": self.user, "token": self.token})
        if data is None:
            return []
        return data.get("stations", [])

    async def get_new_entries(self, station: str, since: int) -> dict | None:
        return await self._get({
            "user": self.user,
            "token": self.token,
            "station": station,
            "since": since,
        })

    async def initialize_cursors(self) -> None:
        stations = await self.list_stations()
        if not stations:
            if self.configured:
                logger.warning("Station API returned no stations. Check credentials.")
            else:
                logger.warning("Station API not configured.")
            return
        cursors = storage.read_cursors()
        changed = False
        for s in stations:
            name = s.get("station", "")
            if not name:
                continue
            if name not in cursors:
                cursors[name] = 0   # start from beginning on first run
                logger.info("New station '%s' — fetching full history (%d entries)",
                            name, s.get("line_count", 0))
                changed = True
        if changed:
            storage.write_cursors(cursors)

    async def _drain_station(self, name: str, cursors: dict) -> None:
        """
        Fetch all pending entries for one station, advancing its cursor.

        The API paginates: one call may return only a batch of entries
        even though total_lines is much larger.  We advance the cursor
        by len(new_raw) (ALL raw lines, including unparseable ones so
        the line-number cursor stays accurate) and keep fetching until
        the API returns an empty batch or we reach total_lines.
        """
        loop         = asyncio.get_running_loop()
        since        = cursors.get(name, 0)
        total_added  = 0
        batch_num    = 0

        while True:
            data = await self.get_new_entries(name, since)
            if data is None:
                break

            new_raw      = data.get("new_entries", [])
            total_lines  = data.get("total_lines", since)
            batch_num   += 1

            entries = [e for raw in new_raw if (e := _parse_entry(raw)) is not None]
            if entries:
                await loop.run_in_executor(
                    None, storage.append_to_station_pool, name, entries
                )
                total_added += len(entries)

            # Advance cursor by the number of raw lines in this batch
            # (not by total_lines — that was the bug: cursor jumped to
            #  the end after the first batch, skipping everything else)
            since += len(new_raw)
            cursors[name] = since

            # Persist cursor after every batch so a crash doesn't lose
            # progress on large historical fetches
            storage.write_cursors(cursors)

            if not new_raw:
                # API returned nothing → fully caught up
                break
            if since >= total_lines:
                # Reached the end of the log
                break

            # Brief pause to avoid hammering the API between batches
            await asyncio.sleep(0.2)

        if total_added > 0:
            logger.info(
                "Station '%s': +%d songs in %d batch(es) (cursor → %d)",
                name, total_added, batch_num, since,
            )
        elif batch_num > 1:
            logger.debug("Station '%s': no new parseable songs (cursor %d)", name, since)

    async def poll_once(self) -> None:
        """Full poll of ALL stations (runs once per hour)."""
        stations_info = await self.list_stations()
        if not stations_info:
            return

        # Build remote line-count map from the list response.
        # Stations whose cursor is already at or beyond line_count need no fetch.
        remote_counts: dict[str, int] = {
            s["station"]: s.get("line_count", 0)
            for s in stations_info
            if s.get("station")
        }

        cursors = storage.read_cursors()

        for name, remote_count in remote_counts.items():
            since = cursors.get(name, 0)
            if since >= remote_count:
                logger.debug(
                    "Station '%s': up to date (cursor %d = remote %d)",
                    name, since, remote_count,
                )
                continue
            logger.info(
                "Station '%s': fetching lines %d → %d (+%d)",
                name, since, remote_count, remote_count - since,
            )
            await self._drain_station(name, cursors)

        storage.write_cursors(cursors)

    async def poll_stations(self, names: list[str]) -> None:
        """
        Poll only the given stations (one request each, no station-list call).
        Used for the per-minute poll of currently playing stations.
        """
        if not names or not self.configured:
            return
        cursors = storage.read_cursors()
        for name in names:
            await self._drain_station(name, cursors)
        storage.write_cursors(cursors)

    async def run_poll_loop(
        self,
        get_active_stations: Callable[[], list[str]] | None = None,
    ) -> None:
        """
        • Alle Sender: 1x pro Stunde.
        • Läuft der Player: die gerade abgespielten Sender 1x pro Minute.
        """
        await self.initialize_cursors()
        last_full = 0.0
        while True:
            try:
                now = time.monotonic()
                if last_full == 0.0 or now - last_full >= FULL_POLL_INTERVAL:
                    await self.poll_once()
                    last_full = time.monotonic()
                elif get_active_stations is not None:
                    try:
                        active = list(get_active_stations() or [])
                    except Exception:
                        active = []
                    if active:
                        await self.poll_stations(active)
            except Exception:
                logger.exception("Unexpected error in poll loop")
            await asyncio.sleep(ACTIVE_POLL_INTERVAL)
