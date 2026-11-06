"""
WebDAV-Abgleich der Liste gehörter Titel.

Ablauf bei jedem Abgleich:

  1. Datei vom Server holen (GET). Gibt es sie noch nicht (404), geht es
     ohne Zusammenführen weiter.
  2. Fremdbestand in den eigenen einarbeiten — additiv, es gewinnt je Titel
     der jüngere Zeitstempel. Dadurch ist die Reihenfolge egal und zwei
     Installationen können parallel hören, ohne sich zu überschreiben.
  3. Den zusammengeführten Stand zurückschreiben (PUT).
  4. Die Rotationslisten neu aufbauen, damit die neu übernommenen Titel
     sofort als gehört gelten.

Es wird nie blind überschrieben: was auf dem Server liegt, ist vorher
eingearbeitet. Geht ein Schritt schief, bleibt der lokale Bestand
unangetastet und der Fehler steht im Log und in der Oberfläche.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time

import httpx

from . import listened

logger = logging.getLogger("personal_radio.webdav")

# Ergebnis des letzten Abgleichs — die Oberfläche zeigt es an.
_last: dict = {"at": 0, "ok": None, "error": None, "added": 0, "total": 0}
_sync_lock = asyncio.Lock()


def config() -> dict:
    return {
        "url":      os.environ.get("WEBDAV_URL", "").strip(),
        "user":     os.environ.get("WEBDAV_USER", "").strip(),
        "password": os.environ.get("WEBDAV_PASSWORD", ""),
        "interval": max(5, int(_int_env("WEBDAV_INTERVAL_MINUTES", 30))),
    }


def _int_env(name: str, default: int) -> int:
    try:
        raw = os.environ.get(name, "")
        return int(float(raw)) if str(raw).strip() != "" else default
    except (TypeError, ValueError):
        return default


def configured() -> bool:
    return bool(config()["url"])


def last_result() -> dict:
    return dict(_last)


def _auth(cfg: dict):
    if cfg["user"] or cfg["password"]:
        return (cfg["user"], cfg["password"])
    return None


async def sync_now(uid: str) -> dict:
    """
    Einmal abgleichen. Gibt das Ergebnis zurück (auch im Fehlerfall).
    Parallele Aufrufe werden serialisiert.
    """
    cfg = config()
    if not cfg["url"]:
        return {"ok": False, "error": "Keine WebDAV-Adresse konfiguriert.",
                "at": int(time.time()), "added": 0, "total": 0}

    async with _sync_lock:
        result = {"at": int(time.time()), "ok": False, "error": None,
                  "added": 0, "updated": 0, "total": 0}
        try:
            async with httpx.AsyncClient(timeout=30, auth=_auth(cfg),
                                         follow_redirects=True) as client:
                # ── 1. Fremdstand holen ──────────────────────────────────
                try:
                    resp = await client.get(cfg["url"])
                except Exception as e:
                    raise RuntimeError(f"Server nicht erreichbar: {e}") from e

                if resp.status_code == 200:
                    try:
                        remote = resp.json()
                    except Exception:
                        try:
                            remote = json.loads(resp.text)
                        except Exception as e:
                            raise RuntimeError(
                                "Datei auf dem Server ist kein gültiges JSON") from e
                    info = await asyncio.get_running_loop().run_in_executor(
                        None, listened.merge, remote)
                    result.update(info)
                elif resp.status_code in (401, 403):
                    raise RuntimeError(
                        f"Anmeldung abgelehnt (HTTP {resp.status_code}) — "
                        "Benutzer/Passwort prüfen")
                elif resp.status_code == 404:
                    logger.info("WebDAV: Datei existiert noch nicht — wird angelegt")
                else:
                    raise RuntimeError(f"Unerwartete Antwort HTTP {resp.status_code}")

                # ── 2. Zusammengeführten Stand hochladen ─────────────────
                body = json.dumps(listened.snapshot(), ensure_ascii=False,
                                  indent=1).encode("utf-8")
                put = await client.put(
                    cfg["url"], content=body,
                    headers={"Content-Type": "application/json"},
                )
                if put.status_code not in (200, 201, 204):
                    raise RuntimeError(
                        f"Hochladen fehlgeschlagen (HTTP {put.status_code}) — "
                        "Pfad und Schreibrechte prüfen")

            # ── 3. Rotationslisten nachziehen ───────────────────────────
            await asyncio.get_running_loop().run_in_executor(
                None, listened.rebuild_played_sets, uid)

            result["ok"]    = True
            result["total"] = listened.stats()["count"]
            logger.info("WebDAV-Abgleich ok: %d neu, %d aktualisiert, %d gesamt",
                        result.get("added", 0), result.get("updated", 0),
                        result["total"])
        except Exception as e:
            result["ok"]    = False
            result["error"] = str(e)
            logger.warning("WebDAV-Abgleich fehlgeschlagen: %s", e)

        _last.clear()
        _last.update(result)
        return result


async def periodic_sync(uid: str) -> None:
    """Hintergrundaufgabe: in festem Abstand abgleichen."""
    if not configured():
        logger.info("WebDAV nicht konfiguriert — kein Abgleich")
        return
    await asyncio.sleep(20)          # Start abwarten
    while True:
        try:
            await sync_now(uid)
        except Exception:
            logger.exception("WebDAV-Abgleich abgestürzt")
        await asyncio.sleep(max(5, config()["interval"]) * 60)
