/**
 * Personal Radio — Frontend SPA
 * Alle fetch()-Pfade relativ (kein führendes /).
 * <base href> wird vom Backend per X-Ingress-Path injiziert.
 */

// ── API helper ────────────────────────────────────────────────────────────────
async function api(path, method = 'GET', body = null) {
  const opts = { method, headers: { 'Content-Type': 'application/json' } };
  if (body !== null) opts.body = JSON.stringify(body);
  try {
    const r = await fetch(path, opts);
    if (!r.ok) { console.warn(`API ${method} ${path} → ${r.status}`); return null; }
    return r.json();
  } catch (e) {
    console.warn(`API error ${path}:`, e.message);
    return null;
  }
}

// ── App state ─────────────────────────────────────────────────────────────────
const S = {
  now:       null,    // nowplaying response
  userState: null,    // /api/user/state
  stations:  [],      // /api/stations
  players:   [],      // /api/user/players
  history:   [],      // /api/user/history

  // Optimistic / UI state
  pending:   null,    // 'play'|'stop'|'skip'|'prev'|null
  optPlay:   null,    // null = use server; true/false = optimistic override
  abortWait: false,   // Stop während laufendem Skip/Prev → Wartephase abbrechen
  volTouchedAt: 0,    // letzte eigene Lautstärke-Eingabe (Regler nicht überschreiben)
  volDragging:  false,
  search:    '',
  volTimer:  null,
  pollTimer: null,
};

// ── DOM refs ───────────────────────────────────────────────────────────────────
const D = {
  statusDot:     document.getElementById('status-dot'),
  playerSelect:  document.getElementById('player-select'),
  artImg:        document.getElementById('art-img'),
  artPlaceholder:document.getElementById('art-placeholder'),
  artWrap:       document.getElementById('art-wrap'),
  playerBg:      document.getElementById('player-bg'),
  trackTitle:    document.getElementById('track-title'),
  trackArtist:   document.getElementById('track-artist'),
  stationChip:   document.getElementById('station-chip'),
  playerLabel:   document.getElementById('player-name-label'),
  btnPlay:       document.getElementById('btn-play'),
  btnPrev:       document.getElementById('btn-prev'),
  btnSkip:       document.getElementById('btn-skip'),
  volSlider:     document.getElementById('volume-slider'),
  selectedChips: document.getElementById('selected-chips'),
  stationsCount: document.getElementById('stations-count'),
  search:        document.getElementById('station-search'),
  stationGrid:   document.getElementById('station-grid'),
  pauseChip:     document.getElementById('pause-chip'),
  historyList:   document.getElementById('history-list'),
  urlsCard:      document.getElementById('urls-card'),
  urlsList:      document.getElementById('urls-list'),
  toasts:        document.getElementById('toast-container'),
};

// ── Toast ─────────────────────────────────────────────────────────────────────
function toast(msg, isError = false) {
  const el = document.createElement('div');
  el.className = 'toast' + (isError ? ' toast-error' : '');
  el.textContent = msg;
  D.toasts.append(el);
  setTimeout(() => el.style.opacity = '0', 3600);
  setTimeout(() => el.remove(), 4000);
}

// ── Render: Player ─────────────────────────────────────────────────────────────
let _lastArtUrl = '';
let _lastBgUrl  = '';

function renderPlayer() {
  const now       = S.now;
  const isPlaying = S.optPlay !== null ? S.optPlay : now?.is_playing;
  const loading   = S.pending === 'play' || S.pending === 'stop';

  // Status dot
  D.statusDot.className = loading ? 'dot-loading' :
                           isPlaying ? 'dot-playing' : 'dot-idle';

  // Album art + background blur
  const artUrl = now?.cover_url || now?.thumbnail || '';
  if (artUrl !== _lastArtUrl) {
    _lastArtUrl = artUrl;
    if (artUrl) {
      D.artImg.src = artUrl;
      D.artImg.onload = () => {
        D.artImg.classList.add('loaded');
        D.artPlaceholder.classList.add('hidden');
      };
      D.artImg.onerror = () => {
        D.artImg.classList.remove('loaded');
        D.artPlaceholder.classList.remove('hidden');
      };
    } else {
      D.artImg.classList.remove('loaded');
      D.artPlaceholder.classList.remove('hidden');
    }
  }
  if (artUrl !== _lastBgUrl) {
    _lastBgUrl = artUrl;
    D.playerBg.style.backgroundImage = artUrl ? `url(${JSON.stringify(artUrl)})` : '';
  }

  // Floating animation when playing
  D.artWrap.classList.toggle('is-playing', !!isPlaying);

  // Track info
  // Pausiert = gestoppt, aber der angefangene Titel wartet an seiner Stelle
  // und wird beim nächsten Play genau dort fortgesetzt.
  const paused = !!now?.paused && !isPlaying && S.pending === null;
  if (now?.song && now?.artist) {
    D.trackTitle.textContent  = now.song;
    D.trackArtist.textContent = now.artist;
    D.stationChip.textContent = now.station || '';
    D.playerLabel.textContent = now.player_name || now.player_entity_id || '';
  } else if (S.now === null) {
    // Noch keine Antwort vom Server — nicht fälschlich "Bereit" zeigen
    D.trackTitle.textContent  = 'Verbinde…';
    D.trackArtist.textContent = '';
    D.stationChip.textContent = '';
    D.playerLabel.textContent = '';
  } else {
    D.trackTitle.textContent  = isPlaying ? 'Lädt…' : 'Bereit';
    D.trackArtist.textContent = isPlaying ? '' : 'Sender auswählen und Play drücken';
    D.stationChip.textContent = '';
    D.playerLabel.textContent = '';
  }
  D.pauseChip.textContent = paused
    ? `Pausiert · weiter bei ${fmtTime(now.paused_position)}`
    : '';

  // Play button state
  const btnPlayIcon  = D.btnPlay.querySelector('.icon-play');
  const btnStopIcon  = D.btnPlay.querySelector('.icon-stop');
  const btnSpinner   = D.btnPlay.querySelector('.btn-spinner');
  if (loading) {
    btnPlayIcon.style.display  = 'none';
    btnStopIcon.style.display  = 'none';
    btnSpinner.style.display   = 'block';
    D.btnPlay.classList.add('loading');
  } else if (isPlaying) {
    btnPlayIcon.style.display  = 'none';
    btnStopIcon.style.display  = '';
    btnSpinner.style.display   = 'none';
    D.btnPlay.classList.remove('loading');
  } else {
    btnPlayIcon.style.display  = '';
    btnStopIcon.style.display  = 'none';
    btnSpinner.style.display   = 'none';
    D.btnPlay.classList.remove('loading');
  }

  // Vor/Zurück bleiben blockiert, solange irgendeine Aktion läuft —
  // erst wenn der Songwechsel tatsächlich vollzogen ist, werden sie frei.
  D.btnPlay.disabled = loading;
  D.btnSkip.disabled = !isPlaying || S.pending !== null;
  D.btnPrev.disabled = !isPlaying || S.pending !== null;

  // Volume — Live-Wert des Players bevorzugen (externe Änderungen via HA,
  // Fernbedienung etc.). Kurz nach eigener Eingabe nicht überschreiben,
  // damit der Regler beim Ziehen nicht zurückspringt.
  let vol = S.userState?.volume ?? 0.7;
  if (typeof S.now?.volume === 'number') vol = S.now.volume;
  const recentlyTouched = Date.now() - S.volTouchedAt < 2500;
  if (!S.volDragging && !recentlyTouched) {
    D.volSlider.value = Math.round(vol * 100);
  }
}

// ── Render: Players dropdown ───────────────────────────────────────────────────
function renderPlayers() {
  const current = S.userState?.player_entity_id || '';
  // Preserve selection during re-render
  const sel = D.playerSelect.value || current;

  D.playerSelect.innerHTML = '<option value="">— Player wählen —</option>';
  for (const p of S.players) {
    const opt = document.createElement('option');
    opt.value = p.entity_id;
    opt.textContent = p.name;
    if (p.entity_id === sel) opt.selected = true;
    D.playerSelect.append(opt);
  }
}

// ── Render: Selected chips ─────────────────────────────────────────────────────
function renderSelectedChips() {
  const selected = S.userState?.selected_stations || [];
  D.selectedChips.innerHTML = '';

  if (!selected.length) {
    const el = document.createElement('span');
    el.className = 'sel-chips-empty';
    el.textContent = 'Keine Sender ausgewählt';
    D.selectedChips.append(el);
    return;
  }

  for (const name of selected) {
    const chip = document.createElement('span');
    chip.className = 'sel-chip';
    const label = document.createTextNode(formatStationName(name));
    const x = document.createElement('span');
    x.className = 'sel-chip-x';
    x.textContent = '×';
    x.title = 'Entfernen';
    x.addEventListener('click', e => { e.stopPropagation(); toggleStation(name); });
    chip.append(label, x);
    D.selectedChips.append(chip);
  }
}

// ── Render: Station list ───────────────────────────────────────────────────────
// Einzeilige Liste. Namen, die breiter als die Zeile sind, laufen durch
// (Marquee) statt abgeschnitten zu werden.
let _stationsSig = '';

function renderStations(force = false) {
  const selected = new Set(S.userState?.selected_stations || []);
  const q = S.search.toLowerCase();
  const list = S.stations.filter(s =>
    !q || s.station.toLowerCase().includes(q) || formatStationName(s.station).toLowerCase().includes(q)
  );

  D.stationsCount.textContent = `${selected.size} / ${S.stations.length}`;

  if (!list.length && !S.stations.length) {
    _stationsSig = 'loading';
    D.stationGrid.innerHTML = '<div class="stations-loading">Sender werden geladen…</div>';
    return;
  }
  if (!list.length) {
    _stationsSig = 'empty:' + q;
    D.stationGrid.innerHTML = '<div class="stations-loading">Keine Treffer</div>';
    return;
  }

  // Recency-Rang aus der History: zuletzt gehörte Sender zuerst
  const recentRank = new Map();
  for (const h of S.history || []) {
    if (h.station && !recentRank.has(h.station)) {
      recentRank.set(h.station, recentRank.size);
    }
  }

  // Sort: selected first, then recently played, then alphabetical
  list.sort((a, b) => {
    const as = selected.has(a.station), bs = selected.has(b.station);
    if (as !== bs) return as ? -1 : 1;
    const ra = recentRank.has(a.station) ? recentRank.get(a.station) : Infinity;
    const rb = recentRank.has(b.station) ? recentRank.get(b.station) : Infinity;
    if (ra !== rb) return ra - rb;
    return formatStationName(a.station).localeCompare(formatStationName(b.station), 'de');
  });

  // Nur neu aufbauen, wenn sich wirklich etwas geändert hat — sonst würde
  // jeder Polling-Durchlauf (alle 3 s) die Laufschrift zurücksetzen.
  const sig = list.map(s => `${s.station}|${s.song_count || 0}|${selected.has(s.station) ? 1 : 0}`).join('\n');
  if (!force && sig === _stationsSig) return;
  _stationsSig = sig;

  D.stationGrid.innerHTML = '';
  for (const s of list) {
    const isSel = selected.has(s.station);
    const tile = document.createElement('div');
    tile.className = 'station-tile' + (isSel ? ' selected' : '');
    tile.title = formatStationName(s.station);
    tile.innerHTML = `
      <span class="station-dot"></span>
      <div class="station-tile-name"><span>${escHtml(formatStationName(s.station))}</span></div>
      <span class="station-tile-count">${(s.song_count || 0).toLocaleString('de')} Songs</span>
    `;
    tile.addEventListener('click', () => toggleStation(s.station));
    D.stationGrid.append(tile);
  }
  scheduleMarqueeUpdate();
}

// ── Laufschrift für zu lange Sendernamen ──────────────────────────────────────
let _marqueeRaf = null;
function scheduleMarqueeUpdate() {
  if (_marqueeRaf) cancelAnimationFrame(_marqueeRaf);
  _marqueeRaf = requestAnimationFrame(() => {
    _marqueeRaf = null;
    updateMarquees();
  });
}

function updateMarquees() {
  for (const el of D.stationGrid.querySelectorAll('.station-tile-name')) {
    const inner = el.firstElementChild;
    if (!inner) continue;
    const shift = Math.ceil(inner.scrollWidth - el.clientWidth);
    if (shift > 3) {
      // ~35 px/s plus Pausen an den Enden
      el.style.setProperty('--marquee-shift', shift + 'px');
      el.style.setProperty('--marquee-dur', (3 + shift / 35).toFixed(1) + 's');
      el.classList.add('is-scrolling');
    } else if (el.classList.contains('is-scrolling')) {
      el.classList.remove('is-scrolling');
      el.style.removeProperty('--marquee-shift');
      el.style.removeProperty('--marquee-dur');
    }
  }
}

// ── Render: Stream-URLs für externe Geräte ────────────────────────────────────
// Hardware-Internetradios kommen mit "?token=…" oft nicht klar und erwarten
// eine Dateiendung — daher stehen die /listen/-Formen zuerst.
const URL_ROWS = [
  ['device_mp3', 'Internetradio (direkt)'],
  ['device_m3u', 'Internetradio (M3U-Playlist)'],
  ['device_pls', 'Internetradio (PLS-Playlist)'],
  ['browser',    'Browser / VLC zum Testen'],
];

let _urlsLoaded = false;
async function loadStreamUrls() {
  if (_urlsLoaded) return;
  const urls = await api('api/user/stream_urls');
  if (!urls) {
    D.urlsList.innerHTML = '<div class="stations-loading">URLs nicht verfügbar.</div>';
    return;
  }
  _urlsLoaded = true;
  D.urlsList.innerHTML = '';
  for (const [key, label] of URL_ROWS) {
    if (!urls[key]) continue;
    const row = document.createElement('div');
    row.className = 'url-row';
    row.innerHTML = `
      <div class="url-info">
        <div class="url-label">${escHtml(label)}</div>
        <div class="url-value">${escHtml(urls[key])}</div>
      </div>
      <button class="url-copy" type="button">Kopieren</button>
    `;
    row.querySelector('.url-copy')
       .addEventListener('click', e => copyUrl(urls[key], e.currentTarget));
    D.urlsList.append(row);
  }
}

async function copyUrl(text, btn) {
  let ok = false;
  try {
    // Nur im sicheren Kontext (https) verfügbar — sonst Fallback.
    await navigator.clipboard.writeText(text);
    ok = true;
  } catch {
    const ta = document.createElement('textarea');
    ta.value = text;
    ta.style.cssText = 'position:fixed;opacity:0';
    document.body.append(ta);
    ta.select();
    try { ok = document.execCommand('copy'); } catch { ok = false; }
    ta.remove();
  }
  btn.textContent = ok ? 'Kopiert' : 'Fehlgeschlagen';
  btn.classList.toggle('copied', ok);
  setTimeout(() => { btn.textContent = 'Kopieren'; btn.classList.remove('copied'); }, 1800);
  if (!ok) toast('Kopieren nicht möglich — URL bitte markieren.', true);
}

// ── Render: History ────────────────────────────────────────────────────────────
function renderHistory() {
  D.historyList.innerHTML = '';
  if (!S.history.length) {
    D.historyList.innerHTML = '<div class="history-empty">Noch nichts gespielt.</div>';
    return;
  }
  for (const item of S.history.slice(0, 20)) {
    const div = document.createElement('div');
    div.className = 'history-item';
    const thumb = item.thumbnail || item.cover_url;
    div.innerHTML = `
      ${thumb
        ? `<img class="history-thumb" src="${escHtml(thumb)}" alt="" loading="lazy">`
        : `<div class="history-thumb-placeholder">
             <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5">
               <circle cx="12" cy="12" r="10"/><circle cx="12" cy="12" r="3"/>
             </svg>
           </div>`
      }
      <div class="history-info">
        <div class="history-song">${escHtml(item.song || '—')}</div>
        <div class="history-artist">${escHtml(item.artist || '')}</div>
      </div>
      <div class="history-station">${escHtml(item.station || '')}</div>
    `;
    D.historyList.append(div);
  }
}

// ── Full render ────────────────────────────────────────────────────────────────
function render() {
  renderPlayer();
  renderSelectedChips();
  renderStations();
}

// ── Helpers ────────────────────────────────────────────────────────────────────
function formatStationName(raw) {
  return raw.replace(/_/g, ' ').replace(/\b\w/g, c => c.toUpperCase());
}
function fmtTime(sec) {
  const t = Math.max(0, Math.round(+sec || 0));
  return `${Math.floor(t / 60)}:${String(t % 60).padStart(2, '0')}`;
}
function escHtml(s) {
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

// ── Actions (optimistic) ───────────────────────────────────────────────────────
async function pressPlay() {
  if (S.pending === 'play' || S.pending === 'stop') return;
  const isPlaying = S.optPlay !== null ? S.optPlay : S.now?.is_playing;

  // Läuft gerade ein Skip/Prev, ist Stop trotzdem erlaubt — man soll
  // jederzeit sofort stoppen können. Die Wartephase wird abgebrochen.
  if (S.pending) {
    if (!isPlaying) return;
    S.abortWait = true;
  }

  if (!isPlaying) {
    if (!D.playerSelect.value) { toast('Bitte zuerst einen Player auswählen.', true); return; }
    if (!(S.userState?.selected_stations?.length)) { toast('Bitte mindestens einen Sender auswählen.', true); return; }
  }

  S.pending  = isPlaying ? 'stop' : 'play';
  S.optPlay  = !isPlaying;
  render();

  const path = isPlaying ? 'api/user/stop' : 'api/user/play';
  const res  = await api(path, 'POST');
  S.pending  = null;

  if (!res) {
    S.optPlay = null;
    toast(isPlaying ? 'Stopp fehlgeschlagen.' : 'Start fehlgeschlagen.', true);
  } else {
    S.optPlay = null;
    // Will be corrected by next poll
  }
  render();
}

function sleep(ms) { return new Promise(r => setTimeout(r, ms)); }
function nowSeq(now) { return now?.seq || 0; }

/**
 * Wartet, bis der Songwechsel tatsächlich vollzogen ist (Sequenznummer
 * ändert sich — erkennt auch denselben Titel erneut) oder die Wiedergabe
 * gestoppt wurde. Solange bleibt S.pending gesetzt → Vor/Zurück blockiert.
 * S.abortWait (Stop-Taste) bricht die Wartephase sofort ab.
 */
async function waitForActionDone(beforeSeq, timeoutMs = 15000) {
  const t0 = Date.now();
  while (Date.now() - t0 < timeoutMs) {
    if (S.abortWait) return false;
    await sleep(600);
    if (S.abortWait) return false;
    const now = await api('api/user/nowplaying');
    if (now) {
      S.now = now;
      render();
      if (nowSeq(now) !== beforeSeq) return true;
      if (!now.is_playing) return true;   // Wiedergabe wurde gestoppt
    }
  }
  return false;
}

async function pressSkip() {
  if (S.pending) return;
  S.pending = 'skip';
  render();
  // Frische Sequenznummer holen, damit ein natürlicher Songwechsel kurz
  // vor dem Klick den Warte-Vergleich nicht verfälscht.
  const fresh  = await api('api/user/nowplaying');
  if (fresh) S.now = fresh;
  const before = nowSeq(S.now);
  const res = await api('api/user/skip', 'POST');
  if (!res) {
    S.pending = null;
    toast('Überspringen fehlgeschlagen.', true);
    render();
    return;
  }
  // Blockiert lassen, bis der Skip wirklich vollzogen ist
  await waitForActionDone(before);
  S.abortWait = false;
  if (S.pending === 'skip') S.pending = null;   // nicht ein evtl. laufendes Stop überschreiben
  render();
}

async function pressPrev() {
  if (S.pending) return;
  S.pending = 'prev';
  render();
  const fresh  = await api('api/user/nowplaying');
  if (fresh) S.now = fresh;
  const before = nowSeq(S.now);
  const res = await api('api/user/prev', 'POST');
  if (!res) {
    S.pending = null;
    toast('Kein vorheriger Titel.', true);
    render();
    return;
  }
  // Blockiert lassen, bis der Wechsel wirklich vollzogen ist
  await waitForActionDone(before);
  S.abortWait = false;
  if (S.pending === 'prev') S.pending = null;
  render();
}

// ── Senderauswahl ──────────────────────────────────────────────────────────────
// Die Auswahl "sprang" bisher gelegentlich zurück: das 15-s-Polling ersetzte
// den kompletten Zustand, während eine Speicherung noch unterwegs war, und
// mehrere schnelle Klicks schickten konkurrierende Anfragen. Jetzt gilt:
//   • es ist immer nur EINE Speicherung unterwegs, spätere Klicks werden
//     zusammengefasst (der zuletzt gewünschte Stand gewinnt),
//   • solange gespeichert wird (plus kurzer Nachlauf) hat die lokale Auswahl
//     Vorrang vor den Polling-Antworten.
const SEL = {
  saving:    false,   // Speicherung läuft
  target:    null,    // gewünschter Stand, der noch geschickt werden muss
  settledAt: 0,       // Zeitpunkt der letzten abgeschlossenen Speicherung
};

// Während dieser Zeitspanne nach dem letzten Speichern gewinnt die lokale
// Auswahl — der Server braucht einen Moment, bis er sie zurückmeldet.
const SEL_GRACE_MS = 2500;

function selectionBusy() {
  return SEL.saving || SEL.target !== null ||
         (Date.now() - SEL.settledAt) < SEL_GRACE_MS;
}

async function flushSelection() {
  if (SEL.saving) return;              // läuft bereits — übernimmt SEL.target
  SEL.saving = true;
  try {
    while (SEL.target !== null) {
      const want = SEL.target;
      SEL.target = null;
      const res = await api('api/user/state', 'POST', { selected_stations: want });
      if (!res) {
        // Fehlgeschlagen: der Serverstand ist maßgeblich — noch offene
        // Änderungen verwerfen und den echten Stand holen.
        toast('Senderauswahl konnte nicht gespeichert werden.', true);
        SEL.target = null;
        const st = await api('api/user/state');
        if (st) S.userState = st;
        break;
      }
      // Nur die Antwort auf den ZULETZT gewünschten Stand übernehmen.
      if (SEL.target === null && S.userState && Array.isArray(res.selected_stations)) {
        S.userState.selected_stations = res.selected_stations;
      }
    }
  } finally {
    SEL.saving    = false;
    SEL.settledAt = Date.now();
    render();
  }
}

function toggleStation(name) {
  if (!S.userState) return;
  const cur = [...(S.userState.selected_stations || [])];
  const idx = cur.indexOf(name);
  if (idx === -1) {
    cur.push(name);
  } else {
    if (cur.length <= 1) { toast('Mindestens 1 Sender muss ausgewählt bleiben.', true); return; }
    cur.splice(idx, 1);
  }
  // Sofort anzeigen, Speicherung serialisiert hinterher
  S.userState.selected_stations = cur;
  SEL.target = cur;
  render();
  if (S.now?.is_playing) {
    toast('Senderauswahl greift ab dem nächsten Song.');
  }
  flushSelection();
}

async function selectPlayer(entityId) {
  if (!entityId) return;
  const res = await api('api/user/state', 'POST', { player_entity_id: entityId });
  if (!res) toast('Player konnte nicht gespeichert werden.', true);
  else {
    if (S.userState) S.userState.player_entity_id = entityId;
  }
}

let _volDebounce = null;
function onVolumeChange(val) {
  S.volTouchedAt = Date.now();
  clearTimeout(_volDebounce);
  _volDebounce = setTimeout(async () => {
    await api('api/user/state', 'POST', { volume: val / 100 });
    if (S.userState) S.userState.volume = val / 100;
    if (S.now) S.now.volume = val / 100;
  }, 400);
}

// ── Polling ────────────────────────────────────────────────────────────────────
async function pollNow() {
  const now = await api('api/user/nowplaying');
  if (now && S.optPlay === null) {
    S.now = now;
    render();
  }
}

async function pollAll() {
  const [now, st, players, hist] = await Promise.all([
    api('api/user/nowplaying'),
    api('api/user/state'),
    api('api/user/players'),
    api('api/user/history'),
  ]);
  if (now && S.optPlay === null) S.now = now;
  if (st) {
    // Eigene, noch nicht bestätigte Senderauswahl behalten — sonst kämen
    // gerade abgewählte Sender kurz darauf wieder zurück.
    if (selectionBusy() && S.userState) {
      st.selected_stations = S.userState.selected_stations;
    }
    S.userState = st;
  }
  if (players) { S.players = players; renderPlayers(); }
  if (hist)    { S.history = hist;   renderHistory(); }
  render();
}

async function pollStations() {
  const stations = await api('api/stations');
  if (stations) {
    S.stations = stations;
    renderStations();
  }
}

// ── Init ──────────────────────────────────────────────────────────────────────
async function init() {
  // Spielzustand SOFORT holen und rendern — der Rest lädt parallel nach.
  // So zeigt die UI beim Öffnen ohne Umweg den echten Zustand statt "Bereit".
  pollNow();

  // Load stations separately (can be slow)
  pollStations();

  // First full load
  await pollAll();

  // Fast now-playing poll (3s when playing, 6s when idle)
  setInterval(() => {
    if (S.pending) return;
    pollNow();
  }, 3000);

  // Slow full poll (state, players, history every 15s)
  setInterval(() => {
    if (S.pending) return;
    pollAll();
  }, 15000);

  // Refresh station list every 60s (new songs come in)
  setInterval(pollStations, 60000);
}

// ── Event listeners ────────────────────────────────────────────────────────────
D.btnPlay.addEventListener('click', pressPlay);
D.btnSkip.addEventListener('click', pressSkip);
D.btnPrev.addEventListener('click', pressPrev);

D.volSlider.addEventListener('input', e => onVolumeChange(+e.target.value));
D.volSlider.addEventListener('pointerdown', () => { S.volDragging = true; });
D.volSlider.addEventListener('pointerup',   () => { S.volDragging = false; S.volTouchedAt = Date.now(); });
D.volSlider.addEventListener('pointercancel', () => { S.volDragging = false; });

D.playerSelect.addEventListener('change', e => selectPlayer(e.target.value));

D.search.addEventListener('input', e => {
  S.search = e.target.value;
  renderStations();
});

// URLs erst beim Aufklappen holen (braucht die HA-Host-Ermittlung).
D.urlsCard.addEventListener('toggle', () => {
  if (D.urlsCard.open) loadStreamUrls();
});

// Schriftarten werden nachgeladen — danach sind die Textbreiten erst final.
if (document.fonts?.ready) document.fonts.ready.then(scheduleMarqueeUpdate);

// Bei geänderter Breite neu prüfen, welche Namen durchlaufen müssen.
let _resizeTimer = null;
window.addEventListener('resize', () => {
  clearTimeout(_resizeTimer);
  _resizeTimer = setTimeout(scheduleMarqueeUpdate, 200);
});

// Keyboard shortcuts
document.addEventListener('keydown', e => {
  if (e.target.tagName === 'INPUT' || e.target.tagName === 'SELECT') return;
  if (e.code === 'Space')      { e.preventDefault(); pressPlay(); }
  if (e.code === 'ArrowRight') pressSkip();
  if (e.code === 'ArrowLeft')  pressPrev();
});

// ── Start ─────────────────────────────────────────────────────────────────────
init();
