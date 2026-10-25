'use strict';
// ── HELP GUIDE ────────────────────────────────────────────────────────────────
const _HG_CONTENT = {
  start: `
    <div class="hg-section-title">🚀 GETTING STARTED</div>
    <div class="hg-section-sub">FIRST-TIME SETUP — READ THIS BEFORE ANYTHING ELSE</div>
    <div class="hg-tip">💡 If you're already up and running, skip to the tab you need in the menu on the left.</div>
    <div class="hg-block">
      <div class="hg-block-title">STEP 1 — GENERATE YOUR SECRET KEY</div>
      <p>ChartHound encrypts all your API tokens and passwords using a SECRET_KEY — a long random string that only you know. You must set this in your <strong>docker-compose.yml</strong> before starting the container. Run one of these commands in a terminal on your server and copy the result:</p>
      <br>
      <p><strong>Linux / Mac / Unraid / TrueNAS / Synology:</strong><br>
      <code style="background:var(--bg);padding:.2rem .5rem;border-radius:4px;color:var(--teal);font-size:.62rem">python3 -c "import secrets; print(secrets.token_hex(32))"</code></p>
      <br>
      <p><strong>Windows PowerShell:</strong><br>
      <code style="background:var(--bg);padding:.2rem .5rem;border-radius:4px;color:var(--teal);font-size:.62rem">python -c "import secrets; print(secrets.token_hex(32))"</code></p>
      <br>
      <p><strong>If Python is not installed:</strong><br>
      <code style="background:var(--bg);padding:.2rem .5rem;border-radius:4px;color:var(--teal);font-size:.62rem">docker run --rm python:3.12-slim python3 -c "import secrets; print(secrets.token_hex(32))"</code></p>
      <br>
      <p>Paste the result into your docker-compose.yml where it says <code style="color:var(--red)">CHANGE_ME_GENERATE_A_STRONG_RANDOM_KEY</code>. Treat it like a master password — never share it.</p>
    </div>
    <div class="hg-warn">⚠ If you change your SECRET_KEY after setup, all saved API keys and tokens will become unreadable and you'll need to re-enter them in The Kennel.</div>
    <div class="hg-block">
      <div class="hg-block-title">STEP 2 — SET YOUR ALLOWED ORIGINS</div>
      <p>In docker-compose.yml, set <strong>CH_ALLOWED_ORIGINS</strong> to the URLs you'll use to access ChartHound in your browser. This prevents other sites from sending commands to your server while you're logged in.</p>
      <br>
      <p>Find your server's LAN IP (run <code style="color:var(--teal)">hostname -I</code> on Linux) and set it like this:</p>
      <br>
      <p><code style="background:var(--bg);padding:.2rem .5rem;border-radius:4px;color:var(--teal);font-size:.62rem">CH_ALLOWED_ORIGINS=http://localhost:8585,http://192.168.1.50:8585</code></p>
      <br>
      <p>If you use a reverse proxy with HTTPS, add that URL too separated by a comma.</p>
    </div>
    <div class="hg-block">
      <div class="hg-block-title">STEP 3 — SET YOUR MUSIC LIBRARY PATH</div>
      <p>In docker-compose.yml, replace <code style="color:var(--red)">CHANGE_ME_PATH_TO_YOUR_MUSIC</code> with the full path to your music folder on your server. This appears twice — once for the <code>/music</code> mount and once for the direct path mount (required for FLAC tag writing).</p>
      <br>
      <p>Also set <strong>MEDIA_SERVER_MUSIC_PREFIX</strong> to the path your media server (Plex/Emby/Jellyfin) shows in file info for any track — everything before the artist folder. For example if Plex shows <code style="color:var(--teal)">/media/nas/Music/ABBA/Gold/track.flac</code> then your prefix is <code style="color:var(--teal)">/media/nas/Music</code>.</p>
    </div>
    <div class="hg-block">
      <div class="hg-block-title">STEP 4 — START CHARTHOUND</div>
      <p>From your ChartHound folder run:</p>
      <br>
      <p><code style="background:var(--bg);padding:.2rem .5rem;border-radius:4px;color:var(--teal);font-size:.62rem">docker compose up --build -d</code></p>
      <br>
      <p>Then open <code style="color:var(--teal)">http://YOUR-SERVER-IP:8585</code> in your browser. Create your admin account — this is a one-time action. After your account is created, registration is automatically locked. No checkbox to forget — ChartHound locks itself the moment the first user exists.</p>
    </div>
    <div class="hg-block">
      <div class="hg-block-title">STEP 5 — CONNECT YOUR SERVICES</div>
      <p>Head to <strong>The Kennel</strong> and connect your media server (Plex, Emby, or Jellyfin), Last.fm, and any other services you use. See The Kennel section of this guide for details on what each connection enables. Always click TEST after saving a connection.</p>
    </div>
    <div class="hg-block">
      <div class="hg-block-title">STEP 6 — TAG YOUR LIBRARY</div>
      <p>Open <strong>The Retriever</strong>, click <strong>▶ RUN ENGINE</strong>, then <strong>REVIEW &amp; WRITE</strong> to put genres into your files. Top Dogs uses these genres for playlists.</p>
    </div>
    <div class="hg-block">
      <div class="hg-block-title">YOUR LIBRARY GETS FASTER THE LONGER YOU USE IT</div>
      <p>ChartHound's database isn't just storage — it's a knowledge cache. Every scan teaches it something: track fingerprints, chart matches, MusicBrainz IDs, Discogs lookups, Last.fm popularity scores, skip cache entries, genre-resolution waterfall results. All of it accumulates and gets reused. The first scan of a large library is the slowest one you'll ever do. Every scan after that gets faster as the database fills out and fewer tracks need fresh lookups. That's why The Veterinarian has 💾 BACKUPS — protect it instead of ever starting over.</p>
    </div>
    <div class="hg-tip">💡 <strong>Recommended order:</strong> The Kennel → Top Dogs ① SCAN → The Retriever → Top Dogs ② BUILD → The Sniffer / The Bloodhound. 🧭 Start Here shows where you are. The Tracker is independent and can be set up any time.</div>
  `,
  kennel: `
    <div class="hg-section-title">🔑 THE KENNEL</div>
    <div class="hg-section-sub">CONNECTION VAULT & SETTINGS</div>
    <div class="hg-tip">💡 <strong>Start here.</strong> The Kennel is the first tab you should set up. Every other tab depends on connections configured here.</div>
    <div class="hg-block">
      <div class="hg-block-title">WHAT IT DOES</div>
      <p>The Kennel is ChartHound's encrypted connection vault. Every API key, token, and password you enter is encrypted with your SECRET_KEY before being stored — they are never returned to your browser in plain text. The Kennel also handles path translation between your media server's file paths and Docker's internal /music mount.</p>
    </div>
    <div class="hg-block">
      <div class="hg-block-title">CONNECTIONS TO SET UP</div>
      <ul>
        <li><strong>Plex / Emby / Jellyfin</strong> — Required for The Retriever and Top Dogs to scan your library. Connect at least one. Plex is recommended as the primary.</li>
        <li><strong>Last.fm</strong> — Strongly recommended. Powers genre metadata and popularity scoring for tracks not in the static Billboard database. Free, takes 30 seconds at last.fm/api/account/create.</li>
        <li><strong>Prowlarr</strong> — Required for The Sniffer and Bloodhound to search for albums to download.</li>
        <li><strong>qBittorrent / Deluge / Transmission</strong> — Required for The Sniffer and Bloodhound to send downloads.</li>
        <li><strong>Radarr / Sonarr</strong> — Required for The Tracker (movies and TV only — not music).</li>
        <li><strong>Discogs</strong> — Optional but recommended. Excellent genre data for classic rock, jazz, soul, and artists with sparse MusicBrainz tags.</li>
      </ul>
    </div>
    <div class="hg-block">
      <div class="hg-block-title">PATH TRANSLATOR</div>
      <p>This tells ChartHound how to convert your media server's file paths to Docker paths. Open any track in Plex/Emby/Jellyfin file info, copy everything before the artist folder — that is your Media Server Path Prefix. Leave Docker Music Mount as /music unless you changed the volume in docker-compose.yml.</p>
    </div>
    <div class="hg-block">
      <div class="hg-block-title">ENCRYPTED CREDENTIALS — HOW IT WORKS</div>
      <p>Every API key, token, and password is Fernet-encrypted (AES-128-CBC) before it touches the database. There is no "show password" button. There is no API endpoint that returns decrypted credentials. Keys are decrypted in memory only at the moment they're used, on the backend, never sent to the browser. If someone were to copy your charthound.db file, they'd get nothing usable without your SECRET_KEY.</p>
    </div>
    <div class="hg-tip">💡 <strong>Best practice:</strong> After saving any connection, always click TEST to confirm it is working before moving on.</div>
  `,
  retriever: `
    <div class="hg-section-title">🏷 THE RETRIEVER</div>
    <div class="hg-section-sub">GENRE ENGINE — WRITES GENRES TO YOUR PHYSICAL FILES</div>
    <div class="hg-block">
      <div class="hg-block-title">WHAT IT DOES</div>
      <p>The Genre Engine gives every song <strong>one family plus up to three styles</strong> (e.g. Rock; Classic Rock; Folk Rock) by a weighted vote of Discogs album styles and genres, MusicBrainz and Last.fm tags. Broad families win over obscure community tags, and subjective tags ("awesome", "favorites") are ignored. Lookups are made once per album and cached.</p>
    </div>
    <div class="hg-block">
      <div class="hg-block-title">HOW TO USE IT</div>
      <ul>
        <li><strong>▶ RUN ENGINE</strong> — looks up new albums and votes. The first run takes 1–3 hours for a large library; later runs only look up new music.</li>
        <li><strong>📋 REVIEW &amp; WRITE</strong> — page through the changes, filter by family, folder or conflicts, edit any song or a whole artist, then write selected or all. Your edits are saved as <strong>manual</strong> and never overwritten.</li>
        <li><strong>↻ RE-VOTE</strong> — re-decides from the cache in seconds with no lookups (use after changing folder rules).</li>
      </ul>
    </div>
    <div class="hg-block">
      <div class="hg-block-title">CONFLICT AND LITTLE DATA</div>
      <p><strong>⚠ CONFLICT</strong> means the sources disagree — worth a look. <strong>? LITTLE DATA</strong> means few sources know the song — usually fine.</p>
    </div>
    <div class="hg-block">
      <div class="hg-block-title">FOLDER RULES</div>
      <p>A folder can decide the family (e.g. FULL CHRISTIAN ALBUMS → Christian; the sources then only pick the style) or be skipped entirely. Click RE-VOTE after saving.</p>
    </div>
    <div class="hg-block">
      <div class="hg-block-title">CONNECTIONS</div>
      <ul>
        <li><strong>Discogs</strong> — strongly recommended; carries the most weight in the vote</li>
        <li><strong>Last.fm</strong> — strongly recommended; track and artist tags</li>
      </ul>
    </div>
    <div class="hg-tip">💡 Only the GENRE tag is written, always to the file first. Lidarr users: set "Tag Audio Files with Metadata" to Never, or Lidarr will overwrite these genres.</div>
  `,
  sniffer: `
    <div class="hg-section-title">📡 THE SNIFFER</div>
    <div class="hg-section-sub">CHART HIT FINDER — FIND WHAT YOU'RE MISSING & GRAB IT</div>
    <div class="hg-block">
      <div class="hg-block-title">WHAT IT DOES</div>
      <p>The Sniffer cross-references a master list of chart hits and popular tracks against your actual music library, then shows you what you own and what you're missing. For missing tracks, you can search Prowlarr and send albums directly to your download client in one click.</p>
      <p style="margin-top:.5rem">The library comparison uses your connected media server (Plex first, then Emby, then Jellyfin) for the most accurate results — it sees your entire library, not just folders you've scanned in The Retriever.</p>
    </div>
    <div class="hg-block">
      <div class="hg-block-title">DEDUPLICATION — ALWAYS THE BEST QUALITY</div>
      <p>When the Sniffer cross-references your library it doesn't count duplicates. It dedupes by track identity (artist + title, normalized) and — when you have multiple copies of the same song — recognizes you own it without double-counting. The same logic applies when building results: you only see one entry per track. A 320kbps MP3 wins over a 128kbps one. A FLAC wins over both. You always get the best version, silently.</p>
    </div>
    <div class="hg-block">
      <div class="hg-block-title">CONNECTIONS REQUIRED</div>
      <ul>
        <li><strong>Plex, Emby, or Jellyfin</strong> — to know what you already own (Plex is checked first)</li>
        <li><strong>Last.fm</strong> — powers the popularity scoring and genre tag data</li>
        <li><strong>Prowlarr + download client</strong> — to search for and grab missing albums</li>
      </ul>
    </div>
    <div class="hg-block">
      <div class="hg-block-title">TWO SEARCH MODES</div>
      <ul>
        <li><strong>Chart Gap Fill</strong> — Builds a master list of the top chart hits for your selected genres and decades. Uses real Billboard chart data plus Last.fm popularity scoring. Select genres + decade + notability tier, then click FIND MISSING TRACKS.</li>
        <li><strong>Trending</strong> — Shows currently trending tracks by genre from Last.fm. Good for discovering recent popular music you might be missing.</li>
      </ul>
    </div>
    <div class="hg-block">
      <div class="hg-block-title">NOTABILITY TIERS</div>
      <ul>
        <li><strong>Essential (top 250)</strong> — Only the biggest hits. #1s and long-charting classics.</li>
        <li><strong>Notable (top 1,000)</strong> — Solid chart performers. The recommended starting point.</li>
        <li><strong>Deep Cuts (top 5,000)</strong> — Includes album tracks and lesser-known chart entries.</li>
      </ul>
    </div>
    <div class="hg-tip">💡 <strong>Best practice:</strong> Start with all genres + Notable. Then narrow to one genre at a time to get deeper coverage per genre. Adding a decade filter gives you 1,000 of just that genre's best hits from that era.</div>
  `,
  groomer: `
    <div class="hg-section-title">🏆 TOP DOGS</div>
    <div class="hg-section-sub">ROAD-TRIP PLAYLISTS FROM THE BIGGEST HITS YOU OWN</div>
    <div class="hg-block">
      <div class="hg-block-title">WHAT IT DOES</div>
      <p>Top Dogs checks every song in your library against Billboard chart history, gives each charted song a <strong>Hit Score</strong> (0–100), and builds playlists of the songs most people know — then pushes them to Plex, Emby or Jellyfin, or exports M3U.</p>
    </div>
    <div class="hg-block">
      <div class="hg-block-title">THE 4 STEPS</div>
      <ul>
        <li><strong>1 · Scan</strong> — Media Server → pick server and library → Start Scan. The first scan takes a while; later scans only check new songs. Re-scan after adding music or importing chart data in The Veterinarian. The genre tree does <em>not</em> affect the scan.</li>
        <li><strong>2 · Choose songs</strong> — Genre tree (middle panel) and the HITS box: Min Hit Score, Decades, One-hit wonders.</li>
        <li><strong>3 · Build</strong> — Playlist Builder: Length, Playlists (days), Familiar hits, Artist spacing → 🚗 Build Playlist. Check the list in the results table.</li>
        <li><strong>4 · Push</strong> — ↑ PLEX / EMBY / JELLYFIN or ↓ M3U. Save your settings as a Preset to rebuild in one click.</li>
      </ul>
    </div>
    <div class="hg-block">
      <div class="hg-block-title">HIT SCORE</div>
      <p>Built mainly from real Billboard Hot 100 data — best peak, weeks on the chart and year-end rank — plus a bonus for strong placings on genre charts (country, R&amp;B, rock, AC…). Sources known to list almost everything as #1 count for very little.</p>
      <ul>
        <li><strong>85+</strong> — all-time smashes (Beat It, Like a Virgin)</li>
        <li><strong>70–84</strong> — big hits most people know</li>
        <li><strong>50–69</strong> — solid chart songs, genre-chart #1s</li>
        <li><strong>under 50</strong> — minor hits and estimates</li>
      </ul>
      <p>Hover a score to see its Hot 100 peak, weeks and year-end rank. Scores recalculate automatically after every scan.</p>
    </div>
    <div class="hg-block">
      <div class="hg-block-title">BUILDER OPTIONS</div>
      <ul>
        <li><strong>Length</strong> — per playlist. 8 hours ≈ 120 songs.</li>
        <li><strong>Playlists (days)</strong> — 1–7 separate playlists named "— Day 1", "— Day 2"…; no song repeats, and each day gets an equal share of the biggest hits.</li>
        <li><strong>Familiar hits</strong> — 90% means 9 of 10 songs come from your top scorers; the rest are lesser-known chart songs. Each build is a fresh random mix.</li>
        <li><strong>Artist spacing</strong> — songs before an artist can repeat. 12–15 for long drives.</li>
      </ul>
      <p>If the log says the build came up short, lower Min Hit Score or add a decade. A 4-day × 8-hour trip needs about 480 matching songs — the log shows how many candidates matched.</p>
    </div>
    <div class="hg-block">
      <div class="hg-block-title">RECIPES</div>
      <ul>
        <li><strong>80s Road Trip</strong> — 80s · Min 70 · 4 days × 8h · 90% familiar · spacing 15</li>
        <li><strong>Decades Mix</strong> — 70s + 80s + 90s · Min 75 · 4 days × 8h</li>
        <li><strong>One-Hit Wonders</strong> — 70s–90s · One-hit only · Min 50 · 1 day × 3h</li>
      </ul>
    </div>
    <div class="hg-block">
      <div class="hg-block-title">ADVANCED (COLLAPSED)</div>
      <ul>
        <li><strong>Results table filters</strong> — peak range, min weeks, confidence. Only filter the table; the builder ignores them.</li>
        <li><strong>Write chart data to COMMENT tags</strong> — writes chart info into your files during the scan. Not needed for playlists; leave off.</li>
      </ul>
    </div>
    <div class="hg-block">
      <div class="hg-block-title">PUSHING SAFELY</div>
      <p>Songs are sent in exact playlist order. A song is only added if both title and artist match — if it can't be found on the server it's skipped and listed in the log, never replaced with a different song. Pushing replaces any playlist with the same name.</p>
    </div>
    <div class="hg-tip">💡 <strong>Best practice:</strong> Run The Retriever first so files have genre tags — genre filtering gets much more accurate. Scan with Media Server mode so pushes use exact server IDs.</div>
  `,
  bloodhound: `
    <div class="hg-section-title">🔍 THE BLOODHOUND</div>
    <div class="hg-section-sub">ALBUM HUNTER — HUNT EVERY ALBUM BY ANY ARTIST</div>
    <div class="hg-block">
      <div class="hg-block-title">WHAT IT DOES</div>
      <p>The Bloodhound searches MusicBrainz for albums and shows you what you own vs. what you're missing. When you find something missing, search Prowlarr and grab it to your download client without leaving the page.</p>
    </div>
    <div class="hg-block">
      <div class="hg-block-title">CONNECTIONS REQUIRED</div>
      <ul>
        <li><strong>Plex, Emby, or Jellyfin</strong> — to cross-reference what you already own</li>
        <li><strong>Prowlarr + download client</strong> — to search for and grab missing albums</li>
      </ul>
    </div>
    <div class="hg-block">
      <div class="hg-block-title">FOUR SEARCH MODES</div>
      <ul>
        <li><strong>Artist</strong> — Search by artist name, then browse their full discography. Filter by Albums, Compilations, Singles, or All. Results are sortable by Artist, Title, Year, or Status (Owned/Missing).</li>
        <li><strong>Album</strong> — Search for any album by name across all of MusicBrainz.</li>
        <li><strong>Comps</strong> — 31 preset compilation series (Now That's What I Call Music, WOW Hits, Grammy Nominees, etc.) plus a custom search field.</li>
        <li><strong>Genre</strong> — Browse by primary genre (Rock, Pop, Country, R&amp;B, Hip-Hop, Electronic, Jazz, Blues, Metal, Alternative, Folk, Classical, CCM/Gospel). Selecting a genre automatically includes all its sub-genres in the search.</li>
      </ul>
    </div>
    <div class="hg-tip">💡 <strong>Best practice:</strong> Use Artist mode to do a complete discography audit — find an artist you love, filter to Albums only, and see at a glance what you're missing. Use Genre mode to discover albums in a genre you want to expand.</div>
  `,
  tracker: `
    <div class="hg-section-title">🎯 THE TRACKER</div>
    <div class="hg-section-sub">MISSING MEDIA HUNTER — RADARR & SONARR AUTOMATIC SEARCH</div>
    <div class="hg-block">
      <div class="hg-block-title">WHAT IT DOES</div>
      <p>The Tracker monitors your Radarr and Sonarr libraries for missing movies and TV episodes, then automatically fires search commands through their APIs. ChartHound doesn't touch Prowlarr directly — it tells Radarr and Sonarr to search, and they handle everything from there exactly as they normally would.</p>
      <p style="margin-top:.5rem"><strong>Note: The Tracker is for movies and TV only — not music.</strong> Music discovery is handled by The Sniffer and The Bloodhound.</p>
    </div>
    <div class="hg-block">
      <div class="hg-block-title">CONNECTIONS REQUIRED</div>
      <ul>
        <li><strong>Radarr</strong> — for missing movie detection and search</li>
        <li><strong>Sonarr</strong> — for missing TV episode detection and search</li>
        <li>Prowlarr is handled internally by Radarr/Sonarr — no direct connection needed here</li>
      </ul>
    </div>
    <div class="hg-block">
      <div class="hg-block-title">IMPORTANT — DEFAULT OFF</div>
      <p>The Tracker is disabled by default and must be explicitly enabled from the Tracker page. This is intentional — automatic searching can burn through indexer request limits quickly if misconfigured. Read the settings before enabling.</p>
    </div>
    <div class="hg-block">
      <div class="hg-block-title">KEY SETTINGS</div>
      <ul>
        <li><strong>Daily cap</strong> — limits total searches per day to prevent overloading indexers</li>
        <li><strong>Cooldown</strong> — won't re-search an unfindable item until the cooldown expires (default 7 days)</li>
        <li><strong>Smart TV ordering</strong> — searches earliest missing season first; won't look for season 3 if season 2 is still missing</li>
        <li><strong>Season search</strong> — when an entire season is missing, fires a single SeasonSearch instead of individual episode searches</li>
      </ul>
    </div>
    <div class="hg-block">
      <div class="hg-block-title">REQUEST JITTER</div>
      <p>When the Tracker fires search commands, it doesn't blast them out at predictable intervals. Each search has a small randomized delay added before it goes out, so the request pattern looks more like a human clicking buttons than a bot. This keeps your indexers happier and reduces the chance of getting rate-limited mid-batch.</p>
    </div>
    <div class="hg-tip">💡 <strong>Best practice:</strong> Start with the Moderate preset (60 searches/day) and a 7-day cooldown. Monitor the activity log for a few days before increasing search limits.</div>
  `,
  vet: `
    <div class="hg-section-title">🩺 THE VETERINARIAN</div>
    <div class="hg-section-sub">DATABASE HEALTH & ADMIN TOOLS</div>
    <div class="hg-block">
      <div class="hg-block-title">WHAT IT DOES</div>
      <p>The Veterinarian gives you visibility into ChartHound's internal database — track counts, artist counts, album counts, skip cache statistics, dynamic vs. static DB sizes — and provides maintenance tools to keep things running cleanly.</p>
    </div>
    <div class="hg-block">
      <div class="hg-block-title">CONNECTIONS REQUIRED</div>
      <p>None — The Veterinarian operates on ChartHound's local database only.</p>
    </div>
    <div class="hg-block">
      <div class="hg-block-title">MAINTENANCE TOOLS</div>
      <ul>
        <li><strong>VACUUM DATABASE</strong> — Reclaims unused space in the SQLite database. Safe to run any time. Does not delete any data.</li>
        <li><strong>INTEGRITY CHECK</strong> — Verifies the database has no corruption. If this fails, restore your newest backup (README → Troubleshooting → Restore a backup).</li>
        <li><strong>RESET SKIP CACHE</strong> — Forces Top Dogs to re-scan all tracks on next run, ignoring previously cached results. Use if you've changed your library significantly.</li>
        <li><strong>💾 BACK UP NOW</strong> — Saves a complete copy of the database to /data/backups (newest 5 kept). Use ⬇ to download one and keep it off the server.</li>
      </ul>
    </div>
    <div class="hg-block">
      <div class="hg-block-title">DEBUG CONSOLE</div>
      <p>An in-tab debug console (off by default, 1,000-line cap) shows real-time backend log output. Turn it on when troubleshooting a stuck scan. Turn it off afterward — debug output can pile up fast, generating thousands of lines per minute during large scans. The cap and opt-in behavior keep the UI snappy and the noise out of your way unless you explicitly need it.</p>
    </div>
    <div class="hg-block">
      <div class="hg-block-title">YOUR DATABASE IS A KNOWLEDGE CACHE</div>
      <p>ChartHound's database accumulates valuable knowledge over time — chart matches, MusicBrainz IDs, Last.fm scores, skip cache entries, genre-resolution results. The first scan of a large library is the slowest you'll ever do. Every scan after gets faster as fewer tracks need fresh lookups. A database that's been growing for weeks is a feature, not clutter.</p>
    </div>
    <div class="hg-warn">⚠ <strong>If the database is ever damaged</strong>, restore the newest backup rather than starting over — the steps are in the README under Troubleshooting → Restore a backup. Your music files are never affected either way.</div>
    <div class="hg-tip">💡 <strong>Best practice:</strong> Run VACUUM once a month. Run INTEGRITY CHECK if ChartHound ever behaves unexpectedly. Back up before big changes, and download a backup now and then.</div>
  `
};

function openHelpGuide(section) {
  document.getElementById('helpGuideOverlay').style.display = 'flex';
  hgShow(section || 'start');
}

function closeHelpGuide() {
  document.getElementById('helpGuideOverlay').style.display = 'none';
}

function hgShow(section) {
  document.querySelectorAll('.hg-toc-btn').forEach(b => b.classList.remove('active'));
  const btn = document.getElementById('hg-toc-' + section);
  if (btn) btn.classList.add('active');
  const content = _HG_CONTENT[section] || '<p style="font-family:var(--mono);font-size:.62rem;color:var(--muted)">Section not found.</p>';
  document.getElementById('hg-content').innerHTML = content;
}

// Close on backdrop click
document.getElementById('helpGuideOverlay').addEventListener('click', function(e) {
  if (e.target === this) closeHelpGuide();
});

// ESC key closes guide
document.addEventListener('keydown', function(e) {
  if (e.key === 'Escape') closeHelpGuide();
});


