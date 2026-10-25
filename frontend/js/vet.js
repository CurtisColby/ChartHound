'use strict';
// ══════════════════════════════════════════════════════════════════════════════
//  THE VETERINARIAN
// ══════════════════════════════════════════════════════════════════════════════
let _vetDbgPoll = null;
let _vetDbgRunning = false;

function vetInit() {
  vetLoadHealth();
  vetLoadSkipCache();
  vetLoadLibraryCache();
  vetLoadStaticSources();
  vetLoadBackups();
}

// ── Static DB Sources (user-imported chart data) ──────────────────────────
var _vetImportPollTimer = null;

async function vetLoadStaticSources() {
  var list = document.getElementById('vet-src-list');
  if (!list) return;
  try {
    var r = await fetch(`${API}/api/groomer/vet/static-sources`, {headers:authHdrs()});
    if (!r.ok) {
      list.innerHTML = '<div style="padding:.4rem 0;font-family:var(--mono);font-size:.55rem;color:var(--red)">Failed to load sources</div>';
      return;
    }
    var d = await r.json();
    var html = '';
    (d.sources || []).forEach(function(s) {
      var loaded = s.loaded;
      var statusHtml, btnHtml;
      if (loaded) {
        // Format last-imported date as a short "Apr 12" string; show stale badge if > 30 days.
        var dateSuffix = '';
        var staleBadge = '';
        if (s.last_imported) {
          try {
            var dt = new Date(String(s.last_imported).replace(' ', 'T') + 'Z');
            if (!isNaN(dt.getTime())) {
              var MO = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
              dateSuffix = ' · ' + MO[dt.getUTCMonth()] + ' ' + dt.getUTCDate();
            }
          } catch(_) {}
        }
        if (s.archive) {
          staleBadge = ' <span style="color:var(--teal);font-family:var(--mono);font-size:.5rem;padding:.05rem .3rem;border:1px solid var(--teal);border-radius:3px;margin-left:.25rem" title="Historical data — it does not change, so it never needs refreshing.">ARCHIVE</span>';
        } else if (s.failing) {
          staleBadge = ' <span style="color:var(--red);font-family:var(--mono);font-size:.5rem;padding:.05rem .3rem;border:1px solid var(--red);border-radius:3px;margin-left:.25rem" title="The last automatic refresh failed (the website may be blocking it). Existing data is still used. Auto-retry in 7 days; ↻ retries now.">✕ REFRESH FAILING</span>';
        } else if (s.stale) {
          staleBadge = ' <span style="color:var(--amber);font-family:var(--mono);font-size:.5rem;padding:.05rem .3rem;border:1px solid var(--amber);border-radius:3px;margin-left:.25rem" title="This source was imported more than 30 days ago. Click IMPORT again to refresh.">⚠ STALE</span>';
        }
        statusHtml = '<span class="vault-row-status ok">Loaded · ' + s.entries.toLocaleString() + ' entries' + dateSuffix + '</span>' + staleBadge;
        btnHtml = '<button class="btn btn-outline btn-sm" onclick="vetImportSource(\'' + s.id + '\',\'' + esc(s.label) + '\')" style="font-size:.5rem;padding:.15rem .45rem;margin-left:.5rem;' + (s.stale ? 'border-color:var(--amber);color:var(--amber)' : '') + '" title="Re-import to refresh">↻</button>'
                + '<button class="btn btn-outline btn-sm" onclick="vetDeleteSource(\'' + s.id + '\',\'' + esc(s.label) + '\')" style="font-size:.5rem;padding:.15rem .45rem;margin-left:.25rem" title="Remove this source">✕</button>';
      } else {
        statusHtml = '<span style="font-family:var(--mono);font-size:.55rem;color:var(--amber)">Not loaded</span>';
        btnHtml = '<button class="btn btn-outline btn-sm" onclick="vetImportSource(\'' + s.id + '\',\'' + esc(s.label) + '\')" style="font-size:.5rem;padding:.15rem .55rem;margin-left:.5rem;border-color:var(--green);color:var(--green)" title="Import this source">⬇ IMPORT</button>';
      }
      html += '<div class="vault-row">'
           +   '<div class="vault-row-label">' + (loaded ? '✅' : '⬜') + ' ' + esc(s.label) + '</div>'
           +   '<div style="display:flex;align-items:center;gap:.25rem">' + statusHtml + btnHtml + '</div>'
           + '</div>';
    });
    list.innerHTML = html;
    // Refresh Top Dogs chart-database card too, since totals changed
    if (typeof grmLoadStats === 'function') grmLoadStats();
  } catch(e) {
    list.innerHTML = '<div style="padding:.4rem 0;font-family:var(--mono);font-size:.55rem;color:var(--red)">Error: ' + esc(e.message) + '</div>';
  }
}

async function vetImportSource(sourceId, label) {
  if (!confirm('Import "' + label + '"?\n\nThis downloads chart data and appends it to your dynamic database. Your music files and static DB are not touched. Takes 10–60 seconds depending on source size.')) return;
  var statusEl = document.getElementById('vet-import-status');
  try {
    var r = await fetch(`${API}/api/groomer/vet/static-sources/import`, {
      method:'POST', headers:authHdrs(),
      body: JSON.stringify({source_id: sourceId})
    });
    var d = await r.json();
    if (!r.ok) {
      toast('Import failed: ' + (d.detail || 'unknown'), 'error');
      return;
    }
    toast('Import started: ' + label, 'ok');
    if (statusEl) statusEl.textContent = '⏳ Importing ' + label + '…';
    // Poll /status every 2s until done/error
    clearInterval(_vetImportPollTimer);
    _vetImportPollTimer = setInterval(async function() {
      try {
        var sr = await fetch(`${API}/api/groomer/vet/static-sources/status`, {headers:authHdrs()});
        var sd = await sr.json();
        if (sd.status === 'done') {
          clearInterval(_vetImportPollTimer);
          if (statusEl) statusEl.textContent = '✓ ' + (sd.message || 'Import complete');
          toast('✓ ' + label + ' imported', 'ok');
          vetLoadStaticSources();
          vetLoadHealth();
        } else if (sd.status === 'error') {
          clearInterval(_vetImportPollTimer);
          if (statusEl) statusEl.textContent = '✗ ' + (sd.message || 'Import failed');
          toast('Import failed: ' + (sd.message || 'unknown'), 'error');
        } else if (sd.status === 'running') {
          if (statusEl) statusEl.textContent = '⏳ Importing ' + label + '… (' + (sd.inserted || 0).toLocaleString() + ' so far)';
        }
      } catch(e) { /* keep polling */ }
    }, 2000);
  } catch(e) {
    toast('Error: ' + e.message, 'error');
  }
}

async function vetDeleteSource(sourceId, label) {
  if (!confirm('Remove "' + label + '" from your database?\n\nThis deletes all imported chart entries for this source. Your shipped static DB is not touched. You can re-import later.')) return;
  try {
    var r = await fetch(`${API}/api/groomer/vet/static-sources/delete`, {
      method:'POST', headers:authHdrs(),
      body: JSON.stringify({source_id: sourceId})
    });
    var d = await r.json();
    if (r.ok) {
      toast(label + ' removed (' + (d.deleted || 0).toLocaleString() + ' rows)', 'ok');
      vetLoadStaticSources();
      vetLoadHealth();
    } else {
      toast('Delete failed: ' + (d.detail || 'unknown'), 'error');
    }
  } catch(e) { toast('Error: ' + e.message, 'error'); }
}

async function vetLoadHealth() {
  try {
    const r = await fetch(`${API}/api/groomer/vet/db_health`, {headers:authHdrs()});
    if (!r.ok) return;
    const d = await r.json();
    // Dynamic DB
    const setT = (id, v) => { const el = document.getElementById(id); if (el) el.textContent = v; };
    setT('vet-dyn-size',       d.dynamic_size || '—');
    setT('vet-tracks-count',   (d.tracks || 0).toLocaleString());
    setT('vet-artists-count',  (d.artists || 0).toLocaleString());
    setT('vet-albums-count',   (d.albums || 0).toLocaleString());
    setT('vet-chartdata-count',(d.chart_data || 0).toLocaleString());
    setT('vet-conn-count',     (d.connections || 0).toLocaleString());
    // Static DB
    setT('vet-static-size',    d.static_size || '—');
    setT('vet-chartref-count', (d.chart_reference || 0).toLocaleString());
    setT('vet-billpop-count',  (d.billboard_pop || 0).toLocaleString());
  } catch {}
}

async function vetLoadSkipCache() {
  try {
    const r = await fetch(`${API}/api/groomer/skip_cache/stats`, {headers:authHdrs()});
    if (!r.ok) return;
    const d = await r.json();
    const setT = (id, v) => { const el = document.getElementById(id); if (el) el.textContent = v; };
    setT('vet-sc-hit',       (d.hit || 0).toLocaleString());
    setT('vet-sc-miss',      (d.miss || 0).toLocaleString());
    setT('vet-sc-unchecked', (d.unchecked || 0).toLocaleString());
  } catch {}
}

// ── Library Cache sync ────────────────────────────────────────────────────
let _lcPollTimer = null;

async function vetLoadLibraryCache() {
  try {
    const r = await fetch(`${API}/api/sniffer/sync-status`, {headers: authHdrs()});
    if (!r.ok) { console.warn('vetLoadLibraryCache: HTTP', r.status); return; }
    const d = await r.json();
    const count = d.cached_count || d.track_count || 0;
    const lcCount = document.getElementById('vet-lc-count');
    const lcSource = document.getElementById('vet-lc-source');
    const lcSynced = document.getElementById('vet-lc-synced');
    const lcStale  = document.getElementById('vet-lc-stale-warn');
    if (lcCount)  lcCount.textContent  = count > 0 ? count.toLocaleString() : '—';

    const srcMap = {plex:'Plex', emby:'Emby', jellyfin:'Jellyfin',
                    file_scan:'File Scan', scanned_only:'Retriever Scan', cache:'Cached'};
    if (lcSource) lcSource.textContent = srcMap[d.source] || (d.source || '—');

    if (d.last_synced) {
      if (lcSynced) lcSynced.textContent = _timeAgo(d.last_synced);
      const hrs = (Date.now() - new Date(d.last_synced).getTime()) / 3600000;
      if (lcStale) lcStale.style.display = hrs > 24 ? 'block' : 'none';
    } else {
      if (lcSynced) lcSynced.textContent = 'Never';
      if (lcStale)  lcStale.style.display = 'none';
    }
  } catch(e) { console.warn('vetLoadLibraryCache error:', e); }
}

function _timeAgo(isoStr) {
  const ms = Date.now() - new Date(isoStr).getTime();
  const mins = Math.floor(ms / 60000);
  if (mins < 2)  return 'Just now';
  if (mins < 60) return `${mins}m ago`;
  const hrs = Math.floor(mins / 60);
  if (hrs < 24)  return `${hrs}h ago`;
  return `${Math.floor(hrs/24)}d ago`;
}

async function vetSyncLibrary() {
  const btn = document.getElementById('vet-lc-sync-btn');
  btn.disabled = true;
  btn.textContent = '⏳ SYNCING…';
  document.getElementById('vet-lc-progress-wrap').style.display = 'block';
  document.getElementById('vet-lc-progress-bar').style.width = '0%';
  document.getElementById('vet-lc-progress-label').textContent = 'Starting sync…';

  try {
    const r = await fetch(`${API}/api/sniffer/sync-library`, {method:'POST', headers:authHdrs()});
    const d = await r.json();
    if (!r.ok) {
      toast(d.detail || 'Sync failed', 'error');
      btn.disabled = false;
      btn.textContent = '🔄 SYNC LIBRARY CACHE';
      document.getElementById('vet-lc-progress-wrap').style.display = 'none';
      return;
    }
  } catch(e) {
    toast('Sync error: ' + e.message, 'error');
    btn.disabled = false;
    btn.textContent = '🔄 SYNC LIBRARY CACHE';
    document.getElementById('vet-lc-progress-wrap').style.display = 'none';
    return;
  }

  // Poll for progress
  if (_lcPollTimer) clearInterval(_lcPollTimer);
  _lcPollTimer = setInterval(async () => {
    try {
      const r = await fetch(`${API}/api/sniffer/sync-status`, {headers: authHdrs()});
      const d = await r.json();
      const pct = d.progress_pct || 0;
      document.getElementById('vet-lc-progress-bar').style.width = pct + '%';
      const count = d.track_count || 0;
      document.getElementById('vet-lc-progress-label').textContent =
        `${count.toLocaleString()} tracks indexed… ${pct}%`;

      if (d.status === 'done' || d.status === 'error') {
        clearInterval(_lcPollTimer);
        _lcPollTimer = null;
        document.getElementById('vet-lc-progress-wrap').style.display = 'none';
        btn.disabled = false;
        btn.textContent = '🔄 SYNC LIBRARY CACHE';
        if (d.status === 'done') {
          toast(`Library cache synced — ${(d.track_count||0).toLocaleString()} tracks (${d.source || 'unknown'})`, 'ok');
        } else {
          toast('Sync failed: ' + (d.error || 'unknown error'), 'error');
        }
        vetLoadLibraryCache();
      }
    } catch(e) { /* silent */ }
  }, 2000);
}

async function vetResetSkipCache() {
  if (!confirm('Reset the skip cache?\n\nAll tracks will be marked as unchecked. The next Top Dogs scan will do a full lookup on every track instead of skipping known misses.\n\nThis is safe — no data is deleted.')) return;
  try {
    const r = await fetch(`${API}/api/groomer/skip_cache/reset`, {method:'POST', headers:authHdrs()});
    const d = await r.json();
    if (r.ok) {
      toast(`Skip cache reset — ${(d.cleared||0).toLocaleString()} tracks cleared.`, 'ok');
      vetLoadSkipCache();
    } else {
      toast('Reset failed: ' + (d.detail || 'unknown error'), 'error');
    }
  } catch(e) { toast('Error: ' + e.message, 'error'); }
}

async function vetVacuum() {
  if (!confirm('Run VACUUM on the dynamic database?\n\nThis reclaims disk space and optimizes performance. It may take a few seconds on large databases. ChartHound remains usable during this operation.')) return;
  toast('Running VACUUM…', 'info');
  try {
    const r = await fetch(`${API}/api/groomer/vet/vacuum`, {method:'POST', headers:authHdrs()});
    const d = await r.json();
    if (r.ok) {
      toast(`VACUUM complete — ${d.before_size} → ${d.after_size}`, 'ok');
      vetLoadHealth();
    } else {
      toast('VACUUM failed: ' + (d.detail || 'unknown error'), 'error');
    }
  } catch(e) { toast('Error: ' + e.message, 'error'); }
}

async function vetIntegrityCheck() {
  toast('Running integrity check…', 'info');
  try {
    const r = await fetch(`${API}/api/groomer/vet/integrity_check`, {method:'POST', headers:authHdrs()});
    const d = await r.json();
    if (r.ok && d.ok) {
      toast('✓ Database integrity check passed — no issues found.', 'ok');
    } else if (r.ok) {
      toast('⚠ Integrity issues found: ' + (d.result || 'unknown'), 'warn');
    } else {
      toast('Check failed: ' + (d.detail || 'unknown error'), 'error');
    }
  } catch(e) { toast('Error: ' + e.message, 'error'); }
}

// ── Vet Debug Console (capped at 1,000 lines, off by default) ────────────────

function vetDbgToggle() {
  if (_vetDbgRunning) {
    _vetDbgRunning = false;
    if (_vetDbgPoll) { clearInterval(_vetDbgPoll); _vetDbgPoll = null; }
    document.getElementById('vet-dbg-toggle').textContent = '▶ START';
    document.getElementById('vet-dbg-status').textContent = 'OFF';
    document.getElementById('vet-dbg-status').style.color = 'var(--red)';
  } else {
    _vetDbgRunning = true;
    document.getElementById('vet-dbg-toggle').textContent = '⏸ STOP';
    document.getElementById('vet-dbg-status').textContent = 'LIVE';
    document.getElementById('vet-dbg-status').style.color = 'var(--green)';
    vetDbgLoad();
    _vetDbgPoll = setInterval(vetDbgLoad, 3000);
  }
}

async function vetDbgLoad() {
  const level = document.getElementById('vet-dbg-level')?.value || '';
  const name  = document.getElementById('vet-dbg-filter')?.value || '';
  try {
    let url = `${API}/api/debug/lines?limit=1000`;
    if (level) url += `&level=${level}`;
    if (name)  url += `&name=${encodeURIComponent(name)}`;
    const r = await fetch(url, {headers:authHdrs()});
    if (!r.ok) return;
    const d = await r.json();
    const el = document.getElementById('vet-dbg-log');
    if (!el) return;
    const lines = d.lines || [];
    document.getElementById('vet-dbg-count').textContent = `${lines.length} entries · max 1,000`;
    const levelColors = {
      'DEBUG':'var(--muted2)','INFO':'var(--text)','WARNING':'var(--amber)',
      'ERROR':'var(--red)','CRITICAL':'var(--red)'
    };
    el.innerHTML = lines.map(l => {
      const c = levelColors[l.level] || 'var(--muted)';
      return `<div style="color:${c}">[${l.ts}] ${l.level} ${l.name}: ${esc(l.msg)}</div>`;
    }).join('');
    el.scrollTop = el.scrollHeight;
  } catch {}
}

function vetDbgClear() {
  const el = document.getElementById('vet-dbg-log');
  if (el) el.innerHTML = '<span style="color:var(--muted2)">Console cleared.</span>';
  document.getElementById('vet-dbg-count').textContent = '0 entries · max 1,000';
}

async function vetDbgDownload() {
  try {
    const r = await fetch(`${API}/api/debug/lines?limit=1000`, {headers:authHdrs()});
    if (!r.ok) return;
    const d = await r.json();
    const lines = (d.lines || []).map(l => `[${l.ts}] ${l.level} ${l.name}: ${l.msg}`).join('\n');
    const blob = new Blob([lines], {type:'text/plain'});
    const url  = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url; a.download = `charthound_debug_${new Date().toISOString().slice(0,10)}.log`;
    document.body.appendChild(a); a.click(); document.body.removeChild(a);
    URL.revokeObjectURL(url);
    toast('Debug log downloaded.', 'ok');
  } catch(e) { toast('Download failed: ' + e.message, 'error'); }
}


// ══════════════════════════════════════════════════════════════════════════════
//  DEBUG LOG
// ══════════════════════════════════════════════════════════════════════════════
let _dbgPoll = null;
const DBG_LEVEL_COLOR = {
  DEBUG:    'color:var(--muted2)',
  INFO:     'color:var(--text)',
  WARNING:  'color:var(--amber)',
  ERROR:    'color:var(--red)',
  CRITICAL: 'color:var(--red)',
};

function dbgInit() {
  dbgLoad();
  if (document.getElementById('dbg-auto')?.checked) dbgStartPoll();
}

function dbgToggleAuto() {
  if (document.getElementById('dbg-auto')?.checked) {
    dbgStartPoll();
  } else {
    if (_dbgPoll) { clearInterval(_dbgPoll); _dbgPoll = null; }
  }
}

function dbgStartPoll() {
  if (_dbgPoll) clearInterval(_dbgPoll);
  _dbgPoll = setInterval(dbgLoad, 2000);
}

async function dbgLoad() {
  const level = document.getElementById('dbg-level')?.value || '';
  const name  = document.getElementById('dbg-name')?.value || '';
  const params = new URLSearchParams({n: 500});
  if (level) params.set('level', level);
  if (name)  params.set('name', name);
  try {
    const r = await fetch(`${API}/api/debug/lines?${params}`, {headers:authHdrs()});
    if (!r.ok) return;
    const d = await r.json();
    const el = document.getElementById('dbg-log');
    const count = document.getElementById('dbg-count');
    const stats = document.getElementById('dbg-stats');
    if (!el) return;

    // Update count badge
    if (count) count.textContent = `${d.total.toLocaleString()} entries`;

    // Update stats
    if (stats && d.by_level) {
      stats.textContent = Object.entries(d.by_level)
        .map(([l,n]) => `${l}: ${n}`).join('  ·  ');
    }

    // Render log lines
    const wasAtBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 40;
    el.innerHTML = d.lines.map(e => {
      const style = DBG_LEVEL_COLOR[e.level] || 'color:var(--text)';
      const nameStyle = 'color:#6366f1;opacity:.7';
      return `<div style="border-bottom:1px solid var(--border);padding:.15rem 0;${style}">` +
        `<span style="color:var(--muted2)">[${e.ts}]</span> ` +
        `<span style="font-weight:bold">[${e.level.padEnd(7)}]</span> ` +
        `<span style="${nameStyle}">[${e.name.padEnd(10)}]</span> ` +
        `${esc(e.msg)}</div>`;
    }).join('');

    // Auto-scroll to bottom if was already at bottom
    if (wasAtBottom) el.scrollTop = el.scrollHeight;

  } catch(e) {
    const el = document.getElementById('dbg-log');
    if (el) el.innerHTML = `<span style="color:var(--red)">Failed to load log: ${esc(e.message)}</span>`;
  }
}

async function dbgClear() {
  if (!confirm('Clear the debug log buffer?')) return;
  try {
    await fetch(`${API}/api/debug/clear`, {method:'POST', headers:authHdrs()});
    dbgLoad();
    toast('Debug log cleared', 'ok');
  } catch(e) { toast(`Clear failed: ${e.message}`, 'error'); }
}

async function dbgDownload() {
  const level = document.getElementById('dbg-level')?.value || '';
  const params = level ? `?level=${level}` : '';
  try {
    const r = await fetch(`${API}/api/debug/download${params}`, {headers:authHdrs()});
    if (!r.ok) { toast('Download failed', 'error'); return; }
    const blob = await r.blob();
    const cd   = r.headers.get('Content-Disposition') || '';
    const fn   = cd.match(/filename="([^"]+)"/)?.[1] || 'charthound_debug.txt';
    const url  = URL.createObjectURL(blob);
    const a    = document.createElement('a');
    a.href = url; a.download = fn;
    document.body.appendChild(a); a.click();
    document.body.removeChild(a); URL.revokeObjectURL(url);
    toast('✓ Debug log downloaded', 'ok');
  } catch(e) { toast(`Download failed: ${e.message}`, 'error'); }
}

// Stop auto-poll when leaving debug tab
const _origSwitchTab = switchTab;
// Patch switchTab to stop debug poll when leaving


// ── Database backups ──
function _vetBkRow(b, i) {
  const d = new Date(b.created.replace(' ', 'T') + 'Z');
  const when = isNaN(d) ? b.created : d.toLocaleString(undefined, {month:'short', day:'numeric', year:'numeric', hour:'numeric', minute:'2-digit'});
  return `<div class="vault-row"><div class="vault-row-label">${i === 0 ? '🟢 ' : ''}${esc(when)}</div>
    <div class="vault-row-status" style="color:var(--text);display:flex;gap:.75rem;align-items:center">${esc(b.size)}
    <a href="#" onclick="vetBackupDownload('${esc(b.name)}');return false" style="color:var(--teal)" data-tip="Download this backup to the computer you're using, to keep a copy off the server.">⬇</a></div></div>`;
}
async function vetLoadBackups() {
  const box = document.getElementById('vet-bk-list'); if (!box) return;
  try {
    const r = await fetch(`${API}/api/groomer/vet/backups`, {headers: authHdrs()});
    const d = await r.json();
    if (!r.ok) throw new Error(d.detail || `HTTP ${r.status}`);
    box.innerHTML = d.backups.length ? d.backups.map(_vetBkRow).join('')
      : '<div class="field-hint" style="margin:0">No backups yet — click BACK UP NOW.</div>';
  } catch (e) { box.innerHTML = `<div class="field-hint" style="margin:0;color:var(--red)">${esc(e.message)}</div>`; }
}
async function vetBackupNow() {
  const btn = document.getElementById('vet-bk-btn'); const t = btn.textContent;
  btn.disabled = true; btn.textContent = 'BACKING UP…';
  try {
    const r = await fetch(`${API}/api/groomer/vet/backup`, {method: 'POST', headers: authHdrs()});
    const d = await r.json();
    if (!r.ok) throw new Error(d.detail || `HTTP ${r.status}`);
    toast(`Backup saved — ${d.size}`, 'ok');
    vetLoadBackups();
  } catch (e) { toast('Backup failed: ' + e.message, 'error'); }
  finally { btn.disabled = false; btn.textContent = t; }
}
async function vetBackupDownload(name) {
  try {
    const r = await fetch(`${API}/api/groomer/vet/backups/${encodeURIComponent(name)}/token`, {method: 'POST', headers: authHdrs()});
    const d = await r.json();
    if (!r.ok) throw new Error(d.detail || `HTTP ${r.status}`);
    const a = document.createElement('a');
    a.href = `${API}/api/groomer/vet/backups/${encodeURIComponent(name)}/download?t=${encodeURIComponent(d.t)}`;
    a.download = name; document.body.appendChild(a); a.click(); a.remove();
  } catch (e) { toast('Download failed: ' + e.message, 'error'); }
}

// ── Fix Moved Files (M10e) ───────────────────────────────────────────────────
let _mvPoll = null;
async function vetMovedCheck() {
  try {
    const r = await fetch(`${API}/api/groomer/vet/moved/check`, {method:'POST', headers:authHdrs()});
    const d = await r.json();
    if (!r.ok) throw new Error(d.detail || `HTTP ${r.status}`);
    document.getElementById('mv-preview').innerHTML = '';
    document.getElementById('mv-apply').style.display = 'none';
    _vetMovedPoll();
  } catch (e) { toast(e.message, 'error'); }
}
function _vetMovedPoll() {
  clearTimeout(_mvPoll);
  _mvPoll = setTimeout(async () => {
    try {
      const d = await (await fetch(`${API}/api/groomer/vet/moved/status`, {headers:authHdrs()})).json();
      const st = document.getElementById('mv-status');
      if (d.status === 'running') {
        st.textContent = d.phase === 'checking' ? `Checking files… ${d.checked.toLocaleString()} / ${d.total.toLocaleString()}` : (d.message || 'Searching…');
        return _vetMovedPoll();
      }
      st.textContent = d.message || '';
      if (d.status === 'done' && d.preview) _vetMovedShow(d.preview);
    } catch (e) { _vetMovedPoll(); }
  }, 1500);
}
function _vetMovedShow(p) {
  const esc2 = s => String(s || '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
  const list = (rows, showNew) => rows.map(r => `<div style="margin:.15rem 0 .35rem .6rem;line-height:1.5"><span style="color:var(--text)">${esc2(r.artist)} — ${esc2(r.title)}</span><br>
      <span style="color:var(--muted2)">${esc2(r.old)}</span>${showNew && r.new ? `<br><span style="color:var(--teal)">→ ${esc2(r.new)}</span>` : ''}</div>`).join('');
  const sec = (n, label, color, rows, showNew, tip) => n ? `<details style="margin-top:.35rem"><summary style="cursor:pointer;color:${color}" title="${tip}">${n.toLocaleString()} ${label}</summary>${list(rows, showNew)}${n > rows.length ? `<div style="color:var(--muted2);margin-left:.6rem">…and ${(n - rows.length).toLocaleString()} more</div>` : ''}</details>` : '';
  const total = p.found + p.dup + p.gone + p.back;
  document.getElementById('mv-preview').innerHTML = !total
    ? '<span style="color:var(--green)">✓ Every song is where ChartHound expects it.</span>'
    : sec(p.found, 'moved — found in a new place (will be relinked)', 'var(--teal)', p.examples.found, true, 'The saved path is updated; genre edits and history stay attached.')
    + sec(p.dup, 'moved — a scan already added the new copy (will be merged)', 'var(--amber)', p.examples.dup, true, 'Your genre edit is copied to the new copy and the old entry is hidden.')
    + sec(p.gone, 'gone — not found, or more than one possible match (will be hidden)', 'var(--red)', p.examples.gone, false, 'Hidden from every list. If the file comes back, the next CHECK brings it back.')
    + (p.back ? `<div style="margin-top:.35rem;color:var(--green)">${p.back.toLocaleString()} hidden songs are back (will be shown again)</div>` : '');
  document.getElementById('mv-apply').style.display = total ? '' : 'none';
}
async function vetMovedApply() {
  if (!confirm('Apply these changes to ChartHound\'s database?\n\nYour music files are not touched. Tip: back up first (💾 BACKUPS).')) return;
  try {
    const r = await fetch(`${API}/api/groomer/vet/moved/apply`, {method:'POST', headers:authHdrs()});
    const d = await r.json();
    if (!r.ok) throw new Error(d.detail || `HTTP ${r.status}`);
    toast(d.message, 'success');
    document.getElementById('mv-status').textContent = d.message;
    document.getElementById('mv-preview').innerHTML = '';
    document.getElementById('mv-apply').style.display = 'none';
  } catch (e) { toast(e.message, 'error'); }
}
