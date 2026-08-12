"""Webhook endpoints for HA automation integration."""
from __future__ import annotations

import logging

import asyncio

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel

from . import storage
from .queue_manager import apply_station_change, get_user_lock

logger = logging.getLogger("personal_radio.webhooks")
router = APIRouter(prefix="/webhook", tags=["webhooks"])

# Import playback engine at runtime to avoid circular imports
_playback_engine = None


def set_playback_engine(engine) -> None:
    global _playback_engine
    _playback_engine = engine


def _resolve_uid_by_token(token: str) -> str | None:
    """Find the HA user id that owns this media token."""
    users_dir = storage.USERS_DIR
    if not users_dir.exists():
        return None
    for token_file in users_dir.glob("*/media_token"):
        try:
            t = token_file.read_text().strip()
            if t == token:
                return token_file.parent.name
        except Exception:
            pass
    return None


def _auth(token: str) -> str:
    uid = _resolve_uid_by_token(token)
    if not uid:
        raise HTTPException(status_code=403, detail="Invalid token")
    return uid


class StationsBody(BaseModel):
    stations: list[str]


@router.post("/play")
async def webhook_play(token: str = Query(...)):
    uid = _auth(token)
    if _playback_engine:
        await _playback_engine.start_playback(uid)
    return {"ok": True}


@router.post("/stop")
async def webhook_stop(token: str = Query(...)):
    uid = _auth(token)
    if _playback_engine:
        await _playback_engine.stop_playback(uid)
    return {"ok": True}


@router.post("/skip")
async def webhook_skip(token: str = Query(...)):
    uid = _auth(token)
    if _playback_engine:
        await _playback_engine.skip_song(uid)
    return {"ok": True}


@router.post("/set_volume")
async def webhook_set_volume(token: str = Query(...), volume: float = Query(...)):
    uid = _auth(token)
    volume = max(0.0, min(1.0, volume))
    if _playback_engine:
        await _playback_engine.set_volume(uid, volume)
    return {"ok": True}


@router.post("/set_stations")
async def webhook_set_stations(token: str = Query(...), body: StationsBody = None, request: Request = None):
    uid = _auth(token)
    if body is None and request is not None:
        data = await request.json()
        stations_list = data.get("stations", [])
    else:
        stations_list = body.stations if body else []

    changed = False
    async with get_user_lock(uid):
        state = storage.read_user_state(uid)
        if stations_list != state.get("selected_stations"):
            state["selected_stations"] = stations_list
            changed = True
        storage.write_user_state(uid, state)
    # Änderung greift auch im laufenden Betrieb ab dem nächsten Song
    if changed:
        asyncio.create_task(apply_station_change(uid))
    return {"ok": True}
