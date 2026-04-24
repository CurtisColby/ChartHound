# ChartHound — Current State Summary
**Last Updated:** April 23, 2026 — Milestone 8 Complete · Tracker Hardened · UI Rollup Pass

---

## KEY PATHS

| Item | Path |
|---|---|
| Working container folder | `/media/colby/NAS1/charthound` |
| Downloaded files from Claude | `/home/colby/Downloads` |
| GitHub clone | `~/ChartHound` |
| Music library (host) | `/media/colby/NAS1/MUSIC TAGGED` |
| Music library (Docker) | `/music` |
| Dynamic DB (Docker volume) | `/var/lib/docker/volumes/charthound_charthound_data/_data/charthound.db` |
| Static DB (host) | `/media/colby/NAS1/charthound/data/charthound_static.db` |
| Static DB (Docker) | `/data/charthound_static.db` (read-only bind mount) |
| Sniffer download folder | `/media/colby/NAS1/MUSIC TAGGED/ChartHound` |
| Whitburn source files | `/media/colby/NAS2/Whitburn Files/` |

---

## EMERGENCY COMMANDS

```bash
# Stop stuck Groomer scan
docker exec charthound python3 -c "
import sqlite3
conn = sqlite3.connect('/data/charthound.db')
conn.execute(\"UPDATE scan_jobs SET status='stopped' WHERE status='running'\")
conn.commit(); conn.close()"

# Wipe scan results only (keeps chart_reference data)
docker exec charthound python3 -c "
import sqlite3
conn = sqlite3.connect('/data/charthound.db')
conn.execute('DELETE FROM chart_data')
conn.execute('DELETE FROM tracks')
conn.commit(); conn.close()"

# Fix zero peak_position on CCM estimate rows (run after first CCM scan)
docker exec charthound python3 -c "
import sqlite3, random
conn = sqlite3.connect('/data/charthound.db')
rows = conn.execute('SELECT chart_id, listener_count FROM chart_data WHERE peak_position=0').fetchall()
print(f'Fixing {len(rows)} rows...')
for chart_id, listeners in rows:
    listeners = listeners or 0
    if listeners >= 100000:   peak = random.randint(1, 10)
    elif listeners >= 40000:  peak = random.randint(11, 20)
    elif listeners >= 15000:  peak = random.randint(21, 40)
    else:                     peak = random.randint(41, 100)
    stars = 5 if listeners>=100000 else 4 if listeners>=40000 else 3 if listeners>=15000 else 2
    conn.execute('UPDATE chart_data SET peak_position=?, star_rating=? WHERE chart_id=?', (peak, stars, chart_id))
conn.commit(); conn.close()
print('Done')"

# Check skip cache status
docker exec charthound python3 -c "
import sqlite3
conn = sqlite3.connect('/data/charthound.db')
rows = conn.execute('SELECT chart_status, COUNT(*) FROM tracks GROUP BY chart_status').fetchall()
for r in rows:
    print(f'{r[0] or \"unchecked\"}: {r[1]}')
conn.close()"

# Reset skip cache (forces full re-scan)
docker exec charthound python3 -c "
import sqlite3
conn = sqlite3.connect('/data/charthound.db')
conn.execute('UPDATE tracks SET chart_status=NULL, chart_last_checked=NULL')
conn.commit()
print('Skip cache reset')
conn.close()"

# Check tracker status
docker exec charthound python3 -c "
import sqlite3
conn = sqlite3.connect('/data/charthound.db')
print('tracker_items:', conn.execute('SELECT COUNT(*) FROM tracker_items').fetchone()[0])
print('by status:')
for r in conn.execute('SELECT status, COUNT(*) FROM tracker_items GROUP BY status').fetchall():
    print(f'  {r[0]}: {r[1]}')
print('tracker_log:', conn.execute('SELECT COUNT(*) FROM tracker_log').fetchone()[0])
conn.close()"

# Reset tracker (clear all tracked items and log, does NOT disable it)
docker exec charthound python3 -c "
import sqlite3
conn = sqlite3.connect('/data/charthound.db')
conn.execute('DELETE FROM tracker_items')
conn.execute('DELETE FROM tracker_log')
conn.commit(); conn.close()
print('Tracker tables cleared')"

# Force-disable tracker if hunt loop is misbehaving
docker exec charthound python3 -c "
import sqlite3
conn = sqlite3.connect('/data/charthound.db')
conn.execute(\"INSERT OR REPLACE INTO app_settings (key, value, updated_at) VALUES ('tracker_enabled', 'false', datetime('now'))\")
conn.commit(); conn.close()
print('Tracker disabled — restart container to stop hunt loop')"

# Reset password
docker exec charthound python3 -c "
import bcrypt, asyncio, aiosqlite
async def reset():
    hashed = bcrypt.hashpw(b'ChartHound1!', bcrypt.gensalt()).decode()
    async with aiosqlite.connect('/data/charthound.db') as db:
        await db.execute('UPDATE users SET password_hash=? WHERE username=?', (hashed, 'Colby'))
        await db.commit()
asyncio.run(reset())"

# Deploy all files (M8+)
docker cp /home/colby/Downloads/retriever.py charthound:/app/app/routers/retriever.py && \
docker cp /home/colby/Downloads/groomer.py charthound:/app/app/routers/groomer.py && \
docker cp /home/colby/Downloads/kennel.py charthound:/app/app/routers/kennel.py && \
docker cp /home/colby/Downloads/sniffer.py charthound:/app/app/routers/sniffer.py && \
docker cp /home/colby/Downloads/bloodhound.py charthound:/app/app/routers/bloodhound.py && \
docker cp /home/colby/Downloads/tracker.py charthound:/app/app/routers/tracker.py && \
docker cp /home/colby/Downloads/main.py charthound:/app/app/main.py && \
docker cp /home/colby/Downloads/index.html charthound:/app/frontend/index.html && \
docker exec charthound find /app -name "*.pyc" -delete && \
docker restart charthound

# CRITICAL: If container is crash-looping, stop first then cp
docker stop charthound && \
docker cp /home/colby/Downloads/tracker.py charthound:/app/app/routers/tracker.py && \
docker cp /home/colby/Downloads/main.py charthound:/app/app/main.py && \
docker start charthound

# Deploy index.html only (no restart needed)
docker cp /home/colby/Downloads/index.html charthound:/app/frontend/index.html

# Diagnostic
docker exec charthound python3 -c "
import sqlite3
conn = sqlite3.connect('/data/charthound.db')
print('chart_data:', conn.execute('SELECT COUNT(*) FROM chart_data').fetchone()[0])
print('tracks:', conn.execute('SELECT COUNT(*) FROM tracks').fetchone()[0])
print('charts:', conn.execute('SELECT DISTINCT chart_name FROM chart_data').fetchall())
print('tracker_items:', conn.execute('SELECT COUNT(*) FROM tracker_items').fetchone()[0])
conn.close()"
```

---

## INFRASTRUCTURE

| Item | Value |
|---|---|
| Live URL | `charthound.duckdns.org` via Caddy reverse proxy |
| Server IP | `192.168.50.42` |
| Laptop IP (qBittorrent) | `192.168.50.225` |
| Host port | `8585` |
| Container name | `charthound` |
| GitHub | `https://github.com/CurtisColby/ChartHound` (private) |
| Login | Username: `Colby` (capital C) |
| Container file path | `/app/app/routers/` for Python, `/app/frontend/` for HTML |

---

## GITHUB WORKFLOW — NOW ACTIVE

```bash
# main = last confirmed working
# dev = active development

# Push to dev
cd ~/ChartHound
git checkout dev
docker cp charthound:/app/app/routers/retriever.py app/routers/retriever.py
docker cp charthound:/app/app/routers/groomer.py app/routers/groomer.py
docker cp charthound:/app/app/routers/kennel.py app/routers/kennel.py
docker cp charthound:/app/app/routers/sniffer.py app/routers/sniffer.py
docker cp charthound:/app/app/routers/bloodhound.py app/routers/bloodhound.py
docker cp charthound:/app/app/routers/tracker.py app/routers/tracker.py
docker cp charthound:/app/app/main.py app/main.py
docker cp charthound:/app/frontend/index.html frontend/index.html
git add -A && git commit -m "description" && git push

# Merge dev to main when confirmed working
git checkout main && git merge dev && git push && git checkout dev
```

---

## DUAL DATABASE ARCHITECTURE

### `charthound_static.db` — Read-only, ships with GitHub
- `chart_reference` — 68,300 Billboard CSV entries (Hot 100, Country, R&B, Rock, Dance, Adult Pop through 2018)
- `billboard_pop` — 39,930 historical pop entries (1890–2015)
- **Total: 108,230 entries**
- **CCM/Gospel: ZERO entries** — uses Last.fm estimates as automatic fallback

### `charthound.db` — Dynamic, user-owned (Docker volume)
- Users, connections, tracks, scan jobs, write log, app settings, chart_data cache
- `chart_status` and `chart_last_checked` columns on tracks table — skip cache for Groomer scans
- `sniffer_download_path` in app_settings — where Sniffer grabs land
- `tracker_items` and `tracker_log` tables — Tracker state (M8)
- `tracker_enabled`, `tracker_interval`, `tracker_cooldown`, `tracker_max_daily`, `tracker_logging` in app_settings — Tracker config (M8)
- Survives app updates

---

## METADATA WATERFALL ORDER

**MusicBrainz → Last.fm → ListenBrainz → Deezer → Discogs → iTunes**

**Year priority:** file `originalyear` tag → MusicBrainz → Discogs → Deezer → iTunes

---

## TAB STATUS

### THE KENNEL ✅ Functional
- All connection cards working (Plex, Emby, Jellyfin, Last.fm, Prowlarr, Radarr, Sonarr, qBittorrent, Deluge, Transmission, YouTube, Discogs)
- Path translator working
- **New in M5:** ChartHound Download Path field on download client card — saves to `app_settings` as `sniffer_download_path`
- **New (Apr 23, 2026):** Each connection card has a ▲ COLLAPSE / ▼ EXPAND rollup button. Connected services auto-collapse on first render; not-connected services stay expanded. Once a user toggles a card manually, their choice is preserved for the session.
- **New (Apr 23, 2026):** 💝 SUPPORT CHARTHOUND button in the Security panel opens the donation modal (BTC + ETH + Buy Me a Coffee).

### THE RETRIEVER ✅ Functional
- 100% working per last test
- **CLEAR FULL DATABASE button removed from sidebar** — now lives only in The Veterinarian (Danger Zone) to keep maintenance actions in one place. A subtle pointer note tells users where to find it.

### THE SNIFFER 📡 ✅ Functional — Milestone 5

**Two search modes:**
- **Chart Gap Fill** — Cross-references static DB chart entries against user's tracks table. Shows ALL/MISSING ONLY/OWNED ONLY filter. Paginated (500 per page with LOAD MORE).
- **Trending** — Fetches top tracks from Last.fm by genre tag. Paginated via Last.fm API pages.

**Search (Prowlarr):**
- Torznab per-indexer endpoints, album-first strategy, concurrent indexer queries
- Filtered to 1+ seeders, sorted descending, capped at 25 results

**Three download buttons per result:** ⬇ GET, ↗ OPEN, 🔗 MAGNET

**Multi-card Prowlarr results (built April 19, 2026):**
- Each track search spawns an independent card appended to the bottom
- Cards stack — selecting multiple tracks creates multiple cards
- Each card has **−** (minimize/roll up) and **✕** (close/remove) buttons
- Minimize collapses to thin header bar; expand restores full results
- Clicking same artist again expands existing card instead of duplicating
- Auto-scroll to bottom on each new card creation
- Each card owns its own results array — GET/OPEN/MAGNET buttons are independent

**Hash-targeted background checkmark (fixed April 19, 2026):**
- After adding torrent, immediately captures the specific torrent hash from qBit
- Passes hash + title to background task — no more "find most recent" guessing
- Each grabbed torrent gets its own independent background watcher
- **Metadata wait phase:** if files list is empty (hash-only), force-resumes to trigger peer connection, retries until files appear
- **Dead torrent logging:** if 60s passes with no metadata, logs warning with title: "may be dead/fake"
- **Multi-grab safe:** grab 5 torrents in 10 seconds, each tracked independently

**UI features:**
- Gold accent color (`--c3`)
- Chart selector checkboxes, year range sliders, peak position slider
- Trending genre dropdown
- ALL / MISSING ONLY / OWNED ONLY filter buttons + text search filter
- Sortable columns (Artist, Title, Year, Peak, Status)
- Activity log with clear button
- Scroll nav hover buttons (gold-colored)
- Lidarr setup info banner

**Files:** `sniffer.py` (backend), section in `index.html` (frontend), `kennel.py` (download path endpoints), `main.py` (router registration)

### THE GROOMER ✅ Functional
- All previously confirmed features working
- Skip cache, smart waterfall, CCM estimates, playlist push, M3U download

### THE VETERINARIAN 🩺 ✅ Built
- DB Health, Skip Cache stats, Maintenance (VACUUM, INTEGRITY CHECK)
- Danger Zone (Clear Full Database)
- Debug Console (off by default, 1000 line cap)
- Static DB Sources display

### THE BLOODHOUND 🔍 ✅ Functional — Milestone 6

**Built April 19, 2026:**

**Three search modes:**
- **Artist Search (🎤)** — Type artist name → MusicBrainz returns top 10 matches with score, country, type, lifespan → Select artist → See all their releases filtered by type
- **Album Search (💿)** — Direct release-group search on MusicBrainz by album name. Paginated (100 per page with LOAD MORE). MusicBrainz returns `mb_count` total so frontend knows remaining.
- **Compilation Search (📀)** — Dropdown with 31 presets organized in optgroups + custom text search

**Compilation dropdown presets (organized by category):**
- **Pop/General:** Greatest Hits, Best Of, Number Ones, Super Hits, Essential, Gold, Platinum Collection, 100 Hits
- **Branded Series:** Now That's What I Call Music, Time Life Music, Billboard Hits, Kidz Bop, Grammy Nominees, Ministry of Sound, Bravo Hits
- **Genre:** MTV Unplugged, Pure Motown, Monster Ballads, Jock Jams, Ultra Music Festival
- **CCM/Gospel:** WOW Hits, WOW Worship, WOW Gospel, Songs 4 Worship, Praise and Worship, iWorship, Hymns, Veggie Tales, Cedarmont Kids, Maranatha Music, Integrity Music

**Artist release type filter:** Albums / Compilations / Singles / All buttons — switches dynamically after artist selection

**Library ownership check:**
- Both `artist_releases` and `album_search` endpoints run `_build_library_index()` + `_check_library()` (imported from sniffer) against each result
- STATUS column shows ✅ Owned or ❌ Missing per row
- Stats bar shows RESULTS / IN LIBRARY / MISSING counters
- ALL / ❌ MISSING ONLY / ✅ OWNED ONLY filter buttons (same pattern as Sniffer)
- Text filter respects active ownership filter (both work together)

**Prowlarr search + grab:** Same Torznab pipeline as Sniffer — reuses `_get_connection`, `_qbt_login`, `_background_checkmark` via imports. Multi-card results with −/✕ buttons, independent results arrays, auto-scroll.

**MusicBrainz rate limiting:**
- 1 request/second enforced via `asyncio.Lock` + `time.monotonic()` timer
- 503 retry: if rate-limited, waits 2 seconds and retries once
- User-Agent: `ChartHound/1.0.0 (charthound.duckdns.org)` — required by MusicBrainz policy

**UI features:**
- Orange accent color (`--c5`)
- Activity log with clear button
- Scroll nav hover buttons (orange-colored)
- LOAD MORE button for album/compilation searches (shows remaining count from MusicBrainz)

**Files:** `bloodhound.py` (backend), section in `index.html` (frontend), `main.py` (router registration)

### THE TRACKER 🎯 ✅ Functional — Milestone 8 (Hardened post-ship)

**Built April 23, 2026 — hardened same day for ban-safety:**

**Core architecture — "don't reinvent the wheel":**
- ChartHound syncs missing items from Radarr/Sonarr into a local `tracker_items` table
- Fires search commands back via Radarr/Sonarr's own `POST /api/v3/command` endpoint
- Radarr handles `MoviesSearch` — picks its own indexers via Prowlarr, uses its own qBit category
- Sonarr handles `SeasonSearch` / `EpisodeSearch` — same, uses its own qBit category
- ChartHound NEVER touches Prowlarr directly for Tracker operations
- No new download client integration needed — Radarr/Sonarr already configured

**Smart TV ordering:**
- Earliest missing season first — won't search season 3 if season 2 is still missing
- When entire season is missing → fires `SeasonSearch` (one command instead of per-episode)
- When season is partially complete → fires `EpisodeSearch` for individual missing episodes
- Skip specials (season 0) automatically
- User can skip a stuck season → unblocks later seasons for auto-search

**Rate control — ban-safe by design:**
- **Gentle / Moderate mode switch** (no Aggressive — intentionally removed to protect users)
  - Gentle: base 3600s (~1/hr), daily cap 20 → default, safest
  - Moderate: base 1200s (~1/20min), daily cap 60 → faster backlog processing
- **Jittered sleep** — every tick sleeps `base ± 50%`, so actual intervals land 30–90 min (Gentle) or 10–30 min (Moderate). Prevents bot-pattern detection.
- **Absolute floor** — 5-minute minimum sleep regardless of settings (safety net)
- **Alternating sources** — each tick tries Radarr first, next tick tries Sonarr first, so movies and TV both get worked without starvation
- **Release-date filter** — items with future `release_date` are skipped (stops pointless searches for unreleased movies/episodes)
- Cooldown per item (default 7 days, clamped 1–30 days) — won't re-search unfindable items
- Daily search cap is now mode-driven (Gentle=20, Moderate=60); Max Daily UI input removed
- Picker priority: never-searched first → newest release → oldest `last_searched`

**Default OFF — explicit user opt-in required:**
- `tracker_enabled` stored in `app_settings`, defaults to `false`
- Toggle on/off from the UI at any time
- Hunt loop resumes automatically on container restart if it was enabled

**Background hunt loop:**
- `asyncio.create_task()` — same proven pattern as Groomer scans and Sniffer checkmarks
- 5-second startup delay to let container settle
- Graceful cancellation on toggle-off
- Error-resilient — catches exceptions, logs them, sleeps 60s, continues

**Sync logic:**
- Pulls all monitored movies from Radarr (`GET /api/v3/movie`) — filters `hasFile=false`
- Pulls all monitored series from Sonarr (`GET /api/v3/series`) then episodes per series (`GET /api/v3/episode?seriesId=X`)
- Captures release dates: Radarr `physicalRelease → digitalRelease → inCinemas` fallback chain; Sonarr `airDateUtc → airDate`
- Refreshes `release_date` on every sync (not just inserts)
- Detects items no longer in Radarr/Sonarr (user removed them) and cleans them from local table
- Marks items that have been found (got a file) as `status='found'`

**Database — self-creating tables (no database.py edit needed):**
- `tracker_items` — item_id, source, external_id, series_id, title, year, season_number, episode_number, search_type, status, last_searched, search_count, cooldown_until, skipped_season, added_at, **release_date** (added via additive migration)
- `tracker_log` — log_id, source, title, action, detail, created_at (capped at 5,000 entries)
- Tables created via `_ensure_tracker_tables()` called from `tracker_startup()` in main.py lifespan
- Indexes: source, status, series, cooldown, release_date

**13 API endpoints (all require_auth):**
- `GET /api/tracker/status` — on/off state, mode, base_interval, stats, connection status
- `POST /api/tracker/toggle` — flip on/off
- `POST /api/tracker/sync` — pull missing lists from Radarr/Sonarr
- `POST /api/tracker/search-now` — manual search for specific item
- `POST /api/tracker/skip` / `POST /api/tracker/unskip` — skip/resume individual items
- `POST /api/tracker/skip-season` / `POST /api/tracker/unskip-season` — skip/resume entire seasons
- `GET /api/tracker/items` — paginated item list with source/status filters
- `GET /api/tracker/log` — activity log (newest first)
- `POST /api/tracker/clear-log` — clear log
- `POST /api/tracker/settings` — mode, cooldown, max_daily (override), logging toggle

**UI features:**
- Crimson accent color (`--c6`)
- Left panel: master on/off toggle, connection status dots (Radarr/Sonarr), sync button, settings (🐢 Gentle / 🐇 Moderate switch + info banner explaining ban risk, cooldown input, logging toggle), activity log with CLEAR button
- Right panel: stats bar (movies missing, episodes missing, found, searched today, skipped), filter buttons (All/Movies/TV/Missing/Found/Skipped + text filter), results table with per-item actions, crimson hover scroll nav
- Per-item actions: 🔍 SEARCH (manual), ⏭ SKIP, ↩ UNSKIP, S# SKIP (season skip for TV)
- **Sortable LAST SEARCHED column** — click to cycle through: newest-first (default ↓) → oldest-first (↑) → never-searched-first (▲)
- 15-second auto-poll for status refresh while on Tracker tab
- LOAD MORE pagination (200 items per page)
- NEVER-searched items render with `— NEVER —` label in crimson for visibility

**Files:** `tracker.py` (backend), section in `index.html` (frontend), `main.py` (router registration + startup call)

### THE SCOUT ⬜ Not Started — Milestone 9
### THE LOOKOUT ⬜ Not Started — Milestone 10

---

## CRITICAL TECHNICAL NOTES

1. **Docker volume + ext4/GVFS** — Cannot create temp files via Docker volume on NAS. FLACs need second identical volume mount.
2. **Async file writes** — `background_tasks.add_task()` unreliable for heavy I/O on NAS. Use `asyncio.create_task()` instead.
3. **Blocking I/O** — `write_tags` must run in `ThreadPoolExecutor`.
4. **Python bytecode cache** — Always delete `.pyc` before restart.
5. **FLAC write** — `metaflac` at `/usr/bin/metaflac` (full path required).
6. **manually_verified=1** — Never overwritten by Auto-Pilot or auto-clear.
7. **Groomer track records** — `_resolve_track_id()` stores server file paths (not Docker paths).
8. **Static DB** — Read-only. `sqlite3` sync in executor threads only.
9. **JF path-index** — Builds `path→jf_id` dict from full library (~10–15s for 33k tracks).
10. **JF batch add** — `/Playlists/{id}/Items` needs `Ids` as query param string, NOT JSON body.
11. **Clipboard on HTTP** — `navigator.clipboard` blocked. HTTP fallback uses `execCommand('copy')`. Donation modal `copyDonationAddr()` uses both paths with fallback.
12. **Skip cache age gate** — Misses older than 6 months are re-checked.
13. **Container file layout** — Python routers at `/app/app/routers/`, frontend at `/app/frontend/`. Double `app`.
14. **Vet endpoints in groomer.py** — Housed under existing router prefix to avoid main.py changes.
15. **Scroll-nav leak across tabs (RESOLVED)** — `ret-scroll-nav` and `grm-scroll-nav` append to `document.body` and persist across tab switches. They now check their owner panel is `.active` before showing opacity. When building future scroll navs, either append to the panel (Sniffer/Bloodhound/Tracker pattern) OR gate opacity on `panel-{name}.classList.contains('active')`.
16. **Kennel conn-card rollup** — CSS-driven via `.conn-card.collapsed` class that hides everything except `.conn-card-header`. Rollup button injected programmatically into each card's header by `injectConnRollupButtons()` called after every `refreshAllDots()`. Auto-collapses connected services on first paint via `_connRollupInitDone` gate — respects user toggles thereafter.

### SNIFFER-SPECIFIC TECHNICAL NOTES

15. **Prowlarr `/api/v1/search` is BROKEN** — Does not accept comma-separated `indexerIds` or `categories`. Returns 400 or 500 errors. Prowlarr's own GitHub issue #2440 confirms this is a known bug. **DO NOT USE this endpoint.**
16. **Torznab per-indexer endpoint is correct** — `/{indexerId}/api?t=search&q=...&cat=...&apikey=...` — this is what Lidarr/Radarr/Sonarr use. Supports comma-separated categories natively. Returns XML.
17. **Prowlarr proxies download URLs** — Download URLs from Torznab are Prowlarr proxy URLs (e.g., `http://192.168.50.42:9696/10/download?apikey=...&link=...`), not direct torrent links. qBittorrent fetches the actual torrent file through Prowlarr.
18. **qBittorrent file priority = checkmarks** — Priority 0 = unchecked (do not download). Priority 1 = Normal (checkmarked). When all files are priority 0, qBit treats the torrent as "complete" and moves to seeding with 0 bytes. The background checkmark task fixes this.
19. **qBittorrent on separate machine** — qBit runs on laptop (192.168.50.225:8080), ChartHound on server (192.168.50.42). NAS path mapping differs: server sees `/media/colby/NAS1/`, laptop also sees `/media/colby/NAS1/`. qBit category save path must use the laptop's perspective.
20. **Album-first search strategy** — Single-track torrents have terrible seeds and metadata (fake files, ASCII garbage, no tags). Album torrents have 10-100x more seeds, proper metadata, and Plex/Emby/JF can identify them. Always search by artist name, not "artist - track".
21. **Last.fm `tag.getTopTracks` returns listeners=0** — The listener count field is always 0 in this endpoint's response. Tracks are pre-ranked by popularity. Do not filter by min_listeners.
22. **Background checkmark hash fix (RESOLVED)** — Previously queried "most recently added charthound-music torrent" causing multi-grab collisions. Now captures specific hash immediately after add and passes to background task. Each torrent tracked independently.
23. **`asyncio.create_task` for background checkmark** — Same pattern as Groomer scan. Runs independently from the HTTP response. User gets immediate "Torrent added" response. Background task retries for 60 seconds.

### BLOODHOUND-SPECIFIC TECHNICAL NOTES

24. **MusicBrainz rate limit: 1 req/sec** — Enforced via `asyncio.Lock` + `time.monotonic()`. If 503 received, waits 2s and retries once. Exceeding rate causes IP block across ALL MusicBrainz access.
25. **MusicBrainz User-Agent required** — Must send identifying UA header per MB policy. Using `ChartHound/1.0.0 (charthound.duckdns.org)`. Apps without proper UA get throttled or blocked.
26. **MusicBrainz `release-group` search returns max 100 per call** — Supports `offset` param for pagination. `count` field in response gives total matches. Used for LOAD MORE.
27. **Bloodhound imports helpers from sniffer** — `_get_connection`, `_qbt_login`, `_background_checkmark`, `_build_library_index`, `_check_library` all imported. Zero code duplication for Prowlarr/qBit/library operations.
28. **qBit checkmark occasional miss** — Some torrents resolve metadata but background task doesn't flip the checkbox. User may occasionally need to manually checkmark in qBit's file pane. Low priority — add tooltip/banner rather than over-engineering.
29. **qBit torrent names show as hashes initially** — When added via Prowlarr proxy URL, qBit shows the info hash until metadata resolves from peers. Force-resume in background task triggers metadata fetch. Dead/fake torrents never resolve.

### TRACKER-SPECIFIC TECHNICAL NOTES

30. **Tracker uses Radarr/Sonarr command API, NOT Prowlarr** — `POST /api/v3/command` with `{"name": "MoviesSearch", "movieIds": [X]}` for Radarr, `{"name": "SeasonSearch", "seriesId": X, "seasonNumber": Y}` or `{"name": "EpisodeSearch", "episodeIds": [Z]}` for Sonarr. Radarr/Sonarr handle their own Prowlarr integration, indexer selection, and download client handoff internally.
31. **Tracker has its own `_get_connection()` copy** — does NOT import from sniffer. This keeps the module self-contained and avoids circular import risks. Same logic, same DB query, just duplicated intentionally.
32. **Self-creating tables pattern** — `tracker_items` and `tracker_log` are created by `_ensure_tracker_tables()` called from `tracker_startup()` in main.py lifespan. Additive column migrations use try/except on ALTER TABLE. No `database.py` edit required. This pattern can be reused for future milestones (Scout, Lookout).
33. **Hunt loop startup recovery** — `tracker_startup()` checks `app_settings` for `tracker_enabled=true` and auto-restarts the hunt loop on container boot. User doesn't need to re-enable after a restart.
34. **Season 0 skip** — Sonarr "Specials" (season 0) are silently excluded during sync. They're almost never available on indexers and would clog the queue.
35. **Sync cleans orphans** — Items that were in `tracker_items` but no longer exist in Radarr/Sonarr (user removed them) are deleted during sync. Prevents stale phantom items.
36. **Jittered sleep, not fixed interval** — Every hunt loop tick sleeps `base_interval ± 50%` via `random.uniform(-0.5, 0.5)`. Fixed 90s intervals were getting users IP-banned because they look like a bot. Moderate base=1200s → actual 600–1800s between requests. Gentle base=3600s → actual 1800–5400s.
37. **Mode presets are single source of truth** — `_MODES` dict in `tracker.py` defines `base_interval` and `max_daily` per mode. Daily cap UI input was removed to prevent users from recreating an "aggressive mode" via backdoor. Backend still honors a `tracker_max_daily` override in `app_settings` for power-user DB pokes.
38. **Release date filter** — Items with `release_date > today` are excluded from the picker. Prevents pointless searches for unreleased movies (Radarr `physicalRelease/digitalRelease/inCinemas`) and unaired episodes (Sonarr `airDateUtc`).
39. **Alternating Radarr/Sonarr** — Each hunt tick flips a toggle: try preferred source first, fall back to the other. Previous version drained all Radarr items before touching Sonarr, leaving TV starved for days.
40. **Picker ORDER BY precedence** — `search_count ASC` (never-searched wins) → `release_date DESC` with NULLs-last (newest release next) → `COALESCE(last_searched, '0000') ASC` (longest-ago gets next shot). For Sonarr, the in-memory pass then filters out episodes blocked by earlier missing seasons.
41. **Log cap** — `tracker_log` auto-prunes to 5,000 entries on every write. Prevents unbounded DB growth from long-running hunt loops.
42. **Settings clamping** — cooldown (1–30 days), max_daily override (5–200). Mode must be `gentle` or `moderate` (400 error otherwise). No interval input exists anymore — it's driven entirely by the mode preset.

---

## STATIC DATABASE — SOURCES

### Currently Loaded
- ✅ Billboard Pop 1890–2015 (39,930 entries)
- ✅ Billboard Hot 100, Country, R&B, Rock, Dance, Adult Pop CSVs (68,300 entries)
- **Total: 108,230 entries**

### Downloadable/Parseable (next priority)
- ⬜ `github.com/utdata/rwd-billboard-data` — Hot 100 post-2018 (highest priority)
- ⬜ Kaggle Billboard Hot 100 Weekly Charts ("Hot Stuff.csv")
- ⬜ Kaggle Kworb Global Charts (USA, UK, Brazil etc.)
- ⬜ Chart2000.com tsort data (1900–1995, 150k+ songs)

---

## FUTURE IDEAS BACKLOG

### Sniffer Enhancements
- Indexer name display (currently shows "Indexer #60" — resolve to name from Prowlarr)
- Genre filter on Prowlarr results
- Download progress polling in UI
- Path translator explanation tooltip for users

### Bloodhound Enhancements
- Add more CCM compilation terms as needed (user can request)
- Music video search mode with resolution filter + `charthound-musicvids` qBit category (deferred — not a priority)
- Tooltip/banner about occasional manual checkmark in qBit

### Tracker Enhancements
- Cutoff upgrade search (search for better quality versions of already-downloaded items)
- Webhook receiver from Radarr/Sonarr (push new missing items instead of polling via sync)
- Per-series search interval override (search weekly shows more frequently)
- Radarr/Sonarr queue awareness (don't re-search items already downloading)

### Veterinarian — Next Priority
- One-click import buttons for downloadable static DB sources (utdata, Kaggle, Chart2000)
- © Colby R. Curtis copyright footer on each tab panel + sidebar
- DB backup/restore tools

### Kennel
- GitHub Sponsors integration once repo is public (Colby's repo currently private)
- QR code display for BTC/ETH addresses in donation modal

### Retriever Bugs
- Preview mode "Write Selected" via API
- Preview table old vs. new side-by-side

### Groomer Enhancements
- Inline audio preview
- Plex/Emby/JF deep links

---

## CONSTITUTION REMINDERS

- ❌ No Spotify API — ever
- 📡 Waterfall: MusicBrainz → Last.fm → ListenBrainz → Deezer → Discogs → iTunes
- 📝 File-First: write to physical file BEFORE media server refresh
- 🚫 Non-Destructive: NEVER move, rename, or delete files/folders
- ⏱ iTunes rate limit: 20 req/min max
- ⏱ MusicBrainz rate limit: 1 req/sec max (enforced via async lock)
- 💾 DB under 200MB, store image paths not blobs
- 🔒 Present logic summary → wait for Go → write code
- 🏷 Retriever owns: GENRE, MOOD, DATE tags
- 📊 Groomer owns: COMMENT tag chart data only
- 🔐 manually_verified=1 tracks NEVER overwritten
- 🎯 Tracker must stay ban-safe: jittered intervals, no mode above "Moderate", never expose a configurable base interval in the UI
- © "ChartHound", all tab names, "Developed by Colby R. Curtis", Support button + donation modal — protected, never remove
- 📄 README.md now exists on GitHub — update it when adding new milestones

---

## PROJECT FILES (as of Milestone 8)

| File | Location (container) | Purpose |
|---|---|---|
| `main.py` | `/app/app/main.py` | FastAPI app, router registration, lifespan startup |
| `database.py` | `/app/app/database.py` | Schema, init_db, migrations |
| `kennel.py` | `/app/app/routers/kennel.py` | Connection vault, test, path translation |
| `retriever.py` | `/app/app/routers/retriever.py` | Metadata tagging engine |
| `groomer.py` | `/app/app/routers/groomer.py` | Chart data scanner, playlist builder, vet endpoints |
| `sniffer.py` | `/app/app/routers/sniffer.py` | Chart gap finder, Prowlarr search, qBit grab |
| `bloodhound.py` | `/app/app/routers/bloodhound.py` | MusicBrainz artist/album hunter |
| `tracker.py` | `/app/app/routers/tracker.py` | Radarr/Sonarr missing media hunter |
| `index.html` | `/app/frontend/index.html` | All frontend UI (single-file) |
| `docker-compose_example.yml` | repo root | Template compose file |
| `README.md` | repo root | Project documentation |
| `LICENSE.md` | repo root | Copyright & license terms |

---

## SESSION LEARNINGS — April 23, 2026 (Tracker Hardening + UI Pass)

**Tracker ban-safety realization:**
- The original Tracker shipped with a fixed 90-second interval and a 100/day cap. Colby flagged it correctly: that pattern looks like a bot to indexers (especially private trackers), not a human. The fix wasn't to tune the interval but to change the shape of the traffic — random jitter (±50% of base) so no two sleeps are the same, alternating Radarr/Sonarr each tick, and capping the ceiling at Moderate (~1/20min). No "Aggressive" mode on purpose — exposing it would defeat the whole design.
- UX lesson: `max_daily` looked like a safety knob but was actually a backdoor aggression dial. Removed from the UI after initial deploy. Mode presets are the single source of truth for pacing.
- When users turn on the new Tracker for the first time, legacy `app_settings` rows like `tracker_interval = 90` and `tracker_max_daily = 100` linger from the old schema. The hunt loop ignores `tracker_interval` entirely now, but it still reads `tracker_max_daily` as an override. Always check for and purge legacy keys on schema-breaking changes: `DELETE FROM app_settings WHERE key IN ('tracker_interval','tracker_max_daily')`.

**Scroll-nav leak across tabs:**
- Retriever and Groomer scroll navs append to `document.body` (vs. Sniffer/Bloodhound/Tracker which append to their panels). That means they persist across tab switches as ghost elements on other panels. Took a minute to spot because the code looked correct in isolation.
- Fix principle: any fixed-position element hung off body needs either (a) to be appended to its owner panel, or (b) to gate its visibility on `panel-{name}.classList.contains('active')`. Went with option (b) because it was the smaller diff.

**SQLite in the container:**
- The ChartHound container image does NOT include the `sqlite3` CLI. All diagnostic queries from the host must go through `docker exec charthound python3 -c "..."` using the `sqlite3` stdlib module. This will bite any new user trying to debug the DB. Consider adding `sqlite3` to the Dockerfile eventually, or ship a `chdb` helper script inside the image.

**Docker deploy workflow that works:**
- `docker cp ~/Downloads/file.py charthound:/app/app/routers/file.py`
- `docker exec charthound find /app/app -name "*.pyc" -delete`
- `docker exec charthound find /app/app -name "__pycache__" -type d -exec rm -rf {} + 2>/dev/null`
- `docker restart charthound`
- For `index.html` ONLY: no restart needed, just browser hard-refresh.
- Colby's GitHub repo lives at `/home/colby/ChartHound` on the Plex server, dev/main branches active.

**Git round-trip when Downloads is on a different machine:**
- If the new file is in the container but not in `~/Downloads` on the host (e.g. you deployed from a different machine), `docker cp` works both ways: `docker cp charthound:/app/app/routers/tracker.py ~/Downloads/tracker.py` extracts it cleanly. Then verify with `grep -c` on the new constants before copying into the git repo.

**CSS-driven rollup pattern:**
- The Kennel rollup didn't need any HTML restructuring. A single class `.conn-card.collapsed` hides everything except the header via `> *:not(.conn-card-header) { display:none }`. The toggle button is injected by JS into every conn-card header in a single pass. Same pattern will work for future "thin cards" requests (Sniffer results groups, Bloodhound artist cards, etc.) without touching HTML.

**Donation modal UX considerations:**
- Crypto addresses must be selectable AND click-to-copy because mobile users can't reliably drag-select 42-character strings. Both handlers in place.
- First-4/last-4 reveal after copy ("✓ COPIED — bc1qdr9j…kk6p8hv") reassures the user they copied the right thing without filling the screen.
- ESC key closes the modal. Background click closes the modal. Modal card click does not.
- `support-btn` has two sizes via `.compact` modifier — one fits in the header-credit line, full size fits on login card and Kennel security panel.
