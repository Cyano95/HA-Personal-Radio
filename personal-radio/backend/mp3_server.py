"""Range-aware MP3 server on the media port (direct host access, no Ingress)."""
from __future__ import annotations

import logging
import os
from pathlib import Path

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import Response, StreamingResponse
from starlette.routing import Route

from . import storage

logger = logging.getLogger("personal_radio.mp3_server")
LIBRARY_DIR = storage.LIBRARY_DIR
CHUNK_SIZE = 65536


def _validate_token(token: str) -> bool:
    """Return True if the token belongs to any known user."""
    users_dir = storage.USERS_DIR
    if not users_dir.exists():
        return False
    for token_file in users_dir.glob("*/media_token"):
        try:
            if token_file.read_text().strip() == token:
                return True
        except Exception:
            pass
    return False


async def serve_mp3(request: Request) -> Response:
    yt_id = request.path_params.get("yt_id", "")
    token = request.query_params.get("token", "")

    if not yt_id or "/" in yt_id or ".." in yt_id:
        return Response("Bad request", status_code=400)

    if not token or not _validate_token(token):
        logger.warning("Media request denied — invalid token for yt_id=%s", yt_id)
        return Response("Forbidden", status_code=403)

    mp3_path = LIBRARY_DIR / f"{yt_id}.mp3"
    if not mp3_path.exists():
        logger.warning("Media not found: %s", mp3_path)
        return Response("Not found", status_code=404)

    file_size = mp3_path.stat().st_size
    range_header = request.headers.get("range")

    base_headers = {
        "Content-Type": "audio/mpeg",
        "Accept-Ranges": "bytes",
        "Cache-Control": "public, max-age=86400",
        "X-Content-Type-Options": "nosniff",
    }

    if not range_header:
        async def stream_full():
            with open(mp3_path, "rb") as f:
                while chunk := f.read(CHUNK_SIZE):
                    yield chunk
        return StreamingResponse(stream_full(),
                                 headers={**base_headers, "Content-Length": str(file_size)},
                                 status_code=200)

    # Parse Range header
    try:
        range_val = range_header.replace("bytes=", "").strip()
        parts = range_val.split("-")
        start = int(parts[0]) if parts[0] else 0
        end = int(parts[1]) if len(parts) > 1 and parts[1] else file_size - 1
        end = min(end, file_size - 1)
    except Exception:
        return Response("Invalid range", status_code=416,
                        headers={"Content-Range": f"bytes */{file_size}"})

    if start >= file_size or start > end:
        return Response("Range Not Satisfiable", status_code=416,
                        headers={"Content-Range": f"bytes */{file_size}"})

    length = end - start + 1

    async def stream_range():
        with open(mp3_path, "rb") as f:
            f.seek(start)
            remaining = length
            while remaining > 0:
                chunk = f.read(min(CHUNK_SIZE, remaining))
                if not chunk:
                    break
                yield chunk
                remaining -= len(chunk)

    return StreamingResponse(stream_range(), status_code=206, headers={
        **base_headers,
        "Content-Range": f"bytes {start}-{end}/{file_size}",
        "Content-Length": str(length),
    })


async def health(request: Request) -> Response:
    """Simple health check — confirms the media server is running."""
    mp3_count = len(list(LIBRARY_DIR.glob("*.mp3"))) if LIBRARY_DIR.exists() else 0
    return Response(f"OK — {mp3_count} MP3s in library", status_code=200)


media_app = Starlette(routes=[
    Route("/media/{yt_id}", serve_mp3, methods=["GET", "HEAD"]),
    Route("/health", health, methods=["GET"]),
])
