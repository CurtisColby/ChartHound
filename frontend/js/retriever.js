
// ── M5b: Genre Engine ────────────────────────────────────────────
let _geTimer = null;
async function geRun() {
  const r = await fetch(`${API}/api/retriever/genre-engine/start`, {method:'POST', headers:authHdrs()});
  if (!r.ok) { const d = await r.json().catch(() => ({})); toast(d.detail || `HTTP ${r.status}`, 'error'); return; }
  toast('Genre Engine started — runs in the background', 'ok'); setTimeout(gePoll, 800);
}
async function geStop() {
  await fetch(`${API}/api/retriever/genre-engine/stop`, {method:'POST', headers:authHdrs()});
  toast('Stopping — finished lookups are kept', 'ok');
}
async function gePoll() {
  try {
    const r = await fetch(`${API}/api/retriever/genre-engine/status`, {headers:authHdrs()});
    if (!r.ok) return;
    const d = await r.json();
    const running = d.status === 'running';
    const tot = (d.albums_total || 0) + (d.artists_total || 0), done = (d.albums_done || 0) + (d.artists_done || 0);
    const bar = document.getElementById('ge-bar'); if (bar) bar.style.width = (tot ? Math.round(100 * done / tot) : (d.status === 'done' ? 100 : 0)) + '%';
    const el = document.getElementById('ge-status');
    if (el) el.innerHTML = running
      ? `<span style="color:var(--teal)">Discogs ${d.albums_done.toLocaleString()}/${d.albums_total.toLocaleString()} · MusicBrainz ${d.artists_done.toLocaleString()}/${d.artists_total.toLocaleString()}</span><br>${esc(d.message || '')}`
      : (d.status === 'idle'
          ? (d.last_run ? `Last run ${stDate(d.last_run)} · ${(d.decided || 0).toLocaleString()} songs have a genre.` : 'Not run yet.')
          : esc(d.message || ''));
    const rb = document.getElementById('ge-btn-run'), sb = document.getElementById('ge-btn-stop');
    if (rb) { rb.disabled = running; rb.style.opacity = running ? '.35' : '1'; }
    if (sb) sb.style.display = running ? '' : 'none';
    clearTimeout(_geTimer);
    if (running) _geTimer = setTimeout(gePoll, 3000);
  } catch {}
}
document.addEventListener('DOMContentLoaded', () => setTimeout(gePoll, 2600));

// ── M5a: Online Genres → files ───────────────────────────────────
let _ogTimer = null;
let _geTax = null;
const _ger = {page: 1, pages: 1, total: 0, rows: [], sel: new Set()};

async function geTaxonomy() {
  if (_geTax) return _geTax;
  const r = await fetch(`${API}/api/retriever/genre-engine/taxonomy`, {headers:authHdrs()});
  _geTax = await r.json();
  const fs = document.getElementById('ger-family');
  if (fs && fs.options.length < 2)
    Object.entries(_geTax.families).forEach(([k, v]) => fs.insertAdjacentHTML('beforeend', `<option value="${k}">${esc(v)}</option>`));
  return _geTax;
}
function _gerFilters() {
  return {scope: document.getElementById('ger-scope').value,
          search: document.getElementById('ger-search').value.trim() || null,
          family: document.getElementById('ger-family').value || null,
          folder: document.getElementById('ger-folder').value.trim() || null,
          hide_same: !!document.getElementById('ger-hide-same')?.checked};
}
async function geOpenReview() {
  document.getElementById('ret-main').classList.add('ge-reviewing');
  document.getElementById('ge-review').style.display = '';
  await geTaxonomy();
  geLoad(1);
}
function geCloseReview() {
  gePlayStop();
  document.getElementById('ret-main').classList.remove('ge-reviewing');
  document.getElementById('ge-review').style.display = 'none';
}
async function geLoad(page) {
  page = Math.max(1, Math.min(page, _ger.pages || 1));
  const body = document.getElementById('ger-body');
  body.innerHTML = '<tr><td colspan="5" style="padding:1.5rem;text-align:center;color:var(--muted2)">Loading…</td></tr>';
  try {
    const r = await fetch(`${API}/api/retriever/genre-engine/review?page=${page}&size=100`, {method:'POST', headers:authHdrs(), body:JSON.stringify(_gerFilters())});
    const d = await r.json();
    if (!r.ok) throw new Error(d.detail || `HTTP ${r.status}`);
    Object.assign(_ger, {page: d.page, pages: Math.max(1, Math.ceil(d.total / d.size)), total: d.total, rows: d.rows, cur: -1});
    _ger.sel.clear();
    const thin = (d.check || 0) - (d.conflict || 0);
    document.getElementById('ger-count').innerHTML = `${d.total.toLocaleString()} songs`
      + (d.conflict ? ` · <span style="color:var(--red)">⚠ ${d.conflict.toLocaleString()} conflicts</span>` : '')
      + (thin > 0 ? ` · <span style="color:var(--amber)">? ${thin.toLocaleString()} little data</span>` : '');
    document.getElementById('ger-page').textContent = `page ${_ger.page} / ${_ger.pages}`;
    geRender();
  } catch (e) { body.innerHTML = `<tr><td colspan="5" style="padding:1.5rem;color:var(--red)">${esc(e.message)}</td></tr>`; }
}
function geRender() {
  const body = document.getElementById('ger-body');
  if (!_ger.rows.length) { body.innerHTML = '<tr><td colspan="5" style="padding:2rem;text-align:center;color:var(--muted2)">Nothing matches. Run the Genre Engine first, or change the filters.</td></tr>'; geSyncSel(); return; }
  let html = '', last = null;
  _ger.rows.forEach((x, i) => {
    if (x.artist !== last) {
      last = x.artist;
      const n = _ger.rows.filter(r => r.artist === x.artist).length;
      html += `<tr class="ger-artist"><td><input type="checkbox" onchange="geSelectArtist(${JSON.stringify(x.artist).replace(/"/g,'&quot;')}, this.checked)"></td>
        <td colspan="4">${esc(x.artist || '(unknown artist)')} <span style="color:var(--muted2);font-weight:normal">· ${n} on this page</span>
        <a href="#" onclick="geEditArtist(${i});return false" style="margin-left:.75rem;color:var(--amber);font-weight:normal;font-size:.62rem">✎ set genre for ALL songs by this artist</a>${geArtistLinks(x.artist)}</td></tr>
        <tr class="ger-edit" id="ger-aedit-${i}" style="display:none"><td></td><td colspan="4" id="ger-aedit-cell-${i}"></td></tr>`;
    }
    const why = `<a href="#" class="ger-why" onclick="geWhy(${i});return false" data-tip="Show what each source (Discogs, MusicBrainz, Last.fm) said about this song.">why?</a>`;
    const conf = x.confidence === 'conflict' ? `<span style="color:var(--red)" title="Sources disagree — worth a look">⚠ CONFLICT</span>${why}`
               : (x.confidence === 'thin' || x.confidence === 'check') ? `<span style="color:var(--amber)" title="Only one source knew this song — usually fine">? LITTLE DATA</span>${why}`
               : x.confidence === 'manual' ? '<span style="color:var(--pink)">EDITED</span>' : `<span style="color:var(--muted2)">ok</span>${why}`;
    html += `<tr id="ger-row-${i}"><td><input type="checkbox" class="ger-cb" data-i="${i}" ${_ger.sel.has(x.track_id) ? 'checked' : ''} onchange="geToggle(${i}, this.checked)"></td>
      <td>${gePlayBtn(x)}${esc(x.title)}${x.path ? `<button class="ger-copy" onclick="geCopyPath(${i}, event.shiftKey)" data-tip="Copies the folder this song lives in on your server (Shift-click: the file itself). In your file manager press Ctrl+L, paste, Enter. Delete it there if you want — the next Top Dogs ① SCAN drops missing songs.">📋</button>` : ''}</td>
      <td><span class="ger-new" onclick="geEditRow(${i})">${esc(x.proposed.join('; '))}</span></td>
      <td style="color:var(--muted2)">${esc(x.current.join('; ') || '—')}</td><td>${conf}</td></tr>
      <tr class="ger-edit" id="ger-edit-${i}" style="display:none"><td></td><td colspan="4" id="ger-edit-cell-${i}"></td></tr>
      <tr class="ger-whyrow" id="ger-why-${i}" style="display:none"><td></td><td colspan="4" id="ger-why-cell-${i}"></td></tr>`;
  });
  body.innerHTML = html;
  geSyncSel();
  geCursor(Math.min(_ger.cur ?? -1, _ger.rows.length - 1), false);
}
function geToggle(i, on) { const id = _ger.rows[i].track_id; on ? _ger.sel.add(id) : _ger.sel.delete(id); geSyncSel(); }
function geSelectAll(on) { _ger.rows.forEach(r => on ? _ger.sel.add(r.track_id) : _ger.sel.delete(r.track_id)); geRender(); }
function geSelectArtist(artist, on) { _ger.rows.filter(r => r.artist === artist).forEach(r => on ? _ger.sel.add(r.track_id) : _ger.sel.delete(r.track_id)); geRender(); }
function geSyncSel() {
  const b = document.getElementById('ger-write-sel'); if (b) { b.textContent = `✎ WRITE SELECTED (${_ger.sel.size})`; b.disabled = !_ger.sel.size; b.style.opacity = _ger.sel.size ? '1' : '.4'; }
  const a = document.getElementById('ger-write-all'); if (a) a.textContent = `✎ WRITE ALL ${_ger.total.toLocaleString()} MATCHING`;
}
function _geEditorHTML(fam, styles, saveCall) {
  const famOpts = Object.entries(_geTax.families).map(([k, v]) => `<option value="${k}" ${k === fam ? 'selected' : ''}>${esc(v)}</option>`).join('');
  const styleSel = (j) => `<select data-st="${j}"></select>`;
  return `<div class="ger-editor" data-styles='${JSON.stringify(styles || [])}'>
      <select data-fam onchange="geFillStyles(this.parentNode)">${famOpts}</select>${styleSel(0)}${styleSel(1)}${styleSel(2)}
      <button class="btn btn-sm" style="background:var(--teal);color:#000;font-size:.6rem" onclick="${saveCall}">SAVE</button>
      <button class="btn btn-outline btn-sm" style="font-size:.6rem" onclick="geRender()">CANCEL</button></div>`;
}
function geFillStyles(ed, initial) {
  const fam = ed.querySelector('[data-fam]').value;
  const fits = Object.entries(_geTax.styles).filter(([, v]) => v.families.includes(fam));
  const keep = initial || [...ed.querySelectorAll('[data-st]')].map(s => s.value);
  ed.querySelectorAll('[data-st]').forEach((sel, j) => {
    sel.innerHTML = '<option value="">— style —</option>' + fits.map(([k, v]) => `<option value="${k}">${esc(v.label)}${v.families.length > 1 ? ' ↔' : ''}</option>`).join('');
    if (keep[j] && fits.some(([k]) => k === keep[j])) sel.value = keep[j];
  });
}
function _geOpenEditor(rowId, cellId, fam, styles, saveCall) {
  geRender();
  document.getElementById(rowId).style.display = '';
  const cell = document.getElementById(cellId);
  cell.innerHTML = _geEditorHTML(fam, styles, saveCall);
  geFillStyles(cell.querySelector('.ger-editor'), styles);
}
function geEditRow(i) { const x = _ger.rows[i]; _geOpenEditor(`ger-edit-${i}`, `ger-edit-cell-${i}`, x.family, x.styles, `geSaveRow(${i}, this.parentNode)`); }
function geEditArtist(i) { const x = _ger.rows[i]; _geOpenEditor(`ger-aedit-${i}`, `ger-aedit-cell-${i}`, x.family, x.styles, `geSaveArtist(${i}, this.parentNode)`); }
function _geReadEditor(ed) {
  return {family: ed.querySelector('[data-fam]').value,
          styles: [...ed.querySelectorAll('[data-st]')].map(s => s.value).filter(Boolean)};
}
async function _geSave(body) {
  const r = await fetch(`${API}/api/retriever/genre-engine/override`, {method:'PUT', headers:authHdrs(), body:JSON.stringify(body)});
  const d = await r.json();
  if (!r.ok) { toast(d.detail || `HTTP ${r.status}`, 'error'); return; }
  toast(`Saved: ${d.genre.join('; ')} — ${d.updated} song${d.updated > 1 ? 's' : ''}`, 'success');
  geLoad(_ger.page);
}
function geSaveRow(i, ed) { _geSave({..._geReadEditor(ed), track_ids: [_ger.rows[i].track_id]}); }
function geSaveArtist(i, ed) {
  const a = _ger.rows[i].artist;
  if (!confirm(`Set this genre for EVERY song by ${a} (all pages)?`)) return;
  _geSave({..._geReadEditor(ed), artist: a});
}
async function geWrite(selectedOnly) {
  const n = selectedOnly ? _ger.sel.size : _ger.total;
  if (!n) return;
  if (!confirm(`Write the new genre to ${n.toLocaleString()} music file${n > 1 ? 's' : ''}?\n\nOnly the GENRE tag changes. Plex/Emby/Jellyfin get a library refresh when it finishes.`)) return;
  const body = selectedOnly ? {track_ids: [..._ger.sel]} : _gerFilters();
  const r = await fetch(`${API}/api/retriever/online-genres/apply`, {method:'POST', headers:authHdrs(), body:JSON.stringify(body)});
  if (!r.ok) { const d = await r.json().catch(() => ({})); toast(d.detail || `HTTP ${r.status}`, 'error'); return; }
  toast('Writing genres in the background — progress shows in the left panel', 'ok');
  ogPoll();
}
let _geRules = [];
async function geLoadRules() {
  await geTaxonomy();
  const r = await fetch(`${API}/api/retriever/genre-engine/rules`, {headers:authHdrs()});
  _geRules = (await r.json()).rules || [];
  geRenderRules();
}
function geRenderRules() {
  const box = document.getElementById('ge-rules'); if (!box) return;
  const fam = f => `<option value="skip" ${f === 'skip' ? 'selected' : ''}>⛔ Skip — don't tag</option>`
    + Object.entries(_geTax.families).map(([k, v]) => `<option value="${k}" ${k === f ? 'selected' : ''}>${esc(v)}</option>`).join('');
  const inp = 'background:var(--bg);border:1px solid var(--border2);border-radius:4px;color:var(--text);font-family:var(--mono);font-size:.62rem;padding:.35rem';
  box.innerHTML = _geRules.map((r, i) => `<div style="border:1px solid var(--border);border-radius:5px;padding:.35rem;margin-bottom:.35rem">
      <input value="${esc(r.prefix)}" oninput="_geRules[${i}].prefix=this.value" placeholder="/music/Folder or full NAS path" style="width:100%;box-sizing:border-box;${inp};margin-bottom:.3rem">
      <div style="display:flex;gap:.25rem"><select onchange="_geRules[${i}].family=this.value" style="flex:1;${inp}">${fam(r.family)}</select>
      <button onclick="_geRules.splice(${i},1);geRenderRules()" title="Remove rule" style="background:none;border:1px solid var(--border2);border-radius:4px;color:var(--red);cursor:pointer;font-size:.65rem;padding:0 .5rem">✕</button></div></div>`).join('')
    || '<div style="font-family:var(--mono);font-size:.55rem;color:var(--muted2)">No rules.</div>';
}
function geAddRule() { _geRules.push({prefix: '/music/', family: 'ccm'}); geRenderRules(); }
async function geSaveRules() {
  const r = await fetch(`${API}/api/retriever/genre-engine/rules`, {method:'PUT', headers:authHdrs(), body:JSON.stringify({rules:_geRules})});
  const d = await r.json();
  if (!r.ok) { toast(d.detail || `HTTP ${r.status}`, 'error'); return; }
  _geRules = d.rules; geRenderRules();
  toast(`${d.rules.length} folder rule${d.rules.length === 1 ? '' : 's'} saved — click RE-VOTE to apply`, 'success');
}

async function geRevote(btn) {
  btn.disabled = true; const t = btn.textContent; btn.textContent = 'RE-VOTING…';
  try {
    const r = await fetch(`${API}/api/retriever/genre-engine/revote`, {method:'POST', headers:authHdrs()});
    const d = await r.json();
    if (!r.ok) throw new Error(d.detail || `HTTP ${r.status}`);
    toast(d.message || 'Re-vote done', 'success'); gePoll();
    if (document.getElementById('ge-review').style.display !== 'none') geLoad(1);
  } catch (e) { toast(e.message, 'error'); }
  finally { btn.disabled = false; btn.textContent = t; }
}
async function ogStop() {
  await fetch(`${API}/api/retriever/online-genres/stop`, {method:'POST', headers:authHdrs()});
  toast('Stopping after the current file', 'ok');
}
async function ogPoll() {
  try {
    const r = await fetch(`${API}/api/retriever/online-genres/status`, {headers:authHdrs()});
    const d = await r.json();
    const el = document.getElementById('og-status'), stop = document.getElementById('og-btn-stop');
    const running = d.status === 'running';
    if (stop) stop.style.display = running ? '' : 'none';
    if (el) el.innerHTML = running
      ? `<span style="color:var(--teal)">Writing ${d.done.toLocaleString()} / ${d.total.toLocaleString()}</span> · ${d.written.toLocaleString()} ok · ${(d.not_found||0).toLocaleString()} not found · ${d.failed} failed<br>${esc(d.message || '')}`
      : (d.status === 'idle' ? '' : esc(d.message || ''));
    clearTimeout(_ogTimer);
    if (running) _ogTimer = setTimeout(ogPoll, 2000);
  } catch {}
}
document.addEventListener('DOMContentLoaded', () => setTimeout(ogPoll, 2500));


// HEARTBEAT
function startHeartbeat() {
  if (heartbeatTimer) clearInterval(heartbeatTimer);
  heartbeatTimer = setInterval(async () => {
    await loadAllStatuses();
    const el = document.getElementById('navHeartbeat');
    if (el) el.textContent = `⟳ ${new Date().toLocaleTimeString()}`;
  }, 60000);
}

// DOT HELPERS
function setCardDot(service, cls) {
  const el = document.getElementById(`cDot-${service}`);
  if (el) el.className = `conn-card-dot${cls?' '+cls:''}`;
}
function setVerified(service, ts, ok) {
  const el = document.getElementById(`cVerified-${service}`);
  if (!el) return;
  if (ok && ts) {
    const d = new Date(ts);
    el.className = 'conn-verified ok';
    el.textContent = `✓ ${d.toLocaleDateString()} ${d.toLocaleTimeString()}`;
  } else { el.className = 'conn-verified error'; el.textContent = 'NOT VERIFIED'; }
}
function setFeedback(service, msg, type) {
  const el = document.getElementById(`cFeedback-${service}`);
  if (el) { el.textContent = msg; el.className = `conn-feedback${type?' '+type:''}`; }
}
function refreshAllDots() {
  const hmap = {plex:'Plex', emby:'Emby', jellyfin:'Jellyfin', lastfm:'Lastfm', prowlarr:'Prowlarr', discogs:'Discogs'};
  for (const [svc,key] of Object.entries(hmap)) {
    const el = document.getElementById(`hDot${key}`);
    if (el) el.className = `dot${connState[svc]?' ok':''}`;
  }
  const vmap = {plex:'Plex',emby:'Emby',jellyfin:'Jellyfin',lastfm:'Lastfm',prowlarr:'Prowlarr',radarr:'Radarr',sonarr:'Sonarr',qbittorrent:'Qbittorrent',discogs:'Discogs'};
  for (const [svc,key] of Object.entries(vmap)) {
    const dot = document.getElementById(`vDot${key}`);
    const status = document.getElementById(`vStatus${key}`);
    if (!dot||!status) continue;
    // Download client: check all three types
    const isConn = svc === 'qbittorrent'
      ? (connState.qbittorrent || connState.deluge || connState.transmission)
      : connState[svc];
    dot.className = isConn ? 'dot ok' : 'dot';
    status.className = isConn ? 'vault-row-status ok' : 'vault-row-status warn';
    status.textContent = isConn ? 'CONNECTED' : 'NOT VERIFIED';
  }
  const navDot = document.getElementById('navDotKennel');
  if (navDot) navDot.className = `nav-conn-dot${(connState.plex||connState.emby||connState.jellyfin)?' ok':''}`;
  // Inject rollup button + auto-collapse connected cards on first paint
  injectConnRollupButtons();
}

// ── Kennel conn-card rollup ──
var _connRollupInitDone = {};  // per-service: has user interacted? prevents auto-collapse override on subsequent refreshAllDots() calls within the same page load
function injectConnRollupButtons() {
  const cards = document.querySelectorAll('.conn-card[id^="connCard-"]');
  cards.forEach(function(card) {
    const service = card.id.replace('connCard-', '');
    const header = card.querySelector('.conn-card-header');
    if (!header) return;
    let btn = header.querySelector('.conn-collapse-btn');
    if (!btn) {
      btn = document.createElement('button');
      btn.className = 'conn-collapse-btn';
      btn.onclick = function(e) { e.stopPropagation(); toggleConnCard(service); };
      header.appendChild(btn);
    }
    // First render this page-load: check localStorage for saved user choice, else fall back to auto-collapse-if-connected
    if (!_connRollupInitDone[service]) {
      let saved = null;
      try { saved = localStorage.getItem('ch_connCard_' + service); } catch(e) {}
      if (saved === 'collapsed')      card.classList.add('collapsed');
      else if (saved === 'open')      card.classList.remove('collapsed');
      else if (connState[service])    card.classList.add('collapsed');     // default: connected → collapsed
      else                             card.classList.remove('collapsed'); // default: not connected → open
      _connRollupInitDone[service] = true;
    }
    // Update button label based on current state
    btn.textContent = card.classList.contains('collapsed') ? '▼ EXPAND' : '▲ COLLAPSE';
  });
}
function toggleConnCard(service) {
  const card = document.getElementById('connCard-' + service);
  if (!card) return;
  card.classList.toggle('collapsed');
  const btn = card.querySelector('.conn-collapse-btn');
  if (btn) btn.textContent = card.classList.contains('collapsed') ? '▼ EXPAND' : '▲ COLLAPSE';
  // Persist user choice across page loads
  try { localStorage.setItem('ch_connCard_' + service, card.classList.contains('collapsed') ? 'collapsed' : 'open'); } catch(e) {}
}

// TOAST
function toast(msg, type='') {
  const el = document.createElement('div');
  el.className = `toast ${type}`; el.textContent = msg;
  el.style.cursor = 'pointer';
  el.title = 'Click to dismiss';
  el.onclick = () => el.remove();
  document.getElementById('toastWrap').appendChild(el);
  setTimeout(() => { if (el.parentNode) el.remove(); }, 4200);
}

// ── DONATION MODAL ──
function openDonationModal() {
  const m = document.getElementById('donationModal');
  if (m) m.classList.remove('hidden');
}
function closeDonationModal() {
  const m = document.getElementById('donationModal');
  if (m) m.classList.add('hidden');
}
async function copyDonationAddr(elId, addr) {
  const el = document.getElementById(elId);
  let ok = false;
  // Try modern clipboard API first (works on HTTPS + localhost)
  if (navigator.clipboard && window.isSecureContext) {
    try { await navigator.clipboard.writeText(addr); ok = true; } catch(e) { ok = false; }
  }
  // Fallback for HTTP: use a hidden textarea + execCommand
  if (!ok) {
    try {
      const ta = document.createElement('textarea');
      ta.value = addr;
      ta.style.position = 'fixed';
      ta.style.top = '-9999px';
      document.body.appendChild(ta);
      ta.select();
      ok = document.execCommand('copy');
      document.body.removeChild(ta);
    } catch(e) { ok = false; }
  }
  if (ok && el) {
    const orig = el.textContent.trim();
    el.classList.add('copied');
    el.textContent = '✓ COPIED — ' + orig.substring(0, 8) + '…' + orig.substring(orig.length - 6);
    toast('Address copied to clipboard', 'ok');
    setTimeout(() => {
      el.textContent = orig;
      el.classList.remove('copied');
    }, 2000);
  } else {
    toast('Copy failed — select the address and copy manually', 'error');
  }
}
// ESC key closes donation modal
document.addEventListener('keydown', function(e) {
  if (e.key === 'Escape') {
    const m = document.getElementById('donationModal');
    if (m && !m.classList.contains('hidden')) closeDonationModal();
  }
});
function esc(s) { return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;'); }
function escJs(s) { return String(s).replace(/\\/g,'\\\\').replace(/'/g,"\\'").replace(/"/g,'\\"'); }


// ── Genre Review ▶ preview player ──
const _GE_PLAYABLE = ['.mp3', '.flac', '.m4a', '.aac', '.ogg', '.oga', '.opus', '.wav'];
const _gePlay = {audio: null, id: null};
function gePlayBtn(x) {
  const ok = _GE_PLAYABLE.includes(x.ext);
  const on = ok && _gePlay.id === x.track_id;
  return `<button class="ger-play${on ? ' on' : ''}" data-tid="${x.track_id}" ${ok ? `onclick="gePlay(${x.track_id})" title="Play / stop"` : `disabled title="${esc(x.ext || 'This file type')} can't play in a browser"`}>${on ? '■' : '▶'}</button>`;
}
function _gePlayMark() {
  document.querySelectorAll('.ger-play[data-tid]').forEach(b => {
    const on = +b.dataset.tid === _gePlay.id;
    b.classList.toggle('on', on); if (!b.disabled) b.textContent = on ? '■' : '▶';
  });
}
function gePlayStop() {
  if (_gePlay.audio) { _gePlay.audio.pause(); _gePlay.audio.removeAttribute('src'); _gePlay.audio.load(); }
  _gePlay.id = null; _gePlayMark();
}
async function gePlay(id) {
  if (_gePlay.id === id) { gePlayStop(); return; }
  gePlayStop();
  try {
    const r = await fetch(`${API}/api/retriever/play-token/${id}`, {method: 'POST', headers: authHdrs()});
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    const {t} = await r.json();
    if (!_gePlay.audio) {
      _gePlay.audio = new Audio();
      _gePlay.audio.addEventListener('ended', gePlayStop);
      _gePlay.audio.addEventListener('error', () => { if (_gePlay.id !== null) { toast('Could not play this file.', 'error'); gePlayStop(); } });
    }
    _gePlay.id = id; _gePlayMark();
    _gePlay.audio.src = `${API}/api/retriever/play/${id}?t=${encodeURIComponent(t)}`;
    await _gePlay.audio.play();
  } catch (e) {
    if (e && e.name === 'AbortError') return;
    toast(`Play failed: ${e.message || e}`, 'error'); gePlayStop();
  }
}


// ── Review helpers: artist links, why?, copy path, keyboard ──
function geArtistLinks(artist) {
  if (!artist) return '';
  const q = encodeURIComponent(artist);
  return `<span class="ger-links"><a href="https://en.wikipedia.org/w/index.php?search=${q}" target="_blank" rel="noopener" data-tip="Open this artist on Wikipedia — the box at the top right lists their genres.">🌐 Wikipedia</a>`
    + `<a href="https://www.discogs.com/search/?q=${q}&type=artist" target="_blank" rel="noopener" data-tip="Open this artist on Discogs — Discogs carries the most weight in ChartHound's genre vote.">💿 Discogs</a></span>`;
}

async function geWhy(i) {
  const row = document.getElementById(`ger-why-${i}`), cell = document.getElementById(`ger-why-cell-${i}`);
  if (!row) return;
  if (row.style.display === '') { row.style.display = 'none'; return; }
  row.style.display = ''; cell.innerHTML = '<span style="color:var(--muted2)">Loading…</span>';
  try {
    const r = await fetch(`${API}/api/retriever/genre-engine/why/${_ger.rows[i].track_id}`, {headers: authHdrs()});
    const d = await r.json();
    if (!r.ok) throw new Error(d.detail || `HTTP ${r.status}`);
    const lines = d.sources.map(s => `<div><span class="ger-src">${esc(s.name)} <i>×${s.weight}</i></span>${s.tags.length ? esc(s.tags.join(', ')) : '<span style="color:var(--muted2)">— nothing found</span>'}</div>`).join('');
    cell.innerHTML = `<div class="ger-whybox">${lines}${d.decided_by ? `<div style="color:var(--amber);margin-top:.3rem">Family decided by ${esc(d.decided_by)}; the sources above only picked the style.</div>` : ''}<div style="color:var(--muted2);margin-top:.3rem">×N = vote weight. Tags that aren't real genres ("seen live", "favorites") are ignored.</div></div>`;
  } catch (e) { cell.innerHTML = `<span style="color:var(--red)">${esc(e.message)}</span>`; }
}

function _geCopyText(text) {
  // navigator.clipboard needs https or localhost; fall back for http:// LAN addresses
  if (navigator.clipboard && window.isSecureContext) return navigator.clipboard.writeText(text);
  const ta = document.createElement('textarea');
  ta.value = text; ta.setAttribute('readonly', ''); ta.style.cssText = 'position:fixed;top:-1000px;opacity:0';
  document.body.appendChild(ta); ta.select();
  let ok = false; try { ok = document.execCommand('copy'); } catch {}
  ta.remove();
  return ok ? Promise.resolve() : Promise.reject(new Error('Copy blocked by the browser'));
}
function geCopyPath(i, fileOnly) {
  const p = _ger.rows[i]?.path; if (!p) return;
  const text = fileOnly ? p : p.replace(/\/[^/]*$/, '');
  _geCopyText(text).then(() => toast(`Copied: ${text}`, 'ok'))
    .catch(() => { prompt('Copy this path (Ctrl+C):', text); });
}

function geCursor(i, scroll = true) {
  document.querySelectorAll('#ger-body tr.ger-cur').forEach(tr => tr.classList.remove('ger-cur'));
  _ger.cur = i;
  const tr = i >= 0 ? document.getElementById(`ger-row-${i}`) : null;
  if (!tr) return;
  tr.classList.add('ger-cur');
  if (scroll) tr.scrollIntoView({block: 'nearest'});
}
document.addEventListener('keydown', e => {
  const rv = document.getElementById('ge-review');
  if (!rv || rv.style.display === 'none' || !document.getElementById('panel-retriever')?.classList.contains('active')) return;
  if (e.ctrlKey || e.metaKey || e.altKey) return;
  const tag = (e.target.tagName || '').toLowerCase();
  if (tag === 'input' || tag === 'select' || tag === 'textarea' || e.target.isContentEditable) {
    if (e.key === 'Escape') e.target.blur();
    return;
  }
  const n = _ger.rows.length; if (!n) return;
  const cur = _ger.cur ?? -1, k = e.key.toLowerCase();
  if (k === 'arrowdown' || k === 'j') { geCursor(Math.min(n - 1, cur + 1)); e.preventDefault(); return; }
  if (k === 'arrowup' || k === 'k')   { geCursor(Math.max(0, cur - 1)); e.preventDefault(); return; }
  if (k === 'escape') { document.querySelectorAll('#ger-body tr.ger-edit, #ger-body tr.ger-whyrow').forEach(tr => tr.style.display = 'none'); return; }
  if (cur < 0) return;
  const x = _ger.rows[cur];
  if (k === ' ') { if (_GE_PLAYABLE.includes(x.ext)) gePlay(x.track_id); e.preventDefault(); }
  else if (k === 'x') { const cb = document.querySelector(`.ger-cb[data-i="${cur}"]`); if (cb) { cb.checked = !cb.checked; geToggle(cur, cb.checked); } }
  else if (k === 'e') { geEditRow(cur); geCursor(cur, false); e.preventDefault(); }
  else if (k === 'w') geWhy(cur);
  else if (k === 'c') geCopyPath(cur, e.shiftKey);
});
