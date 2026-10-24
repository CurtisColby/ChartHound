'use strict';
const API = '';
let authToken = localStorage.getItem('ch_token') || '';
let currentUser = localStorage.getItem('ch_user') || '';
let authMode = 'login';
let heartbeatTimer = null;
let scanJobId = null;
let scanPollTimer = null;
let currentMode = 'preview';
let currentScope = 'subfolder';
let selectedTrackIds = new Set();
let scanPaused = false;
let browseCurrentPath = '';

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
  if (scanPollTimer) clearInterval(scanPollTimer);
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
    showApp(hd.secret_key_placeholder);
  } catch { errEl.textContent = 'Cannot reach server. Is Docker running?'; }
}
document.addEventListener('keydown', e => {
  if (e.key === 'Enter' && !document.getElementById('loginScreen').classList.contains('hidden')) doLogin();
});
function doLogout() {
  clearAuth();
  if (heartbeatTimer) clearInterval(heartbeatTimer);
  if (scanPollTimer) clearInterval(scanPollTimer);
  document.getElementById('appHeader').style.display = 'none';
  document.getElementById('appShell').style.display = 'none';
  document.getElementById('secretKeyBanner').classList.remove('show');
  document.getElementById('loginScreen').classList.remove('hidden');
  document.getElementById('loginPass').value = '';
}
async function showApp(isPlaceholderKey = false) {
  document.getElementById('loginScreen').classList.add('hidden');
  document.getElementById('appHeader').style.display = 'flex';
  document.getElementById('appShell').style.display = 'flex';
  document.getElementById('userBadge').textContent = `${currentUser.toUpperCase()} · LOGOUT`;
  if (isPlaceholderKey) document.getElementById('secretKeyBanner').classList.add('show');
  await loadAllStatuses();
  await loadPathSettings();
  startHeartbeat();
  toast(`Welcome back, ${currentUser}. Head to The Kennel to connect your services.`, 'ok');
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
  if (name === 'retriever') { checkRetrieverBanners(); loadCustomTags(); retInjectScrollNav(); }
  if (name === 'groomer') grmInit();
  if (name === 'sniffer') snfInit();
  if (name === 'bloodhound') bhInit();
  if (name === 'tracker') trkInit();
  if (name === 'vet') vetInit();
}
function checkRetrieverBanners() {
  const b = document.getElementById('retrieverLfmBanner');
  if (b) b.style.display = connState.lastfm ? 'none' : 'flex';
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

// RETRIEVER
function setScope(scope, el) {
  currentScope = 'subfolder';
}

function setMode(mode, el) {
  currentMode = mode;
  document.querySelectorAll('.mode-btn').forEach(t => t.classList.remove('active'));
  el.classList.add('active');
  const desc = {
    preview:
      '🌐 GOES ONLINE — fetches proposed tags from MusicBrainz, Last.fm, Deezer & more in chunks. ' +
      'Results appear live as they come in. You review and approve before anything writes to your files. ' +
      'You can Override any individual track before writing.\n\n' +
      '⚠️ WARNING: If you select a root folder with many subfolders, Preview will process ALL of them at once. ' +
      'This can take hours on large libraries. Select one or two specific folders at a time for best results.',
    album_tagger:
      '⭐ RECOMMENDED — This is the preferred method for tagging your library. ' +
      'Select a subfolder, then click READ FROM FILE to load album cards. ' +
      'One card per folder. Choose 🔍 LOOKUP (fetches genre from internet for that album), ' +
      '✏️ OVERRIDE (set all tags yourself), or ✓ KEEP (tags are fine — card disappears). ' +
      'After Lookup, click 🔍 WRITE ALL LOOKUPS to write all found genres at once. ' +
      'Use the checkboxes for bulk override — select multiple cards and set genres/moods for all at once.',
    autopilot:
      '⚡ AUTO-PILOT — goes online and writes metadata automatically in the background. ' +
      'Skips tracks you have already manually verified. Low-confidence results go to a Review Queue instead of writing. ' +
      'Test on a small subfolder first.\n\n' +
      '⚠️ WARNING: Auto-Pilot can take hours on large libraries and you accept whatever Last.fm tags it assigns. ' +
      'Review results carefully. Album Tagger gives you more control.',
    genre_scout:
      '📂 PHYSICAL FILES — Reads actual Mutagen tags from your files. No database, no internet. ' +
      'Scans 500 files at a time and pauses so you can tag and write before continuing. ' +
      'Use BY ARTIST or BY ALBUM view for card-based editing with 💡 SUGGEST to get genre recommendations.',
  };
  var descText = desc[mode] || desc.preview;
  var descEl = document.getElementById('modeDesc');
  descText = descText.replace(/⚠️ WARNING:/g, '<br><br><span style="color:var(--crimson);font-weight:bold">⚠️ WARNING:</span>');
  descText = descText.replace(/⭐ RECOMMENDED/g, '<span style="color:var(--green);font-weight:bold">⭐ RECOMMENDED</span>');
  descEl.innerHTML = descText;
  document.getElementById('albumTaggerSection').style.display = mode === 'album_tagger' ? 'block' : 'none';
  document.getElementById('previewSection').style.display = mode === 'preview' ? 'block' : 'none';
  document.getElementById('genreScoutSection').style.display = mode === 'genre_scout' ? 'block' : 'none';
  const startBtn  = document.getElementById('btnStartScan');
  const scoutBtn  = document.getElementById('btnScoutScan');
  const batchSect = document.getElementById('batchSizeSection');
  if (startBtn)  startBtn.style.display  = (mode === 'album_tagger' || mode === 'genre_scout') ? 'none' : '';
  if (scoutBtn)  scoutBtn.style.display  = mode === 'genre_scout' ? '' : 'none';
  if (batchSect) batchSect.style.display = mode === 'genre_scout' ? 'none' : '';
  if (mode === 'genre_scout') _scoutPopulateGenreOpts();
}
async function startScan() {
  const body = {scope:currentScope, mode:currentMode, chunk_size:parseInt(document.getElementById('chunkSlider').value)};
  if (currentScope === 'subfolder') {
    if (_subfolderQueue.length > 0) {
      body.subfolders = _subfolderQueue;
      body.scope = 'subfolder';
    } else {
      const sel = document.getElementById('subfolderPath');
      const subfolder = sel ? sel.value.trim() : '';
      if (!subfolder || subfolder === '__up__' || subfolder === '') {
        toast('Select a subfolder first using the ⟳ button.', 'warn');
        return;
      }
      body.subfolder = subfolder;
    }
  }
  addLog('Starting scan…', 'info');
  addLog('⟳ Fetching metadata from MusicBrainz · Last.fm · Discogs · Deezer · ListenBrainz — please wait…', 'info');
  // Clear previous scan results
  document.getElementById('previewTableBody').innerHTML = '';
  document.getElementById('previewSection').style.display = 'none';
  const _pzm = document.getElementById('previewZeroMatch'); if (_pzm) _pzm.style.display = 'none';
  selectedTrackIds.clear();
  _retAllTracks = [];
  document.getElementById('selectedCount').textContent = '0 selected';
  document.getElementById('btnStartScan').disabled = true;
  document.getElementById('btnPauseScan').disabled = false;
  document.getElementById('btnStopScan').disabled = false;
  document.getElementById('scanStatusTitle').textContent = 'SCANNING…';
  document.getElementById('scanStatusSub').textContent = 'Fetching metadata — please wait…';
  document.getElementById('vinylIcon').classList.add('spinning');
  document.getElementById('scanProgressWrap').classList.add('bar-pulsing');
  try {
    const r = await fetch(`${API}/api/retriever/scan/start`, {method:'POST', headers:authHdrs(), body:JSON.stringify(body)});
    const d = await r.json();
    if (!r.ok) throw new Error(d.detail || 'Failed to start scan');
    scanJobId = d.job_id;
    scanPaused = false;
    document.getElementById('btnPauseScan').textContent = '⏸ PAUSE';
    addLog(`Job ${scanJobId} started — scanning ${d.scan_path} in ${currentMode} mode`, 'ok');
    if (d.verified_skipped > 0) {
      addLog(`⚠ ${d.verified_skipped} track(s) skipped — manually verified. To re-tag these, click Clear Metadata Cache and rescan.`, 'warn');
    }
    startScanPolling();
  } catch(e) {
    addLog(`✗ ${e.message}`, 'err');
    document.getElementById('btnStartScan').disabled = false;
    document.getElementById('btnPauseScan').disabled = true;
    document.getElementById('btnStopScan').disabled = true;
    document.getElementById('vinylIcon').classList.remove('spinning');
  }
}
async function pauseScan() {
  if (scanPaused) {
    // RESUME
    const r = await fetch(`${API}/api/retriever/scan/resume`, {method:'POST', headers:authHdrs()});
    if (r.ok) {
      scanPaused = false;
      document.getElementById('btnPauseScan').textContent = '⏸ PAUSE';
      addLog('Scan resumed.', 'ok');
      document.getElementById('scanStatusTitle').textContent = 'SCANNING…';
      startScanPolling();
    }
  } else {
    // PAUSE
    await fetch(`${API}/api/retriever/scan/pause`, {method:'POST', headers:authHdrs()});
    scanPaused = true;
    document.getElementById('btnPauseScan').textContent = '▶ RESUME';
    addLog('Scan paused. No files were corrupted.', 'warn');
    document.getElementById('scanStatusTitle').textContent = 'PAUSED';
  }
}
async function stopScan() {
  if (scanPollTimer) { clearInterval(scanPollTimer); scanPollTimer = null; }
  // Always call stop endpoint — works even if scanJobId is null (kills any running job)
  try {
    await fetch(`${API}/api/retriever/scan/stop`, {method:'POST', headers:authHdrs()});
  } catch(e) { addLog('Stop request failed: ' + e.message, 'err'); }
  scanPaused = false;
  scanJobId = null;
  document.getElementById('btnPauseScan').textContent = '⏸ PAUSE';
  addLog('Scan stopped.', 'warn');
  document.getElementById('btnStartScan').disabled = false;
  document.getElementById('btnPauseScan').disabled = true;
  document.getElementById('btnStopScan').disabled = true;
  document.getElementById('scanStatusTitle').textContent = 'STOPPED';
  document.getElementById('vinylIcon').classList.remove('spinning');
}
function startScanPolling() {
  if (scanPollTimer) clearInterval(scanPollTimer);
  scanPollTimer = setInterval(async () => {
    if (!scanJobId) return;
    try {
      const r = await fetch(`${API}/api/retriever/scan/job/${scanJobId}`, {headers:authHdrs()});
      if (!r.ok) return;
      const job = await r.json();
      updateScanProgress(job);
      // Incremental preview refresh during scan
      if (currentMode === 'preview' && job.processed > 0) loadPreviewResults();
      if (job.status === 'done') {
        clearInterval(scanPollTimer); scanPollTimer = null;
        addLog(`✓ Scan complete — ${job.processed} processed, ${job.failed} unmatched`, 'ok');
        document.getElementById('btnStartScan').disabled = false;
        document.getElementById('btnPauseScan').disabled = true;
        document.getElementById('btnStopScan').disabled = true;
        document.getElementById('scanStatusTitle').textContent = 'COMPLETE';
        document.getElementById('vinylIcon').classList.remove('spinning');
        if (currentMode === 'preview') loadPreviewResults();
      }
    } catch {}
  }, 3000);
}
function updateScanProgress(job) {
  const total = job.total_tracks || 1;
  const pct = Math.round((job.processed / total) * 100);
  document.getElementById('scanProgressBar').style.width = pct + '%';
  if (pct > 0) document.getElementById('scanProgressWrap').classList.remove('bar-pulsing');
  document.getElementById('statTotal').textContent = job.total_tracks || 0;
  document.getElementById('statProcessed').textContent = job.processed || 0;
  document.getElementById('statFailed').textContent = job.failed || 0;
  document.getElementById('scanStatusSub').textContent = `${pct}% complete`;
}
async function loadPreviewResults() {
  try {
    const r = await fetch(`${API}/api/retriever/scan/preview/${scanJobId}?limit=100`, {headers:authHdrs()});
    if (!r.ok) return;
    const tracks = await r.json();
    const zeroEl = document.getElementById('previewZeroMatch');
    if (!tracks.length) {
      if (zeroEl) zeroEl.style.display = 'block';
      return;
    }
    if (zeroEl) zeroEl.style.display = 'none';
    document.getElementById('previewSection').style.display = 'block';
    _retAllTracks = tracks;
    retRenderTable(tracks);
    document.getElementById('statMatched').textContent = tracks.length;
    if (currentMode === 'album_tagger') loadAlbumTagger();
  } catch(e) { addLog(`Preview load error: ${e.message}`, 'err'); }
}
function toggleTrack(id, checked) {
  if (checked) selectedTrackIds.add(id); else selectedTrackIds.delete(id);
  document.getElementById('selectedCount').textContent = `${selectedTrackIds.size} selected`;
}
function selectAll(checked) {
  document.querySelectorAll('#previewTableBody input[type="checkbox"]').forEach(cb => {
    cb.checked = checked;
    const match = cb.getAttribute('onchange').match(/\d+/);
    if (match) { const id = parseInt(match[0]); if (checked) selectedTrackIds.add(id); else selectedTrackIds.delete(id); }
  });
  document.getElementById('selectedCount').textContent = `${selectedTrackIds.size} selected`;
}

function retSelectAll(checked) { selectAll(checked); }

// ── Retriever sortable columns ────────────────────────────────────────────────
let _retSortKey = 'artist';
let _retSortAsc = true;
let _retAllTracks = [];

function retFilter(q) {
  const lq = q.toLowerCase();
  document.querySelectorAll('#previewTableBody tr[data-id]').forEach(tr => {
    tr.style.display = (tr.dataset.text || '').includes(lq) ? '' : 'none';
  });
}

function retRenderTable(tracks) {
  const _sortFns = {
    artist:   t => (t.tag_artist || '').toLowerCase(),
    title:    t => (t.title || '').toLowerCase(),
    album:    t => (t.tag_album || '').toLowerCase(),
    current:  t => (t.current_genre || '').toLowerCase(),
    proposed: t => ([t.genre_1,t.genre_2,t.genre_3].filter(Boolean).join(',') || '').toLowerCase(),
    year:     t => parseInt(t.year) || 9999,
    conf:     t => t.confidence || '',
  };
  const fn = _sortFns[_retSortKey] || _sortFns.artist;
  tracks = [...tracks].sort((a,b) => {
    const av = fn(a), bv = fn(b);
    if (av < bv) return _retSortAsc ? -1 : 1;
    if (av > bv) return _retSortAsc ? 1 : -1;
    return 0;
  });
  const tbody = document.getElementById('previewTableBody');
  tbody.innerHTML = '';
  tracks.forEach(t => {
    const proposed = [t.genre_1,t.genre_2,t.genre_3].filter(Boolean).join(', ') || '—';
    const current  = t.current_genre || '—';
    const conf     = t.confidence || 'low';
    const confColor = conf === 'high' ? 'var(--green)' : conf === 'medium' ? 'var(--amber)' : 'var(--muted2)';
    const confDot   = `<span style="display:inline-block;width:7px;height:7px;border-radius:50%;background:${confColor};margin-right:.35rem;vertical-align:middle"></span>`;
    const changed = proposed !== '—' && current !== '—' && current !== proposed;
    const moodList = [t.mood_1,t.mood_2,t.mood_3].filter(Boolean);
    const moodHTML = moodList.length
      ? moodList.map(m=>`<span style="display:inline-block;font-size:.5rem;padding:.1rem .35rem;border-radius:10px;background:rgba(192,132,252,.12);border:1px solid rgba(192,132,252,.3);color:var(--purple);margin:.1rem .1rem 0 0;white-space:nowrap">${esc(m)}</span>`).join('')
      : '<span style="color:var(--muted2)">—</span>';
    const row = document.createElement('tr');
    row.dataset.id    = t.track_id;
    row.dataset.album = (t.tag_album || '').toLowerCase();
    row.dataset.text  = `${(t.tag_artist||'').toLowerCase()} ${(t.title||'').toLowerCase()} ${(t.tag_album||'').toLowerCase()} ${proposed.toLowerCase()} ${current.toLowerCase()}`;
    row.innerHTML = `
      <td><input type="checkbox" onchange="toggleTrack(${t.track_id},this.checked)" style="accent-color:var(--teal)"></td>
      <td style="color:var(--muted)">${esc(t.tag_artist||'')}</td>
      <td style="color:var(--muted)">${esc(t.title||'')}</td>
      <td style="color:var(--muted2);font-size:.58rem">${esc(t.tag_album||'—')}</td>
      <td style="color:var(--muted);font-size:.7rem">${esc(current)}</td>
      <td class="tag-proposed" style="${changed?'color:var(--teal)':''}">${esc(proposed)}</td>
      <td>${moodHTML}</td>
      <td class="tag-proposed">${t.year||'—'}</td>
      <td>${confDot}<span style="color:${confColor};font-size:.58rem">${conf}</span></td>`;
    tbody.appendChild(row);
  });
}

function retSort(key) {
  if (_retSortKey === key) { _retSortAsc = !_retSortAsc; }
  else { _retSortKey = key; _retSortAsc = (key === 'artist' || key === 'title' || key === 'album'); }
  ['artist','title','album','current','proposed','year','conf'].forEach(k => {
    const el = document.getElementById('ret-sort-' + k);
    if (el) el.textContent = k === _retSortKey ? (_retSortAsc ? ' ▲' : ' ▼') : '';
  });
  retRenderTable(_retAllTracks);
}

function selectAllByAlbum() {
  const albums = new Set();
  document.querySelectorAll('#previewTableBody tr[data-id]').forEach(tr => {
    const cb = tr.querySelector('input[type="checkbox"]');
    if (cb && cb.checked) albums.add(tr.dataset.album);
  });
  if (!albums.size) { toast('Select at least one track first, then use this to select the whole album.', 'warn'); return; }
  document.querySelectorAll('#previewTableBody tr[data-id]').forEach(tr => {
    if (albums.has(tr.dataset.album)) {
      const cb = tr.querySelector('input[type="checkbox"]');
      if (cb && !cb.checked) {
        cb.checked = true;
        const id = parseInt(tr.dataset.id);
        if (id) selectedTrackIds.add(id);
      }
    }
  });
  document.getElementById('selectedCount').textContent = `${selectedTrackIds.size} selected`;
  toast(`Selected all tracks in ${albums.size} album(s).`, 'ok');
}

function pvwKeepSelected() {
  if (!selectedTrackIds.size) { toast('Select at least one track first.', 'warn'); return; }
  let removed = 0;
  document.querySelectorAll('#previewTableBody tr[data-id]').forEach(tr => {
    const id = parseInt(tr.dataset.id);
    if (selectedTrackIds.has(id)) {
      selectedTrackIds.delete(id);
      tr.remove();
      removed++;
    }
  });
  document.getElementById('selectedCount').textContent = `${selectedTrackIds.size} selected`;
  toast(`Kept ${removed} track(s) as-is — removed from list.`, 'ok');
}

