'use strict';
// ══════════════════════════════════════════════════════════════════════════════
//  THE BLOODHOUND
// ══════════════════════════════════════════════════════════════════════════════

var _bhMode = 'artist';
var _bhResults = [];
var _bhSelectedArtist = null; // { mbid, name }
var _bhReleaseType = 'album';
var _bhCardCounter = 0;
var _bhCards = {};
var _bhOffset = 0;
var _bhMbCount = 0;
var _bhLastQuery = '';
var _bhLastLabel = '';
var _bhLastSearchType = ''; // 'album', 'comp', or 'genre'
var _bhCurrentFilter = 'all';

function bhInit() {
  bhInjectScrollNav();
  _loadCacheStatusStrip('bh-cache-info');
}

async function _loadCacheStatusStrip(elId) {
  const el = document.getElementById(elId);
  if (!el) return;
  try {
    const r = await fetch(`${API}/api/sniffer/sync-status`, {headers: authHdrs()});
    if (!r.ok) { el.textContent = 'Unknown'; return; }
    const d = await r.json();
    const count = d.cached_count || d.track_count || 0;
    const srcMap = {plex:'Plex', emby:'Emby', jellyfin:'Jellyfin',
                    file_scan:'File Scan', scanned_only:'Retriever Scan'};
    const src = srcMap[d.source] || d.source || '?';
    if (count > 0 && d.last_synced) {
      el.textContent = `${count.toLocaleString()} tracks · ${src} · synced ${_timeAgo(d.last_synced)}`;
      el.style.color = 'var(--teal)';
    } else {
      el.textContent = 'Never synced — results may be inaccurate';
      el.style.color = 'var(--amber)';
    }
  } catch(e) {
    el.textContent = 'Unknown';
  }
}

function bhLog(msg, type) {
  var el = document.getElementById('bh-log');
  if (!el) return;
  var colors = {ok:'var(--green)', err:'var(--red)', warn:'var(--amber)', info:'var(--muted)'};
  el.innerHTML += '<div style="color:' + (colors[type]||colors.info) + '">' + esc(msg) + '</div>';
  el.scrollTop = el.scrollHeight;
}

function bhSetMode(mode) {
  _bhMode = mode;
  ['artist','album','comp','essential','genre'].forEach(function(m) {
    var btn = document.getElementById('bh-mode-' + m);
    if (!btn) return;
    if (m === mode) {
      btn.style.background = 'var(--orange)'; btn.style.color = '#000'; btn.style.border = 'none';
    } else {
      btn.style.background = 'var(--surface2)'; btn.style.color = 'var(--muted)'; btn.style.border = '1px solid var(--border)';
    }
  });
  document.getElementById('bh-artist-section').style.display = mode === 'artist' ? 'block' : 'none';
  document.getElementById('bh-album-section').style.display  = mode === 'album'  ? 'block' : 'none';
  document.getElementById('bh-comp-section').style.display   = mode === 'comp'   ? 'block' : 'none';
  document.getElementById('bh-genre-section').style.display  = mode === 'genre'  ? 'block' : 'none';
  document.getElementById('bh-essential-section').style.display = mode === 'essential' ? 'block' : 'none';
  if (mode === 'essential') bhB200Status();
}

// ── M6: Essential albums (Billboard 200) ──
async function bhB200Status() {
  try {
    const d = await (await fetch(API + '/api/groomer/albums/status', {headers:authHdrs()})).json();
    const el = document.getElementById('bh-b200-status');
    if (el) el.textContent = d.albums ? '(' + d.albums.toLocaleString() + ' albums · updated ' + (d.imported_at || '').slice(0,10) + ')'
                                      : (d.status === 'running' ? '(downloading chart history…)' : '(chart history downloads automatically a few minutes after startup)');
  } catch(e) {}
}
async function bhEssential() {
  const decades = Array.from(document.querySelectorAll('.bh-ess-dec:checked')).map(function(c){ return parseInt(c.value); });
  bhLog('⭐ Loading essential albums…', 'info');
  document.getElementById('bh-status-title').textContent = 'LOADING…'; document.getElementById('bh-pulse-bar').style.display = 'block';
  try {
    const r = await fetch(API + '/api/groomer/albums/essential', {method:'POST', headers:authHdrs(),
      body: JSON.stringify({decades: decades, include_owned: !document.getElementById('bh-ess-missing').checked, limit: 400})});
    const d = await r.json();
    if (!r.ok) throw new Error(d.detail || 'HTTP ' + r.status);
    _bhResults = (d.results || []).map(function(a) {
      return {artist: a.artist, title: a.album, type: 'Album', year: a.year ? String(a.year) : '', secondary: [],
              in_library: a.in_library, b200_peak: a.b200_peak, b200_weeks: a.b200_weeks, essential: true};
    });
    document.getElementById('bh-s-total').textContent = d.total.toLocaleString();
    document.getElementById('bh-status-title').textContent = 'ESSENTIAL ALBUMS';
    document.getElementById('bh-status-sub').textContent = d.total.toLocaleString() + ' albums · ' + d.owned.toLocaleString() + ' owned' + (d.total > _bhResults.length ? ' · showing top ' + _bhResults.length : '');
    if (!d.total) bhLog('No album chart data yet — it downloads automatically a few minutes after ChartHound starts.', 'warn');
    bhRenderResults();
  } catch(e) { bhLog('✗ ' + e.message, 'err'); document.getElementById('bh-status-title').textContent = 'ERROR'; document.getElementById('bh-pulse-bar').style.display = 'none'; }
}

// ── Artist Search ──
async function bhArtistSearch() {
  var q = document.getElementById('bh-artist-input').value.trim();
  if (!q) return;
  bhLog('🔍 Searching MusicBrainz for artist: ' + q, 'info');
  document.getElementById('bh-artist-results').innerHTML = '<div style="text-align:center;padding:.5rem;color:var(--muted);font-size:.6rem">Searching…</div>';
  document.getElementById('bh-release-filter').style.display = 'none';
  _bhSelectedArtist = null;
  try {
    var r = await fetch(API + '/api/bloodhound/artist-search', {
      method:'POST', headers:authHdrs(),
      body: JSON.stringify({ query: q }),
    });
    var d = await r.json();
    if (!r.ok) throw new Error(d.detail || 'HTTP ' + r.status);
    var artists = d.results || [];
    if (!artists.length) {
      document.getElementById('bh-artist-results').innerHTML = '<div style="text-align:center;padding:.5rem;color:var(--muted2);font-size:.6rem">No artists found.</div>';
      bhLog('✗ No artists found for: ' + q, 'warn');
      return;
    }
    bhLog('✓ Found ' + artists.length + ' artist(s)', 'ok');
    document.getElementById('bh-artist-results').innerHTML = artists.map(function(a, i) {
      var extra = [];
      if (a.type) extra.push(a.type);
      if (a.country) extra.push(a.country);
      if (a.begin) extra.push(a.begin.substring(0,4) + (a.ended && a.end ? '–' + a.end.substring(0,4) : '–'));
      return '<div onclick="bhSelectArtist(' + i + ')" style="background:var(--surface2);border:1px solid var(--border);border-radius:6px;padding:.55rem .75rem;cursor:pointer;transition:background .15s" onmouseenter="this.style.borderColor=\'var(--orange)\'" onmouseleave="this.style.borderColor=\'var(--border)\'">'
        + '<div style="font-size:.72rem;color:var(--text)">' + esc(a.name) + '</div>'
        + '<div style="font-size:.52rem;color:var(--muted);margin-top:.15rem">' + esc(extra.join(' · ')) + (a.score ? ' · Score: ' + a.score : '') + '</div>'
        + '</div>';
    }).join('');
    window._bhArtistList = artists;
  } catch(e) {
    document.getElementById('bh-artist-results').innerHTML = '<div style="color:var(--red);font-size:.6rem;padding:.5rem">✗ ' + esc(e.message) + '</div>';
    bhLog('✗ Artist search failed: ' + e.message, 'err');
  }
}

async function bhSelectArtist(idx) {
  var a = window._bhArtistList[idx];
  if (!a) return;
  _bhSelectedArtist = { mbid: a.mbid, name: a.name };
  document.getElementById('bh-artist-results').innerHTML = '<div style="background:rgba(251,146,60,.12);border:1px solid var(--orange);border-radius:6px;padding:.55rem .75rem">'
    + '<div style="font-size:.72rem;color:var(--orange);font-family:var(--display);letter-spacing:1px">' + esc(a.name) + '</div>'
    + '<div style="font-size:.52rem;color:var(--muted);margin-top:.15rem">Selected · Loading releases…</div></div>';
  document.getElementById('bh-release-filter').style.display = 'block';
  _bhReleaseType = 'album';
  bhSetReleaseType('album');
  await bhLoadReleases();
}

function bhSetReleaseType(type) {
  _bhReleaseType = type;
  ['album','compilation','single','all'].forEach(function(t) {
    var btn = document.getElementById('bh-rt-' + t);
    if (!btn) return;
    if (t === type) {
      btn.style.background = 'var(--orange)'; btn.style.color = '#000'; btn.style.border = 'none';
    } else {
      btn.style.background = 'var(--surface2)'; btn.style.color = 'var(--muted)'; btn.style.border = '1px solid var(--border)';
    }
  });
  if (_bhSelectedArtist) bhLoadReleases();
}

async function bhLoadReleases() {
  if (!_bhSelectedArtist) return;
  bhLog('📀 Loading ' + _bhReleaseType + 's for ' + _bhSelectedArtist.name, 'info');
  document.getElementById('bh-status-title').textContent = 'LOADING…'; document.getElementById('bh-pulse-bar').style.display = 'block';
  document.getElementById('bh-status-sub').textContent = _bhSelectedArtist.name + ' — ' + _bhReleaseType + 's';
  try {
    var r = await fetch(API + '/api/bloodhound/artist-releases', {
      method:'POST', headers:authHdrs(),
      body: JSON.stringify({ artist_mbid: _bhSelectedArtist.mbid, artist_name: _bhSelectedArtist.name, release_type: _bhReleaseType }),
    });
    var d = await r.json();
    if (!r.ok) throw new Error(d.detail || 'HTTP ' + r.status);
    _bhResults = (d.results || []).map(function(rg) {
      return { artist: d.artist_name || _bhSelectedArtist.name, title: rg.title, type: rg.primary_type || '', year: (rg.first_release||'').substring(0,4), mbid: rg.mbid, secondary: rg.secondary_types || [], in_library: rg.in_library || false,
               b200_peak: rg.b200_peak, b200_weeks: rg.b200_weeks, essential: rg.essential };
    });
    bhLog('✓ Found ' + _bhResults.length + ' release(s)', 'ok');
    document.getElementById('bh-s-total').textContent = _bhResults.length;
    document.getElementById('bh-status-title').textContent = _bhSelectedArtist.name.toUpperCase();
    document.getElementById('bh-status-sub').textContent = _bhResults.length + ' ' + _bhReleaseType + '(s) found on MusicBrainz';
    bhRenderResults();
  } catch(e) {
    bhLog('✗ Failed to load releases: ' + e.message, 'err');
    document.getElementById('bh-status-title').textContent = 'ERROR'; document.getElementById('bh-pulse-bar').style.display = 'none';
  }
}

// ── Album Search (direct) ──
async function bhAlbumSearch(loadMore) {
  var q = loadMore ? _bhLastQuery : document.getElementById('bh-album-input').value.trim();
  if (!q) return;
  if (!loadMore) { _bhOffset = 0; _bhResults = []; _bhLastQuery = q; _bhLastSearchType = 'album'; }
  bhLog('🔍 Searching MusicBrainz for album: ' + q + (loadMore ? ' (offset ' + _bhOffset + ')' : ''), 'info');
  document.getElementById('bh-status-title').textContent = 'SEARCHING…'; document.getElementById('bh-pulse-bar').style.display = 'block';
  try {
    var r = await fetch(API + '/api/bloodhound/album-search', {
      method:'POST', headers:authHdrs(),
      body: JSON.stringify({ query: q, offset: _bhOffset }),
    });
    var d = await r.json();
    if (!r.ok) throw new Error(d.detail || 'HTTP ' + r.status);
    var newResults = (d.results || []).map(function(rg) {
      return { artist: rg.artist || '', title: rg.title || '', type: rg.primary_type || '', year: (rg.first_release||'').substring(0,4), mbid: rg.mbid, secondary: rg.secondary_types || [], in_library: rg.in_library || false };
    });
    _bhResults = _bhResults.concat(newResults);
    _bhOffset = _bhOffset + (d.limit || 100);
    _bhMbCount = d.mb_count || 0;
    bhLog('✓ Found ' + newResults.length + ' result(s) — ' + _bhResults.length + ' total loaded' + (_bhMbCount ? ' of ' + _bhMbCount + ' on MusicBrainz' : ''), 'ok');
    document.getElementById('bh-s-total').textContent = _bhResults.length;
    document.getElementById('bh-status-title').textContent = 'ALBUM SEARCH';
    document.getElementById('bh-status-sub').textContent = _bhResults.length + ' loaded' + (_bhMbCount ? ' of ' + _bhMbCount : '') + ' for "' + q + '"';
    bhRenderResults();
    bhUpdateLoadMore();
  } catch(e) {
    bhLog('✗ Album search failed: ' + e.message, 'err');
    document.getElementById('bh-status-title').textContent = 'ERROR'; document.getElementById('bh-pulse-bar').style.display = 'none';
  }
}

// ── Compilation Search ──
function bhCompSearch() {
  var q = document.getElementById('bh-comp-select').value;
  if (!q) { bhLog('Select a compilation series first', 'warn'); return; }
  bhCompDoSearch(q);
}
function bhCompCustomSearch() {
  var q = document.getElementById('bh-comp-custom').value.trim();
  if (!q) return;
  bhCompDoSearch(q);
}
async function bhCompDoSearch(q, loadMore) {
  if (!loadMore) { _bhOffset = 0; _bhResults = []; _bhLastQuery = q; _bhLastSearchType = 'comp'; }
  bhLog('🔍 Searching MusicBrainz for compilations: ' + q + (loadMore ? ' (offset ' + _bhOffset + ')' : ''), 'info');
  document.getElementById('bh-status-title').textContent = 'SEARCHING…'; document.getElementById('bh-pulse-bar').style.display = 'block';
  try {
    var r = await fetch(API + '/api/bloodhound/album-search', {
      method:'POST', headers:authHdrs(),
      body: JSON.stringify({ query: q, offset: _bhOffset }),
    });
    var d = await r.json();
    if (!r.ok) throw new Error(d.detail || 'HTTP ' + r.status);
    var newResults = (d.results || []).map(function(rg) {
      return { artist: rg.artist || 'Various Artists', title: rg.title || '', type: rg.primary_type || '', year: (rg.first_release||'').substring(0,4), mbid: rg.mbid, secondary: rg.secondary_types || [], in_library: rg.in_library || false };
    });
    _bhResults = _bhResults.concat(newResults);
    _bhOffset = _bhOffset + (d.limit || 100);
    _bhMbCount = d.mb_count || 0;
    bhLog('✓ Found ' + newResults.length + ' compilation(s) — ' + _bhResults.length + ' total loaded' + (_bhMbCount ? ' of ' + _bhMbCount : ''), 'ok');
    document.getElementById('bh-s-total').textContent = _bhResults.length;
    document.getElementById('bh-status-title').textContent = 'COMPILATIONS';
    document.getElementById('bh-status-sub').textContent = _bhResults.length + ' loaded' + (_bhMbCount ? ' of ' + _bhMbCount : '') + ' for "' + q + '"';
    bhRenderResults();
    bhUpdateLoadMore();
  } catch(e) {
    bhLog('✗ Compilation search failed: ' + e.message, 'err');
    document.getElementById('bh-status-title').textContent = 'ERROR'; document.getElementById('bh-pulse-bar').style.display = 'none';
  }
}

function bhLoadMore() {
  if (_bhLastSearchType === 'album') bhAlbumSearch(true);
  else if (_bhLastSearchType === 'comp') bhCompDoSearch(_bhLastQuery, true);
  else if (_bhLastSearchType === 'genre') bhGenreDoSearch(_bhLastQuery, _bhLastLabel, true);
}

// ── Genre Browse ──
const _BH_GENRE_MAP = {
  rock:        { label: 'Rock',          query: 'tag:rock' },
  pop:         { label: 'Pop',           query: 'tag:pop' },
  country:     { label: 'Country',       query: 'tag:country' },
  rnb:         { label: 'R&B / Soul',    query: 'tag:rnb OR tag:soul OR tag:rhythm-and-blues' },
  hiphop:      { label: 'Hip-Hop',       query: 'tag:hip-hop OR tag:rap' },
  electronic:  { label: 'Electronic',    query: 'tag:electronic OR tag:edm OR tag:house OR tag:techno' },
  jazz:        { label: 'Jazz',          query: 'tag:jazz' },
  blues:       { label: 'Blues',         query: 'tag:blues' },
  metal:       { label: 'Metal',         query: 'tag:metal OR tag:heavy-metal' },
  alternative: { label: 'Alternative',   query: 'tag:alternative OR tag:indie' },
  folk:        { label: 'Folk',          query: 'tag:folk OR tag:acoustic' },
  classical:   { label: 'Classical',     query: 'tag:classical OR tag:orchestral' },
  ccm:         { label: 'CCM / Gospel',  query: 'tag:christian OR tag:gospel OR tag:ccm OR tag:worship' },
};

function bhGenreSearch(key) {
  var g = _BH_GENRE_MAP[key];
  if (!g) return;
  bhGenreDoSearch(g.query, g.label, false);
}

async function bhGenreDoSearch(q, label, loadMore) {
  if (!loadMore) { _bhOffset = 0; _bhResults = []; _bhLastQuery = q; _bhLastLabel = label || q; _bhLastSearchType = 'genre'; }
  bhLog('🎵 Browsing genre: ' + (label || q) + (loadMore ? ' (offset ' + _bhOffset + ')' : ''), 'info');
  document.getElementById('bh-status-title').textContent = 'SEARCHING…'; document.getElementById('bh-pulse-bar').style.display = 'block';
  try {
    var r = await fetch(API + '/api/bloodhound/album-search', {
      method:'POST', headers:authHdrs(),
      body: JSON.stringify({ query: q, offset: _bhOffset }),
    });
    var d = await r.json();
    if (!r.ok) throw new Error(d.detail || 'HTTP ' + r.status);
    var newResults = (d.results || []).map(function(rg) {
      return { artist: rg.artist || '', title: rg.title || '', type: rg.primary_type || '', year: (rg.first_release||'').substring(0,4), mbid: rg.mbid, secondary: rg.secondary_types || [], in_library: rg.in_library || false };
    });
    _bhResults = _bhResults.concat(newResults);
    _bhOffset = _bhOffset + (d.limit || 100);
    _bhMbCount = d.mb_count || 0;
    var lbl = label || _bhLastLabel || q;
    bhLog('✓ Found ' + newResults.length + ' result(s) — ' + _bhResults.length + ' total loaded' + (_bhMbCount ? ' of ' + _bhMbCount : ''), 'ok');
    document.getElementById('bh-s-total').textContent = _bhResults.length;
    document.getElementById('bh-status-title').textContent = lbl.toUpperCase();
    document.getElementById('bh-status-sub').textContent = _bhResults.length + ' loaded' + (_bhMbCount ? ' of ' + _bhMbCount : '');
    bhRenderResults();
    bhUpdateLoadMore();
  } catch(e) {
    bhLog('✗ Genre search failed: ' + e.message, 'err');
    document.getElementById('bh-status-title').textContent = 'ERROR'; document.getElementById('bh-pulse-bar').style.display = 'none';
  }
}

function bhUpdateLoadMore() {
  var el = document.getElementById('bh-load-more');
  var remaining = Math.max(0, _bhMbCount - _bhOffset);
  if (remaining > 0) {
    el.style.display = 'block';
    document.getElementById('bh-load-count').textContent = remaining.toLocaleString() + ' remaining on MusicBrainz';
  } else {
    el.style.display = 'none';
  }
}

// ── Render Results Table ──
var _bhSortKey = '';
var _bhSortAsc = true;

function bhSort(key) {
  if (_bhSortKey === key) { _bhSortAsc = !_bhSortAsc; } else { _bhSortKey = key; _bhSortAsc = key === 'year' ? false : true; }
  ['artist','title','year','status'].forEach(function(k) {
    var el = document.getElementById('bh-sort-' + k);
    if (el) el.textContent = k === _bhSortKey ? (_bhSortAsc ? '▲' : '▼') : '';
  });
  var fns = {
    artist: function(r) { return (r.artist||'').toLowerCase(); },
    title:  function(r) { return (r.title||'').toLowerCase(); },
    year:   function(r) { return parseInt(r.year) || 0; },
    status: function(r) { return r.in_library ? 0 : 1; },
  };
  var fn = fns[key];
  if (!fn) return;
  _bhResults.sort(function(a,b) {
    var av = fn(a), bv = fn(b);
    return _bhSortAsc ? (av < bv ? -1 : av > bv ? 1 : 0) : (av > bv ? -1 : av < bv ? 1 : 0);
  });
  bhRenderResults();
}

function bhRenderResults() {
  var pb = document.getElementById('bh-pulse-bar'); if (pb) pb.style.display = 'none';
  var tbody = document.getElementById('bh-tbody');
  var empty = document.getElementById('bh-empty');
  tbody.innerHTML = '';
  if (!_bhResults.length) { empty.style.display = 'block'; return; }
  empty.style.display = 'none';
  var ownedCt = 0, missingCt = 0;
  _bhResults.forEach(function(r, idx) {
    var owned = r.in_library || false;
    var b200 = r.b200_peak ? ' <span title="Billboard 200: peak #' + r.b200_peak + ', ' + (r.b200_weeks||0) + ' weeks" style="font-size:.5rem;padding:.08rem .35rem;border-radius:8px;border:1px solid var(--gold);color:var(--gold);white-space:nowrap">' + (r.essential ? '⭐ ' : '') + 'B200 #' + r.b200_peak + ' · ' + (r.b200_weeks||0) + 'w</span>' : '';
    if (owned) ownedCt++; else missingCt++;
    var tr = document.createElement('tr');
    tr.dataset.text = ((r.artist||'') + ' ' + (r.title||'')).toLowerCase();
    tr.dataset.status = owned ? 'owned' : 'missing';
    tr.style.borderBottom = '1px solid var(--border)';
    if (_bhCurrentFilter === 'missing' && owned) tr.style.display = 'none';
    if (_bhCurrentFilter === 'owned' && !owned) tr.style.display = 'none';
    var typeLabel = r.type || '\u2014';
    if (r.secondary && r.secondary.length) typeLabel += ' \u00b7 ' + r.secondary.join(', ');
    var typeColor = r.type === 'Album' ? 'var(--teal)' : r.type === 'Single' ? 'var(--purple)' : 'var(--amber)';
    var statusHTML = owned ? '<span style="color:var(--green)">\u2705 Owned</span>' : '<span style="color:var(--red)">\u274c Missing</span>';
    tr.innerHTML = '<td style="padding:.45rem .75rem;color:var(--muted)">' + esc(r.artist||'') + '</td>'
      + '<td style="padding:.45rem .75rem;color:var(--text)">' + esc(r.title||'') + b200 + '</td>'
      + '<td style="padding:.45rem .75rem"><span style="font-size:.55rem;padding:.1rem .35rem;border-radius:8px;background:rgba(251,146,60,.1);border:1px solid rgba(251,146,60,.25);color:' + typeColor + ';white-space:nowrap">' + esc(typeLabel) + '</span></td>'
      + '<td style="padding:.45rem .75rem;color:var(--muted)">' + esc(r.year||'\u2014') + '</td>'
      + '<td style="padding:.45rem .75rem">' + statusHTML + '</td>'
      + '<td style="padding:.45rem .75rem"><button onclick="bhSearchProwlarr(' + idx + ')" style="padding:.25rem .6rem;background:var(--surface2);color:var(--orange);border:1px solid var(--orange);border-radius:4px;font-family:var(--mono);font-size:.55rem;cursor:pointer;letter-spacing:.5px;white-space:nowrap">\ud83d\udd0d SEARCH</button></td>';
    tbody.appendChild(tr);
  });
  document.getElementById('bh-s-owned').textContent = ownedCt;
  document.getElementById('bh-s-missing').textContent = missingCt;
}

function bhFilterView(filter) {
  _bhCurrentFilter = filter;
  ['all','missing','owned'].forEach(function(f) {
    var btn = document.getElementById('bh-filter-' + f);
    if (!btn) return;
    if (f === filter) {
      btn.style.background = f==='all'?'var(--orange)':f==='missing'?'rgba(248,113,113,.2)':'rgba(74,222,128,.2)';
      btn.style.color = f==='all'?'#000':f==='missing'?'var(--red)':'var(--green)';
      btn.style.border = f==='all'?'none':f==='missing'?'1px solid var(--red)':'1px solid var(--green)';
    } else {
      btn.style.background = 'var(--surface2)';
      btn.style.color = f==='all'?'var(--muted)':f==='missing'?'var(--red)':'var(--green)';
      btn.style.border = '1px solid var(--border)';
    }
  });
  document.querySelectorAll('#bh-tbody tr').forEach(function(tr) {
    if (filter === 'all') tr.style.display = '';
    else if (filter === 'missing') tr.style.display = tr.dataset.status === 'missing' ? '' : 'none';
    else if (filter === 'owned') tr.style.display = tr.dataset.status === 'owned' ? '' : 'none';
  });
}

function bhFilterText(q) {
  var lq = q.toLowerCase();
  document.querySelectorAll('#bh-tbody tr').forEach(function(tr) {
    var textMatch = (tr.dataset.text||'').includes(lq);
    var filterMatch = _bhCurrentFilter === 'all' || tr.dataset.status === _bhCurrentFilter;
    tr.style.display = (textMatch && filterMatch) ? '' : 'none';
  });
}

// ── Prowlarr Multi-Card Search (same pattern as Sniffer) ──
async function bhSearchProwlarr(idx) {
  var release = _bhResults[idx];
  if (!release) return;
  var searchQuery = release.artist ? release.artist + ' ' + release.title : release.title;
  var cardKey = searchQuery.toLowerCase().trim();
  var container = document.getElementById('bh-prowlarr-cards');

  // Check for existing card
  var existingId = null;
  Object.keys(_bhCards).forEach(function(cid) {
    if (_bhCards[cid].key === cardKey) existingId = cid;
  });
  if (existingId) {
    var existingCard = document.getElementById(existingId);
    if (existingCard) {
      var body = existingCard.querySelector('.bh-card-body');
      if (body) body.style.display = 'flex';
      var minBtn = existingCard.querySelector('.bh-card-min');
      if (minBtn) minBtn.textContent = '\u2212';
      existingCard.style.padding = '1.25rem';
      existingCard.scrollIntoView({behavior:'smooth'});
      return;
    }
  }

  _bhCardCounter++;
  var cardId = 'bh-card-' + _bhCardCounter;
  _bhCards[cardId] = { key: cardKey, results: [], usenet_results: [] };

  var card = document.createElement('div');
  card.id = cardId;
  card.style.cssText = 'background:var(--surface);border:1px solid var(--orange);border-radius:10px;padding:1.25rem;';

  card.innerHTML =
    '<div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:.75rem">'
    + '<div>'
    + '<div style="font-family:var(--display);font-size:1rem;letter-spacing:1.5px;color:var(--orange)">PROWLARR RESULTS</div>'
    + '<div style="font-family:var(--mono);font-size:.55rem;color:var(--muted);margin-top:.15rem" id="' + cardId + '-sub">Searching: ' + esc(searchQuery) + '</div>'
    + '</div>'
    + '<div style="display:flex;gap:.35rem">'
    + '<button class="bh-card-min" onclick="bhToggleCard(\'' + cardId + '\')" style="background:none;border:none;color:var(--muted);font-size:1.1rem;cursor:pointer;line-height:1" title="Minimize">\u2212</button>'
    + '<button onclick="bhCloseCard(\'' + cardId + '\')" style="background:none;border:none;color:var(--muted);font-size:1rem;cursor:pointer;line-height:1" title="Close">\u2715</button>'
    + '</div>'
    + '</div>'
    + '<div class="bh-card-body" style="display:flex;flex-direction:column;gap:.5rem">'
    + '<div style="text-align:center;padding:1rem;color:var(--muted);font-family:var(--mono);font-size:.6rem">\ud83d\udd0d Searching Prowlarr\u2026</div>'
    + '</div>';

  container.appendChild(card);
  card.scrollIntoView({behavior:'smooth'});
  bhLog('\ud83d\udd0d Searching Prowlarr for: ' + searchQuery, 'info');

  try {
    var r = await fetch(API + '/api/bloodhound/search-prowlarr', {
      method:'POST', headers:authHdrs(),
      body: JSON.stringify({ query: searchQuery, artist: release.artist || '' }),
    });
    var d = await r.json();
    if (!r.ok) throw new Error(d.detail || 'HTTP ' + r.status);
    var results = d.results || [];
    var usenetResults = d.usenet_results || [];
    _bhCards[cardId].results = results;
    _bhCards[cardId].usenet_results = usenetResults;
    var bodyEl = card.querySelector('.bh-card-body');
    var subEl = document.getElementById(cardId + '-sub');

    if (!results.length && !usenetResults.length) {
      bodyEl.innerHTML = '<div style="text-align:center;padding:1rem;color:var(--muted2);font-family:var(--mono);font-size:.6rem">No results found.</div>';
      bhLog('\u2717 No results for: ' + searchQuery, 'warn');
      return;
    }

    var totalCount = results.length + usenetResults.length;
    bhLog('\u2713 Found ' + totalCount + ' result(s) (' + results.length + ' torrent, ' + usenetResults.length + ' usenet)', 'ok');
    subEl.textContent = totalCount + ' result(s) for: ' + searchQuery;

    var html = '';

    // ── Torrent results section ──
    if (results.length) {
      html += '<div style="font-family:var(--mono);font-size:.52rem;letter-spacing:2px;color:var(--orange);margin-top:.25rem;display:flex;align-items:center;gap:.5rem">\ud83e\uddf2 TORRENT RESULTS (' + results.length + ')<span style="flex:1;height:1px;background:var(--border);display:block"></span></div>';
      html += results.map(function(r, i) {
        var sizeMB = r.size_mb || 0;
        var sizeStr = sizeMB > 1024 ? (sizeMB/1024).toFixed(1) + ' GB' : sizeMB + ' MB';
        return '<div style="background:var(--surface2);border:1px solid var(--border);border-radius:6px;padding:.65rem .85rem;display:flex;align-items:center;gap:.75rem;flex-wrap:wrap">'
          + '<div style="flex:1;min-width:200px">'
          + '<div style="font-size:.68rem;color:var(--text);margin-bottom:.25rem;word-break:break-all">' + esc(r.title||'') + '</div>'
          + '<div style="display:flex;gap:.5rem;align-items:center;flex-wrap:wrap">'
          + '<span style="font-size:.52rem;color:var(--muted)">' + esc(r.indexer||'') + '</span>'
          + '<span style="font-size:.52rem;color:var(--muted)">' + sizeStr + '</span>'
          + '</div></div>'
          + '<div style="text-align:center;min-width:50px">'
          + '<div style="font-family:var(--display);font-size:1.1rem;color:var(--green)">' + (r.seeders||0) + '</div>'
          + '<div style="font-size:.45rem;color:var(--muted);letter-spacing:.5px">SEEDS</div></div>'
          + '<div style="display:flex;gap:.35rem;flex-wrap:wrap">'
          + '<button onclick="bhGrabGet(\'' + cardId + '\',' + i + ')" style="padding:.3rem .6rem;background:var(--orange);color:#000;border:none;border-radius:4px;font-family:var(--mono);font-size:.58rem;cursor:pointer" title="Send to download client">\u2b07 GET</button>'
          + (r.info_url ? '<a href="' + esc(r.info_url) + '" target="_blank" rel="noopener" style="display:inline-block;padding:.3rem .6rem;background:var(--surface3);color:var(--teal);border:1px solid var(--teal);border-radius:4px;font-family:var(--mono);font-size:.58rem;cursor:pointer;text-decoration:none" title="Open source page">\u2197 OPEN</a>' : '')
          + '<button onclick="bhCopyMagnet(\'' + cardId + '\',' + i + ')" style="padding:.3rem .6rem;background:var(--surface3);color:var(--purple);border:1px solid var(--purple);border-radius:4px;font-family:var(--mono);font-size:.58rem;cursor:pointer" title="Copy download URL">\ud83d\udd17 MAGNET</button>'
          + '</div></div>';
      }).join('');
    }

    // ── Usenet results section ──
    if (usenetResults.length) {
      html += '<div style="font-family:var(--mono);font-size:.52rem;letter-spacing:2px;color:var(--amber);margin-top:.75rem;display:flex;align-items:center;gap:.5rem">\ud83d\udce6 USENET RESULTS (' + usenetResults.length + ')<span style="flex:1;height:1px;background:var(--border);display:block"></span></div>';
      html += usenetResults.map(function(r, i) {
        var sizeMB = r.size_mb || 0;
        var sizeStr = sizeMB > 1024 ? (sizeMB/1024).toFixed(1) + ' GB' : sizeMB + ' MB';
        var ageStr = (r.age_days === 0) ? 'Today' : r.age_days + 'd ago';
        return '<div style="background:var(--surface2);border:1px solid var(--border);border-radius:6px;padding:.65rem .85rem;display:flex;align-items:center;gap:.75rem;flex-wrap:wrap">'
          + '<div style="flex:1;min-width:200px">'
          + '<div style="font-size:.68rem;color:var(--text);margin-bottom:.25rem;word-break:break-all">' + esc(r.title||'') + '</div>'
          + '<div style="display:flex;gap:.5rem;align-items:center;flex-wrap:wrap">'
          + '<span style="font-size:.52rem;color:var(--muted)">' + esc(r.indexer||'') + '</span>'
          + '<span style="font-size:.52rem;color:var(--muted)">' + sizeStr + '</span>'
          + '</div></div>'
          + '<div style="text-align:center;min-width:50px">'
          + '<div style="font-family:var(--display);font-size:.9rem;color:var(--amber)">' + ageStr + '</div>'
          + '<div style="font-size:.45rem;color:var(--muted);letter-spacing:.5px">AGE</div></div>'
          + '<div style="display:flex;gap:.35rem;flex-wrap:wrap">'
          + '<button onclick="bhGrabNzb(\'' + cardId + '\',' + i + ')" style="padding:.3rem .6rem;background:var(--amber);color:#000;border:none;border-radius:4px;font-family:var(--mono);font-size:.58rem;cursor:pointer" title="Send NZB to download client via Prowlarr">\u2b07 GET NZB</button>'
          + (r.info_url ? '<a href="' + esc(r.info_url) + '" target="_blank" rel="noopener" style="display:inline-block;padding:.3rem .6rem;background:var(--surface3);color:var(--teal);border:1px solid var(--teal);border-radius:4px;font-family:var(--mono);font-size:.58rem;cursor:pointer;text-decoration:none" title="Open source page">\u2197 OPEN</a>' : '')
          + '</div></div>';
      }).join('');
    }

    if (!usenetResults.length && d.usenet_total === 0 && typeof d.usenet_total !== 'undefined') {
      html += '<div style="margin-top:.5rem;padding:.5rem .7rem;background:rgba(251,191,36,.06);border:1px solid rgba(251,191,36,.2);border-radius:6px;font-family:var(--mono);font-size:.48rem;color:var(--muted)">'
        + '\u2139\ufe0f No Usenet indexers detected in Prowlarr. Add a Newznab indexer (e.g. NZBGeek) to enable NZB search. '
        + 'Your NZB client also needs a <strong style="color:var(--amber)">charthound-music</strong> category pointing to your music folder.'
        + '</div>';
    }

    bodyEl.innerHTML = html;
  } catch(e) {
    card.querySelector('.bh-card-body').innerHTML = '<div style="text-align:center;padding:1rem;color:var(--red);font-family:var(--mono);font-size:.6rem">\u2717 ' + esc(e.message) + '</div>';
    bhLog('\u2717 Search failed: ' + e.message, 'err');
  }
}

function bhToggleCard(cardId) {
  var card = document.getElementById(cardId);
  if (!card) return;
  var body = card.querySelector('.bh-card-body');
  var btn = card.querySelector('.bh-card-min');
  if (!body || !btn) return;
  if (body.style.display === 'none') {
    body.style.display = 'flex'; btn.textContent = '\u2212'; card.style.padding = '1.25rem';
  } else {
    body.style.display = 'none'; btn.textContent = '+'; card.style.padding = '.6rem 1.25rem';
  }
}

function bhCloseCard(cardId) {
  var card = document.getElementById(cardId);
  if (card) card.remove();
  delete _bhCards[cardId];
}

async function bhGrabGet(cardId, resultIdx) {
  var cardData = _bhCards[cardId];
  if (!cardData) return;
  var result = cardData.results[resultIdx];
  if (!result) return;
  bhLog('⬇ Sending to download client: ' + result.title, 'info');
  try {
    var r = await fetch(API + '/api/bloodhound/grab', {
      method:'POST', headers:authHdrs(),
      body: JSON.stringify({ download_url: result.download_url, title: result.title, indexer: result.indexer || '' }),
    });
    var d = await r.json();
    if (!r.ok) throw new Error(d.detail || 'HTTP ' + r.status);
    bhLog('✓ ' + d.message, 'ok');
    toast(d.message, 'ok');
  } catch(e) {
    bhLog('✗ Grab failed: ' + e.message, 'err');
    toast('Grab failed: ' + e.message, 'error');
  }
}

function bhCopyMagnet(cardId, resultIdx) {
  var cardData = _bhCards[cardId];
  if (!cardData) return;
  var result = cardData.results[resultIdx];
  if (!result || !result.download_url) { bhLog('No URL available', 'err'); return; }
  try {
    if (navigator.clipboard && window.isSecureContext) {
      navigator.clipboard.writeText(result.download_url);
    } else {
      var ta = document.createElement('textarea');
      ta.value = result.download_url;
      document.body.appendChild(ta);
      ta.select();
      document.execCommand('copy');
      document.body.removeChild(ta);
    }
    bhLog('✓ Download URL copied to clipboard', 'ok');
    toast('URL copied to clipboard', 'ok');
  } catch(e) {
    bhLog('✗ Copy failed: ' + e.message, 'err');
  }
}

async function bhGrabNzb(cardId, resultIdx) {
  var cardData = _bhCards[cardId];
  if (!cardData) return;
  var result = (cardData.usenet_results || [])[resultIdx];
  if (!result) return;
  bhLog('⬇ Sending NZB to download client: ' + result.title, 'info');
  try {
    var r = await fetch(API + '/api/bloodhound/grab-nzb', {
      method:'POST', headers:authHdrs(),
      body: JSON.stringify({ download_url: result.download_url, title: result.title, indexer_id: result.indexer_id || 0 }),
    });
    var d = await r.json();
    if (!r.ok) throw new Error(d.detail || 'HTTP ' + r.status);
    bhLog('✓ ' + d.message, 'ok');
    toast(d.message, 'ok');
  } catch(e) {
    bhLog('✗ NZB grab failed: ' + e.message, 'err');
    toast('NZB grab failed: ' + e.message, 'error');
  }
}

function bhInjectScrollNav() {
  if (document.getElementById("bh-scroll-nav")) return;
  const bhScroller = document.querySelector(".content-area") || document.documentElement;
  const nav = document.createElement("div");
  nav.id = "bh-scroll-nav";
  nav.style.cssText = "position:fixed;right:1.5rem;bottom:5rem;z-index:500;display:flex;flex-direction:column;gap:.4rem;opacity:0;transition:opacity .3s;pointer-events:none;";
  const btnStyle = "width:36px;height:36px;border-radius:50%;background:var(--surface2);border:1px solid var(--border2);color:var(--orange);font-size:1rem;cursor:pointer;display:flex;align-items:center;justify-content:center;transition:all .15s;line-height:1;";
  nav.innerHTML = "<button style=\"" + btnStyle + "\" title=\"Scroll to top\" onclick=\"(document.querySelector(\x27.content-area\x27)||document.documentElement).scrollTo({top:0,behavior:\x27smooth\x27})\">↑</button><button style=\"" + btnStyle + "\" title=\"Scroll to bottom\" onclick=\"(document.querySelector(\x27.content-area\x27)||document.documentElement).scrollTo({top:99999,behavior:\x27smooth\x27})\">↓</button>";
  document.body.appendChild(nav);
  bhScroller.addEventListener("scroll", function() {
    const panel = document.getElementById("panel-bloodhound");
    const tabActive = panel && panel.classList.contains("active");
    const show = tabActive && bhScroller.scrollTop > 200;
    nav.style.opacity = show ? "1" : "0";
    nav.style.pointerEvents = show ? "auto" : "none";
  }, {passive:true});
}

