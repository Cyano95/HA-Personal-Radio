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
  sources:   [],      // /api/user/sources — Sender + Playlists, geordnet
  queue:     null,    // /api/user/queue
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
  sourceOrder:   document.getElementById('source-order'),
  queueCard:     document.getElementById('queue-card'),
  queueCount:    document.getElementById('queue-count'),
  queueList:     document.getElementById('queue-list'),
  btnQueueClear: document.getElementById('btn-queue-clear'),
  ytCard:        document.getElementById('yt-card'),
  ytQuery:       document.getElementById('yt-query'),
  ytGo:          document.getElementById('yt-go'),
  ytResults:     document.getElementById('yt-results'),
  stationsCount: document.getElementById('stations-count'),
  search:        document.getElementById('station-search'),
  stationGrid:   document.getElementById('station-grid'),
  pauseChip:     document.getElementById('pause-chip'),
  deviceChip:    document.getElementById('device-chip'),
  historyList:   document.getElementById('history-list'),
  listenedCard:  document.getElementById('listened-card'),
  listenedCount: document.getElementById('listened-count'),
  listenedStats: document.getElementById('listened-stats'),
  btnSync:       document.getElementById('btn-sync'),
  btnExport:     document.getElementById('btn-export'),
  btnImport:     document.getElementById('btn-import'),
  importFile:    document.getElementById('import-file'),
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
  // Der Stream kann auch von einem Gerät geöffnet worden sein, das die URL
  // direkt eingetragen hat. Dann wirken Lautstärke und Player-Auswahl nicht
  // auf das, was gerade zu hören ist — das sagt die Anzeige.
  D.deviceChip.textContent =
    (isPlaying && now?.external_only && S.pending === null) ? 'Externes Gerät' : '';

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

// ── Quellen in Abspielreihenfolge ─────────────────────────────────────────────
// Sender und Playlists sind gleichwertig: die Liste bestimmt, in welcher
// Reihenfolge reihum ein Titel geholt wird.
function renderSources() {
  const list = S.sources || [];
  const act  = list.filter(x => x.enabled !== false).length;
  D.stationsCount.textContent = list.length
    ? (act === list.length ? `${list.length}` : `${act} / ${list.length}`) : '0';
  D.stationsCount.title = `${act} von ${list.length} Quellen aktiv`;
  D.sourceOrder.innerHTML = '';
  if (!list.length) {
    D.sourceOrder.innerHTML =
      '<div class="sources-empty">Noch keine Quelle — Sender unten auswählen ' +
      'oder eine YouTube-Playlist hinzufügen.</div>';
    return;
  }
  list.forEach((src, i) => {
    const row = document.createElement('div');
    row.className = 'src-row' + (src.enabled === false ? ' off' : '') +
                    (src.kind === 'playlist' ? ' playlist' : '');
    const name = src.kind === 'playlist' ? src.name : formatStationName(src.name);
    row.innerHTML = `
      <span class="src-pos">${i + 1}</span>
      <span class="src-name" title="${escHtml(name)}">${escHtml(name)}</span>
      <span class="src-meta">${(src.count || 0).toLocaleString('de')} Titel</span>
      <button class="icon-btn" data-act="up"   title="Nach oben"  ${i === 0 ? 'disabled' : ''}>↑</button>
      <button class="icon-btn" data-act="down" title="Nach unten" ${i === list.length - 1 ? 'disabled' : ''}>↓</button>
      <button class="icon-btn" data-act="toggle" title="${src.enabled === false ? 'Einschalten' : 'Pausieren'}">${src.enabled === false ? '○' : '●'}</button>
      <button class="icon-btn danger" data-act="remove" title="Entfernen">×</button>
    `;
    row.querySelectorAll('.icon-btn').forEach(btn =>
      btn.addEventListener('click', () => sourceAction(btn.dataset.act, i)));
    D.sourceOrder.append(row);
  });
}

async function sourceAction(act, i) {
  const list = (S.sources || []).map(s => ({ ...s }));
  if (!list[i]) return;
  if (act === 'remove') {
    const res = await api('api/user/sources/remove', 'POST', { key: list[i].key });
    if (res) { S.sources = res.sources; renderSources(); renderStations(true); }
    else toast('Quelle konnte nicht entfernt werden.', true);
    return;
  }
  if (act === 'up'   && i > 0)                list.splice(i - 1, 0, list.splice(i, 1)[0]);
  if (act === 'down' && i < list.length - 1)  list.splice(i + 1, 0, list.splice(i, 1)[0]);
  if (act === 'toggle') list[i].enabled = list[i].enabled === false;
  await saveSources(list);
}

async function saveSources(list) {
  S.sources = list;                     // sofort anzeigen
  renderSources();
  const res = await api('api/user/sources', 'PUT', { sources: list });
  if (!res) { toast('Reihenfolge konnte nicht gespeichert werden.', true); await loadSources(); }
  else { S.sources = res.sources; renderSources(); renderStations(true); }
}

async function loadSources() {
  const res = await api('api/user/sources');
  if (res) { S.sources = res.sources; renderSources(); }
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

  const act = (S.sources || []).filter(x => x.enabled !== false).length;
  const all = (S.sources || []).length;
  D.stationsCount.textContent = all ? (act === all ? `${all}` : `${act} / ${all}`) : '0';
  D.stationsCount.title = `${act} von ${all} Quellen aktiv`;

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

// ── Warteschlange ─────────────────────────────────────────────────────────────
function fmtDur(sec) {
  if (!sec) return '';
  const t = Math.round(sec);
  return `${Math.floor(t / 60)}:${String(t % 60).padStart(2, '0')}`;
}

function renderQueue() {
  const q = S.queue;
  if (!q) return;
  D.queueCount.textContent = q.count || 0;
  const rows = [];
  if (q.current) {
    rows.push(`
      <div class="queue-row now">
        <div class="queue-info">
          <div class="queue-song">${escHtml(q.current.song || '')}</div>
          <div class="queue-sub">${escHtml(q.current.artist || '')} · läuft gerade</div>
        </div>
      </div>`);
  }
  if (!q.upcoming?.length) {
    rows.push('<div class="queue-empty">Nichts eingereiht — wird beim Abspielen ' +
              'automatisch aus den Quellen nachgefüllt.</div>');
  }
  D.queueList.innerHTML = rows.join('');

  (q.upcoming || []).forEach((it, i, arr) => {
    const row = document.createElement('div');
    row.className = 'queue-row';
    const sub = [it.artist, it.source_name, fmtDur(it.duration)].filter(Boolean).join(' · ');
    row.innerHTML = `
      <div class="queue-info">
        <div class="queue-song">${escHtml(it.song || '')}</div>
        <div class="queue-sub">${escHtml(sub)}</div>
      </div>
      <button class="icon-btn" data-act="up"   title="Nach vorn"  ${i === 0 ? 'disabled' : ''}>↑</button>
      <button class="icon-btn" data-act="down" title="Nach hinten" ${i === arr.length - 1 ? 'disabled' : ''}>↓</button>
      <button class="icon-btn danger" data-act="remove" title="Entfernen">×</button>
    `;
    row.querySelectorAll('.icon-btn').forEach(btn =>
      btn.addEventListener('click', () => queueAction(btn.dataset.act, it.position)));
    D.queueList.append(row);
  });
}

async function queueAction(act, pos) {
  let res = null;
  if (act === 'remove')    res = await api('api/user/queue/remove', 'POST', { position: pos });
  else if (act === 'up')   res = await api('api/user/queue/move', 'POST', { position: pos, to: pos - 1 });
  else if (act === 'down') res = await api('api/user/queue/move', 'POST', { position: pos, to: pos + 1 });
  if (res) { S.queue = res; renderQueue(); }
  else toast('Änderung an der Warteschlange fehlgeschlagen.', true);
}

async function loadQueue() {
  const res = await api('api/user/queue');
  if (res) { S.queue = res; renderQueue(); }
}

// ── YouTube-Suche ─────────────────────────────────────────────────────────────
async function ytSearch() {
  const q = D.ytQuery.value.trim();
  if (!q) return;
  D.ytGo.disabled = true;
  D.ytResults.innerHTML = '<div class="yt-empty">Suche läuft…</div>';

  // Eine eingefügte Playlist-Adresse direkt übernehmen statt zu suchen
  if (/[?&]list=|^https?:\/\/.*youtube|^PL[A-Za-z0-9_-]{10,}/.test(q)) {
    const res = await api('api/playlists/add', 'POST', { value: q });
    D.ytGo.disabled = false;
    if (res) {
      S.sources = res.sources; renderSources();
      D.ytResults.innerHTML = '';
      toast(`Playlist „${res.playlist.name}" übernommen (${res.playlist.count} Titel).`);
      return;
    }
    D.ytResults.innerHTML = '<div class="yt-empty">Playlist nicht erreichbar.</div>';
    return;
  }

  const res = await api(`api/search?q=${encodeURIComponent(q)}`);
  D.ytGo.disabled = false;
  if (!res) {
    D.ytResults.innerHTML = '<div class="yt-empty">Suche fehlgeschlagen.</div>';
    return;
  }
  renderYtResults(res);
}

function renderYtResults(res) {
  D.ytResults.innerHTML = '';
  const tracks = res.tracks || [], playlists = res.playlists || [];
  if (!tracks.length && !playlists.length) {
    D.ytResults.innerHTML = '<div class="yt-empty">Keine Treffer.</div>';
    return;
  }
  if (playlists.length) {
    D.ytResults.insertAdjacentHTML('beforeend',
      '<div class="yt-group-title">Playlists</div>');
    for (const pl of playlists) {
      const row = document.createElement('div');
      row.className = 'yt-row';
      row.innerHTML = `
        <div class="yt-info">
          <div class="yt-title">${escHtml(pl.name)}</div>
          <div class="yt-sub">${escHtml([pl.uploader, pl.count ? pl.count + ' Titel' : '']
                                        .filter(Boolean).join(' · '))}</div>
        </div>
        <button class="url-copy" type="button">Als Quelle</button>`;
      row.querySelector('button').addEventListener('click', e => addPlaylist(pl, e.currentTarget));
      D.ytResults.append(row);
    }
  }
  if (tracks.length) {
    D.ytResults.insertAdjacentHTML('beforeend',
      '<div class="yt-group-title">Titel</div>');
    for (const tr of tracks) {
      const row = document.createElement('div');
      row.className = 'yt-row';
      const sub = [tr.artist, fmtDur(tr.duration)].filter(Boolean).join(' · ');
      row.innerHTML = `
        <div class="yt-info">
          <div class="yt-title">${escHtml(tr.song || tr.title)}</div>
          <div class="yt-sub">${escHtml(sub)}</div>
        </div>
        <button class="url-copy" data-next="1" type="button">Als nächstes</button>
        <button class="url-copy" type="button">Ans Ende</button>`;
      row.querySelectorAll('button').forEach(btn =>
        btn.addEventListener('click', () => addTrack(tr, !!btn.dataset.next, btn)));
      D.ytResults.append(row);
    }
  }
}

async function addPlaylist(pl, btn) {
  btn.disabled = true; btn.textContent = 'Lade…';
  const res = await api('api/playlists/add', 'POST', { value: pl.id });
  btn.disabled = false; btn.textContent = res ? 'Hinzugefügt' : 'Als Quelle';
  if (res) {
    S.sources = res.sources; renderSources();
    toast(`„${res.playlist.name}" übernommen (${res.playlist.count} Titel).`);
  } else toast('Playlist konnte nicht übernommen werden.', true);
}

async function addTrack(tr, playNext, btn) {
  const label = btn.textContent;
  btn.disabled = true;
  const res = await api('api/user/queue/add', 'POST', {
    artist: tr.artist, song: tr.song, yt_id: tr.yt_id, play_next: playNext,
  });
  btn.disabled = false;
  btn.textContent = res ? 'Eingereiht' : label;
  if (res) {
    S.queue = res; renderQueue();
    toast(playNext ? 'Spielt als nächstes.' : 'Ans Ende der Warteschlange gesetzt.');
  } else toast('Titel konnte nicht eingereiht werden.', true);
}

// ── Gehörte Titel ─────────────────────────────────────────────────────────────
function fmtWhen(ts) {
  if (!ts) return 'nie';
  const d = new Date(ts * 1000);
  const mins = Math.round((Date.now() - d.getTime()) / 60000);
  if (mins < 1)    return 'gerade eben';
  if (mins < 60)   return `vor ${mins} min`;
  if (mins < 1440) return `vor ${Math.round(mins / 60)} h`;
  return d.toLocaleDateString('de');
}

function renderListened(d) {
  if (!d) return;
  D.listenedCount.textContent = `${(d.count || 0).toLocaleString('de')}`;
  const sync = d.sync || {};
  const items = [
    ['stat-value', (d.count || 0).toLocaleString('de'), 'Titel gemerkt'],
    ['stat-value', (d.plays || 0).toLocaleString('de'), 'Wiedergaben'],
    ['stat-value', fmtWhen(d.last_at), 'zuletzt gehört'],
  ];
  if (sync.configured) {
    const ok = sync.ok === true;
    items.push([
      'stat-value ' + (sync.ok === false ? 'stat-warn' : ok ? 'stat-ok' : ''),
      sync.at ? fmtWhen(sync.at) : 'noch nicht',
      sync.error ? 'Abgleich fehlgeschlagen' : `WebDAV-Abgleich (alle ${sync.interval} min)`,
    ]);
  } else {
    items.push(['stat-value', 'aus', 'WebDAV nicht eingerichtet']);
  }
  D.listenedStats.innerHTML = items.map(([cls, v, l]) =>
    `<div class="stat-item"><span class="${cls}">${escHtml(v)}</span>` +
    `<span class="stat-label">${escHtml(l)}</span></div>`).join('');
  if (sync.error) D.listenedStats.title = sync.error;
  D.btnSync.disabled = !sync.configured;
  D.btnSync.title = sync.configured ? '' : 'WebDAV-Adresse in den Addon-Optionen eintragen';
}

async function loadListened() {
  renderListened(await api('api/user/listened'));
}

async function syncListened() {
  D.btnSync.disabled = true;
  D.btnSync.textContent = 'Gleiche ab…';
  const res = await api('api/user/listened/sync', 'POST');
  D.btnSync.textContent = 'Jetzt abgleichen';
  if (res) toast(`Abgeglichen — ${res.added || 0} neu, ${res.count || 0} insgesamt.`);
  else     toast('Abgleich fehlgeschlagen — Details im Addon-Log.', true);
  await loadListened();
}

async function importListened(file) {
  let payload;
  try {
    payload = JSON.parse(await file.text());
  } catch {
    toast('Datei ist kein gültiges JSON.', true);
    return;
  }
  const res = await api('api/user/listened/import', 'POST', payload);
  if (res) toast(`Importiert — ${res.added || 0} neu, ${res.count || 0} insgesamt.`);
  else     toast('Import fehlgeschlagen.', true);
  await loadListened();
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
  renderSources();
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
  // Quellen und Warteschlange nur holen, wenn sie auch sichtbar sind.
  loadSources();
  if (D.queueCard.open) loadQueue();
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
  loadSources();

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

D.queueCard.addEventListener('toggle', () => { if (D.queueCard.open) loadQueue(); });
D.btnQueueClear.addEventListener('click', async () => {
  const res = await api('api/user/queue/clear', 'POST');
  if (res) { S.queue = res; renderQueue(); toast(`${res.removed} Titel entfernt.`); }
  else toast('Leeren fehlgeschlagen.', true);
});
D.ytGo.addEventListener('click', ytSearch);
D.ytQuery.addEventListener('keydown', e => { if (e.key === 'Enter') ytSearch(); });

D.listenedCard.addEventListener('toggle', () => {
  if (D.listenedCard.open) loadListened();
});
D.btnSync.addEventListener('click', syncListened);
D.btnExport.addEventListener('click', () => {
  // Ingress-tauglich: relativer Pfad, der Browser lädt die Datei herunter.
  const a = document.createElement('a');
  a.href = 'api/user/listened/export';
  a.download = 'personal-radio-gehoert.json';
  document.body.append(a); a.click(); a.remove();
});
D.btnImport.addEventListener('click', () => D.importFile.click());
D.importFile.addEventListener('change', e => {
  const f = e.target.files?.[0];
  if (f) importListened(f);
  e.target.value = '';
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
