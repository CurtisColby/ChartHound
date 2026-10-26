
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
// ── Job progress (M10c.5): phase, %, time left — left panel + Review banner ──
const _jobRate = {};
let _geRunning = false, _geWasRunning = false, _ogRunning = false;
function _jobProgress(kind, phase, done, total) {
  const now = Date.now(), k = _jobRate[kind];
  if (!k || k.phase !== phase || done < k.d0) _jobRate[kind] = {phase, t0: now, d0: done};
  const s = _jobRate[kind], secs = (now - s.t0) / 1000, rate = (done - s.d0) / Math.max(secs, 1);
  let eta = '';
  if (total && done < total && secs > 8 && rate > 0) {
    const left = (total - done) / rate;
    eta = left < 60 ? '≈ under a minute left' : left < 3600 ? `≈ ${Math.round(left / 60)} min left` : `≈ ${(left / 3600).toFixed(1)} h left`;
  }
  return {pct: total ? Math.min(100, Math.round(100 * done / total)) : 0, eta};
}
function _geJobView(d) {
  const ph = d.phase || '';
  if (ph === 'lookups') {
    const done = (d.albums_done || 0) + (d.artists_done || 0), total = (d.albums_total || 0) + (d.artists_total || 0);
    return {label: `Looking up genres — Discogs ${(d.albums_done||0).toLocaleString()}/${(d.albums_total||0).toLocaleString()} · MusicBrainz ${(d.artists_done||0).toLocaleString()}/${(d.artists_total||0).toLocaleString()}`, done, total};
  }
  if (ph === 'fingerprint') return {label: `🎧 Fingerprinting — ${(d.fp_done||0).toLocaleString()} / ${(d.fp_total||0).toLocaleString()} songs`, done: d.fp_done || 0, total: d.fp_total || 0};
  if (ph === 'mbrec') return {label: `🎧 MusicBrainz genres — ${(d.rec_done||0).toLocaleString()} / ${(d.rec_total||0).toLocaleString()} recordings`, done: d.rec_done || 0, total: d.rec_total || 0};
  if (ph === 'voting') return {label: `Voting on genres — ${(d.vote_done||0).toLocaleString()} / ${(d.vote_total||0).toLocaleString()} songs`, done: d.vote_done || 0, total: d.vote_total || 0};
  if (ph === 'saving') return {label: 'Saving decisions…', done: 1, total: 1};
  return {label: 'Preparing…', done: 0, total: 0};
}
function _jobBanner(label, pct, eta, msg) {
  const b = document.getElementById('ger-job'); if (!b) return;
  if (!label) { b.style.display = 'none'; return; }
  b.style.display = '';
  document.getElementById('ger-job-label').textContent = `${label}  ·  ${pct}%`;
  document.getElementById('ger-job-eta').textContent = eta;
  document.getElementById('ger-job-bar').style.width = pct + '%';
  document.getElementById('ger-job-msg').textContent = msg || '';
}
function _jobLocks() {
  const busy = _geRunning;
  const rv = document.getElementById('ge-btn-revote');
  if (rv) { rv.disabled = busy; rv.style.opacity = busy ? '.35' : '1'; rv.title = busy ? 'Wait for the Genre Engine to finish' : 'Recalculate genres and CHECK flags from saved lookups — no internet lookups, takes seconds. Your manual edits are kept.'; }
}
async function gePoll() {
  try {
    const r = await fetch(`${API}/api/retriever/genre-engine/status`, {headers:authHdrs()});
    if (!r.ok) return;
    const d = await r.json();
    const running = d.status === 'running';
    _geRunning = running;
    const v = _geJobView(d), p = running ? _jobProgress('ge', d.phase, v.done, v.total) : {pct: d.status === 'done' ? 100 : 0, eta: ''};
    const bar = document.getElementById('ge-bar'); if (bar) bar.style.width = p.pct + '%';
    if (running) _jobBanner('🧠 Genre Engine — ' + v.label, p.pct, p.eta, d.message);
    else if (!_ogRunning) _jobBanner('');
    _jobLocks();
    if (_geWasRunning && !running) {
      toast(d.message || 'Genre Engine finished', d.status === 'failed' ? 'error' : 'success');
      if (document.getElementById('ge-review')?.style.display !== 'none') geLoad(_ger.page || 1);
    }
    _geWasRunning = running;
    const el = document.getElementById('ge-status');
    if (el) el.innerHTML = running
      ? `<span style="color:var(--teal)">${esc(v.label)} · ${p.pct}%${p.eta ? ' · ' + p.eta : ''}</span><br>${esc(d.message || '')}`
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
  gePoll(); ogPoll();
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
    Object.assign(_ger, {page: d.page, pages: Math.max(1, Math.ceil(d.total / d.size)), total: d.total, rows: d.rows, cur: -1, flagged: d.check || 0});
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
  const se = document.getElementById('ger-seledit'); if (se) { se.innerHTML = ''; se.style.display = 'none'; }
  const body = document.getElementById('ger-body');
  if (!_ger.rows.length) { body.innerHTML = '<tr><td colspan="5" style="padding:2rem;text-align:center;color:var(--muted2)">Nothing matches. Run the Genre Engine first, or change the filters.</td></tr>'; geSyncSel(); return; }
  const q = v => JSON.stringify(v).replace(/"/g, '&quot;');
  const keyOf = r => r.akey || r.artist;
  let html = '', last = null, lastAl = null, multi = false;
  _ger.rows.forEach((x, i) => {
    const ak = keyOf(x);
    if (ak !== last) {
      last = ak; lastAl = null;
      const grp = _ger.rows.filter(r => keyOf(r) === ak);
      multi = new Set(grp.map(r => r.alkey)).size > 1;
      const okAll = grp.some(_geCanOk) ? `<a href="#" class="ger-chip ok" onclick="geApprove(false, ${q(x.artist)});return false" data-tip="Keep the current genre for every song by this artist (all pages) as your own decision and write it to the files.">✓ artist</a>` : '';
      const alChip = multi ? '' : `<a href="#" class="ger-chip" onclick="geEditAlbum(${i});return false" data-tip="Set one genre for every song in this album's folder (all pages, CD1/Disc 2 included). Other albums are not touched.">✎ album</a>`;
      html += `<tr class="ger-artist"><td><input type="checkbox" ${grp.every(r => _ger.sel.has(r.track_id)) ? 'checked' : ''} onchange="geSelectArtist(${q(ak)}, this.checked)"></td>
        <td colspan="4"><span class="ger-aname">${esc(x.artist || '(unknown artist)')}</span>${multi ? '' : `<span class="ger-alname">💿 ${esc(x.album || '(no album)')}</span>`}<span class="ger-cnt">${grp.length}</span>
        <span class="ger-chips"><a href="#" class="ger-chip" onclick="geEditArtist(${i});return false" data-tip="Set one genre for EVERY song by this artist (all pages).">✎ artist</a>${alChip}${okAll}${geArtistLinks(x.artist)}</span></td></tr>
        <tr class="ger-edit" id="ger-aedit-${i}" style="display:none"><td></td><td colspan="4" id="ger-aedit-cell-${i}"></td></tr>`;
      if (!multi) { lastAl = x.alkey; html += `<tr class="ger-edit" id="ger-bedit-${i}" style="display:none"><td></td><td colspan="4" id="ger-bedit-cell-${i}"></td></tr>`; }
    }
    if (multi && x.alkey !== lastAl) {
      lastAl = x.alkey;
      const grp = _ger.rows.filter(r => keyOf(r) === ak && r.alkey === x.alkey);
      html += `<tr class="ger-album"><td><input type="checkbox" ${grp.every(r => _ger.sel.has(r.track_id)) ? 'checked' : ''} onchange="geSelectAlbum(${i}, this.checked)"></td>
        <td colspan="4">💿 ${esc(x.album || '(no album)')}<span class="ger-cnt">${grp.length}</span>
        <span class="ger-chips"><a href="#" class="ger-chip" onclick="geEditAlbum(${i});return false" data-tip="Set one genre for every song in this album's folder (all pages, CD1/Disc 2 included). Other albums are not touched.">✎ album</a></span></td></tr>
        <tr class="ger-edit" id="ger-bedit-${i}" style="display:none"><td></td><td colspan="4" id="ger-bedit-cell-${i}"></td></tr>`;
    }
    const why = `<a href="#" class="ger-why" onclick="geWhy(${i});return false" data-tip="Show what each source (Discogs, MusicBrainz, Last.fm) said about this song. (Key: W)">why?</a>`;
    const ok = _geCanOk(x) ? `<button class="ger-ok" onclick="geApprove([${x.track_id}])" data-tip="Looks right — keep this genre as your decision and write it to the file. It stops counting as a conflict and the engine never changes it. (Key: Y)">✓</button>` : '';
    const redo = `<button class="ger-redo" onclick="geReset(${i})" data-tip="Let the engine decide again — removes your edit or ✓ for this song and re-votes it from saved lookups (no internet, a few seconds).">↻</button>`;
    const badge = (cls, txt, tip) => `<span class="ger-badge ${cls}" data-tip="${tip}">${txt}</span>`;
    const conf = x.confidence === 'conflict' ? ok + badge('bad', '⚠ CONFLICT', 'Sources disagree — worth a look') + why
               : (x.confidence === 'thin' || x.confidence === 'check') ? ok + badge('warn', '? LITTLE', 'Only one source knew this song — usually fine') + why
               : x.confidence === 'none' ? badge('none', 'NO DATA', 'No source knew this song — set its genre by hand')
               : x.confidence === 'manual' ? (x.checked ? badge('good', '✓ CHECKED', 'You confirmed this genre') : badge('edit', 'EDITED', 'You set this genre by hand')) + redo + (x.checked ? why : '')
               : badge('fine', 'ok', 'Sources agree') + why;
    html += `<tr id="ger-row-${i}" class="ger-song"><td><input type="checkbox" class="ger-cb" data-i="${i}" ${_ger.sel.has(x.track_id) ? 'checked' : ''} onchange="geToggle(${i}, this.checked)"></td>
      <td>${gePlayBtn(x)}${esc(x.title)}${x.heard ? `<span class="ger-heard" data-tip="The fingerprint heard a different artist. Fix the artist tag in 🎤 ARTIST CHECK (shown there as 🎧 HEARD AS).">🎧 ${esc(x.heard)}</span>` : ''}${x.path ? `<button class="ger-copy" onclick="geCopyPath(${i}, event.shiftKey)" data-tip="Copies the folder this song lives in on your server (Shift-click: the file itself). In your file manager press Ctrl+L, paste, Enter. Delete it there if you want — the next Top Dogs ① SCAN drops missing songs.">📋</button>` : ''}</td>
      <td><span class="ger-new" onclick="geEditRow(${i})">${x.proposed.length ? esc(x.proposed.join('; ')) : '<i style="color:var(--muted2)">— set genre</i>'}</span></td>
      <td style="color:var(--muted2)">${esc(x.current.join('; ') || '—')}</td><td class="ger-conf">${conf}</td></tr>
      <tr class="ger-edit" id="ger-edit-${i}" style="display:none"><td></td><td colspan="4" id="ger-edit-cell-${i}"></td></tr>
      <tr class="ger-whyrow" id="ger-why-${i}" style="display:none"><td></td><td colspan="4" id="ger-why-cell-${i}"></td></tr>`;
  });
  body.innerHTML = html;
  geSyncSel();
  geCursor(Math.min(_ger.cur ?? -1, _ger.rows.length - 1), false);
}
async function geReset(i) {
  if (_geRunning) { toast('Wait for the Genre Engine to finish — progress is shown at the top', 'info'); return; }
  const x = _ger.rows[i];
  if (!confirm(`Let the engine decide "${x.title}" again?\n\nYour edit / ✓ for this song is removed. Saved lookups only — no internet.`)) return;
  toast('Re-deciding…', 'info');
  try {
    const r = await fetch(`${API}/api/retriever/genre-engine/reset`, {method:'POST', headers:authHdrs(), body:JSON.stringify({track_ids: [x.track_id]})});
    const d = await r.json();
    if (!r.ok) throw new Error(d.detail || `HTTP ${r.status}`);
    const s = d.songs[0] || {};
    toast(s.genre && s.genre.length ? `Engine says: ${s.genre.join('; ')}` : 'No source knows this song — set it by hand.', 'success');
    geLoad(_ger.page);
  } catch (e) { toast(e.message, 'error'); }
}
function geToggle(i, on) { const id = _ger.rows[i].track_id; on ? _ger.sel.add(id) : _ger.sel.delete(id); geSyncSel(); }
function geSelectAll(on) { _ger.rows.forEach(r => on ? _ger.sel.add(r.track_id) : _ger.sel.delete(r.track_id)); geRender(); }
function geSelectAlbum(i, on) { const x = _ger.rows[i], ak = x.akey || x.artist; _ger.rows.filter(r => (r.akey || r.artist) === ak && r.alkey === x.alkey).forEach(r => on ? _ger.sel.add(r.track_id) : _ger.sel.delete(r.track_id)); geRender(); }
function geSelectArtist(ak, on) { _ger.rows.filter(r => (r.akey || r.artist) === ak).forEach(r => on ? _ger.sel.add(r.track_id) : _ger.sel.delete(r.track_id)); geRender(); }
function geSyncSel() {
  const hd = document.getElementById('ger-all'); if (hd) hd.checked = _ger.rows.length > 0 && _ger.rows.every(r => _ger.sel.has(r.track_id));
  const flagged = _ger.rows.filter(_geCanOk).length;
  const pg = document.getElementById('ger-ok-page'); if (pg) { pg.textContent = `✓ THIS PAGE (${flagged})`; pg.disabled = !flagged; pg.style.opacity = flagged ? '1' : '.4'; }
  const al = document.getElementById('ger-ok-all'); if (al) { const n = _ger.flagged || 0; al.textContent = `✓ ALL ${n.toLocaleString()} MATCHING`; al.disabled = !n; al.style.opacity = n ? '1' : '.4'; }
  const o = document.getElementById('ger-ok-sel'); if (o) { o.textContent = `✓ LOOKS RIGHT (${_ger.sel.size})`; o.disabled = !_ger.sel.size; o.style.opacity = _ger.sel.size ? '1' : '.4'; }
  const es = document.getElementById('ger-edit-sel'); if (es) { es.textContent = `✎ EDIT SELECTED (${_ger.sel.size})`; es.disabled = !_ger.sel.size; es.style.opacity = _ger.sel.size ? '1' : '.4'; }
  const b = document.getElementById('ger-write-sel'); if (b) { b.textContent = `✎ WRITE SELECTED (${_ger.sel.size})`; b.disabled = !_ger.sel.size; b.style.opacity = _ger.sel.size ? '1' : '.4'; }
  const a = document.getElementById('ger-write-all'); if (a) a.textContent = `✎ WRITE ALL ${_ger.total.toLocaleString()} MATCHING`;
  const f = document.getElementById('ger-fp'); if (f) f.textContent = _ger.sel.size ? `🎧 FINGERPRINT (${_ger.sel.size})` : `🎧 FINGERPRINT ALL ${_ger.total.toLocaleString()}`;
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
    sel.innerHTML = '<option value="">— style —</option>' + fits.map(([k, v]) => `<option value="${k}">${esc(v.label)}${v.families.length > 1 && !v.any ? ' ↔' : ''}</option>`).join('');
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
function geEditAlbum(i) { const x = _ger.rows[i]; _geOpenEditor(`ger-bedit-${i}`, `ger-bedit-cell-${i}`, x.family, x.styles, `geSaveAlbum(${i}, this.parentNode)`); }
function geSaveAlbum(i, ed) {
  const x = _ger.rows[i];
  if (!confirm(`Set this genre for every song in "${x.album}" (all pages)?\n\nOther albums by ${x.artist || 'this artist'} are not changed.`)) return;
  _geSave({..._geReadEditor(ed), album_of: x.track_id});
}
function geEditSelected() {
  if (!_ger.sel.size) return;
  const first = _ger.rows.find(r => _ger.sel.has(r.track_id)) || {};
  geRender();
  const box = document.getElementById('ger-seledit');
  box.innerHTML = `<div style="font-family:var(--mono);font-size:.65rem;color:var(--muted);margin-bottom:.35rem">Genre for the ${_ger.sel.size} selected song${_ger.sel.size > 1 ? 's' : ''} only — the artist's other songs are not changed:</div>`
    + _geEditorHTML(first.family || Object.keys(_geTax.families)[0], first.styles || [], 'geSaveSelected(this.parentNode)');
  box.style.display = '';
  geFillStyles(box.querySelector('.ger-editor'), first.styles || []);
}
function geSaveSelected(ed) { _geSave({..._geReadEditor(ed), track_ids: [..._ger.sel]}); }
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
  _ogWasRunning = true;
  if (selectedOnly) _ger.sel.clear();
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

async function geLoadCounts() {
  const el = document.getElementById('ge-counts');
  el.innerHTML = '<span style="color:var(--muted2)">Counting…</span>';
  try {
    const r = await fetch(`${API}/api/retriever/genre-engine/style-counts`, {headers: authHdrs()});
    const d = await r.json();
    if (!r.ok) throw new Error(d.detail || `HTTP ${r.status}`);
    const row = (label, n, indent) => `<tr><td style="padding:.1rem 0 .1rem ${indent ? '.8rem' : '0'};color:${indent ? (n < 75 ? 'var(--amber)' : 'var(--text)') : 'var(--teal)'}">${esc(label)}</td><td style="text-align:right;color:${!indent ? 'var(--teal)' : n < 75 ? 'var(--amber)' : 'var(--muted)'}">${(n || 0).toLocaleString()}</td></tr>`;
    let html = `<div style="font-family:var(--mono);font-size:.55rem;color:var(--muted);margin-bottom:.3rem">${d.songs.toLocaleString()} songs</div><table style="width:100%;font-family:var(--mono);font-size:.55rem;border-collapse:collapse">`;
    const fams = Object.keys(d.family_labels).sort((a, b) => (d.families[b] || 0) - (d.families[a] || 0));
    for (const f of fams) {
      html += row(d.family_labels[f], d.families[f] || 0, false);
      Object.entries(d.style_families).filter(([, fs]) => fs[0] === f)
        .map(([k]) => [k, d.styles[k] || 0]).sort((a, b) => b[1] - a[1])
        .forEach(([k, n]) => { html += row(d.labels[k] + (d.style_families[k].length > 1 ? ' ↔' : ''), n, true); });
    }
    if (d.styles.holiday) html += row('🎄 Holiday (any family)', d.styles.holiday, true);
    el.innerHTML = html + '</table>';
  } catch (e) { el.innerHTML = `<span style="color:var(--red)">${esc(e.message)}</span>`; }
}
async function geLoadUnmatched() {
  const el = document.getElementById('ge-unmatched');
  el.innerHTML = '<span style="color:var(--muted2)">Counting…</span>';
  try {
    const r = await fetch(`${API}/api/retriever/genre-engine/unmatched-tags`, {headers: authHdrs()});
    const d = await r.json();
    if (!r.ok) throw new Error(d.detail || `HTTP ${r.status}`);
    if (!d.tags.length) { el.innerHTML = '<span style="color:var(--muted2)">Nothing unmatched with 15+ songs.</span>'; return; }
    el.innerHTML = '<table style="width:100%;font-family:var(--mono);font-size:.55rem;border-collapse:collapse">'
      + d.tags.map(t => {
        const tip = `${t.artists.join(', ')} — ${Object.entries(t.sources).map(([k, v]) => `${k} ${v.toLocaleString()}`).join(' · ')}`;
        return `<tr data-tip="${esc(tip).replace(/"/g, '&quot;')}"><td style="padding:.12rem 0;color:var(--text)">${esc(t.tag)}</td><td style="text-align:right;color:var(--teal)">${t.songs.toLocaleString()}</td></tr>`;
      }).join('') + '</table>';
  } catch (e) { el.innerHTML = `<span style="color:var(--red)">${esc(e.message)}</span>`; }
}
async function geRevote(btn) {
  if (_geRunning) { toast('Wait for the Genre Engine to finish', 'info'); return; }
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
let _ogWasRunning = false;
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
    _ogRunning = running;
    if (running) {
      const p = _jobProgress('og', 'write', d.done || 0, d.total || 0);
      _jobBanner(`✎ Writing genres to files — ${(d.done||0).toLocaleString()} / ${(d.total||0).toLocaleString()}`, p.pct, p.eta, d.message);
    } else if (!_geRunning) _jobBanner('');
    if (running) _ogTimer = setTimeout(ogPoll, 2000);
    else if (_ogWasRunning && document.getElementById('ge-review')?.style.display !== 'none') geLoad(_ger.page);
    _ogWasRunning = running;
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
  return `<span class="ger-links"><a href="https://en.wikipedia.org/w/index.php?search=${q}" target="_blank" rel="noopener" class="ger-chip" data-tip="Open this artist on Wikipedia — the box at the top right lists their genres.">🌐</a>`
    + `<a href="https://www.discogs.com/search/?q=${q}&type=artist" target="_blank" rel="noopener" class="ger-chip" data-tip="Open this artist on Discogs — Discogs carries the most weight in ChartHound's genre vote.">💿</a></span>`;
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
    const altNote = d.alt_artist ? `<div style="color:var(--teal);margin-bottom:.3rem">Looked up as <b>${esc(d.alt_artist)}</b> — the tagged artist found nothing, so the file name or first listed artist was used.</div>` : '';
    cell.innerHTML = `<div class="ger-whybox">${altNote}${lines}${d.decided_by ? `<div style="color:var(--amber);margin-top:.3rem">Family decided by ${esc(d.decided_by)}; the sources above only picked the style.</div>` : ''}<div style="color:var(--muted2);margin-top:.3rem">×N = vote weight. Tags that aren't real genres ("seen live", "favorites") are ignored.</div></div>`;
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
  else if (k === 'y') { if (_geCanOk(x)) { geApprove([x.track_id], null, cur); } }
  else if (k === 'c') geCopyPath(cur, e.shiftKey);
});


// ── ✓ Looks right: keep the engine's genre as your own decision ──
function _geCanOk(x) { return ['conflict', 'thin', 'check'].includes(x.confidence); }
async function geApprove(idsOrSel, artist, keepCursor) {
  let body;
  if (artist) body = {artist};
  else if (idsOrSel === 'page') body = {track_ids: _ger.rows.filter(_geCanOk).map(r => r.track_id)};
  else if (idsOrSel === 'all') body = {filters: _gerFilters()};
  else body = {track_ids: idsOrSel === true ? [..._ger.sel] : idsOrSel};
  if (idsOrSel !== 'page' && idsOrSel !== 'all') body.write = true;
  if (body.track_ids && !body.track_ids.length) return;
  if (artist && !confirm(`Keep the current genre for every song by ${artist} (all pages) and write it to the files now?`)) return;
  if (idsOrSel === 'all' && !confirm(`Keep the current genre for ALL ${(_ger.flagged || 0).toLocaleString()} flagged songs matching these filters (every page)?\n\nThey become your decisions: they stop counting as conflicts and the engine never changes them. Back up first (Veterinarian) if you want an undo.`)) return;
  try {
    const r = await fetch(`${API}/api/retriever/genre-engine/approve`, {method: 'POST', headers: authHdrs(), body: JSON.stringify(body)});
    const d = await r.json();
    if (!r.ok) throw new Error(d.detail || `HTTP ${r.status}`);
    toast(`✓ ${d.checked.toLocaleString()} song${d.checked === 1 ? '' : 's'} marked as checked`
      + (d.written ? ` · ${d.written.toLocaleString()} written to file` : '')
      + (d.failed ? ` · ${d.failed} failed` : ''), d.failed ? 'error' : 'ok');
    if (d.written) body.track_ids?.forEach(id => _ger.sel.delete(id));
    if (typeof _thAt !== 'undefined') _thAt = 0;
    const cur = keepCursor;
    await geLoad(_ger.page);
    if (cur !== undefined && cur !== null) geCursor(Math.min(cur, _ger.rows.length - 1));
  } catch (e) { toast('Could not save: ' + e.message, 'error'); }
}

// ══ ARTIST CHECK (M10d) — fix album names, run-together and multi-artist tags ══
const _acr = {page: 1, pages: 1, total: 0, rows: [], sel: new Set(), edits: {}, poll: null};
const _AC_BADGE = {heard: ['good', '🎧 HEARD AS'], joined: ['warn', 'RUN TOGETHER'], album: ['bad', 'ALBUM AS ARTIST'], multi: ['edit', 'SEVERAL'], twin: ['fine', 'SPELLING'], intitle: ['warn', 'ARTIST IN TITLE']};
function acOpen() {
  if (document.getElementById('ret-main').classList.contains('ge-reviewing')) geCloseReview();
  document.getElementById('ret-main').classList.add('ac-reviewing');
  document.getElementById('ac-review').style.display = '';
  acLoad(1);
  acFindPoll(false);
  acFpPoll(false);
}
function acClose() {
  gePlayStop();
  document.getElementById('ret-main').classList.remove('ac-reviewing');
  document.getElementById('ac-review').style.display = 'none';
}
function _acFilters() {
  return {reason: document.getElementById('ac-reason').value, search: document.getElementById('ac-search').value.trim() || null};
}
async function acLoad(page) {
  page = Math.max(1, Math.min(page, _acr.pages || 1));
  const body = document.getElementById('ac-body');
  body.innerHTML = '<tr><td colspan="5" style="padding:1.5rem;text-align:center;color:var(--muted2)">Checking every artist tag…</td></tr>';
  try {
    const r = await fetch(`${API}/api/retriever/artist-check/list?page=${page}&size=100`, {method:'POST', headers:authHdrs(), body:JSON.stringify(_acFilters())});
    const d = await r.json();
    if (!r.ok) throw new Error(d.detail || `HTTP ${r.status}`);
    Object.assign(_acr, {page: d.page, pages: Math.max(1, Math.ceil(d.total / d.size)), total: d.total, rows: d.rows});
    _acr.sel.clear();
    const c = d.counts;
    document.getElementById('ac-count').innerHTML = `${d.total.toLocaleString()} songs`
      + ` <span style="color:var(--muted2)">· run together ${c.joined} · album as artist ${c.album} · several ${c.multi} · spelling ${c.twin} · artist in title ${c.intitle || 0} · 🎧 heard ${c.heard || 0}`
      + (c.nofix ? ` · <span style="color:var(--amber)">${c.nofix} need you</span>` : '') + '</span>';
    document.getElementById('ac-page').textContent = `page ${_acr.page} / ${_acr.pages}`;
    acRender();
  } catch (e) { body.innerHTML = `<tr><td colspan="5" style="padding:1.5rem;color:var(--red)">${esc(e.message)}</td></tr>`; }
}
function _acVal(x) { return _acr.edits[x.track_id] || {artist: x.proposed, title: x.proposed_title}; }
function acRender() {
  const body = document.getElementById('ac-body');
  if (!_acr.rows.length) { body.innerHTML = '<tr><td colspan="5" style="padding:2rem;text-align:center;color:var(--muted2)">No suspect artist tags here. 🎉</td></tr>'; acSyncSel(); return; }
  const q = v => JSON.stringify(v).replace(/"/g, '&quot;');
  let html = '', last = null;
  _acr.rows.forEach((x, i) => {
    if (x.artist !== last) {
      last = x.artist;
      const grp = _acr.rows.filter(r => r.artist === x.artist);
      html += `<tr class="ger-artist"><td><input type="checkbox" ${grp.every(r => _acr.sel.has(r.track_id)) ? 'checked' : ''} onchange="acSelectGroup(${q(x.artist)}, this.checked)"></td>
        <td colspan="4"><span class="ger-aname">${esc(x.artist)}</span><span class="ger-cnt">${grp.length}</span>
        <span class="ger-chips"><a href="#" class="ger-chip" onclick="acSetGroup(${q(x.artist)});return false" data-tip="Type one artist for every song under this tag on this page.">✎ set for all</a>${geArtistLinks(x.proposed || x.artist)}</span></td></tr>`;
    }
    const v = _acVal(x), [cls, txt] = _AC_BADGE[x.reason] || ['fine', x.reason];
    html += `<tr class="ger-song" id="ac-row-${i}"><td><input type="checkbox" ${_acr.sel.has(x.track_id) ? 'checked' : ''} onchange="acToggle(${i}, this.checked)"></td>
      <td>${gePlayBtn(x)}${esc(x.title)}<div class="ac-album">💿 ${esc(x.album)}</div></td>
      <td><input class="ac-in" value="${esc(v.artist || '').replace(/"/g, '&quot;')}" placeholder="type the artist" oninput="acEdit(${i}, 'artist', this.value)">${x.source ? `<div class="ac-album" style="color:${x.source.startsWith('🏆') ? 'var(--amber)' : x.source.startsWith('🎧 low') ? 'var(--amber)' : x.source.startsWith('🎧') ? 'var(--green)' : 'var(--teal)'}">${esc(x.source)}</div>` : ''}${x.proposed_title ? `<div class="ac-tl">title → <input class="ac-in ac-in-t" value="${esc(v.title || '').replace(/"/g, '&quot;')}" oninput="acEdit(${i}, 'title', this.value)"></div>` : ''}</td>
      <td><span class="ger-badge ${cls}" data-tip="${esc(x.why)}">${txt}</span></td>
      <td class="ger-conf"><button class="ger-ok" onclick="acWrite([${i}])" data-tip="Write this artist${x.proposed_title ? ' and title' : ''} into the file now.">✓</button><button class="ger-redo" onclick="acIgnore([${x.track_id}])" data-tip="Keep the tag as it is — this song is never flagged again.">✕</button></td></tr>`;
  });
  body.innerHTML = html;
  acSyncSel();
}
function acEdit(i, field, val) { const x = _acr.rows[i]; _acr.edits[x.track_id] = {..._acVal(x), [field]: val}; }
function acToggle(i, on) { const id = _acr.rows[i].track_id; on ? _acr.sel.add(id) : _acr.sel.delete(id); acSyncSel(); }
function acSelectAll(on) { _acr.rows.forEach(r => on ? _acr.sel.add(r.track_id) : _acr.sel.delete(r.track_id)); acRender(); }
function acSelectGroup(a, on) { _acr.rows.filter(r => r.artist === a).forEach(r => on ? _acr.sel.add(r.track_id) : _acr.sel.delete(r.track_id)); acRender(); }
function acSetGroup(a) {
  const first = _acr.rows.find(r => r.artist === a);
  const v = prompt(`Artist for every song tagged "${a}" on this page:`, first ? (_acVal(first).artist || '') : '');
  if (v === null) return;
  _acr.rows.filter(r => r.artist === a).forEach(r => { _acr.edits[r.track_id] = {..._acVal(r), artist: v.trim()}; _acr.sel.add(r.track_id); });
  acRender();
}
function acSyncSel() {
  const n = _acr.sel.size;
  const hd = document.getElementById('ac-all'); if (hd) hd.checked = _acr.rows.length > 0 && _acr.rows.every(r => _acr.sel.has(r.track_id));
  const w = document.getElementById('ac-write-sel'); w.textContent = `✓ WRITE SELECTED (${n})`; w.disabled = !n; w.style.opacity = n ? '1' : '.4';
  const k = document.getElementById('ac-keep-sel'); k.textContent = `✕ KEEP AS IS (${n})`; k.disabled = !n; k.style.opacity = n ? '1' : '.4';
  const fb = document.getElementById('ac-fp'); if (fb) fb.textContent = n ? `🎧 FINGERPRINT (${n})` : '🎧 FINGERPRINT';
  document.getElementById('ac-write-all').textContent = `✓ WRITE ALL ${_acr.total.toLocaleString()} MATCHING`;
}
async function acWrite(which) {
  let body, n;
  if (which === 'all') {
    n = _acr.total;
    if (!n || !confirm(`Write the suggested artist for every matching song (all pages)?\n\nSongs with no suggestion and 🎧 low guesses are skipped (✓ those one by one). Only the ARTIST tag changes (and TITLE for run-together tags).\nTip: back up first on The Veterinarian.`)) return;
    body = {filters: _acFilters()};
  } else {
    const rows = which === 'sel' ? _acr.rows.filter(r => _acr.sel.has(r.track_id)) : which.map(i => _acr.rows[i]);
    const items = rows.map(r => ({track_id: r.track_id, artist: (_acVal(r).artist || '').trim(), title: r.proposed_title ? ((_acVal(r).title || '').trim() || null) : null}))
                      .filter(it => it.artist);
    if (!items.length) { toast('Type an artist first.', 'error'); return; }
    if (items.length < rows.length) toast(`${rows.length - items.length} song(s) skipped — no artist typed.`, 'info');
    n = items.length;
    if (n > 1 && !confirm(`Write the artist into ${n} music files?`)) return;
    body = {items};
  }
  try {
    const r = await fetch(`${API}/api/retriever/artist-check/write`, {method:'POST', headers:authHdrs(), body:JSON.stringify(body)});
    const d = await r.json();
    if (!r.ok) throw new Error(d.detail || `HTTP ${r.status}`);
    toast(`Writing ${d.count.toLocaleString()} file${d.count > 1 ? 's' : ''}…`, 'info');
    acPoll();
  } catch (e) { toast(e.message, 'error'); }
}
// ── 🔎 FIND ARTISTS ──
let _afTimer = null;
async function acFind() {
  if (!confirm('Look up every song with no suggestion by its title on Last.fm?\n\nRuns in the background (about 4 lookups a second). Results only fill the boxes — nothing is written until you press ✓.')) return;
  const r = await fetch(`${API}/api/retriever/artist-check/find`, {method: 'POST', headers: authHdrs()});
  if (!r.ok) { const d = await r.json().catch(() => ({})); toast(d.detail || `HTTP ${r.status}`, 'error'); return; }
  toast('Finding artists — progress shows above the list', 'ok');
  acFindPoll(true);
}
async function acFindStop() {
  await fetch(`${API}/api/retriever/artist-check/find-stop`, {method: 'POST', headers: authHdrs()});
  toast('Stopping — found artists are kept', 'ok');
}
async function acFindPoll(started) {
  clearTimeout(_afTimer);
  try {
    const d = await (await fetch(`${API}/api/retriever/artist-check/find-status`, {headers: authHdrs()})).json();
    const running = d.status === 'running', el = document.getElementById('ac-status');
    document.getElementById('ac-find-stop').style.display = running ? '' : 'none';
    const fb = document.getElementById('ac-find'); fb.disabled = running; fb.style.opacity = running ? '.4' : '1';
    if (running) {
      const p = _jobProgress('af', 'find', d.done || 0, d.total || 0);
      el.innerHTML = `<div style="border:1px solid var(--teal);border-radius:8px;padding:.5rem .7rem">
        <div style="display:flex;justify-content:space-between;gap:1rem"><span>🔎 Finding artists — ${(d.done||0).toLocaleString()} / ${(d.total||0).toLocaleString()} · ${p.pct}% · ${(d.found||0).toLocaleString()} found</span><span style="color:var(--muted)">${p.eta}</span></div>
        <div style="background:var(--bg);border-radius:4px;height:10px;overflow:hidden;border:1px solid var(--border);margin:.3rem 0"><div style="height:100%;width:${p.pct}%;background:var(--teal);transition:width .4s"></div></div>
        <div style="color:var(--muted2);white-space:nowrap;overflow:hidden;text-overflow:ellipsis">${esc(d.message || '')}</div></div>`;
      _afTimer = setTimeout(() => acFindPoll(true), 2000);
    } else if (started && d.status !== 'idle') {
      el.textContent = d.message || '';
      toast(d.message || 'Done', d.status === 'failed' ? 'error' : 'success');
      acLoad(1);
    }
  } catch { _afTimer = setTimeout(() => acFindPoll(started), 4000); }
}
// ── 🎧 FINGERPRINT (AcoustID) ──
let _fpTimer = null;
async function acFp() {
  const ids = [..._acr.sel];
  if (ids.length ? !confirm(`Fingerprint the ${ids.length} ticked song${ids.length > 1 ? 's' : ''}?\n\nChecked again even if done before. Low scores are shown as a labelled guess. Nothing is written until you press ✓.`)
                 : !confirm('Fingerprint every album-as-artist song and every song with no suggestion?\n\nRuns in the background (about 1 song a second). Already-checked files are skipped. Tick rows first to fingerprint only those. Nothing is written until you press ✓.')) return;
  const r = await fetch(`${API}/api/retriever/artist-check/fp`, {method: 'POST', headers: authHdrs(), body: JSON.stringify({track_ids: ids})});
  if (!r.ok) { const d = await r.json().catch(() => ({})); toast(d.detail || `HTTP ${r.status}`, 'error'); return; }
  toast('Fingerprinting — progress shows above the list', 'ok');
  acFpPoll(true);
}
async function acFpStop() {
  await fetch(`${API}/api/retriever/artist-check/fp-stop`, {method: 'POST', headers: authHdrs()});
  toast('Stopping — identified songs are kept', 'ok');
}
async function acFpPoll(started) {
  clearTimeout(_fpTimer);
  try {
    const d = await (await fetch(`${API}/api/retriever/artist-check/fp-status`, {headers: authHdrs()})).json();
    const running = d.status === 'running', el = document.getElementById('ac-status');
    document.getElementById('ac-fp-stop').style.display = running ? '' : 'none';
    const fb = document.getElementById('ac-fp'); fb.disabled = running; fb.style.opacity = running ? '.4' : '1';
    if (running) {
      const p = _jobProgress('fp', 'fp', d.done || 0, d.total || 0);
      el.innerHTML = `<div style="border:1px solid var(--green);border-radius:8px;padding:.5rem .7rem">
        <div style="display:flex;justify-content:space-between;gap:1rem"><span>🎧 Fingerprinting — ${(d.done||0).toLocaleString()} / ${(d.total||0).toLocaleString()} · ${p.pct}% · ${(d.found||0).toLocaleString()} identified</span><span style="color:var(--muted)">${p.eta}</span></div>
        <div style="background:var(--bg);border-radius:4px;height:10px;overflow:hidden;border:1px solid var(--border);margin:.3rem 0"><div style="height:100%;width:${p.pct}%;background:var(--green);transition:width .4s"></div></div>
        <div style="color:var(--muted2);white-space:nowrap;overflow:hidden;text-overflow:ellipsis">${esc(d.message || '')}</div></div>`;
      _fpTimer = setTimeout(() => acFpPoll(true), 2000);
    } else if (started && d.status !== 'idle') {
      el.textContent = d.message || '';
      toast(d.message || 'Done', d.status === 'failed' ? 'error' : 'success');
      acLoad(1);
    }
  } catch { _fpTimer = setTimeout(() => acFpPoll(started), 4000); }
}
function acPoll() {
  clearTimeout(_acr.poll);
  _acr.poll = setTimeout(async () => {
    try {
      const d = await (await fetch(`${API}/api/retriever/artist-check/status`, {headers:authHdrs()})).json();
      const el = document.getElementById('ac-status');
      el.textContent = d.status === 'running' ? `Writing ${d.done.toLocaleString()} / ${d.total.toLocaleString()} — ${d.message}` : (d.message || '');
      if (d.status === 'running') return acPoll();
      toast(d.message, d.status === 'done' ? 'success' : 'error');
      _acr.edits = {};
      acLoad(_acr.page);
    } catch (e) { acPoll(); }
  }, 1200);
}
async function acIgnore(ids) {
  if (ids === 'sel') ids = [..._acr.sel];
  if (!ids.length) return;
  const r = await fetch(`${API}/api/retriever/artist-check/ignore`, {method:'POST', headers:authHdrs(), body:JSON.stringify({track_ids: ids})});
  if (!r.ok) { toast(`HTTP ${r.status}`, 'error'); return; }
  toast(`Kept ${ids.length} as is — won't be flagged again.`, 'success');
  acLoad(_acr.page);
}

// ── M12a: 🎧 FINGERPRINT in Genre Review ──
async function geFp() {
  if (_geRunning) { toast('Wait for the Genre Engine to finish', 'info'); return; }
  const ids = [..._ger.sel];
  const n = ids.length || _ger.total;
  if (!n) return;
  if (!confirm(`Fingerprint ${ids.length ? 'the ' + n + ' ticked' : 'all ' + n.toLocaleString() + ' matching'} song${n > 1 ? 's' : ''}?\n\nListens to each file, asks AcoustID which recording it is, then gets that recording's genres from MusicBrainz and re-votes (about 1–4 s a song; already-fingerprinted files are skipped).\nNothing is written to files. A different artist is flagged in 🎤 ARTIST CHECK.`)) return;
  const r = await fetch(`${API}/api/retriever/genre-engine/fp`, {method: 'POST', headers: authHdrs(), body: JSON.stringify({..._gerFilters(), track_ids: ids.length ? ids : null})});
  const d = await r.json().catch(() => ({}));
  if (!r.ok) { toast(d.detail || `HTTP ${r.status}`, 'error'); return; }
  toast(`Fingerprinting ${d.count.toLocaleString()} songs — progress shows above the list`, 'ok');
  setTimeout(gePoll, 800);
}
