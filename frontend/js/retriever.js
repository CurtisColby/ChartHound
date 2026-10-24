'use strict';
// ══════════════════════════════════════════════════════════════════════════════
//  GENRE SCOUT
// ══════════════════════════════════════════════════════════════════════════════
let _scoutJobId      = null;
let _scoutPolling    = null;
let _scoutViewMode   = 'flat';
let _scoutAllResults = [];
const _scoutFlatSel      = new Set();
const _scoutFlatAssigned = {};

function _scoutGenreOpts(selected) {
  const allG = [...new Set([...GENRE_LIST.filter(g=>g!=='Custom…'),..._customGenres])].sort((a,b)=>a.localeCompare(b));
  return '<option value="">— select genre —</option>' + allG.map(g=>`<option value="${esc(g)}"${g===selected?' selected':''}>${esc(g)}</option>`).join('');
}
function _scoutMoodOpts(selected) {
  const allM = [...new Set([...MOOD_LIST.filter(m=>m!=='Custom…'),..._customMoods])].sort((a,b)=>a.localeCompare(b));
  return '<option value="">— optional —</option>' + allM.map(m=>`<option value="${esc(m)}"${m===selected?' selected':''}>${esc(m)}</option>`).join('');
}
function _scoutPopulateGenreOpts() {
  const opts = _scoutGenreOpts('');
  const s = document.getElementById('scoutFlatQuickGenre');
  if (s) s.innerHTML = opts;
}

async function scoutStart() {
  _scoutJobId = null; _scoutAllResults = []; _scoutPollBusy = false;
  _scoutFlatSel.clear();
  Object.keys(_scoutFlatAssigned).forEach(k=>delete _scoutFlatAssigned[k]);
  ['scoutFlatBody','scoutCards'].forEach(id=>{const el=document.getElementById(id);if(el)el.innerHTML='';});
  document.getElementById('scoutFlatWrap').style.display='none';
  document.getElementById('scoutCardWrap').style.display='none';
  document.getElementById('scoutEmpty').style.display='none';
  document.getElementById('scoutProgressWrap').style.display='block';
  document.getElementById('scoutProgressBar').style.width='0%';
  document.getElementById('scoutProgressPct').textContent='0%';
  document.getElementById('scoutProgressLabel').textContent='Indexing files…';
  document.getElementById('scoutStatFiles').textContent='0 / 0 files read';
  document.getElementById('scoutStatFound').textContent='0 missing genre';
  document.getElementById('scoutCount').textContent='—';
  document.getElementById('btnScoutNextChunk').style.display='none';
  document.getElementById('btnScoutScanAll').style.display='none';
  document.getElementById('btnScoutStop').style.display='inline-flex';
  document.getElementById('btnScoutScan').disabled=true;
  _scoutPopulateGenreOpts();

  const subfolder=(()=>{const sel=document.getElementById('subfolderPath');if(currentScope==='subfolder'&&sel&&sel.value&&sel.value!=='__up__')return sel.value;return '';})();
  try {
    const body={scope:subfolder?'subfolder':'library',subfolder:subfolder||undefined,chunk_size:500};
    const r=await fetch(`${API}/api/retriever/scout/start`,{method:'POST',headers:authHdrs(),body:JSON.stringify(body)});
    const d=await r.json();
    if(!r.ok) throw new Error(d.detail||'Failed to start scout');
    if(d.total===0){addLog('🔍 Genre Scout: No audio files found.','warn');_scoutResetButtons();return;}
    _scoutJobId=d.job_id;
    document.getElementById('scoutProgressLabel').textContent=`Scanning ${d.total.toLocaleString()} files…`;
    addLog(`🔍 Genre Scout started — ${d.total.toLocaleString()} files to scan`,'info');
    _scoutStartPolling();
  } catch(e){addLog(`✗ Genre Scout: ${e.message}`,'err');_scoutResetButtons();}
}

function _scoutStartPolling(){if(_scoutPolling)clearInterval(_scoutPolling);_scoutPolling=setInterval(_scoutPoll,800);}

let _scoutPollBusy = false;
async function _scoutPoll(){
  if(!_scoutJobId||_scoutPollBusy)return;
  try{
    const r=await fetch(`${API}/api/retriever/scout/status/${_scoutJobId}`,{headers:authHdrs()});
    if(!r.ok)return;
    const d=await r.json();
    const pct=d.total>0?Math.round((d.processed/d.total)*100):0;
    document.getElementById('scoutProgressBar').style.width=pct+'%';
    document.getElementById('scoutProgressPct').textContent=pct+'%';
    document.getElementById('scoutStatFiles').textContent=`${d.processed.toLocaleString()} / ${d.total.toLocaleString()} files read`;
    document.getElementById('scoutStatFound').textContent=`${d.found} missing genre`;
    if(d.status==='paused'||d.status==='done'){
      _scoutPollBusy=true;
      clearInterval(_scoutPolling);_scoutPolling=null;
      const known=new Set(_scoutAllResults.map(t=>t.file_path));
      _scoutAllResults=_scoutAllResults.concat(d.results.filter(t=>!known.has(t.file_path)));
      document.getElementById('scoutCount').textContent=`${_scoutAllResults.length} track(s) with no genre`;
      _scoutRender();
      if(d.status==='paused'){
        const rem=d.total-d.processed;
        document.getElementById('scoutProgressLabel').textContent=`Chunk done — ${rem.toLocaleString()} files remaining`;
        document.getElementById('btnScoutNextChunk').style.display='inline-flex';
        document.getElementById('btnScoutScanAll').style.display='inline-flex';
        addLog(`🔍 Chunk done — ${d.found} missing genres found, ${rem.toLocaleString()} files left`,'ok');
      } else {
        document.getElementById('scoutProgressLabel').textContent=`Scan complete — ${d.found} files with no genre`;
        document.getElementById('btnScoutNextChunk').style.display='none';
        document.getElementById('btnScoutScanAll').style.display='none';
        if(d.found===0)document.getElementById('scoutEmpty').style.display='block';
        addLog(`🔍 Genre Scout complete — ${d.found} / ${d.total.toLocaleString()} files had no genre`,'ok');
      }
      document.getElementById('btnScoutStop').style.display='none';
      document.getElementById('btnScoutScan').disabled=false;
    }
  }catch(e){}
}

async function scoutNextChunk(){
  if(!_scoutJobId)return;
  _scoutPollBusy=false;
  document.getElementById('btnScoutNextChunk').style.display='none';
  document.getElementById('btnScoutScanAll').style.display='none';
  document.getElementById('btnScoutStop').style.display='inline-flex';
  document.getElementById('scoutProgressLabel').textContent='Scanning next chunk…';
  const r=await fetch(`${API}/api/retriever/scout/next/${_scoutJobId}`,{method:'POST',headers:authHdrs()});
  if(r.ok)_scoutStartPolling();
}

async function scoutScanAll(){
  if(!_scoutJobId)return;
  _scoutPollBusy=false;
  addLog('⚡ Genre Scout — scanning all remaining files…','info');
  document.getElementById('btnScoutNextChunk').style.display='none';
  document.getElementById('btnScoutScanAll').style.display='none';
  document.getElementById('btnScoutStop').style.display='inline-flex';

  // With the new backend (scans until 500 found), "paused" = found a batch for review.
  // So scoutScanAll just kicks off one chunk and lets normal polling handle the rest.
  // It does NOT loop — the user decides whether to continue after reviewing results.
  try {
    const r=await fetch(`${API}/api/retriever/scout/next/${_scoutJobId}`,{method:'POST',headers:authHdrs()});
    if(!r.ok){ _scoutResetButtons(); return; }
    _scoutStartPolling();
  } catch(e){ addLog(`Scout error: ${e.message}`,'err'); _scoutResetButtons(); }
}

async function scoutStop(){
  if(_scoutJobId)await fetch(`${API}/api/retriever/scout/stop`,{method:'POST',headers:authHdrs()});
  if(_scoutPolling){clearInterval(_scoutPolling);_scoutPolling=null;}
  _scoutResetButtons();
  addLog('🔍 Genre Scout stopped.','warn');
}
function _scoutResetButtons(){
  document.getElementById('btnScoutScan').disabled=false;
  document.getElementById('btnScoutStop').style.display='none';
  document.getElementById('btnScoutNextChunk').style.display='none';
  document.getElementById('btnScoutScanAll').style.display='none';
}

function setScoutGroup(mode,el){
  _scoutViewMode=mode;
  document.querySelectorAll('.scout-group-btn').forEach(b=>b.classList.remove('active'));
  el.classList.add('active');
  _scoutRender();
}

function _scoutRender(){
  if(_scoutViewMode==='flat'){
    document.getElementById('scoutFlatWrap').style.display=_scoutAllResults.length?'block':'none';
    document.getElementById('scoutCardWrap').style.display='none';
    _scoutRenderFlat();
  } else {
    document.getElementById('scoutFlatWrap').style.display='none';
    document.getElementById('scoutCardWrap').style.display=_scoutAllResults.length?'block':'none';
    _scoutRenderCards();
  }
}

// ── FLAT VIEW ─────────────────────────────────────────────────────────────────
function _scoutRenderFlat(){
  const tbody=document.getElementById('scoutFlatBody');
  tbody.innerHTML='';
  _scoutFlatSel.clear();
  document.getElementById('scoutFlatSelectedCount').textContent='0 selected';
  _scoutAllResults.forEach(t=>{
    const assigned=_scoutFlatAssigned[t.file_path]||'';
    const tr=document.createElement('tr');
    tr.dataset.fp=t.file_path;
    tr.dataset.text=`${(t.tag_artist||'').toLowerCase()} ${(t.title||'').toLowerCase()} ${(t.tag_album||'').toLowerCase()}`;
    tr.innerHTML=`
      <td><input type="checkbox" onchange="_scoutFlatCheck('${escJs(t.file_path)}',this.checked)" style="accent-color:var(--teal)"></td>
      <td style="color:var(--muted)">${esc(t.tag_artist||'')}</td>
      <td style="color:var(--muted)">${esc(t.title||'')}</td>
      <td style="color:var(--muted2);font-size:.58rem">${esc(t.tag_album||'—')}</td>
      <td><select class="scout-row-genre" data-fp="${esc(t.file_path)}" onchange="_scoutFlatAssign('${escJs(t.file_path)}',this.value)"
        style="background:var(--surface);color:var(--text);border:1px solid var(--border);border-radius:4px;padding:.2rem .4rem;font-family:var(--mono);font-size:.55rem;width:100%">
        ${_scoutGenreOpts(assigned)}</select></td>`;
    tbody.appendChild(tr);
  });
}
function scoutFilterRows(q){const lq=q.toLowerCase();document.querySelectorAll('#scoutFlatBody tr').forEach(tr=>{tr.style.display=(tr.dataset.text||'').includes(lq)?'':'none';});}
function _scoutFlatAssign(fp,genre){_scoutFlatAssigned[fp]=genre;}
function _scoutFlatCheck(fp,checked){if(checked)_scoutFlatSel.add(fp);else _scoutFlatSel.delete(fp);document.getElementById('scoutFlatSelectedCount').textContent=`${_scoutFlatSel.size} selected`;}
function scoutFlatSelectAll(checked){document.querySelectorAll('#scoutFlatBody tr[data-fp]').forEach(tr=>{if(tr.style.display==='none')return;const cb=tr.querySelector('input[type="checkbox"]');if(cb){cb.checked=checked;_scoutFlatCheck(tr.dataset.fp,checked);}});}
function scoutFlatApplyGenre(){
  const genre=document.getElementById('scoutFlatQuickGenre').value;
  if(!genre){toast('Select a genre first.','warn');return;}
  let count=0;
  document.querySelectorAll('#scoutFlatBody tr[data-fp]').forEach(tr=>{
    const cb=tr.querySelector('input[type="checkbox"]');
    if(cb&&cb.checked){_scoutFlatAssigned[tr.dataset.fp]=genre;const sel=tr.querySelector('.scout-row-genre');if(sel)sel.value=genre;count++;}
  });
  toast(`Genre "${genre}" applied to ${count} track(s).`,'ok');
}
async function scoutFlatWriteSelected(){
  if(!_scoutFlatSel.size){toast('Select tracks and assign genres first.','warn');return;}
  for(const fp of _scoutFlatSel){if(!_scoutFlatAssigned[fp]){toast('Some selected tracks have no genre assigned.','warn');return;}}
  let ok=0,fail=0;
  for(const fp of _scoutFlatSel){
    try{const r=await fetch(`${API}/api/retriever/override`,{method:'POST',headers:authHdrs(),body:JSON.stringify({track_ids:[],file_paths:[fp],genres:[_scoutFlatAssigned[fp]],moods:[]})});const d=await r.json();ok+=d.success||0;fail+=d.failed||0;}catch{fail++;}
  }
  toast(`Genre Scout: ${ok} written, ${fail} failed.`,ok>0?'ok':'error');
  addLog(`🔍 Genre Scout wrote ${ok} tracks, ${fail} failed.`,ok>0?'ok':'err');
  if(ok>0){
    const written=new Set([..._scoutFlatSel].filter(fp=>_scoutFlatAssigned[fp]));
    _scoutAllResults=_scoutAllResults.filter(t=>!written.has(t.file_path));
    written.forEach(fp=>delete _scoutFlatAssigned[fp]);
    _scoutFlatSel.clear();
    _scoutRenderFlat();
    document.getElementById('scoutCount').textContent=`${_scoutAllResults.length} track(s) with no genre`;
    document.getElementById('scoutStatFound').textContent=`${_scoutAllResults.length} missing genre`;
  }
}

// ── CARD VIEW ─────────────────────────────────────────────────────────────────
function _scoutRenderCards(){
  const container=document.getElementById('scoutCards');
  const groupKey=_scoutViewMode==='artist'?'tag_artist':_scoutViewMode==='folder'?null:'tag_album';
  const groups={};
  _scoutAllResults.forEach(t=>{
    let gk;
    if(_scoutViewMode==='folder'){
      const parts=t.file_path.split('/');parts.pop();
      gk=parts.join('/')||'(Unknown)';
    } else {
      gk=(t[groupKey]||'(Unknown)').trim();
    }
    if(!groups[gk])groups[gk]=[];groups[gk].push(t);
  });
  document.querySelectorAll('#scoutCards .scout-card').forEach(card=>{if(!groups[card.dataset.group])card.remove();});
  Object.keys(groups).sort().forEach(groupName=>{
    const safeId='sc-'+groupName.replace(/[^a-z0-9]/gi,'_');
    if(document.getElementById(safeId))return;
    container.appendChild(_scoutBuildCard(groupName,groups[groupName],safeId));
  });
}

function _scoutBuildCard(groupName,tracks,safeId){
  const icon=_scoutViewMode==='artist'?'🎤':_scoutViewMode==='folder'?'📁':'💿';
  const repArtist = tracks[0]?.tag_artist || groupName;

  // Derive common parent folder path from track file paths
  // Useful when groupName is '(Unknown)' — shows the actual folder on disk
  const folderPath = (() => {
    if (!tracks.length) return '';
    const parts = tracks[0].file_path.split('/');
    parts.pop(); // remove filename
    const folder = parts.join('/');
    // Check if all tracks share the same folder
    const allSame = tracks.every(t => t.file_path.startsWith(folder + '/'));
    if (allSame) return folder;
    // Multiple folders — show common prefix
    const commonParts = parts.slice();
    tracks.forEach(t => {
      const tp = t.file_path.split('/');
      tp.pop();
      while (commonParts.length > 0 && commonParts.join('/') !== tp.slice(0, commonParts.length).join('/')) {
        commonParts.pop();
      }
    });
    return commonParts.length > 2 ? commonParts.join('/') : '';
  })();
  const folderLabel = folderPath && _scoutViewMode !== 'folder'
    ? `<div style="font-family:var(--mono);font-size:.50rem;color:var(--text);margin-top:.15rem;word-break:break-all;user-select:all;cursor:text;opacity:.85" title="Click to select path for copying">📁 ${esc(atTranslatePath(folderPath))}</div>`
    : '';

  const trackRows=tracks.map((t,i)=>
    `<div style="display:flex;align-items:center;gap:.4rem;padding:.2rem 0;border-bottom:1px solid var(--border)">
      <input type="checkbox" id="${safeId}-tr-${i}" checked style="accent-color:var(--teal)">
      <span style="font-family:var(--mono);font-size:.5rem;flex:1;color:var(--text);overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${esc(t.file_path)}">${esc(t.title||t.file_path.split('/').pop())}</span>
      <span style="font-family:var(--mono);font-size:.46rem;color:var(--muted2);flex-shrink:0">${esc(t.tag_artist||'')}</span>
    </div>`
  ).join('');

  const genreDrops=[1,2,3].map(n=>`
    <select id="${safeId}-g${n}" style="flex:1;background:var(--surface2);color:var(--text);border:1px solid var(--border);border-radius:4px;padding:.25rem;font-family:var(--mono);font-size:.5rem">
      ${_scoutGenreOpts('')}
    </select>`).join('');
  const moodDrops=[1,2,3].map(n=>`
    <select id="${safeId}-m${n}" style="flex:1;background:var(--surface2);color:var(--text);border:1px solid var(--border);border-radius:4px;padding:.25rem;font-family:var(--mono);font-size:.5rem">
      ${_scoutMoodOpts('')}
    </select>`).join('');

  const card=document.createElement('div');
  card.className='scout-card';
  card.id=safeId;
  card.dataset.group=groupName;
  card.innerHTML=`
    <div style="display:flex;align-items:flex-start;gap:.75rem;flex-wrap:wrap">
      <div style="flex:1;min-width:200px">
        <div style="font-family:var(--mono);font-size:.65rem;color:var(--text);font-weight:bold">
          ${icon} ${esc(groupName)}
          <span style="display:inline-block;background:var(--surface);border:1px solid var(--border);color:var(--muted);font-size:.48rem;padding:.1rem .35rem;border-radius:10px;margin-left:.35rem">${tracks.length} track${tracks.length!==1?'s':''}</span>
        </div>
        ${folderLabel}
        <!-- Suggest chips row — hidden until suggest is clicked -->
        <div id="${safeId}-chips" style="display:none;margin-top:.4rem">
          <span style="font-family:var(--mono);font-size:.46rem;color:var(--muted2);letter-spacing:1px">SUGGESTED ▶ </span>
          <span id="${safeId}-chips-inner"></span>
        </div>
      </div>
      <div style="display:flex;gap:.35rem;flex-wrap:wrap;align-items:center">
        <button class="btn btn-outline btn-sm" id="${safeId}-suggest-btn" onclick="_scoutSuggest('${escJs(groupName)}','${escJs(repArtist)}','${safeId}')" title="Get genre suggestions from DB or Last.fm">💡 SUGGEST</button>
        <button class="btn btn-teal btn-sm" onclick="_scoutCardWrite('${escJs(groupName)}','${safeId}')">✓ WRITE &amp; DISMISS</button>
        <button class="btn btn-outline btn-sm" onclick="_scoutCardKeep('${safeId}')">✓ KEEP (skip)</button>
      </div>
    </div>

    <div style="margin-top:.6rem;padding:.6rem;background:var(--surface);border-radius:6px;border:1px solid var(--border)">
      <div style="font-family:var(--mono);font-size:.48rem;color:var(--muted2);letter-spacing:1px;margin-bottom:.25rem">GENRES (up to 3)</div>
      <div style="display:flex;gap:.3rem;margin-bottom:.4rem">${genreDrops}</div>
      <div style="font-family:var(--mono);font-size:.48rem;color:var(--muted2);letter-spacing:1px;margin-bottom:.25rem">MOODS (up to 3, optional)</div>
      <div style="display:flex;gap:.3rem">${moodDrops}</div>
    </div>

    <div style="margin-top:.5rem">
      <div style="font-family:var(--mono);font-size:.48rem;color:var(--muted2);letter-spacing:1px;margin-bottom:.2rem">
        TRACKS — uncheck any to exclude
        <button onclick="_scoutCardSelAll('${safeId}',${tracks.length},true)" style="background:none;border:none;color:var(--teal);font-family:var(--mono);font-size:.46rem;cursor:pointer">all</button>
        <button onclick="_scoutCardSelAll('${safeId}',${tracks.length},false)" style="background:none;border:none;color:var(--muted);font-family:var(--mono);font-size:.46rem;cursor:pointer">none</button>
      </div>
      <div style="max-height:140px;overflow-y:auto;background:var(--surface2);border-radius:4px;border:1px solid var(--border);padding:.25rem .4rem">
        ${trackRows}
      </div>
    </div>`;
  return card;
}

// ── Suggest button handler ────────────────────────────────────────────────────
async function _scoutSuggest(groupName, artist, safeId) {
  const btn = document.getElementById(`${safeId}-suggest-btn`);
  if (btn) { btn.disabled=true; btn.textContent='⟳ …'; }
  try {
    const r = await fetch(`${API}/api/retriever/scout/suggest?artist=${encodeURIComponent(artist)}`, {headers:authHdrs()});
    const d = await r.json();
    const chipsInner = document.getElementById(`${safeId}-chips-inner`);
    const chipsWrap  = document.getElementById(`${safeId}-chips`);
    if (!chipsInner || !chipsWrap) return;

    if (!d.suggestions || !d.suggestions.length) {
      chipsInner.innerHTML = '<span style="font-family:var(--mono);font-size:.5rem;color:var(--muted2)">No suggestions found</span>';
      chipsWrap.style.display = 'block';
      if (btn) { btn.disabled=false; btn.textContent='💡 SUGGEST'; }
      return;
    }

    // Build clickable chips
    chipsInner.innerHTML = d.suggestions.map(g =>
      `<span class="scout-chip" onclick="_scoutChipClick('${safeId}','${escJs(g)}')" title="Click to fill next empty genre slot">${esc(g)}</span>`
    ).join('');

    // Source badge
    const src = d.source === 'db'
      ? '<span style="font-family:var(--mono);font-size:.44rem;color:var(--green);margin-left:.4rem">● from library</span>'
      : '<span style="font-family:var(--mono);font-size:.44rem;color:var(--gold);margin-left:.4rem">● Last.fm</span>';
    chipsInner.insertAdjacentHTML('beforeend', src);

    chipsWrap.style.display = 'block';
    if (btn) { btn.disabled=false; btn.textContent='💡 SUGGEST'; }
    addLog(`💡 Suggestions for "${artist}": ${d.suggestions.join(', ')} (${d.source})`,'info');
  } catch(e) {
    if (btn) { btn.disabled=false; btn.textContent='💡 SUGGEST'; }
    addLog(`✗ Suggest failed: ${e.message}`,'err');
  }
}

// Chip click — fills next empty genre slot, auto-saves to custom genres if not in list
function _scoutChipClick(safeId, genre) {
  // Fill first empty genre dropdown
  for (let n=1; n<=3; n++) {
    const sel = document.getElementById(`${safeId}-g${n}`);
    if (sel && !sel.value) {
      // If genre not in GENRE_LIST or _customGenres, save it
      if (!GENRE_LIST.includes(genre) && !_customGenres.includes(genre)) {
        saveCustomTag('genre', genre);
        // Add to dropdown so it appears selected
        const opt = document.createElement('option');
        opt.value = genre; opt.textContent = genre;
        sel.appendChild(opt);
      }
      sel.value = genre;
      // Visual feedback — briefly highlight chip as used
      document.querySelectorAll(`#${safeId}-chips-inner .scout-chip`).forEach(c=>{
        if(c.textContent===genre){c.style.background='rgba(45,212,191,.35)';c.style.borderColor='var(--teal)';}
      });
      return;
    }
  }
  toast('All 3 genre slots are filled. Clear one first.','warn');
}

function _scoutCardSelAll(safeId,count,checked){for(let i=0;i<count;i++){const cb=document.getElementById(`${safeId}-tr-${i}`);if(cb)cb.checked=checked;}}

function _scoutCardKeep(safeId){
  const card=document.getElementById(safeId);
  if(!card)return;
  const groupName=card.dataset.group;
  _scoutAllResults=_scoutAllResults.filter(t=>{
    let gk;
    if(_scoutViewMode==='folder'){const parts=t.file_path.split('/');parts.pop();gk=parts.join('/')||'(Unknown)';}
    else{gk=(_scoutViewMode==='artist'?t.tag_artist:t.tag_album)||'(Unknown)';}
    return gk.trim()!==groupName;
  });
  card.style.opacity='0';setTimeout(()=>card.remove(),250);
  document.getElementById('scoutCount').textContent=`${_scoutAllResults.length} track(s) with no genre`;
  document.getElementById('scoutStatFound').textContent=`${_scoutAllResults.length} missing genre`;
}

async function _scoutCardWrite(groupName,safeId){
  const card=document.getElementById(safeId);
  if(!card)return;
  const genres=[1,2,3].map(n=>document.getElementById(`${safeId}-g${n}`)?.value||'').filter(Boolean);
  const moods =[1,2,3].map(n=>document.getElementById(`${safeId}-m${n}`)?.value||'').filter(Boolean);
  if(!genres.length){toast('Select at least one genre before writing.','warn');return;}
  const groupKey=_scoutViewMode==='artist'?'tag_artist':_scoutViewMode==='folder'?null:'tag_album';
  const groupTracks=_scoutAllResults.filter(t=>{
    if(_scoutViewMode==='folder'){const parts=t.file_path.split('/');parts.pop();return (parts.join('/')||'(Unknown)')===groupName;}
    return (t[groupKey]||'(Unknown)').trim()===groupName;
  });
  const selectedPaths=[];
  groupTracks.forEach((t,i)=>{const cb=document.getElementById(`${safeId}-tr-${i}`);if(!cb||cb.checked)selectedPaths.push(t.file_path);});
  if(!selectedPaths.length){toast('No tracks selected.','warn');return;}
  addLog(`🔍 Scout writing ${selectedPaths.length} tracks for "${groupName}"…`,'info');
  try{
    const r=await fetch(`${API}/api/retriever/override`,{method:'POST',headers:authHdrs(),body:JSON.stringify({track_ids:[],file_paths:selectedPaths,genres,moods})});
    const d=await r.json();
    if(d.success>0){
      toast(`✓ ${d.success} track${d.success!==1?'s':''} written`,'ok');
      addLog(`🔍 Scout: ${d.success} tracks written for "${groupName}"`,'ok');
      const writtenSet=new Set(selectedPaths);
      _scoutAllResults=_scoutAllResults.filter(t=>!writtenSet.has(t.file_path));
      card.style.opacity='0';setTimeout(()=>card.remove(),300);
      document.getElementById('scoutCount').textContent=`${_scoutAllResults.length} track(s) with no genre`;
      document.getElementById('scoutStatFound').textContent=`${_scoutAllResults.length} missing genre`;
    } else {
      toast(`Write failed: ${d.message||'unknown error'}`,'error');
      addLog(`❌ Scout write failed for "${groupName}": ${d.message||''}`,'err');
    }
  }catch(e){toast(`Write error: ${e.message}`,'error');}
}

// ── Multi-folder queue ────────────────────────────────────────────────────────
let _subfolderQueue = [];
let _processedFolders = new Set();

function addSubfolderToQueue() {
  const sel = document.getElementById('subfolderPath');
  const val = sel ? sel.value.trim() : '';
  if (!val || val === '__up__') { toast('Select a folder first.', 'warn'); return; }
  const name = sel.options[sel.selectedIndex]?.text || val;
  if (_subfolderQueue.includes(val)) { toast('Already in queue.', 'warn'); return; }
  _subfolderQueue.push(val);
  renderSubfolderQueue();
}

function removeFromQueue(idx) {
  var removed = _subfolderQueue.splice(idx, 1);
  if (removed.length) _processedFolders.delete(removed[0]);
  renderSubfolderQueue();
}

function clearProcessedFromQueue() {
  _subfolderQueue = _subfolderQueue.filter(p => !_processedFolders.has(p));
  _processedFolders.clear();
  renderSubfolderQueue();
  toast('Processed folders cleared.', 'ok');
}

function markFoldersProcessed(paths) {
  paths.forEach(p => _processedFolders.add(p));
  renderSubfolderQueue();
}

function renderSubfolderQueue() {
  const wrap = document.getElementById('subfolderQueue');
  const list = document.getElementById('subfolderQueueList');
  if (!_subfolderQueue.length) { wrap.style.display = 'none'; return; }
  wrap.style.display = 'block';
  var hasProcessed = _subfolderQueue.some(p => _processedFolders.has(p));
  list.innerHTML = _subfolderQueue.map((p,i) => {
    var done = _processedFolders.has(p);
    var bg = done ? 'rgba(45,212,191,.15)' : 'var(--surface)';
    var border = done ? '1px solid var(--teal)' : 'none';
    var color = done ? 'var(--teal)' : 'var(--text)';
    var icon = done ? '✓ ' : '';
    return `
    <div style="display:flex;align-items:center;gap:.4rem;font-family:var(--mono);font-size:.55rem;color:${color};background:${bg};border:${border};padding:.3rem .5rem;border-radius:4px">
      <span style="flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${esc(p)}">${icon}${esc(p.split('/').pop())}</span>
      <button onclick="removeFromQueue(${i})" style="background:none;border:none;color:var(--muted2);cursor:pointer;font-size:.7rem;padding:0">✕</button>
    </div>`;
  }).join('');
  // Add CLEAR PROCESSED button if any are processed
  if (hasProcessed) {
    list.innerHTML += '<button onclick="clearProcessedFromQueue()" style="margin-top:.3rem;background:none;border:1px solid var(--teal);color:var(--teal);border-radius:4px;padding:.25rem .5rem;font-family:var(--mono);font-size:.48rem;cursor:pointer;letter-spacing:.5px;width:100%">✓ CLEAR PROCESSED</button>';
  }
}

// ── Override panel ────────────────────────────────────────────────────────────
const GENRE_LIST = [
  // Rock
  'Rock',
    'Classic Rock','Hard Rock','Soft Rock','Arena Rock','Glam Rock',
    'Alternative Rock','Indie Rock','Progressive Rock','Psychedelic Rock',
    'Punk Rock','New Wave','Blues Rock','Folk Rock','Southern Rock','Rockabilly',
  // Pop
  'Pop',
    'Adult Contemporary','Easy Listening','Oldies','Classic Hits',
    'Synth-Pop','Dance-Pop','Pop Rock','Indie Pop','Dream Pop',
    'Bubblegum Pop','Country Pop',
  // Country
  'Country',
    'Classic Country','Outlaw Country','Country Rock','Country Pop',
    'Bluegrass','Americana','Western','Honky Tonk',
  // R&B / Soul
  'R&B',
    'Soul','Motown','Funk','Neo Soul','Rhythm and Blues',
  // Hip-Hop
  'Hip-Hop',
    'Rap','Trap','Old School Hip-Hop',
  // Electronic / Dance
  'Electronic',
    'Dance','EDM','House','Techno','Trance','Synthwave','Disco',
  // Jazz
  'Jazz',
    'Smooth Jazz','Big Band','Swing','Bebop','Jazz Fusion',
  // Blues
  'Blues',
    'Delta Blues','Chicago Blues','Electric Blues',
  // Metal
  'Metal',
    'Heavy Metal','Hard Rock','Death Metal','Power Metal','Thrash Metal',
  // Alternative
  'Alternative',
    'Indie','Grunge','Post-Punk','Emo',
  // Folk / Acoustic
  'Folk',
    'Acoustic','Singer-Songwriter','Bluegrass','Americana',
  // Classical
  'Classical',
    'Orchestral','Chamber Music','Opera',
  // CCM / Gospel
  'CCM',
    'Contemporary Christian','Christian Rock','Christian Pop',
    'Christian Country','Christian Folk','Christian Blues',
    'Worship','Gospel','Southern Gospel','Urban Gospel',
    'Inspirational','Devotional',
  // Special / Mixed
  'Compilation','Soundtrack','Holiday','Instrumental',
  'Spoken Word','Comedy','Children\'s',
  // Custom — type your own
  'Custom…',
];
const MOOD_LIST = [
  'Energetic','Upbeat','Happy','Joyful','Feel Good','Singalong',
  'Chill','Peaceful','Relaxing','Laid Back','Dreamy',
  'Romantic','Warm','Nostalgic',
  'Melancholic','Emotional','Bittersweet',
  'Dark','Intense','Powerful','Anthemic','Epic',
  'Soulful','Funky','Groovy',
  'Uplifting','Inspirational','Motivating',
  'Party','Driving','Workout',
  'Worshipful','Reverent','Devotional','Spiritual',
  // Custom — type your own
  'Custom…',
];

// ── Custom genre/mood persistence ─────────────────────────────────────────────
let _customGenres = [];
let _customMoods  = [];

async function loadCustomTags() {
  try {
    const r = await fetch(`${API}/api/retriever/genres`, {headers:authHdrs()});
    if (!r.ok) return;
    const d = await r.json();
    _customGenres = d.genres || [];
    _customMoods  = d.moods  || [];
  } catch {}
}

async function saveCustomTag(kind, name) {
  try {
    await fetch(`${API}/api/retriever/genres`, {
      method:'POST', headers:authHdrs(),
      body: JSON.stringify({kind, name, action:'add'})
    });
    if (kind === 'genre' && !_customGenres.includes(name)) {
      _customGenres.push(name);
      populateDropdowns();
    }
    if (kind === 'mood'  && !_customMoods.includes(name))  {
      _customMoods.push(name);
      populateDropdowns();
    }
  } catch {}
}

async function deleteCustomTag(kind, name) {
  if (!confirm(`Remove "${name}" from your custom ${kind} list?`)) return;
  try {
    const r = await fetch(`${API}/api/retriever/genres`, {
      method:'POST', headers:authHdrs(),
      body: JSON.stringify({kind, name, action:'delete'})
    });
    const d = await r.json();
    if (r.ok) {
      if (kind === 'genre') _customGenres = _customGenres.filter(g => g !== name);
      if (kind === 'mood')  _customMoods  = _customMoods.filter(m => m !== name);
      populateDropdowns();
      toast(`Removed "${name}" from custom ${kind}s`, 'ok');
    }
  } catch(e) { toast(`Delete failed: ${e.message}`, 'error'); }
}

function populateDropdowns() {
  const allGenres = [...new Set([...GENRE_LIST.filter(g=>g!=='Custom…'), ..._customGenres, 'Custom…'])].sort((a,b)=>a==='Custom…'?1:b==='Custom…'?-1:a.localeCompare(b));
  const allMoods  = [...new Set([...MOOD_LIST.filter(m=>m!=='Custom…'),  ..._customMoods,  'Custom…'])].sort((a,b)=>a==='Custom…'?1:b==='Custom…'?-1:a.localeCompare(b));
  ['override-g1','override-g2','override-g3'].forEach((id,i) => {
    const sel = document.getElementById(id);
    if (!sel) return;
    const first = i === 0 ? '— select —' : '— optional —';
    sel.innerHTML = `<option value="">${first}</option>` +
      allGenres.map(g => `<option value="${esc(g)}">${esc(g)}</option>`).join('');
    sel.onchange = () => handleCustomOption(sel, 'genre');
  });
  ['override-m1','override-m2','override-m3'].forEach((id,i) => {
    const sel = document.getElementById(id);
    if (!sel) return;
    const first = i === 0 ? '— select —' : '— optional —';
    sel.innerHTML = `<option value="">${first}</option>` +
      allMoods.map(m => `<option value="${esc(m)}">${esc(m)}</option>`).join('');
    sel.onchange = () => handleCustomOption(sel, 'mood');
  });
  // Rebuild custom tag manager list (shows ✕ delete buttons for custom entries only)
  buildCustomTagManager();
}

function buildCustomTagManager() {
  // Genre custom tag manager
  const gWrap = document.getElementById('custom-genre-manager');
  const mWrap = document.getElementById('custom-mood-manager');
  if (gWrap) {
    gWrap.innerHTML = _customGenres.length
      ? _customGenres.map(g => `
          <span style="display:inline-flex;align-items:center;gap:.2rem;
            background:var(--surface3);border:1px solid var(--border2);
            border-radius:12px;padding:.15rem .45rem .15rem .55rem;
            font-family:var(--mono);font-size:.55rem;color:var(--teal);margin:.15rem">
            ${esc(g)}
            <button onclick="deleteCustomTag('genre','${esc(g)}')"
              style="background:none;border:none;color:var(--muted2);cursor:pointer;
              font-size:.65rem;padding:0;line-height:1;margin-left:.1rem"
              title="Remove ${esc(g)}" onmouseover="this.style.color='var(--red)'"
              onmouseout="this.style.color='var(--muted2)'">✕</button>
          </span>`).join('')
      : '<span style="font-family:var(--mono);font-size:.55rem;color:var(--muted2)">No custom genres saved</span>';
  }
  if (mWrap) {
    mWrap.innerHTML = _customMoods.length
      ? _customMoods.map(m => `
          <span style="display:inline-flex;align-items:center;gap:.2rem;
            background:var(--surface3);border:1px solid var(--border2);
            border-radius:12px;padding:.15rem .45rem .15rem .55rem;
            font-family:var(--mono);font-size:.55rem;color:var(--purple);margin:.15rem">
            ${esc(m)}
            <button onclick="deleteCustomTag('mood','${esc(m)}')"
              style="background:none;border:none;color:var(--muted2);cursor:pointer;
              font-size:.65rem;padding:0;line-height:1;margin-left:.1rem"
              title="Remove ${esc(m)}" onmouseover="this.style.color='var(--red)'"
              onmouseout="this.style.color='var(--muted2)'">✕</button>
          </span>`).join('')
      : '<span style="font-family:var(--mono);font-size:.55rem;color:var(--muted2)">No custom moods saved</span>';
  }
}

function handleCustomOption(sel, kind) {
  if (sel.value !== 'Custom…') {
    const existing = document.getElementById('custom-input-' + sel.id);
    if (existing) existing.remove();
    return;
  }
  if (document.getElementById('custom-input-' + sel.id)) return;
  const input = document.createElement('input');
  input.type        = 'text';
  input.id          = 'custom-input-' + sel.id;
  input.placeholder = 'Type custom value, press Enter…';
  input.style.cssText =
    'width:100%;box-sizing:border-box;margin-top:.2rem;display:block;' +
    'background:var(--surface2);color:var(--text);' +
    'border:1px solid var(--teal);border-radius:4px;' +
    'padding:.25rem .4rem;font-family:var(--mono);font-size:.52rem;';
  function commit() {
    const val = input.value.trim();
    if (val) {
      const opt = document.createElement('option');
      opt.value = val; opt.textContent = val;
      sel.appendChild(opt); sel.value = val;
      if (kind) saveCustomTag(kind, val);
    } else { sel.value = ''; }
    input.remove();
  }
  input.addEventListener('keydown', function(e) {
    if (e.key === 'Enter') { e.preventDefault(); commit(); }
    if (e.key === 'Escape') { sel.value = ''; input.remove(); }
  });
  input.addEventListener('blur', commit);
  sel.parentNode.insertBefore(input, sel.nextSibling);
  input.focus();
}

function openOverride() {
  if (!selectedTrackIds.size) { toast('Select tracks to override first.', 'warn'); return; }
  populateDropdowns();
  document.getElementById('overrideCount').textContent = `${selectedTrackIds.size} track(s) selected for override`;
  document.getElementById('overridePanel').style.display = 'block';
  document.getElementById('overridePanel').scrollIntoView({behavior:'smooth'});
}

function closeOverride() {
  document.getElementById('overridePanel').style.display = 'none';
}

async function applyOverride() {
  const genres = ['override-g1','override-g2','override-g3']
    .map(id => document.getElementById(id)?.value || '').filter(Boolean);
  const moods  = ['override-m1','override-m2','override-m3']
    .map(id => document.getElementById(id)?.value || '').filter(Boolean);
  if (!genres.length) { toast('Select at least one genre.', 'warn'); return; }
  try {
    const ids = [...selectedTrackIds];
    const r = await fetch(`${API}/api/retriever/override`, {
      method:'POST', headers:authHdrs(),
      body: JSON.stringify({track_ids:ids, genres, moods})
    });
    const d = await r.json();
    if (r.ok) {
      toast(`✓ ${d.message}`, 'ok');
      addLog(`✓ ${d.message}`, 'ok');
      closeOverride();
      ids.forEach(id => {
        const row = document.querySelector(`#previewTableBody tr[data-id="${id}"]`);
        if (row) row.remove();
      });
      _retAllTracks = _retAllTracks.filter(t => !ids.includes(t.track_id));
      selectedTrackIds.clear();
      document.getElementById('selectedCount').textContent = '0 selected';
      const retAll = document.getElementById('ret-select-all');
      if (retAll) retAll.checked = false;
      genres.forEach(g => { if (!GENRE_LIST.includes(g)) saveCustomTag('genre', g); });
      moods.forEach(m => { if (!MOOD_LIST.includes(m)) saveCustomTag('mood', m); });
    } else toast(`Override failed: ${d.detail || 'unknown error'}`, 'error');
  } catch(e) { toast(`Server error: ${e.message}`, 'error'); }
}

// ══════════════════════════════════════════════════════════════════════════════
//  ALBUM TAGGER — Folder-based, paginated, enhanced override
//  Cards group by FOLDER PATH (not album tag). 20 folders per page.
//  Override panel: Genre, Mood, Album name, Album Artist, Year,
//                  Compilation flag, Clear MBIDs.
//  Cards disappear on successful write — work pane = live to-do list.
// ══════════════════════════════════════════════════════════════════════════════

// State store — keyed by full folder path (unique, no collisions across folders)
let _atAlbums     = {};
let _atPaths      = [];     // current selected paths
let _atOffset     = 0;      // pagination cursor
let _atPageSize   = 20;
let _atTotalFolders = 0;

async function atReadTags() {
  // Collect selected paths
  let paths = [];
  if (_subfolderQueue.length > 0) {
    paths = [..._subfolderQueue];
  } else {
    const sel = document.getElementById('subfolderPath');
    const val = sel ? sel.value.trim() : '';
    if (val && val !== '__up__') paths = [val];
  }
  if (!paths.length) {
    toast('Select a folder in SUBFOLDER mode first.', 'warn');
    return;
  }

  // Reset pagination for a fresh load
  _atPaths    = paths;
  _atOffset   = 0;
  _atAlbums   = {};
  const cards = document.getElementById('albumTaggerCards');
  cards.innerHTML = '';
  document.getElementById('atStep1Results').style.display = 'none';

  // Ensure path prefix is loaded so folder paths show host paths, not docker paths
  await _grmLoadPathSettings();

  await _atFetchPage(true);
}

async function atLoadMore() {
  const btn = document.getElementById('atLoadMoreBtn');
  if (btn) { btn.disabled = true; btn.textContent = '⟳ Loading…'; }
  await _atFetchPage(false);
  if (btn) { btn.disabled = false; btn.textContent = '⬇ LOAD MORE FOLDERS'; }
}

async function _atFetchPage(isFirst) {
  const btn     = document.getElementById('atReadBtn');
  const loading = document.getElementById('atLoadingMsg');
  if (isFirst && btn)  { btn.disabled = true; btn.textContent = '⟳ Reading files…'; }
  if (loading) loading.style.display = 'block';

  addLog(`📂 Reading folder page — offset ${_atOffset}, page size ${_atPageSize}…`, 'info');

  try {
    const r = await fetch(`${API}/api/retriever/read-tags`, {
      method:'POST', headers:authHdrs(),
      body: JSON.stringify({
        paths:     _atPaths,
        page_size: _atPageSize,
        offset:    _atOffset
      })
    });
    const d = await r.json();
    if (!r.ok) throw new Error(d.detail || 'Read failed');

    _atTotalFolders = d.total_folders || 0;

    // Log the console message from the backend
    if (d.console_msg) addLog(d.console_msg, 'info');

    const cards = document.getElementById('albumTaggerCards');

    for (const alb of (d.albums || [])) {
      const key = alb.key;   // = full folder path — globally unique
      _atAlbums[key] = {
        folderName:     alb.folder_name,
        folderPath:     alb.folder_path,
        year:           alb.year,
        trackCount:     alb.track_count,
        tracks:         alb.tracks || [],       // per-track detail
        currentGenres:  alb.genres  || [],
        currentMoods:   alb.moods   || [],
        isCompilation:  alb.is_compilation || false,
        artists:        alb.artists || [],
        proposedGenres: [],
        proposedMoods:  [],
        overrideGenres: [],
        overrideMoods:  [],
        overrideAlbum:  '',
        overrideAlbumArtist: alb.is_compilation ? 'Various Artists' : '',
        overrideYear:   alb.year || null,
        isCompilationFlag: alb.is_compilation || false,
        clearMbids:     true,
        state:          'idle',
      };
      cards.appendChild(atBuildCard(key, _atAlbums[key]));
    }

    // Advance cursor
    _atOffset = (d.next_offset != null) ? d.next_offset : _atOffset + _atPageSize;

    // Show/hide Load More
    const lmWrap = document.getElementById('atLoadMoreWrap');
    const lmHint = document.getElementById('atLoadMoreHint');
    if (lmWrap) lmWrap.style.display = d.has_more ? 'block' : 'none';
    if (lmHint && d.has_more) {
      const remaining = _atTotalFolders - _atOffset;
      lmHint.textContent = `${remaining} more folder${remaining!==1?'s':''} remaining`;
    }

    document.getElementById('atStep1Results').style.display = 'block';
    atUpdateStatusBadge();

    // Mark scanned folders as processed in the queue
    markFoldersProcessed(_atPaths);

    addLog(`✓ Loaded ${d.albums?.length || 0} folders (${d.total_files || 0} files total).${d.has_more?' More available.':' All loaded.'}`, 'ok');

    if (isFirst && btn) { btn.textContent = '🔄 RE-READ TAGS'; btn.disabled = false; }
  } catch(e) {
    toast(`Read failed: ${e.message}`, 'error');
    addLog(`❌ Read tags failed: ${e.message}`, 'err');
    if (isFirst && btn) { btn.disabled = false; btn.textContent = '📂 READ TAGS FROM FILES'; }
  } finally {
    if (loading) loading.style.display = 'none';
  }
}

function atBuildCard(key, alb) {
  // safeKey: full folder path → safe DOM id segment
  const safeKey = key.replace(/[^a-z0-9]/gi, '_');

  const genreDisplay = alb.currentGenres.length
    ? alb.currentGenres.map(g =>
        `<span style="display:inline-block;background:rgba(20,184,166,.12);border:1px solid rgba(20,184,166,.3);color:var(--teal);font-size:.5rem;padding:.1rem .35rem;border-radius:10px;margin:.1rem">${esc(g)}</span>`
      ).join('')
    : '<span style="color:var(--muted2);font-size:.52rem;font-style:italic">no genres on file</span>';

  const moodDisplay = alb.currentMoods.length
    ? alb.currentMoods.map(m =>
        `<span style="display:inline-block;background:rgba(192,132,252,.12);border:1px solid rgba(192,132,252,.3);color:var(--purple);font-size:.5rem;padding:.1rem .35rem;border-radius:10px;margin:.1rem">${esc(m)}</span>`
      ).join('')
    : '<span style="color:var(--muted2);font-size:.52rem;font-style:italic">no moods on file</span>';

  const compilationBadge = alb.isCompilation
    ? `<span style="display:inline-block;background:rgba(251,191,36,.12);border:1px solid rgba(251,191,36,.3);color:var(--amber);font-size:.48rem;padding:.1rem .4rem;border-radius:10px;margin-left:.3rem">COMPILATION</span>`
    : '';

  const trackCountBadge = `<span style="display:inline-block;background:var(--surface);border:1px solid var(--border);color:var(--muted);font-size:.48rem;padding:.1rem .35rem;border-radius:10px;margin-left:.3rem">${alb.trackCount} track${alb.trackCount!==1?'s':''}</span>`;

  // Per-track rows for the Override panel
  const trackRows = (alb.tracks || []).map((t,i) =>
    `<div style="display:flex;align-items:center;gap:.4rem;padding:.2rem 0;border-bottom:1px solid var(--border)">
      <input type="checkbox" id="at-tr-${safeKey}-${i}" checked style="accent-color:var(--teal)">
      <span style="font-family:var(--mono);font-size:.5rem;flex:1;color:var(--text);overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${esc(t.file_path)}">${esc(t.title || t.file_path.split('/').pop())}</span>
      <span style="font-family:var(--mono);font-size:.46rem;color:var(--muted2);flex-shrink:0">${esc(t.album_tag || '—')}</span>
    </div>`
  ).join('');

  const card = document.createElement('div');
  card.id = `at-card-${safeKey}`;
  card.style.cssText = 'background:var(--surface2);border:1px solid var(--border);border-radius:8px;padding:.75rem;transition:border-color .2s,opacity .3s';

  card.innerHTML = `
    <!-- Card header -->
    <div style="display:flex;align-items:flex-start;gap:.75rem;flex-wrap:wrap">
      <div style="flex-shrink:0;padding-top:.15rem">
        <input type="checkbox" class="at-bulk-cb" data-key="${esc(key)}" onchange="atUpdateBulkBar()" style="accent-color:var(--crimson);width:16px;height:16px;cursor:pointer">
      </div>
      <div style="flex:1;min-width:180px">
        <div style="font-family:var(--mono);font-size:.65rem;color:var(--text);font-weight:bold">
          📁 ${esc(alb.folderName)}${compilationBadge}${trackCountBadge}
        </div>
        <div class="at-folder-path" style="font-family:var(--mono);font-size:.50rem;color:var(--text);margin-top:.15rem;word-break:break-all;user-select:all;cursor:text;opacity:.85" title="Click to select path for copying">${esc(atTranslatePath(alb.folderPath || key))}</div>
        ${alb.isCompilation && alb.artists.length
          ? `<div style="font-family:var(--mono);font-size:.48rem;color:var(--muted);margin-top:.15rem">Artists: ${esc(alb.artists.slice(0,4).join(', '))}${alb.artists.length>4?'…':''}</div>`
          : ''}
        <div style="margin-top:.35rem">
          <span style="font-family:var(--mono);font-size:.48rem;color:var(--muted2);letter-spacing:1px">GENRES: </span>${genreDisplay}
        </div>
        <div style="margin-top:.2rem">
          <span style="font-family:var(--mono);font-size:.48rem;color:var(--muted2);letter-spacing:1px">MOODS: </span>${moodDisplay}
        </div>
        <!-- Suggest chips row -->
        <div id="at-chips-${safeKey}" style="display:none;margin-top:.4rem">
          <span style="font-family:var(--mono);font-size:.46rem;color:var(--muted2);letter-spacing:1px">SUGGESTED ▶ </span>
          <span id="at-chips-inner-${safeKey}"></span>
        </div>
        <div id="at-proposed-${safeKey}" style="margin-top:.35rem;display:none"></div>
      </div>
      <div style="display:flex;flex-direction:column;gap:.3rem;align-items:flex-end">
        <div style="display:flex;gap:.35rem;flex-wrap:wrap;justify-content:flex-end">
          <button class="btn btn-outline btn-sm" onclick="atLookup('${escJs(key)}')"
            id="at-lookup-btn-${safeKey}"
            title="${alb.isCompilation ? 'Various Artists — Lookup may not work well. Consider Override instead.' : 'Get genre recommendation from MusicBrainz'}">
            ${alb.isCompilation ? '⚠️ LOOKUP' : '🔍 LOOKUP'}
          </button>
          <button class="btn btn-outline btn-sm" onclick="atSuggest('${escJs(key)}')"
            id="at-suggest-btn-${safeKey}"
            title="Get genre suggestions from your library DB or Last.fm artist tags">
            💡 SUGGEST
          </button>
          <button class="btn btn-pink btn-sm" onclick="atToggleOverride('${escJs(key)}')" title="Manually set all tags for this folder">✏️ OVERRIDE</button>
          <button class="btn btn-outline btn-sm" onclick="atKeep('${escJs(key)}')" title="Tags are fine — leave them alone">✓ KEEP</button>
        </div>
        <div id="at-state-${safeKey}" style="font-family:var(--mono);font-size:.48rem;color:var(--muted2)">idle</div>
      </div>
    </div>

    <!-- Enhanced Override Panel (hidden by default) -->
    <div id="at-override-${safeKey}" style="display:none;margin-top:.65rem;padding:.65rem;background:var(--surface);border-radius:6px;border:1px solid var(--border)">

      <!-- Album fields -->
      <div style="font-family:var(--mono);font-size:.5rem;color:var(--muted);letter-spacing:1px;margin-bottom:.35rem">ALBUM INFO</div>
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:.35rem;margin-bottom:.45rem">
        <div>
          <div style="font-family:var(--mono);font-size:.46rem;color:var(--muted2);margin-bottom:.15rem">ALBUM NAME</div>
          <input id="at-album-${safeKey}" type="text" value="${esc(alb.folderName)}"
            style="width:100%;box-sizing:border-box;background:var(--surface2);color:var(--text);border:1px solid var(--border);border-radius:4px;padding:.25rem .4rem;font-family:var(--mono);font-size:.52rem">
        </div>
        <div>
          <div style="font-family:var(--mono);font-size:.46rem;color:var(--muted2);margin-bottom:.15rem">ALBUM ARTIST</div>
          <input id="at-albumartist-${safeKey}" type="text"
            value="${esc(alb.isCompilation ? 'Various Artists' : (alb.artists[0] || ''))}"
            style="width:100%;box-sizing:border-box;background:var(--surface2);color:var(--text);border:1px solid var(--border);border-radius:4px;padding:.25rem .4rem;font-family:var(--mono);font-size:.52rem">
        </div>
      </div>
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:.35rem;margin-bottom:.45rem">
        <div>
          <div style="font-family:var(--mono);font-size:.46rem;color:var(--muted2);margin-bottom:.15rem">YEAR</div>
          <input id="at-year-${safeKey}" type="number" min="1900" max="2030"
            value="${alb.year || ''}" placeholder="e.g. 1985"
            style="width:100%;box-sizing:border-box;background:var(--surface2);color:var(--text);border:1px solid var(--border);border-radius:4px;padding:.25rem .4rem;font-family:var(--mono);font-size:.52rem">
        </div>
        <div style="display:flex;flex-direction:column;gap:.25rem;justify-content:flex-end">
          <label style="display:flex;align-items:center;gap:.35rem;font-family:var(--mono);font-size:.5rem;color:var(--amber);cursor:pointer">
            <input type="checkbox" id="at-comp-${safeKey}" ${alb.isCompilation?'checked':''} style="accent-color:var(--amber)">
            Mark as Compilation
          </label>
          <label style="display:flex;align-items:center;gap:.35rem;font-family:var(--mono);font-size:.5rem;color:var(--muted);cursor:pointer"
            title="Recommended: MusicBrainz IDs on files can cause the Retriever to skip re-fetching fresh metadata. Clearing them lets ChartHound do a clean lookup next scan.">
            <input type="checkbox" id="at-mbid-${safeKey}" checked style="accent-color:var(--teal)">
            Clear MusicBrainz IDs ⓘ
          </label>
        </div>
      </div>

      <!-- Genre dropdowns -->
      <div style="font-family:var(--mono);font-size:.5rem;color:var(--muted);letter-spacing:1px;margin-bottom:.3rem">GENRES (max 3)</div>
      <div style="display:grid;grid-template-columns:repeat(3,1fr);gap:.35rem;margin-bottom:.45rem">
        ${[1,2,3].map(n => `
        <select id="at-g${n}-${safeKey}" onchange="handleCustomOption(this)" style="background:var(--surface2);color:var(--text);border:1px solid var(--border);border-radius:4px;padding:.25rem;font-family:var(--mono);font-size:.52rem">
          <option value="">— genre ${n}${n>1?' (opt)':''} —</option>
          ${GENRE_LIST.map(g=>`<option value="${esc(g)}">${esc(g)}</option>`).join('')}
        </select>`).join('')}
      </div>

      <!-- Mood dropdowns -->
      <div style="font-family:var(--mono);font-size:.5rem;color:var(--muted);letter-spacing:1px;margin-bottom:.3rem">MOODS (max 3)</div>
      <div style="display:grid;grid-template-columns:repeat(3,1fr);gap:.35rem;margin-bottom:.5rem">
        ${[1,2,3].map(n => `
        <select id="at-m${n}-${safeKey}" onchange="handleCustomOption(this)" style="background:var(--surface2);color:var(--text);border:1px solid var(--border);border-radius:4px;padding:.25rem;font-family:var(--mono);font-size:.52rem">
          <option value="">— mood ${n}${n>1?' (opt)':''} —</option>
          ${MOOD_LIST.map(m=>`<option value="${esc(m)}">${esc(m)}</option>`).join('')}
        </select>`).join('')}
      </div>

      <!-- Per-track selection -->
      <div style="font-family:var(--mono);font-size:.5rem;color:var(--muted);letter-spacing:1px;margin-bottom:.3rem">
        TRACKS — uncheck any to exclude from write
        <button onclick="atSelectAllTracks('${escJs(key)}',true)" style="background:none;border:none;color:var(--teal);font-family:var(--mono);font-size:.46rem;cursor:pointer;padding:0 .3rem">all</button>
        <button onclick="atSelectAllTracks('${escJs(key)}',false)" style="background:none;border:none;color:var(--muted);font-family:var(--mono);font-size:.46rem;cursor:pointer;padding:0 .3rem">none</button>
      </div>
      <div style="max-height:120px;overflow-y:auto;background:var(--surface2);border-radius:4px;border:1px solid var(--border);padding:.25rem .4rem;margin-bottom:.5rem">
        ${trackRows}
      </div>

      <!-- Action buttons -->
      <div style="display:flex;gap:.35rem;flex-wrap:wrap">
        <button class="btn btn-pink btn-sm" onclick="atApplyOverride('${escJs(key)}')">✓ APPLY OVERRIDE</button>
        <button class="btn btn-teal btn-sm" onclick="atWriteAlbum('${escJs(key)}')">⬇ WRITE NOW</button>
        <button class="btn btn-outline btn-sm" onclick="atToggleOverride('${escJs(key)}')">✕ CLOSE</button>
      </div>
    </div>`;

  return card;
}

// ── Lookup (warns on compilation) ────────────────────────────────────────────
async function atLookup(key) {
  const safeKey = key.replace(/[^a-z0-9]/gi,'_');
  const alb = _atAlbums[key];
  if (!alb) return;

  // Compilation warning — still works but user is warned
  if (alb.isCompilation) {
    if (!confirm('This folder has multiple artists — MusicBrainz Lookup may not return useful results.\n\nContinue anyway? (Override is usually better for compilations.)')) return;
  }

  const btn   = document.getElementById(`at-lookup-btn-${safeKey}`);
  const propEl = document.getElementById(`at-proposed-${safeKey}`);
  if (btn)   { btn.disabled = true; btn.textContent = '⟳'; }
  if (propEl){ propEl.style.display = 'block'; propEl.innerHTML = '<span style="color:var(--muted2);font-size:.52rem">🌐 Looking up MusicBrainz…</span>'; }

  addLog(`🔍 Lookup: ${alb.folderName}…`, 'info');

  try {
    // Use folder name as album, first artist as artist for lookup
    const artist = alb.artists[0] || alb.folderName;
    const r = await fetch(`${API}/api/retriever/album-lookup`, {
      method:'POST', headers:authHdrs(),
      body: JSON.stringify({artist, album: alb.folderName, year: alb.year})
    });
    const d = await r.json();
    if (r.ok && d.proposed_genres?.length) {
      _atAlbums[key].proposedGenres = d.proposed_genres;
      atSetState(key, 'accepted');

      const currStr = alb.currentGenres.join(' · ') || 'none';
      const propStr = d.proposed_genres.join(' · ');
      const changed = currStr !== propStr;
      if (propEl) propEl.innerHTML = changed
        ? `<span style="font-family:var(--mono);font-size:.5rem">
             <span style="color:var(--muted2);text-decoration:line-through">${esc(currStr)}</span>
             <span style="color:var(--text)"> → </span>
             <span style="color:var(--teal)">${esc(propStr)}</span>
             <span style="color:var(--muted2)"> (${d.confidence})</span>
           </span>`
        : `<span style="font-family:var(--mono);font-size:.5rem;color:var(--muted2)">✓ Already matches (${d.confidence})</span>`;
      addLog(`✓ Lookup complete: ${alb.folderName} → ${propStr}`, 'ok');
    } else {
      if (propEl) propEl.innerHTML = '<span style="color:var(--muted2);font-size:.52rem">No recommendation found — use Override.</span>';
      addLog(`⚠️ No result for: ${alb.folderName}`, 'warn');
    }
  } catch(e) {
    if (propEl) propEl.innerHTML = `<span style="color:var(--pink);font-size:.52rem">Lookup failed: ${esc(e.message)}</span>`;
    addLog(`❌ Lookup error: ${e.message}`, 'err');
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = alb.isCompilation ? '⚠️ LOOKUP' : '🔍 LOOKUP'; }
  }
}


// ── 💡 Suggest — DB-first, Last.fm fallback, chip UI ─────────────────────────
async function atSuggest(key) {
  const safeKey = key.replace(/[^a-z0-9]/gi,'_');
  const alb = _atAlbums[key];
  if (!alb) return;
  const btn        = document.getElementById(`at-suggest-btn-${safeKey}`);
  const chipsWrap  = document.getElementById(`at-chips-${safeKey}`);
  const chipsInner = document.getElementById(`at-chips-inner-${safeKey}`);
  if (!chipsWrap || !chipsInner) return;
  if (btn) { btn.disabled = true; btn.textContent = '⟳'; }
  const artist = alb.isCompilation ? '' : (alb.artists[0] || alb.folderName);
  if (!artist) {
    chipsInner.innerHTML = '<span style="font-family:var(--mono);font-size:.5rem;color:var(--muted2)">Compilation — no single artist to suggest from.</span>';
    chipsWrap.style.display = 'block';
    if (btn) { btn.disabled = false; btn.textContent = '💡 SUGGEST'; }
    return;
  }
  addLog(`💡 Suggest: fetching for "${artist}"…`, 'info');
  try {
    const r = await fetch(`${API}/api/retriever/scout/suggest?artist=${encodeURIComponent(artist)}`, { headers: authHdrs() });
    const d = await r.json();
    if (!d.suggestions || !d.suggestions.length) {
      chipsInner.innerHTML = '<span style="font-family:var(--mono);font-size:.5rem;color:var(--muted2)">No suggestions found — try LOOKUP or OVERRIDE.</span>';
      addLog(`💡 No suggestions for "${artist}"`, 'warn');
    } else {
      const src = d.source === 'db'
        ? '<span style="font-family:var(--mono);font-size:.44rem;color:var(--green);margin-left:.4rem">● from library</span>'
        : '<span style="font-family:var(--mono);font-size:.44rem;color:var(--gold);margin-left:.4rem">● Last.fm</span>';
      chipsInner.innerHTML = d.suggestions.map(g =>
        `<span class="scout-chip" id="at-chip-${safeKey}-${g.replace(/[^a-z0-9]/gi,'_')}" onclick="atSuggestChipClick('${escJs(key)}','${escJs(g)}')" title="Click to fill next empty genre slot in Override">${esc(g)}</span>`
      ).join('') + src;
      addLog(`💡 Suggestions for "${artist}": ${d.suggestions.join(', ')} (${d.source})`, 'ok');
      // Auto-open Override panel so chips fill directly into the dropdowns
      const overridePanel = document.getElementById(`at-override-${safeKey}`);
      if (overridePanel && overridePanel.style.display === 'none') atToggleOverride(key);
    }
    chipsWrap.style.display = 'block';
  } catch(e) {
    chipsInner.innerHTML = `<span style="font-family:var(--mono);font-size:.5rem;color:var(--pink)">Error: ${esc(e.message)}</span>`;
    chipsWrap.style.display = 'block';
    addLog(`✗ Suggest failed: ${e.message}`, 'err');
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = '💡 SUGGEST'; }
  }
}

function atSuggestChipClick(key, genre) {
  const safeKey = key.replace(/[^a-z0-9]/gi,'_');
  const alb = _atAlbums[key];
  if (!alb) return;
  const overridePanel = document.getElementById(`at-override-${safeKey}`);
  if (overridePanel && overridePanel.style.display === 'none') atToggleOverride(key);
  for (let n = 1; n <= 3; n++) {
    const sel = document.getElementById(`at-g${n}-${safeKey}`);
    if (sel && !sel.value) {
      if (!GENRE_LIST.includes(genre) && !_customGenres.includes(genre)) {
        saveCustomTag('genre', genre);
        const opt = document.createElement('option');
        opt.value = genre; opt.textContent = genre;
        sel.appendChild(opt);
      }
      sel.value = genre;
      const chip = document.getElementById(`at-chip-${safeKey}-${genre.replace(/[^a-z0-9]/gi,'_')}`);
      if (chip) { chip.classList.add('used'); chip.onclick = null; }
      return;
    }
  }
  toast('All 3 genre slots are filled. Clear one first.', 'warn');
}

// ── Card sort ─────────────────────────────────────────────────────────────────
let _atSortKey = 'folder';
let _atSortAsc = true;

function atSortCards(key, el) {
  if (_atSortKey === key) { _atSortAsc = !_atSortAsc; }
  else { _atSortKey = key; _atSortAsc = key !== 'year'; }
  document.querySelectorAll('[id^="atSort-"]').forEach(b => b.classList.remove('active'));
  if (el) el.classList.add('active');
  const container = document.getElementById('albumTaggerCards');
  const loadMore  = document.getElementById('atLoadMoreWrap');
  const cards     = [...container.querySelectorAll('[id^="at-card-"]')];
  const safeToEntry = {};
  Object.entries(_atAlbums).forEach(([k, a]) => { safeToEntry[k.replace(/[^a-z0-9]/gi,'_')] = [k, a]; });
  cards.sort((a, b) => {
    const eA = safeToEntry[a.id.replace('at-card-','')];
    const eB = safeToEntry[b.id.replace('at-card-','')];
    if (!eA || !eB) return 0;
    let vA, vB;
    if (_atSortKey === 'artist') {
      vA = (eA[1].artists[0] || eA[1].folderName).toLowerCase();
      vB = (eB[1].artists[0] || eB[1].folderName).toLowerCase();
    } else if (_atSortKey === 'year') {
      vA = eA[1].year || 9999;
      vB = eB[1].year || 9999;
    } else {
      vA = eA[1].folderName.toLowerCase();
      vB = eB[1].folderName.toLowerCase();
    }
    if (vA < vB) return _atSortAsc ? -1 : 1;
    if (vA > vB) return _atSortAsc ? 1 : -1;
    return 0;
  });
  if (loadMore) container.removeChild(loadMore);
  cards.forEach(c => container.appendChild(c));
  if (loadMore) container.appendChild(loadMore);
}

// ── Card filter ───────────────────────────────────────────────────────────────
function atFilterCards(q) {
  const lq = q.toLowerCase();
  const safeToEntry = {};
  Object.entries(_atAlbums).forEach(([k, a]) => { safeToEntry[k.replace(/[^a-z0-9]/gi,'_')] = a; });
  document.querySelectorAll('[id^="at-card-"]').forEach(card => {
    const a = safeToEntry[card.id.replace('at-card-','')];
    if (!a) return;
    const text = [a.folderName, ...(a.artists||[]), ...(a.currentGenres||[]), String(a.year||'')].join(' ').toLowerCase();
    card.style.display = text.includes(lq) ? '' : 'none';
  });
}


// ── Toggle override panel open/close ─────────────────────────────────────────
function atToggleOverride(key) {
  const safeKey = key.replace(/[^a-z0-9]/gi,'_');
  const el = document.getElementById('at-override-' + safeKey);
  if (!el) return;
  const opening = el.style.display === 'none';
  el.style.display = opening ? 'block' : 'none';

  if (opening) {
    // Pre-fill dropdowns with current file tags
    const alb = _atAlbums[key];
    if (!alb) return;
    [1,2,3].forEach(n => {
      const gs = document.getElementById(`at-g${n}-${safeKey}`);
      if (gs && alb.currentGenres[n-1]) gs.value = alb.currentGenres[n-1];
      const ms = document.getElementById(`at-m${n}-${safeKey}`);
      if (ms && alb.currentMoods[n-1])  ms.value = alb.currentMoods[n-1];
    });
  }
}

function atSelectAllTracks(key, checked) {
  const safeKey = key.replace(/[^a-z0-9]/gi,'_');
  const alb = _atAlbums[key];
  if (!alb) return;
  alb.tracks.forEach((_,i) => {
    const cb = document.getElementById(`at-tr-${safeKey}-${i}`);
    if (cb) cb.checked = checked;
  });
}

// ── Apply override (stores values, shows preview on card) ─────────────────────
function atApplyOverride(key) {
  const safeKey = key.replace(/[^a-z0-9]/gi,'_');
  const alb = _atAlbums[key];
  if (!alb) return;

  const genres = [1,2,3].map(n => document.getElementById(`at-g${n}-${safeKey}`)?.value||'').filter(Boolean);
  const moods  = [1,2,3].map(n => document.getElementById(`at-m${n}-${safeKey}`)?.value||'').filter(Boolean);

  // Read album-level fields
  const albumName   = document.getElementById(`at-album-${safeKey}`)?.value.trim() || '';
  const albumArtist = document.getElementById(`at-albumartist-${safeKey}`)?.value.trim() || '';
  const yearVal     = parseInt(document.getElementById(`at-year-${safeKey}`)?.value || '') || null;
  const isComp      = document.getElementById(`at-comp-${safeKey}`)?.checked || false;
  const clearMbids  = document.getElementById(`at-mbid-${safeKey}`)?.checked ?? true;

  // Store in state
  alb.overrideGenres      = genres;
  alb.overrideMoods       = moods;
  alb.overrideAlbum       = albumName;
  alb.overrideAlbumArtist = albumArtist;
  alb.overrideYear        = yearVal;
  alb.isCompilationFlag   = isComp;
  alb.clearMbids          = clearMbids;

  // Show summary on card face
  const propEl = document.getElementById(`at-proposed-${safeKey}`);
  if (propEl) {
    const parts = [];
    if (albumName) parts.push(`Album: "${esc(albumName)}"`);
    if (albumArtist) parts.push(`Artist: "${esc(albumArtist)}"`);
    if (genres.length) parts.push(`Genres: ${esc(genres.join(' · '))}`);
    if (moods.length)  parts.push(`Moods: ${esc(moods.join(' · '))}`);
    if (isComp)        parts.push('COMPILATION ✓');
    propEl.style.display = 'block';
    propEl.innerHTML = `<span style="font-family:var(--mono);font-size:.5rem;color:var(--pink)">${parts.join('  ·  ')}</span>`;
  }

  atSetState(key, 'override');
  document.getElementById(`at-override-${safeKey}`).style.display = 'none';
}

// ── Write a single album card ─────────────────────────────────────────────────
async function atWriteAlbum(key) {
  const alb = _atAlbums[key];
  if (!alb) return;
  const safeKey = key.replace(/[^a-z0-9]/gi,'_');

  // Determine which tracks to write (checked checkboxes in override panel)
  const selectedPaths = [];
  alb.tracks.forEach((t, i) => {
    const cb = document.getElementById(`at-tr-${safeKey}-${i}`);
    // If override panel was never opened, include all tracks
    if (!cb || cb.checked) selectedPaths.push(t.file_path);
  });
  if (!selectedPaths.length) {
    toast('No tracks selected.', 'warn');
    return;
  }

  // Determine genres/moods — override wins over lookup wins over nothing
  const genres = alb.overrideGenres.length ? alb.overrideGenres : alb.proposedGenres;
  const moods  = alb.overrideMoods.length  ? alb.overrideMoods  : alb.proposedMoods;

  addLog(`⬇ Writing: ${alb.folderName} (${selectedPaths.length} tracks)…`, 'info');

  try {
    const r = await fetch(`${API}/api/retriever/album-override`, {
      method: 'POST', headers: authHdrs(),
      body: JSON.stringify({
        file_paths:   selectedPaths,
        album_name:   alb.overrideAlbum || null,
        album_artist: alb.overrideAlbumArtist || null,
        year:         alb.overrideYear || null,
        is_compilation: alb.isCompilationFlag || false,
        clear_mbids:  alb.clearMbids ?? true,
        genres,
        moods,
      })
    });
    const d = await r.json();

    // Print per-file console messages from backend
    if (d.console_msgs) d.console_msgs.forEach(m => addLog(m, m.includes('❌') ? 'err' : 'info'));

    if (r.ok && d.success > 0) {
      toast(`✓ ${d.success} track${d.success!==1?'s':''} written`, 'ok');
      // Card disappears — it's done
      atRemoveCard(key);
    } else {
      toast(`Write failed: ${d.detail || d.message || 'error'}`, 'error');
      atSetState(key, 'override');
    }
  } catch(e) {
    toast(`Write error: ${e.message}`, 'error');
    addLog(`❌ Write error: ${e.message}`, 'err');
  }
}

// ── Remove a card from the DOM after successful write ─────────────────────────
function atRemoveCard(key) {
  const safeKey = key.replace(/[^a-z0-9]/gi,'_');
  const card = document.getElementById(`at-card-${safeKey}`);
  if (card) {
    card.style.opacity = '0';
    setTimeout(() => {
      card.remove();
      delete _atAlbums[key];
      atUpdateStatusBadge();
    }, 300);
  }
}

function atKeep(key) {
  // Keep = "tags are fine, nothing to write" — remove card immediately
  atSetState(key, 'keep');
  setTimeout(() => atRemoveCard(key), 150);
}

function atSetState(key, state) {
  const safeKey = key.replace(/[^a-z0-9]/gi,'_');
  if (_atAlbums[key]) _atAlbums[key].state = state;
  const card  = document.getElementById(`at-card-${safeKey}`);
  const label = document.getElementById(`at-state-${safeKey}`);
  const colors = {
    idle:     'var(--border)',
    keep:     'var(--muted)',
    override: 'var(--pink)',
    accepted: 'var(--teal)',
    written:  'var(--amber)',
  };
  const labels = {
    idle:     'idle',
    keep:     '✓ keeping as-is',
    override: '✏️ override set — ready to write',
    accepted: '✓ lookup accepted — ready to write',
    written:  '⬇ written',
  };
  if (card)  card.style.borderColor = colors[state] || 'var(--border)';
  if (label) label.textContent      = labels[state] || state;
  atUpdateStatusBadge();
}

function atUpdateStatusBadge() {
  const states  = Object.values(_atAlbums).map(a => a.state);
  const total   = states.length;
  const pending = states.filter(s => s === 'idle').length;
  const done    = total - pending;
  const badge   = document.getElementById('atStatusBadge');
  const pageBadge = document.getElementById('atPageBadge');
  if (badge) badge.textContent =
    `${total} folder${total!==1?'s':''} loaded · ${done} handled · ${pending} pending`;
  if (pageBadge) pageBadge.textContent =
    _atTotalFolders > total ? `(${_atTotalFolders - total} more available — click Load More)` : '';
}

// ── Bulk write all overrides ──────────────────────────────────────────────────
async function atWriteAllOverrides() {
  const toWrite = Object.entries(_atAlbums).filter(([,a]) => a.state === 'override');
  if (!toWrite.length) { toast('No albums with overrides set.', 'warn'); return; }
  addLog(`⬇ Writing ${toWrite.length} override album${toWrite.length!==1?'s':''}…`, 'info');
  let written = 0;
  for (const [key, alb] of toWrite) {
    const safeKey = key.replace(/[^a-z0-9]/gi,'_');
    const genres = alb.overrideGenres.length ? alb.overrideGenres : alb.proposedGenres;
    const moods  = alb.overrideMoods.length  ? alb.overrideMoods  : [];
    const selectedPaths = alb.tracks.map(t => t.file_path);
    try {
      const r = await fetch(`${API}/api/retriever/album-override`, {
        method:'POST', headers:authHdrs(),
        body: JSON.stringify({
          file_paths:     selectedPaths,
          album_name:     alb.overrideAlbum || null,
          album_artist:   alb.overrideAlbumArtist || null,
          year:           alb.overrideYear || null,
          is_compilation: alb.isCompilationFlag || false,
          clear_mbids:    alb.clearMbids ?? true,
          genres, moods,
        })
      });
      const d = await r.json();
      if (d.console_msgs) d.console_msgs.forEach(m => addLog(m, 'info'));
      if (r.ok && d.success > 0) { written += d.success; atRemoveCard(key); }
    } catch(e) { addLog(`❌ ${e.message}`, 'err'); }
  }
  toast(`✓ ${written} tracks written`, 'ok');
}

// ── Bulk write all accepted lookups ───────────────────────────────────────────
async function atWriteAllAccepted() {
  const toWrite = Object.entries(_atAlbums).filter(([,a]) => a.state === 'accepted' && a.proposedGenres.length);
  if (!toWrite.length) { toast('No accepted lookups to write.', 'warn'); return; }
  addLog(`⬇ Writing ${toWrite.length} accepted album${toWrite.length!==1?'s':''}…`, 'info');
  let written = 0;
  for (const [key, alb] of toWrite) {
    try {
      const r = await fetch(`${API}/api/retriever/album-override`, {
        method:'POST', headers:authHdrs(),
        body: JSON.stringify({
          file_paths:  alb.tracks.map(t => t.file_path),
          genres:      alb.proposedGenres,
          moods:       alb.proposedMoods || [],
          clear_mbids: true,
        })
      });
      const d = await r.json();
      if (d.console_msgs) d.console_msgs.forEach(m => addLog(m, 'info'));
      if (r.ok && d.success > 0) { written += d.success; atRemoveCard(key); }
    } catch(e) { addLog(`❌ ${e.message}`, 'err'); }
  }
  toast(`✓ ${written} tracks written (accepted lookups)`, 'ok');
}

function atKeepAll() {
  Object.keys(_atAlbums).forEach(k => {
    atSetState(k, 'keep');
    setTimeout(() => atRemoveCard(k), 150);
  });
}

// ══════════════════════════════════════════════════════════════════════════════
//  BULK OVERRIDE — Select multiple cards, apply genre/mood to all at once
// ══════════════════════════════════════════════════════════════════════════════

var _atBulkDropdownsPopulated = false;

function atTranslatePath(path) {
  // Translate docker path (/music/...) to host path using Top Dogs's prefix settings
  if (!path) return path;
  if (_grmServerPrefix && _grmDockerPrefix && path.startsWith(_grmDockerPrefix)) {
    return _grmServerPrefix + path.slice(_grmDockerPrefix.length);
  }
  return path;
}

function atPopulateBulkDropdowns() {
  if (_atBulkDropdownsPopulated) return;
  _atBulkDropdownsPopulated = true;
  [1,2,3].forEach(n => {
    const gs = document.getElementById('atBulk-g' + n);
    if (gs) {
      gs.innerHTML = '<option value="">— genre ' + n + (n>1?' (opt)':'') + ' —</option>'
        + GENRE_LIST.map(g => '<option value="' + esc(g) + '">' + esc(g) + '</option>').join('');
    }
    const ms = document.getElementById('atBulk-m' + n);
    if (ms) {
      ms.innerHTML = '<option value="">— mood ' + n + (n>1?' (opt)':'') + ' —</option>'
        + MOOD_LIST.map(m => '<option value="' + esc(m) + '">' + esc(m) + '</option>').join('');
    }
  });
}

function atToggleSelectAll(checked) {
  document.querySelectorAll('.at-bulk-cb').forEach(cb => { cb.checked = checked; });
  atUpdateBulkBar();
}

function atGetSelectedKeys() {
  var keys = [];
  document.querySelectorAll('.at-bulk-cb:checked').forEach(cb => {
    keys.push(cb.getAttribute('data-key'));
  });
  return keys;
}

function atUpdateBulkBar() {
  var keys = atGetSelectedKeys();
  var bar = document.getElementById('atBulkOverrideBar');
  var count = document.getElementById('atBulkCount');
  var allCbs = document.querySelectorAll('.at-bulk-cb');
  if (keys.length > 0 && allCbs.length > 0) {
    bar.style.display = 'block';
    count.textContent = keys.length;
    atPopulateBulkDropdowns();
  } else {
    bar.style.display = 'none';
  }
  // Sync the select-all checkbox
  var allChecked = allCbs.length > 0 && keys.length === allCbs.length;
  var selectAll = document.getElementById('atSelectAllCb');
  if (selectAll) selectAll.checked = allChecked;
}

function atBulkApplyOnly() {
  var keys = atGetSelectedKeys();
  if (!keys.length) { toast('No cards selected.', 'warn'); return; }
  var genres = [1,2,3].map(n => document.getElementById('atBulk-g' + n)?.value || '').filter(Boolean);
  var moods  = [1,2,3].map(n => document.getElementById('atBulk-m' + n)?.value || '').filter(Boolean);
  if (!genres.length && !moods.length) { toast('Select at least one genre or mood.', 'warn'); return; }

  var applied = 0;
  keys.forEach(function(key) {
    var alb = _atAlbums[key];
    if (!alb) return;
    if (genres.length) alb.overrideGenres = genres.slice();
    if (moods.length)  alb.overrideMoods  = moods.slice();

    // Show summary on card face
    var safeKey = key.replace(/[^a-z0-9]/gi, '_');
    var propEl = document.getElementById('at-proposed-' + safeKey);
    if (propEl) {
      var parts = [];
      if (alb.overrideGenres.length) parts.push('Genres: ' + esc(alb.overrideGenres.join(' · ')));
      if (alb.overrideMoods.length)  parts.push('Moods: ' + esc(alb.overrideMoods.join(' · ')));
      propEl.style.display = 'block';
      propEl.innerHTML = '<span style="font-family:var(--mono);font-size:.5rem;color:var(--pink)">' + parts.join('  ·  ') + '</span>';
    }
    atSetState(key, 'override');
    applied++;
  });
  toast(applied + ' card' + (applied !== 1 ? 's' : '') + ' set to override — click WRITE ALL OVERRIDES to save.', 'ok');
}

async function atBulkApplyAndWrite() {
  var keys = atGetSelectedKeys();
  if (!keys.length) { toast('No cards selected.', 'warn'); return; }
  var genres = [1,2,3].map(n => document.getElementById('atBulk-g' + n)?.value || '').filter(Boolean);
  var moods  = [1,2,3].map(n => document.getElementById('atBulk-m' + n)?.value || '').filter(Boolean);
  if (!genres.length && !moods.length) { toast('Select at least one genre or mood.', 'warn'); return; }

  addLog('⬇ Bulk writing ' + keys.length + ' album' + (keys.length !== 1 ? 's' : '') + '…', 'info');
  var written = 0;
  for (var i = 0; i < keys.length; i++) {
    var key = keys[i];
    var alb = _atAlbums[key];
    if (!alb) continue;
    var filePaths = alb.tracks.map(function(t) { return t.file_path; });
    try {
      var r = await fetch(API + '/api/retriever/album-override', {
        method: 'POST', headers: authHdrs(),
        body: JSON.stringify({
          file_paths:     filePaths,
          album_name:     null,
          album_artist:   null,
          year:           null,
          is_compilation: false,
          clear_mbids:    true,
          genres:         genres,
          moods:          moods,
        })
      });
      var d = await r.json();
      if (d.console_msgs) d.console_msgs.forEach(function(m) { addLog(m, 'info'); });
      if (r.ok && d.success > 0) { written += d.success; atRemoveCard(key); }
    } catch(e) { addLog('❌ ' + alb.folderName + ': ' + e.message, 'err'); }
  }
  toast('✓ ' + written + ' tracks written (bulk override)', 'ok');
  // Delay bar update to let card removal animations finish
  setTimeout(function() { atUpdateBulkBar(); }, 500);
}

// Legacy stubs
function loadAlbumTagger() {}
function albumTaggerAcceptAll() { atKeepAll(); }
function updateAlbumAcceptedCount() { atUpdateStatusBadge(); }
async function writeSelected() {
  if (!selectedTrackIds.size) { toast('Select tracks to write first.', 'warn'); return; }
  try {
    const ids = [...selectedTrackIds];
    // If scanJobId was cleared after scan completed, fetch the most recent job_id
    let jobId = scanJobId;
    if (!jobId) {
      try {
        const jr = await fetch(`${API}/api/retriever/scan/latest`, {headers:authHdrs()});
        if (jr.ok) { const jd = await jr.json(); jobId = jd.job_id || null; }
      } catch {}
    }
    const r = await fetch(`${API}/api/retriever/write`, {method:'POST', headers:authHdrs(), body:JSON.stringify({job_id:jobId, track_ids:ids, write_art:true})});
    if (r.ok) {
      const d = await r.json();
      toast(`✓ ${d.message}`, 'ok');
      addLog(`✓ ${d.message}`, 'ok');
      if (d.success > 0) {
        ids.forEach(id => {
          const row = document.querySelector(`#previewTableBody tr[data-id="${id}"]`);
          if (row) row.remove();
        });
        _retAllTracks = _retAllTracks.filter(t => !ids.includes(t.track_id));
      }
      selectedTrackIds.clear();
      document.getElementById('selectedCount').textContent = '0 selected';
      const retAll = document.getElementById('ret-select-all');
      if (retAll) retAll.checked = false;
    } else toast('Write request failed.', 'error');
  } catch { toast('Server error.', 'error'); }
}
async function confirmClearDatabase() {
  const msg = `⚠️ CLEAR FULL DATABASE\n\nThis will permanently delete:\n• All scanned track records\n• All chart data\n• All write logs\n• All scan job history\n\nYour music FILES on disk will NOT be affected.\n\nThis cannot be undone. Are you sure?`;
  if (!confirm(msg)) return;
  if (!confirm('Last chance — are you absolutely sure you want to clear the entire database?')) return;
  try {
    const r = await fetch(`${API}/api/retriever/cache`, {method:'DELETE', headers:authHdrs()});
    if (r.ok) {
      toast('Database cleared.', 'ok');
      addLog('Full database cleared. Ready for fresh scan.', 'info');
      document.getElementById('previewSection').style.display = 'none';
      document.getElementById('previewTableBody').innerHTML = '';
      selectedTrackIds.clear();
      document.getElementById('selectedCount').textContent = '0 selected';
      document.getElementById('statTotal').textContent = '0';
      document.getElementById('statProcessed').textContent = '0';
      document.getElementById('statMatched').textContent = '0';
      document.getElementById('statFailed').textContent = '0';
      document.getElementById('statCached').textContent = '0';
      document.getElementById('scanProgressBar').style.width = '0%'; document.getElementById('scanProgressWrap').classList.remove('bar-pulsing');
    } else {
      toast('Failed to clear database.', 'error');
    }
  } catch(e) {
    toast('Error: ' + e.message, 'error');
  }
}

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
      : (d.status === 'idle' ? 'Not run yet.' : esc(d.message || ''));
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
          folder: document.getElementById('ger-folder').value.trim() || null};
}
async function geOpenReview() {
  document.getElementById('ret-main').classList.add('ge-reviewing');
  document.getElementById('ge-review').style.display = '';
  await geTaxonomy();
  geLoad(1);
}
function geCloseReview() {
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
    Object.assign(_ger, {page: d.page, pages: Math.max(1, Math.ceil(d.total / d.size)), total: d.total, rows: d.rows});
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
        <a href="#" onclick="geEditArtist(${i});return false" style="margin-left:.75rem;color:var(--amber);font-weight:normal;font-size:.62rem">✎ set genre for ALL songs by this artist</a></td></tr>
        <tr class="ger-edit" id="ger-aedit-${i}" style="display:none"><td></td><td colspan="4" id="ger-aedit-cell-${i}"></td></tr>`;
    }
    const conf = x.confidence === 'conflict' ? '<span style="color:var(--red)" title="Sources disagree — worth a look">⚠ CONFLICT</span>'
               : (x.confidence === 'thin' || x.confidence === 'check') ? '<span style="color:var(--amber)" title="Only one source knew this song — usually fine">? LITTLE DATA</span>'
               : x.confidence === 'manual' ? '<span style="color:var(--pink)">EDITED</span>' : '<span style="color:var(--muted2)">ok</span>';
    html += `<tr id="ger-row-${i}"><td><input type="checkbox" class="ger-cb" data-i="${i}" ${_ger.sel.has(x.track_id) ? 'checked' : ''} onchange="geToggle(${i}, this.checked)"></td>
      <td>${esc(x.title)}</td>
      <td><span class="ger-new" onclick="geEditRow(${i})">${esc(x.proposed.join('; '))}</span></td>
      <td style="color:var(--muted2)">${esc(x.current.join('; ') || '—')}</td><td>${conf}</td></tr>
      <tr class="ger-edit" id="ger-edit-${i}" style="display:none"><td></td><td colspan="4" id="ger-edit-cell-${i}"></td></tr>`;
  });
  body.innerHTML = html;
  geSyncSel();
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

async function clearCache() {
  if (!confirm('Clear the Retriever lookup cache?\n\nThe next Retriever scan re-fetches metadata online. Your tracks, Top Dogs results, Hit Scores and hand-edited tracks are NOT affected.')) return;
  try {
    const r = await fetch(`${API}/api/retriever/cache`, {method:'DELETE', headers:authHdrs()});
    if (r.ok) {
      toast('Cache cleared.', 'ok');
      addLog('Cache cleared. Ready for fresh scan.', 'info');
      // Reset preview table and selected tracks
      document.getElementById('previewSection').style.display = 'none';
      document.getElementById('previewTableBody').innerHTML = '';
      selectedTrackIds.clear();
      document.getElementById('selectedCount').textContent = '0 selected';
      document.getElementById('statTotal').textContent = '0';
      document.getElementById('statProcessed').textContent = '0';
      document.getElementById('statMatched').textContent = '0';
      document.getElementById('statFailed').textContent = '0';
      document.getElementById('statCached').textContent = '0';
      document.getElementById('scanProgressBar').style.width = '0%'; document.getElementById('scanProgressWrap').classList.remove('bar-pulsing');
      document.getElementById('scanStatusTitle').textContent = 'READY TO SCAN';
      document.getElementById('scanStatusSub').textContent = 'Configure options and click Start Scan';
      scanJobId = null;
    }
  } catch { toast('Error clearing cache.', 'error'); }
}
function addLog(msg, type='') {
  const el = document.getElementById('scanLog');
  const line = document.createElement('div');
  line.className = type ? `log-${type}` : '';
  line.textContent = `[${new Date().toLocaleTimeString()}] ${msg}`;
  el.appendChild(line); el.scrollTop = el.scrollHeight;
}

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

// SUBFOLDER BROWSER
async function browseSubfolder(subPath) {
  // If called from ⟳ button with no arg, drill into currently selected folder
  let path;
  if (subPath !== undefined) {
    path = subPath;
  } else {
    const sel = document.getElementById('subfolderPath');
    const selected = sel ? sel.value : '';
    if (selected && selected !== '__up__' && selected !== '') {
      // Drill into selected folder
      path = selected.replace(/^.*?\/music/, '').replace(/^\//, '');
    } else {
      path = browseCurrentPath;
    }
  }
  try {
    const r = await fetch(`${API}/api/retriever/browse?path=${encodeURIComponent(path)}`, {headers:authHdrs()});
    if (!r.ok) { toast('Could not browse music folder. Is your /music volume mounted?', 'error'); return; }
    const data = await r.json();
    const sel = document.getElementById('subfolderPath');
    const hint = document.getElementById('subfolderHint');
    sel.innerHTML = '<option value="">— select a folder —</option>';
    // Add parent option if we're inside a subfolder
    if (path) {
      const parentPath = path.split('/').slice(0,-1).join('/');
      const opt = document.createElement('option');
      opt.value = '__up__'; opt.textContent = '⬆ ../ (go up)';
      sel.appendChild(opt);
    }
    data.entries.forEach(entry => {
      const opt = document.createElement('option');
      opt.value = entry.path; opt.textContent = entry.name;
      sel.appendChild(opt);
    });
    browseCurrentPath = path;
    hint.textContent = `📂 ${data.path} — ${data.entries.length} folder(s)`;
    sel.onchange = function() {
      if (this.value === '__up__') {
        // Go up one level
        const up = browseCurrentPath.split('/').slice(0,-1).join('/');
        const rel = up.replace(data.base,'').replace(/^\//,'');
        browseSubfolder(rel);
      } else if (this.value) {
        hint.textContent = `Selected: ${this.value} — click ⟳ to browse inside, or click START SCAN to scan this folder`;
      }
    };
  } catch(e) { toast(`Browse error: ${e.message}`, 'error'); }
}


