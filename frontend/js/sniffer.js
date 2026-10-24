'use strict';
// ══════════════════════════════════════════════════════════════════════════════
//  THE SNIFFER
// ══════════════════════════════════════════════════════════════════════════════

let snfCurrentMode = 'gap';
let snfResults = [];
let snfCurrentFilter = 'all';
let snfOffset = 0;
let snfTotal = 0;
const SNF_PAGE = 500;
let _snfSortKey = 'peak';
let _snfSortAsc = true;
let _snfTrendingPage = 1;

function snfSetMode(mode) {
  snfCurrentMode = mode;
  const on  = 'background:var(--gold);color:#000;border:none';
  const off = 'background:var(--surface2);color:var(--muted);border:1px solid var(--border)';
  ['gap','bighits','trending'].forEach(function(m) {
    const b = document.getElementById('snf-mode-' + m); if (!b) return;
    const st = (m === mode ? on : off).split(';');
    st.forEach(function(kv){ const p = kv.split(':'); b.style[p[0].trim()] = p.slice(1).join(':').trim(); });
  });
  const showGap = mode === 'gap' || mode === 'bighits';
  const gs = document.getElementById('snf-gap-section');
  gs.style.display = showGap ? 'flex' : 'none'; gs.style.flexDirection = 'column'; gs.style.gap = '1rem';
  document.getElementById('snf-bighits-section').style.display = mode === 'bighits' ? 'block' : 'none';
  document.getElementById('snf-trending-section').style.display = mode === 'trending' ? 'block' : 'none';
  document.getElementById('snf-btn-search').textContent =
    mode === 'gap' ? '\ud83d\udd0d FIND MISSING TRACKS' : mode === 'bighits' ? '\ud83c\udfc6 FIND BIG HITS I DON\u2019T OWN' : '\ud83d\udd25 FIND TRENDING';
}

function snfLog(msg, type='') {
  const el = document.getElementById('snf-log');
  if (!el) return;
  const color = type==='ok'?'var(--green)':type==='err'?'var(--red)':type==='warn'?'var(--amber)':'var(--muted)';
  const div = document.createElement('div');
  div.style.color = color;
  div.textContent = msg;
  el.appendChild(div);
  el.scrollTop = el.scrollHeight;
}

function snfGetSelectedGenres() {
  return Array.from(document.querySelectorAll('.snf-genre:checked')).map(function(el){return el.value});
}

function snfGetSelectedDecades() {
  // If "All decades" master is checked OR no individual decade is checked, return [] (= no filter)
  const allEl = document.getElementById('snf-decade-all');
  if (allEl && allEl.checked) return [];
  const picks = Array.from(document.querySelectorAll('.snf-decade:checked')).map(function(el){return el.value});
  return picks;
}

function snfGetSelectedTier() {
  const el = document.querySelector('input[name="snf-tier"]:checked');
  return el ? el.value : 'notable';
}

function snfToggleAllDecades(checked) {
  // When user (re)checks the master "All", clear individual picks. They become available
  // to pick again, which auto-unchecks the master via snfDecadeChanged.
  if (checked) {
    document.querySelectorAll('.snf-decade').forEach(function(el){el.checked = false});
  }
}

function snfDecadeChanged() {
  // Any individual decade pick implicitly disables the "All decades" master.
  const anyPicked = document.querySelectorAll('.snf-decade:checked').length > 0;
  const allEl = document.getElementById('snf-decade-all');
  if (allEl) allEl.checked = !anyPicked;
}

function snfToggleAdvanced() {
  const sec = document.getElementById('snf-adv-section');
  const arr = document.getElementById('snf-adv-arrow');
  const open = sec.style.display !== 'none';
  sec.style.display = open ? 'none' : 'block';
  if (arr) arr.textContent = open ? '\u25b8' : '\u25be';
}

function snfUpdatePeak(v) {
  const n = parseInt(v);
  document.getElementById('snf-peak-display').textContent =
    n === 1 ? '#1 Only' : n >= 100 ? 'All peaks' : 'Top ' + n;
}

async function snfRunAnalysis() {
  if (snfCurrentMode === 'gap') await snfGapAnalysis();
  else if (snfCurrentMode === 'bighits') await snfBigHits();
  else await snfTrending();
}

// ── M6: Big hits you don't own (Hit Score) ───────────────────────
const _SNF_GENRE_KEY = {alternative:'alternative', indie:'indierock', electronic:'dance'};
async function snfBigHits() {
  const genres  = snfGetSelectedGenres().map(function(g){ return _SNF_GENRE_KEY[g] || g; });
  const decades = snfGetSelectedDecades().map(function(d){ return parseInt(d); }).filter(function(d){ return d > 0; });
  const minScore = parseInt(document.getElementById('snf-bh-score').value);
  const ohw = document.getElementById('snf-bh-ohw').checked;
  snfResults = []; snfOffset = 0;
  snfLog('Finding big hits you don\u2019t own \u00b7 score ' + minScore + '+' + (decades.length ? ' \u00b7 ' + decades.map(function(d){return d+'s'}).join(', ') : '') + (genres.length ? ' \u00b7 ' + genres.join(', ') : '') + '\u2026', 'info');
  document.getElementById('snf-status-title').textContent = 'SEARCHING\u2026'; document.getElementById('snf-pulse-bar').style.display = 'block';
  document.getElementById('snf-btn-search').disabled = true;
  try {
    const r = await fetch(API + '/api/groomer/big-hits', {method:'POST', headers:authHdrs(),
      body: JSON.stringify({decades: decades, genres: genres, min_score: minScore, ohw_only: ohw, crossovers: true, limit: 1000})});
    const d = await r.json();
    if (!r.ok) throw new Error(d.detail || 'HTTP ' + r.status);
    snfResults = (d.results || []).map(function(x) {
      return {artist: x.artist, title: x.title, chart_year: x.year, peak_position: x.hot100_peak,
              hit_score: x.hit_score, listeners: x.listeners, in_library: false,
              primary_chart_label: 'SCORE ' + x.hit_score + (x.one_hit_wonder ? ' \u00b7 1-HIT' : '') + (x.yearend_rank ? ' \u00b7 year-end #' + x.yearend_rank : ''),
              sources: x.families || []};
    });
    _snfSortKey = 'score'; _snfSortAsc = true;
    snfTotal = d.total || 0;
    document.getElementById('snf-s-total').textContent = snfTotal.toLocaleString();
    document.getElementById('snf-s-owned').textContent = '0';
    document.getElementById('snf-s-missing').textContent = snfTotal.toLocaleString();
    document.getElementById('snf-status-title').textContent = 'BIG HITS YOU DON\u2019T OWN';
    document.getElementById('snf-status-sub').textContent = snfTotal.toLocaleString() + ' songs' + (snfTotal > snfResults.length ? ' \u00b7 showing top ' + snfResults.length : '');
    snfRenderResults(); snfUpdateLoadMore();
    snfLog('\u2713 ' + snfTotal.toLocaleString() + ' big hits missing from your library', 'ok');
    if (d.lookups_pending) snfLog('\u23f3 Looking up how much ' + d.lookups_pending + ' of these are still played today (Last.fm, in the background). Click FIND again in a few minutes for sharper scores.', 'info');
  } catch(e) { snfLog('\u2717 ' + e.message, 'err'); document.getElementById('snf-status-title').textContent = 'ERROR'; }
  document.getElementById('snf-btn-search').disabled = false; document.getElementById('snf-pulse-bar').style.display = 'none';
}

async function snfGapAnalysis(append) {
  const genres  = snfGetSelectedGenres();
  const decades = snfGetSelectedDecades();
  const tier    = snfGetSelectedTier();
  const peakMax = parseInt(document.getElementById('snf-peak').value);
  if (!append) { snfOffset = 0; snfResults = []; }
  const tierLabel  = tier === 'essential' ? 'Essential (250)' : tier === 'deep' ? 'Deep cuts (5,000)' : 'Notable (1,000)';
  const genreLabel = genres.length ? genres.join(', ') : 'all genres';
  const decadeLabel = decades.length ? decades.join(', ') : 'all decades';
  const peakLabel  = peakMax >= 100 ? '' : ' \u00b7 peak \u2264 #' + peakMax;
  snfLog('Searching ' + genreLabel + ' \u00b7 ' + decadeLabel + ' \u00b7 ' + tierLabel + peakLabel + '\u2026', 'info');
  document.getElementById('snf-status-title').textContent = 'SEARCHING\u2026'; document.getElementById('snf-pulse-bar').style.display = 'block';
  document.getElementById('snf-btn-search').disabled = true;
  try {
    const r = await fetch(API + '/api/sniffer/gap-by-genre', {
      method:'POST', headers:authHdrs(),
      body: JSON.stringify({ genres, decades, tier, limit: SNF_PAGE, offset: snfOffset }),
    });
    const d = await r.json();
    if (!r.ok) throw new Error(d.detail || 'HTTP ' + r.status);

    // Server already filters by genre/decade/tier and paginates. Apply client-side
    // peak filter only when the user dialed it in (peakMax < 100).
    let incoming = d.results || [];
    if (peakMax < 100) {
      incoming = incoming.filter(function(r){
        const p = parseInt(r.peak_position);
        return isFinite(p) && p > 0 && p <= peakMax;
      });
    }

    snfTotal = d.total || 0;
    snfResults = append ? snfResults.concat(incoming) : incoming;
    snfOffset += (d.results || []).length;
    document.getElementById('snf-s-total').textContent = snfTotal.toLocaleString();
    document.getElementById('snf-s-owned').textContent = (d.owned || 0).toLocaleString();
    document.getElementById('snf-s-missing').textContent = (d.missing || 0).toLocaleString();
    document.getElementById('snf-status-title').textContent = 'ANALYSIS COMPLETE';
    const peakNote = peakMax < 100 ? ' \u00b7 peak filter \u2264 #' + peakMax : '';
    document.getElementById('snf-status-sub').textContent =
      snfTotal.toLocaleString() + ' master entries \u00b7 ' + (d.owned||0).toLocaleString() + ' owned \u00b7 ' + (d.missing||0).toLocaleString() + ' missing' + peakNote;
    snfRenderResults();
    snfUpdateLoadMore();
    snfLog('\u2713 Found ' + snfTotal.toLocaleString() + ' entries \u2014 ' + (d.missing||0).toLocaleString() + ' missing' + (d.library_source ? ' \u00b7 library via ' + d.library_source : ''), 'ok');
  } catch(e) {
    snfLog('\u2717 ' + e.message, 'err');
    document.getElementById('snf-status-title').textContent = 'ERROR';
  }
  document.getElementById('snf-btn-search').disabled = false; document.getElementById('snf-pulse-bar').style.display = 'none';
}

async function snfTrending(append) {
  const tag = document.getElementById('snf-trending-tag').value;
  if (!append) { snfResults = []; snfOffset = 0; _snfTrendingPage = 1; }
  snfLog('Fetching trending ' + tag + ' tracks (page ' + _snfTrendingPage + ')\u2026', 'info');
  document.getElementById('snf-status-title').textContent = 'SEARCHING\u2026'; document.getElementById('snf-pulse-bar').style.display = 'block';
  document.getElementById('snf-btn-search').disabled = true;
  try {
    const r = await fetch(API + '/api/sniffer/trending', {
      method:'POST', headers:authHdrs(),
      body: JSON.stringify({ tag, limit: 200, page: _snfTrendingPage }),
    });
    const d = await r.json();
    if (!r.ok) throw new Error(d.detail || 'HTTP ' + r.status);
    const newResults = (d.results || []).map(function(t) {
      return Object.assign({}, t, { chart_name: 'trending', chart_display: 'Trending', peak_position: null, weeks_on_chart: null, chart_year: null });
    });
    snfResults = append ? snfResults.concat(newResults) : newResults;
    snfTotal = snfResults.length;
    snfOffset = snfResults.length;
    document.getElementById('snf-s-total').textContent = snfTotal.toLocaleString();
    document.getElementById('snf-s-owned').textContent = (d.owned||0).toLocaleString();
    document.getElementById('snf-s-missing').textContent = (d.missing||0).toLocaleString();
    document.getElementById('snf-status-title').textContent = 'TRENDING RESULTS';
    document.getElementById('snf-status-sub').textContent = snfTotal + ' trending ' + tag + ' tracks \u00b7 page ' + _snfTrendingPage;
    snfRenderResults();
    // Show load more for trending pagination
    const lm = document.getElementById('snf-load-more');
    if (newResults.length >= 100) { lm.style.display = 'block'; document.getElementById('snf-load-count').textContent = 'Load page ' + (_snfTrendingPage + 1); }
    else { lm.style.display = 'none'; }
    snfLog('\u2713 Found ' + newResults.length + ' trending tracks', 'ok');
  } catch(e) {
    snfLog('\u2717 ' + e.message, 'err');
    document.getElementById('snf-status-title').textContent = 'ERROR';
  }
  document.getElementById('snf-btn-search').disabled = false; document.getElementById('snf-pulse-bar').style.display = 'none';
}

function snfLoadMore() {
  if (snfCurrentMode === 'gap') snfGapAnalysis(true);
  else { _snfTrendingPage++; snfTrending(true); }
}

function snfUpdateLoadMore() {
  const el = document.getElementById('snf-load-more');
  const rem = Math.max(0, snfTotal - snfOffset);
  if (rem > 0 && snfCurrentMode === 'gap') {
    el.style.display = 'block';
    document.getElementById('snf-load-count').textContent = rem.toLocaleString() + ' remaining';
  } else if (snfCurrentMode !== 'trending') { el.style.display = 'none'; }
}

function snfRenderResults() {
  const tbody = document.getElementById('snf-tbody');
  const empty = document.getElementById('snf-empty');
  tbody.innerHTML = '';
  let data = snfResults.slice();
  data.forEach(function(r, i) { r._origIdx = i; });
  const sortFns = { artist: function(r){return (r.artist||'').toLowerCase()}, title: function(r){return (r.title||'').toLowerCase()}, year: function(r){return r.chart_year||0}, peak: function(r){return r.peak_position||999}, status: function(r){return r.in_library?1:0}, score: function(r){return -(r.hit_score||0)} };
  const fn = sortFns[_snfSortKey] || sortFns.peak;
  data.sort(function(a,b) { const av=fn(a), bv=fn(b); if(av<bv)return _snfSortAsc?-1:1; if(av>bv)return _snfSortAsc?1:-1; return 0; });
  if (!data.length) { empty.style.display = 'block'; return; }
  empty.style.display = 'none';
  data.forEach(function(r, idx) {
    const tr = document.createElement('tr');
    tr.dataset.status = r.in_library ? 'owned' : 'missing';
    tr.dataset.text = ((r.artist||'') + ' ' + (r.title||'')).toLowerCase();
    tr.style.borderBottom = '1px solid var(--border)';
    if (snfCurrentFilter === 'missing' && r.in_library) tr.style.display = 'none';
    if (snfCurrentFilter === 'owned' && !r.in_library) tr.style.display = 'none';
    const statusHTML = r.in_library ? '<span style="color:var(--green)">\u2705 Owned</span>' : '<span style="color:var(--red)">\u274c Missing</span>';
    const peakHTML = r.peak_position ? '<span style="font-family:var(--display);font-size:1rem;color:var(--amber)">#'+r.peak_position+'</span>' : '<span style="color:var(--muted2)">\u2014</span>';
    // Prefer primary_chart_label (e.g. "Country #1 (1985)") when the new endpoint provides it
    const badgeText = r.primary_chart_label || r.chart_display || '';
    const srcList   = Array.isArray(r.sources) ? r.sources : [];
    const srcSuffix = srcList.length > 1 ? ' \u00b7 ' + srcList.length + ' sources' : '';
    const srcTitle  = srcList.length ? ' title="Sources: ' + esc(srcList.join(', ')) + '"' : '';
    const chartBadge = badgeText ? '<span'+srcTitle+' style="font-size:.5rem;padding:.1rem .35rem;border-radius:8px;background:rgba(245,166,35,.1);border:1px solid rgba(245,166,35,.25);color:var(--gold);white-space:nowrap;cursor:'+(srcList.length?'help':'default')+'">'+esc(badgeText)+srcSuffix+'</span>' : '\u2014';
    const listenersHTML = r.listeners ? '<span style="font-size:.55rem;color:var(--muted2);margin-left:.35rem">('+(r.listeners/1000).toFixed(0)+'k)</span>' : '';
    const actionHTML = r.in_library ? '' : '<button onclick="snfSearchTrack('+r._origIdx+')" style="padding:.25rem .6rem;background:var(--surface2);color:var(--gold);border:1px solid var(--gold);border-radius:4px;font-family:var(--mono);font-size:.55rem;cursor:pointer;letter-spacing:.5px;white-space:nowrap">\ud83d\udd0d SEARCH</button>';
    tr.innerHTML = '<td style="padding:.45rem .75rem;color:var(--muted)">'+esc(r.artist||'')+'</td><td style="padding:.45rem .75rem;color:var(--text)">'+esc(r.title||'')+listenersHTML+'</td><td style="padding:.45rem .75rem;color:var(--muted)">'+(r.chart_year||'\u2014')+'</td><td style="padding:.45rem .75rem">'+peakHTML+'</td><td style="padding:.45rem .75rem">'+chartBadge+'</td><td style="padding:.45rem .75rem">'+statusHTML+'</td><td style="padding:.45rem .75rem">'+actionHTML+'</td>';
    tbody.appendChild(tr);
  });
}

function snfSort(key) {
  if (_snfSortKey === key) _snfSortAsc = !_snfSortAsc;
  else { _snfSortKey = key; _snfSortAsc = true; }
  snfRenderResults();
}

function snfFilterView(filter) {
  snfCurrentFilter = filter;
  ['all','missing','owned'].forEach(function(f) {
    const btn = document.getElementById('snf-filter-' + f);
    if (!btn) return;
    if (f === filter) {
      btn.style.background = f==='all'?'var(--gold)':f==='missing'?'rgba(248,113,113,.2)':'rgba(74,222,128,.2)';
      btn.style.color = f==='all'?'#000':f==='missing'?'var(--red)':'var(--green)';
      btn.style.border = f==='all'?'none':f==='missing'?'1px solid var(--red)':'1px solid var(--green)';
    } else {
      btn.style.background = 'var(--surface2)';
      btn.style.color = f==='all'?'var(--muted)':f==='missing'?'var(--red)':'var(--green)';
      btn.style.border = '1px solid var(--border)';
    }
  });
  document.querySelectorAll('#snf-tbody tr').forEach(function(tr) {
    if (filter === 'all') tr.style.display = '';
    else if (filter === 'missing') tr.style.display = tr.dataset.status === 'missing' ? '' : 'none';
    else if (filter === 'owned') tr.style.display = tr.dataset.status === 'owned' ? '' : 'none';
  });
}

function snfFilterText(q) {
  const lq = q.toLowerCase();
  document.querySelectorAll('#snf-tbody tr').forEach(function(tr) {
    const textMatch = (tr.dataset.text||'').includes(lq);
    const filterMatch = snfCurrentFilter === 'all' || tr.dataset.status === snfCurrentFilter;
    tr.style.display = (textMatch && filterMatch) ? '' : 'none';
  });
}

// ── Prowlarr Multi-Card Search ──
var _snfCardCounter = 0;
var _snfCards = {}; // cardId -> { artist, results[] }

async function snfSearchTrack(idx) {
  const track = snfResults[idx];
  if (!track) return;
  const artistKey = (track.artist||'').toLowerCase().trim();
  const container = document.getElementById('snf-prowlarr-cards');

  // If card for this artist already exists, expand it and scroll to it
  var existingId = null;
  Object.keys(_snfCards).forEach(function(cid) {
    if (_snfCards[cid].artist === artistKey) existingId = cid;
  });
  if (existingId) {
    var existingCard = document.getElementById(existingId);
    if (existingCard) {
      var body = existingCard.querySelector('.snf-card-body');
      if (body) body.style.display = 'flex';
      var minBtn = existingCard.querySelector('.snf-card-min');
      if (minBtn) minBtn.textContent = '\u2212';
      existingCard.style.padding = '1.25rem';
      existingCard.scrollIntoView({behavior:'smooth'});
      return;
    }
  }

  _snfCardCounter++;
  var cardId = 'snf-card-' + _snfCardCounter;
  _snfCards[cardId] = { artist: artistKey, results: [], usenet_results: [] };

  var card = document.createElement('div');
  card.id = cardId;
  card.style.cssText = 'background:var(--surface);border:1px solid var(--gold);border-radius:10px;padding:1.25rem;';

  card.innerHTML =
    '<div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:.75rem">'
    + '<div>'
    + '<div style="font-family:var(--display);font-size:1rem;letter-spacing:1.5px;color:var(--gold)">PROWLARR RESULTS</div>'
    + '<div style="font-family:var(--mono);font-size:.55rem;color:var(--muted);margin-top:.15rem" id="' + cardId + '-sub">Searching albums by: ' + esc(track.artist) + '</div>'
    + '</div>'
    + '<div style="display:flex;gap:.35rem">'
    + '<button class="snf-card-min" onclick="snfToggleCard(\'' + cardId + '\')" style="background:none;border:none;color:var(--muted);font-size:1.1rem;cursor:pointer;line-height:1" title="Minimize">\u2212</button>'
    + '<button onclick="snfCloseCard(\'' + cardId + '\')" style="background:none;border:none;color:var(--muted);font-size:1rem;cursor:pointer;line-height:1" title="Close">\u2715</button>'
    + '</div>'
    + '</div>'
    + '<div class="snf-card-body" style="display:flex;flex-direction:column;gap:.5rem">'
    + '<div style="text-align:center;padding:1rem;color:var(--muted);font-family:var(--mono);font-size:.6rem">\ud83d\udd0d Searching Prowlarr for albums by ' + esc(track.artist) + '\u2026</div>'
    + '</div>';

  container.appendChild(card);
  card.scrollIntoView({behavior:'smooth'});
  snfLog('\ud83d\udd0d Searching Prowlarr for albums by: ' + track.artist, 'info');

  try {
    var r = await fetch(API + '/api/sniffer/search', {
      method:'POST', headers:authHdrs(),
      body: JSON.stringify({ artist: track.artist, title: track.title, album: null }),
    });
    var d = await r.json();
    if (!r.ok) throw new Error(d.detail || 'HTTP ' + r.status);
    var results = d.results || [];
    var usenetResults = d.usenet_results || [];
    _snfCards[cardId].results = results;
    _snfCards[cardId].usenet_results = usenetResults;
    var bodyEl = card.querySelector('.snf-card-body');
    var subEl = document.getElementById(cardId + '-sub');

    if (!results.length && !usenetResults.length) {
      bodyEl.innerHTML = '<div style="text-align:center;padding:1rem;color:var(--muted2);font-family:var(--mono);font-size:.6rem">No results found for ' + esc(track.artist) + '.</div>';
      snfLog('\u2717 No results for ' + track.artist, 'warn');
      return;
    }

    var totalCount = results.length + usenetResults.length;
    snfLog('\u2713 Found ' + totalCount + ' result(s) (' + results.length + ' torrent, ' + usenetResults.length + ' usenet)', 'ok');
    subEl.textContent = totalCount + ' result(s) for: ' + track.artist;

    var html = '';

    // ── Torrent results section ──
    if (results.length) {
      html += '<div style="font-family:var(--mono);font-size:.52rem;letter-spacing:2px;color:var(--gold);margin-top:.25rem;display:flex;align-items:center;gap:.5rem">\ud83e\uddf2 TORRENT RESULTS (' + results.length + ')<span style="flex:1;height:1px;background:var(--border);display:block"></span></div>';
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
          + '<button onclick="snfGrabGet(\'' + cardId + '\',' + i + ')" style="padding:.3rem .6rem;background:var(--gold);color:#000;border:none;border-radius:4px;font-family:var(--mono);font-size:.58rem;cursor:pointer" title="Send to download client">\u2b07 GET</button>'
          + (r.info_url ? '<a href="' + esc(r.info_url) + '" target="_blank" rel="noopener" style="display:inline-block;padding:.3rem .6rem;background:var(--surface3);color:var(--teal);border:1px solid var(--teal);border-radius:4px;font-family:var(--mono);font-size:.58rem;cursor:pointer;text-decoration:none" title="Open source page">\u2197 OPEN</a>' : '')
          + '<button onclick="snfCopyMagnet(\'' + cardId + '\',' + i + ')" style="padding:.3rem .6rem;background:var(--surface3);color:var(--purple);border:1px solid var(--purple);border-radius:4px;font-family:var(--mono);font-size:.58rem;cursor:pointer" title="Copy download URL">\ud83d\udd17 MAGNET</button>'
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
          + '<button onclick="snfGrabNzb(\'' + cardId + '\',' + i + ')" style="padding:.3rem .6rem;background:var(--amber);color:#000;border:none;border-radius:4px;font-family:var(--mono);font-size:.58rem;cursor:pointer" title="Send NZB to download client via Prowlarr">\u2b07 GET NZB</button>'
          + (r.info_url ? '<a href="' + esc(r.info_url) + '" target="_blank" rel="noopener" style="display:inline-block;padding:.3rem .6rem;background:var(--surface3);color:var(--teal);border:1px solid var(--teal);border-radius:4px;font-family:var(--mono);font-size:.58rem;cursor:pointer;text-decoration:none" title="Open source page">\u2197 OPEN</a>' : '')
          + '</div></div>';
      }).join('');
    }

    // ── Usenet setup hint (only if zero usenet results AND zero usenet indexers) ──
    if (!usenetResults.length && d.usenet_total === 0 && typeof d.usenet_total !== 'undefined') {
      html += '<div style="margin-top:.5rem;padding:.5rem .7rem;background:rgba(251,191,36,.06);border:1px solid rgba(251,191,36,.2);border-radius:6px;font-family:var(--mono);font-size:.48rem;color:var(--muted)">'
        + '\u2139\ufe0f No Usenet indexers detected in Prowlarr. Add a Newznab indexer (e.g. NZBGeek) to enable NZB search. '
        + 'Your NZB client also needs a <strong style="color:var(--amber)">charthound-music</strong> category pointing to your music folder.'
        + '</div>';
    }

    bodyEl.innerHTML = html;
  } catch(e) {
    var bodyEl2 = card.querySelector('.snf-card-body');
    bodyEl2.innerHTML = '<div style="text-align:center;padding:1rem;color:var(--red);font-family:var(--mono);font-size:.6rem">\u2717 ' + esc(e.message) + '</div>';
    snfLog('\u2717 Search failed: ' + e.message, 'err');
  }
}

function snfToggleCard(cardId) {
  var card = document.getElementById(cardId);
  if (!card) return;
  var body = card.querySelector('.snf-card-body');
  var btn = card.querySelector('.snf-card-min');
  if (!body || !btn) return;
  if (body.style.display === 'none') {
    body.style.display = 'flex';
    btn.textContent = '\u2212';
    card.style.padding = '1.25rem';
  } else {
    body.style.display = 'none';
    btn.textContent = '+';
    card.style.padding = '.6rem 1.25rem';
  }
}

function snfCloseCard(cardId) {
  var card = document.getElementById(cardId);
  if (card) card.remove();
  delete _snfCards[cardId];
}

async function snfGrabGet(cardId, resultIdx) {
  var cardData = _snfCards[cardId];
  if (!cardData) return;
  var result = cardData.results[resultIdx];
  if (!result) return;
  snfLog('\u2b07 Sending to download client: ' + result.title, 'info');
  try {
    var r = await fetch(API + '/api/sniffer/grab', {
      method:'POST', headers:authHdrs(),
      body: JSON.stringify({ download_url: result.download_url, title: result.title, indexer: result.indexer || '' }),
    });
    var d = await r.json();
    if (!r.ok) throw new Error(d.detail || 'HTTP ' + r.status);
    snfLog('\u2713 ' + d.message, 'ok');
    toast(d.message, 'ok');
  } catch(e) {
    snfLog('\u2717 Grab failed: ' + e.message, 'err');
    toast('Grab failed: ' + e.message, 'error');
  }
}

function snfCopyMagnet(cardId, resultIdx) {
  var cardData = _snfCards[cardId];
  if (!cardData) return;
  var result = cardData.results[resultIdx];
  if (!result || !result.download_url) { snfLog('No URL available', 'err'); return; }
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
    snfLog('\u2713 Download URL copied to clipboard', 'ok');
    toast('URL copied to clipboard', 'ok');
  } catch(e) {
    snfLog('\u2717 Copy failed: ' + e.message, 'err');
  }
}

async function snfGrabNzb(cardId, resultIdx) {
  var cardData = _snfCards[cardId];
  if (!cardData) return;
  var result = (cardData.usenet_results || [])[resultIdx];
  if (!result) return;
  snfLog('\u2b07 Sending NZB to download client: ' + result.title, 'info');
  try {
    var r = await fetch(API + '/api/sniffer/grab-nzb', {
      method:'POST', headers:authHdrs(),
      body: JSON.stringify({ download_url: result.download_url, title: result.title, indexer_id: result.indexer_id || 0 }),
    });
    var d = await r.json();
    if (!r.ok) throw new Error(d.detail || 'HTTP ' + r.status);
    snfLog('\u2713 ' + d.message, 'ok');
    toast(d.message, 'ok');
  } catch(e) {
    snfLog('\u2717 NZB grab failed: ' + e.message, 'err');
    toast('NZB grab failed: ' + e.message, 'error');
  }
}

function snfInit() {
  // Phase 3: no year-range fetch needed. Decades multi-select replaces sliders.
  // Initialize peak slider display (default = "All peaks")
  const peakEl = document.getElementById('snf-peak');
  if (peakEl) snfUpdatePeak(peakEl.value);
  snfInjectScrollNav();
  _loadCacheStatusStrip('snf-cache-info');
}

function snfInjectScrollNav() {
  if (document.getElementById('snf-scroll-nav')) return;
  const scroller = document.querySelector('.content-area') || document.documentElement;
  const nav = document.createElement('div');
  nav.id = 'snf-scroll-nav';
  nav.style.cssText = 'position:fixed;right:1.5rem;bottom:5rem;z-index:500;display:flex;flex-direction:column;gap:.4rem;opacity:0;transition:opacity .3s;pointer-events:none;';
  const btnStyle = 'width:36px;height:36px;border-radius:50%;background:var(--surface2);border:1px solid var(--border2);color:var(--gold);font-size:1rem;cursor:pointer;display:flex;align-items:center;justify-content:center;transition:all .15s;line-height:1;';
  nav.innerHTML = '<button style="' + btnStyle + '" title="Scroll to top" onclick="(document.querySelector(\'.content-area\')||document.documentElement).scrollTo({top:0,behavior:\'smooth\'})">↑</button><button style="' + btnStyle + '" title="Scroll to bottom" onclick="(document.querySelector(\'.content-area\')||document.documentElement).scrollTo({top:99999,behavior:\'smooth\'})">↓</button>';
  document.body.appendChild(nav);
  scroller.addEventListener('scroll', function() {
    var show = scroller.scrollTop > 200;
    nav.style.opacity = show ? '1' : '0';
    nav.style.pointerEvents = show ? 'auto' : 'none';
  }, {passive:true});
}

// \u2500\u2500 Kennel: Sniffer Download Path \u2500\u2500
async function saveSnifferPath() {
  const path = document.getElementById('cSnifferPath')?.value?.trim();
  if (!path) { setFeedback('snifferpath', 'Enter a download path.', 'error'); return; }
  try {
    const r = await fetch(API + '/api/kennel/save-sniffer-path', {
      method:'POST', headers:authHdrs(),
      body: JSON.stringify({ download_path: path }),
    });
    const d = await r.json();
    if (!r.ok) throw new Error(d.detail || 'Save failed');
    setFeedback('snifferpath', '\u2713 Download path saved.', 'ok');
    toast('Sniffer download path saved.', 'ok');
  } catch(e) { setFeedback('snifferpath', '\u2717 ' + e.message, 'error'); }
}

async function loadSnifferPath() {
  try {
    const r = await fetch(API + '/api/kennel/sniffer-path', {headers:authHdrs()});
    if (!r.ok) return;
    const d = await r.json();
    if (d.download_path) {
      const el = document.getElementById('cSnifferPath');
      if (el) el.value = d.download_path;
    }
  } catch(e) {}
}

