# ChartHound — Current State Summary
**Last Updated:** April 24, 2026 — Milestone 9 Complete · Static DB Importers · Tracker Scroll Nav Fixed

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

# Wipe all user-imported chart reference data (keeps shipped static DB intact)
docker exec charthound python3 -c "
import sqlite3
conn = sqlite3.connect('/data/charthound.db')
conn.execute('DELETE FROM chart_reference_extras')
conn.commit(); conn.close()"

# Count chart_reference_extras rows by source
docker exec charthound python3 -c "
import sqlite3
conn = sqlite3.connect('/data/charthound.db')
for r in conn.execute('SELECT data_source, COUNT(*) FROM chart_reference_extras GROUP BY data_source').fetchall():
    print(f'{r[0]}: {r[1]:,}')
conn.close()"

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

# Deploy all files (M9+)
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

# Deploy groomer + index only (most common — what M9 static DB importer edits require)
docker cp /home/colby/Downloads/groomer.py charthound:/app/app/routers/groomer.py && \
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

# Diagnostic SQL helper
docker exec charthound python3 -c "
import sqlite3
conn = sqlite3.connect('/data/charthound.db')
# query here
conn.close()"
```

---

## DUAL DATABASE ARCHITECTURE

### `charthound_static.db` — Read-only, ships with GitHub
- `chart_reference` — 68,300 Billboard CSV entries (Hot 100, Country, R&B, Rock, Dance, Adult Pop through 2018)
- `billboard_pop` — 39,930 historical pop entries (1890–2015)
- **Total shipped: 108,230 entries**

### `charthound.db` — Dynamic, user-owned (Docker volume)
- Users, connections, tracks, scan jobs, write log, app settings, chart_data cache
- `chart_status` and `chart_last_checked` columns on tracks table — skip cache for Groomer scans
- `sniffer_download_path` in app_settings — where Sniffer grabs land
- `tracker_items` and `tracker_log` tables — Tracker state (M8)
- `tracker_enabled`, `tracker_cooldown_days_radarr`, `tracker_cooldown_days_sonarr`, `tracker_logging` in app_settings — Tracker config
- **NEW (M9):** `chart_reference_extras` table — user-imported chart data, appended automatically to lookups via UNION in the Groomer and Sniffer

---

## METADATA WATERFALL ORDER

**MusicBrainz → Last.fm → ListenBrainz → Deezer → Discogs → iTunes**

**Year priority:** file `originalyear` tag → MusicBrainz → Discogs → Deezer → iTunes

---

## TAB STATUS

### THE KENNEL ✅ Functional
- All connection cards working (Plex, Emby, Jellyfin, Last.fm, Prowlarr, Radarr, Sonarr, qBittorrent, Deluge, Transmission, YouTube, Discogs)
- Path translator working
- Per-card rollup buttons with session preservation
- 💝 SUPPORT CHARTHOUND button in Security panel

### THE RETRIEVER ✅ Functional
- 100% working per last test
- CLEAR FULL DATABASE button lives in The Veterinarian (Danger Zone)

### THE SNIFFER 📡 ✅ Functional

### THE GROOMER ✂️ ✅ Functional
- **NEW (M9):** Lookup waterfall now UNIONs `chart_reference_extras` from dynamic DB alongside shipped static DB tables

### THE BLOODHOUND 🔍 ✅ Functional

### THE TRACKER 🎯 ✅ Functional — Milestone 8
- **NEW (M9):** Scroll-nav rebuilt in gray scroll-gated style matching Retriever/Groomer (was crimson + always-visible)

### THE VETERINARIAN 🏥 ✅ Functional — Milestone 9
- **NEW:** Static DB Sources panel now has working one-click IMPORT buttons for 6 sources (up from 1)
- All imports land in `chart_reference_extras` in the dynamic DB; shipped static DB untouched
- Staleness indicator — sources imported more than 30 days ago get a `⚠ STALE` amber badge
- Each loaded source shows: entry count, short date (`Apr 24`), ↻ re-import and ✕ delete buttons
- Debug console, danger zone, DB health card all retained

---

## STATIC DATABASE — SOURCES (M9 complete)

### Currently Loaded — Shipped
- ✅ Billboard Pop 1890–2015 (39,930 entries) — shipped
- ✅ Billboard Hot 100, Country, R&B, Rock, Dance, Adult Pop CSVs (68,300 entries) — shipped
- **Shipped subtotal: 108,230 entries**

### Currently Loaded — User-imported via Veterinarian
- ✅ utdata Hot 100 post-2018 — ~32,490 entries (GitHub CSV)
- ✅ Chart2000.com 2000–2024 (global) — ~12,504 entries (HTTP CSV)
- ✅ tsort.info 1900+ Historical (global) — ~52,108 entries (HTTP CSV, version 2-9-0001)
- ✅ Kworb iTunes US (current) — ~100 entries (HTML scrape, snapshot-style)
- ✅ Billboard Christian Songs (current week) — ~16 entries (HTML scrape, snapshot-style)
- ✅ UK Official Charts Singles (current week) — ~100 entries (HTML scrape, href-slug parse)
- **User-imported subtotal: ~97,318 entries**

### **Total chart reference data: ~205,548 entries** (2x pre-M9)

### Deferred / Not Implemented
- Kaggle Billboard Hot 100 Weekly — REMOVED from UI (redundant with utdata; utdata was originally seeded from this Kaggle dataset)

---

## FUTURE IDEAS BACKLOG

### Sniffer Enhancements
- Indexer name display (currently shows "Indexer #60" — resolve to name from Prowlarr)
- Genre filter on Prowlarr results
- Download progress polling in UI
- Path translator explanation tooltip for users

### Bloodhound Enhancements
- Add more CCM compilation terms as needed (user can request)
- Music video search mode with resolution filter + `charthound-musicvids` qBit category (deferred)
- Tooltip/banner about occasional manual checkmark in qBit

### Tracker Enhancements
- Cutoff upgrade search (search for better quality versions of already-downloaded items)
- Webhook receiver from Radarr/Sonarr (push new missing items instead of polling via sync)
- Per-series search interval override (search weekly shows more frequently)
- Radarr/Sonarr queue awareness (don't re-search items already downloading)
- Tweaks.md note: Radarr/Sonarr alternation reported as uneven (16 Sonarr out of 52 total) — worth investigating

### Veterinarian — Next Priority
- Scheduled auto-refresh for static DB sources — deferred as its own milestone (needs generic jobs/scheduler service, ban-safe jitter, per-source cooldowns). Staleness badge in place meanwhile.
- Dockerfile CA bundle note — base image `python:3.12-slim-trixie` includes ca-certificates but tsort.info and chart2000.com have incomplete HTTPS cert chains that fail in non-browser clients. Worked around by using `http://` for those two hosts in `groomer.py` — safe because data is public and no auth/PII in transit.
- © Colby R. Curtis copyright footer on each tab panel + sidebar
- DB backup/restore tools

### Kennel
- GitHub Sponsors integration once repo is public
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
- 🎯 Tracker must stay ban-safe: jittered intervals, no mode above "Moderate", never expose a configurable base interval
- © "ChartHound", all tab names, "Developed by Colby R. Curtis", Support button + donation modal — protected, never remove
- 📄 README.md exists on GitHub — update it when adding new milestones
- 🆕 **Static DB imports go to `chart_reference_extras` in the dynamic DB, NEVER to the shipped static DB**
- 🆕 **No new API keys required for chart data imports — scrapers and CSV pulls only**

---

## PROJECT FILES (as of Milestone 9)

| File | Location (container) | Purpose |
|---|---|---|
| `main.py` | `/app/app/main.py` | FastAPI app, router registration, lifespan startup |
| `database.py` | `/app/app/database.py` | Schema, init_db, migrations |
| `kennel.py` | `/app/app/routers/kennel.py` | Connection vault, test, path translation |
| `retriever.py` | `/app/app/routers/retriever.py` | Metadata tagging engine |
| `groomer.py` | `/app/app/routers/groomer.py` | Chart data scanner + **6 static DB importers** (M9) |
| `sniffer.py` | `/app/app/routers/sniffer.py` | Chart gap finder, Prowlarr search, qBit grab |
| `bloodhound.py` | `/app/app/routers/bloodhound.py` | MusicBrainz artist/album hunter |
| `tracker.py` | `/app/app/routers/tracker.py` | Radarr/Sonarr missing media hunter |
| `index.html` | `/app/frontend/index.html` | All frontend UI (single-file) |
| `docker-compose_example.yml` | repo root | Template compose file |
| `README.md` | repo root | Project documentation |
| `LICENSE.md` | repo root | Copyright & license terms |

---

## SESSION LEARNINGS — April 24, 2026 (Milestone 9 — Static DB Importers)

**Chart data source architecture:**
- All 6 importers follow the same async pattern: fetch → parse → aggregate (dedupe via UPSERT) → bulk insert into `chart_reference_extras` tagged with a `data_source` string. Registration is a single line in `_STATIC_SOURCES` + one entry in `_IMPORTER_MAP`. Adding a 7th source later is nearly mechanical.
- Snapshot-style imports (Kworb, CCM, UK Official) call `_purge_source(data_tag)` before re-inserting. Historical imports (utdata, Chart2000, tsort) use the UPSERT's `MIN(peak), MAX(weeks)` conflict resolution to preserve best-ever chart data as refreshes layer on.
- Status page query UNIONs across `chart_reference` (static) and `chart_reference_extras` (dynamic) — any new import works in Groomer scans and Sniffer chart-gap-fill with zero downstream code changes.

**HTTPS cert chain gotchas:**
- Both tsort.info and chart2000.com serve incomplete HTTPS certificate chains — the leaf cert is signed correctly but they don't include the intermediate cert in the TLS handshake. Browsers silently fetch missing intermediates via AIA; Python, curl, and most non-browser clients do not. Result: `CERTIFICATE_VERIFY_FAILED` even though `ca-certificates` is installed correctly inside the container.
- Fix: use `http://` for those two hosts. Safe because this is public chart data, no auth, no PII. Mentioned in FUTURE IDEAS as a thing to revisit if/when those hosts fix their cert chains.
- Kworb, Billboard, OfficialCharts all have proper HTTPS cert chains — no workaround needed.

**Scraping strategy — regex vs DOM tree:**
- Regex-based HTML parsing is fragile for any layout that uses generic class names. UK Official was an example — my first attempt targeted `class="title"` / `class="artist"` selectors that didn't match the current redesign.
- Much more resilient: **parse href slugs**. OfficialCharts.com emits `/songs/ARTIST-TITLE/` and `/artist/[ID/]ARTIST/` links in document order for each chart row. Even across multiple redesigns, these URL patterns stay stable because they're the site's information architecture, not presentation.
- De-slugging trick: title_slug = song_slug with artist_slug prefix stripped. Handles ft./feat./& collab artists cleanly because the artist slug itself carries the full collaboration string.

**tsort.info schema discovery:**
- The R package docs referenced column `title`. The actual CSV schema as of version 2-9-0001 has column `name`, not `title`. Easy fix once diagnosed but the parser silently skipped 100% of rows until I inspected the live CSV headers.
- Better move for future scrapers: **always log or surface a sample row + detected columns in the first import run**. The empty-result guard (raise RuntimeError if 0 entries extracted) caught this case cleanly — way better than silent success.
- tsort's `notes` column carries the actual chart peak: `"US Songs 2014-23 peak 94 - Mar 2019 (2 weeks)"`. Regex `peak (\d+)` and `\((\d+) weeks?\)` extract both cleanly. Much better data than score-based bucketing.

**Kworb DOM was simple — I read the wrong cell:**
- Kworb's iTunes pages have 3 `<td>` per row: `[rank, change_indicator, artist-title]`. My first attempt read cell[1] (the change indicator like `"+1"` or `"NEW"`) instead of cell[2]. One-character fix in a code review, but the "0 entries" error made it obvious what to look for.

**Staleness indicator design:**
- Chose 30-day threshold for the `⚠ STALE` badge. Short enough to remind users of weekly-current sources (Kworb/CCM/UK), long enough not to nag historical-only sources that truly never need refresh (tsort, Chart2000 are basically static).
- Showing a compact date (`Apr 24`) next to entry count is subtle enough to ignore when fresh but obvious when you come back to it months later.

**Scroll-nav consistency:**
- Crimson tracker scroll nav was the odd one out — always-visible, panel-appended, different color from Retriever/Groomer. Rewrote to match the existing pattern (body-appended, gated on `panel-tracker.active` and `scrollTop > 200`, subdued gray `var(--muted)` on `var(--surface2)`).
- Worth auditing: do any other tabs' scroll navs drift from this pattern? If yes, make the pattern a helper so all 5 share one function.

**Session closing file inventory:**
- This session modified: `groomer.py` (+668 lines for importers + helpers), `index.html` (+19 lines net: removed 5 placeholder rows, added staleness UI, rebuilt tracker scroll nav).
- Neither `tracker.py` nor `main.py` needed touching — the existing Veterinarian endpoint wiring (`/api/groomer/vet/static-sources/*`) already registered with no changes needed.
