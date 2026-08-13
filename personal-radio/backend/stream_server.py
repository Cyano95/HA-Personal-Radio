"""
Continuous per-user radio stream with PCM crossfade.

Route: GET /stream/{uid}?token=<media_token>

Key design decisions:
  • PCM is written at REALTIME rate → no encoder flood, no client eviction.
  • Next song is decoded concurrently while current song streams → zero gap.
  • Burst-on-connect: new subscribers receive the last ~2 s of already-encoded
    audio before the live feed. This lets VLC (and strict HTTP audio clients)
    sync to a valid MP3 frame without getting garbled audio.
  • HEAD requests are answered without subscribing (avoids spurious listeners
    when HA or players probe the URL).
  • The producer stops automatically when the last listener disconnects.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from typing import AsyncIterator, Callable

import numpy as np
from starlette.requests import Request
from starlette.responses import Response, StreamingResponse
from starlette.routing import Route

from . import storage

logger = logging.getLogger("personal_radio.stream")

# ── Audio constants ──────────────────────────────────────────────────────────
SAMPLE_RATE     = 44_100
CHANNELS        = 2
BPS             = 2                               # s16le: 2 bytes/sample
BYTES_PER_SEC   = SAMPLE_RATE * CHANNELS * BPS   # 176 400 B/s
CROSSFADE_SEC   = 5
CROSSFADE_BYTES = CROSSFADE_SEC * BYTES_PER_SEC
ENCODE_BITRATE  = "128k"
PCM_CHUNK       = 65_536    # ffmpeg decoder read chunk
STREAM_CHUNK    = 8_192     # HTTP broadcast chunk size

# How many seconds we allow ahead of realtime when feeding the encoder.
# 1.5 s Vorlauf überbrückt kurze Event-Loop-Verzögerungen (Datei-I/O,
# yt-dlp-Resolves), die sonst als kurzes Stocken hörbar wurden. Größerer
# Vorlauf würde Skip/Prev spürbar verzögern (bereits gesendetes Audio
# spielt beim Client erst zu Ende).
REALTIME_AHEAD  = 1.5

# Burst-on-connect: how many bytes of recent audio to send to new subscribers
# so VLC (and other strict clients) can find a valid MP3 sync frame quickly.
# ~2 s at 128 kbps = 32 000 bytes.
BURST_BYTES = 32_768

# Maximale Dauer eines Vordergrund-Decodes (Song wird gerade NICHT aus dem
# Prefetch bedient). Gedrosselte/tote YouTube-URLs lassen ffmpeg sonst
# beliebig lange hängen — währenddessen käme kein Audio und der ICY-Watchdog
# (60 s Stille) würde die Verbindung kappen und den Stream beenden.
FG_DECODE_TIMEOUT = 45.0

# ── Global registry ──────────────────────────────────────────────────────────
_streams: dict[str, "UserStream"] = {}
_registry_lock: asyncio.Lock | None = None


def _get_lock() -> asyncio.Lock:
    global _registry_lock
    if _registry_lock is None:
        _registry_lock = asyncio.Lock()
    return _registry_lock


# ── Callbacks injected by main.py ────────────────────────────────────────────
_cb_get_current_song: Callable | None = None
_cb_advance_queue:    Callable | None = None
_cb_ensure_queue:     Callable | None = None
_cb_fire_event:       Callable | None = None
_cb_mark_played:      Callable | None = None


def set_stream_callbacks(
    get_current_song: Callable,
    advance_queue:    Callable,
    ensure_queue:     Callable,
    fire_event:       Callable,
    mark_played:      Callable | None = None,
) -> None:
    global _cb_get_current_song, _cb_advance_queue, _cb_ensure_queue, \
        _cb_fire_event, _cb_mark_played
    _cb_get_current_song = get_current_song
    _cb_advance_queue    = advance_queue
    _cb_ensure_queue     = ensure_queue
    _cb_fire_event       = fire_event
    _cb_mark_played      = mark_played


# ── Public helpers ────────────────────────────────────────────────────────────

async def get_or_create_stream(uid: str) -> "UserStream":
    async with _get_lock():
        s = _streams.get(uid)
        if s is None or s.stopped:
            s = UserStream(uid)
            _streams[uid] = s
        return s


def stop_stream(uid: str) -> None:
    s = _streams.get(uid)
    if s and not s.stopped:
        s._force_stop()


# ── UserStream ────────────────────────────────────────────────────────────────

class UserStream:

    def __init__(self, uid: str) -> None:
        self.uid          = uid
        self.stopped      = False
        self.current_song: dict | None = None
        # Zählt jeden Song-Start hoch — die UI wartet auf eine Änderung
        # dieser Nummer statt auf den Titelnamen (derselbe Titel kann
        # zweimal hintereinander kommen).
        self.song_seq     = 0

        self._subs:        list[asyncio.Queue] = []
        self._subs_lock    = asyncio.Lock()
        self._skip_event   = asyncio.Event()
        self._producer:    asyncio.Task | None = None
        self._started      = False

        # Set when the station selection changed: any prefetched "next song"
        # (old selection) must be discarded; the current song keeps playing.
        self._queue_dirty  = False

        # Burst-on-connect ring buffer (recent encoded MP3 bytes)
        self._burst:       bytearray = bytearray()

    # ── Listener management ───────────────────────────────────────────────

    async def subscribe(self) -> asyncio.Queue:
        """
        Connect a new listener.  The subscriber queue is pre-filled with
        the last BURST_BYTES of encoded audio so VLC can find an MP3 sync
        frame without getting garbled output on first connection.
        """
        q: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=128)
        async with self._subs_lock:
            # Burst-on-connect: seed the queue with recent audio
            if self._burst:
                q.put_nowait(bytes(self._burst))

            self._subs.append(q)

            if not self._started:
                self._started  = True
                self._producer = asyncio.create_task(
                    self._run_producer(), name=f"producer_{self.uid}"
                )
        logger.info("[%s] Listener connected (total: %d)", self.uid, len(self._subs))
        return q

    async def unsubscribe(self, q: asyncio.Queue) -> None:
        async with self._subs_lock:
            try:
                self._subs.remove(q)
            except ValueError:
                pass
            remaining = len(self._subs)
        logger.info("[%s] Listener left (remaining: %d)", self.uid, remaining)
        if remaining == 0:
            logger.info("[%s] No listeners — stopping stream", self.uid)
            self._force_stop()
            try:
                state = storage.read_user_state(self.uid)
                if state.get("is_playing"):
                    state["is_playing"] = False
                    storage.write_user_state(self.uid, state)
            except Exception:
                pass

    def skip(self) -> None:
        logger.info("[%s] Skip requested", self.uid)
        self._skip_event.set()

    def invalidate_upcoming(self) -> None:
        """
        Station selection changed. The currently playing song continues
        uninterrupted; any already-prefetched next song is discarded so the
        NEXT song comes from the new selection.
        """
        logger.info("[%s] Station selection changed — discarding prefetched next song", self.uid)
        self._queue_dirty = True

    def _force_stop(self) -> None:
        self.stopped = True
        self._skip_event.set()
        if self._producer and not self._producer.done():
            self._producer.cancel()

    # ── Broadcasting ──────────────────────────────────────────────────────

    async def _broadcast(self, chunk: bytes | None) -> None:
        """Push chunk to all subscriber queues; update burst ring buffer."""
        if chunk is not None:
            # Update ring buffer — keep last BURST_BYTES
            self._burst.extend(chunk)
            excess = len(self._burst) - BURST_BYTES
            if excess > 0:
                del self._burst[:excess]

        async with self._subs_lock:
            dead: list[asyncio.Queue] = []
            for q in list(self._subs):
                try:
                    q.put_nowait(chunk)
                except asyncio.QueueFull:
                    dead.append(q)
            for q in dead:
                try:
                    self._subs.remove(q)
                except ValueError:
                    pass

    # ── FFmpeg helpers ────────────────────────────────────────────────────

    @staticmethod
    async def _decode_pcm(
        source: str,
        skip_event: asyncio.Event | None = None,
    ) -> bytes | None:
        is_url = source.startswith("http://") or source.startswith("https://")
        cmd = ["ffmpeg", "-y"]
        if is_url:
            cmd += ["-reconnect", "1", "-reconnect_streamed", "1",
                    "-reconnect_delay_max", "5"]
        cmd += ["-i", source,
                "-f", "s16le", "-ar", str(SAMPLE_RATE), "-ac", str(CHANNELS),
                "pipe:1"]
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        assert proc.stdout is not None
        chunks: list[bytes] = []
        while True:
            if skip_event and skip_event.is_set():
                proc.kill()
                await proc.wait()
                return None
            chunk = await proc.stdout.read(PCM_CHUNK)
            if not chunk:
                break
            chunks.append(chunk)
        await proc.wait()
        return b"".join(chunks)

    @staticmethod
    async def _start_encoder() -> asyncio.subprocess.Process:
        return await asyncio.create_subprocess_exec(
            "ffmpeg", "-y",
            "-f", "s16le", "-ar", str(SAMPLE_RATE), "-ac", str(CHANNELS),
            "-i", "pipe:0",
            "-f", "mp3", "-b:a", ENCODE_BITRATE,
            "-write_xing", "0",
            "pipe:1",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )

    # ── Crossfade (numpy) ─────────────────────────────────────────────────

    @staticmethod
    def _blend(tail: bytes, head: bytes) -> bytes:
        n = (min(len(tail), len(head), CROSSFADE_BYTES) // 4) * 4
        if n == 0:
            return b""
        a = np.frombuffer(tail[:n], dtype=np.int16).astype(np.float32)
        b = np.frombuffer(head[:n], dtype=np.int16).astype(np.float32)
        n_frames = n // 4
        fi = np.linspace(0.0, 1.0, n_frames, dtype=np.float32)
        fo = 1.0 - fi
        mixed = np.clip(
            a * np.repeat(fo, 2) + b * np.repeat(fi, 2),
            -32768, 32767,
        ).astype(np.int16)
        return mixed.tobytes()

    # ── Rate-limited PCM writer ───────────────────────────────────────────

    async def _decode_foreground(
        self,
        encoder: asyncio.subprocess.Process,
        source: str,
    ) -> bytes | None:
        """
        Song im Vordergrund dekodieren (kein Prefetch vorhanden) und dabei
        den Stream mit Stille am Leben halten, damit ICY-Watchdog und Player
        die Verbindung nicht wegen fehlender Bytes trennen.

        Rückgabe: PCM | None (Skip/Stop) | b"" (Fehler/Timeout → Song
        überspringen; die Stream-URL sollte invalidiert werden).
        """
        decode_t = asyncio.create_task(self._decode_pcm(source, self._skip_event))
        silence  = bytes(BYTES_PER_SEC // 2)          # 0.5 s Stille
        deadline = time.monotonic() + FG_DECODE_TIMEOUT
        try:
            while True:
                try:
                    # 0.5 s auf den Decoder warten …
                    return await asyncio.wait_for(asyncio.shield(decode_t), timeout=0.5)
                except asyncio.TimeoutError:
                    pass
                except Exception as exc:
                    logger.warning("[%s] Decode error: %s", self.uid, exc)
                    return b""
                if self.stopped or self._skip_event.is_set():
                    decode_t.cancel()
                    return None
                if time.monotonic() > deadline:
                    logger.warning(
                        "[%s] Foreground decode timed out after %.0fs — skipping song",
                        self.uid, FG_DECODE_TIMEOUT,
                    )
                    decode_t.cancel()
                    return b""
                # … und solange ~Echtzeit-Stille schreiben (Keepalive)
                if await self._write_pcm(encoder, silence):
                    decode_t.cancel()
                    return None
        finally:
            if decode_t.done() and not decode_t.cancelled():
                # Exceptions des Decode-Tasks nicht unbeobachtet lassen
                exc = decode_t.exception()
                if exc:
                    logger.warning("[%s] Decode error: %s", self.uid, exc)

    async def _write_pcm(
        self,
        encoder: asyncio.subprocess.Process,
        pcm: bytes,
    ) -> bool:
        """
        Write *pcm* to encoder stdin at approx REALTIME speed.
        Returns True if interrupted by stop/skip.
        """
        if not pcm:
            return False
        assert encoder.stdin is not None
        t0            = time.monotonic()
        bytes_written = 0
        for offset in range(0, len(pcm), PCM_CHUNK):
            if self.stopped or self._skip_event.is_set():
                return True
            chunk = pcm[offset: offset + PCM_CHUNK]
            try:
                encoder.stdin.write(chunk)
                await encoder.stdin.drain()
            except (BrokenPipeError, ConnectionResetError, OSError):
                logger.warning("[%s] Encoder pipe broken", self.uid)
                self.stopped = True
                return True
            bytes_written += len(chunk)
            elapsed  = time.monotonic() - t0
            expected = bytes_written / BYTES_PER_SEC
            surplus  = expected - elapsed - REALTIME_AHEAD
            if surplus > 0.05:
                await asyncio.sleep(surplus)
            else:
                await asyncio.sleep(0)
        return False

    # ── Producer ──────────────────────────────────────────────────────────

    async def _run_producer(self) -> None:
        """
        Per-song flow (gapless crossfade):
          1. Decode song N (or use prefetched PCM)
          2. Fire song-started event
          3. Advance queue → N+1
          4. Start background decode of N+1  ← during body streaming
          5. Write body_N at realtime rate
          6. Await background decode of N+1  (should already be done)
          7. Write blend(tail_N, head_N+1)   ← zero gap
          8. Pass remaining N+1 PCM as prefetch for next iteration
        """
        encoder    = await self._start_encoder()
        relay_task = asyncio.create_task(self._relay_encoder(encoder))

        prefetched_song: dict | None  = None
        prefetched_pcm:  bytes | None = None

        try:
            while not self.stopped:
                self._skip_event.clear()

                # Station selection changed mid-song → drop the prefetched
                # next song (old selection); the queue was already rebuilt.
                if self._queue_dirty:
                    self._queue_dirty = False
                    prefetched_song = None
                    prefetched_pcm  = None

                if _cb_ensure_queue:
                    await _cb_ensure_queue(self.uid, count=1)

                # ── Get current song + PCM ─────────────────────────────────
                if prefetched_song is not None and prefetched_pcm is not None:
                    song     = prefetched_song
                    song_pcm = prefetched_pcm
                    prefetched_song = None
                    prefetched_pcm  = None
                else:
                    song = (_cb_get_current_song(self.uid)
                            if _cb_get_current_song else None)
                    if not song:
                        await asyncio.sleep(2)
                        continue

                    from .downloader import get_audio_source as _get_src
                    artist_    = song.get("artist", "")
                    song_name_ = song.get("song", "")
                    source_    = await _get_src(artist_, song_name_)
                    if not source_:
                        logger.warning("[%s] Cannot resolve audio: %s — %s",
                                       self.uid, artist_, song_name_)
                        if _cb_advance_queue:
                            await _cb_advance_queue(self.uid, song)
                        continue

                    src_type = "file" if source_.startswith("/") else "url"
                    logger.info("[%s] Decoding (%s): %s — %s",
                                self.uid, src_type, artist_, song_name_)
                    # Mit Stille-Keepalive + Timeout: ein hängender ffmpeg
                    # (gedrosselte/tote URL) darf nie mehr den ganzen Stream
                    # verstummen lassen (→ ICY-Watchdog kappte sonst alles).
                    song_pcm = await self._decode_foreground(encoder, source_)

                    if song_pcm is None or self._skip_event.is_set():
                        logger.info("[%s] Skipped during decode", self.uid)
                        if _cb_advance_queue:
                            await _cb_advance_queue(self.uid, song)
                        self._skip_event.clear()
                        continue

                if not song_pcm:
                    # Decoder lieferte nichts (URL abgelaufen/tot/gedrosselt).
                    # Bisher rückte dieser Pfad STILL weiter — jetzt loggen wir
                    # und verwerfen die gecachte URL, damit der Titel beim
                    # nächsten Versuch frisch aufgelöst wird.
                    logger.warning("[%s] Decode returned no audio: %s — %s",
                                   self.uid, song.get("artist", ""), song.get("song", ""))
                    try:
                        from .downloader import invalidate_stream_url
                        await invalidate_stream_url(song.get("artist", ""),
                                                    song.get("song", ""))
                    except Exception:
                        pass
                    if _cb_advance_queue:
                        await _cb_advance_queue(self.uid, song)
                    continue

                self.current_song = song
                self.song_seq    += 1

                # Zeitstempel auf den tatsächlichen Abspielbeginn setzen
                # (No-Repeat-Spanne zählt ab Wiedergabestart, nicht ab Queuing)
                if _cb_mark_played:
                    try:
                        _cb_mark_played(self.uid, song)
                    except Exception:
                        logger.debug("[%s] mark_played callback failed", self.uid)

                if _cb_fire_event:
                    asyncio.create_task(_cb_fire_event("personal_radio_song_started", {
                        "ha_user_id": self.uid,
                        "artist":    song.get("artist", ""),
                        "song":      song.get("song", ""),
                        "station":   song.get("station", ""),
                        "thumbnail": song.get("thumbnail", ""),
                    }))

                # ── Split body / tail ──────────────────────────────────────
                if len(song_pcm) > CROSSFADE_BYTES:
                    body_pcm = song_pcm[:-CROSSFADE_BYTES]
                    tail_pcm = song_pcm[-CROSSFADE_BYTES:]
                else:
                    body_pcm = b""
                    tail_pcm = song_pcm

                # ── Advance queue; start background prefetch ───────────────
                if _cb_advance_queue:
                    await _cb_advance_queue(self.uid, song)
                if _cb_ensure_queue:
                    asyncio.create_task(_cb_ensure_queue(self.uid, count=1))

                next_song   = (_cb_get_current_song(self.uid)
                               if _cb_get_current_song else None)
                next_yt_id  = (next_song or {}).get("yt_id", "")
                bg_skip = asyncio.Event()

                async def _prefetch_next(ns=next_song):
                    if not ns:
                        return None
                    from .downloader import get_audio_source as _get_src
                    src = await _get_src(ns.get("artist", ""), ns.get("song", ""))
                    if not src:
                        return None
                    return await UserStream._decode_pcm(src, bg_skip)

                prefetch_t = (
                    asyncio.create_task(_prefetch_next())
                    if next_song else None
                )

                # ── Write body at realtime rate ────────────────────────────
                skipped = await self._write_pcm(encoder, body_pcm)

                if skipped:
                    bg_skip.set()
                    if prefetch_t:
                        prefetch_t.cancel()
                    self._skip_event.clear()
                    continue

                # ── Station change during body playback? ──────────────────
                # The prefetched next song is from the OLD selection: drop it,
                # finish this song's tail without crossfade, and let the next
                # loop iteration pick up the rebuilt queue.
                if self._queue_dirty:
                    bg_skip.set()
                    if prefetch_t:
                        prefetch_t.cancel()
                    await self._write_pcm(encoder, tail_pcm)
                    continue

                # ── Collect prefetch ───────────────────────────────────────
                next_pcm: bytes | None = None
                if prefetch_t:
                    try:
                        next_pcm = await asyncio.wait_for(prefetch_t, timeout=20.0)
                    except (asyncio.TimeoutError, asyncio.CancelledError):
                        logger.warning("[%s] Prefetch timed out — no crossfade", self.uid)
                        # Vermutlich tote/gedrosselte URL: sofort verwerfen,
                        # damit der folgende Vordergrund-Decode frisch resolved
                        # statt an derselben URL erneut zu hängen.
                        if next_song:
                            try:
                                from .downloader import invalidate_stream_url
                                await invalidate_stream_url(
                                    next_song.get("artist", ""),
                                    next_song.get("song", ""))
                            except Exception:
                                pass

                # ── Write blend + hand off remainder ──────────────────────
                if next_pcm and tail_pcm and not self._queue_dirty:
                    blend_n = (min(len(tail_pcm), len(next_pcm), CROSSFADE_BYTES) // 4) * 4
                    blended = self._blend(tail_pcm[:blend_n], next_pcm[:blend_n])
                    skipped = await self._write_pcm(encoder, blended)
                    if not skipped and next_song and not self._queue_dirty:
                        prefetched_song = next_song
                        prefetched_pcm  = next_pcm[blend_n:]
                    elif skipped:
                        self._skip_event.clear()
                else:
                    await self._write_pcm(encoder, tail_pcm)

        except asyncio.CancelledError:
            logger.info("[%s] Producer cancelled", self.uid)
        except Exception:
            logger.exception("[%s] Unexpected error in producer", self.uid)
        finally:
            self.stopped      = True
            self.current_song = None
            try:
                if encoder.stdin and not encoder.stdin.is_closing():
                    encoder.stdin.close()
            except Exception:
                pass
            relay_task.cancel()
            await self._broadcast(None)
            logger.info("[%s] Producer stopped", self.uid)
            # Clear cached stream URLs for this user's queue (they expire anyway;
            # clearing them now keeps the cache lean and forces a fresh resolve
            # on next play — which happens in the background and is fast).
            try:
                from .downloader import clear_stream_urls as _clear
                us = storage.read_user_state(self.uid)
                yt_ids = [
                    item["yt_id"] for item in us.get("queue", [])
                    if item.get("yt_id")
                ]
                asyncio.create_task(_clear(yt_ids or None))
            except Exception:
                pass

    async def _relay_encoder(self, encoder: asyncio.subprocess.Process) -> None:
        assert encoder.stdout is not None
        while True:
            chunk = await encoder.stdout.read(STREAM_CHUNK)
            if not chunk:
                break
            await self._broadcast(chunk)


# ── HTTP handlers ─────────────────────────────────────────────────────────────

# Minimal headers for a clean HTTP audio stream.
# Do NOT include icy-metaint — it makes VLC expect ICY metadata injected
# into the binary stream, causing it to interpret audio bytes as text = garble.
_STREAM_HEADERS = {
    "Cache-Control":          "no-cache, no-store",
    "X-Content-Type-Options": "nosniff",
    "icy-name":               "Personal Radio",
    "icy-br":                 "128",          # bitrate hint for players
    "Connection":             "keep-alive",
}


def _token_to_uid(token: str) -> str | None:
    if not token or not storage.USERS_DIR.exists():
        return None
    for token_file in storage.USERS_DIR.glob("*/media_token"):
        try:
            if token_file.read_text().strip() == token:
                return token_file.parent.name
        except Exception:
            pass
    return None


async def stream_handler(request: Request) -> Response:
    uid_path = request.path_params.get("uid", "")
    token    = request.query_params.get("token", "")

    uid = _token_to_uid(token)
    if not uid or uid != uid_path:
        logger.warning("Stream denied — bad token for uid=%s", uid_path)
        return Response("Forbidden", status_code=403)

    # HEAD: players / HA probe the URL before connecting. Answer without
    # subscribing so we don't create a phantom listener that stops the stream.
    if request.method == "HEAD":
        return Response(
            status_code=200,
            headers={"Content-Type": "audio/mpeg", **_STREAM_HEADERS},
        )

    stream = await get_or_create_stream(uid)
    q      = await stream.subscribe()

    async def generate() -> AsyncIterator[bytes]:
        try:
            while True:
                try:
                    chunk = await asyncio.wait_for(q.get(), timeout=60.0)
                except asyncio.TimeoutError:
                    logger.warning("[%s] No audio for 60 s — closing connection", uid)
                    break
                if chunk is None:
                    break
                yield chunk
        except (asyncio.CancelledError, GeneratorExit):
            pass
        finally:
            await stream.unsubscribe(q)

    return StreamingResponse(
        generate(),
        media_type="audio/mpeg",
        headers=_STREAM_HEADERS,
    )


stream_routes = [
    Route("/stream/{uid}", stream_handler, methods=["GET", "HEAD"]),
]
