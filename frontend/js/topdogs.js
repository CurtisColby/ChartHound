'use strict';
// ══════════════════════════════════════════════════════════════════
//  TOP DOGS — JavaScript
// ══════════════════════════════════════════════════════════════════

let grmResults       = [];
let _grmHasScanned   = false;
let grmSelected      = new Set();
let grmScanJobId     = null;
let grmScanPoll      = null;
let grmScannedServer = null;   // server used in last scan — locks push buttons
let grmCurrentMode   = 'server';
let grmLivePoll      = null;   // auto-refresh results during scan
let grmBrowsePath    = '';     // current relative path being browsed (under /music)
let grmBrowseBase    = '/music'; // absolute base returned by /browse

// ── Local folder picker ──────────────────────────────────────────
// Reuses the Retriever's /browse endpoint (it just lists subfolders of /music).
// Dropdown shows immediate subfolders; selecting one fills the text field.
// ⟳ button drills into the currently selected folder.
async function grmBrowseLoad(relPath) {
  const sel  = document.getElementById('grm-local-browse');
  const hint = document.getElementById('grm-local-hint');
  if (!sel || !hint) return;
  try {
    const r = await fetch(`${API}/api/retriever/browse?path=${encodeURIComponent(relPath || '')}`, {headers:authHdrs()});
    if (!r.ok) {
      hint.textContent = '⚠ Could not browse /music — is the volume mounted?';
      toast('Browse failed — see hint', 'warn');
      return;
    }
    const data = await r.json();
    grmBrowsePath = relPath || '';
    grmBrowseBase = data.base || '/music';

    sel.innerHTML = '<option value="">— select a folder —</option>';

    // Add "go up" option when not at root
    if (grmBrowsePath) {
      const up = document.createElement('option');
      up.value = '__up__'; up.textContent = '⬆ ../ (go up)';
      sel.appendChild(up);
    }

    (data.entries || []).forEach(entry => {
      const opt = document.createElement('option');
      opt.value = entry.path;        // absolute path
      opt.textContent = entry.name;
      opt.dataset.rel = entry.rel_path;
      sel.appendChild(opt);
    });

    hint.textContent = `📂 ${data.path} — ${(data.entries || []).length} folder(s). Select one, then ⟳ to drill in.`;
  } catch(e) {
    hint.textContent = `⚠ Browse error: ${e.message}`;
  }
}

function grmBrowseSelect() {
  // When user picks from dropdown: auto-fill text field so it's ready to scan as-is.
  const sel = document.getElementById('grm-local-browse');
  const txt = document.getElementById('grm-local-path');
  if (!sel || !txt) return;
  if (sel.value === '__up__' || sel.value === '') return;
  txt.value = sel.value;
}

async function grmBrowseRefresh() {
  // ⟳ button: drill into selected folder, or (if none selected) re-list current path.
  const sel = document.getElementById('grm-local-browse');
  if (!sel) return;
  if (sel.value === '__up__') {
    const parentRel = grmBrowsePath.split('/').slice(0, -1).join('/');
    await grmBrowseLoad(parentRel);
    return;
  }
  if (sel.value) {
    // Selected folder's rel_path is on the option dataset
    const opt = sel.options[sel.selectedIndex];
    const rel = opt?.dataset?.rel || '';
    await grmBrowseLoad(rel);
    return;
  }
  // Nothing selected — refresh current level
  await grmBrowseLoad(grmBrowsePath);
}

// ── Init ──────────────────────────────────────────────────────────
function grmInit() {
  // Build genre tree
  grmBuildTree();
  // Mood pills
  const moods = ['Energetic','Intense','Upbeat','Happy','Chill','Peaceful',
                 'Romantic','Soulful','Melancholic','Dark','Epic','Nostalgic',
                 'Uplifting','Joyful'];
  const pw = document.getElementById('grm-mood-pills');
  if (pw && !pw.children.length) {
    pw.innerHTML = moods.map(m =>
      `<span onclick="grmToggleMood('${m}')" data-mood="${m}" data-active="0"
        style="font-family:var(--mono);font-size:.57rem;padding:.18rem .5rem;
        border-radius:20px;border:1px solid var(--border);background:var(--bg);
        color:var(--muted);cursor:pointer;transition:all .15s">${m}</span>`
    ).join('');
  }
  // Check if any tracks have been Retriever-tagged — show genre/mood only if so
  // Inject scroll nav buttons if not already present
  grmInjectScrollNav();
  grmLoadStats();
  _grmLoadPathSettings();
  grmLoadLibraries();
  if (!grmScanPoll || window._stWantView) grmInitView();
  grmLfmPoll();
}

// Genre/mood pills replaced by post-scan dropdown (grmBuildGenreDropdown)

function retInjectScrollNav() {
  if (document.getElementById('ret-scroll-nav')) return;
  const scroller = document.querySelector('.content-area') || document.documentElement;
  const nav = document.createElement('div');
  nav.id = 'ret-scroll-nav';
  nav.style.cssText = 'position:fixed;right:1.5rem;bottom:5rem;z-index:500;display:flex;flex-direction:column;gap:.4rem;opacity:0;transition:opacity .3s;pointer-events:none;';
  const btnStyle = 'width:36px;height:36px;border-radius:50%;background:var(--surface2);border:1px solid var(--border2);color:var(--teal);font-size:1rem;cursor:pointer;display:flex;align-items:center;justify-content:center;transition:all .15s;line-height:1;';
  nav.innerHTML = `
    <button style="${btnStyle}" title="Scroll to top"
      onclick="(document.querySelector('.content-area')||document.documentElement).scrollTo({top:0,behavior:'smooth'})">↑</button>
    <button style="${btnStyle}" title="Scroll to bottom"
      onclick="(document.querySelector('.content-area')||document.documentElement).scrollTo({top:99999,behavior:'smooth'})">↓</button>`;
  document.body.appendChild(nav);
  scroller.addEventListener('scroll', () => {
    const panel = document.getElementById('panel-retriever');
    const tabActive = panel && panel.classList.contains('active');
    const show = tabActive && scroller.scrollTop > 200;
    nav.style.opacity       = show ? '1'    : '0';
    nav.style.pointerEvents = show ? 'auto' : 'none';
  }, {passive:true});
}

function grmInjectScrollNav() {
  if (document.getElementById('grm-scroll-nav')) return;
  // The groomer panel scrolls inside .content-area, not window
  const scroller = document.querySelector('.content-area') || document.documentElement;
  const nav = document.createElement('div');
  nav.id = 'grm-scroll-nav';
  nav.style.cssText = 'position:fixed;right:1.5rem;bottom:5rem;z-index:500;display:flex;flex-direction:column;gap:.4rem;opacity:0;transition:opacity .3s;pointer-events:none;';
  const btnStyle = 'width:36px;height:36px;border-radius:50%;background:var(--surface2);border:1px solid var(--border2);color:var(--pink);font-size:1rem;cursor:pointer;display:flex;align-items:center;justify-content:center;transition:all .15s;line-height:1;';
  nav.innerHTML = `
    <button style="${btnStyle}" title="Scroll to top"
      onclick="(document.querySelector('.content-area')||document.documentElement).scrollTo({top:0,behavior:'smooth'})">↑</button>
    <button style="${btnStyle}" title="Scroll to push bar"
      onclick="(document.querySelector('.content-area')||document.documentElement).scrollTo({top:99999,behavior:'smooth'})">↓</button>`;
  document.body.appendChild(nav);
  scroller.addEventListener('scroll', () => {
    const panel = document.getElementById('panel-groomer');
    const tabActive = panel && panel.classList.contains('active');
    const show = tabActive && scroller.scrollTop > 200;
    nav.style.opacity       = show ? '1'    : '0';
    nav.style.pointerEvents = show ? 'auto' : 'none';
  }, {passive:true});
}

// Cached path settings for clipboard translation
let _grmServerPrefix = '';
let _grmDockerPrefix = '/music';
async function _grmLoadPathSettings() {
  if (_grmServerPrefix !== '' || _grmDockerPrefix !== '/music') return; // already loaded
  try {
    const r = await fetch(`${API}/api/kennel/path-settings`, {headers:authHdrs()});
    if (r.ok) {
      const d = await r.json();
      _grmServerPrefix = d.server_prefix || '';
      _grmDockerPrefix = d.docker_prefix || '/music';
    }
  } catch {}
}

function grmCopyPath(path, el) {
  if (!path) return;
  // Translate stored path → desktop path for file manager use.
  // Stored paths can be:
  //   a) Docker: /music/FULL ALBUMS/...
  //   b) Server raw: /media/NAS1/MUSIC TAGGED/FULL ALBUMS/...
  //   c) Desktop: /media/colby/NAS1/MUSIC TAGGED/FULL ALBUMS/... (already correct)
  let realPath = path;
  if (_grmServerPrefix) {
    if (path.startsWith(_grmServerPrefix)) {
      // Already the desktop path — no change
      realPath = path;
    } else if (_grmDockerPrefix && path.startsWith(_grmDockerPrefix)) {
      // Docker path → desktop path
      realPath = _grmServerPrefix + path.slice(_grmDockerPrefix.length);
    } else {
      // Server raw path — find the common anchor folder and replace
      // e.g. server_prefix = /media/colby/NAS1/MUSIC TAGGED
      //      anchor = "MUSIC TAGGED"
      //      stored = /media/NAS1/MUSIC TAGGED/FULL ALBUMS/...
      //      result = /media/colby/NAS1/MUSIC TAGGED/FULL ALBUMS/...
      const anchor = _grmServerPrefix.replace(/\/+$/, '').split('/').pop();
      if (anchor) {
        const idx = path.indexOf('/' + anchor + '/');
        if (idx >= 0) {
          realPath = _grmServerPrefix + path.slice(idx + anchor.length + 1);
        }
      }
    }
  }
  // Try modern clipboard API first; fall back to execCommand for HTTP
  const doFlash = () => {
    const orig = el.style.color;
    el.style.color = 'var(--green)';
    el.textContent = '✅';
    setTimeout(() => { el.style.color = orig; el.textContent = '📋'; }, 1500);
  };
  if (navigator.clipboard && window.isSecureContext) {
    navigator.clipboard.writeText(realPath).then(doFlash)
      .catch(() => _grmFallbackCopy(realPath, doFlash));
  } else {
    _grmFallbackCopy(realPath, doFlash);
  }
}

function _grmFallbackCopy(text, callback) {
  // HTTP-safe fallback using temporary textarea + execCommand
  const ta = document.createElement('textarea');
  ta.value = text;
  ta.style.cssText = 'position:fixed;left:-9999px;top:-9999px;opacity:0';
  document.body.appendChild(ta);
  ta.focus(); ta.select();
  try {
    const ok = document.execCommand('copy');
    if (ok && callback) callback();
    else toast('Copy failed — try accessing via HTTPS', 'warn');
  } catch {
    toast('Copy not supported on this browser/connection', 'warn');
  }
  document.body.removeChild(ta);
}

function grmToggleGenre(genre) {
  const p = document.querySelector(`[data-genre="${genre}"]`);
  if (!p) return;
  const on = p.dataset.active === '1';
  p.dataset.active    = on ? '0' : '1';
  p.style.borderColor = on ? 'var(--border)' : 'var(--teal)';
  p.style.background  = on ? 'var(--bg)' : 'rgba(45,212,191,.12)';
  p.style.color       = on ? 'var(--muted)' : 'var(--teal)';
  grmSearch();
}

function grmGetSelectedGenres() {
  return [...document.querySelectorAll('[data-genre][data-active="1"]')]
    .map(p => p.dataset.genre);
}

function grmSetMode(mode) {
  grmCurrentMode = mode;
  const isServer = mode === 'server';
  document.getElementById('grm-server-section').style.display = isServer ? '' : 'none';
  document.getElementById('grm-local-section').style.display  = isServer ? 'none' : '';
  document.getElementById('grm-mode-server').style.background = isServer ? 'var(--pink)' : 'var(--surface2)';
  document.getElementById('grm-mode-server').style.color      = isServer ? '#000' : 'var(--muted)';
  document.getElementById('grm-mode-server').style.border     = isServer ? 'none' : '1px solid var(--border)';
  document.getElementById('grm-mode-local').style.background  = isServer ? 'var(--surface2)' : 'var(--teal)';
  document.getElementById('grm-mode-local').style.color       = isServer ? 'var(--muted)' : '#000';
  document.getElementById('grm-mode-local').style.border      = isServer ? '1px solid var(--border)' : 'none';
  // Hide server push buttons in local mode
  ['grm-push-plex','grm-push-emby','grm-push-jf'].forEach(id => {
    const b = document.getElementById(id);
    if (b) b.style.display = isServer ? '' : 'none';
  });
  // Auto-load root folder list first time Local mode is shown
  if (!isServer) {
    const sel = document.getElementById('grm-local-browse');
    if (sel && sel.options.length <= 1) grmBrowseLoad('');
  }
  grmSyncBar();
}

async function grmLoadStats() {
  grmLibraryHits();
  try {
    const r = await fetch(`${API}/api/groomer/db_stats`, {headers:authHdrs()});
    if (!r.ok) return;
    const d = await r.json();
    const staticEntries = d.static_entries || 0;
    const extrasEntries = d.extras_entries || 0;
    const totalEntries  = d.total_entries  || (staticEntries + extrasEntries);

    const rc = document.getElementById('grm-ref-count');
    if (rc) rc.textContent = staticEntries.toLocaleString();

    // Extras row + labels — show only if user has imported anything
    const extrasRow   = document.getElementById('grm-ref-extras-row');
    const extrasCount = document.getElementById('grm-ref-extras-count');
    const tipEl       = document.getElementById('grm-ref-tip');
    const staticLabel = document.querySelector('#grm-ref-static-row span:first-child');
    if (extrasEntries > 0) {
      if (extrasRow) extrasRow.style.display = 'flex';
      if (extrasCount) extrasCount.textContent = extrasEntries.toLocaleString();
      if (tipEl) tipEl.style.display = 'none';
      if (staticLabel) staticLabel.textContent = 'Static entries';
    } else {
      if (extrasRow) extrasRow.style.display = 'none';
      if (tipEl) tipEl.style.display = 'block';
      if (staticLabel) staticLabel.textContent = 'Reference entries';
    }

    const st = document.getElementById('grm-ref-status');
    if (st) {
      if (d.static_db_ready) {
        if (extrasEntries > 0) {
          st.textContent = '✓ Ready · ' + totalEntries.toLocaleString() + ' total chart entries';
        } else {
          st.textContent = '✓ Static DB ready · ' + staticEntries.toLocaleString() + ' chart entries';
        }
        st.style.color = 'var(--green)';
      } else {
        st.textContent = '⚠ charthound_static.db not found — check volume mount';
        st.style.color = 'var(--red)';
      }
    }
    if (d.active_job && !grmScanJobId) {
      grmScanJobId     = d.active_job.job_id;
      grmScannedServer = d.active_job.scope;
      grmStartPoll();
    }
  } catch {}
}

function grmJumpToVetSources() {
  try {
    if (typeof switchTab === 'function') switchTab('veterinarian');
    setTimeout(function() {
      var card = document.getElementById('vet-static-sources');
      if (card) {
        card.scrollIntoView({behavior:'smooth', block:'start'});
        // Brief teal flash to draw the eye
        var orig = card.style.boxShadow;
        card.style.transition = 'box-shadow .4s';
        card.style.boxShadow = '0 0 0 2px var(--teal)';
        setTimeout(function(){ card.style.boxShadow = orig || 'none'; }, 900);
      }
    }, 150);
  } catch(e) { /* silent */ }
}

// Chart status now comes from static DB via grmLoadStats — no separate load needed
async function grmLoadChartStatus() { grmLoadStats(); }
async function grmLoadCharts() { toast('Chart data ships pre-loaded in the static database. No download needed.', 'ok'); }

function grmLog(msg, type='') {
  const el = document.getElementById('grm-log');
  if (!el) return;
  const colors = {ok:'var(--green)',err:'var(--red)',warn:'var(--amber)',info:'var(--teal)'};
  const div = document.createElement('div');
  div.style.color = colors[type] || 'var(--muted)';
  div.textContent = `[${new Date().toLocaleTimeString()}] ${msg}`;
  el.appendChild(div);
  el.scrollTop = el.scrollHeight;
}

// ── Chart data loading ────────────────────────────────────────────
async function grmLoadCharts() {
  const btn = document.getElementById('grm-load-btn');
  btn.disabled = true; btn.textContent = '⏳ LOADING…';
  grmLog('Starting chart data load — scraping Billboard.com year by year 1990–present.', 'info');
  grmLog('This takes 5–15 minutes depending on Billboard server speed. App remains usable.', 'warn');
  try {
    const r = await fetch(`${API}/api/groomer/charts/load`, {
      method:'POST', headers:authHdrs(),
      body: JSON.stringify({charts:[]}),
    });
    if (!r.ok) { const d=await r.json(); throw new Error(d.detail||`HTTP ${r.status}`); }
    grmLog('Chart loader running in background…', 'ok');
    // Poll load status every 5 seconds — show elapsed time so it doesn't look frozen
    const poll = setInterval(async () => {
      try {
        const sr = await fetch(`${API}/api/groomer/charts/load/status`, {headers:authHdrs()});
        const sd = await sr.json();
        const elapsed = sd.elapsed_seconds || 0;
        const elapsedStr = elapsed > 0 ? ` ${elapsed}s` : '';
        if (sd.status === 'done') {
          clearInterval(poll);
          btn.disabled = false; btn.textContent = '🔄 LOAD CHART DATA';
          grmLog(`✓ ${sd.message}`, 'ok');
          toast('Chart data loaded!', 'success');
          grmLoadChartStatus();
        } else if (sd.status === 'error') {
          clearInterval(poll);
          btn.disabled = false; btn.textContent = '🔄 LOAD CHART DATA';
          grmLog(`✗ ${sd.message}`, 'err');
          toast('Chart load failed — check log', 'error');
        } else if (sd.status === 'running') {
          btn.textContent = `⏳ Loading…${elapsedStr}`;
          // Only log a new line every 30s so the log doesn't flood
          if (elapsed % 30 === 0 && elapsed > 0) {
            grmLog(`Still loading… ${elapsedStr} elapsed. (Scraping Billboard year by year)`, 'info');
          }
        } else if (sd.status === 'idle' && sd.loaded > 0) {
          clearInterval(poll);
          btn.disabled = false; btn.textContent = '🔄 LOAD CHART DATA';
        }
      } catch { clearInterval(poll); btn.disabled=false; btn.textContent='🔄 LOAD CHART DATA'; }
    }, 5000);
  } catch(e) {
    grmLog(`Load failed: ${e.message}`, 'err');
    btn.disabled=false; btn.textContent='🔄 LOAD CHART DATA';
  }
}

// ── Scan controls ─────────────────────────────────────────────────
async function grmLoadLibraries() {
  const server = grmGetServer();
  const sec = document.getElementById('grm-library-section');
  const sel = document.getElementById('grm-library-select');
  if (!sec || !sel) return;
  sel.innerHTML = '<option value="">All Libraries</option>';
  sec.style.display = 'none';
  try {
    const r = await fetch(`${API}/api/groomer/libraries?server=${server}`, {headers:authHdrs()});
    if (!r.ok) return;
    const d = await r.json();
    if (!d.libraries || !d.libraries.length) return;
    d.libraries.forEach(lib => {
      const opt = document.createElement('option');
      opt.value = lib.id;
      opt.textContent = lib.name + (lib.count ? ` (${lib.count})` : '');
      sel.appendChild(opt);
    });
    sec.style.display = '';
  } catch {}
}

function grmGetServer() {
  return document.querySelector('input[name="grm-server"]:checked')?.value || 'plex';
}

async function grmStartScan() {
  const mode      = grmCurrentMode;
  const server    = mode === 'server' ? grmGetServer() : 'local';
  const localPath = mode === 'local'
    ? (document.getElementById('grm-local-path')?.value.trim() || '/music') : null;
  const peakMax   = parseInt(document.getElementById('grm-peak').value);
  const peakMin   = parseInt(document.getElementById('grm-peak-min')?.value || 1);
  const weeksMin  = parseInt(document.getElementById('grm-weeks').value);
  const writeTags = document.getElementById('grm-write-tags').checked;
  const name      = document.getElementById('grm-playlist-name').value.trim() || 'Chart Hits';
  const labels    = {plex:'Plex', emby:'Emby', jellyfin:'Jellyfin', local:'Local'};
  const scope     = mode === 'server' ? labels[server] : `Local: ${localPath}`;

  grmLog(`Starting scan — ${scope} · scanning ALL chart sources · peak ≤ ${peakMax} · weeks ≥ ${weeksMin}`, 'info');
  grmLog('ℹ Scan finds ALL chart hits in your library. Use the Playlist Filters to shape results after the scan.', 'info');

  try {
    const r = await fetch(`${API}/api/groomer/scan/start`, {
      method:'POST', headers:authHdrs(),
      body: JSON.stringify({
        source:      mode === 'server' ? server : 'local',
        data_source:    'auto',
        use_estimates:  document.getElementById('grm-use-estimates')?.checked || false,
        write_tags:     writeTags,
        folder_path:    localPath,
        library_id:     document.getElementById('grm-library-select')?.value || null,
        limit:          null,
      }),
    });
    const d = await r.json();
    if (!r.ok) throw new Error(d.detail || `HTTP ${r.status}`);
    grmScanJobId     = d.job_id;
    grmScannedServer = mode === 'server' ? server : null;
    document.getElementById('grm-btn-scan').disabled  = true;
    document.getElementById('grm-btn-scan').style.opacity = '.5';
    document.getElementById('grm-scan-wrap').classList.add('bar-pulsing');
    document.getElementById('grm-btn-stop').disabled  = false;
    document.getElementById('grm-btn-stop').style.opacity = '1';
    document.getElementById('grm-status-title').textContent = `SCANNING ${scope.toUpperCase()}…`;
    grmResults = []; grmSelected.clear();
    var _tb2 = document.getElementById('grm-tbody'); if (_tb2) _tb2.innerHTML = '';
    var _bg2 = document.getElementById('grm-badge'); if (_bg2) _bg2.textContent = '0 songs';
    grmStartPoll();
    grmLivePoll = setInterval(() => { if (grmScanJobId) grmSearch(); }, 15000);
    toast(`Top Dogs scan started on ${scope}`, 'ok');
    grmLog('⏳ Scanning against static chart database — first results may take a minute to appear…', 'info');
    if (document.getElementById('grm-use-estimates')?.checked) {
      grmLog('⚠ Last.fm estimates enabled — scan will be slower (~0.2s per unmatched track)', 'warn');
    }
  } catch(e) {
    grmLog(`Failed to start: ${e.message}`, 'err');
    toast('Scan failed to start — see log', 'error');
  }
}

async function grmStopScan() {
  try {
    await fetch(`${API}/api/groomer/scan/stop`, {method:'POST', headers:authHdrs()});
    grmLog('Scan stopped.', 'warn');
    grmScanDone(false);
  } catch(e) { grmLog(`Stop error: ${e.message}`, 'err'); }
}

function grmStartPoll() {
  if (grmScanPoll) clearInterval(grmScanPoll);
  grmScanPoll = setInterval(grmPollScan, 2000);
}

async function grmPollScan() {
  if (!grmScanJobId) return;
  try {
    const r = await fetch(`${API}/api/groomer/scan/status/${grmScanJobId}`, {headers:authHdrs()});
    const d = await r.json();
    const total     = d.total     || d.total_tracks || 0;
    const processed = d.processed || 0;
    const matched   = d.matched   || 0;
    const cached    = d.cached    || 0;
    const failed    = d.failed    || 0;
    const pct = total > 0 ? Math.round((processed/total)*100) : 0;

    document.getElementById('grm-scan-bar').style.width = `${pct}%`;
    if (pct > 0) document.getElementById('grm-scan-wrap').classList.remove('bar-pulsing');
    document.getElementById('grm-s-total').textContent   = total.toLocaleString();
    document.getElementById('grm-s-scanned').textContent = processed.toLocaleString();
    document.getElementById('grm-s-hits').textContent    = matched.toLocaleString();
    document.getElementById('grm-s-cached').textContent  = cached.toLocaleString();
    document.getElementById('grm-s-failed').textContent  = failed.toLocaleString();

    if (d.status === 'running') {
      grmScanBadge(processed > 0 ? `${pct}%` : 'reading');
      const banner = document.getElementById('grm-phase-banner');
      const phTitle = document.getElementById('grm-phase-title');
      const phSub  = document.getElementById('grm-phase-sub');

      if (processed === 0 && d.message) {
        // ── PHASE 1: Tag reading (processed=0, message has progress) ──
        document.getElementById('grm-status-sub').textContent = d.message;
        if (banner) {
          banner.style.display = 'block';
          banner.style.background = 'rgba(0,180,216,.1)';
          banner.style.border = '1px solid rgba(0,180,216,.35)';
          banner.style.color = 'var(--teal)';
          phTitle.textContent = '📖 PHASE 1 — READING FILE TAGS';
          phSub.textContent = d.message + '  ·  Chart lookups begin after all tags are read. Please wait…';
        }
        // Pulse the progress bar so user sees movement
        const bar = document.getElementById('grm-scan-bar');
        if (bar && total > 0) {
          // Extract read count from message like "Reading tags: 12,400/33,390"
          const m = (d.message || '').match(/(\d[\d,]*)\s*\/\s*(\d[\d,]*)/);
          if (m) {
            const readDone = parseInt(m[1].replace(/,/g,''));
            const readTotal = parseInt(m[2].replace(/,/g,''));
            bar.style.width = Math.round((readDone/readTotal)*100) + '%';
            bar.style.background = 'var(--teal)';
          }
        }
      } else {
        // ── PHASE 2: Chart lookups (processed > 0) ──
        document.getElementById('grm-status-sub').textContent =
          `${processed.toLocaleString()} / ${total.toLocaleString()} (${pct}%)`;
        if (banner) {
          banner.style.display = 'block';
          banner.style.background = 'rgba(244,114,182,.08)';
          banner.style.border = '1px solid rgba(244,114,182,.3)';
          banner.style.color = 'var(--pink)';
          phTitle.textContent = '🔍 PHASE 2 — LOOKING UP CHART DATA';
          phSub.textContent = `Searching ${total.toLocaleString()} tracks against chart database…`;
        }
        // Restore pink progress bar for lookup phase
        const bar = document.getElementById('grm-scan-bar');
        if (bar) bar.style.background = 'var(--pink)';
      }
    }

    // Stale: server restarted or job_id no longer active — reset UI automatically
    // instead of polling forever with "Failed to fetch" errors.
    if (d.status === 'stale') {
      clearInterval(grmScanPoll); grmScanPoll = null;
      clearInterval(grmLivePoll); grmLivePoll = null;
      grmScanJobId = null;
      grmLog('⚠ Scan no longer active on server (restart or new scan started). UI reset.', 'warn');
      document.getElementById('grm-status-title').textContent = 'SCAN INACTIVE';
      document.getElementById('grm-status-sub').textContent =
        'Server no longer tracking this scan — start a new one.';
      grmScanDone(false);
      return;
    }

    if (['done','stopped','failed'].includes(d.status)) {
      clearInterval(grmScanPoll); grmScanPoll = null;
      grmScanBadge('');
      if (d.status === 'done') grmSetView('build');
      clearInterval(grmLivePoll); grmLivePoll = null;
      grmScanDone(d.status === 'done');
      if (d.status === 'done') {
        grmLog(`✓ Scan complete — ${matched.toLocaleString()} new chart hit${matched === 1 ? '' : 's'} this run`, 'ok');
        toast(`Scan done — ${matched.toLocaleString()} new chart hit${matched === 1 ? '' : 's'}`, 'success');
        document.getElementById('grm-status-title').textContent = 'SCAN COMPLETE';
        document.getElementById('grm-status-sub').textContent =
          `${matched.toLocaleString()} new chart hit${matched === 1 ? '' : 's'} this run`;
        grmLibraryHits();
        grmSearch();  // populate results table
        grmLoadStats();
        // If write tags was enabled, verify a sample of written files
        if (document.getElementById('grm-write-tags')?.checked) {
          setTimeout(grmVerifyWrittenTags, 2000);
        }
      } else {
        document.getElementById('grm-status-title').textContent =
          d.status === 'stopped' ? 'SCAN STOPPED' : 'SCAN FAILED';
        if (d.status === 'failed') {
          const why = d.message || 'Scan failed';
          document.getElementById('grm-status-sub').textContent = why;
          grmLog(`✗ ${why}`, 'err');
          toast(why, 'error');
        } else {
          document.getElementById('grm-status-sub').textContent =
            `${matched.toLocaleString()} hits found before stop`;
        }
        if (matched > 0) grmSearch();
      }
    }
  } catch(e) { grmLog(`Poll error: ${e.message}`, 'err'); }
}

async function grmVerifyWrittenTags() {
  grmLog('🔍 Verifying COMMENT tags were written to files…', 'info');
  try {
    const r = await fetch(`${API}/api/groomer/verify-tags`, {headers:authHdrs()});
    if (!r.ok) { grmLog('⚠ Tag verification failed — could not reach endpoint', 'warn'); return; }
    const d = await r.json();
    if (!d.verified || !d.verified.length) {
      grmLog('⚠ Tag verification — no files to check (no chart_data with file paths)', 'warn');
      return;
    }
    let passed = 0; let failed = 0;
    d.verified.forEach(v => {
      if (v.ok) {
        passed++;
        grmLog(`✓ Tag verified: ${v.file_path.split('/').pop()} — "${v.actual}"`, 'ok');
      } else {
        failed++;
        const reason = v.error || `Expected "${v.expected}" · Got "${v.actual || 'nothing'}"`;
        grmLog(`✗ Tag missing: ${v.file_path.split('/').pop()} — ${reason}`, 'err');
      }
    });
    const summary = `Tag verification: ${passed}/${d.count} files confirmed written`;
    grmLog(passed === d.count ? `✓ ${summary}` : `⚠ ${summary}`, passed === d.count ? 'ok' : 'warn');
  } catch(e) {
    grmLog(`Tag verification error: ${e.message}`, 'err');
  }
}

function grmScanDone(success) {
  const scan = document.getElementById('grm-btn-scan');
  scan.disabled = false; scan.style.opacity = '1';
  const stop = document.getElementById('grm-btn-stop');
  stop.disabled = true;  stop.style.opacity = '.4';
  grmScanJobId = null;
  document.getElementById('grm-scan-wrap').classList.remove('bar-pulsing');
  // Hide phase banner
  const banner = document.getElementById('grm-phase-banner');
  if (banner) banner.style.display = 'none';
  grmSyncBar();
}

// ── Filter helpers ────────────────────────────────────────────────
function grmUpdatePeakMin(v) {
  document.getElementById('grm-peak-min-display').textContent = `#${v}`;
  // Ensure peak-min never exceeds peak-max
  const maxSlider = document.getElementById('grm-peak');
  if (parseInt(v) > parseInt(maxSlider.value)) {
    maxSlider.value = v;
    grmUpdatePeak(v);
  }
  grmSyncBar();
}
function grmUpdatePeak(v) {
  const minSlider = document.getElementById('grm-peak-min');
  const minVal = parseInt(minSlider ? minSlider.value : 1);
  // Ensure peak-max never goes below peak-min
  if (parseInt(v) < minVal) {
    minSlider.value = v;
    document.getElementById('grm-peak-min-display').textContent = `#${v}`;
  }
  const fromVal = minSlider ? parseInt(minSlider.value) : 1;
  const label = fromVal === 1
    ? (v == 1 ? '#1 Only' : `Top ${v}`)
    : `#${fromVal}–#${v}`;
  document.getElementById('grm-peak-display').textContent = label;
  document.getElementById('grm-playlist-name').value = fromVal === 1
    ? (v == 1 ? '#1 Hits' : `Top ${v} Hits`)
    : `Top ${fromVal}-${v} Hits`;
  grmSyncBar();
}
function grmUpdateWeeks(v) {
  document.getElementById('grm-weeks-display').textContent = v;
}
function grmToggleMood(mood) {
  const p = document.querySelector(`[data-mood="${mood}"]`);
  if (!p) return;
  const on = p.dataset.active === '1';
  p.dataset.active = on?'0':'1';
  p.style.borderColor = on?'var(--border)':'var(--purple)';
  p.style.background  = on?'var(--bg)':'rgba(192,132,252,.12)';
  p.style.color       = on?'var(--muted)':'var(--purple)';
}
function grmGetSelectedMoods() {
  return [...document.querySelectorAll('[data-mood]')]
    .filter(p=>p.dataset.active==='1').map(p=>p.dataset.mood);
}
// ── Genre Tree ───────────────────────────────────────────────────
// Tree definition: [key, label, children[], isCCM]
const _GRM_TREE = [
  ['rock','Rock',[
    ['classicrock','Classic Rock',[]],['hardrock','Hard Rock',[]],
    ['softrock','Soft Rock',[]],['arenarock','Arena Rock',[]],
    ['indierock','Indie Rock',[]],['folkrock','Folk Rock',[]],
    ['southernrock','Southern Rock',[]],['punkrock','Punk',[]],
    ['newwave','New Wave ↔',[]],['artrock','Art Rock',[]],
    ['britinv','British Invasion ↔',[]],
    ['alternative','Alternative Rock',[]],['poprock','Pop Rock ↔',[]],
    ['countryrock','Country Rock ↔',[]],['bluesrock','Blues Rock ↔',[]],
    ['progrock','Progressive Rock',[]],['psychrock','Psychedelic Rock',[]],
    ['rocknroll','Rock & Roll',[]],
  ]],
  ['pop','Pop',[
    ['dancepop','Dance-Pop',[]],['synthpop','Synth-Pop',[]],
    ['teenpop','Teen Pop',[]],['powerpop','Power Pop',[]],
    ['adultpop','Adult Contemporary',[]],['indipop','Indie Pop',[]],
    ['easylistening','Easy Listening',[]],['standards','Standards ↔',[]],
    ['chill','Chill / Downtempo ↔',[]],['soundtrack','Soundtrack',[]],
  ]],
  ['country','Country',[
    ['tradcountry','Traditional Country',[]],['countrypop','Country Pop',[]],
    ['outlawcountry','Outlaw / Texas Country',[]],['americana','Americana / Bluegrass',[]],
  ]],
  ['rnb','R&B / Soul',[
    ['soul','Soul',[]],['neosoul','Neo-Soul',[]],['funk','Funk',[]],
    ['newjack','New Jack Swing',[]],['doowop','Doo-Wop',[]],
    ['contrnb','Contemporary R&B',[]],
  ]],
  ['hiphop','Hip-Hop / Rap',[
    ['trap','Trap',[]],['boombap','Boom Bap',[]],
    ['gangstarap','Gangsta Rap',[]],['conscrap','Conscious Rap',[]],
  ]],
  ['metal','Metal',[
    ['heavymetal','Heavy Metal',[]],['thrashmetal','Thrash Metal',[]],
    ['glaemmetal','Glam Metal',[]],['altmetal','Alt Metal ↔',[]],
  ]],
  ['dance','Dance / Electronic',[
    ['edm','EDM',[]],['house','House',[]],['techno','Techno',[]],
    ['trance','Trance',[]],['disco','Disco',[]],['eurodance','Eurodance',[]],
  ]],
  ['folk','Folk',[
    ['singersong','Singer-Songwriter',[]],
  ]],
  ['jazz','Jazz',[
    ['smoothjazz','Smooth Jazz',[]],['vocaljazz','Vocal Jazz',[]],
    ['bebop','Bebop',[]],['jazzfusion','Jazz Fusion',[]],
    ['swing','Swing / Big Band',[]],
  ]],
  ['blues','Blues',[]],
  ['classical','Classical',[]],
  ['world','World',[
    ['reggae','Reggae & Ska',[]],['latin','Latin',[]],
  ]],
  ['mellow','🌙 Mellow / Easy Listening',[]],
  // CCM family — separator handled in render
  ['ccm','CCM',[
    ['ccm-ac','Christian AC',[]],['ccm-rock','Christian Rock',[]],
    ['worship','Worship / Praise',[]],['ccm-country','Christian Country',[]],
    ['ccm-folk','Christian Folk',[]],['ccm-pop','Christian Pop',[]],
    ['ccm-hiphop','Christian Hip-Hop',[]],
  ],true],
  ['gospel','Gospel',[
    ['sgospel','Southern Gospel',[]],['ugospel','Urban Gospel',[]],
    ['tgospel','Traditional Gospel',[]],
  ],true],
];

// State: set of selected leaf keys. Empty = ALL.
let _grmTreeSelected = new Set();
// Track which parents are expanded
let _grmTreeExpanded = new Set(['rock']); // rock open by default

let _grmCounts = null;
async function grmLoadCounts() {
  try {
    const r = await fetch(`${API}/api/retriever/genre-engine/style-counts`, {headers: authHdrs()});
    if (r.ok) { const d = await r.json(); _grmCounts = {...d.families, ...d.styles}; grmBuildTree(); }
  } catch {}
}
function _grmCnt(k) {
  const n = _grmCounts && _grmCounts[k];
  return n ? ` <span style="color:var(--muted2);font-size:.5rem">${n.toLocaleString()}</span>` : '';
}
function grmBuildTree() {
  const container = document.getElementById('grm-tree-nodes');
  if (!container) return;
  if (_grmCounts === null) { _grmCounts = {}; grmLoadCounts(); }
  let html = '';
  let ccmDividerDone = false;
  _GRM_TREE.forEach(([key, label, children, isCCM]) => {
    if (isCCM && !ccmDividerDone) {
      html += '<div class="grm-tree-divider"></div>';
      ccmDividerDone = true;
    }
    const hasChildren = children.length > 0;
    const expanded = _grmTreeExpanded.has(key);
    const cbState = _grmParentState(key, children);
    const ccmClass = isCCM ? ' ccm' : '';
    const cbClass = isCCM
      ? (cbState==='chk'?'gt-cb ccm-chk':cbState==='mix'?'gt-cb ccm-mix':'gt-cb')
      : (cbState==='chk'?'gt-cb chk':cbState==='mix'?'gt-cb mix':'gt-cb');
    const cbText = cbState==='chk'?'✓':cbState==='mix'?'—':'';
    html += `<div class="gt-row" id="gt-row-${key}" onclick="grmTreeParentClick('${key}')">
      <span class="gt-chev${hasChildren?'':' leaf'}${expanded?' open':''}" id="gt-chev-${key}">${hasChildren?'▶':''}</span>
      <div class="${cbClass}" id="gt-cb-${key}">${cbText}</div>
      <span class="gt-label${ccmClass}">${label}${_grmCnt(key)}</span>
    </div>`;
    if (hasChildren) {
      html += `<div id="gt-children-${key}" style="${expanded?'':'display:none'}">`;
      children.forEach(([ckey, clabel]) => {
        const sel = _grmTreeSelected.has(ckey);
        const ccbClass = isCCM
          ? (sel?'gt-cb ccm-chk':'gt-cb')
          : (sel?'gt-cb chk':'gt-cb');
        const ccbText = sel?'✓':'';
        html += `<div class="gt-row depth-1" id="gt-row-${ckey}" onclick="grmTreeLeaf('${ckey}','${key}')">
          <span class="gt-chev leaf"></span>
          <div class="${ccbClass}" id="gt-cb-${ckey}">${ccbText}</div>
          <span class="gt-label${isCCM?' ccm':''}" style="font-size:.58rem">${clabel}${_grmCnt(ckey)}</span>
        </div>`;
      });
      html += '</div>';
    }
  });
  container.innerHTML = html;
  _grmSyncAllRow();
}

function _grmParentState(pkey, children) {
  if (!children.length) return _grmTreeSelected.has(pkey)?'chk':'';
  const checked = children.filter(([k])=>_grmTreeSelected.has(k)).length;
  if (checked === 0) return '';
  if (checked === children.length) return 'chk';
  return 'mix';
}

function _grmSyncAllRow() {
  const cb = document.getElementById('gt-cb-all');
  if (!cb) return;
  if (_grmTreeSelected.size === 0) {
    cb.className='gt-cb chk'; cb.textContent='✓';
  } else {
    cb.className='gt-cb'; cb.textContent='';
  }
}

function grmTreeParentClick(pkey) {
  // Find children for this parent
  const node = _GRM_TREE.find(n=>n[0]===pkey);
  if (!node) return;
  const children = node[2];
  // If click is on chevron area — toggle expand. But since we only have one onclick,
  // we toggle expand AND handle checkbox together:
  // First toggle expand
  const childDiv = document.getElementById('gt-children-'+pkey);
  const chev = document.getElementById('gt-chev-'+pkey);
  if (childDiv) {
    const open = childDiv.style.display !== 'none';
    childDiv.style.display = open ? 'none' : '';
    if (chev) chev.classList.toggle('open', !open);
    if (!open) _grmTreeExpanded.add(pkey); else _grmTreeExpanded.delete(pkey);
  }
  // Then toggle parent checkbox (select/deselect all children + parent key)
  if (children.length) {
    const allChecked = children.every(([k])=>_grmTreeSelected.has(k));
    children.forEach(([k])=>{
      if (allChecked) _grmTreeSelected.delete(k); else _grmTreeSelected.add(k);
    });
    // Also add/remove the parent key itself so bare parent-tagged tracks are included
    if (allChecked) _grmTreeSelected.delete(pkey); else _grmTreeSelected.add(pkey);
  } else {
    if (_grmTreeSelected.has(pkey)) _grmTreeSelected.delete(pkey);
    else _grmTreeSelected.add(pkey);
  }
  grmBuildTree();
  _grmFireFilter();
}

function grmTreeLeaf(key, parentKey) {
  if (_grmTreeSelected.has(key)) _grmTreeSelected.delete(key);
  else _grmTreeSelected.add(key);
  if (parentKey) {   // whole family only while every style is ticked
    const pn = _GRM_TREE.find(n=>n[0]===parentKey);
    if (pn && pn[2].length && pn[2].every(([k])=>_grmTreeSelected.has(k))) _grmTreeSelected.add(parentKey);
    else _grmTreeSelected.delete(parentKey);
  }
  // Sync parent state
  if (parentKey) {
    const node = _GRM_TREE.find(n=>n[0]===parentKey);
    if (node) {
      const children = node[2];
      const cb = document.getElementById('gt-cb-'+parentKey);
      const isCCM = !!node[3];
      const state = _grmParentState(parentKey, children);
      if (cb) {
        cb.className = isCCM
          ? (state==='chk'?'gt-cb ccm-chk':state==='mix'?'gt-cb ccm-mix':'gt-cb')
          : (state==='chk'?'gt-cb chk':state==='mix'?'gt-cb mix':'gt-cb');
        cb.textContent = state==='chk'?'✓':state==='mix'?'—':'';
      }
    }
  }
  // Update the leaf cb directly
  const node2 = parentKey ? _GRM_TREE.find(n=>n[0]===parentKey) : null;
  const isCCM2 = node2?!!node2[3]:false;
  const sel = _grmTreeSelected.has(key);
  const leafCb = document.getElementById('gt-cb-'+key);
  if (leafCb) {
    leafCb.className = isCCM2 ? (sel?'gt-cb ccm-chk':'gt-cb') : (sel?'gt-cb chk':'gt-cb');
    leafCb.textContent = sel?'✓':'';
  }
  _grmSyncAllRow();
  // Show/hide retriever warning
  const warn = document.getElementById('grm-retriever-warn');
  if (warn) warn.style.display = _grmTreeSelected.size > 0 ? '' : 'none';
  _grmFireFilter();
}

function grmTreeAll() {
  _grmTreeSelected.clear();
  grmBuildTree();
  const warn = document.getElementById('grm-retriever-warn');
  if (warn) warn.style.display = 'none';
  _grmFireFilter();
}

function _grmFireFilter() {
  if (_grmHasScanned) grmSearch();
}

// Expose for grmSearch compatibility
function grmGetActivePills() {
  return [..._grmTreeSelected];
}

// Panel collapse/expand
function grmToggleConfig() {
  const panel = document.getElementById('grm-config-panel');
  panel.classList.toggle('collapsed');
}

function grmToggleTree() {
  const panel = document.getElementById('grm-tree-panel');
  panel.classList.toggle('collapsed');
}

// ── Drag-to-resize pane dividers ─────────────────────────────────
(function() {
  function initDivider(dividerId, leftPanelId, minLeft, minRight) {
    const divider = document.getElementById(dividerId);
    if (!divider) return;
    let dragging = false;
    let startX = 0;
    let startWidth = 0;

    divider.addEventListener('mousedown', e => {
      const leftPanel = document.getElementById(leftPanelId);
      if (!leftPanel || leftPanel.classList.contains('collapsed')) return;
      dragging = true;
      startX = e.clientX;
      startWidth = leftPanel.offsetWidth;
      divider.classList.add('dragging');
      document.body.style.cursor = 'col-resize';
      document.body.style.userSelect = 'none';
      e.preventDefault();
    });

    document.addEventListener('mousemove', e => {
      if (!dragging) return;
      const leftPanel = document.getElementById(leftPanelId);
      if (!leftPanel) return;
      const layout = document.getElementById('grm-layout');
      const delta = e.clientX - startX;
      const newWidth = Math.max(minLeft, Math.min(startWidth + delta, layout.offsetWidth - minRight));
      leftPanel.style.width = newWidth + 'px';
    });

    document.addEventListener('mouseup', () => {
      if (!dragging) return;
      dragging = false;
      divider.classList.remove('dragging');
      document.body.style.cursor = '';
      document.body.style.userSelect = '';
    });

    // Double-click divider = collapse left panel
    divider.addEventListener('dblclick', () => {
      const leftPanel = document.getElementById(leftPanelId);
      if (leftPanel) leftPanel.classList.toggle('collapsed');
    });
  }

  document.addEventListener('DOMContentLoaded', () => {
    initDivider('grm-divider-1', 'grm-config-panel', 180, 400);
    initDivider('grm-divider-2', 'grm-tree-panel',   160, 300);
  });
})();

// ── Playlist filter helpers ───────────────────────────────────────
let grmCurrentConf = '';

function grmPFSelectAll(checked) { /* legacy stub */ }

function grmSetConf(val) {
  grmCurrentConf = val;
  // Update button styles
  const btns = {
    '':       'grm-conf-all',
    'high':   'grm-conf-high',
    'medium': 'grm-conf-medium',
    'low':    'grm-conf-low',
  };
  Object.entries(btns).forEach(([k, id]) => {
    const btn = document.getElementById(id);
    if (!btn) return;
    const active = k === val;
    const colors = {'':'var(--pink)', 'high':'var(--green)', 'medium':'var(--amber)', 'low':'var(--orange)'};
    btn.style.background   = active ? (colors[k] || 'var(--pink)') : 'var(--surface3)';
    btn.style.color        = active ? '#000' : (colors[k] || 'var(--muted)');
    btn.style.border       = active ? 'none' : '1px solid var(--border2)';
  });
  grmSearch();
}

// Wire slider auto-refresh
document.addEventListener('DOMContentLoaded', () => {
  ['grm-peak','grm-peak-min','grm-weeks'].forEach(id => {
    const el = document.getElementById(id);
    if (el) el.addEventListener('change', () => { if (grmResults.length) grmSearch(); });
  });
});

// ── Top Dogs tooltips (fixed-position so the narrow panel never clips them) ──
(function () {
  let tip;
  const show = el => {
    if (!tip) { tip = document.createElement('div'); tip.id = 'grm-tip'; document.body.appendChild(tip); }
    tip.textContent = el.dataset.tip; tip.style.display = 'block';
    const r = el.getBoundingClientRect(), w = tip.offsetWidth, h = tip.offsetHeight;
    let x = r.right + 8, y = r.top - 4;
    if (x + w > innerWidth - 8) x = Math.max(8, r.left - w - 8);
    if (y + h > innerHeight - 8) y = innerHeight - h - 8;
    tip.style.left = x + 'px'; tip.style.top = Math.max(8, y) + 'px';
  };
  const hide = () => { if (tip) tip.style.display = 'none'; };
  document.addEventListener('mouseover', e => { const q = e.target.closest?.('[data-tip]'); if (q) show(q); });
  document.addEventListener('mouseout',  e => { if (e.target.closest?.('[data-tip]')) hide(); });
  document.addEventListener('focusin',   e => { if (e.target.matches?.('[data-tip]')) show(e.target); });
  document.addEventListener('focusout',  e => { if (e.target.matches?.('[data-tip]')) hide(); });
  document.addEventListener('DOMContentLoaded', () => {
    const qs = document.getElementById('grm-qs'); if (!qs) return;
    try { if (localStorage.getItem('ch_grm_qs') === 'closed') qs.open = false; } catch {}
    qs.addEventListener('toggle', () => { try { localStorage.setItem('ch_grm_qs', qs.open ? 'open' : 'closed'); } catch {} });
  });
})();

// ── M4: genre match mode ─────────────────────────────────────────
let grmGenreMode = 'main';
function grmSetGenreMode(m, silent) {
  grmGenreMode = m;
  [['main','grm-gm-main'],['any','grm-gm-any']].forEach(([k,id]) => {
    const b = document.getElementById(id); if (!b) return;
    b.style.background = k === m ? 'var(--pink)' : 'var(--surface3)';
    b.style.color      = k === m ? '#000' : 'var(--muted)';
  });
  if (!silent && grmGetActivePills().length) grmSearch();
}

// ── M4: Last.fm online data ──────────────────────────────────────
let _grmLfmTimer = null;
async function grmLfmPoll() {
  try {
    const r = await fetch(`${API}/api/groomer/lastfm/status`, {headers:authHdrs()});
    if (!r.ok) return;
    const d = await r.json();
    const st = document.getElementById('grm-lfm-state'), msg = document.getElementById('grm-lfm-msg');
    const bar = document.getElementById('grm-lfm-bar');
    const running = d.status === 'running';
    const pct = d.total ? Math.round(100 * d.done / d.total) : (d.status === 'done' ? 100 : 0);
    if (bar) bar.style.width = pct + '%';
    const ok = (d.coverage && d.coverage.ok) || 0;
    if (st) st.textContent = running
      ? `${d.phase === 'artists' ? 'ARTISTS' : 'SONGS'} ${d.done.toLocaleString()}/${d.total.toLocaleString()}`
      : `${ok.toLocaleString()} songs cached`;
    if (st) st.style.color = running ? 'var(--teal)' : d.status === 'failed' ? 'var(--red)' : 'var(--muted2)';
    if (msg) msg.textContent = d.message || (d.status === 'idle' ? 'Starts automatically a few minutes after ChartHound starts, and after each scan.' : '');
    const sb = document.getElementById('grm-lfm-start'), tb = document.getElementById('grm-lfm-stop');
    if (sb) { sb.disabled = running; sb.style.opacity = running ? '.35' : '1'; }
    if (tb) { tb.disabled = !running; tb.style.opacity = running ? '1' : '.35'; }
    clearTimeout(_grmLfmTimer);
    _grmLfmTimer = setTimeout(grmLfmPoll, running ? 5000 : 30000);
  } catch {}
}
async function grmLfmStart() {
  const r = await fetch(`${API}/api/groomer/lastfm/start`, {method:'POST', headers:authHdrs()});
  const d = await r.json().catch(() => ({}));
  if (!r.ok) { toast(d.detail || `HTTP ${r.status}`, 'error'); return; }
  toast('Last.fm lookup started — runs in the background', 'ok'); setTimeout(grmLfmPoll, 800);
}
async function grmLfmStop() {
  await fetch(`${API}/api/groomer/lastfm/stop`, {method:'POST', headers:authHdrs()});
  toast('Stopping after the current song — progress is saved', 'ok'); setTimeout(grmLfmPoll, 1500);
}

// ── Scan / Build views ───────────────────────────────────────────
function grmSetView(v) {
  const lay = document.getElementById('grm-layout'); if (!lay) return;
  lay.dataset.view = v;
  document.getElementById('grm-vbtn-scan')?.classList.toggle('on', v === 'scan');
  document.getElementById('grm-vbtn-build')?.classList.toggle('on', v === 'build');
  if (v === 'build' && !grmResults.length && typeof grmSearch === 'function') grmSearch();
}
function grmScanBadge(text) {
  const b = document.getElementById('grm-vbtn-scan'); if (b) b.textContent = text ? `① SCAN · ${text}` : '① SCAN';
}
async function grmInitView() {
  if (window._stWantView) { grmSetView(window._stWantView); window._stWantView = null; return; }
  // Open on BUILD when scored results exist, otherwise SCAN
  try {
    const r = await fetch(`${API}/api/groomer/results?limit=1&min_peak=1&max_peak=100`, {headers:authHdrs()});
    const d = r.ok ? await r.json() : {};
    grmSetView((d.total || 0) > 0 ? 'build' : 'scan');
  } catch { grmSetView('scan'); }
}

// ── M2: Hits filters + playlist builder ─────────────────────────
const grmDecades = new Set();
let grmBuiltDays = null;   // [{day, track_ids, minutes}] when a multi-day set is shown

// One job per playlist: multi-day build → one per day (unless rows are hand-selected)
function grmPlaylistJobs() {
  const name = document.getElementById('grm-playlist-name').value.trim() || 'Chart Hits';
  if (grmBuiltDays && grmSelected.size === 0)
    return grmBuiltDays.map(d => ({name: `${name} — Day ${d.day}`, ids: d.track_ids}));
  const ids = grmSelected.size > 0 ? [...grmSelected] : grmResults.map(r => r.track_id);
  return ids.length ? [{name, ids}] : [];
}

// ── M6: "N big hits you don't own" after a build ─────────────────
async function grmMissingHint(body) {
  const el = document.getElementById('grm-missing-hint'); if (!el) return;
  el.style.display = 'none';
  const minScore = Math.max(70, body.min_score || 0);
  try {
    const r = await fetch(`${API}/api/groomer/big-hits`, {method:'POST', headers:authHdrs(),
      body: JSON.stringify({decades: body.decades, genres: (body.genre || '').split(',').filter(Boolean),
                            min_score: minScore, ohw_only: body.ohw_only, crossovers: body.crossovers, limit: 0})});
    const d = await r.json();
    if (!r.ok || !d.total) return;
    const dec = (body.decades || []).map(x => `${String(x).slice(2)}s`).join('+');
    el.innerHTML = `🎯 <b>${d.total.toLocaleString()}</b> big ${dec ? dec + ' ' : ''}hits (score ${minScore}+) you don't own → <u>find them in The Sniffer</u>`;
    el.onclick = () => grmOpenSnifferGaps(body, minScore);
    el.style.display = '';
  } catch {}
}
function grmOpenSnifferGaps(body, minScore) {
  switchTab('sniffer');
  setTimeout(() => {
    snfSetMode('bighits');
    const all = document.getElementById('snf-decade-all');
    const decs = (body.decades || []).map(d => `${d}s`);
    if (all) all.checked = !decs.length;
    document.querySelectorAll('.snf-decade').forEach(cb => cb.checked = decs.includes(cb.value));
    const fams = (body.genre || '').split(',');
    document.querySelectorAll('.snf-genre').forEach(cb => cb.checked = fams.includes(cb.value));
    document.getElementById('snf-bh-score').value = minScore;
    document.getElementById('snf-bh-score-display').textContent = minScore + '+';
    document.getElementById('snf-bh-ohw').checked = !!body.ohw_only;
    snfRunAnalysis();
  }, 300);
}

// ── Presets ──────────────────────────────────────────────────────
let grmPresets = {};
async function grmLoadPresets() {
  try {
    const r = await fetch(`${API}/api/groomer/presets`, {headers:authHdrs()});
    grmPresets = (await r.json()).presets || {};
  } catch { grmPresets = {}; }
  const sel = document.getElementById('grm-preset'); if (!sel) return;
  sel.innerHTML = '<option value="">— Presets —</option>' +
    Object.keys(grmPresets).sort().map(k => `<option value="${esc(k)}">${esc(k)}</option>`).join('');
}
async function grmPutPresets() {
  const r = await fetch(`${API}/api/groomer/presets`, {method:'PUT', headers:authHdrs(), body:JSON.stringify({presets:grmPresets})});
  if (!r.ok) throw new Error(`HTTP ${r.status}`);
}
async function grmSavePreset() {
  const def = document.getElementById('grm-preset').value || document.getElementById('grm-playlist-name').value.trim();
  const name = (prompt('Preset name:', def) || '').trim();
  if (!name) return;
  grmPresets[name] = {
    genres: grmGetActivePills(), decades: [...grmDecades],
    min_score: document.getElementById('grm-min-score').value,
    ohw: !!document.getElementById('grm-ohw').checked, genre_mode: grmGenreMode,
    crossovers: !!document.getElementById('grm-cross').checked,
    holiday: document.getElementById('grm-holiday')?.value || 'hide',
    len: document.getElementById('grm-len').value, days: document.getElementById('grm-days').value,
    fam: document.getElementById('grm-fam').value, gap: document.getElementById('grm-gap').value,
    playlist: document.getElementById('grm-playlist-name').value,
  };
  try { await grmPutPresets(); await grmLoadPresets(); document.getElementById('grm-preset').value = name; toast(`Preset "${name}" saved`, 'success'); }
  catch (e) { toast(`Save failed: ${e.message}`, 'error'); }
}
async function grmDeletePreset() {
  const name = document.getElementById('grm-preset').value;
  if (!name || !confirm(`Delete preset "${name}"?`)) return;
  delete grmPresets[name];
  try { await grmPutPresets(); await grmLoadPresets(); toast('Preset deleted', 'ok'); }
  catch (e) { toast(`Delete failed: ${e.message}`, 'error'); }
}
function grmLoadPreset(name) {
  const p = grmPresets[name]; if (!p) return;
  const set = (id, v, disp, fmt) => { const el = document.getElementById(id); if (el && v != null) { el.value = v; const d = document.getElementById(disp); if (d) d.textContent = fmt(v); } };
  set('grm-min-score', p.min_score, 'grm-score-display', v => v > 0 ? v + '+' : 'Any');
  set('grm-len', p.len, 'grm-len-display', v => grmFmtHours(v * 60));
  set('grm-days', p.days, 'grm-days-display', v => v);
  set('grm-fam', p.fam, 'grm-fam-display', v => v + '%');
  set('grm-gap', p.gap, 'grm-gap-display', v => v);
  document.getElementById('grm-ohw').checked = !!p.ohw;
  grmSetGenreMode(p.genre_mode || 'main', true);
  document.getElementById('grm-cross').checked = p.crossovers !== false;
  { const h = document.getElementById('grm-holiday'); if (h) h.value = p.holiday || 'hide'; }
  if (p.playlist) document.getElementById('grm-playlist-name').value = p.playlist;
  grmDecades.clear(); (p.decades || []).forEach(d => grmDecades.add(parseInt(d)));
  document.querySelectorAll('.grm-dec').forEach(b => {
    const on = grmDecades.has(parseInt(b.dataset.dec));
    b.style.background = on ? 'var(--pink)' : 'var(--surface3)'; b.style.color = on ? '#000' : 'var(--muted)';
  });
  _grmTreeSelected.clear(); (p.genres || []).forEach(g => _grmTreeSelected.add(g));
  grmBuildTree(); _grmSyncAllRow();
  toast(`Preset "${name}" loaded — click Build`, 'ok');
}
document.addEventListener('DOMContentLoaded', () => setTimeout(grmLoadPresets, 1500));
function grmToggleDecade(d) {
  if (grmDecades.has(d)) grmDecades.delete(d); else grmDecades.add(d);
  document.querySelectorAll('.grm-dec').forEach(b => {
    const on = grmDecades.has(parseInt(b.dataset.dec));
    b.style.background = on ? 'var(--pink)' : 'var(--surface3)';
    b.style.color      = on ? '#000' : 'var(--muted)';
  });
  grmSearch();
}
function grmFmtHours(min) {
  min = Math.round(min); const h = Math.floor(min / 60), m = min % 60;
  return h ? `${h}h${m ? ' ' + String(m).padStart(2,'0') + 'm' : ''}` : `${m}m`;
}
async function grmBuildPlaylist() {
  const btn = document.getElementById('grm-btn-build');
  const pills = grmGetActivePills();
  const body = {
    genre:        pills.length ? pills.join(',') : null,
    decades:      [...grmDecades],
    min_score:    parseInt(document.getElementById('grm-min-score')?.value || 0),
    ohw_only:     !!document.getElementById('grm-ohw')?.checked,
    genre_mode:   grmGenreMode,
    crossovers:   !!document.getElementById('grm-cross')?.checked,
    holiday:      document.getElementById('grm-holiday')?.value || 'hide',
    minutes:      Math.round(parseFloat(document.getElementById('grm-len').value) * 60),
    days:         parseInt(document.getElementById('grm-days').value),
    familiar_pct: parseInt(document.getElementById('grm-fam').value),
    artist_gap:   parseInt(document.getElementById('grm-gap').value),
  };
  btn.disabled = true; btn.textContent = 'BUILDING…';
  try {
    const r = await fetch(`${API}/api/groomer/playlist/build`, {method:'POST', headers:authHdrs(), body:JSON.stringify(body)});
    const d = await r.json();
    if (!r.ok) throw new Error(typeof d.detail === 'string' ? d.detail : `HTTP ${r.status}`);
    if (!d.count) { toast(d.message || 'No songs match these filters.', 'warn'); return; }
    const nameEl = document.getElementById('grm-playlist-name');
    if (!nameEl.value.trim() || nameEl.value === 'Chart Hits') {
      const dec = [...grmDecades].sort().map(x => `${String(x).slice(2)}s`).join('+');
      nameEl.value = `Road Trip${dec ? ' ' + dec : ''}${body.ohw_only ? ' One-Hit Wonders' : ''} Hits`;
    }
    grmResults = d.results; _grmTotal = d.count; _grmOffset = d.count;
    grmBuiltDays = (d.days && d.days.length > 1) ? d.days : null;
    _sortKey = 'seq'; _sortAsc = true;
    ['artist','title','year','stars','score','peak','weeks','conf','genre'].forEach(k => {
      const el = document.getElementById('sort-ind-' + k); if (el) el.textContent = '';
    });
    grmRenderResults(grmResults);
    grmUpdateLoadMore();
    document.getElementById('grm-badge').textContent = grmBuiltDays
      ? `${grmBuiltDays.length} days · ${d.count} songs · ${grmFmtHours(d.minutes)}`
      : `${d.count} songs · ${grmFmtHours(d.minutes)}`;
    let msg = `✓ Built ${grmBuiltDays ? grmBuiltDays.length + ' playlists, ' : ''}${d.count} songs (${grmFmtHours(d.minutes)}) from ${d.candidates.toLocaleString()} candidates`;
    if (grmBuiltDays) msg += ' · ' + grmBuiltDays.map(x => `Day ${x.day}: ${grmFmtHours(x.minutes)}`).join(', ');
    if (d.unknown_durations) msg += ` · ${d.unknown_durations} lengths estimated (rescan fills these in)`;
    grmLog(msg, 'ok');
    grmMissingHint(body);
    if (d.short) toast('Not enough matching songs to fill the full length — loosen the filters.', 'warn');
    else toast(`Playlist built — ${d.count} songs`, 'success');
  } catch (e) {
    grmLog(`✗ Build failed: ${e.message}`, 'err'); toast(`Build failed: ${e.message}`, 'error');
  } finally { btn.disabled = false; btn.textContent = '🚗 BUILD PLAYLIST'; }
}
async function grmRecalcScores(btn) {
  btn.disabled = true; const t = btn.textContent; btn.textContent = 'CALCULATING…';
  try {
    const r = await fetch(`${API}/api/groomer/hitscore/rebuild`, {method:'POST', headers:authHdrs()});
    const d = await r.json();
    if (!r.ok) throw new Error(typeof d.detail === 'string' ? d.detail : `HTTP ${r.status}`);
    toast(`Hit Scores updated — ${d.count.toLocaleString()} tracks`, 'success');
    grmSearch();
  } catch (e) { toast(e.message, 'error'); }
  finally { btn.disabled = false; btn.textContent = t; }
}

// ── Results ───────────────────────────────────────────────────────
let _grmOffset   = 0;
let _grmTotal    = 0;
const GRM_PAGE   = 500;

async function grmSearch(append = false) {
  const peakMax  = document.getElementById('grm-peak').value;
  const peakMin  = document.getElementById('grm-peak-min')?.value || 1;
  const weeksMin = document.getElementById('grm-weeks')?.value || 1;
  const conf     = grmCurrentConf || '';
  if (!append) { _grmOffset = 0; grmResults = []; grmSelected.clear(); }
  const activePills = grmGetActivePills();
  const params  = new URLSearchParams({
    min_peak: peakMin,
    max_peak: peakMax,
    limit:    GRM_PAGE,
    offset:   _grmOffset,
  });
  if (activePills.length) params.set('genre', activePills.join(','));
  if (weeksMin > 1) params.set('min_weeks', weeksMin);
  if (conf) params.set('confidence', conf);
  const minScore = parseInt(document.getElementById('grm-min-score')?.value || 0);
  if (minScore > 0) params.set('min_score', minScore);
  if (grmDecades.size) params.set('decades', [...grmDecades].join(','));
  if (document.getElementById('grm-ohw')?.checked) params.set('ohw', 'true');
  params.set('genre_mode', grmGenreMode);
  params.set('crossovers', document.getElementById('grm-cross')?.checked ? 'true' : 'false');
  params.set('holiday', document.getElementById('grm-holiday')?.value || 'hide');
  if (_sortKey === 'seq') { _sortKey = 'score'; _sortAsc = true; }
  grmBuiltDays = null;
  try {
    const r = await fetch(`${API}/api/groomer/results?${params}`, {headers:authHdrs()});
    if (!r.ok) return;
    const d = await r.json();
    _grmTotal = d.total || 0;
    if (append) {
      grmResults = grmResults.concat(d.results || []);
    } else {
      grmResults = d.results || [];
    }
    _grmOffset += (d.results || []).length;
    _grmHasScanned = true;
    grmRenderResults(grmResults, append);
    grmSyncBar();
    grmUpdateLoadMore();
  } catch {}
}

function grmUpdateLoadMore() {
  let btn = document.getElementById('grm-load-more');
  if (!btn) {
    btn = document.createElement('div');
    btn.id = 'grm-load-more';
    btn.style.cssText = 'text-align:center;padding:.75rem;display:none';
    btn.innerHTML = `<button onclick="grmLoadMore()" style="padding:.4rem 1.2rem;background:var(--surface2);color:var(--pink);border:1px solid var(--pink);border-radius:5px;font-family:var(--mono);font-size:.65rem;cursor:pointer;letter-spacing:1px">LOAD MORE</button><span id="grm-load-count" style="font-family:var(--mono);font-size:.58rem;color:var(--muted);margin-left:.75rem"></span>`;
    const wrap = document.querySelector('#grm-tbody')?.closest('.overflow-x-auto, div[style*="overflow-x"]');
    if (wrap) wrap.appendChild(btn);
    else document.getElementById('grm-tbody')?.parentElement?.parentElement?.appendChild(btn);
  }
  const remaining = (_grmTotal > 0) ? Math.max(0, _grmTotal - _grmOffset) : 0;
  if (remaining > 0) {
    btn.style.display = 'block';
    const span = document.getElementById('grm-load-count');
    if (span) span.textContent = `${grmResults.length.toLocaleString()} shown · ${_grmTotal.toLocaleString()} total`;
  } else {
    btn.style.display = 'none';
  }
}

let _grmLoadingMore = false;
let _grmRenderedIds = new Set();  // track_ids already in the DOM

async function grmLoadMore() {
  if (_grmLoadingMore) return;
  _grmLoadingMore = true;
  const btn = document.querySelector('#grm-load-more button');
  if (btn) { btn.textContent = 'LOADING…'; btn.disabled = true; }
  try {
    await grmSearch(true);
  } finally {
    _grmLoadingMore = false;
    if (btn) { btn.textContent = 'LOAD MORE'; btn.disabled = false; }
  }
}

function grmRenderResults(results, append = false) {
  const fn = _sortFields[_sortKey] || _sortFields.peak;
  const tbody = document.getElementById('grm-tbody');
  const empty = document.getElementById('grm-empty');
  const badge = document.getElementById('grm-badge');

  if (!append) {
    // Full re-render — sort entire set, rebuild table
    _grmRenderedIds.clear();
    grmSelected.clear();
    const sa = document.getElementById('grm-select-all');
    if (sa) sa.checked = false;
    results = [...results].sort((a,b) => {
      const av = fn(a), bv = fn(b);
      if (av < bv) return _sortAsc ? -1 : 1;
      if (av > bv) return _sortAsc ? 1 : -1;
      return 0;
    });
  }

  if (!results.length && !append) {
    tbody.innerHTML = '';
    if (empty) empty.style.display = 'block';
    if (badge) badge.textContent = '0 songs';
    grmSyncBar(); return;
  }
  if (empty) empty.style.display = 'none';
  if (badge) badge.textContent = `${results.length.toLocaleString()} song${results.length!==1?'s':''}`;
  if (!append) tbody.innerHTML = '';

  // In append mode: only render rows not already in the DOM
  const toRender = append
    ? results.filter(t => !_grmRenderedIds.has(t.track_id))
    : results;

  toRender.forEach(t => {
    const artist = t.tag_artist || t.artist || '';
    const stars  = t.star_rating || t.stars || 0;
    const useH   = !!t.hot100_peak;
    const peak   = useH ? t.hot100_peak : (t.peak_position || 0);
    const conf   = t.confidence || 'low';
    const starsHTML = stars > 0
      ? '<span style="color:var(--amber)">'+'★'.repeat(stars)+'</span>'
        +'<span style="color:var(--muted2)">'+'★'.repeat(5-stars)+'</span>'
      : '<span style="color:var(--muted2)">—</span>';
    const chartName = useH ? 'hot100' : (t.chart_name || '');
    const chartDisplay = chartName ? chartName.toUpperCase() : '—';
    const peakHTML = conf === 'low' && peak
      ? `<span style="font-family:var(--display);font-size:1rem;color:var(--muted2)" title="Estimated">~#${peak}</span>`
      : peak
        ? `<span style="font-family:var(--display);font-size:1rem;color:var(--amber)">#${peak}</span>`
        : '<span style="color:var(--muted2)">—</span>';
    const weeks = (useH && t.hot100_weeks) ? t.hot100_weeks : (t.weeks_on_chart || 0);
    const wBar  = weeks > 0
      ? `<span style="color:var(--teal)">${'▪'.repeat(Math.min(weeks,10))}</span>`
        +`<span style="color:var(--muted2);font-size:.55rem"> ${weeks}w</span>` : '—';
    const chartBadge = `<span style="font-size:.5rem;padding:.1rem .35rem;border-radius:8px;
      background:rgba(244,114,182,.1);border:1px solid rgba(244,114,182,.25);
      color:var(--pink);white-space:nowrap">${esc(chartDisplay)}</span>`;
    const confColor = conf === 'high' ? 'var(--green)' : conf === 'medium' ? 'var(--amber)' : 'var(--orange)';
    const confDot   = `<span style="display:inline-block;width:7px;height:7px;border-radius:50%;background:${confColor};margin-right:.35rem;vertical-align:middle"></span>`;
    const confLabel = `${confDot}<span style="color:${confColor};font-size:.55rem">${conf}</span>`;
    const filePath  = t.file_path || '';
    const hs = t.hit_score;
    const hsColor = hs >= 85 ? 'var(--pink)' : hs >= 70 ? 'var(--amber)' : hs >= 50 ? 'var(--teal)' : 'var(--muted)';
    const fmtL = n => n >= 1e6 ? (n/1e6).toFixed(1) + 'M' : n >= 1e3 ? Math.round(n/1e3) + 'k' : String(n);
    const hsTip = [t.never_charted ? 'Never charted — included for staying power'
                   : (t.hot100_peak ? `Hot 100 #${t.hot100_peak}` : 'No Hot 100 data'),
                   t.listeners ? `${fmtL(t.listeners)} Last.fm listeners` : '',
                   t.main_genre ? `Genre: ${t.main_genre}` : '',
                   t.hot100_weeks ? `${t.hot100_weeks} wks` : '',
                   t.yearend_rank ? `Year-end #${t.yearend_rank}` : ''].filter(Boolean).join(' · ');
    const scoreHTML = (hs != null
      ? `<span title="${esc(hsTip)}" style="font-family:var(--display);font-size:1rem;color:${hsColor}">${hs}</span>`
      : '<span style="color:var(--muted2)">—</span>')
      + (t.one_hit_wonder ? ' <span title="One-hit wonder" style="font-size:.45rem;padding:.05rem .3rem;border-radius:6px;border:1px solid var(--amber);color:var(--amber)">1-HIT</span>' : '')
      + (t._seq ? ` <span style="color:var(--muted2);font-size:.5rem">${t._day ? 'D' + t._day + ' ' : ''}#${t._seq}</span>` : '');
    const tr = document.createElement('tr');
    tr.dataset.id     = t.track_id;
    tr.dataset.text   = `${artist} ${t.title||''} ${chartName}`.toLowerCase();
    tr.dataset.genres = [t.genre_1,t.genre_2,t.genre_3].filter(Boolean).join(',');
    // Build genre pills for this track
    const genreTags = [t.genre_1, t.genre_2, t.genre_3].filter(Boolean);
    const genreColors = {
      'rock':'var(--crimson)','classic rock':'var(--crimson)','hard rock':'var(--crimson)',
      'country':'var(--green)','country rock':'var(--green)','americana':'var(--green)',
      'pop':'var(--pink)','pop rock':'var(--pink)','dance-pop':'var(--pink)',
      'r&b':'var(--purple)','soul':'var(--purple)','funk':'var(--purple)',
      'hip hop':'var(--orange)','hip-hop':'var(--orange)','rap':'var(--orange)',
      'electronic':'var(--blue)','dance':'var(--blue)','edm':'var(--blue)',
      'metal':'var(--red)','heavy metal':'var(--red)',
      'jazz':'var(--amber)','blues':'var(--teal)',
      'folk':'var(--green)','indie':'var(--blue)',
      'classical':'var(--gold)','christian':'var(--purple)','ccm':'var(--purple)',
      'contemporary christian':'var(--purple)','gospel':'var(--purple)',
      'christian rock':'var(--purple)','new wave':'var(--blue)',
      'alternative':'var(--teal)','alternative rock':'var(--teal)',
    };
    const genrePillsHTML = genreTags.length
      ? genreTags.map(g => {
          const c = genreColors[g.toLowerCase()] || 'var(--muted)';
          return `<span style="display:inline-block;font-family:var(--mono);font-size:.48rem;padding:.1rem .35rem;border-radius:6px;background:${c}22;border:1px solid ${c}44;color:${c};white-space:nowrap;margin:.1rem .15rem .1rem 0">${esc(g)}</span>`;
        }).join('')
      : '<span style="color:var(--muted2);font-size:.5rem">—</span>';

    tr.style.borderBottom = '1px solid var(--border)';
    tr.innerHTML = `
      <td style="padding:.45rem .75rem"><input type="checkbox" onchange="grmToggleTrack(${t.track_id},this.checked)" style="accent-color:var(--pink)"></td>
      <td style="padding:.45rem .75rem;color:var(--muted)">${esc(artist)}</td>
      <td style="padding:.45rem .75rem;color:var(--text)">
        ${gePlayBtn({track_id: t.track_id, ext: filePath.includes('.') ? filePath.slice(filePath.lastIndexOf('.')).toLowerCase() : ''})}${esc(t.title||'')}${filePath
          ? `<button onclick="grmCopyPath('${esc(filePath)}',this)" title="Copy file path to clipboard"
              style="margin-left:.35rem;background:none;border:none;color:var(--muted2);cursor:pointer;
              font-size:.65rem;padding:.05rem .2rem;border-radius:3px;vertical-align:middle;
              transition:color .15s" onmouseover="this.style.color='var(--teal)'"
              onmouseout="this.style.color='var(--muted2)'">📋</button>`
          : ''}
      </td>
      <td class="grm-gcell" style="padding:.45rem .75rem;max-width:180px" onclick="grmGenreEdit(${t.track_id})" title="Click to change this song's genre">${genrePillsHTML}<span class="grm-gedit">✎</span></td>
      <td style="padding:.45rem .75rem;color:var(--muted)">${t.chart_year||t.year||'—'}</td>
      <td style="padding:.45rem .75rem">${starsHTML}</td>
      <td style="padding:.45rem .75rem;white-space:nowrap">${scoreHTML}</td>
      <td style="padding:.45rem .75rem">${peakHTML}</td>
      <td style="padding:.45rem .75rem">${wBar}</td>
      <td style="padding:.45rem .75rem">${chartBadge}</td>
      <td style="padding:.45rem .75rem">${confLabel}</td>`;
    _grmRenderedIds.add(t.track_id);
    tbody.appendChild(tr);
  });
  grmSyncBar();
}

let _sortKey = 'score';
let _sortAsc  = true;

function grmFilter(q) {
  const lq = q.toLowerCase();
  const activeGenre = document.getElementById('grm-genre-filter')?.value || '';
  document.querySelectorAll('#grm-tbody tr[data-id]').forEach(tr => {
    const textMatch  = tr.dataset.text?.includes(lq);
    const genreMatch = !activeGenre || tr.dataset.genres?.split(',').includes(activeGenre);
    tr.style.display = (textMatch && genreMatch) ? '' : 'none';
  });
}

function grmApplyGenreFilter() {
  grmFilter(document.querySelector('#panel-groomer input[placeholder="Filter results…"]')?.value || '');
}

function grmBuildGenreDropdown() {
  const sel = document.getElementById('grm-genre-filter');
  if (!sel) return;
  // Collect all unique genres from current result set
  const genres = new Set();
  grmResults.forEach(t => {
    [t.genre_1, t.genre_2, t.genre_3].forEach(g => { if (g) genres.add(g); });
  });
  if (genres.size === 0) { sel.style.display = 'none'; return; }
  // Rebuild options
  sel.innerHTML = '<option value="">All Genres</option>';
  [...genres].sort().forEach(g => {
    const opt = document.createElement('option');
    opt.value = g; opt.textContent = g;
    sel.appendChild(opt);
  });
  sel.style.display = '';
}

const _confRank = {high:0, medium:1, low:2};
const _sortFields = {
  artist: t => (t.tag_artist || t.artist || '').toLowerCase(),
  title:  t => (t.title  || '').toLowerCase(),
  year:   t => parseInt(t.chart_year || t.year) || 9999,
  stars:  t => -(t.star_rating || t.stars || 0),
  peak:   t => t.hot100_peak || t.peak_position || 9999,
  weeks:  t => -(t.hot100_weeks || t.weeks_on_chart || 0),
  conf:   t => _confRank[t.confidence] ?? 9,
  genre:  t => (t.genre_1 || '').toLowerCase(),
  score:  t => -(t.hit_score || 0),
  seq:    t => (t._day || 0) * 100000 + (t._seq || 99999),
};

function grmSort(key) {
  if (_sortKey === key) { _sortAsc = !_sortAsc; }
  else { _sortKey = key; _sortAsc = (key === 'artist' || key === 'title'); }
  // Update indicators
  ['artist','title','year','stars','score','peak','weeks','conf','genre'].forEach(k => {
    const el = document.getElementById('sort-ind-' + k);
    if (el) el.textContent = k === _sortKey ? (_sortAsc ? ' ▲' : ' ▼') : '';
  });
  grmRenderResults(grmResults);
}
function grmToggleTrack(id, checked) {
  if (checked) grmSelected.add(id); else grmSelected.delete(id);
  grmSyncBar();
}
function grmSelectAll(checked) {
  document.querySelectorAll('#grm-tbody input[type="checkbox"]').forEach(cb => {
    cb.checked = checked;
    const m = cb.getAttribute('onchange').match(/\d+/);
    if (m) { const id=parseInt(m[0]); if(checked) grmSelected.add(id); else grmSelected.delete(id); }
  });
  grmSyncBar();
}

// ── Push bar ──────────────────────────────────────────────────────
function grmSyncBar() {
  const name  = document.getElementById('grm-playlist-name').value || 'Chart Hits';
  const count = grmSelected.size || grmResults.length;
  document.getElementById('grm-bar-title').textContent = `Playlist: ${name}`;
  document.getElementById('grm-bar-sub').textContent   = `${count} song${count!==1?'s':''} ready to push`;
  const hasResults = grmResults.length > 0;
  const isServer   = grmCurrentMode === 'server';
  const serverMap  = {plex:'grm-push-plex', emby:'grm-push-emby', jellyfin:'grm-push-jf'};

  // Server push buttons — only active if results exist AND this was the scanned server
  Object.entries(serverMap).forEach(([srv, btnId]) => {
    const btn = document.getElementById(btnId);
    if (!btn) return;
    const active = hasResults && isServer && (grmScannedServer === srv || grmScannedServer === null);
    btn.disabled      = !active;
    btn.style.opacity = active ? '1' : '.35';
    btn.style.cursor  = active ? 'pointer' : 'default';
  });

  // M3U always available when there are results
  const m3u = document.getElementById('grm-push-m3u');
  if (m3u) {
    m3u.disabled      = !hasResults;
    m3u.style.opacity = hasResults ? '1' : '.35';
    m3u.style.cursor  = hasResults ? 'pointer' : 'default';
  }

  // Navidrome M3U — requires results AND a configured Navidrome path in Kennel
  const navBtn = document.getElementById('grm-push-m3u-nav');
  if (navBtn) {
    const navEl      = document.getElementById('pathNavidrome');
    const navPath    = navEl ? navEl.value.trim() : '';
    const navActive  = hasResults && navPath.length > 0;
    navBtn.disabled      = !navActive;
    navBtn.style.opacity = navActive ? '1' : '.35';
    navBtn.style.cursor  = navActive ? 'pointer' : 'default';
    navBtn.title = navActive
      ? 'Download M3U with Navidrome Docker paths'
      : 'Configure Navidrome Music Path in The Kennel → Path Translator to enable';
  }
}

// ── Push to server ────────────────────────────────────────────────
async function grmPushPlaylist(server) {
  const jobs = grmPlaylistJobs();
  if (!jobs.length) { toast('No tracks to push.', 'warn'); return; }
  const labels = {plex:'Plex', emby:'Emby', jellyfin:'Jellyfin'};
  const label  = labels[server] || server;
  const total  = jobs.reduce((n, j) => n + j.ids.length, 0);
  const what   = jobs.length > 1 ? `${jobs.length} playlists (${jobs.map(j => j.name).join(', ')})` : `"${jobs[0].name}"`;
  if (!confirm(`Push ${what} — ${total} tracks — to ${label}?\n\nThis replaces any existing playlist with the same name.`)) return;
  ['grm-push-plex','grm-push-emby','grm-push-jf','grm-push-m3u','grm-push-m3u-nav'].forEach(id => {
    const b=document.getElementById(id); if(b){b.disabled=true;b.style.opacity='.35';}
  });
  let ok = 0;
  try {
    for (const job of jobs) {
      grmLog(`Pushing "${job.name}" (${job.ids.length} tracks) to ${label}…`, 'info');
      try {
        const r = await fetch(`${API}/api/groomer/playlist/push`, {
          method:'POST', headers:authHdrs(),
          body:JSON.stringify({server, playlist_name:job.name, track_ids:job.ids}),
        });
        const d = await r.json();
        if (r.ok && d.ok) {
          ok++;
          const nf = d.not_found || 0;
          grmLog(`✓ "${job.name}" → ${label}: ${d.added} added${nf ? ` · ${nf} not found (skipped, never substituted)` : ''}`, 'ok');
        } else {
          grmLog(`✗ "${job.name}" push failed: ${d.detail||d.message||'HTTP '+r.status}`, 'err');
        }
      } catch (e) { grmLog(`✗ "${job.name}" push error: ${e.message}`, 'err'); }
    }
    toast(ok === jobs.length ? `✓ ${ok} playlist${ok>1?'s':''} pushed to ${label}` : `${ok}/${jobs.length} pushed — see log`, ok === jobs.length ? 'success' : 'warn');
  } finally { grmSyncBar(); }
}

// ── M3U Download ──────────────────────────────────────────────────
async function grmDownloadM3U() {
  const jobs = grmPlaylistJobs();
  if (!jobs.length) { toast('No tracks to export.', 'warn'); return; }
  for (const j of jobs) await _grmM3UOne(j.name, j.ids);
}
async function _grmM3UOne(name, trackIds) {
  grmLog(`Generating M3U for ${trackIds.length} tracks…`, 'info');
  try {
    const r = await fetch(`${API}/api/groomer/playlist/m3u`, {
      method:'POST', headers:authHdrs(),
      body:JSON.stringify({track_ids:trackIds, playlist_name:name}),
    });
    if (!r.ok) {
      const d = await r.json();
      const msg = typeof d.detail === 'string' ? d.detail
                : Array.isArray(d.detail) ? d.detail.map(e=>e.msg||JSON.stringify(e)).join('; ')
                : JSON.stringify(d.detail) || `HTTP ${r.status}`;
      throw new Error(msg);
    }
    const blob = await r.blob();
    const url  = URL.createObjectURL(blob);
    const a    = document.createElement('a');
    a.href     = url;
    a.download = name.replace(/\s+/g,'_')+'.m3u';
    document.body.appendChild(a); a.click();
    document.body.removeChild(a); URL.revokeObjectURL(url);
    grmLog(`✓ M3U downloaded: ${a.download}`, 'ok');
    toast('✓ M3U downloaded!', 'success');
  } catch(e) {
    grmLog(`M3U error: ${e.message}`, 'err');
    toast('M3U download failed', 'error');
  }
}

// ── Navidrome M3U Download ────────────────────────────────────────
async function grmDownloadNavidromeM3U() {
  const jobs = grmPlaylistJobs();
  if (!jobs.length) { toast('No tracks to export.', 'warn'); return; }
  for (const j of jobs) await _grmNavM3UOne(j.name, j.ids);
}
async function _grmNavM3UOne(name, trackIds) {
  grmLog(`Generating Navidrome M3U for ${trackIds.length} tracks…`, 'info');
  try {
    const r = await fetch(`${API}/api/groomer/playlist/m3u`, {
      method:'POST', headers:authHdrs(),
      body:JSON.stringify({track_ids:trackIds, playlist_name:name, path_mode:'navidrome'}),
    });
    if (!r.ok) {
      const d = await r.json();
      const msg = typeof d.detail === 'string' ? d.detail
                : Array.isArray(d.detail) ? d.detail.map(e=>e.msg||JSON.stringify(e)).join('; ')
                : JSON.stringify(d.detail) || `HTTP ${r.status}`;
      throw new Error(msg);
    }
    const blob = await r.blob();
    const url  = URL.createObjectURL(blob);
    const a    = document.createElement('a');
    a.href     = url;
    a.download = name.replace(/\s+/g,'_')+'_navidrome.m3u';
    document.body.appendChild(a); a.click();
    document.body.removeChild(a); URL.revokeObjectURL(url);
    grmLog(`✓ Navidrome M3U downloaded: ${a.download}`, 'ok');
    toast('✓ Navidrome M3U downloaded!', 'success');
  } catch(e) {
    grmLog(`Navidrome M3U error: ${e.message}`, 'err');
    toast('Navidrome M3U failed — check Kennel path setting', 'error');
  }
}

// Hook into tab navigation to init Top Dogs when tab is opened
const _origShowTab = typeof showTab === 'function' ? showTab : null;


// ══════════════════════════════════════════════════════════════════════════════
//  SIDEBAR COLLAPSE
// ══════════════════════════════════════════════════════════════════════════════

function toggleSidebar() {
  const nav = document.getElementById('appSidebar');
  const btn = document.getElementById('sidebarToggleBtn');
  if (!nav) return;
  const collapsing = !nav.classList.contains('collapsed');
  nav.classList.toggle('collapsed', collapsing);
  if (btn) btn.textContent = collapsing ? '▶' : '◀ hide';
  if (btn) btn.title       = collapsing ? 'Expand navigation' : 'Collapse navigation';
  try { localStorage.setItem('ch_sidebar', collapsing ? 'collapsed' : 'open'); } catch(e) {}
}

// ══════════════════════════════════════════════════════════════════════════════
//  PANE COLLAPSE & RESIZE
// ══════════════════════════════════════════════════════════════════════════════

function toggleAside(name) {
  const aside = document.getElementById('aside-' + name);
  const btn   = aside ? aside.querySelector('.pane-collapse-btn') : null;
  if (!aside) return;
  const collapsing = !aside.classList.contains('collapsed');
  aside.classList.toggle('collapsed', collapsing);
  if (btn) btn.textContent = collapsing ? '▶' : '◀';
  if (btn) btn.title       = collapsing ? 'Expand panel' : 'Collapse panel';
  try { localStorage.setItem('ch_aside_' + name, collapsing ? 'collapsed' : 'open'); } catch(e) {}
}

function startResize(e, name) {
  e.preventDefault();
  const aside   = document.getElementById('aside-' + name);
  const resizer = document.getElementById('resizer-' + name);
  if (!aside || aside.classList.contains('collapsed')) return;
  resizer.classList.add('dragging');
  const startX = e.clientX;
  const startW = aside.offsetWidth;
  function onMove(ev) {
    const newW = Math.min(600, Math.max(180, startW + ev.clientX - startX));
    aside.style.width = newW + 'px';
  }
  function onUp() {
    resizer.classList.remove('dragging');
    document.removeEventListener('mousemove', onMove);
    document.removeEventListener('mouseup',   onUp);
    try { localStorage.setItem('ch_aside_w_' + name, aside.offsetWidth); } catch(e) {}
  }
  document.addEventListener('mousemove', onMove);
  document.addEventListener('mouseup',   onUp);
}

// Restore all saved states on page load
(function restoreUIState() {
  // Sidebar
  try {
    if (localStorage.getItem('ch_sidebar') === 'collapsed') {
      const nav = document.getElementById('appSidebar');
      const btn = document.getElementById('sidebarToggleBtn');
      if (nav) nav.classList.add('collapsed');
      if (btn) { btn.textContent = '▶'; btn.title = 'Expand navigation'; }
    }
  } catch(e) {}

  // Pane asides
  ['kennel', 'retriever'].forEach(function(name) {
    const aside = document.getElementById('aside-' + name);
    if (!aside) return;
    try {
      const state = localStorage.getItem('ch_aside_' + name);
      const width = localStorage.getItem('ch_aside_w_' + name);
      if (state === 'collapsed') {
        aside.classList.add('collapsed');
        const btn = aside.querySelector('.pane-collapse-btn');
        if (btn) { btn.textContent = '▶'; btn.title = 'Expand panel'; }
      } else if (width) {
        aside.style.width = parseInt(width) + 'px';
      }
    } catch(e) {}
  });
})();




// ── Library-wide hit count for the scan card (M10b) ──────────────────────────
async function grmLibraryHits() {
  try {
    const d = await (await fetch(`${API}/api/groomer/skip_cache/stats`, {headers:authHdrs()})).json();
    const el = document.getElementById('grm-s-rate');
    if (el && d.hit != null) el.textContent = d.hit.toLocaleString();
    const sub = document.getElementById('grm-status-sub');
    if (sub && d.hit != null && /this run$/.test(sub.textContent)) sub.textContent += ` · ${d.hit.toLocaleString()} in your library`;
  } catch (e) {}
}

// ── ✎ Genre editor on Top Dogs (M10b) — same decisions as Genre Review ──────
async function grmGenreEdit(id) {
  try {
    await geTaxonomy();
    const r = await fetch(`${API}/api/retriever/genre-engine/track/${id}`, {headers:authHdrs()});
    const d = await r.json();
    if (!r.ok) throw new Error(d.detail || `HTTP ${r.status}`);
    let box = document.getElementById('grm-gedit-modal');
    if (!box) {
      box = document.createElement('div');
      box.id = 'grm-gedit-modal';
      box.className = 'grm-gmodal';
      box.onclick = e => { if (e.target === box) grmGenreClose(); };
      document.body.appendChild(box);
    }
    const fam = d.family || Object.keys(_geTax.families)[0];
    const famOpts = Object.entries(_geTax.families).map(([k, v]) => `<option value="${k}" ${k === fam ? 'selected' : ''}>${esc(v)}</option>`).join('');
    box.innerHTML = `<div class="grm-gpanel">
      <div class="grm-gtitle">✎ GENRE</div>
      <div class="grm-gsong">${gePlayBtn({track_id: id, ext: d.ext})}<b>${esc(d.artist)}</b> — ${esc(d.title)}<div class="grm-gmeta">💿 ${esc(d.album)} · in file now: ${esc(d.current.join('; ') || '—')}</div></div>
      <div class="ger-editor grm-geditor" data-styles='${JSON.stringify(d.styles || [])}'>
        <select data-fam onchange="geFillStyles(this.parentNode)">${famOpts}</select><select data-st="0"></select><select data-st="1"></select><select data-st="2"></select>
      </div>
      <div class="grm-gscope">Apply to:
        <label><input type="radio" name="grm-gscope" value="song" checked> this song</label>
        <label><input type="radio" name="grm-gscope" value="album"> this album</label>
        <label><input type="radio" name="grm-gscope" value="artist"> every song by ${esc(d.artist || 'this artist')}</label>
      </div>
      <label class="grm-gwrite"><input type="checkbox" id="grm-gwrite" checked> Write to the music file now <span style="color:var(--muted2)">(GENRE tag only)</span></label>
      <div class="grm-gbtns"><button class="btn btn-outline btn-sm" onclick="grmGenreClose()">CANCEL</button>
        <button class="btn btn-sm" style="background:var(--pink);color:#000" onclick="grmGenreSave(${id}, ${JSON.stringify(d.artist).replace(/"/g, '&quot;')})">SAVE</button></div>
    </div>`;
    geFillStyles(box.querySelector('.ger-editor'), d.styles || []);
    box.style.display = 'flex';
  } catch (e) { toast(e.message, 'error'); }
}
function grmGenreClose() {
  gePlayStop();
  const b = document.getElementById('grm-gedit-modal'); if (b) b.style.display = 'none';
}
async function grmGenreSave(id, artist) {
  const box = document.getElementById('grm-gedit-modal');
  const ed = _geReadEditor(box.querySelector('.ger-editor'));
  const scope = box.querySelector('input[name="grm-gscope"]:checked').value;
  const body = {...ed, ...(scope === 'song' ? {track_ids: [id]} : scope === 'album' ? {album_of: id} : {artist})};
  if (scope === 'artist' && !confirm(`Set this genre for EVERY song by ${artist}?`)) return;
  try {
    const r = await fetch(`${API}/api/retriever/genre-engine/override`, {method:'PUT', headers:authHdrs(), body:JSON.stringify(body)});
    const d = await r.json();
    if (!r.ok) throw new Error(d.detail || `HTTP ${r.status}`);
    const ids = d.track_ids || [id];
    const g = d.genre.slice(0, 3);
    // Show the new genre in the table right away
    ids.forEach(tid => {
      const tr = document.querySelector(`#grm-tbody tr[data-id="${tid}"]`);
      if (!tr) return;
      const cell = tr.querySelector('.grm-gcell');
      if (cell) cell.innerHTML = g.map(x => `<span class="grm-gpill">${esc(x)}</span>`).join('') + '<span class="grm-gedit">✎</span>';
      tr.dataset.genres = g.join(',');
    });
    let msg = `Saved: ${d.genre.join('; ')} — ${d.updated} song${d.updated > 1 ? 's' : ''}`;
    if (document.getElementById('grm-gwrite').checked) {
      const w = await fetch(`${API}/api/retriever/online-genres/apply`, {method:'POST', headers:authHdrs(),
        body: JSON.stringify({scope: 'all', track_ids: ids})});
      msg += w.ok ? ' · writing to file' + (ids.length > 1 ? 's' : '') + '…' : ' · file write busy — use Review & Write later';
    }
    toast(msg, 'success');
    grmGenreClose();
  } catch (e) { toast(e.message, 'error'); }
}
