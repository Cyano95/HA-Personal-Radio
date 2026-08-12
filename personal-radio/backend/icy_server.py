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
"""
from __future__ import annotations

import asyncio
import logging
from urllib.parse import parse_qs, urlparse

from . import storage
from .stream_server import _token_to_uid, get_or_create_stream

logger = logging.getLogger("personal_radio.icy")

# ICY response header block sent to every client.
# HTTP/1.0 → no Transfer-Encoding: chunked.
# icy-metaint: 0 → no in-stream metadata injection.
_ICY_RESPONSE = (
    b"HTTP/1.0 200 OK\r\n"
    b"Content-Type: audio/mpeg\r\n"
    b"icy-name: Personal Radio\r\n"
    b"icy-br: 128\r\n"
    b"icy-pub: 0\r\n"
    b"icy-metaint: 0\r\n"
    b"\r\n"
)

_ICY_HEAD_RESPONSE = (
    b"HTTP/1.0 200 OK\r\n"
    b"Content-Type: audio/mpeg\r\n"
    b"icy-name: Personal Radio\r\n"
    b"icy-br: 128\r\n"
    b"\r\n"
)


async def _handle_client(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
) -> None:
    peer = writer.get_extra_info("peername", ("?", 0))
    try:
        # ── Read request line ─────────────────────────────────────────────
        raw_line = await asyncio.wait_for(reader.readline(), timeout=15.0)
        if not raw_line:
            return

        parts = raw_line.decode("latin-1").strip().split()
        if len(parts) < 2:
            return
        method    = parts[0].upper()
        full_path = parts[1]           # e.g. /stream/uid123?token=abc

        # ── Drain request headers ─────────────────────────────────────────
        while True:
            hdr = await asyncio.wait_for(reader.readline(), timeout=10.0)
            if hdr in (b"\r\n", b"\n", b""):
                break

        # ── Parse path + token ────────────────────────────────────────────
        parsed = urlparse(full_path)
        path   = parsed.path                           # /stream/{uid}
        qs     = parse_qs(parsed.query)
        token  = qs.get("token", [""])[0]

        if not path.startswith("/stream/"):
            writer.write(b"HTTP/1.0 404 Not Found\r\n\r\nNot Found")
            await writer.drain()
            return

        uid_path = path[len("/stream/"):]

        # ── Validate token ────────────────────────────────────────────────
        uid = _token_to_uid(token)
        if not uid or uid != uid_path:
            logger.warning("ICY denied — bad token for uid=%s from %s", uid_path, peer)
            writer.write(b"HTTP/1.0 403 Forbidden\r\n\r\nForbidden")
            await writer.drain()
            return

        # ── HEAD request ──────────────────────────────────────────────────
        if method == "HEAD":
            writer.write(_ICY_HEAD_RESPONSE)
            await writer.drain()
            return

        # ── GET: subscribe and stream ─────────────────────────────────────
        logger.info("ICY connect  uid=%s from=%s", uid, peer)
        stream = await get_or_create_stream(uid)
        q      = await stream.subscribe()

        # Send ICY response — body is raw bytes, no framing
        writer.write(_ICY_RESPONSE)
        await writer.drain()

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
                    writer.write(chunk)
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
