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
  historyList:   document.getElementById('history-list'),
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
  if (now?.song && now?.artist) {
    D.trackTitle.textContent  = now.song;
    D.trackArtist.textContent = now.artist;
    D.stationChip.textContent = now.station || '';
    D.playerLabel.textContent = now.player_name || now.player_entity_id || '';
  } else {
    D.trackTitle.textContent  = isPlaying ? 'Lädt…' : 'Bereit';
    D.trackArtist.textContent = isPlaying ? '' : 'Sender auswählen und Play drücken';
    D.stationChip.textContent = '';
    D.playerLabel.textContent = '';
  }

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

  // Volume
  const vol = S.userState?.volume ?? 0.7;
  if (document.activeElement !== D.volSlider) {
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

// ── Render: Station grid ───────────────────────────────────────────────────────
function renderStations() {
  const selected = new Set(S.userState?.selected_stations || []);
  const q = S.search.toLowerCase();
  const list = S.stations.filter(s =>
    !q || s.station.toLowerCase().includes(q) || formatStationName(s.station).toLowerCase().includes(q)
  );

  D.stationsCount.textContent = `${selected.size} / ${S.stations.length}`;

  if (!list.length && !S.stations.length) {
    D.stationGrid.innerHTML = '<div class="stations-loading">Sender werden geladen…</div>';
    return;
  }
  if (!list.length) {
    D.stationGrid.innerHTML = '<div class="stations-loading">Keine Treffer</div>';
    return;
  }

  // Sort: selected first, then alphabetical
  list.sort((a, b) => {
    const as = selected.has(a.station), bs = selected.has(b.station);
    if (as !== bs) return as ? -1 : 1;
    return formatStationName(a.station).localeCompare(formatStationName(b.station), 'de');
  });

  D.stationGrid.innerHTML = '';
  for (const s of list) {
    const isSel = selected.has(s.station);
    const tile = document.createElement('div');
    tile.className = 'station-tile' + (isSel ? ' selected' : '');
    tile.innerHTML = `
      <div class="station-tile-name">${escHtml(formatStationName(s.station))}</div>
      <div class="station-tile-count">${(s.song_count || 0).toLocaleString('de')} Songs</div>
    `;
    tile.addEventListener('click', () => toggleStation(s.station));
    D.stationGrid.append(tile);
  }
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

async function toggleStation(name) {
  const state = S.userState;
  if (!state) return;
  const cur = [...(state.selected_stations || [])];
  const idx = cur.indexOf(name);
  if (idx === -1) {
    cur.push(name);
  } else {
    if (cur.length <= 1) { toast('Mindestens 1 Sender muss ausgewählt bleiben.', true); return; }
    cur.splice(idx, 1);
  }
  // Optimistic update
  S.userState.selected_stations = cur;
  render();
  // Persist
  const res = await api('api/user/state', 'POST', { selected_stations: cur });
  if (!res) {
    // Rollback
    S.userState.selected_stations = state.selected_stations;
    render();
    toast('Senderauswahl konnte nicht gespeichert werden.', true);
  } else if (S.now?.is_playing) {
    toast('Senderauswahl gespeichert — greift ab dem nächsten Song.');
  }
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
  clearTimeout(_volDebounce);
  _volDebounce = setTimeout(async () => {
    await api('api/user/state', 'POST', { volume: val / 100 });
    if (S.userState) S.userState.volume = val / 100;
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
  if (st)      S.userState = st;
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

D.playerSelect.addEventListener('change', e => selectPlayer(e.target.value));

D.search.addEventListener('input', e => {
  S.search = e.target.value;
  renderStations();
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
