"""HA media_player controller — stream-based playback."""
from __future__ import annotations

import asyncio
import logging
import os
from urllib.parse import urlparse

import httpx

from . import storage

logger = logging.getLogger("personal_radio.player")

HA_BASE = "http://supervisor/core/api"
_supervisor_token = os.environ.get("SUPERVISOR_TOKEN", "")
_ha_host: str | None = None


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {_supervisor_token}",
        "Content-Type": "application/json",
    }


def _is_docker_ip(ip: str) -> bool:
    return (
        ip.startswith("172.")
        or ip.startswith("10.0.")
        or ip == "127.0.0.1"
    )


async def get_ha_host() -> str:
    global _ha_host
    if _ha_host:
        return _ha_host

    media_host = os.environ.get("MEDIA_HOST", "").strip()
    if media_host:
        _ha_host = media_host
        logger.info("Media host from config option: %s", _ha_host)
        return _ha_host

    try:
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.get(
                "http://supervisor/network/interface/default/info",
                headers=_headers(),
            )
            if resp.status_code == 200:
                data = resp.json()
                ipv4 = data.get("data", {}).get("ipv4", {}).get("address", [])
                if isinstance(ipv4, list) and ipv4:
                    ip = ipv4[0].split("/")[0]
                elif isinstance(ipv4, str):
                    ip = ipv4.split("/")[0]
                else:
                    ip = ""
                if ip and not _is_docker_ip(ip):
                    _ha_host = ip
                    logger.info("Media host from Supervisor network API: %s", _ha_host)
                    return _ha_host
    except Exception as e:
        logger.debug("Supervisor network API failed: %s", e)

    try:
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.get(f"{HA_BASE}/config", headers=_headers())
            resp.raise_for_status()
            internal_url = resp.json().get("internal_url", "")
            if internal_url:
                host = urlparse(internal_url).hostname or ""
                if host and not _is_docker_ip(host):
                    _ha_host = host
                    logger.info("Media host from internal_url: %s", _ha_host)
                    return _ha_host
                elif host:
                    logger.warning(
                        "internal_url host '%s' looks like a Docker-internal IP — ignoring. "
                        "Set 'media_host' in addon options to your HA LAN IP.",
                        host,
                    )
    except Exception as e:
        logger.debug("Could not read HA internal_url: %s", e)

    _ha_host = "homeassistant"
    logger.warning(
        "Could not determine LAN IP automatically. "
        "Please set 'media_host' in addon options to your HA machine's IP address."
    )
    return _ha_host


async def get_media_players() -> list[dict]:
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(f"{HA_BASE}/states", headers=_headers())
            resp.raise_for_status()
            ONLINE = {"playing", "paused", "idle", "on", "standby"}
            return [
                {
                    "entity_id": s["entity_id"],
                    "name": s.get("attributes", {}).get("friendly_name", s["entity_id"]),
                    "state": s.get("state"),
                }
                for s in resp.json()
                if s["entity_id"].startswith("media_player.")
                and s.get("state") in ONLINE
            ]
    except Exception as e:
        logger.warning("Failed to fetch media_players: %s", e)
        return []


async def play_stream(uid: str) -> bool:
    """
    Send the user's personal radio stream URL to their configured HA media player.
    The stream URL is stable — HA connects once and the stream runs continuously.
    Song transitions and crossfading are handled server-side by the stream producer.
    """
    state       = storage.read_user_state(uid)
    entity_id   = state.get("player_entity_id")
    media_token = state.get("media_token", "")

    if not entity_id:
        logger.warning("[%s] Cannot play: no player_entity_id configured", uid)
        return False

    # ICY stream server (port 8789) — speaks HTTP/1.0, compatible with all media players
    stream_port = int(os.environ.get("STREAM_PORT", "8789"))
    ha_host     = await get_ha_host()
    url         = f"http://{ha_host}:{stream_port}/stream/{uid}?token={media_token}"

    logger.info("[%s] Sending stream URL to %s: %s", uid, entity_id, url)

    # media_content_type is player-specific:
    #   "music"      → works for most integrations (Sonos, DLNA, generic)
    #   "audio/mp3"  → Chromecast, some Cast devices
    #   "audio/mpeg" → DLNA / Kodi
    # Set MEDIA_CONTENT_TYPE env var (addon option) to override.
    content_type = os.environ.get("MEDIA_CONTENT_TYPE", "music").strip() or "music"
    logger.info("[%s] media_content_type=%s", uid, content_type)

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                f"{HA_BASE}/services/media_player/play_media",
                headers=_headers(),
                json={
                    "entity_id":          entity_id,
                    "media_content_id":   url,
                    "media_content_type": content_type,
                },
            )
            resp.raise_for_status()
            logger.info("[%s] play_media OK (status %d)", uid, resp.status_code)
        return True
    except Exception as e:
        logger.error("[%s] play_media failed: %s", uid, e)
        return False


async def stop_playback(uid: str) -> None:
    state     = storage.read_user_state(uid)
    entity_id = state.get("player_entity_id")
    if entity_id:
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                await client.post(
                    f"{HA_BASE}/services/media_player/media_stop",
                    headers=_headers(),
                    json={"entity_id": entity_id},
                )
        except Exception as e:
            logger.warning("[%s] Stop failed: %s", uid, e)

    # Nur dieses eine Feld ändern — der übrige Zustand wird frisch gelesen.
    # (Das komplette state-Dict von oben ist nach dem await veraltet und
    # würde z.B. eine zwischenzeitlich geänderte Senderauswahl überschreiben.)
    storage.update_user_state(uid, is_playing=False)
    await _fire_event("personal_radio_stopped", {
        "ha_user_id":       uid,
        "player_entity_id": entity_id,
    })


async def set_volume(uid: str, volume: float) -> None:
    state     = storage.read_user_state(uid)
    entity_id = state.get("player_entity_id")
    if not entity_id:
        return
    volume = max(0.0, min(1.0, volume))
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            await client.post(
                f"{HA_BASE}/services/media_player/volume_set",
                headers=_headers(),
                json={"entity_id": entity_id, "volume_level": volume},
            )
    except Exception as e:
        logger.warning("[%s] Volume failed: %s", uid, e)
    storage.update_user_state(uid, volume=volume)


async def _fire_event(event_type: str, data: dict) -> None:
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            await client.post(
                f"{HA_BASE}/events/{event_type}",
                headers=_headers(),
                json=data,
            )
    except Exception as e:
        logger.debug("Fire event '%s' failed: %s", event_type, e)


async def get_player_state(entity_id: str) -> dict | None:
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.get(
                f"{HA_BASE}/states/{entity_id}",
                headers=_headers(),
            )
            if resp.status_code == 200:
                return resp.json()
    except Exception:
        pass
    return None
