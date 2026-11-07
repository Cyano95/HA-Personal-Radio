# Personal Radio — Home Assistant Addon

Turns any HA `media_player` entity into a personalized radio station.  
Songs are fetched from your Station Log API, resolved via yt-dlp + ffmpeg, and streamed locally — a single shared instance (no per-user accounts).

---

## Installation

1. Add this repository to Home Assistant → **Settings → Add-ons → Add-on Store → ⋮ → Repositories**.
2. Install **Personal Radio**.
3. Configure the options (see below).
4. Start the addon — it appears in the HA sidebar as **Personal Radio**.

---

## Configuration

| Option | Description |
|---|---|
| `station_api_url` | Full URL of your Station Log API endpoint |
| `station_api_token` | API authentication token |
| `station_api_user` | API username |
| `fanart_api_key` | (Optional) fanart.tv API key for artist background photos |
| `media_port` | Port for direct MP3 serving (default: `8788`). Must be reachable by your media players on the local network. |
| `max_song_minutes` | Maximale Spieldauer eines Titels in Minuten — längere Titel werden nicht gespielt. `0` = unbegrenzt. |
| `no_repeat_hours` | Spanne in Stunden, in der kein Titel doppelt gespielt wird (gilt **pro Titel über alle Sender hinweg**) (tatsächlich vergangene Zeit, nicht Abspieldauer). `0` = klassische Vollrotation: erst wiederholen, wenn alle Titel des Senders gespielt wurden. Sind alle Titel bereits gespielt, bevor die Spanne abgelaufen ist, darf auch früher wiederholt werden (ältester zuerst). |
| `min_repeat_hours` | **Harte Untergrenze:** derselbe Titel kommt innerhalb dieser Spanne nicht erneut — unabhängig vom Rotationsmodus und über alle Quellen, Geräte und Neuinstallationen hinweg (Grundlage ist die dauerhafte Hörliste). Greift auch, wenn derselbe Titel unter abweichender Schreibweise in mehreren Quellen steht. `0` = aus. Standard: `4`. |
| `icy_metadata` | Titelmetadaten (ICY) in den Stream einbetten — nur für Clients, die sie mit `Icy-MetaData: 1` anfordern. Dann zeigt z.B. ein Internetradio Interpret und Titel im Display. Auf `false` stellen, falls ein Gerät damit nicht klarkommt. Standard: `true`. |
| `normalize_loudness` | Alle Titel auf dieselbe wahrgenommene Lautstärke bringen. Gemessen wird einmal pro Titel nach ITU-R BS.1770 (LUFS), angewendet wird eine **konstante** Verstärkung — wie ReplayGain. Kein Kompressor, die Dynamik im Titel bleibt vollständig erhalten. Standard: `true`. |
| `target_loudness` | Ziel-Lautheit in LUFS. `-14` ist der Streaming-Standard (Spotify/YouTube). Niedrigere Werte wie `-18` lassen dynamischen Aufnahmen mehr Reserve. Standard: `-14`. |
| `trim_silence` | Stille am **Anfang und Ende** eines Titels abschneiden, damit die Überblendung nicht ins Leere läuft. Pausen **innerhalb** eines Titels bleiben unangetastet. Standard: `true`. |
| `webdav_url` | Vollständige Datei-URL für den Abgleich der Hörliste, z.B. `https://cloud.example.org/remote.php/dav/files/ich/radio/gehoert.json`. Leer = kein Abgleich. |
| `webdav_user` / `webdav_password` | Zugangsdaten für den WebDAV-Server. |
| `webdav_interval_minutes` | Abstand zwischen zwei Abgleichen (mindestens 5). Standard: `30`. |

---

## Ports

| Port | Purpose |
|---|---|
| `8787` | Web UI + REST API — via HA Ingress (Seitenleiste) **und direkt** unter `http://<ha_host>:8787` erreichbar (auch außerhalb von HA) |
| `8788` (configurable) | MP3 file server — **direct host access**, no authentication proxy. Reachable by Sonos, Chromecast, etc. |

---

## Automation / Webhook Integration

Because HA addons cannot register native HA services, you interact with Personal Radio from automations via **webhook HTTP calls**.

Each webhook requires a `token` query parameter equal to the **media token** (visible in `/api/user/state` → `media_token`). Es gibt nur eine Instanz — der Token ist für alle Aufrufe derselbe.

### Available Webhooks (all `POST`)

| URL | Action |
|---|---|
| `http://localhost:8787/webhook/play?token=YOUR_TOKEN` | Start playback |
| `http://localhost:8787/webhook/stop?token=YOUR_TOKEN` | Stop playback |
| `http://localhost:8787/webhook/skip?token=YOUR_TOKEN` | Skip to next song |
| `http://localhost:8787/webhook/set_volume?token=YOUR_TOKEN&volume=0.7` | Set volume (0.0–1.0) |
| `http://localhost:8787/webhook/set_stations?token=YOUR_TOKEN` | Set stations (body: `{"stations": ["berlin"]}`) |

### Example: HA REST Command

```yaml
# configuration.yaml
rest_command:
  radio_play:
    url: "http://localhost:8787/webhook/play?token=YOUR_MEDIA_TOKEN"
    method: POST
  radio_stop:
    url: "http://localhost:8787/webhook/stop?token=YOUR_MEDIA_TOKEN"
    method: POST
  radio_skip:
    url: "http://localhost:8787/webhook/skip?token=YOUR_MEDIA_TOKEN"
    method: POST
  radio_volume:
    url: "http://localhost:8787/webhook/set_volume?token=YOUR_MEDIA_TOKEN&volume={{ volume }}"
    method: POST
```

### Example: Automation Trigger

Personal Radio fires HA events you can use as automation triggers:

```yaml
trigger:
  - platform: event
    event_type: personal_radio_song_started
action:
  - service: notify.mobile_app
    data:
      message: "Now playing: {{ trigger.event.data.artist }} — {{ trigger.event.data.song }}"
```

**Events fired:** `personal_radio_song_started`, `personal_radio_stopped`, `personal_radio_skipped`

---

## How It Works

```
Station Log API  ──(poll 1x/h; playing stations 1x/min)──►  Local station pool  ──►  Queue manager
                                                                        │
                                         yt-dlp + ffmpeg  ◄────────────┘
                                                │
                                         /data/library/<id>.mp3
                                                │
                                    http://<ha_host>:<media_port>/media/<id>
                                                │
                                         HA media_player entity
```

- Sender und Playlists sind gleichwertige Quellen; die Reihenfolge der Liste bestimmt, wie sie sich abwechseln.
- Es wird immer genau **ein** Song im Voraus vorbereitet (aufgelöst und während des laufenden Titels vordekodiert für den Crossfade).
- Es gibt genau **eine Instanz** (Queue, History, Media-Token) — keine Benutzertrennung.
- Die Station-Log-API wird **1x pro Stunde** komplett abgefragt; läuft der Player, werden die gerade abgespielten Sender zusätzlich **1x pro Minute** abgefragt.
- Dubletten werden erst NACH dem Auflösen endgültig geprüft — vorher ist die YouTube-ID bei Senderpool-Einträgen nicht bekannt. Steht dieselbe Aufnahme in zwei Quellen oder unter zwei Schreibweisen, wird sie nur einmal eingereiht.
- Liefert eine Quelle nichts (erschöpft, alles gesperrt), rückt die Rotation zur nächsten Quelle weiter. Erst wenn **keine** Quelle etwas hat, wird als letzte Rettung wiederholt — der laufende und der unmittelbar nächste Titel bleiben dabei immer tabu.
- Rotation: mit `no_repeat_hours: 0` wird ein Song erst wiederholt, wenn alle Titel des Senders gespielt wurden. Mit einem Wert > 0 wird ein Song innerhalb dieser Spanne (Wanduhrzeit) nicht wiederholt — außer alle Titel wurden bereits gespielt, dann darf früher wiederholt werden.
- Titel mit einer Spieldauer über `max_song_minutes` Minuten werden nie gespielt.
- Die No-Repeat-Spanne zählt ab dem tatsächlichen Abspielbeginn eines Titels und gilt senderübergreifend pro Titel.
- Eine geänderte Senderauswahl greift auch im laufenden Betrieb **ab dem nächsten Song**; der aktuelle Song läuft ungestört zu Ende.
- **Stop pausiert den laufenden Titel:** Der Stream wird beendet und der Player gestoppt, die Abspielstelle und das restliche Audio bleiben aber im Cache. Play setzt genau dort fort (ein paar Sekunden Vorlauf, damit nichts verloren geht). Skip/Vor/Zurück verwerfen die Pausenstelle; ist der Titel ohnehin fast zu Ende, beginnt Play mit dem nächsten.
- Öffnet ein Gerät den Stream von sich aus (URL direkt im Internetradio), erkennt das Webinterface das: der Zustand steht auf „spielt", Stop/Vor/Zurück wirken auf den laufenden Stream, und ein Hinweis „Externes Gerät" sagt, dass Lautstärke und Player-Auswahl dort nicht greifen.
- Lautheit und Ränder jedes Titels werden vor der Wiedergabe aufbereitet (siehe „Klangaufbereitung").
- Library is pruned to the 10 most recent MP3s (files currently playing are never deleted).

---

## Quellen: Sender und Playlists

Sender und YouTube-Playlists sind gleichwertige **Quellen**. Reihum wird aus
jeder Quelle der nächste Titel geholt — die Reihenfolge in der Liste bestimmt
also, wie sich die Quellen abwechseln. Jede Quelle lässt sich verschieben,
pausieren (bleibt in der Liste, liefert aber nichts) oder entfernen.

Playlists werden einmal über yt-dlp aufgelöst und als Titelvorrat unter
`/data/playlists/<id>.json` abgelegt. Ihre Titel bringen die Video-ID schon
mit, die Suche entfällt dort also. Rotation, No-Repeat-Spanne und die
dauerhafte Hörliste gelten für Playlists genauso wie für Sender.

`selected_stations` wird weiter mitgeführt (abgeleitet aus den Sender-Quellen),
damit Webhooks, die HA-Integration und bestehende Automationen unverändert
funktionieren.

| Endpunkt | Zweck |
|---|---|
| `GET /api/user/sources` | Quellen in Abspielreihenfolge |
| `PUT /api/user/sources` | ganze Liste ersetzen (Reihenfolge, Ein/Aus) |
| `POST /api/user/sources/remove` | Quelle entfernen |

## Auf YouTube suchen

Titel- und Playlistsuche über **yt-dlp**, ohne API-Schlüssel und ohne
Kontingent. Gesuchte Titel lassen sich direkt einreihen (als nächstes oder ans
Ende), gefundene Playlists als Quelle übernehmen; eine Playlist-Adresse kann
auch direkt eingefügt werden.

Echte YouTube-Empfehlungen gibt es damit nicht — die setzen die YouTube Data
API voraus. `GET /api/suggestions` leitet Vorschläge stattdessen aus den
meistgehörten Interpreten ab und nennt in `because`, worauf ein Vorschlag
beruht.

| Endpunkt | Zweck |
|---|---|
| `GET /api/search?q=…&kind=both\|tracks\|playlists` | Suche |
| `POST /api/playlists/add` | Playlist holen und als Quelle setzen |
| `POST /api/playlists/refresh` | Playlist neu einlesen |
| `GET /api/playlists` | gespeicherte Playlists |

## Warteschlange

Die Karte **Warteschlange** zeigt den laufenden Titel und alles, was folgt,
mit Herkunft und Spieldauer. Einträge lassen sich verschieben, einzeln
entfernen oder alle leeren; nachgefüllt wird automatisch aus den Quellen.

Bereits gespielte Titel werden nie angetastet. Entfernte, nie gehörte Titel
gibt das Addon für Rotation und Sperrfrist wieder frei — sie gelten also
nicht als gehört. Betrifft eine Änderung den unmittelbar nächsten Titel,
verwirft der Producer seinen Vorgriff, damit sie sofort greift.

| Endpunkt | Zweck |
|---|---|
| `GET /api/user/queue` | laufender Titel + kommende (Platz 0 = als nächstes) |
| `POST /api/user/queue/move` | `{position, to}` |
| `POST /api/user/queue/remove` | `{position}` |
| `POST /api/user/queue/add` | `{artist, song, yt_id?, play_next}` |
| `POST /api/user/queue/clear` | kommende Titel verwerfen |

---

## Gehörte Titel (dauerhaft, abgleichbar)

`/data/listened.json` merkt sich jeden tatsächlich abgespielten Titel —
**nach Interpret und Titel**, nicht nach YouTube-ID, unbefristet und in einer
einzelnen portablen Datei. Daraus werden beim Start die Rotationslisten wieder
aufgebaut; die Auswahl-Logik selbst bleibt unverändert, überlebt aber Neustart
und Neuinstallation.

Warum nicht die bisherigen Listen? `played/<sender>.json` und
`played_global.json` hängen an der YouTube-ID, werden nach 30 Tagen
beschnitten und liegen nur im Addon-Verzeichnis. Nach einer Neuinstallation
war alles weg — und selbst mit Backup half es wenig, weil derselbe Titel beim
erneuten Auflösen eine andere ID bekommen kann.

Die Liste wirkt auf zwei Wegen: Beim Start werden daraus die Rotationslisten wieder
aufgebaut, und `min_repeat_hours` sperrt denselben Titel zusätzlich quellen- und
geräteübergreifend. Diese Sperre ist nötig, weil die Vollrotation (`no_repeat_hours: 0`)
pro Quelle und nach Video-ID arbeitet — sie kann einen Titel also nicht erkennen, der
über eine andere Quelle, unter abweichender Schreibweise oder auf einem anderen Gerät
lief. Nachgeschlagen wird dafür sowohl nach Interpret+Titel als auch nach Video-ID.

In der Oberfläche unter **„Gehörte Titel"**: Umfang, Stand des Abgleichs und
die Knöpfe *Jetzt abgleichen*, *Exportieren*, *Importieren*.

**WebDAV-Abgleich** (Nextcloud, ownCloud, jeder WebDAV-Server): Bei jedem
Durchlauf wird die Datei vom Server geholt, **additiv** eingearbeitet (je
Titel gewinnt der jüngere Zeitstempel, die höhere Zählung bleibt) und der
zusammengeführte Stand zurückgeschrieben. Dadurch ist die Reihenfolge egal und
zwei Installationen können parallel hören, ohne sich zu überschreiben.
Schlägt ein Schritt fehl, bleibt der lokale Bestand unangetastet und der
Grund steht im Log und in der Oberfläche.

| Endpunkt | Zweck |
|---|---|
| `GET /api/user/listened` | Umfang und Stand des Abgleichs |
| `POST /api/user/listened/sync` | sofort abgleichen |
| `GET /api/user/listened/export` | Liste als Datei herunterladen |
| `POST /api/user/listened/import` | Liste einarbeiten (additiv) |

---

## Klangaufbereitung

Der Producer hat jeden Titel vollständig als PCM im Speicher, bevor er
abgespielt wird. Deshalb wird nicht live geregelt (ein Kompressor/AGC würde
pumpen und die Dynamik plätten), sondern **einmal pro Titel gemessen und ein
konstanter Faktor angewendet** — dasselbe Prinzip wie ReplayGain.

**Lautheit.** Gemessen wird die integrierte Lautheit nach ITU-R BS.1770
(K-Bewertung, absolutes und relatives Gate), also dasselbe Verfahren wie bei
`ffmpeg -af ebur128`; die Implementierung stimmt damit auf ±0,25 LU überein.
Die Anhebung ist auf 12 dB begrenzt und wird zusätzlich durch eine
Spitzenwertreserve gedeckelt (Ziel −1 dBFS), damit nichts übersteuert.

**Ränder.** Stille am Anfang und Ende wird abgeschnitten. Gesucht wird
ausschließlich von den Rändern nach innen, deshalb kann eine Pause *innerhalb*
eines Titels nie als Ende gewertet werden. Höchstens 30 s je Seite.

**Adaptive Überblendung.** Ist der Ausklang des laufenden oder der Anfang des
nächsten Titels deutlich leiser als der jeweilige Songkörper (sanftes Intro,
langer Ausklang), würde eine volle 5-Sekunden-Überblendung ein hörbares Loch
erzeugen — dort wären beide Titel gleichzeitig leise. Die Überblendung wird
dann verkürzt (3 s / 1,5 s / 0,6 s je nach Pegelabfall), der leise Teil bleibt
aber vollständig erhalten. Die Blendkurven arbeiten mit konstanter Leistung
(sin/cos); eine lineare Blende senkt die wahrgenommene Lautstärke in der Mitte
um ~3 dB.

Die Aufbereitung läuft im Threadpool und für den *nächsten* Titel im
Hintergrund, während der aktuelle noch spielt — hörbar wird sie nur beim
allerersten Titel nach dem Start (dort überbrückt die Keepalive-Stille).

---

## Stream in externen Geräten (Internetradio, VLC, Sonos …)

Die URLs stehen in der Weboberfläche unter **„Stream-URL für externe Geräte"**
(auch über `GET /api/user/stream_urls`). Der ICY-Server auf Port 8789 bietet
mehrere Formen derselben Wiedergabe:

| URL | Wofür |
|-----|-------|
| `http://<host>:8789/listen/<token>.mp3` | **Hardware-Internetradios.** Kein Query-String, Dateiendung vorhanden — damit kommt auch sparsame Firmware klar. |
| `http://<host>:8789/listen/<token>.m3u` | Playlist, die auf die `.mp3`-URL zeigt (manche Geräte wollen eine Playlist). |
| `http://<host>:8789/listen/<token>.pls` | dieselbe Playlist im PLS-Format. |
| `http://<host>:8789/stream/<uid>?token=<token>` | Bisherige Form; wird von Home Assistant genutzt und bleibt gültig. |
| `http://<host>:8788/stream/<uid>?token=<token>` | Zum Testen im Browser. |

Der ICY-Server verhält sich wie ein Icecast-Server: Antwort in der HTTP-Version
der Anfrage, Kopfzeilen `Server`, `Cache-Control`, `Connection: close`,
`Accept-Ranges: none`, dazu `icy-name`/`icy-genre`/`icy-br`/`icy-pub`.

**`icy-metaint` wird nur gesendet, wenn der Client `Icy-MetaData: 1`
anfordert** — dann mit Intervall 16000 und eingebetteten `StreamTitle`-Blöcken,
sodass das Gerät Interpret und Titel anzeigt. Wird der Header ungefragt (oder
mit dem Wert 0) geschickt, lesen viele Geräte Audiobytes als Metadaten und
melden „Stream nicht abspielbar" — genau das war bis Version 1.6.0 der Fall.

Kommt ein Gerät mit den Metadaten nicht klar, schaltet die Option
`icy_metadata: false` sie ab.

---

## Data Storage

All persistent data lives under `/data/` (addon data directory):

```
/data/
├── library/          # Downloaded MP3 files
├── cache/            # yt-dlp + cover art + artist background caches
├── stations/         # Local song pools per station
├── users/<uid>/      # Per-user queue, history, played sets, media token
└── station_cursors.json
```
