# ChartHound — Current State Summary
**Last Updated:** April 18, 2026 — Late Night Session (Skip Cache + Veterinarian Tab)

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

# Reset password
docker exec charthound python3 -c "
import bcrypt, asyncio, aiosqlite
async def reset():
    hashed = bcrypt.hashpw(b'ChartHound1!', bcrypt.gensalt()).decode()
    async with aiosqlite.connect('/data/charthound.db') as db:
        await db.execute('UPDATE users SET password_hash=? WHERE username=?', (hashed, 'Colby'))
        await db.commit()
asyncio.run(reset())"

# Deploy all files
docker cp /home/colby/Downloads/retriever.py charthound:/app/app/routers/retriever.py && \
docker cp /home/colby/Downloads/groomer.py charthound:/app/app/routers/groomer.py && \
docker cp /home/colby/Downloads/index.html charthound:/app/frontend/index.html && \
docker exec charthound find /app -name "*.pyc" -delete && \
docker restart charthound

# CRITICAL: If container is crash-looping, stop first then cp
docker stop charthound && \
docker cp /home/colby/Downloads/groomer.py charthound:/app/app/routers/groomer.py && \
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
conn.close()"
```

---

## INFRASTRUCTURE

| Item | Value |
|---|---|
| Live URL | `charthound.duckdns.org` via Caddy reverse proxy |
| Server IP | `192.168.50.42` |
| Host port | `8585` |
| Container name | `charthound` |
| GitHub | `https://github.com/CurtisColby/ChartHound` (private) |
| Login | Username: `Colby` (capital C) |
| Container file path | `/app/app/routers/` for Python, `/app/frontend/` for HTML |

---

## GITHUB WORKFLOW — NOW ACTIVE

```bash
# main = last confirmed working (e815b29)
# dev = active development (6bbbf45 — skip cache confirmed working)

# Push to dev
cd ~/ChartHound
git checkout dev
docker cp charthound:/app/app/routers/retriever.py app/routers/retriever.py
docker cp charthound:/app/app/routers/groomer.py app/routers/groomer.py
docker cp charthound:/app/app/routers/kennel.py app/routers/kennel.py
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
- **New columns on tracks table (April 18):** `chart_status TEXT` and `chart_last_checked TEXT` — skip cache for Groomer scans
- Survives app updates

---

## METADATA WATERFALL ORDER

**MusicBrainz → Last.fm → ListenBrainz → Deezer → Discogs → iTunes**

**Year priority:** file `originalyear` tag → MusicBrainz → Discogs → Deezer → iTunes

---

## TAB STATUS

### THE KENNEL ✅ Complete
- All 10 services connected and working (Plex, Emby, JF, Last.fm, Prowlarr, Radarr, Sonarr, qBittorrent, YouTube, Discogs)
- Discogs personal access token saved and verified
- Discogs header dot (`DISC`) added to top bar alongside PLEX · EMBY · JF · LFM · PWL
- **TODO (deferred):** Rollup cards UI — connected services collapse to thin strip

---

### THE RETRIEVER ✅ Complete

**Confirmed working (prior session):**
- Full waterfall ✅ | Preview mode ✅ | Write Selected ✅ | Override ✅ | Auto-Pilot ✅
- Album Tagger: Keep removes card ✅ | 🔍 WRITE ALL LOOKUPS ✅ | Override ✅
- Subfolder queue multi-folder fix ✅ | scanJobId fallback ✅
- Custom genre/mood persistent + delete ✅ | Scroll nav ✅
- Refresh button styled + tooltip ✅ | Mode descriptions updated ✅

**Outstanding (from prior sessions — not yet re-tested):**
1. Preview mode "Write Selected" broken via API (only Auto-Pilot works)
2. Preview table not showing old vs. new proposed tags side-by-side
3. Genre count exceeding 3 (Discogs styles incorrectly merging with genres)
4. Discogs using track-level rather than album-level search

**Low priority remaining:**
1. Genre blacklist expansion (films/games, jazz-funk, bro-country, downtempo etc.)
2. Compilation artist fingerprinting
3. Year showing reissue dates on some albums
4. Auto-Pilot rolling card UI feedback
5. Font/text size pass on small UI text
6. POPM Rating not showing in Kid3 — one-liner fix: change POPM email to `"Windows Media Player 9 Series"`

---

### THE GROOMER ✅ Functional

**Fixed this session (April 18):**
- **Skip cache (`chart_status`)** ✅ — Two new columns on `tracks` table: `chart_status` (NULL/hit/miss) and `chart_last_checked` (ISO timestamp). On repeat scans, tracks marked `miss` within 6 months skip instantly — no static DB query, no Last.fm call. Confirmed working: 72 hits, 904 misses, 36,870 unchecked after partial scan. Reduces 33k lookups to ~3k on repeat scans.
- **Skip cache API endpoints** ✅ — `GET /api/groomer/skip_cache/stats` and `POST /api/groomer/skip_cache/reset`
- **Veterinarian DB endpoints** ✅ — `GET /api/groomer/vet/db_health`, `POST /api/groomer/vet/vacuum`, `POST /api/groomer/vet/integrity_check` — all housed in groomer.py under the existing router prefix (no main.py changes needed)

**Previously confirmed working:**
- Smart waterfall (static DB → Last.fm opt-in) ✅
- CCM via Last.fm estimates — working with real peak positions ✅
- Library selector dropdown ✅
- Genre from file on matched tracks only ✅
- Plex 100% | Emby 99.4% | JF 98% push accuracy ✅
- Write Tags / COMMENT tag write ✅
- Local folder scan with dedicated ThreadPoolExecutor ✅
- M3U download + path translation ✅
- Phase banners (teal Phase 1 / pink Phase 2) ✅

**Outstanding / Deferred:**
1. **JF >200 tracks push** — not yet confirmed at scale
2. **JF overwrite no duplicate** — not yet confirmed
3. **Duplicates in results** — compilation dedup fix deployed but needs re-testing
4. **Local scan speed** — skip cache now addresses this for repeat scans; first scan still ~1 track/sec
5. **M3U path edge case** — anchor-based fallback not 100% tested across all configurations

**Not yet built:**
- utdata Hot 100 post-2018 import
- CCM static DB data
- Kaggle Spotify Top 10000

---

### THE VETERINARIAN 🩺 ✅ Built — April 18

**Deployed and working:**
- **Nav item** — 🩺 red theme, between The Lookout and bottom of sidebar
- **Sidebar panel:**
  - DB Health — row counts for tracks/artists/albums/chart_data/connections, file sizes for both DBs, static DB chart_reference and billboard_pop counts
  - Skip Cache — shows hit/miss/unchecked counts with RESET button
  - Maintenance — VACUUM DATABASE and INTEGRITY CHECK buttons
- **Main panel:**
  - Danger Zone — Clear Full Database (moved from Retriever sidebar, big red two-step confirm)
  - Debug Console — off by default, 1,000 line cap, START/STOP toggle, level filter, module filter, CLEAR, DOWNLOAD. Replaces old Debug Log tab entirely.
  - Static DB Sources — shows loaded sources (Billboard Pop 1890–2015, Billboard CSVs) and coming-soon sources (utdata, Kaggle, Chart2000, CCM, UK Official Charts)
  - Guided Tour — launches existing setup wizard overlay

**Old Debug Log tab removed** — nav item and panel both deleted. Vet debug console is the replacement.
**Setup Wizard sidebar button removed** — Guided Tour in Veterinarian replaces it.

**TODO for next session:**
1. Static DB import tool — one-click buttons to fetch CSV from GitHub/Kaggle URLs and append to `chart_reference` in static DB
2. © Colby R. Curtis copyright footer on each tab panel
3. Copyright line in sidebar where Setup Wizard button used to be
4. DB backup/restore tools (export/import charthound.db)

---

### THE SNIFFER ⬜ Not Started — Milestone 5
### THE BLOODHOUND ⬜ Not Started — Milestone 6
### THE TRACKER ⬜ Not Started — Milestone 7
### THE SCOUT ⬜ Not Started — Milestone 8
### THE LOOKOUT ⬜ Not Started — Milestone 9

---

## CRITICAL TECHNICAL NOTES

1. **Docker volume + ext4/GVFS** — Cannot create temp files via Docker volume on NAS. FLACs need second identical volume mount.
2. **Async file writes** — `background_tasks.add_task()` unreliable for heavy I/O on NAS. Use `asyncio.create_task()` instead. Confirmed again — BackgroundTasks froze the event loop during 33k Mutagen reads.
3. **Blocking I/O** — `write_tags` must run in `ThreadPoolExecutor`.
4. **Python bytecode cache** — Always delete `.pyc` before restart. Command: `docker exec charthound find /app -name "*.pyc" -delete 2>/dev/null`
5. **FLAC write** — `metaflac` at `/usr/bin/metaflac` (full path required).
6. **manually_verified=1** — Never overwritten by Auto-Pilot or auto-clear.
7. **Groomer track records** — `_resolve_track_id()` stores server file paths (not Docker paths). Clipboard and M3U translate via Kennel path settings.
8. **Static DB** — Read-only. `sqlite3` sync in executor threads only.
9. **chart_year + tag_artist + chart_status + chart_last_checked** — Added via `ALTER TABLE` migration on scan start in `_migrate_chart_data()`.
10. **JF path-index** — Builds `path→jf_id` dict from full library (~10–15s for 33k tracks).
11. **JF batch add** — `/Playlists/{id}/Items` needs `Ids` as query param string, NOT JSON body.
12. **CCM peak=0 bug** — First CCM scan may store peak=0. Run fix command above. Future scans correct.
13. **Clipboard on HTTP** — `navigator.clipboard` blocked. HTTP fallback uses `execCommand('copy')`.
14. **Subfolder queue** — Pipe-separated paths `path1|path2`. `run_scan_job` splits on `|`.
15. **Smart waterfall** — Last.fm fires ONLY when: use_estimates=True OR all charts have zero static entries. Prevents 2-hour scans.
16. **Genre enrichment** — `_enrich_genre_from_file()` reads Mutagen tags on matched tracks only. Splits on `;/\n`, max 3 genres stored.
17. **Dedicated ThreadPoolExecutor for local scan** — `ThreadPoolExecutor(max_workers=2, thread_name_prefix="tag_read")` prevents Mutagen reads from starving uvicorn's default executor. Shut down with `wait=False` in a `finally` block.
18. **Path translation chain** — Docker `/music` → server raw `/media/NAS1/MUSIC TAGGED` → desktop `/media/colby/NAS1/MUSIC TAGGED`. M3U and clipboard both use anchor-based fallback: extract last folder from `server_prefix`, find it in stored path, replace everything before it.
19. **Scan phase UX** — Local scans have two distinct phases visible in the UI: Phase 1 (teal, reading file tags via Mutagen) and Phase 2 (pink, looking up chart data in static DB). Phase 1 can take 30-90 seconds for 33k files over NAS.
20. **`_get_charts_with_static_data()`** — Returns only `{'hot100', 'adultpop'}` for the 4-chart selection `hot100,adultpop,ac,uk`. The `ac` and `uk` chart names may not match what's stored in `chart_reference`. Needs investigation if those charts should be returning hits.
21. **Skip cache age gate** — Misses older than 6 months (`_MISS_AGE_SECONDS = 6 * 30 * 24 * 3600`) are re-checked on the next scan. Configurable in groomer.py.
22. **Container file layout** — Python routers at `/app/app/routers/`, frontend at `/app/frontend/`. Double `app` — always use `docker cp ... charthound:/app/app/routers/` not `/app/routers/`.
23. **Vet endpoints in groomer.py** — The Veterinarian backend endpoints (`/api/groomer/vet/*`) are housed in `groomer.py` under the existing router prefix to avoid needing to register a new router in `main.py`. This is intentional — keeps deployment simple.

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
- ⬜ `github.com/Grimi94/data-science-music`

### Requires Scraping/API Work (future)
- ⬜ OfficialCharts.com (UK singles from 1952, BeautifulSoup)
- ⬜ Billboard artist chart history pages (BeautifulSoup)
- ⬜ EveryHit.com (UK Top 40 archive)
- ⬜ Wikipedia discography tables (structured templates)
- ⬜ TheAudioDB free JSON API
- ⬜ Mediabase/CMB for CCM charts
- ⬜ billboard.py Python library (scrapes Billboard rankings)

### Tools for Future Import
- BrowserAct Billboard Hot 100 Scraper (non-coder CSV export)
- Octoparse / Stevesie Data (no-code web scraping → CSV)
- Wikidata SPARQL queries (structured Wikipedia data)

---

## FUTURE IDEAS BACKLOG

### Veterinarian — Next Session
- One-click import buttons for downloadable static DB sources (utdata, Kaggle, Chart2000)
- © Colby R. Curtis copyright footer on each tab panel + sidebar
- DB backup/restore tools

### Kennel
- Rollup cards — connected services collapse to thin strip

### Retriever Bugs
- Preview mode "Write Selected" via API
- Preview table old vs. new side-by-side
- Genre count exceeding 3 (Discogs styles merging)
- Discogs track-level vs. album-level search

### Groomer Enhancements
- Inline audio preview (HTML5 player, stream from `/music` mount)
- Plex/Emby/JF deep links for tracks with server IDs

### Full 33k Library Scan
- Deliberately deferred until all bugs confirmed fixed
- Skip cache will make repeat scans dramatically faster

---

## CONSTITUTION REMINDERS

- ❌ No Spotify API — ever
- 📡 Waterfall: MusicBrainz → Last.fm → ListenBrainz → Deezer → Discogs → iTunes
- 📝 File-First: write to physical file BEFORE media server refresh
- 🚫 Non-Destructive: NEVER move, rename, or delete files/folders
- ⏱ iTunes rate limit: 20 req/min max
- 💾 DB under 200MB, store image paths not blobs
- 🔒 Present logic summary → wait for Go → write code
- 🏷 Retriever owns: GENRE, MOOD, DATE tags
- 📊 Groomer owns: COMMENT tag chart data only
- 🔐 manually_verified=1 tracks NEVER overwritten
- © "ChartHound", all tab names, "Developed by Colby R. Curtis", Buy Me a Coffee links — protected, never remove
