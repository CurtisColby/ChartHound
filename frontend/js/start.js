'use strict';
// ══════════════════════════════════════════════════════════════════════════════
//  START HERE — guided flow + text size
// ══════════════════════════════════════════════════════════════════════════════

function stTextSize(px) {
  px = [16, 18, 20].includes(+px) ? +px : 18;
  document.documentElement.style.fontSize = px + 'px';
  try { localStorage.setItem('ch_textsize', String(px)); } catch {}
  document.querySelectorAll('.st-ts button').forEach(b => b.classList.toggle('on', +b.dataset.ts === px));
}
(function () {
  let px = 18;
  try { px = +(localStorage.getItem('ch_textsize') || 18); } catch {}
  stTextSize(px);
  document.addEventListener('DOMContentLoaded', () => stTextSize(px));
})();

// "YYYY-MM-DD HH:MM:SS" (UTC from SQLite) → "Oct 24"
function stDate(s) {
  if (!s) return '';
  const d = new Date(String(s).replace(' ', 'T') + (/[zZ+]/.test(String(s).slice(10)) ? '' : 'Z'));
  if (isNaN(d)) return String(s).slice(0, 10);
  const opts = {month: 'short', day: 'numeric'};
  if (d.getFullYear() !== new Date().getFullYear()) opts.year = 'numeric';
  return d.toLocaleDateString(undefined, opts);
}
const stN = n => (n || 0).toLocaleString();

function stGo(tab, view) {
  if (view) window._stWantView = view;
  switchTab(tab);
  const ca = document.querySelector('.content-area'); if (ca) ca.scrollTop = 0;
}

const _ST_NAMES = {plex:'Plex', emby:'Emby', jellyfin:'Jellyfin', lastfm:'Last.fm', prowlarr:'Prowlarr',
  radarr:'Radarr', sonarr:'Sonarr', qbittorrent:'qBittorrent', sabnzbd:'SABnzbd', discogs:'Discogs',
  deluge:'Deluge', transmission:'Transmission'};

function _stCard(s, isNext) {
  const icon = {done:'✅', todo:'⚠️', optional:'○', off:'○'}[s.state] || '○';
  const btns = s.go.map(g => `<button class="st-go" onclick="stGo('${g[1]}'${g[2] ? `,'${g[2]}'` : ''})">${g[0]}</button>`).join('');
  return `<div class="st-card st-${s.state}${isNext ? ' st-next' : ''}">
    <div class="st-num">${s.n}</div>
    <div class="st-body">
      <div class="st-card-title">${s.title}${s.optional ? ' <span class="st-opt">OPTIONAL</span>' : ''}${isNext ? ' <span class="st-next-badge">▶ DO THIS NEXT</span>' : ''}</div>
      <div class="st-what">${s.what}</div>
      <div class="st-state">${icon} ${s.status}</div>
    </div>
    <div class="st-btns">${btns}</div>
  </div>`;
}

async function stLoad() {
  const box = document.getElementById('st-steps'); if (!box) return;
  let d;
  try {
    const r = await fetch(`${API}/api/start/status`, {headers: authHdrs()});
    if (!r.ok) throw new Error(r.status);
    d = await r.json();
  } catch (e) {
    box.innerHTML = `<div class="st-loading">Couldn’t load your progress (${esc(String(e.message || e))}). The steps still work — use the GO buttons in the sidebar.</div>`;
    return;
  }
  const c = d.connect, sc = d.scan, g = d.genres, p = d.playlists, gp = d.gaps, t = d.tracker;
  const names = (c.verified || []).map(s => _ST_NAMES[s] || s);
  const steps = [
    { n: 1, title: 'CONNECT YOUR SERVICES', go: [['GO → THE KENNEL', 'kennel']],
      what: 'Tell ChartHound where your media server (Plex, Emby or Jellyfin), chart sources and downloaders are. Save, then TEST each one.',
      state: c.media.length ? 'done' : 'todo',
      status: c.media.length ? `Connected: ${names.join(', ')}` : 'Connect at least one media server (Plex, Emby or Jellyfin).' },
    { n: 2, title: 'SCAN YOUR LIBRARY', go: [['GO → ① SCAN', 'groomer', 'scan']],
      what: 'Reads your song list from your media server and gives every song a Hit Score (0–100) from its chart history. Re-run after adding music — it only adds what’s new.',
      state: sc.tracks ? 'done' : 'todo',
      status: sc.tracks ? `${stN(sc.tracks)} songs · ${stN(sc.charted)} charted${sc.newest ? ' · newest added ' + stDate(sc.newest) : ''}`
                        : 'Not scanned yet — open Top Dogs and click ① SCAN.' },
    { n: 3, title: 'TAG GENRES', go: [['GO → THE RETRIEVER', 'retriever']],
      what: 'Looks up each album’s genre online and writes it into your music files: click ▶ RUN ENGINE, then REVIEW &amp; WRITE. Hand-edited songs are never overwritten.',
      state: !g.decided || g.pending ? 'todo' : 'done',
      status: !g.decided ? 'Not run yet.'
        : (g.pending ? `${stN(g.pending)} songs have no genre yet — run the engine. ` : 'All songs have a genre. ')
          + `${stN(g.decided)} done${g.last_run ? ', last run ' + stDate(g.last_run) : ''}${g.conflicts ? ` · ${stN(g.conflicts)} to review (CONFLICT)` : ''}` },
    { n: 4, title: 'BUILD ROAD-TRIP PLAYLISTS', go: [['GO → ② BUILD', 'groomer', 'build']],
      what: 'Pick decades, genres and how many hours. Send the playlist to Plex, Emby or Jellyfin, or download an M3U for Navidrome.',
      state: p.last ? 'done' : 'todo',
      status: p.last ? `Last: “${esc(p.last.name || 'playlist')}” · ${stN(p.last.count)} songs → ${esc(String(p.last.dest || '').toUpperCase())} · ${stDate(p.last.at)}`
                     : 'No playlist sent yet (counted from this update on).' },
    { n: 5, title: 'FILL THE GAPS', optional: true, go: [['SONGS → SNIFFER', 'sniffer'], ['ALBUMS → BLOODHOUND', 'bloodhound']],
      what: 'Finds big hits and essential albums you don’t own and sends them to your downloader. Then follow “After new music arrives” below.',
      state: gp.prowlarr && gp.downloader ? 'optional' : 'off',
      status: gp.prowlarr && gp.downloader ? `Ready${gp.path ? ' · downloads go to ' + esc(gp.path) : ''}`
                                           : 'Needs Prowlarr and a download client connected in The Kennel.' },
    { n: 6, title: 'MOVIES & TV (TRACKER)', optional: true, go: [['GO → THE TRACKER', 'tracker']],
      what: 'Slowly searches for missing movies and episodes in Radarr and Sonarr, newest first, without hammering your indexers.',
      state: t.enabled ? 'done' : 'off',
      status: t.enabled ? `On · ${stN(t.missing)} missing${t.last_grab ? ' · last grab ' + stDate(t.last_grab) : ''}`
                        : (t.radarr || t.sonarr ? 'Off — turn it on in The Tracker if you want it.' : 'Needs Radarr or Sonarr connected in The Kennel.') },
  ];
  const next = steps.find(s => !s.optional && s.state === 'todo');
  box.innerHTML = steps.map(s => _stCard(s, s === next)).join('');
}
