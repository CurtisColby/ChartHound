'use strict';
const API = '';
let authToken = localStorage.getItem('ch_token') || '';
let currentUser = localStorage.getItem('ch_user') || '';
let authMode = 'login';
let heartbeatTimer = null;

const SERVICES = ['plex','emby','jellyfin','lastfm','prowlarr','radarr','sonarr','qbittorrent','sabnzbd','discogs'];
const connState = Object.fromEntries(SERVICES.map(s => [s, false]));

const OS_COMMANDS = {
  linux:    'python3 -c "import secrets; print(secrets.token_hex(32))"',
  mac:      'python3 -c "import secrets; print(secrets.token_hex(32))"',
  winps:    'python -c "import secrets; print(secrets.token_hex(32))"',
  wincmd:   'python -c "import secrets; print(secrets.token_hex(32))"',
  synology: 'docker run --rm python:3.12-slim python3 -c "import secrets; print(secrets.token_hex(32))"',
  qnap:     'docker run --rm python:3.12-slim python3 -c "import secrets; print(secrets.token_hex(32))"',
  unraid:   'python3 -c "import secrets; print(secrets.token_hex(32))"',
  truenas:  'python3 -c "import secrets; print(secrets.token_hex(32))"',
  proxmox:  'python3 -c "import secrets; print(secrets.token_hex(32))"',
};

const CLIENT_PORTS = {qbittorrent:'8080', deluge:'8112', transmission:'9091'};

// BOOT
window.addEventListener('DOMContentLoaded', async () => {
  await checkLockdown();
  if (authToken) await tryAutoLogin();
});

async function checkLockdown() {
  try {
    const r = await fetch(`${API}/api/auth/status`);
    const d = await r.json();
    if (!d.lockdown_active) document.getElementById('registerToggle').classList.remove('hidden');
  } catch {}
}

async function tryAutoLogin() {
  try {
    const r = await fetch(`${API}/api/health`, {headers: authHdrs()});
    if (r.ok) { const d = await r.json(); showApp(d.secret_key_placeholder); return; }
    if (r.status === 401) { handleSessionExpired(); return; }
  } catch {}
  clearAuth();
}

// AUTH
function authHdrs() { return {'Authorization': `Bearer ${authToken}`, 'Content-Type': 'application/json'}; }

// Tracks whether the session-expired banner has already fired this page-lifetime.
// Prevents banner spam when multiple polling calls 401 in the same second.
let _sessionExpiredFired = false;

// Drop-in fetch wrapper that auto-injects auth headers and handles 401 globally.
// Use this for new fetch calls and for any call that runs on a poll/timer.
async function authFetch(url, opts) {
  opts = opts || {};
  const headers = Object.assign({}, authHdrs(), opts.headers || {});
  const r = await fetch(url, Object.assign({}, opts, {headers}));
  if (r.status === 401 && authToken) {
    // Token was present but server rejected it. Treat as expired session.
    handleSessionExpired();
  }
  return r;
}

// Triggered when a 401 reveals the JWT has expired during a session, OR when the
// boot-time auto-login health check fails with 401. Bounces the user back to the
// login screen with an explanatory banner so they understand what just happened.
function handleSessionExpired() {
  if (_sessionExpiredFired) return;
  _sessionExpiredFired = true;
  if (heartbeatTimer) clearInterval(heartbeatTimer);
  clearAuth();
  // Hide app, show login
  const hdr = document.getElementById('appHeader'); if (hdr) hdr.style.display = 'none';
  const shl = document.getElementById('appShell');  if (shl) shl.style.display = 'none';
  const skb = document.getElementById('secretKeyBanner'); if (skb) skb.classList.remove('show');
  const ls  = document.getElementById('loginScreen'); if (ls) ls.classList.remove('hidden');
  // Show the banner
  const info = document.getElementById('loginInfo');
  if (info) {
    info.innerHTML = '⏱ Your session has expired.<br>Please log back in to reconnect.';
    info.classList.remove('hidden');
  }
  // Clear any stale error from a previous attempt
  const errEl = document.getElementById('loginErr');
  if (errEl) errEl.textContent = '';
  // Clear password field for the new login
  const pw = document.getElementById('loginPass'); if (pw) pw.value = '';
}

function clearAuth() {
  authToken = ''; currentUser = '';
  localStorage.removeItem('ch_token'); localStorage.removeItem('ch_user');
}
function toggleAuthMode() {
  authMode = authMode === 'login' ? 'register' : 'login';
  document.getElementById('loginBtn').textContent = authMode === 'register' ? 'CREATE ACCOUNT' : 'ENTER THE KENNEL';
  document.getElementById('registerToggle').textContent = authMode === 'register' ? '← BACK TO LOGIN' : '↓ CREATE ACCOUNT';
  document.getElementById('loginErr').textContent = '';
}
async function doLogin() {
  const username = document.getElementById('loginUser').value.trim();
  const password = document.getElementById('loginPass').value;
  const errEl = document.getElementById('loginErr');
  errEl.textContent = '';
  // Clear any session-expired banner from a previous expired session
  const info = document.getElementById('loginInfo');
  if (info) { info.classList.add('hidden'); info.innerHTML = ''; }
  _sessionExpiredFired = false;
  if (!username || !password) { errEl.textContent = 'Enter username and password.'; return; }
  const ep = authMode === 'register' ? '/api/auth/register' : '/api/auth/login';
  try {
    const r = await fetch(`${API}${ep}`, {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({username, password})});
    const d = await r.json();
    if (!r.ok) { errEl.textContent = d.detail || 'Authentication failed.'; return; }
    authToken = d.access_token; currentUser = d.username;
    localStorage.setItem('ch_token', authToken); localStorage.setItem('ch_user', currentUser);
    const hr = await fetch(`${API}/api/health`, {headers: authHdrs()});
    const hd = hr.ok ? await hr.json() : {};
    showApp(hd.secret_key_placeholder, authMode === 'register');
  } catch { errEl.textContent = 'Cannot reach server. Is Docker running?'; }
}
document.addEventListener('keydown', e => {
  if (e.key === 'Enter' && !document.getElementById('loginScreen').classList.contains('hidden')) doLogin();
});
function doLogout() {
  clearAuth();
  if (heartbeatTimer) clearInterval(heartbeatTimer);
  document.getElementById('appHeader').style.display = 'none';
  document.getElementById('appShell').style.display = 'none';
  document.getElementById('secretKeyBanner').classList.remove('show');
  document.getElementById('loginScreen').classList.remove('hidden');
  document.getElementById('loginPass').value = '';
}
async function showApp(isPlaceholderKey = false, newAccount = false) {
  document.getElementById('loginScreen').classList.add('hidden');
  document.getElementById('appHeader').style.display = 'flex';
  document.getElementById('appShell').style.display = 'flex';
  document.getElementById('userBadge').textContent = `${currentUser.toUpperCase()} · LOGOUT`;
  if (isPlaceholderKey) document.getElementById('secretKeyBanner').classList.add('show');
  await loadAllStatuses();
  await loadPathSettings();
  startHeartbeat();
  stLoad();
  if (newAccount) { switchTab('start'); toast('Account created — follow Start Here, step 1.', 'ok'); }
  else toast(`Welcome back, ${currentUser}.`, 'ok');
  checkForUpdate();
}

async function checkForUpdate() {
  const CURRENT = 'v1.0.0';
  try {
    const r = await fetch('https://api.github.com/repos/DowntownDiscipline96/ChartHound/releases/latest', {
      headers: {'Accept': 'application/vnd.github+json'},
      cache: 'no-store'
    });
    if (!r.ok) return;
    const d = await r.json();
    const latest = d.tag_name || '';
    if (latest && latest !== CURRENT) {
      document.getElementById('nav-update-text').textContent = `Update: ${CURRENT} → ${latest}`;
      document.getElementById('nav-update-banner').style.display = 'block';
    }
  } catch(e) { /* silent — no network or rate limited */ }
}

// TABS
function switchTab(name) {
  document.querySelectorAll('.nav-item').forEach(el => el.classList.remove('active'));
  document.querySelectorAll('.tab-panel').forEach(el => el.classList.remove('active'));
  document.getElementById(`nav-${name}`).classList.add('active');
  document.getElementById(`panel-${name}`).classList.add('active');
  document.getElementById('vinylIcon').classList.toggle('spinning', ['retriever','sniffer','bloodhound','tracker'].includes(name));
  if (name !== 'retriever' && typeof gePlayStop === 'function') gePlayStop();
  if (name === 'kennel' && typeof checkDownloadPath === 'function') checkDownloadPath();
  if (name === 'start') stLoad();
  else tabHeader(name);
  if (name === 'retriever') {
    retInjectScrollNav();
    if (['conflict', 'nogenre'].includes(window._stWantView)) {
      const sc = document.getElementById('ger-scope'); if (sc) sc.value = window._stWantView;
      window._stWantView = null; geOpenReview();
    }
  }
  if (name === 'groomer') grmInit();
  if (name === 'sniffer') snfInit();
  if (name === 'bloodhound') bhInit();
  if (name === 'tracker') trkInit();
  if (name === 'vet') vetInit();
}

// CONNECTIONS — all bugs fixed (BUG-001, BUG-002, BUG-006, BUG-007, BUG-010)
async function saveConn(service) {
  const urlEl = document.getElementById(`cURL-${service}`);
  const tokenEl = document.getElementById(`cToken-${service}`);
  const userIDEl = document.getElementById(`cUserID-${service}`);
  const clientUserEl = document.getElementById(`cUser-${service}`);
  const token = tokenEl ? tokenEl.value.trim() : '';
  const url = urlEl ? urlEl.value.trim() : '';
  if (!token && !url) { setFeedback(service, 'Enter your credentials before saving.', 'error'); return; }
  const extra = {};
  if (userIDEl && userIDEl.value.trim()) extra.user_id = userIDEl.value.trim();
  setFeedback(service, 'Saving…', '');
  try {
    const body = {service};
    if (url) body.base_url = url;
    if (token) body.token = token;
    if (clientUserEl && clientUserEl.value.trim()) body.username = clientUserEl.value.trim();
    if (Object.keys(extra).length) body.extra_json = extra;
    const r = await fetch(`${API}/api/kennel/save`, {method:'POST', headers:authHdrs(), body:JSON.stringify(body)});
    const d = await r.json();
    if (!r.ok) throw new Error(d.detail || 'Save failed');
    setFeedback(service, '✓ Saved and encrypted. Click TEST to verify the live connection.', 'ok');
    if (tokenEl && token) { tokenEl.value = ''; tokenEl.placeholder = '●●●●●●●● (stored encrypted)'; }
    toast(`${service} credentials saved.`, 'ok');
  } catch(e) { setFeedback(service, `✗ ${e.message}`, 'error'); }
}

async function testConn(service) {
  const btn = document.getElementById(`testBtn-${service}`);
  btn.disabled = true;
  setCardDot(service, 'spin');
  setFeedback(service, 'Testing connection…', '');
  try {
    const r = await fetch(`${API}/api/kennel/test/${service}`, {method:'POST', headers:authHdrs()});
    const d = await r.json();
    if (r.ok && d.ok) {
      setCardDot(service, 'ok');
      setFeedback(service, `✓ ${d.detail}`, 'ok');
      setVerified(service, d.verified_at, true);
      connState[service] = true;
      const card = document.getElementById(`connCard-${service}`);
      if (card) card.className = 'conn-card connected';
      toast(`${service} connected!`, 'ok');
    } else {
      const errMsg = d.error || d.detail || 'Connection failed';
      if (errMsg.includes('503')) {
        setFeedback(service, `⟳ Server starting up — retried automatically. ${errMsg}`, 'warn');
      } else { throw new Error(errMsg); }
    }
  } catch(e) {
    setCardDot(service, 'error');
    setFeedback(service, `✗ ${e.message}`, 'error');
    setVerified(service, null, false);
    connState[service] = false;
    const card = document.getElementById(`connCard-${service}`);
    if (card) card.className = 'conn-card failed';
  }
  btn.disabled = false;
  refreshAllDots();
}

async function disconnectConn(service) {
  if (!confirm(`Remove saved ${service} credentials?`)) return;
  try {
    const r = await fetch(`${API}/api/kennel/disconnect/${service}`, {method:'DELETE', headers:authHdrs()});
    const d = await r.json();
    if (r.ok) {
      connState[service] = false;
      setCardDot(service, '');
      setVerified(service, null, false);
      setFeedback(service, '✓ Disconnected. Credentials removed.', 'ok');
      const card = document.getElementById(`connCard-${service}`);
      if (card) card.className = 'conn-card';
      const tokenEl = document.getElementById(`cToken-${service}`);
      if (tokenEl) { tokenEl.value = ''; tokenEl.placeholder = 'Stored encrypted · never logged'; }
      const urlEl = document.getElementById(`cURL-${service}`);
      if (urlEl) { urlEl.value = ''; }
      refreshAllDots();
      toast(`${service} disconnected.`, 'warn');
    }
  } catch(e) { setFeedback(service, `✗ ${e.message}`, 'error'); }
}

function updateClientURL() {
  const type = document.getElementById('cClientType').value;
  const port = CLIENT_PORTS[type] || '8080';
  const el = document.getElementById('cURL-qbittorrent');
  if (el) el.placeholder = `http://YOUR-SERVER-IP:${port}`;
}

function _getDLClientType() {
  return document.getElementById('cClientType').value;
}

async function saveDLClient() {
  const svc = _getDLClientType();
  const url = (document.getElementById('cURL-qbittorrent')?.value || '').trim();
  const token = (document.getElementById('cToken-qbittorrent')?.value || '').trim();
  const username = (document.getElementById('cUser-qbittorrent')?.value || '').trim();
  if (!token && !url) { setFeedback('qbittorrent', 'Enter your credentials before saving.', 'error'); return; }
  setFeedback('qbittorrent', 'Saving…', '');
  try {
    const body = {service: svc};
    if (url) body.base_url = url;
    if (token) body.token = token;
    if (username) body.username = username;
    const r = await fetch(`${API}/api/kennel/save`, {method:'POST', headers:authHdrs(), body:JSON.stringify(body)});
    const d = await r.json();
    if (!r.ok) throw new Error(d.detail || 'Save failed');
    setFeedback('qbittorrent', `✓ Saved as ${svc}. Click TEST to verify.`, 'ok');
    const tokenEl = document.getElementById('cToken-qbittorrent');
    if (tokenEl && token) { tokenEl.value = ''; tokenEl.placeholder = '●●●●●●●● (stored encrypted)'; }
    toast(`${svc} credentials saved.`, 'ok');
  } catch(e) { setFeedback('qbittorrent', `✗ ${e.message}`, 'error'); }
}

async function testDLClient() {
  const svc = _getDLClientType();
  const btn = document.getElementById('testBtn-qbittorrent');
  btn.disabled = true;
  setCardDot('qbittorrent', 'spin');
  setFeedback('qbittorrent', `Testing ${svc} connection…`, '');
  try {
    const r = await fetch(`${API}/api/kennel/test/${svc}`, {method:'POST', headers:authHdrs()});
    const d = await r.json();
    if (r.ok && d.ok) {
      setCardDot('qbittorrent', 'ok');
      setFeedback('qbittorrent', `✓ ${d.detail}`, 'ok');
      setVerified('qbittorrent', d.verified_at, true);
      connState['qbittorrent'] = true;
      const card = document.getElementById('connCard-qbittorrent');
      if (card) card.className = 'conn-card connected';
      toast(`${svc} connected!`, 'ok');
    } else {
      const errMsg = d.error || d.detail || 'Connection failed';
      throw new Error(errMsg);
    }
  } catch(e) {
    setCardDot('qbittorrent', 'error');
    setFeedback('qbittorrent', `✗ ${e.message}`, 'error');
    setVerified('qbittorrent', null, false);
    connState['qbittorrent'] = false;
    const card = document.getElementById('connCard-qbittorrent');
    if (card) card.className = 'conn-card failed';
  }
  btn.disabled = false;
  refreshAllDots();
}

async function disconnectDLClient() {
  const svc = _getDLClientType();
  if (!confirm(`Remove saved ${svc} credentials?`)) return;
  try {
    const r = await fetch(`${API}/api/kennel/disconnect/${svc}`, {method:'DELETE', headers:authHdrs()});
    const d = await r.json();
    if (r.ok) {
      connState['qbittorrent'] = false;
      setCardDot('qbittorrent', '');
      setVerified('qbittorrent', null, false);
      setFeedback('qbittorrent', `✓ ${svc} disconnected. Credentials removed.`, 'ok');
      const card = document.getElementById('connCard-qbittorrent');
      if (card) card.className = 'conn-card';
      const tokenEl = document.getElementById('cToken-qbittorrent');
      if (tokenEl) { tokenEl.value = ''; tokenEl.placeholder = 'Stored encrypted · never logged'; }
      const urlEl = document.getElementById('cURL-qbittorrent');
      if (urlEl) { urlEl.value = ''; }
      refreshAllDots();
      toast(`${svc} disconnected.`, 'warn');
    }
  } catch(e) { setFeedback('qbittorrent', `✗ ${e.message}`, 'error'); }
}

const NZB_PORTS = {sabnzbd:'8080', nzbget:'6789'};

function updateNzbClientURL() {
  const type = document.getElementById('cNzbClientType').value;
  const port = NZB_PORTS[type] || '8080';
  const el = document.getElementById('cURL-sabnzbd');
  if (el) el.placeholder = `http://YOUR-SERVER-IP:${port}`;
}

async function saveNzbClient() {
  const clientType = document.getElementById('cNzbClientType').value;
  const url = (document.getElementById('cURL-sabnzbd')?.value || '').trim();
  const token = (document.getElementById('cToken-sabnzbd')?.value || '').trim();
  if (!url && !token) { setFeedback('sabnzbd', 'Enter your URL and API key before saving.', 'error'); return; }
  setFeedback('sabnzbd', 'Saving…', '');
  try {
    const body = { service: clientType, base_url: url };
    if (token) body.token = token;
    const r = await fetch(`${API}/api/kennel/save`, { method:'POST', headers:authHdrs(), body:JSON.stringify(body) });
    const d = await r.json();
    if (!r.ok) throw new Error(d.detail || 'Save failed');
    setFeedback('sabnzbd', '✓ Saved and encrypted. Click TEST to verify.', 'ok');
    const tokenEl = document.getElementById('cToken-sabnzbd');
    if (tokenEl && token) { tokenEl.value = ''; tokenEl.placeholder = '●●●●●●●● (stored encrypted)'; }
    toast(`${clientType} credentials saved.`, 'ok');
  } catch(e) { setFeedback('sabnzbd', `✗ ${e.message}`, 'error'); }
}

async function testNzbClient() {
  const btn = document.getElementById('testBtn-sabnzbd');
  btn.disabled = true;
  setCardDot('sabnzbd', 'spin');
  setFeedback('sabnzbd', 'Testing connection…', '');
  try {
    const r = await fetch(`${API}/api/sniffer/test-nzb-client`, { method:'POST', headers:authHdrs() });
    const d = await r.json();
    if (r.ok && d.ok) {
      setCardDot('sabnzbd', 'ok');
      setFeedback('sabnzbd', `✓ ${d.detail}`, 'ok');
      setVerified('sabnzbd', d.verified_at, true);
      connState['sabnzbd'] = true;
      const card = document.getElementById('connCard-sabnzbd');
      if (card) card.className = 'conn-card connected';
      toast('NZB client connected!', 'ok');
    } else {
      throw new Error(d.error || d.detail || 'Connection failed');
    }
  } catch(e) {
    setCardDot('sabnzbd', 'error');
    setFeedback('sabnzbd', `✗ ${e.message}`, 'error');
    setVerified('sabnzbd', null, false);
    connState['sabnzbd'] = false;
    const card = document.getElementById('connCard-sabnzbd');
    if (card) card.className = 'conn-card failed';
  }
  btn.disabled = false;
  refreshAllDots();
}

async function loadAllStatuses() {
  try {
    const r = await fetch(`${API}/api/kennel/status`, {headers:authHdrs()});
    if (r.status === 401 && authToken) { handleSessionExpired(); return; }
    if (!r.ok) return;
    const statuses = await r.json();
    const DL_CLIENTS = ['qbittorrent', 'deluge', 'transmission'];
    for (const s of statuses) {
      connState[s.service] = s.connected;
      // Download clients share one card — map onto qbittorrent DOM IDs
      if (DL_CLIENTS.includes(s.service) && s.service !== 'qbittorrent') {
        if (s.connected || s.has_token) {
          // Set the dropdown to match the connected client
          const dd = document.getElementById('cClientType');
          if (dd) dd.value = s.service;
          updateClientURL();
          setCardDot('qbittorrent', s.connected ? 'ok' : (s.has_token ? 'warn' : ''));
          setVerified('qbittorrent', s.verified_at, s.connected);
          if (s.base_url) {
            const el = document.getElementById('cURL-qbittorrent');
            if (el) el.value = s.base_url;
          }
          if (s.has_token) {
            const el = document.getElementById('cToken-qbittorrent');
            if (el) el.placeholder = '●●●●●●●● (stored encrypted)';
          }
          if (s.connected) {
            const card = document.getElementById('connCard-qbittorrent');
            if (card) card.className = 'conn-card connected';
          }
        }
        continue;
      }
      setCardDot(s.service, s.connected ? 'ok' : (s.has_token ? 'warn' : ''));
      setVerified(s.service, s.verified_at, s.connected);
      if (s.base_url) {
        const el = document.getElementById(`cURL-${s.service}`);
        if (el && !el.value) el.value = s.base_url;
      }
      if (s.has_token) {
        const el = document.getElementById(`cToken-${s.service}`);
        if (el) el.placeholder = '●●●●●●●● (stored encrypted)';
      }
      if (s.connected) {
        const card = document.getElementById(`connCard-${s.service}`);
        if (card) card.className = 'conn-card connected';
      }
    }
    refreshAllDots();
  } catch {}
  // Load sniffer download path into Kennel UI
  loadSnifferPath();
}

// PATH TRANSLATOR — BUG-005 fixed
async function loadPathSettings() {
  try {
    const r = await fetch(`${API}/api/kennel/path-settings`, {headers:authHdrs()});
    if (!r.ok) return;
    const d = await r.json();
    const serverEl    = document.getElementById('pathServer');
    const dockerEl    = document.getElementById('pathDocker');
    const navidromeEl = document.getElementById('pathNavidrome');
    if (serverEl    && d.server_prefix)    { serverEl.value = d.server_prefix; serverEl.dispatchEvent(new Event('input')); }
    if (dockerEl    && d.docker_prefix)    dockerEl.value    = d.docker_prefix;
    if (navidromeEl && d.navidrome_prefix) navidromeEl.value = d.navidrome_prefix;
    if (!d.server_prefix) advOpen('kennel');
    // Sync Navidrome M3U button state in Top Dogs if results are loaded
    grmSyncBar();
  } catch {}
}
async function savePath() {
  const sp = document.getElementById('pathServer').value.trim();
  const dp = document.getElementById('pathDocker').value.trim() || '/music';
  const np = document.getElementById('pathNavidrome') ? document.getElementById('pathNavidrome').value.trim() : '';
  if (!sp) { toast('Enter a media server path prefix first.', 'warn'); return; }
  try {
    const r = await fetch(`${API}/api/kennel/save-path`, {method:'POST', headers:authHdrs(), body:JSON.stringify({server_prefix:sp, docker_prefix:dp, navidrome_prefix:np})});
    if (r.ok) { toast('Path translation saved.', 'ok'); grmSyncBar(); }
    else toast('Failed to save path.', 'error');
  } catch { toast('Server error.', 'error'); }
}
function previewPath() {
  const input = document.getElementById('pathTestInput').value;
  const sp = document.getElementById('pathServer').value.trim();
  const dp = document.getElementById('pathDocker').value.trim() || '/music';
  const el = document.getElementById('pathPreview');
  if (!input) { el.textContent = 'Enter a path above to preview.'; return; }
  const translated = (sp && input.startsWith(sp)) ? dp + input.slice(sp.length) : input;
  el.innerHTML = `<span class="path-arrow">SERVER ▶</span> ${esc(input)}<br><span class="path-arrow">DOCKER ▶</span> <span style="color:var(--teal)">${esc(translated)}</span>`;
}

// ══════════════════════════════════════════════════════════════════════════════
//  M8c — per-tab "What this does / Do this next" header + Advanced folds
// ══════════════════════════════════════════════════════════════════════════════
const _thReady = g => g.prowlarr && g.downloader
  ? `✅ Ready${g.path ? ' · downloads go to ' + esc(g.path) : ''}`
  : '⚠️ Needs Prowlarr and a download client connected in The Kennel.';
const TAB_HELP = {
  kennel: {
    what: 'Stores the addresses and keys for your media server, chart sources and downloaders — encrypted.',
    next: 'Fill in a card, <b>SAVE</b>, then <b>TEST</b>. A green dot means it works.',
    live: s => s.connect.media.length
      ? `✅ ${s.connect.verified.length} services working, including ${s.connect.media.map(m => _ST_NAMES[m]).join(', ')}`
      : '⚠️ No media server working yet — start with Plex, Emby or Jellyfin.' },
  retriever: {
    what: 'Gives every song one genre family plus up to 3 styles and writes it into the music file.',
    next: '<b>▶ RUN ENGINE</b>, then <b>REVIEW &amp; WRITE</b>. Look over the ⚠ CONFLICT songs first.',
    live: s => { const g = s.genres; if (!g.decided) return '⚠️ Engine not run yet.';
      return (g.pending_new ? `⚠️ ${stN(g.pending_new)} new songs need genres — ▶ RUN ENGINE.` : '✅ Every song has been looked up.')
        + (g.conflicts ? ` · <a href="#" onclick="stGo('retriever','conflict');return false">${stN(g.conflicts)} conflicts to check</a>` : '')
        + (g.no_source ? ` · <a href="#" onclick="stGo('retriever','nogenre');return false">${stN(g.no_source)} songs no source knows</a> — set by hand or with a folder rule` : ''); } },
  groomer: {
    what: 'Scores every song 0–100 from its chart history and builds road-trip playlists from the biggest hits.',
    next: '<b>① SCAN</b> after new music arrives, then <b>② BUILD</b> and push to your media server.',
    live: s => s.scan.tracks
      ? `✅ ${stN(s.scan.tracks)} songs · ${stN(s.scan.charted)} charted${s.playlists.last ? ` · last playlist “${esc(s.playlists.last.name || 'playlist')}” ${stDate(s.playlists.last.at)}` : ''}`
      : '⚠️ Not scanned yet — click ① SCAN.' },
  sniffer: {
    what: 'Finds the big chart hits you don’t own and sends them to your downloader.',
    next: 'Pick <b>🏆 BIG HITS</b>, click <b>FIND BIG HITS I DON’T OWN</b>, then <b>SEARCH</b> a song and grab one result.',
    live: s => _thReady(s.gaps) },
  bloodhound: {
    what: 'Finds albums you’re missing — essential Billboard 200 albums or any artist’s full discography.',
    next: 'Pick <b>⭐ ESSENTIAL</b> (or type an artist), tick “Only albums I’m missing”, then <b>SEARCH</b> and grab.',
    live: s => _thReady(s.gaps) },
  tracker: {
    what: 'Slowly asks Radarr and Sonarr to search for missing movies and episodes in the background.',
    next: 'Turn it on once. After that just check the log now and then — it runs with the browser closed.',
    live: s => { const t = s.tracker;
      if (t.enabled) return `✅ On · ${stN(t.missing)} missing${t.last_grab ? ' · last grab ' + stDate(t.last_grab) : ''}`;
      return t.radarr || t.sonarr ? 'Off — click the TRACKER OFF button to start it.' : '⚠️ Needs Radarr or Sonarr connected in The Kennel.'; } },
  vet: {
    what: 'Database health, chart data sources and maintenance tools.',
    next: 'Click <b>💾 BACK UP NOW</b> before big changes. If a chart source shows <b>REFRESH FAILING</b>, try ↻ later.' },
};

let _thStatus = null, _thAt = 0;
async function _thFetch() {
  if (_thStatus && Date.now() - _thAt < 60000) return _thStatus;
  try {
    const r = await fetch(`${API}/api/start/status`, {headers: authHdrs()});
    if (r.ok) { _thStatus = await r.json(); _thAt = Date.now(); }
  } catch {}
  return _thStatus;
}
const _thHidden = t => { try { return localStorage.getItem('ch_hdr_hide_' + t) === '1'; } catch { return false; } };
function thToggle(t, hide) {
  try { localStorage.setItem('ch_hdr_hide_' + t, hide ? '1' : '0'); } catch {}
  tabHeader(t);
}
async function tabHeader(t) {
  const h = TAB_HELP[t], panel = document.getElementById('panel-' + t);
  if (!h || !panel) return;
  let el = document.getElementById('thdr-' + t);
  if (!el) { el = document.createElement('div'); el.id = 'thdr-' + t; panel.insertBefore(el, panel.firstChild); }
  if (_thHidden(t)) {
    el.className = 'tab-hdr th-min';
    el.innerHTML = `<button class="th-show" onclick="thToggle('${t}',false)">ⓘ WHAT THIS TAB DOES ▸</button>`;
    return;
  }
  el.className = 'tab-hdr';
  el.innerHTML = `<div class="th-row"><span class="th-k">WHAT THIS DOES</span><span class="th-v">${h.what}</span></div>
    <div class="th-row"><span class="th-k">DO THIS NEXT</span><span class="th-v">${h.next}</span></div>
    <div class="th-live" id="thl-${t}" style="display:none"></div>
    <button class="th-x" title="Hide tips (bring back with ⓘ)" onclick="thToggle('${t}',true)">✕</button>`;
  if (!h.live) return;
  const s = await _thFetch(), live = document.getElementById('thl-' + t);
  if (!s || !live) return;
  let txt = ''; try { txt = h.live(s) || ''; } catch {}
  live.innerHTML = txt; live.style.display = txt ? '' : 'none';
}

function advOpen(key, open = true) {
  const d = document.querySelector(`details[data-adv="${key}"]`);
  if (d && d.open !== !!open) d.open = !!open;
}
document.addEventListener('toggle', e => {
  const d = e.target;
  if (d && d.matches && d.matches('details[data-adv]'))
    try { localStorage.setItem('ch_adv_' + d.dataset.adv, d.open ? '1' : '0'); } catch {}
}, true);
(function _advRestore() {
  const run = () => document.querySelectorAll('details[data-adv]').forEach(d => {
    let v = null; try { v = localStorage.getItem('ch_adv_' + d.dataset.adv); } catch {}
    if (v !== null) d.open = v === '1';
  });
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', run); else run();
})();
