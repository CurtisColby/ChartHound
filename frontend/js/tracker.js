'use strict';
// ══════════════════════════════════════════════════════════════════════════════
//  THE TRACKER — Radarr / Sonarr Missing Media Hunter
// ══════════════════════════════════════════════════════════════════════════════

var _trkItems = [];
var _trkFilter = 'all';
var _trkTextFilter = '';
var _trkOffset = 0;
var _trkTotal = 0;
var _trkPollTimer = null;
var _trkInitDone = false;
var _trkSortDir = 'newest-first'; // 'never-first' | 'oldest-first' | 'newest-first'
var _trkNextTickUTC = null;
var _trkCountdownTimer = null;

function _trkStartCountdown() {
  if (_trkCountdownTimer) clearInterval(_trkCountdownTimer);
  _trkCountdownTimer = setInterval(function() {
    var el = document.getElementById('trk-countdown');
    if (!el || !_trkNextTickUTC) { el && (el.style.display = 'none'); clearInterval(_trkCountdownTimer); return; }
    var diff = Math.max(0, Math.floor((_trkNextTickUTC - Date.now()) / 1000));
    if (diff <= 0) {
      el.textContent = 'Searching now...';
      el.style.color = 'var(--green)';
      clearInterval(_trkCountdownTimer);
      // Refresh status + log after a few seconds to catch the new entry
      setTimeout(function() { trkRefreshStatus(); trkLoadLog(); }, 5000);
      return;
    }
    var min = Math.floor(diff / 60);
    var sec = diff % 60;
    el.textContent = 'Next search: ' + min + ':' + (sec < 10 ? '0' : '') + sec;
    el.style.color = 'var(--crimson)';
    el.style.display = 'block';
  }, 1000);
}

function trkInit() {
  if (!_trkInitDone) {
    _trkInitDone = true;
  }
  trkRefreshStatus();
  trkLoadItems();
  trkLoadLog();
  trkInjectScrollNav();
  // Poll status + log every 15s while on tracker tab
  clearInterval(_trkPollTimer);
  _trkPollTimer = setInterval(function() {
    var panel = document.getElementById('panel-tracker');
    if (panel && panel.classList.contains('active')) {
      trkRefreshStatus();
      trkLoadLog();
    } else {
      clearInterval(_trkPollTimer);
    }
  }, 15000);
}

function trkInjectScrollNav() {
  if (document.getElementById('trk-scroll-nav')) return;
  const scroller = document.querySelector('.content-area') || document.documentElement;
  const nav = document.createElement('div');
  nav.id = 'trk-scroll-nav';
  nav.style.cssText = 'position:fixed;right:1.5rem;bottom:5rem;z-index:500;display:flex;flex-direction:column;gap:.4rem;opacity:0;transition:opacity .3s;pointer-events:none;';
  const btnStyle = 'width:36px;height:36px;border-radius:50%;background:var(--surface2);border:1px solid var(--border2);color:var(--muted);font-size:1rem;cursor:pointer;display:flex;align-items:center;justify-content:center;transition:all .15s;line-height:1;';
  nav.innerHTML = `
    <button style="${btnStyle}" title="Scroll to top"
      onclick="(document.querySelector('.content-area')||document.documentElement).scrollTo({top:0,behavior:'smooth'})">↑</button>
    <button style="${btnStyle}" title="Scroll to bottom"
      onclick="(document.querySelector('.content-area')||document.documentElement).scrollTo({top:99999,behavior:'smooth'})">↓</button>`;
  document.body.appendChild(nav);
  scroller.addEventListener('scroll', () => {
    const panel = document.getElementById('panel-tracker');
    const tabActive = panel && panel.classList.contains('active');
    const show = tabActive && scroller.scrollTop > 200;
    nav.style.opacity       = show ? '1'    : '0';
    nav.style.pointerEvents = show ? 'auto' : 'none';
  }, {passive:true});
}

async function trkRefreshStatus() {
  try {
    var r = await fetch(API + '/api/tracker/status', { headers: authHdrs() });
    var d = await r.json();
    if (!r.ok) return;

    // Toggle button
    var btn = document.getElementById('trk-toggle-btn');
    var hint = document.getElementById('trk-toggle-hint');
    var cdEl = document.getElementById('trk-countdown');
    if (d.enabled) {
      btn.style.background = 'var(--crimson)';
      btn.style.color = '#000';
      btn.style.border = 'none';
      btn.innerHTML = '▶ TRACKER ON';
      hint.style.color = 'var(--crimson)';
      hint.style.fontSize = '.62rem';
      hint.textContent = d.hunt_loop_active
        ? ('Hunting · ' + (d.mode || 'continuous') + ' mode · ~' + Math.round((d.base_interval||2400)/60) + 'min ± jitter')
        : 'Enabled but hunt loop idle';
      // Countdown timer
      if (d.next_tick_utc && d.hunt_loop_active) {
        cdEl.style.display = 'block';
        _trkNextTickUTC = new Date(d.next_tick_utc + 'Z').getTime();
        _trkStartCountdown();
      } else {
        cdEl.style.display = 'none';
        _trkNextTickUTC = null;
      }
      // Auto-sync hint
      var syncHint = document.getElementById('trk-auto-sync-hint');
      if (syncHint) {
        syncHint.style.display = 'block';
        if (d.next_sync_in_seconds != null) {
          var sh = Math.floor(d.next_sync_in_seconds / 3600);
          var sm = Math.floor((d.next_sync_in_seconds % 3600) / 60);
          syncHint.textContent = 'Auto-sync: next in ' + sh + 'h ' + sm + 'm';
        } else {
          syncHint.textContent = 'Auto-sync: every 6 hours';
        }
      }
    } else {
      btn.style.background = 'var(--surface2)';
      btn.style.color = 'var(--muted)';
      btn.style.border = '1px solid var(--border)';
      btn.innerHTML = '⏻ TRACKER OFF';
      hint.style.color = 'var(--muted2)';
      hint.style.fontSize = '.52rem';
      hint.textContent = 'Click to enable automatic hunting';
      cdEl.style.display = 'none';
      _trkNextTickUTC = null;
      var syncHint2 = document.getElementById('trk-auto-sync-hint');
      if (syncHint2) syncHint2.style.display = 'none';
    }

    // Connection dots
    var rdot = document.getElementById('trk-radarr-dot');
    var rstat = document.getElementById('trk-radarr-status');
    if (rdot) rdot.style.background = d.radarr_connected ? 'var(--green)' : 'var(--red)';
    if (rstat) { rstat.textContent = d.radarr_connected ? 'OK' : '—'; rstat.style.color = d.radarr_connected ? 'var(--green)' : 'var(--muted2)'; }

    var sdot = document.getElementById('trk-sonarr-dot');
    var sstat = document.getElementById('trk-sonarr-status');
    if (sdot) sdot.style.background = d.sonarr_connected ? 'var(--green)' : 'var(--red)';
    if (sstat) { sstat.textContent = d.sonarr_connected ? 'OK' : '—'; sstat.style.color = d.sonarr_connected ? 'var(--green)' : 'var(--muted2)'; }

    // Stats
    document.getElementById('trk-s-movies').textContent  = d.movies_missing || 0;
    document.getElementById('trk-s-tv').textContent      = d.tv_missing || 0;
    document.getElementById('trk-s-found').textContent   = (d.movies_found || 0) + (d.tv_found || 0);
    document.getElementById('trk-s-today').textContent   = d.searches_today || 0;
    document.getElementById('trk-s-skipped').textContent = d.skipped || 0;

    // Settings
    var cdR = document.getElementById('trk-cooldown-radarr');
    var cdS = document.getElementById('trk-cooldown-sonarr');
    if (cdR) cdR.value = d.cooldown_days_radarr || d.cooldown_days || 7;
    if (cdS) cdS.value = d.cooldown_days_sonarr || d.cooldown_days || 7;
    document.getElementById('trk-logging').checked = d.logging_enabled !== false;

    // Status sub
    document.getElementById('trk-status-sub').textContent = d.enabled
      ? ((d.searches_today || 0) + ' searched today · ' + (d.total || 0) + ' tracked')
      : 'Sync to load missing items';
  } catch(e) { /* silent */ }
}

async function trkToggle() {
  try {
    var r = await fetch(API + '/api/tracker/toggle', { method:'POST', headers: authHdrs() });
    var d = await r.json();
    if (!r.ok) { toast(d.detail || 'Toggle failed', 'error'); return; }
    toast(d.enabled ? 'Tracker ON' : 'Tracker OFF', 'ok');
    trkRefreshStatus();
    trkLoadLog();
  } catch(e) { toast('Error: ' + e.message, 'error'); }
}

async function trkSync() {
  var btn = document.getElementById('trk-sync-btn');
  btn.disabled = true; btn.textContent = '⟳ SYNCING…';
  try {
    var r = await fetch(API + '/api/tracker/sync', { method:'POST', headers: authHdrs() });
    var d = await r.json();
    if (!r.ok) { toast(d.detail || 'Sync failed', 'error'); return; }
    toast(d.detail || 'Synced', 'ok');
    trkRefreshStatus();
    trkLoadItems();
    trkLoadLog();
  } catch(e) { toast('Error: ' + e.message, 'error'); }
  finally { btn.disabled = false; btn.textContent = '⟳ SYNC MISSING LISTS'; }
}

async function trkLoadItems() {
  var params = new URLSearchParams({ limit: '200', offset: String(_trkOffset) });
  if (_trkFilter === 'radarr')      params.set('source', 'radarr');
  else if (_trkFilter === 'sonarr') params.set('source', 'sonarr');
  else if (_trkFilter === 'missing')params.set('status', 'missing');
  else if (_trkFilter === 'found')  params.set('status', 'found');
  else if (_trkFilter === 'skipped')params.set('status', 'skipped');

  try {
    var r = await fetch(API + '/api/tracker/items?' + params, { headers: authHdrs() });
    var d = await r.json();
    if (!r.ok) return;
    if (_trkOffset === 0) _trkItems = d.items || [];
    else _trkItems = _trkItems.concat(d.items || []);
    _trkTotal = d.total || 0;
    trkRenderTable();
  } catch(e) { /* silent */ }
}

function trkToggleSortLastSearched() {
  // 3-way cycle: newest-first → oldest-first → never-first → back
  if (_trkSortDir === 'newest-first')      _trkSortDir = 'oldest-first';
  else if (_trkSortDir === 'oldest-first') _trkSortDir = 'never-first';
  else                                     _trkSortDir = 'newest-first';
  var arrow = document.getElementById('trk-sort-arrow');
  if (arrow) {
    arrow.textContent = 'LAST SEARCHED ' +
      (_trkSortDir === 'newest-first' ? '↓' :
       _trkSortDir === 'oldest-first' ? '↑' : '▲');
  }
  trkRenderTable();
}

function trkSortItems(items) {
  var arr = items.slice();
  if (_trkSortDir === 'never-first') {
    arr.sort(function(a, b) {
      var ac = a.search_count || 0;
      var bc = b.search_count || 0;
      if (ac === 0 && bc !== 0) return -1;
      if (bc === 0 && ac !== 0) return 1;
      // Within each group: oldest last_searched first
      return (a.last_searched || '') < (b.last_searched || '') ? -1 : 1;
    });
  } else if (_trkSortDir === 'oldest-first') {
    arr.sort(function(a, b) {
      return (a.last_searched || '0000') < (b.last_searched || '0000') ? -1 : 1;
    });
  } else {
    arr.sort(function(a, b) {
      return (a.last_searched || '0000') > (b.last_searched || '0000') ? -1 : 1;
    });
  }
  return arr;
}

function trkRenderTable() {
  var tbody = document.getElementById('trk-tbody');
  var empty = document.getElementById('trk-empty');
  var loadMore = document.getElementById('trk-load-more');

  var items = _trkItems;
  if (_trkTextFilter) {
    var q = _trkTextFilter.toLowerCase();
    items = items.filter(function(it) { return (it.title||'').toLowerCase().indexOf(q) >= 0; });
  }
  items = trkSortItems(items);

  if (!items.length) {
    tbody.innerHTML = '';
    empty.style.display = 'block';
    loadMore.style.display = 'none';
    return;
  }

  empty.style.display = 'none';
  var html = '';
  items.forEach(function(it) {
    var typeIcon = it.source === 'radarr' ? '🎬' : '📺';
    var statusColor = it.status === 'missing' ? 'var(--red)' : it.status === 'found' ? 'var(--green)' : it.status === 'skipped' ? 'var(--amber)' : 'var(--muted)';
    var statusLabel = it.status === 'missing' ? '❌ Missing' : it.status === 'found' ? '✅ Found' : it.status === 'skipped' ? (it.auto_skipped ? '⏭ Skipped (auto)' : '⏭ Skipped') : it.status;
    var lastSearched = it.last_searched
      ? it.last_searched.replace('T',' ').substring(0,16)
      : '<span style="color:var(--crimson);letter-spacing:.5px">— NEVER —</span>';
    var year = it.year || '—';

    var actions = '';
    if (it.status === 'missing') {
      actions += '<button onclick="trkSearchNow(' + it.item_id + ')" style="padding:.2rem .5rem;background:var(--crimson);color:#000;border:none;border-radius:4px;font-family:var(--mono);font-size:.52rem;cursor:pointer;margin-right:.25rem" title="Search now">🔍 SEARCH</button>';
      actions += '<button onclick="trkSkip(' + it.item_id + ')" style="padding:.2rem .5rem;background:var(--surface2);color:var(--amber);border:1px solid var(--border);border-radius:4px;font-family:var(--mono);font-size:.52rem;cursor:pointer" title="Skip this item">⏭ SKIP</button>';
    } else if (it.status === 'skipped') {
      actions += '<button onclick="trkUnskip(' + it.item_id + ')" style="padding:.2rem .5rem;background:var(--surface2);color:var(--green);border:1px solid var(--border);border-radius:4px;font-family:var(--mono);font-size:.52rem;cursor:pointer" title="Resume searching">↩ UNSKIP</button>';
    }

    // Season skip button for sonarr items
    if (it.source === 'sonarr' && it.season_number && it.status === 'missing') {
      actions += ' <button onclick="trkSkipSeason(' + it.series_id + ',' + it.season_number + ')" style="padding:.2rem .5rem;background:var(--surface2);color:var(--muted);border:1px solid var(--border);border-radius:4px;font-family:var(--mono);font-size:.48rem;cursor:pointer;margin-left:.15rem" title="Skip entire season ' + it.season_number + '">S' + it.season_number + ' SKIP</button>';
    }

    html += '<tr style="border-bottom:1px solid var(--border)">' +
      '<td style="padding:.45rem .75rem;white-space:nowrap">' + typeIcon + '</td>' +
      '<td style="padding:.45rem .75rem;color:var(--text);max-width:350px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="' + esc(it.title) + '">' + esc(it.title) + '</td>' +
      '<td style="padding:.45rem .75rem;color:var(--muted)">' + year + '</td>' +
      '<td style="padding:.45rem .75rem;color:' + statusColor + '">' + statusLabel + '</td>' +
      '<td style="padding:.45rem .75rem;color:var(--muted);text-align:center">' + (it.search_count || 0) + '</td>' +
      '<td style="padding:.45rem .75rem;color:var(--muted2);font-size:.52rem">' + lastSearched + '</td>' +
      '<td style="padding:.45rem .75rem">' + actions + '</td>' +
    '</tr>';
  });
  tbody.innerHTML = html;

  // Load more
  if (_trkItems.length < _trkTotal) {
    loadMore.style.display = 'block';
    document.getElementById('trk-load-count').textContent = (_trkTotal - _trkItems.length) + ' remaining';
  } else {
    loadMore.style.display = 'none';
  }
}

function trkLoadMore() {
  _trkOffset = _trkItems.length;
  trkLoadItems();
}

function trkFilter(f) {
  _trkFilter = f;
  _trkOffset = 0;
  ['all','radarr','sonarr','missing','found','skipped'].forEach(function(k) {
    var btn = document.getElementById('trk-f-' + k);
    if (!btn) return;
    if (k === f) {
      btn.style.background = 'var(--crimson)'; btn.style.color = '#000'; btn.style.border = 'none';
    } else {
      btn.style.background = 'var(--surface2)'; btn.style.border = '1px solid var(--border)';
      var colors = {all:'var(--muted)',radarr:'var(--muted)',sonarr:'var(--muted)',missing:'var(--red)',found:'var(--green)',skipped:'var(--amber)'};
      btn.style.color = colors[k] || 'var(--muted)';
    }
  });
  trkLoadItems();
}

function trkTextFilter(val) {
  _trkTextFilter = val;
  trkRenderTable();
}

async function trkSearchNow(itemId) {
  try {
    var r = await fetch(API + '/api/tracker/search-now', {
      method:'POST', headers:authHdrs(), body: JSON.stringify({ item_id: itemId })
    });
    var d = await r.json();
    if (!r.ok) { toast(d.detail || 'Search failed', 'error'); return; }
    toast(d.message || 'Search fired', 'ok');
    trkLoadItems();
    trkLoadLog();
    trkRefreshStatus();
  } catch(e) { toast('Error: ' + e.message, 'error'); }
}

async function trkSkip(itemId) {
  try {
    await fetch(API + '/api/tracker/skip', {
      method:'POST', headers:authHdrs(), body: JSON.stringify({ item_id: itemId })
    });
    trkLoadItems(); trkRefreshStatus();
  } catch(e) { toast('Error: ' + e.message, 'error'); }
}

async function trkUnskip(itemId) {
  try {
    await fetch(API + '/api/tracker/unskip', {
      method:'POST', headers:authHdrs(), body: JSON.stringify({ item_id: itemId })
    });
    trkLoadItems(); trkRefreshStatus();
  } catch(e) { toast('Error: ' + e.message, 'error'); }
}

async function trkSkipSeason(seriesId, seasonNum) {
  if (!confirm('Skip all missing episodes in Season ' + seasonNum + '? This allows later seasons to be searched.')) return;
  try {
    await fetch(API + '/api/tracker/skip-season', {
      method:'POST', headers:authHdrs(),
      body: JSON.stringify({ series_id: seriesId, season_number: seasonNum })
    });
    toast('Season ' + seasonNum + ' skipped', 'ok');
    trkLoadItems(); trkRefreshStatus();
  } catch(e) { toast('Error: ' + e.message, 'error'); }
}

async function trkSaveSettings() {
  try {
    await fetch(API + '/api/tracker/settings', {
      method:'POST', headers:authHdrs(),
      body: JSON.stringify({
        cooldown_days_radarr: parseInt(document.getElementById('trk-cooldown-radarr').value) || 7,
        cooldown_days_sonarr: parseInt(document.getElementById('trk-cooldown-sonarr').value) || 7,
        logging_enabled: document.getElementById('trk-logging').checked,
      })
    });
  } catch(e) { /* silent save */ }
}

async function trkLoadLog() {
  try {
    var r = await fetch(API + '/api/tracker/log?limit=50', { headers: authHdrs() });
    var d = await r.json();
    if (!r.ok) return;
    var el = document.getElementById('trk-log');
    var html = '';
    var entries = d.entries || [];
    entries.forEach(function(e) {
      var colors = {sync:'var(--blue)',search:'var(--crimson)',found:'var(--green)',skip:'var(--amber)',error:'var(--red)',toggle:'var(--purple)'};
      var color = colors[e.action] || 'var(--muted)';
      var time = (e.created_at || '').substring(11,16) || '';
      var icon = {sync:'⟳',search:'🔍',found:'✅',skip:'⏭',error:'⚠',toggle:'⏻'}[e.action] || '·';
      html += '<div style="color:' + color + '">' + icon + ' <span style="color:var(--muted2)">' + time + '</span> ' + esc(e.title||'') + ' — ' + esc(e.detail||e.action) + '</div>';
    });
    el.innerHTML = html || '<div style="color:var(--muted2)">No log entries yet</div>';
  } catch(e) { /* silent */ }
}

async function trkClearLog() {
  try {
    await fetch(API + '/api/tracker/clear-log', { method:'POST', headers:authHdrs() });
    document.getElementById('trk-log').innerHTML = '<div style="color:var(--muted2)">Log cleared</div>';
    toast('Activity log cleared', 'ok');
  } catch(e) { /* silent */ }
}

// Stop tracker poll when leaving tab
window._dbgTabPatch = true;

