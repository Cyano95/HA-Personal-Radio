"""
Raw TCP stream server speaking HTTP/1.0 / ICY protocol.

Why a raw TCP server instead of Starlette/uvicorn?

  Starlette's StreamingResponse uses HTTP/1.1 Transfer-Encoding: chunked.
  Each audio chunk is wrapped in hex-prefixed framing bytes:

      5\r\n          ← chunk size in hex
      hello\r\n      ← chunk data
      0\r\n\r\n      ← end marker

  Browsers decode this transparently. Strict media clients (VLC, Chromecast,
  Sonos, Cast SDK, DLNA renderers) do not — they interpret the framing bytes
  as audio data and produce garbled output or refuse to play.

  HTTP/1.0 has no chunked encoding concept.  After the response headers, the
  server writes raw bytes until the connection closes.  This is the format
  used by Icecast, SHOUTcast, and every Internet radio server since 1999.
  Every audio client on the planet understands it.

Listens on STREAM_PORT (default 8789).
The stream URL sent to HA media players uses this port.
The Starlette server on MEDIA_PORT (8788) still handles the browser UI and
health/file endpoints; browsers work fine with either port.

Kompatibilität mit Hardware-Internetradios
------------------------------------------
Solche Geräte sind deutlich pingeliger als VLC oder ein Browser:

  • ICY-Metadaten: ``icy-metaint`` wird NUR gesendet, wenn der Client sie mit
    ``Icy-MetaData: 1`` anfordert — und dann mit einem echten Intervall plus
    eingebetteten Titelblöcken. Ein Gerät, das den Header ungefragt (oder mit
    dem Wert 0) bekommt, liest Audiobytes als Metadaten und meldet
    "Stream nicht abspielbar".
  • Die Antwort spiegelt die HTTP-Version der Anfrage und trägt dieselben
    Kopfzeilen wie ein Icecast-Server (Server, Cache-Control, Connection,
    Accept-Ranges).
  • Neben ``/stream/{uid}?token=…`` gibt es URL-Formen ohne Query-String und
    mit Dateiendung — manche Firmware kommt mit "?" nicht klar oder leitet
    den Codec aus der Endung ab:

        /listen/{token}.mp3      Stream
        /listen/{token}.m3u      Playlist, die auf .mp3 zeigt
        /listen/{token}.pls      dito im PLS-Format
"""
from __future__ import annotations

import asyncio
import logging
import os
from urllib.parse import parse_qs, urlparse

from .stream_server import _token_to_uid, get_or_create_stream

logger = logging.getLogger("personal_radio.icy")

STREAM_NAME = "Personal Radio"
BITRATE     = "128"

# Abstand zwischen zwei ICY-Metadatenblöcken (Standardwert von Shoutcast und
# Icecast). Nur relevant, wenn der Client Metadaten angefordert hat.
METAINT = 16_000


def _metadata_enabled() -> bool:
    """Addon-Option `icy_metadata` — Notausgang für zickige Geräte."""
    return os.environ.get("ICY_METADATA", "1").strip().lower() not in (
        "0", "false", "no", "off",
    )


# ── Antwort-Kopfzeilen ────────────────────────────────────────────────────────

def _http_version(request_version: str) -> str:
    """Antwort in derselben HTTP-Version wie die Anfrage."""
    return "HTTP/1.1" if request_version.strip().upper() == "HTTP/1.1" else "HTTP/1.0"


def _stream_headers(version: str, metaint: int | None) -> bytes:
    """
    Kopfzeilen wie ein Icecast-Server. Ohne Content-Length und mit
    ``Connection: close`` ist der Body auch in HTTP/1.1 eindeutig
    (Ende = Verbindungsende), also ohne chunked encoding.
    """
    lines = [
        f"{version} 200 OK",
        "Content-Type: audio/mpeg",
        "Server: PersonalRadio (Icecast compatible)",
        "Cache-Control: no-cache, no-store",
        "Pragma: no-cache",
        "Accept-Ranges: none",
        "Connection: close",
        f"icy-name: {STREAM_NAME}",
        "icy-genre: Various",
        f"icy-br: {BITRATE}",
        "icy-pub: 0",
    ]
    # NUR auf Anfrage — ein ungefragter (oder 0-)Wert bringt Radios dazu,
    # Audiobytes als Metadaten zu lesen.
    if metaint:
        lines.append(f"icy-metaint: {metaint}")
    return ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1")


def _simple_response(version: str, status: str, ctype: str, body: bytes) -> bytes:
    head = "\r\n".join([
        f"{version} {status}",
        f"Content-Type: {ctype}",
        f"Content-Length: {len(body)}",
        "Cache-Control: no-cache, no-store",
        "Connection: close",
    ]) + "\r\n\r\n"
    return head.encode("latin-1") + body


# ── ICY-Metadaten ─────────────────────────────────────────────────────────────

def _now_title(stream) -> str:
    song = getattr(stream, "current_song", None) or {}
    artist = str(song.get("artist", "")).strip()
    title  = str(song.get("song", "")).strip()
    both   = " - ".join(p for p in (artist, title) if p)
    # ' und ; beenden im ICY-Format das Feld — entfernen statt escapen.
    return both.replace("'", "").replace(";", "")[:200]


def _meta_block(title: str) -> bytes:
    """
    Ein ICY-Metadatenblock: 1 Längenbyte (Vielfache von 16), dann der
    aufgefüllte Text. Leerer Block (= keine Änderung) ist ein einzelnes 0-Byte.
    """
    if not title:
        return b"\x00"
    payload = f"StreamTitle='{title}';".encode("utf-8", "replace")
    payload = payload[:255 * 16]
    payload += b"\x00" * (-len(payload) % 16)
    return bytes([len(payload) // 16]) + payload


# ── Routing ───────────────────────────────────────────────────────────────────

_EXTENSIONS = {"mp3": "stream", "m3u": "m3u", "pls": "pls"}


def _split_extension(rest: str) -> tuple[str, str]:
    """'abc.m3u' → ('abc', 'm3u'); ohne bekannte Endung → (rest, 'stream')."""
    base, dot, ext = rest.rpartition(".")
    if dot and ext.lower() in _EXTENSIONS:
        return base, _EXTENSIONS[ext.lower()]
    return rest, "stream"


def _resolve_target(path: str, query: dict) -> tuple[str | None, str, str]:
    """
    Anfrage auf (uid, art, token) abbilden.
    *art* ∈ "stream" | "m3u" | "pls"; uid None = nicht berechtigt/unbekannt.
    """
    token = (query.get("token") or [""])[0]

    if path.startswith("/listen/"):
        # Token steckt im Pfad: /listen/<token>.mp3 — kein Query-String nötig
        rest, kind = _split_extension(path[len("/listen/"):])
        token = rest or token
        return _token_to_uid(token), kind, token

    if path.startswith("/stream/"):
        rest, kind = _split_extension(path[len("/stream/"):])
        uid = _token_to_uid(token)
        if not uid or uid != rest:
            return None, kind, token
        return uid, kind, token

    return None, "stream", token


def _playlist_body(kind: str, host: str, token: str) -> tuple[str, bytes]:
    url = f"http://{host}/listen/{token}.mp3"
    if kind == "pls":
        body = (
            "[playlist]\r\n"
            "NumberOfEntries=1\r\n"
            f"File1={url}\r\n"
            f"Title1={STREAM_NAME}\r\n"
            "Length1=-1\r\n"
            "Version=2\r\n"
        )
        return "audio/x-scpls", body.encode("utf-8")
    body = (
        "#EXTM3U\r\n"
        f"#EXTINF:-1,{STREAM_NAME}\r\n"
        f"{url}\r\n"
    )
    return "audio/x-mpegurl", body.encode("utf-8")


# ── Verbindungsbehandlung ─────────────────────────────────────────────────────

async def _read_request(reader: asyncio.StreamReader) -> tuple[str, str, str, dict]:
    """Requestzeile + Kopfzeilen lesen → (methode, pfad, version, headers)."""
    raw_line = await asyncio.wait_for(reader.readline(), timeout=15.0)
    if not raw_line:
        return "", "", "", {}

    parts = raw_line.decode("latin-1").strip().split()
    if len(parts) < 2:
        return "", "", "", {}
    method    = parts[0].upper()
    full_path = parts[1]
    version   = parts[2] if len(parts) > 2 else "HTTP/1.0"

    headers: dict[str, str] = {}
    while True:
        hdr = await asyncio.wait_for(reader.readline(), timeout=10.0)
        if hdr in (b"\r\n", b"\n", b""):
            break
        name, sep, value = hdr.decode("latin-1").partition(":")
        if sep:
            headers[name.strip().lower()] = value.strip()
    return method, full_path, version, headers


async def _handle_client(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
) -> None:
    peer = writer.get_extra_info("peername", ("?", 0))
    try:
        method, full_path, req_version, headers = await _read_request(reader)
        if not method:
            return
        version = _http_version(req_version)

        parsed = urlparse(full_path)
        path   = parsed.path                           # /stream/{uid}, /listen/…
        query  = parse_qs(parsed.query)

        uid, kind, token = _resolve_target(path, query)

        if not path.startswith(("/stream/", "/listen/")):
            writer.write(_simple_response(version, "404 Not Found",
                                          "text/plain", b"Not Found"))
            await writer.drain()
            return

        if not uid:
            logger.warning("ICY denied — bad token for %s from %s", path, peer)
            writer.write(_simple_response(version, "403 Forbidden",
                                          "text/plain", b"Forbidden"))
            await writer.drain()
            return

        # ── Playlist (.m3u/.pls) ──────────────────────────────────────────
        if kind in ("m3u", "pls"):
            host = headers.get("host") or _local_host(writer)
            ctype, body = _playlist_body(kind, host, token)
            writer.write(_simple_response(version, "200 OK", ctype, body))
            await writer.drain()
            logger.info("ICY playlist %s uid=%s from=%s", kind, uid, peer)
            return

        # Metadaten nur, wenn ausdrücklich angefordert (und nicht per
        # Addon-Option abgeschaltet)
        wants_meta = (headers.get("icy-metadata", "").strip() == "1"
                      and _metadata_enabled())
        metaint    = METAINT if wants_meta else None

        # ── HEAD request ──────────────────────────────────────────────────
        if method == "HEAD":
            writer.write(_stream_headers(version, metaint))
            await writer.drain()
            return

        # ── GET: subscribe and stream ─────────────────────────────────────
        logger.info("ICY connect  uid=%s from=%s ua=%r metadata=%s",
                    uid, peer, headers.get("user-agent", "-"),
                    "ja" if wants_meta else "nein")
        stream = await get_or_create_stream(uid)
        q      = await stream.subscribe()

        writer.write(_stream_headers(version, metaint))
        await writer.drain()

        since_meta = 0          # Audiobytes seit dem letzten Metadatenblock
        last_title = None       # zuletzt gesendeter Titel

        try:
            while True:
                try:
                    chunk = await asyncio.wait_for(q.get(), timeout=60.0)
                except asyncio.TimeoutError:
                    logger.warning("ICY timeout (60 s silence) uid=%s", uid)
                    break
                if chunk is None:
                    break
                try:
                    if metaint is None:
                        writer.write(chunk)
                    else:
                        # Audio in Blöcke von METAINT Bytes zerlegen und
                        # dazwischen den Titel einbetten.
                        rest = chunk
                        while rest:
                            room = metaint - since_meta
                            part, rest = rest[:room], rest[room:]
                            writer.write(part)
                            since_meta += len(part)
                            if since_meta >= metaint:
                                since_meta = 0
                                title = _now_title(stream)
                                if title and title != last_title:
                                    writer.write(_meta_block(title))
                                    last_title = title
                                else:
                                    writer.write(b"\x00")
                    await writer.drain()
                except (BrokenPipeError, ConnectionResetError, OSError):
                    break   # client disconnected
        finally:
            await stream.unsubscribe(q)
            logger.info("ICY disconnect uid=%s from=%s", uid, peer)

    except asyncio.TimeoutError:
        logger.debug("ICY handshake timeout from %s", peer)
    except Exception as exc:
        logger.debug("ICY client error from %s: %s", peer, exc)
    finally:
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:
            pass


def _local_host(writer: asyncio.StreamWriter) -> str:
    """Fallback für Playlists, wenn der Client keinen Host-Header schickt."""
    sock = writer.get_extra_info("sockname") or ("localhost", 0)
    try:
        return f"{sock[0]}:{sock[1]}"
    except Exception:
        return "localhost"


async def start_icy_server(port: int) -> None:
    """Start the ICY stream server and run until cancelled."""
    server = await asyncio.start_server(
        _handle_client,
        host="0.0.0.0",
        port=port,
    )
    addrs = [s.getsockname() for s in server.sockets]
    logger.info("ICY stream server listening on %s", addrs)
    async with server:
        await server.serve_forever()
