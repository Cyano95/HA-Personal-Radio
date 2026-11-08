/**
 * Personal Radio — Android-App, Oberfläche im Stil einer Musik-App.
 * Alle Aufrufe gehen über die native Brücke an die lokale Engine
 * (gleiche Pfade wie die REST-API des Addons).
 */
'use strict';

// ── REST-Schnittstelle ────────────────────────────────────────────────────────
// Alle Pfade relativ (kein führendes /) — das Backend setzt für HA-Ingress
// ein <base href> in die Seite ein.
async function api(path, method = 'GET', body = null) {
  const opts = { method, headers: { 'Content-Type': 'application/json' } };
  if (body !== null) opts.body = JSON.stringify(body);
  try {
    const r = await fetch(path, opts);
    if (!r.ok) {
      let detail = '';
      try { detail = (await r.json()).detail || ''; } catch {}
      console.warn(`API ${method} ${path} → ${r.status} ${detail}`);
      return null;
    }
    return await r.json();
  } catch (e) {
    console.warn(`API ${path}:`, e.message);
    return null;
  }
}

// ── Zustand ───────────────────────────────────────────────────────────────────
const S = {
  now: null, sources: [], stations: [], history: [], queue: null, settings: null,
  sugg: null, tab: 'home', searchKind: 'both', searchRes: null, searching: false,
  libFilter: 'all', stationFilter: '', pending: false, lastErr: '', seq: -1, qver: -1,
};
const $ = id => document.getElementById(id);
const esc = s => String(s ?? '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
const sleep = ms => new Promise(r => setTimeout(r, ms));
const fmt = sec => { const t = Math.max(0, Math.round(+sec || 0)); return `${Math.floor(t / 60)}:${String(t % 60).padStart(2, '0')}`; };
const stationName = raw => String(raw || '').replace(/_/g, ' ').replace(/\b\w/g, c => c.toUpperCase());

function toast(msg, err = false) {
  const el = document.createElement('div');
  el.className = 'toast' + (err ? ' err' : '');
  el.textContent = msg;
  $('toasts').append(el);
  setTimeout(() => el.style.opacity = '0', 3200);
  setTimeout(() => el.remove(), 3600);
}

// ── Farbstil ──────────────────────────────────────────────────────────────────
const ACCENTS = [
  { id: 'green',   name: 'Grün',     c: '#1db954' },
  { id: 'blue',    name: 'Blau',     c: '#2e8bff' },
  { id: 'cyan',    name: 'Original', c: '#03a9f4' },
  { id: 'violet',  name: 'Violett',  c: '#a970ff' },
  { id: 'pink',    name: 'Pink',     c: '#ff4fa3' },
  { id: 'red',     name: 'Rot',      c: '#f2434a' },
  { id: 'orange',  name: 'Orange',   c: '#ff8a2a' },
  { id: 'gold',    name: 'Gold',     c: '#f5c518' },
  { id: 'teal',    name: 'Türkis',   c: '#1ec9a8' },
  { id: 'dynamic', name: 'Cover',    c: null },
];
const BGS = {
  dark:     { '--bg': '#121212', '--surface': '#1e1e1e', '--surface-2': '#2a2a2a', '--surface-3': '#3a3a3a' },
  black:    { '--bg': '#000000', '--surface': '#121212', '--surface-2': '#1f1f1f', '--surface-3': '#2e2e2e' },
  midnight: { '--bg': '#0e1322', '--surface': '#182033', '--surface-2': '#222b42', '--surface-3': '#303a56' },
};
const theme = { accent: 'green', bg: 'dark' };
let _barColor = '';

function lum(hex) {
  const n = parseInt(hex.slice(1), 16), r = (n >> 16) & 255, g = (n >> 8) & 255, b = n & 255;
  const f = v => { v /= 255; return v <= .03928 ? v / 12.92 : ((v + .055) / 1.055) ** 2.4; };
  return .2126 * f(r) + .7152 * f(g) + .0722 * f(b);
}
function applyTheme() {
  const bg = BGS[theme.bg] || BGS.dark;
  const songColor = S.now?.color || '';
  let accent = (ACCENTS.find(a => a.id === theme.accent) || ACCENTS[0]).c;
  if (!accent) accent = songColor || '#1db954';
  const vars = { ...bg, '--accent': accent, '--on-accent': lum(accent) > .32 ? '#000' : '#fff',
                 '--song': songColor || bg['--surface-3'] };
  const root = document.documentElement.style;
  for (const k in vars) root.setProperty(k, vars[k]);
  try { localStorage.setItem('pr-theme', JSON.stringify(vars)); } catch {}
  // Browser-Oberflächenfarbe (Adressleiste auf dem Handy)
  if (_barColor !== bg['--bg']) {
    _barColor = bg['--bg'];
    let m = document.querySelector('meta[name="theme-color"]');
    if (!m) { m = document.createElement('meta'); m.name = 'theme-color'; document.head.append(m); }
    m.content = _barColor;
  }
}
async function saveTheme() {
  applyTheme();
  renderSwatches();
  await api('api/settings', 'POST', { theme_accent: theme.accent, theme_background: theme.bg });
}
function renderSwatches() {
  $('sw-accent').innerHTML = ACCENTS.map(a => {
    const fill = a.c ? `background:${a.c}` : 'background:conic-gradient(#f2434a,#f5c518,#1db954,#2e8bff,#a970ff,#f2434a)';
    return `<button class="swatch${theme.accent === a.id ? ' on' : ''}" data-accent="${a.id}"><i style="${fill}"></i>${a.name}</button>`;
  }).join('');
  for (const b of $('sw-bg').children) b.classList.toggle('on', b.dataset.bg === theme.bg);
}
$('sw-accent').addEventListener('click', e => {
  const b = e.target.closest('[data-accent]'); if (!b) return;
  theme.accent = b.dataset.accent; saveTheme();
});
$('sw-bg').addEventListener('click', e => {
  const b = e.target.closest('[data-bg]'); if (!b) return;
  theme.bg = b.dataset.bg; saveTheme();
});

// ── Bilder ────────────────────────────────────────────────────────────────────
function hue(s) { let h = 0; for (const c of String(s)) h = (h * 31 + c.charCodeAt(0)) % 360; return h; }
function initials(name) {
  // Buchstaben-Wörter bevorzugen: "105.5-Spreeradio 80er" → "SP", "Bayern 3" → "BA", "Fritz" → "FR"
  const w = String(name).replace(/[^\p{L}\p{N} ]/gu, ' ').trim().split(/\s+/).filter(Boolean);
  const letters = w.filter(x => /^\p{L}/u.test(x));
  if (letters.length >= 2) return (letters[0][0] + letters[1][0]).toUpperCase();
  if (letters.length === 1) return letters[0].slice(0, 2).toUpperCase();
  return w.length ? w[0].slice(0, 3) : '♪';
}
/** Cover oder erzeugte Kachel (Farbe aus dem Namen, Initialen bzw. Radio-Symbol). */
function art(url, name, kind = 'station', cls = '') {
  const h = hue(name);
  const gen = `style="background:linear-gradient(135deg,hsl(${h} 55% 45%),hsl(${(h + 50) % 360} 60% 24%))"`;
  const inner = kind === 'playlist' || kind === 'track' ? '<svg viewBox="0 0 24 24"><use href="#i-radio"/></svg>' : esc(initials(name));
  if (url) {
    return `<img class="art ${cls}" src="${esc(url)}" alt="" loading="lazy" onerror="this.outerHTML='<div class=&quot;art gen ${cls}&quot; ${gen.replace(/"/g, '&quot;')}>${inner.replace(/"/g, '&quot;')}</div>'">`;
  }
  return `<div class="art gen ${cls}" ${gen}>${inner}</div>`;
}
const songArt = s => s?.cover_url || s?.thumbnail || '';

// ── Coverfarbe ────────────────────────────────────────────────────────────────
// Die Android-App lässt die Farbe des laufenden Titels nativ berechnen; im
// Browser geht das aus dem Bild selbst. Cover Art Archive und YouTube
// liefern CORS-Header, sonst bleibt es bei der gewählten Akzentfarbe.
const _colorCache = new Map();
function coverColor(url) {
  if (!url) return Promise.resolve('');
  if (_colorCache.has(url)) return Promise.resolve(_colorCache.get(url));
  return new Promise(resolve => {
    const img = new Image();
    img.crossOrigin = 'anonymous';
    img.onerror = () => { _colorCache.set(url, ''); resolve(''); };
    img.onload = () => {
      let hex = '';
      try {
        const n = 24;
        const c = document.createElement('canvas');
        c.width = c.height = n;
        const ctx = c.getContext('2d', { willReadFrequently: true });
        ctx.drawImage(img, 0, 0, n, n);
        const d = ctx.getImageData(0, 0, n, n).data;
        // Kräftige Bildpunkte stärker gewichten — ein Durchschnitt über
        // alles ergibt sonst fast immer ein müdes Grau.
        let r = 0, g = 0, b = 0, w = 0;
        for (let i = 0; i < d.length; i += 4) {
          const mx = Math.max(d[i], d[i+1], d[i+2]), mn = Math.min(d[i], d[i+1], d[i+2]);
          const sat = mx ? (mx - mn) / mx : 0;
          const lum = (d[i] + d[i+1] + d[i+2]) / 765;
          if (lum < .08 || lum > .95) continue;          // fast Schwarz/Weiß
          const k = .25 + sat;
          r += d[i] * k; g += d[i+1] * k; b += d[i+2] * k; w += k;
        }
        if (w > 0) {
          const h = v => Math.round(v / w).toString(16).padStart(2, '0');
          hex = '#' + h(r) + h(g) + h(b);
        }
      } catch { hex = ''; }                              // Bild ohne CORS
      _colorCache.set(url, hex);
      resolve(hex);
    };
    img.src = url;
  });
}

// ── Navigation (Hash → Back-Taste von Android funktioniert) ───────────────────
const TABS = ['home', 'search', 'library'];
const OVERLAYS = ['player', 'queue', 'settings', 'sheet'];
function go(name) {                      // Overlay/Seite öffnen (mit Verlauf)
  if (location.hash !== '#' + name) location.hash = name;
}
function back() { history.back(); }
function route() {
  const h = location.hash.slice(1) || S.tab;
  if (TABS.includes(h)) S.tab = h;
  for (const t of TABS) $('page-' + t).hidden = !(S.tab === t && h !== 'settings');
  $('page-settings').hidden = h !== 'settings';
  for (const b of $('tabs').children) b.classList.toggle('on', b.dataset.tab === S.tab);
  $('player').hidden = !(h === 'player' || (h === 'queue' && _queueFrom === 'player') || (h === 'sheet' && _sheetOver === 'player'));
  $('queue').hidden = !(h === 'queue' || (h === 'sheet' && _sheetOver === 'queue'));
  const sheetOpen = h === 'sheet';
  $('sheet').hidden = !sheetOpen; $('sheet-backdrop').hidden = !sheetOpen;
  if (!sheetOpen && _sheetResolve) { const r = _sheetResolve; _sheetResolve = null; r(); }
  $('tabs').hidden = h === 'settings';
  renderMini();
  if (h === 'settings') { loadSettings(); loadListened(); renderSwatches(); loadPlayers(); loadUrls(); }
  if (h === 'library') { loadStations(); loadSources(); }
  if (h === 'queue') loadQueue();
  if (h === 'home') { loadQueue(); loadSugg(); }
  if (TABS.includes(h)) window.scrollTo(0, 0);
}
window.addEventListener('hashchange', route);
$('tabs').addEventListener('click', e => {
  const b = e.target.closest('[data-tab]'); if (!b) return;
  S.tab = b.dataset.tab;
  history.replaceState(null, '', '#' + S.tab);
  route();
});
document.addEventListener('click', e => {
  if (e.target.closest('[data-open="settings"]')) go('settings');
  if (e.target.closest('[data-back]')) back();
  if (e.target.closest('[data-close]')) back();
  if (e.target.closest('[data-open-queue]')) { _queueFrom = 'player'; go('queue'); }
});
let _queueFrom = '';

// ── Aktionsblatt ──────────────────────────────────────────────────────────────
let _sheetResolve = null, _sheetOver = '';
function sheet(head, actions) {
  _sheetOver = !$('player').hidden && $('queue').hidden ? 'player' : (!$('queue').hidden ? 'queue' : '');
  $('sheet-head').innerHTML = head;
  $('sheet-head').hidden = !head;
  const box = $('sheet-actions');
  box.innerHTML = '';
  for (const a of actions) {
    if (a.html) { const d = document.createElement('div'); d.innerHTML = a.html; box.append(...d.childNodes); continue; }
    const b = document.createElement('button');
    if (a.danger) b.className = 'danger';
    b.innerHTML = `<svg viewBox="0 0 24 24"><use href="#${a.icon || 'i-plus'}"/></svg><span>${esc(a.label)}</span>`;
    b.addEventListener('click', async () => { await closeSheet(); a.run(); });
    box.append(b);
  }
  go('sheet');
}
function closeSheet() {
  return new Promise(r => { if (location.hash !== '#sheet') { r(); return; } _sheetResolve = r; back(); });
}
$('sheet-backdrop').addEventListener('click', () => closeSheet());
const sheetHead = (artHtml, t, s) => `${artHtml}<div><div class="t">${esc(t)}</div><div class="s">${esc(s)}</div></div>`;

// ── Daten laden ───────────────────────────────────────────────────────────────
async function pollNow() {
  const now = await api('api/user/nowplaying');
  if (!now) return;
  if (now.error && now.error !== S.lastErr && !S.pending) toast(now.error, true);
  S.lastErr = now.error || '';
  const prevArt = songArt(S.now);
  now.color = S.now?.color || '';
  S.now = now;
  const nowArt = songArt(now);
  if (nowArt !== prevArt) {
    now.color = '';
    applyTheme();
    coverColor(nowArt).then(hex => {
      if (songArt(S.now) === nowArt && S.now.color !== hex) {
        S.now.color = hex;
        applyTheme();
      }
    });
  }
  if (now.seq !== S.seq) { S.seq = now.seq; loadHistory(); }
  if (now.queue_version !== S.qver) { S.qver = now.queue_version; loadQueue(); }
  if (now.pool_version !== S.pver) {             // ein Sender wurde frisch geladen
    if (S.pver !== undefined) { loadSources(); if (S.tab === 'library') loadStations(); }
    S.pver = now.pool_version;
  }
  renderMini(); renderPlayer(); renderMix();
}
async function loadHistory() { const h = await api('api/user/history'); if (h) { S.history = h; renderRecent(); } }
async function loadQueue() { const q = await api('api/user/queue'); if (q) { S.queue = q; renderUpNext(); renderQueue(); } }
async function loadSources() {
  // Während eine Speicherung unterwegs ist (und kurz danach) hat die lokale
  // Liste Vorrang — sonst springt eine gerade geänderte Auswahl zurück,
  // wenn ein Polling-Durchlauf dazwischenfunkt.
  if (_srcSaving || Date.now() - _srcSavedAt < 1500) return;
  const s = await api('api/user/sources');
  if (s) { S.sources = s; renderSources(); renderLibrary(); renderMix(); }
}
let _srcSaving = false, _srcSavedAt = 0;
async function loadStations() { const s = await api('api/stations'); if (s) { S.stations = s; renderLibrary(); if (S.searchKind === 'stations' || S.searchRes) renderSearch(); } }
async function loadSugg(force = false) {
  if (S.sugg && !force) { renderSugg(); return; }
  const s = await api('api/suggestions', 'POST', { force });
  S.sugg = s || [];
  renderSugg();
}
async function refreshAll() {
  await Promise.all([pollNow(), loadSources(), loadHistory(), loadQueue()]);
  const st = await api('api/status');
  $('setup-hint').hidden = !(st && !st.configured && !S.sources.some(s => s.kind === 'playlist'));
}

// ── Mini-Player & Vollbild-Player ─────────────────────────────────────────────
function playState() {
  const n = S.now || {};
  return { active: !!n.active, playing: !!n.is_playing, loading: !!(n.is_playing && n.loading) || S.pending };
}
function setBtn(el, st) {
  el.classList.toggle('is-playing', st.playing);
  el.classList.toggle('is-loading', !!st.loading);
}
let _miniArt = '', _pArt = '';
function renderMini() {
  const n = S.now || {}, st = playState();
  const overlayOpen = !$('player').hidden || !$('queue').hidden || location.hash === '#settings';
  $('mini').hidden = !st.active || overlayOpen;
  if (!st.active) return;
  $('mini-title').textContent = n.song || (st.loading ? 'Titel wird vorbereitet…' : 'Pausiert');
  $('mini-sub').textContent = n.artist
    ? `${n.artist}${n.source_name ? ' · ' + stationName(n.source_name) : ''}`
    : (n.error || '');
  const a = songArt(n);
  if (a !== _miniArt) { _miniArt = a; $('mini-art').src = a || 'data:,'; }
  $('mini-progress').style.width = n.duration ? (100 * Math.min(n.position, n.duration) / n.duration) + '%' : '0';
  setBtn($('mini-play'), st);
}
function renderPlayer() {
  if ($('player').hidden) return;
  const n = S.now || {}, st = playState();
  const from = [stationName(n.source_name || ''), n.external_only ? 'externes Gerät' : '']
    .filter(Boolean).join(' · ');
  $('p-source').textContent = from || (st.active ? '—' : 'Nichts läuft');
  $('p-title').textContent = n.song
    || (st.loading ? 'Titel wird vorbereitet…' : st.active ? 'Pausiert' : 'Bereit');
  $('p-artist').textContent = n.artist || '';
  const a = songArt(n);
  if (a !== _pArt) {
    _pArt = a;
    $('p-art').hidden = !a; $('p-art-ph').hidden = !!a;
    if (a) $('p-art').src = a;
  }
  const bg = $('player-bg');
  bg.classList.toggle('has-img', !!n.artist_bg);
  if (n.artist_bg) bg.style.setProperty('--bg-img', `url(${JSON.stringify(n.artist_bg)})`);
  // Angefangener Titel wartet an seiner Stelle — Play setzt dort fort.
  const note = n.error || (n.paused && !st.active
    ? `Pausiert · weiter bei ${fmt(n.paused_position)}` : '');
  $('p-error').hidden = !note;
  $('p-error').textContent = note;
  $('p-error').classList.toggle('info', !n.error && !!note);
  const dur = +n.duration || 0, pos = Math.min(+n.position || 0, dur);
  $('p-fill').style.width = dur ? (100 * pos / dur) + '%' : '0';
  $('p-pos').textContent = fmt(pos); $('p-dur').textContent = fmt(dur);
  setBtn($('p-play'), st);
  $('p-prev').disabled = !st.active || !n.can_prev;
  $('p-next').disabled = !st.active;
  $('p-next-label').textContent = n.next ? 'Als Nächstes: ' + n.next : '';
}
function renderMix() {
  const st = playState();
  setBtn($('mix-play'), st);
  const on = S.sources.filter(s => s.enabled);
  $('mix-sub').textContent = on.length ? on.map(s => s.display).join(' · ') : 'Noch keine Quelle — füge Sender oder Playlists hinzu';
}

async function togglePlay() {
  if (S.pending) return;
  const st = playState();
  if (!st.playing && !S.sources.some(s => s.enabled) && !(S.queue?.count)) {
    toast('Wähle zuerst eine Quelle in der Bibliothek.', true);
    S.tab = 'library'; history.replaceState(null, '', '#library'); route();
    return;
  }
  S.pending = true; renderMini(); renderPlayer(); renderMix();
  const res = await api(st.playing ? 'api/user/stop' : 'api/user/play', 'POST');
  S.pending = false;
  if (!res || res.ok === false) toast(res?.error || 'Aktion fehlgeschlagen.', true);
  await pollNow();
}
async function step(path) {
  if (S.pending) return;
  S.pending = true; renderPlayer(); renderMini();
  const before = S.now?.seq;
  const res = await api(path, 'POST');
  if (!res) { S.pending = false; toast(path.endsWith('prev') ? 'Kein vorheriger Titel.' : 'Nicht möglich.', true); renderPlayer(); return; }
  for (let i = 0; i < 150; i++) {             // bis der neue Titel läuft (max. ~60 s)
    await sleep(400);
    await pollNow();
    if (S.now?.seq !== before || S.now?.error || !S.now?.is_playing) break;
    if (i === 3) { S.pending = false; renderPlayer(); renderMini(); }   // Spinner nur kurz, Lade-Status zeigt der Player
  }
  S.pending = false;
  renderPlayer(); renderMini();
}
$('mini-play').addEventListener('click', e => { e.stopPropagation(); togglePlay(); });
$('mini-open').addEventListener('click', () => go('player'));
$('mix-play').addEventListener('click', togglePlay);
$('p-play').addEventListener('click', togglePlay);
$('p-next').addEventListener('click', () => step('api/user/skip'));
$('p-prev').addEventListener('click', () => step('api/user/prev'));
$('p-more').addEventListener('click', () => {
  const n = S.now || {};
  sheet(sheetHead(art(songArt(n), n.song || '', 'track'), n.song || 'Personal Radio', n.artist || ''), [
    { icon: 'i-queue', label: 'Warteschlange', run: () => { _queueFrom = 'player'; go('queue'); } },
    { icon: 'i-search', label: `„${n.artist || ''}“ suchen`, run: () => { back(); setTimeout(() => searchFor(n.artist || ''), 120); } },
    { icon: 'i-x', label: 'Wiedergabe beenden', danger: true, run: async () => { await api('api/user/stop', 'POST'); back(); pollNow(); } },
  ]);
});

// ── Start ─────────────────────────────────────────────────────────────────────
function greeting() {
  const h = new Date().getHours();
  $('greeting').textContent = h < 5 ? 'Gute Nacht' : h < 11 ? 'Guten Morgen' : h < 17 ? 'Hallo' : h < 22 ? 'Guten Abend' : 'Gute Nacht';
}
function renderSources() {
  const playingKey = S.now?.active ? S.now?.station : '';
  const list = [...S.sources].sort((a, b) => (b.enabled - a.enabled)).slice(0, 8);
  $('home-sources').innerHTML = list.map(s =>
    `<button class="tile${s.enabled ? '' : ' off'}${s.key === playingKey ? ' playing' : ''}" data-src="${esc(s.key)}">
       ${art(s.thumbnail, s.display, s.kind)}<span>${esc(s.display)}</span></button>`).join('');
}
$('home-sources').addEventListener('click', e => {
  const t = e.target.closest('[data-src]'); if (t) sourceSheet(S.sources.find(s => s.key === t.dataset.src));
});
function renderUpNext() {
  const up = S.queue?.upcoming || [];
  $('h-upnext').hidden = !up.length;
  $('home-upnext').innerHTML = up.slice(0, 10).map(q =>
    `<button class="card-v" data-q="${q.position}">${art(songArt(q), q.song, 'track')}
       <div class="t">${esc(q.song)}</div><div class="s">${esc(q.artist)}</div></button>`).join('');
}
$('home-upnext').addEventListener('click', e => {
  const c = e.target.closest('[data-q]'); if (c) queueSheet(S.queue.upcoming[+c.dataset.q]);
});
function renderSugg() {
  const s = S.sugg || [];
  $('h-sugg').hidden = $('sugg-sub').hidden = !s.length;
  $('home-sugg').innerHTML = s.map((p, i) =>
    `<button class="card-v" data-sg="${i}">${art(p.thumbnail, p.name, 'playlist')}
       <div class="t">${esc(p.name)}</div><div class="s">Weil du ${esc(p.because)} hörst</div></button>`).join('');
}
$('home-sugg').addEventListener('click', e => {
  const c = e.target.closest('[data-sg]'); if (c) playlistSheet(S.sugg[+c.dataset.sg]);
});
function renderRecent() {
  const h = S.history.slice(0, 15);
  $('h-recent').hidden = !h.length;
  $('home-recent').innerHTML = h.map((s, i) =>
    `<button class="card-v" data-h="${i}">${art(songArt(s), s.song, 'track')}
       <div class="t">${esc(s.song)}</div><div class="s">${esc(s.artist)}</div></button>`).join('');
}
$('home-recent').addEventListener('click', e => {
  const c = e.target.closest('[data-h]'); if (c) trackSheet(S.history[+c.dataset.h]);
});

// ── Aktionen für Titel, Playlists, Quellen ────────────────────────────────────
function trackSheet(t) {
  const add = async (next) => {
    const r = await api('api/user/queue/add', 'POST', { artist: t.artist, song: t.song, yt_id: t.yt_id || '',
      duration: t.duration || 0, thumbnail: songArt(t), play_next: next });
    toast(r ? (next ? 'Kommt als Nächstes.' : 'Zur Warteschlange hinzugefügt.') : 'Einreihen fehlgeschlagen.', !r);
    loadQueue();
    if (r && !S.now?.active) toast('Tippe auf Play, um zu starten.');
  };
  sheet(sheetHead(art(songArt(t), t.song, 'track'), t.song, t.artist + (t.duration ? ' · ' + fmt(t.duration) : '')), [
    { icon: 'i-next', label: 'Als Nächstes spielen', run: () => add(true) },
    { icon: 'i-queue', label: 'Zur Warteschlange hinzufügen', run: () => add(false) },
    { icon: 'i-search', label: `Mehr von ${t.artist}`, run: () => searchFor(t.artist) },
  ]);
}
async function addPlaylist(p, playNow) {
  toast(`„${p.name}“ wird eingelesen…`);
  const r = await api('api/playlists/add', 'POST', { value: p.id || p.url });
  if (!r || r.ok === false) { toast(r?.error || 'Playlist konnte nicht gelesen werden.', true); return; }
  toast(`„${r.name}“ hinzugefügt — ${r.count} Titel.`);
  await loadSources();
  if (playNow) { await api('api/user/play_source', 'POST', { key: 'pl:' + r.id }); pollNow(); }
}
function playlistSheet(p) {
  const have = S.sources.some(s => s.key === 'pl:' + p.id);
  sheet(sheetHead(art(p.thumbnail, p.name, 'playlist'), p.name, [p.uploader, p.count ? p.count + ' Titel' : ''].filter(Boolean).join(' · ')), [
    { icon: 'i-play', label: 'Nur diese Playlist spielen', run: () => addPlaylist(p, true) },
    { icon: have ? 'i-check' : 'i-plus', label: have ? 'Ist schon eine Quelle — neu einlesen' : 'Als Quelle hinzufügen', run: () => addPlaylist(p, false) },
  ]);
}
async function saveSources(list) {
  S.sources = list; renderSources(); renderLibrary(); renderMix();
  _srcSaving = true;
  const r = await api('api/user/sources', 'POST', { sources: list.map(({ key, kind, name, enabled, id, url }) => ({ key, kind, name, enabled, id, url })) });
  _srcSaving = false; _srcSavedAt = Date.now();
  if (r) { S.sources = r; renderSources(); renderLibrary(); renderMix(); }
  else toast('Quellen konnten nicht gespeichert werden.', true);
  if (S.now?.active) toast('Gilt ab dem nächsten automatisch gewählten Titel.');
}
function sourceSheet(s) {
  if (!s) return;
  const i = S.sources.indexOf(s);
  const move = d => { const l = [...S.sources]; const [x] = l.splice(i, 1); l.splice(Math.max(0, Math.min(l.length, i + d)), 0, x); saveSources(l); };
  const acts = [
    { icon: 'i-play', label: 'Nur diese Quelle spielen', run: async () => { await api('api/user/play_source', 'POST', { key: s.key }); await loadSources(); pollNow(); } },
    { icon: s.enabled ? 'i-x' : 'i-check', label: s.enabled ? 'Pausieren (bleibt in der Liste)' : 'Wieder einschalten',
      run: () => saveSources(S.sources.map(x => x === s ? { ...x, enabled: !x.enabled } : x)) },
  ];
  if (i > 0) acts.push({ icon: 'i-up', label: 'Nach oben', run: () => move(-1) });
  if (i < S.sources.length - 1) acts.push({ icon: 'i-down', label: 'Nach unten', run: () => move(1) });
  if (s.kind === 'playlist') acts.push({ icon: 'i-queue', label: 'Neu einlesen', run: async () => {
    toast('Wird neu eingelesen…');
    const r = await api('api/playlists/refresh', 'POST', { id: s.id });
    toast(r?.ok ? `${r.count} Titel.` : (r?.error || 'Fehlgeschlagen.'), !r?.ok); loadSources();
  } });
  acts.push({ icon: 'i-x', label: 'Entfernen', danger: true, run: async () => {
    const r = await api('api/user/sources/remove', 'POST', { key: s.key }); if (r) { S.sources = r; renderSources(); renderLibrary(); renderMix(); }
  } });
  sheet(sheetHead(art(s.thumbnail, s.display, s.kind), s.display,
    `${s.kind === 'playlist' ? 'Playlist' : 'Sender'} · ${(s.count || 0).toLocaleString('de')} Titel${s.enabled ? '' : ' · pausiert'}`), acts);
}

// ── Suche ─────────────────────────────────────────────────────────────────────
function searchFor(q) {
  S.tab = 'search'; history.replaceState(null, '', '#search'); route();
  $('search-input').value = q; $('search-clear').hidden = !q;
  runSearch();
}
async function runSearch() {
  const q = $('search-input').value.trim();
  if (!q || S.searchKind === 'stations') { S.searchRes = null; renderSearch(); return; }
  S.searching = true; renderSearch();
  const kind = S.searchKind === 'both' ? 'both' : S.searchKind;
  const r = await api('api/search?q=' + encodeURIComponent(q) + '&kind=' + kind);
  S.searching = false;
  S.searchRes = r || { tracks: [], playlists: [], failed: true };
  renderSearch();
}
function matchingStations(q) {
  const n = q.toLowerCase();
  return S.stations.filter(s => s.station.toLowerCase().includes(n) || stationName(s.station).toLowerCase().includes(n)).slice(0, 30);
}
function stationRow(st) {
  const src = S.sources.find(s => s.kind === 'station' && s.name === st.station);
  return `<div class="row${src && src.enabled ? ' active' : ''}">${art('', stationName(st.station), 'station')}
    <div class="txt"><div class="t">${esc(stationName(st.station))}</div><div class="s">Sender · ${(st.song_count || 0).toLocaleString('de')} Titel</div></div>
    <div class="acts"><button class="icon-btn" data-addst="${esc(st.station)}" aria-label="Hinzufügen">
      <svg viewBox="0 0 24 24"><use href="#${src ? 'i-check' : 'i-plus'}"/></svg></button></div></div>`;
}
function renderSearch() {
  const q = $('search-input').value.trim(), box = $('search-results');
  if (!q) {
    box.innerHTML = `<p class="section-sub" style="margin-top:4px">Suche auf YouTube nach Titeln und Playlists — oder füge einen Playlist-Link ein.
      Unter „Sender“ durchsuchst du deine Radiosender.</p>`;
    return;
  }
  let html = '';
  if (S.searchKind === 'both' || S.searchKind === 'stations') {
    const st = matchingStations(q);
    if (st.length) html += `<div class="result-group"><h3>Sender</h3>${st.slice(0, S.searchKind === 'both' ? 4 : 30).map(stationRow).join('')}</div>`;
    else if (S.searchKind === 'stations') html += '<p class="empty">Kein Sender gefunden.</p>';
  }
  if (S.searchKind !== 'stations') {
    if (S.searching) html += '<div class="loading-line"><span class="spinner" style="display:block"></span>Suche auf YouTube…</div>';
    else if (S.searchRes) {
      const { tracks = [], playlists = [] } = S.searchRes;
      if (playlists.length) html += `<div class="result-group"><h3>Playlists</h3>${playlists.map((p, i) =>
        `<button class="row" data-pl="${i}">${art(p.thumbnail, p.name, 'playlist')}<div class="txt"><div class="t">${esc(p.name)}</div>
          <div class="s">${p.direct ? 'Link erkannt — antippen zum Hinzufügen' : 'Playlist' + (p.uploader ? ' · ' + esc(p.uploader) : '') + (p.count ? ' · ' + p.count + ' Titel' : '')}</div></div></button>`).join('')}</div>`;
      if (tracks.length) html += `<div class="result-group"><h3>Titel</h3>${tracks.map((t, i) =>
        `<button class="row" data-tr="${i}">${art(t.thumbnail, t.song, 'track')}<div class="txt"><div class="t">${esc(t.song)}</div>
          <div class="s">${esc(t.artist)}${t.duration ? ' · ' + fmt(t.duration) : ''}</div></div></button>`).join('')}</div>`;
      if (!tracks.length && !playlists.length) html += `<p class="empty">${S.searchRes.failed ? 'Suche fehlgeschlagen — Netz prüfen, Details im Protokoll.' : 'Keine Treffer.'}</p>`;
    } else html += '<p class="empty">Mit Enter auf YouTube suchen.</p>';
  }
  box.innerHTML = html;
}
$('search-results').addEventListener('click', e => {
  const tr = e.target.closest('[data-tr]'), pl = e.target.closest('[data-pl]'), st = e.target.closest('[data-addst]');
  if (tr) trackSheet(S.searchRes.tracks[+tr.dataset.tr]);
  if (pl) playlistSheet(S.searchRes.playlists[+pl.dataset.pl]);
  if (st) toggleStation(st.dataset.addst);
});
$('search-form').addEventListener('submit', e => { e.preventDefault(); $('search-input').blur(); runSearch(); });
$('search-input').addEventListener('input', () => {
  $('search-clear').hidden = !$('search-input').value;
  S.searchRes = null; renderSearch();
});
$('search-clear').addEventListener('click', () => { $('search-input').value = ''; $('search-clear').hidden = true; S.searchRes = null; renderSearch(); $('search-input').focus(); });
$('search-chips').addEventListener('click', e => {
  const c = e.target.closest('[data-kind]'); if (!c) return;
  S.searchKind = c.dataset.kind;
  for (const x of $('search-chips').children) x.classList.toggle('on', x === c);
  if (S.searchKind === 'stations' && !S.stations.length) loadStations();
  if ($('search-input').value.trim() && S.searchKind !== 'stations') runSearch(); else renderSearch();
});

// ── Bibliothek ────────────────────────────────────────────────────────────────
function toggleStation(name) {
  const has = S.sources.find(s => s.kind === 'station' && s.name === name);
  const list = has ? S.sources.filter(s => s !== has)
                   : [...S.sources, { key: name, kind: 'station', name, enabled: true, display: stationName(name), count: (S.stations.find(x => x.station === name) || {}).song_count || 0 }];
  toast(has ? `${stationName(name)} entfernt.` : `${stationName(name)} hinzugefügt.`);
  saveSources(list).then(() => { renderSearch(); if (!has) setTimeout(loadSources, 400); });
}
function renderLibrary() {
  const f = S.libFilter;
  const list = S.sources.filter(s => f === 'all' || s.kind === f);
  $('lib-sources').innerHTML = list.length ? list.map(s => {
    const i = S.sources.indexOf(s);
    return `<div class="row${s.enabled ? '' : ' off'}" data-i="${i}">
      ${art(s.thumbnail, s.display, s.kind)}
      <div class="txt" data-more="${i}"><div class="t">${esc(s.display)}</div>
        <div class="s">${s.kind === 'playlist' ? 'Playlist' : 'Sender'} · ${s.loading ? 'wird geladen …' : (s.count || 0).toLocaleString('de') + ' Titel'}</div></div>
      <div class="acts">
        <label class="switch"><input type="checkbox" data-toggle="${i}" ${s.enabled ? 'checked' : ''}><i></i></label>
        <button class="icon-btn" data-more="${i}" aria-label="Mehr"><svg viewBox="0 0 24 24"><use href="#i-more"/></svg></button>
      </div></div>`;
  }).join('') : `<p class="empty">${f === 'playlist' ? 'Noch keine Playlist — über „+“ oder die Suche hinzufügen.' : 'Noch keine Quelle — unten Sender hinzufügen.'}</p>`;

  const q = S.stationFilter.toLowerCase();
  const inSrc = new Set(S.sources.filter(s => s.kind === 'station').map(s => s.name));
  const avail = S.stations.filter(s => !inSrc.has(s.station) && (!q || stationName(s.station).toLowerCase().includes(q) || s.station.toLowerCase().includes(q)))
    .sort((a, b) => stationName(a.station).localeCompare(stationName(b.station), 'de'));
  $('lib-stations').innerHTML = (f === 'playlist') ? '' : (avail.length ? avail.slice(0, 80).map(stationRow).join('')
    + (avail.length > 80 ? `<p class="empty">… und ${avail.length - 80} weitere — Filter benutzen.</p>` : '')
    : `<p class="empty">${S.stations.length ? 'Alle passenden Sender sind schon Quellen.' : 'Senderliste wird geladen… (Station-API in den Einstellungen)'}</p>`);
}
$('lib-sources').addEventListener('click', e => {
  const m = e.target.closest('[data-more]');
  if (m) sourceSheet(S.sources[+m.dataset.more]);
});
$('lib-sources').addEventListener('change', e => {
  const t = e.target.closest('[data-toggle]'); if (!t) return;
  const i = +t.dataset.toggle;
  saveSources(S.sources.map((s, k) => k === i ? { ...s, enabled: t.checked } : s));
});
$('lib-stations').addEventListener('click', e => { const b = e.target.closest('[data-addst]'); if (b) toggleStation(b.dataset.addst); });
$('station-filter').addEventListener('input', e => { S.stationFilter = e.target.value; renderLibrary(); });
$('lib-chips').addEventListener('click', e => {
  const c = e.target.closest('[data-f]'); if (!c) return;
  S.libFilter = c.dataset.f;
  for (const x of $('lib-chips').children) x.classList.toggle('on', x === c);
  renderLibrary();
});
$('lib-add').addEventListener('click', () => {
  sheet('<div><div class="t">Playlist hinzufügen</div><div class="s">YouTube- oder YouTube-Music-Link bzw. Playlist-ID</div></div>', [
    { html: '<input type="url" id="pl-url" placeholder="https://www.youtube.com/playlist?list=…">' },
    { icon: 'i-plus', label: 'Hinzufügen', run: () => {
      const v = (window._plUrl || '').trim();
      if (!v) { toast('Bitte einen Link eingeben.', true); return; }
      addPlaylist({ id: v, name: 'Playlist' }, false);
    } },
    { icon: 'i-search', label: 'Stattdessen auf YouTube suchen', run: () => { S.searchKind = 'playlists';
      for (const x of $('search-chips').children) x.classList.toggle('on', x.dataset.kind === 'playlists'); searchFor(''); } },
  ]);
  const inp = $('pl-url');
  window._plUrl = '';
  inp.addEventListener('input', () => window._plUrl = inp.value);
  setTimeout(() => inp.focus(), 250);
});

// ── Warteschlange ─────────────────────────────────────────────────────────────
function qRow(q, current = false) {
  const tag = q.source_kind === 'manual' ? '<span class="tag manual">Von Hand</span>' : (q.source_name ? `<span class="tag">${esc(q.source_name)}</span>` : '');
  return `<div class="row${current ? ' active' : ''}">${art(songArt(q), q.song, 'track')}
    <div class="txt" ${current ? '' : `data-qm="${q.position}"`}><div class="t">${esc(q.song)}</div>
      <div class="s">${tag}${esc(q.artist)}${q.duration ? ' · ' + fmt(q.duration) : ''}</div></div>
    ${current ? '' : `<div class="acts">
      ${q.position > 0 ? `<button class="icon-btn" data-qup="${q.position}" aria-label="Nach oben"><svg viewBox="0 0 24 24"><use href="#i-up"/></svg></button>` : ''}
      <button class="icon-btn" data-qrm="${q.position}" aria-label="Entfernen"><svg viewBox="0 0 24 24"><use href="#i-x"/></svg></button></div>`}
  </div>`;
}
function renderQueue() {
  if ($('queue').hidden) return;
  const q = S.queue || {};
  $('q-current').innerHTML = q.current ? qRow(q.current, true) : '<p class="empty">Gerade läuft nichts.</p>';
  $('q-list').innerHTML = (q.upcoming || []).length ? q.upcoming.map(x => qRow(x)).join('') : '<p class="empty">Leer — wird beim Abspielen aus den Quellen nachgefüllt.</p>';
}
function queueSheet(q) {
  if (!q) return;
  sheet(sheetHead(art(songArt(q), q.song, 'track'), q.song, q.artist), [
    ...(q.position > 0 ? [{ icon: 'i-next', label: 'Als Nächstes spielen', run: () => qAct('move', { position: q.position, to: 0 }) }] : []),
    { icon: 'i-x', label: 'Aus der Warteschlange entfernen', danger: true, run: () => qAct('remove', { position: q.position }) },
  ]);
}
async function qAct(kind, body) {
  const r = await api('api/user/queue/' + kind, 'POST', body);
  if (!r) toast('Nicht möglich.', true);
  await loadQueue(); pollNow();
}
$('q-list').addEventListener('click', e => {
  const up = e.target.closest('[data-qup]'), rm = e.target.closest('[data-qrm]'), m = e.target.closest('[data-qm]');
  if (up) qAct('move', { position: +up.dataset.qup, to: +up.dataset.qup - 1 });
  else if (rm) qAct('remove', { position: +rm.dataset.qrm });
  else if (m) queueSheet(S.queue.upcoming[+m.dataset.qm]);
});
$('q-clear').addEventListener('click', async () => {
  const r = await api('api/user/queue/clear', 'POST');
  toast(r ? 'Warteschlange geleert — wird aus den Quellen nachgefüllt.' : 'Nicht möglich.', !r);
  loadQueue();
});

// ── Einstellungen ─────────────────────────────────────────────────────────────
// Die Konfiguration gehört im Addon dem Supervisor: sie wird angezeigt,
// geändert wird sie in den Addon-Optionen. Änderbar ist hier der Farbstil,
// dazu Player und Lautstärke, die zur Laufzeit gehören.
const CFG_LABELS = [
  ['station_api_url',    'Station-API'],
  ['no_repeat_hours',    'Keine Wiederholung (h)'],
  ['min_repeat_hours',   'Mindestsperre (h)'],
  ['max_song_minutes',   'Max. Titellänge (min)'],
  ['target_loudness',    'Ziel-Lautheit (LUFS)'],
  ['normalize_loudness', 'Lautheit angleichen'],
  ['trim_silence',       'Stille abschneiden'],
  ['icy_metadata',       'ICY-Titelanzeige'],
  ['webdav_url',         'WebDAV-Abgleich'],
  ['media_content_type', 'Medientyp'],
];
const cfgValue = v =>
  v === true ? 'an' : v === false ? 'aus' :
  v === '' || v === null || v === undefined ? '—' : String(v);

async function loadSettings() {
  const s = await api('api/settings');
  if (!s) return;
  S.settings = s;
  const ro = s.read_only || {};
  $('config-stats').innerHTML = CFG_LABELS
    .map(([k, label]) => `<div class="stat"><b>${esc(cfgValue(ro[k]))}</b><span>${esc(label)}</span></div>`)
    .join('');
}
$('btn-reset-cursors').addEventListener('click', async () => {
  const r = await api('api/debug/reset_cursors', 'POST');
  toast(r ? 'Senderverlauf wird neu geladen…' : 'Nicht möglich.', !r);
});

// ── Player und Lautstärke (gibt es nur im Addon) ──────────────────────────────
let _volTouched = 0;
async function loadPlayers() {
  const [players, state] = await Promise.all([
    api('api/user/players'), api('api/user/state'),
  ]);
  if (!players) return;
  const cur = state?.player_entity_id || '';
  $('player-select').innerHTML = '<option value="">— Player wählen —</option>' +
    players.map(p => `<option value="${esc(p.entity_id)}"${p.entity_id === cur ? ' selected' : ''}>${esc(p.name)}</option>`).join('');
  if (Date.now() - _volTouched > 3000) {
    const vol = typeof S.now?.volume === 'number' ? S.now.volume : (state?.volume ?? 0.7);
    $('volume-slider').value = Math.round(vol * 100);
  }
}
$('player-select').addEventListener('change', async e => {
  const r = await api('api/user/state', 'POST', { player_entity_id: e.target.value });
  toast(r ? 'Player gespeichert.' : 'Player konnte nicht gespeichert werden.', !r);
});
let _volTimer = null;
$('volume-slider').addEventListener('input', e => {
  _volTouched = Date.now();
  const v = +e.target.value;
  clearTimeout(_volTimer);
  _volTimer = setTimeout(() => api('api/user/state', 'POST', { volume: v / 100 }), 400);
});

// ── Stream-URLs für externe Geräte ────────────────────────────────────────────
const URL_ROWS = [
  ['device_mp3', 'Internetradio (direkt)'],
  ['device_m3u', 'Playlist (M3U)'],
  ['device_pls', 'Playlist (PLS)'],
  ['browser',    'Browser / VLC zum Testen'],
];
let _urlsLoaded = false;
async function loadUrls() {
  if (_urlsLoaded) return;
  const u = await api('api/user/stream_urls');
  if (!u) return;
  _urlsLoaded = true;
  $('urls-list').innerHTML = URL_ROWS.filter(([k]) => u[k]).map(([k, label]) =>
    `<div class="url-row"><div class="url-info"><div class="url-label">${esc(label)}</div>
      <div class="url-value">${esc(u[k])}</div></div>
      <button class="btn-outline" type="button" data-copy="${esc(u[k])}">Kopieren</button></div>`).join('');
}
$('urls-list').addEventListener('click', async e => {
  const b = e.target.closest('[data-copy]'); if (!b) return;
  const text = b.dataset.copy;
  let ok = false;
  try { await navigator.clipboard.writeText(text); ok = true; }
  catch {
    const ta = document.createElement('textarea');
    ta.value = text; ta.style.cssText = 'position:fixed;opacity:0';
    document.body.append(ta); ta.select();
    try { ok = document.execCommand('copy'); } catch {}
    ta.remove();
  }
  b.textContent = ok ? 'Kopiert' : 'Fehlgeschlagen';
  setTimeout(() => b.textContent = 'Kopieren', 1800);
  if (!ok) toast('Kopieren nicht möglich — URL bitte markieren.', true);
});

function when(ts) {
  if (!ts) return 'nie';
  const m = Math.round((Date.now() - ts * 1000) / 60000);
  return m < 1 ? 'gerade eben' : m < 60 ? `vor ${m} min` : m < 1440 ? `vor ${Math.round(m / 60)} h` : new Date(ts * 1000).toLocaleDateString('de');
}
async function loadListened() {
  const d = await api('api/user/listened');
  if (!d) return;
  $('listened-count').textContent = (d.count || 0).toLocaleString('de');
  const sync = d.sync || {};
  const items = [[(d.count || 0).toLocaleString('de'), 'Titel gemerkt', ''], [(d.plays || 0).toLocaleString('de'), 'Wiedergaben', ''],
                 [when(d.last_at), 'zuletzt gehört', '']];
  items.push(sync.configured ? [sync.at ? when(sync.at) : 'noch nicht', sync.error ? 'Abgleich fehlgeschlagen' : `WebDAV (alle ${sync.interval} min)`,
                                sync.ok === false ? 'warn' : sync.ok ? 'ok' : ''] : ['aus', 'WebDAV nicht eingerichtet', '']);
  $('listened-stats').innerHTML = items.map(([v, l, c]) => `<div class="stat ${c}"><b>${esc(v)}</b><span>${esc(l)}</span></div>`).join('');
  $('btn-sync').disabled = !sync.configured;
}
$('btn-sync').addEventListener('click', async () => {
  const b = $('btn-sync'); b.disabled = true; b.textContent = 'Gleiche ab…';
  const r = await api('api/user/listened/sync', 'POST');
  b.textContent = 'Jetzt abgleichen';
  toast(r ? `Abgeglichen — ${r.added || 0} neu, ${r.count || 0} insgesamt.` : 'Abgleich fehlgeschlagen — siehe Protokoll.', !r);
  loadListened();
});
$('btn-export').addEventListener('click', () => {
  const a = document.createElement('a');
  a.href = 'api/user/listened/export';
  a.download = 'personal-radio-gehoert.json';
  document.body.append(a); a.click(); a.remove();
});
$('btn-import').addEventListener('click', () => $('import-file').click());
$('import-file').addEventListener('change', async e => {
  const f = e.target.files?.[0]; e.target.value = '';
  if (!f) return;
  let payload;
  try { payload = JSON.parse(await f.text()); }
  catch { toast('Datei ist kein gültiges JSON.', true); return; }
  const r = await api('api/user/listened/import', 'POST', payload);
  toast(r ? `Importiert — ${r.added || 0} neu, ${r.count || 0} insgesamt.`
          : 'Import fehlgeschlagen.', !r);
  loadListened();
});
$('log-card').addEventListener('toggle', () => { if ($('log-card').open) loadLog(); });
$('btn-log-refresh').addEventListener('click', loadLog);
async function loadLog() {
  const r = await api('api/log');
  if (r) { $('log-view').textContent = r.log || '(leer)'; $('log-view').scrollTop = 1e9; }
}

// ── Start ─────────────────────────────────────────────────────────────────────
async function init() {
  greeting();
  const s = await api('api/settings');
  if (s) {
    S.settings = s;
    theme.accent = s.theme_accent || 'green';
    theme.bg = s.theme_background || 'dark';
  }
  applyTheme();
  if (!location.hash || location.hash === '#sheet') history.replaceState(null, '', '#home');
  route();
  await refreshAll();
  loadStations();
  loadSugg();
  if (s && !s.configured && !S.sources.length) go('settings');

  setInterval(() => { if (!document.hidden) pollNow(); }, 1000);
  setInterval(() => { if (!document.hidden) { loadSources(); greeting(); } }, 15000);
  setInterval(() => { if (!document.hidden && (S.tab === 'library' || !S.stations.length)) loadStations(); }, 20000);
  // Fortschritt der Senderaktualisierung in der Bibliothek
  let wasPolling = false;
  setInterval(async () => {
    if (document.hidden || S.tab !== 'library') return;
    const st = await api('api/status');
    const busy = !!st?.polling;
    const el = $('lib-progress');
    el.hidden = !busy;
    if (busy) el.textContent = st.progress_total ? `Sender werden aktualisiert … ${st.progress_done} / ${st.progress_total}` : 'Senderliste wird abgerufen …';
    if (wasPolling && !busy) { loadStations(); loadSources(); }
    wasPolling = busy;
  }, 1500);
}
init();
