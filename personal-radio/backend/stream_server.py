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

# Fällt die Takt-Uhr weiter als diese Spanne hinter Echtzeit zurück
# (Skip, kurzer Stillstand), wird sie neu verankert statt aufzuholen.
PACE_MAX_BEHIND = 2.0
# Zeitbudgets für die übrigen Zwischen-Song-Phasen (Queue füllen kann einen
# yt-dlp-Resolve enthalten, URL-Auflösung ebenso) — auch dort wird jetzt
# Keepalive-Stille geschrieben, damit der Stream nie verstummt.
QUEUE_FILL_TIMEOUT = 60.0
RESOLVE_TIMEOUT    = 40.0

# ── Pause / Fortsetzen ───────────────────────────────────────────────────────
# Beim Stoppen wird der laufende Song nicht verworfen, sondern an der
# aktuellen Stelle "pausiert": Position + restliches PCM bleiben im Cache,
# damit beim nächsten Play genau dort weitergespielt wird.
#
# Der Vorlauf gegenüber Echtzeit (Encoder-Lead + Player-Puffer) lässt sich
# nicht exakt bestimmen — lieber ein paar Sekunden doppelt hören als eine
# Lücke, deshalb wird die gemerkte Position um diesen Wert zurückgesetzt.
RESUME_PREROLL_SEC    = 4.0
# Weniger Rest als das → Song gilt als beendet, keine Pause merken.
MIN_RESUME_SEC        = 5.0
# Obergrenze für das im RAM gehaltene Rest-PCM (~6 min). Größere Reste
# werden beim Fortsetzen per ffmpeg -ss neu dekodiert statt gepuffert.
MAX_PAUSE_CACHE_BYTES = 64 * 1024 * 1024

# {uid: {"key": str, "pcm": bytes}} — Rest-Audio des pausierten Songs.
_paused_audio: dict[str, dict] = {}


def song_key(song: dict | None) -> str:
    """Stabile Kennung eines Songs (yt_id, sonst Interpret|Titel)."""
    if not song:
        return ""
    yt_id = (song.get("yt_id") or "").strip()
    if yt_id:
        return yt_id
    return f"{song.get('artist', '').lower()}|{song.get('song', '').lower()}"


def pause_position(uid: str, song: dict | None) -> float:
    """Gemerkte Abspielposition (Sekunden) — 0, wenn keine passende Pause."""
    if not song:
        return 0.0
    try:
        paused = storage.read_user_state(uid).get("paused_song") or {}
    except Exception:
        return 0.0
    if not paused or song_key(paused) != song_key(song):
        return 0.0
    try:
        return max(0.0, float(paused.get("position", 0.0)))
    except (TypeError, ValueError):
        return 0.0


def pop_paused_audio(uid: str, song: dict | None) -> bytes | None:
    """Zwischengespeichertes Rest-PCM des pausierten Songs entnehmen."""
    entry = _paused_audio.get(uid)
    if not entry or entry.get("key") != song_key(song):
        return None
    del _paused_audio[uid]
    return entry.get("pcm")


def clear_paused(uid: str) -> None:
    """Pausenzustand verwerfen (Skip/Prev oder Song wurde fortgesetzt)."""
    _paused_audio.pop(uid, None)
    try:
        if storage.read_user_state(uid).get("paused_song"):
            storage.update_user_state(uid, paused_song=None)
    except Exception:
        logger.debug("[%s] Could not clear paused_song", uid)


def paused_yt_id(uid: str) -> str:
    try:
        paused = storage.read_user_state(uid).get("paused_song") or {}
    except Exception:
        return ""
    return (paused.get("yt_id") or "").strip()


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
_cb_request_restart:  Callable | None = None


def set_stream_callbacks(
    get_current_song: Callable,
    advance_queue:    Callable,
    ensure_queue:     Callable,
    fire_event:       Callable,
    mark_played:      Callable | None = None,
    request_restart:  Callable | None = None,
) -> None:
    global _cb_get_current_song, _cb_advance_queue, _cb_ensure_queue, \
        _cb_fire_event, _cb_mark_played, _cb_request_restart
    _cb_get_current_song = get_current_song
    _cb_advance_queue    = advance_queue
    _cb_ensure_queue     = ensure_queue
    _cb_fire_event       = fire_event
    _cb_mark_played      = mark_played
    _cb_request_restart  = request_restart


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

        # Globale Takt-Uhr über ALLE Schreibvorgänge (Body, Blend, Stille).
        # Vorher startete jeder _write_pcm-Aufruf eine eigene Uhr und
        # genehmigte sich 1.5 s Vorsprung — die Drift summierte sich
        # unbegrenzt, ließ die Subscriber-Queue volllaufen und der Hörer
        # wurde still entfernt (Musik stoppte ohne Logeintrag).
        self._pace_base:   float | None = None   # monotonic-Anker
        self._pace_bytes:  int   = 0             # PCM-Bytes seit Anker
        self._lag_warn_at: float = 0.0           # rate-limit für Lag-Warnung

        # Position im aktuell laufenden Song — Grundlage für Pause/Fortsetzen.
        self._song_pcm:      bytes | None = None  # PCM ab Wiedergabebeginn
        self._song_pos:      int   = 0            # bereits geschriebene Bytes
        self._song_offset_s: float = 0.0          # Startversatz im Song (s)

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
                if storage.read_user_state(self.uid).get("is_playing"):
                    storage.update_user_state(self.uid, is_playing=False)
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
            for q in list(self._subs):
                try:
                    q.put_nowait(chunk)
                except asyncio.QueueFull:
                    # Der Hörer hinkt hinterher (z.B. WLAN-Aussetzer beim
                    # Player). Vorher wurde er hier STILL entfernt — die
                    # Musik verstummte ohne jeden Logeintrag und der Stream
                    # erholte sich nie. Jetzt: ältesten Chunk verwerfen und
                    # den neuen einreihen (Ring-Verhalten). Der Hörer
                    # überspringt etwas Audio, bleibt aber verbunden.
                    try:
                        q.get_nowait()
                    except asyncio.QueueEmpty:
                        pass
                    try:
                        q.put_nowait(chunk)
                    except asyncio.QueueFull:
                        pass
                    now = time.monotonic()
                    if now - self._lag_warn_at > 30:
                        self._lag_warn_at = now
                        logger.warning(
                            "[%s] Listener lagging — dropping oldest audio "
                            "to keep the stream alive", self.uid,
                        )

    # ── FFmpeg helpers ────────────────────────────────────────────────────

    @staticmethod
    async def _decode_pcm(
        source: str,
        skip_event: asyncio.Event | None = None,
        headers: dict | None = None,
        start_at: float = 0.0,
    ) -> bytes | None:
        """
        *start_at*: Sekunden, die am Anfang übersprungen werden — damit ein
        pausierter Song genau dort fortgesetzt wird, wo er gestoppt wurde.
        """
        is_url = source.startswith("http://") or source.startswith("https://")
        cmd = ["ffmpeg", "-y"]
        if is_url:
            cmd += ["-reconnect", "1", "-reconnect_streamed", "1",
                    "-reconnect_delay_max", "5"]
            # Dieselben Header wie beim yt-dlp-Resolve verwenden — YouTube
            # lehnt Abrufe mit abweichendem User-Agent zunehmend mit 403 ab.
            if headers:
                ua = headers.get("User-Agent") or headers.get("user-agent")
                if ua:
                    cmd += ["-user_agent", ua]
                other = "".join(
                    f"{k}: {v}\r\n" for k, v in headers.items()
                    if k.lower() != "user-agent" and v
                )
                if other:
                    cmd += ["-headers", other]
        if start_at > 0:
            # vor -i = schnelles Suchen (Input-Seeking)
            cmd += ["-ss", f"{start_at:.3f}"]
        cmd += ["-i", source,
                "-f", "s16le", "-ar", str(SAMPLE_RATE), "-ac", str(CHANNELS),
                "pipe:1"]
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        assert proc.stdout is not None and proc.stderr is not None

        # stderr nebenläufig einsammeln (begrenzt), sonst kann die Pipe
        # volllaufen und ffmpeg blockieren. Bei leerem Ergebnis liefert das
        # den GRUND (z.B. "403 Forbidden") ins Log statt Rätselraten.
        err_tail = bytearray()

        async def _drain_stderr() -> None:
            while True:
                data = await proc.stderr.read(1024)
                if not data:
                    break
                err_tail.extend(data)
                if len(err_tail) > 4096:
                    del err_tail[: len(err_tail) - 4096]

        err_task = asyncio.create_task(_drain_stderr())

        chunks: list[bytes] = []
        while True:
            if skip_event and skip_event.is_set():
                proc.kill()
                await proc.wait()
                err_task.cancel()
                return None
            chunk = await proc.stdout.read(PCM_CHUNK)
            if not chunk:
                break
            chunks.append(chunk)
        await proc.wait()
        try:
            await asyncio.wait_for(err_task, timeout=2.0)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            err_task.cancel()

        data = b"".join(chunks)
        if not data:
            text  = err_tail.decode(errors="replace")
            lines = [l.strip() for l in text.splitlines() if l.strip()]
            tail  = " | ".join(lines[-3:])[:400] if lines else "keine ffmpeg-Ausgabe"
            logger.warning("ffmpeg decode failed: %s", tail)
        return data

    @staticmethod
    async def _start_encoder() -> asyncio.subprocess.Process:
        return await asyncio.create_subprocess_exec(
            "ffmpeg", "-y",
            # Rohes PCM ist durch -f/-ar/-ac vollständig beschrieben — ohne
            # diese beiden Optionen liest ffmpeg erst ~5 s Audio zur
            # Formatanalyse ein, bevor überhaupt das erste MP3-Byte
            # herauskommt (gemessen: 2,96 s → 0,00 s). Genau diese Stille
            # am Anfang lässt Hardware-Radios abbrechen.
            "-probesize", "32", "-analyzeduration", "0",
            "-f", "s16le", "-ar", str(SAMPLE_RATE), "-ac", str(CHANNELS),
            "-i", "pipe:0",
            "-f", "mp3", "-b:a", ENCODE_BITRATE,
            "-write_xing", "0",
            # Jedes fertige MP3-Paket sofort ausgeben statt zu puffern —
            # sonst vergehen mehrere Sekunden bis zum ersten Ton, und
            # Hardware-Radios brechen bei zu langer Stille ab.
            "-flush_packets", "1",
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

    async def _await_with_keepalive(
        self,
        encoder: asyncio.subprocess.Process,
        aw,
        timeout: float,
        label: str,
    ) -> tuple:
        """
        *aw* ausführen und währenddessen den Stream mit Echtzeit-Stille am
        Leben halten (ICY-Watchdog und Player trennen sonst bei fehlenden
        Bytes). Deckt ALLE Zwischen-Song-Phasen ab: Queue füllen, URL
        auflösen, Dekodieren.

        Rückgabe: (result, status) mit status ∈ "ok" | "interrupted" |
        "timeout" | "error".
        """
        task     = asyncio.ensure_future(aw)
        silence  = bytes(BYTES_PER_SEC // 2)          # 0.5 s Stille
        deadline = time.monotonic() + timeout
        while True:
            try:
                # 0.5 s auf das Ergebnis warten …
                result = await asyncio.wait_for(asyncio.shield(task), timeout=0.5)
                return result, "ok"
            except asyncio.TimeoutError:
                pass
            except Exception as exc:
                logger.warning("[%s] %s failed: %s", self.uid, label, exc)
                return None, "error"
            if self.stopped or self._skip_event.is_set():
                task.cancel()
                return None, "interrupted"
            if time.monotonic() > deadline:
                logger.warning("[%s] %s timed out after %.0fs",
                               self.uid, label, timeout)
                task.cancel()
                return None, "timeout"
            # … und solange ~Echtzeit-Stille schreiben (Keepalive)
            if await self._write_pcm(encoder, silence):
                task.cancel()
                return None, "interrupted"

    async def _decode_foreground(
        self,
        encoder: asyncio.subprocess.Process,
        source: str,
        headers: dict | None = None,
        start_at: float = 0.0,
    ) -> bytes | None:
        """
        Song im Vordergrund dekodieren (kein Prefetch vorhanden).
        Rückgabe: PCM | None (Skip/Stop) | b"" (Fehler/Timeout → Song
        überspringen; die Stream-URL sollte invalidiert werden).
        """
        pcm, status = await self._await_with_keepalive(
            encoder,
            self._decode_pcm(source, self._skip_event, headers, start_at),
            FG_DECODE_TIMEOUT,
            "Foreground decode",
        )
        if status == "ok":
            return pcm
        if status == "interrupted":
            return None
        return b""

    async def _write_pcm(
        self,
        encoder: asyncio.subprocess.Process,
        pcm: bytes,
    ) -> bool:
        """
        Write *pcm* to encoder stdin at approx REALTIME speed.
        Returns True if interrupted by stop/skip.

        Verwendet die GLOBALE Takt-Uhr der Stream-Instanz: der Vorsprung
        gegenüber Echtzeit bleibt dauerhaft auf REALTIME_AHEAD begrenzt —
        egal wie viele Einzelaufrufe (Body, Blend, Keepalive-Stille) erfolgen.
        """
        if not pcm:
            return False
        assert encoder.stdin is not None
        if self._pace_base is None:
            self._pace_base = time.monotonic()
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
            self._pace_bytes += len(chunk)
            self._song_pos   += len(chunk)
            now  = time.monotonic()
            lead = self._pace_bytes / BYTES_PER_SEC - (now - self._pace_base)
            if lead > REALTIME_AHEAD:
                await asyncio.sleep(lead - REALTIME_AHEAD)
            elif lead < -PACE_MAX_BEHIND:
                # Nach Skip/Stillstand NICHT im Schnellvorlauf "aufholen"
                # (würde die Queue fluten) — Uhr neu verankern.
                self._pace_base  = now
                self._pace_bytes = 0
                await asyncio.sleep(0)
            else:
                await asyncio.sleep(0)
        return False

    # ── Pause / Fortsetzen ────────────────────────────────────────────────

    def _save_pause_state(self) -> None:
        """
        Beim Stoppen den laufenden Song "einfrieren": Position merken und das
        restliche PCM im RAM behalten, damit beim nächsten Play genau dort
        weitergespielt wird. Ist der Song praktisch zu Ende, wird nichts
        gemerkt (dann beginnt der nächste Song ganz normal).
        """
        song = self.current_song
        _paused_audio.pop(self.uid, None)
        if not song or not self._song_pcm:
            return

        # Rest ab der tatsächlichen Stopp-Stelle — ist davon kaum noch etwas
        # übrig, lohnt das Fortsetzen nicht (der Song war praktisch zu Ende).
        pos = max(0, min(len(self._song_pcm), (self._song_pos // 4) * 4))
        if len(self._song_pcm) - pos < MIN_RESUME_SEC * BYTES_PER_SEC:
            try:
                storage.update_user_state(self.uid, paused_song=None)
            except Exception:
                pass
            return

        played_s = self._song_offset_s + pos / BYTES_PER_SEC
        position = max(0.0, played_s - RESUME_PREROLL_SEC)

        # Auf Frame-Grenze (4 Byte = 1 Stereo-Sample) ausrichten
        cut  = int((position - self._song_offset_s) * BYTES_PER_SEC)
        cut  = max(0, min(pos, (cut // 4) * 4))
        rest = self._song_pcm[cut:]

        try:
            paused = dict(song)
            paused["position"] = round(position, 2)
            storage.update_user_state(self.uid, paused_song=paused)
        except Exception:
            logger.debug("[%s] Could not persist paused_song", self.uid)
            return

        if rest and len(rest) <= MAX_PAUSE_CACHE_BYTES:
            _paused_audio[self.uid] = {"key": song_key(song), "pcm": rest}

        logger.info(
            "[%s] Pausiert bei %.0fs: %s — %s (%s)",
            self.uid, position, song.get("artist", ""), song.get("song", ""),
            "Rest im Cache" if self.uid in _paused_audio else "wird neu dekodiert",
        )

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

        prefetched_song:   dict | None  = None
        prefetched_pcm:    bytes | None = None
        prefetched_offset: float        = 0.0

        try:
            while not self.stopped:
                self._skip_event.clear()

                # Station selection changed mid-song → drop the prefetched
                # next song (old selection); the queue was already rebuilt.
                if self._queue_dirty:
                    self._queue_dirty = False
                    prefetched_song   = None
                    prefetched_pcm    = None
                    prefetched_offset = 0.0

                if _cb_ensure_queue:
                    if prefetched_song is not None and prefetched_pcm is not None:
                        # Nahtloser Übergang steht bereit: Queue-Füllung im
                        # Hintergrund — KEIN Keepalive-Await, das zwischen
                        # Blend und Body des nächsten Songs Stille einfügen
                        # würde.
                        asyncio.create_task(_cb_ensure_queue(self.uid, count=1))
                    else:
                        # Kann einen yt-dlp-Resolve enthalten (5–30 s) — mit
                        # Keepalive, damit der Stream währenddessen nicht verstummt.
                        await self._await_with_keepalive(
                            encoder, _cb_ensure_queue(self.uid, count=1),
                            QUEUE_FILL_TIMEOUT, "Queue fill",
                        )
                        if self.stopped:
                            break
                        if self._skip_event.is_set():
                            continue          # wird am Loop-Anfang neu geleert

                # ── Get current song + PCM ─────────────────────────────────
                song_offset_s = 0.0
                if prefetched_song is not None and prefetched_pcm is not None:
                    song          = prefetched_song
                    song_pcm      = prefetched_pcm
                    # Der Anfang des Songs steckt bereits in der Überblendung
                    # — für Pause/Fortsetzen zählt diese Zeit mit.
                    song_offset_s = prefetched_offset
                    prefetched_song   = None
                    prefetched_pcm    = None
                    prefetched_offset = 0.0
                else:
                    song = (_cb_get_current_song(self.uid)
                            if _cb_get_current_song else None)
                    if not song:
                        # Leere Queue: 1 s Stille schreiben statt still zu
                        # warten — sonst kappt der ICY-Watchdog nach 60 s.
                        await self._write_pcm(encoder, bytes(BYTES_PER_SEC))
                        await asyncio.sleep(1.0)
                        continue

                    artist_    = song.get("artist", "")
                    song_name_ = song.get("song", "")

                    # ── Pausierter Song? Genau dort fortsetzen ────────────
                    # Das Rest-PCM liegt meist noch im Cache (Stop hat es
                    # aufgehoben) — dann startet die Wiedergabe sofort, ohne
                    # erneutes Auflösen/Dekodieren.
                    resume_at = pause_position(self.uid, song)
                    song_pcm  = pop_paused_audio(self.uid, song) if resume_at else None
                    if song_pcm:
                        logger.info("[%s] Fortsetzen bei %.0fs (aus Cache): %s — %s",
                                    self.uid, resume_at, artist_, song_name_)

                    if song_pcm is None:
                        from .downloader import get_audio_source as _get_src
                        # Auch der Resolve kann yt-dlp anwerfen → Keepalive.
                        source_, src_status = await self._await_with_keepalive(
                            encoder, _get_src(artist_, song_name_),
                            RESOLVE_TIMEOUT, "Resolve",
                        )
                        if src_status == "interrupted":
                            continue
                        if not source_:
                            logger.warning("[%s] Cannot resolve audio: %s — %s",
                                           self.uid, artist_, song_name_)
                            if _cb_advance_queue:
                                await _cb_advance_queue(self.uid, song)
                            continue

                        src_type = "file" if source_.startswith("/") else "url"
                        logger.info("[%s] Decoding (%s)%s: %s — %s",
                                    self.uid, src_type,
                                    f" ab {resume_at:.0f}s" if resume_at else "",
                                    artist_, song_name_)
                        from .downloader import get_source_headers as _get_hdrs
                        headers_ = (await _get_hdrs(artist_, song_name_)
                                    if src_type == "url" else None)
                        # Mit Stille-Keepalive + Timeout: ein hängender ffmpeg
                        # (gedrosselte/tote URL) darf nie mehr den ganzen Stream
                        # verstummen lassen (→ ICY-Watchdog kappte sonst alles).
                        song_pcm = await self._decode_foreground(
                            encoder, source_, headers_, resume_at)

                        if song_pcm is None or self._skip_event.is_set():
                            logger.info("[%s] Skipped during decode", self.uid)
                            if _cb_advance_queue:
                                await _cb_advance_queue(self.uid, song)
                            self._skip_event.clear()
                            continue

                    song_offset_s = resume_at

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

                # Grundlage für Pause/Fortsetzen: PCM ab hier + Schreibposition
                self._song_pcm      = song_pcm
                self._song_pos      = 0
                self._song_offset_s = song_offset_s
                # Der Pausenzustand ist mit dem Start aufgebraucht — sonst
                # würde der Song beim nächsten Play erneut ab hier starten.
                clear_paused(self.uid)

                # ── Diagnose: wie viel Audio haben wir wirklich? ──────────
                pcm_sec  = len(song_pcm) / BYTES_PER_SEC
                exp_sec  = song.get("duration") or 0
                logger.info(
                    "[%s] Now playing: %s — %s (%.0fs PCM%s%s)",
                    self.uid, song.get("artist", ""), song.get("song", ""),
                    pcm_sec, f", erwartet ~{exp_sec}s" if exp_sec else "",
                    f", fortgesetzt ab {song_offset_s:.0f}s" if song_offset_s else "",
                )
                # Beim Fortsetzen ist das PCM naturgemäß kürzer als die
                # Songdauer — das ist kein abgerissener Download.
                if not song_offset_s and exp_sec and pcm_sec < exp_sec * 0.8 - 5:
                    # Download ist vermutlich mittendrin abgerissen — URL
                    # verwerfen, damit der Titel nächstes Mal frisch resolved.
                    logger.warning(
                        "[%s] PCM deutlich kürzer als erwartet (%.0fs < %ds) — "
                        "Download abgerissen? Stream-URL wird invalidiert",
                        self.uid, pcm_sec, exp_sec,
                    )
                    try:
                        from .downloader import invalidate_stream_url
                        asyncio.create_task(invalidate_stream_url(
                            song.get("artist", ""), song.get("song", "")))
                    except Exception:
                        pass

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
                    from .downloader import (get_audio_source as _get_src,
                                             get_source_headers as _get_hdrs)
                    src = await _get_src(ns.get("artist", ""), ns.get("song", ""))
                    if not src:
                        return None
                    hdrs = (await _get_hdrs(ns.get("artist", ""), ns.get("song", ""))
                            if src.startswith("http") else None)
                    return await UserStream._decode_pcm(src, bg_skip, hdrs)

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
                        prefetched_song   = next_song
                        prefetched_pcm    = next_pcm[blend_n:]
                        prefetched_offset = blend_n / BYTES_PER_SEC
                    elif skipped:
                        self._skip_event.clear()
                else:
                    await self._write_pcm(encoder, tail_pcm)

        except asyncio.CancelledError:
            logger.info("[%s] Producer cancelled", self.uid)
        except Exception:
            logger.exception("[%s] Unexpected error in producer", self.uid)
        finally:
            self.stopped = True
            # Laufenden Song "einfrieren", BEVOR current_song verworfen wird —
            # beim nächsten Play geht es genau hier weiter.
            try:
                self._save_pause_state()
            except Exception:
                logger.debug("[%s] Could not save pause state", self.uid, exc_info=True)
            self.current_song = None
            self._song_pcm    = None
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
                us    = storage.read_user_state(self.uid)
                keep  = paused_yt_id(self.uid)   # pausierter Song: URL behalten
                yt_ids = [
                    item["yt_id"] for item in us.get("queue", [])
                    if item.get("yt_id") and item["yt_id"] != keep
                ]
                if yt_ids:
                    asyncio.create_task(_clear(yt_ids))
            except Exception:
                pass

    async def _relay_encoder(self, encoder: asyncio.subprocess.Process) -> None:
        assert encoder.stdout is not None
        try:
            while True:
                chunk = await encoder.stdout.read(STREAM_CHUNK)
                if not chunk:
                    break
                await self._broadcast(chunk)
            if not self.stopped:
                logger.warning("[%s] Encoder output ended unexpectedly — stopping stream",
                               self.uid)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("[%s] Encoder relay crashed — stopping stream", self.uid)
        finally:
            # Stirbt der Relay, würde der Producer sonst LAUTLOS in
            # encoder.stdin.drain() hängen (volle Pipe) — ohne Log, ohne
            # Disconnect. Stattdessen: Stream sauber beenden und die
            # ICY-Handler sofort aufwecken.
            if not self.stopped:
                self._force_stop()
                await self._broadcast(None)


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
