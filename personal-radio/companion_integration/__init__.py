"""
Personal Radio — Companion Integration v1.5

Kernproblem der vorherigen Versionen:
  - POST /api/hassio/ingress/session erfordert in HA 2022.11+ Admin-Rechte
    wenn über den HA-Proxy aufgerufen (hass.callApi → 401 für alle User)
  - SUPERVISOR_TOKEN aus os.environ ist in custom_components nicht zuverlässig

Lösung:
  1. Session-Erstellung passiert SERVER-SEITIG in Python über HA's eigenen
     hassio-Handler (hass.data["hassio"].send_command). Der hat immer Zugriff.
  2. Das Panel ruft GET /api/personal_radio/session_url ab — unser eigener
     Endpoint, require_admin=False, nutzt hass.fetchWithAuth im JS.
  3. hass.fetchWithAuth sendet den Bearer-Token — kein Admin nötig.
"""
from __future__ import annotations

import json
import pathlib
import logging
import os

from homeassistant.components import frontend
from homeassistant.components.http import HomeAssistantView
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED
from homeassistant.core import HomeAssistant
from aiohttp import web

_LOGGER   = logging.getLogger(__name__)
DOMAIN    = "personal_radio"
ADDON_SLUG = "local_personal-radio"

# Hassio handler key in hass.data (HA core)
_HASSIO_KEY = "hassio"

# Cached ingress URL (e.g. "/api/hassio_ingress/abc123/")
_ingress_url: str | None = None


# ── Supervisor-Zugriff über HA's eigenen Handler ──────────────────────────────

async def _hassio_command(hass: HomeAssistant, path: str, method: str = "get") -> dict:
    """Rufe den Supervisor über HA's eigenen hassio-Handler auf.

    send_command gibt den vollen Response zurück: {"result":"ok","data":{...}}
    Wir extrahieren immer den "data"-Teil.
    """
    hassio = hass.data.get(_HASSIO_KEY)
    if not hassio:
        raise RuntimeError("hassio-Handler nicht in hass.data — HA Supervisor aktiv?")
    raw = await hassio.send_command(path, method=method) or {}
    # send_command gibt {"result":"ok","data":{...}} zurück → "data" extrahieren
    if "data" in raw:
        return raw["data"] or {}
    return raw


async def _fetch_ingress_url(hass: HomeAssistant) -> str:
    global _ingress_url
    if _ingress_url:
        return _ingress_url
    try:
        data = await _hassio_command(hass, f"/addons/{ADDON_SLUG}/info")
        url  = data.get("ingress_url") or data.get("ingress_entry") or ""
        if url:
            _ingress_url = url
            _LOGGER.info("Ingress-URL gefunden: %s (Slug: %s)", url, ADDON_SLUG)
        else:
            _LOGGER.warning("Kein ingress_url/ingress_entry in Addon-Info. Keys: %s",
                            list(data.keys()))
    except Exception as exc:
        _LOGGER.warning("Ingress-URL nicht ermittelbar: %s", exc)
    return _ingress_url or ""


async def _create_session(hass: HomeAssistant) -> str:
    """Erstellt eine Ingress-Session über HA's hassio-Handler."""
    data = await _hassio_command(hass, "/ingress/session", method="post")
    return data.get("session", "")


# ── HTTP Views ─────────────────────────────────────────────────────────────────

class SessionURLView(HomeAssistantView):
    """
    GET /api/personal_radio/session_url

    Gibt eine vollständige Ingress-URL mit frischer Session zurück.
    require_admin=False — jeder eingeloggte HA-User darf dies aufrufen.
    Das Panel-JS nutzt hass.fetchWithAuth → Bearer-Token wird mitgesendet.
    """
    url           = "/api/personal_radio/session_url"
    name          = "api:personal_radio:session_url"
    requires_auth = True   # HA-Login erforderlich, KEIN Admin

    def __init__(self, hass_obj: HomeAssistant) -> None:
        self._hass = hass_obj

    async def get(self, request: web.Request) -> web.Response:
        try:
            ingress = await _fetch_ingress_url(self._hass)
            session = await _create_session(self._hass)
            if not ingress or not session:
                raise RuntimeError("Ingress-URL oder Session leer")
            url = ingress.rstrip("/") + "/?ingress_session=" + session
            return web.Response(
                text=json.dumps({"url": url, "ok": True}),
                content_type="application/json",
            )
        except Exception as exc:
            _LOGGER.warning("session_url fehlgeschlagen: %s", exc)
            return web.Response(
                status=503,
                text=json.dumps({"ok": False, "error": str(exc)}),
                content_type="application/json",
            )


class PanelJSView(HomeAssistantView):
    """
    GET /api/personal_radio/panel.js — das Custom Element.
    requires_auth=False: wird als JS-Modul vom Browser geladen.
    """
    url           = "/api/personal_radio/panel.js"
    name          = "api:personal_radio:panel_js"
    requires_auth = False
    cors_allowed  = True

    async def get(self, request: web.Request) -> web.Response:
        return web.Response(
            text=_PANEL_JS,
            content_type="application/javascript",
            headers={"Cache-Control": "no-cache, no-store"},
        )


class TestView(HomeAssistantView):
    """GET /api/personal_radio/test — Detaillierte Diagnose."""
    url           = "/api/personal_radio/test"
    name          = "api:personal_radio:test"
    requires_auth = False

    def __init__(self, hass_obj: HomeAssistant) -> None:
        self._hass = hass_obj

    async def get(self, request: web.Request) -> web.Response:
        result: dict = {
            "hassio_in_hass_data": _HASSIO_KEY in self._hass.data,
        }

        hassio = self._hass.data.get(_HASSIO_KEY)
        if not hassio:
            result["error"] = "hassio not in hass.data"
            return web.Response(text=json.dumps(result), content_type="application/json")

        # Alle Addons listen um den richtigen Slug zu finden
        try:
            addons_data = await hassio.send_command("/addons", method="get") or {}
            addons = addons_data.get("addons", [])
            radio_addons = [
                {"slug": a.get("slug"), "name": a.get("name"), "state": a.get("state")}
                for a in addons
                if "radio" in (a.get("slug","") + a.get("name","")).lower()
            ]
            result["radio_addons_found"] = radio_addons
        except Exception as exc:
            result["addons_list_error"] = str(exc)

        # Direkter Versuch — zeige sowohl raw als auch unwrapped
        try:
            raw  = await hassio.send_command(f"/addons/{ADDON_SLUG}/info", method="get") or {}
            data = raw.get("data", raw)   # unwrap falls nötig
            result["addon_info"] = {
                "raw_top_keys":  list(raw.keys()),
                "data_keys":     list(data.keys()) if isinstance(data, dict) else str(data),
                "ingress_url":   data.get("ingress_url") if isinstance(data, dict) else None,
                "ingress_entry": data.get("ingress_entry") if isinstance(data, dict) else None,
                "slug":          data.get("slug") if isinstance(data, dict) else None,
                "ingress":       data.get("ingress") if isinstance(data, dict) else None,
            }
        except Exception as exc:
            result["addon_info_error"] = str(exc)

        # Ingress-Panels listen
        try:
            panels = await hassio.send_command("/ingress/panels", method="get") or {}
            result["ingress_panels_keys"] = list(panels.keys())[:10]
        except Exception as exc:
            result["ingress_panels_error"] = str(exc)

        return web.Response(
            text=json.dumps(result, indent=2),
            content_type="application/json",
        )


# ── Panel JS ───────────────────────────────────────────────────────────────────

_PANEL_JS = r"""
/* Personal Radio Panel v1.5
   Nutzt hass.fetchWithAuth → kein Admin nötig */
class PersonalRadioPanel extends HTMLElement {
  connectedCallback() {
    this.style.cssText =
      'display:block;position:fixed;inset:0;background:#111318;overflow:hidden;z-index:1;';
    this._show('\u29d7 Starte\u2026', '#8b90a8');
    this._poll = setInterval(() => this._tryStart(), 200);
  }

  set hass(h)  { this._hass  = h; this._tryStart(); }
  set panel(p) { this._panel = p; this._tryStart(); }

  _tryStart() {
    if (this._started || !this._hass || !this._panel) return;
    this._started = true;
    clearInterval(this._poll);
    this._run();
  }

  async _run() {
    const u = this._hass?.user;
    this._show(`\u23f3 ${u?.name||'User'} (admin:${u?.is_admin}) \u2014 Session\u2026`, '#8b90a8');
    try {
      // hass.fetchWithAuth: sendet Bearer-Token, kein Admin nötig
      const resp = await this._hass.fetchWithAuth('/api/personal_radio/session_url');
      if (!resp.ok) {
        const txt = await resp.text().catch(() => '');
        throw new Error('HTTP ' + resp.status + (txt ? ': ' + txt.slice(0,120) : ''));
      }
      const data = await resp.json();
      if (!data.ok) throw new Error(data.error || 'Server-Fehler');
      this._loadFrame(data.url);
    } catch(e) {
      this._show('\u274c ' + (e.message || String(e)), '#ef4444');
      const btn = document.createElement('button');
      btn.textContent = '\u21ba Erneut versuchen';
      btn.style.cssText =
        'margin-top:16px;padding:8px 20px;background:#03a9f4;' +
        'color:#fff;border:none;border-radius:8px;cursor:pointer;font-size:14px';
      btn.onclick = () => { this._started = false; this.innerHTML = ''; this._run(); };
      this.appendChild(btn);
    }
  }

  _loadFrame(url) {
    this.innerHTML = '';
    const f = document.createElement('iframe');
    f.allow = 'autoplay';
    f.style.cssText = 'position:absolute;inset:0;width:100%;height:100%;border:none';
    f.src = url;
    this.appendChild(f);
    // Session-Erneuerung alle 25 Minuten
    clearTimeout(this._t);
    this._t = setTimeout(() => {
      this._started = false;
      this.innerHTML = '';
      this._run();
    }, 25 * 60 * 1000);
  }

  _show(msg, color) {
    this.innerHTML =
      '<div style="position:absolute;inset:0;display:flex;flex-direction:column;' +
      'align-items:center;justify-content:center;gap:12px;' +
      'font-family:sans-serif;font-size:14px;color:' + color + '">' +
      '<svg width="40" height="40" viewBox="0 0 24 24" fill="none" stroke="' + color +
      '" stroke-width="1.5"><circle cx="12" cy="12" r="10"/><circle cx="12" cy="12" r="3"/>' +
      '<path d="M12 9a6 6 0 0 1 6 6"/><path d="M12 6a9 9 0 0 1 9 9"/></svg>' +
      '<span>' + msg + '</span></div>';
  }

  disconnectedCallback() {
    clearTimeout(this._t);
    clearInterval(this._poll);
    this._started = false;
  }
}

if (!customElements.get('personal-radio-panel'))
  customElements.define('personal-radio-panel', PersonalRadioPanel);
"""


# ── Setup ─────────────────────────────────────────────────────────────────────

async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Wird beim ersten Einrichten via Einstellungen → Integrationen aufgerufen."""

    # HTTP Views registrieren (Session-URL + Panel-JS + Test)
    for view in (SessionURLView(hass), PanelJSView(), TestView(hass)):
        try:
            hass.http.register_view(view)
        except Exception:
            pass   # bereits registriert nach Reload

    async def _setup(event=None) -> None:
        # Ingress-URL vorab cachen
        ingress = await _fetch_ingress_url(hass)
        _LOGGER.info("Personal Radio bereit. Ingress: %s", ingress)

        # panel_custom via YAML ist zuverlässiger als async_register_built_in_panel.
        # Zeige einmalig die genaue YAML-Konfiguration als HA-Benachrichtigung.
        marker = pathlib.Path("/data/.panel_yaml_shown")
        if marker.exists():
            return

        yaml_snippet = (
            "panel_custom:\n"
            "  - name: personal-radio-panel\n"
            "    sidebar_title: Personal Radio\n"
            "    sidebar_icon: mdi:radio\n"
            "    url_path: personal-radio-panel\n"
            "    module_url: /api/personal_radio/panel.js\n"
            "    require_admin: false\n"
        )
        try:
            import httpx, os
            async with httpx.AsyncClient(timeout=5) as c:
                await c.post(
                    "http://supervisor/core/api/services/persistent_notification/create",
                    headers={"Authorization": f"Bearer {os.environ.get('SUPERVISOR_TOKEN','')}"},
                    json={
                        "title": "Personal Radio — Sidebar einrichten (letzter Schritt)",
                        "message": (
                            "Füge folgendes in `/config/configuration.yaml` ein "
                            "und starte Home Assistant neu:\n\n"
                            f"```yaml\n{yaml_snippet}```\n\n"
                            "Danach ist Personal Radio für **alle User** in der Seitenleiste.\n"
                            "*(Sessions werden serverseitig erstellt — kein Admin nötig)*"
                        ),
                        "notification_id": "personal_radio_panel_yaml",
                    },
                )
            marker.write_text("shown")
        except Exception as exc:
            _LOGGER.warning("Benachrichtigung fehlgeschlagen: %s", exc)
            _LOGGER.info(
                "\n\nFüge folgendes in configuration.yaml ein:\n%s", yaml_snippet
            )

    if hass.is_running:
        await _setup()
    else:
        hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STARTED, _setup)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    return True
