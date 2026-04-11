"""
ChartHound — Database Layer
SQLite schema: Artists > Albums > Tracks (relational, under 200MB target).
Image paths stored — never blobs. MBIDs and file hashes included (Suggestions.txt spec).

M5 Addition: chart_reference table — local Billboard/UK chart history database.
             Loaded once from free CSV/GitHub sources. No API key needed.
             Used by The Groomer for real chart lookups instead of Last.fm guessing.
"""

import aiosqlite
import logging
from app.config import get_settings

log = logging.getLogger("charthound.db")
settings = get_settings()


# ══════════════════════════════════════════════════════════════════════════════
#  SCHEMA
# ══════════════════════════════════════════════════════════════════════════════

SCHEMA = """
-- ── USERS (Security bootstrap — Constitution §4) ──────────────────────────
CREATE TABLE IF NOT EXISTS users (
    user_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    username    TEXT    NOT NULL UNIQUE,
    -- bcrypt hash. Never store plaintext.
    password_hash TEXT  NOT NULL,
    is_admin    INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT    NOT NULL DEFAULT (datetime('now'))
);

-- ── ENCRYPTED CONNECTIONS (Kennel tab) ────────────────────────────────────
-- All token values stored encrypted via Fernet (SECRET_KEY). Never plaintext.
CREATE TABLE IF NOT EXISTS connections (
    conn_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    service     TEXT    NOT NULL UNIQUE,  -- 'plex', 'emby', 'jellyfin', 'lastfm', 'prowlarr'
    base_url    TEXT,
    -- encrypted token/key blob
    token_enc   TEXT,
    extra_json  TEXT,   -- JSON for any service-specific extras (e.g. machine ID)
    verified_at TEXT,
    updated_at  TEXT    NOT NULL DEFAULT (datetime('now'))
);

-- ── ARTISTS ───────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS artists (
    artist_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT    NOT NULL,
    name_sort   TEXT,
    mbid        TEXT    UNIQUE,    -- MusicBrainz Artist ID
    art_path    TEXT,              -- path to artist image, never a blob
    created_at  TEXT    NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_artists_name ON artists(name COLLATE NOCASE);
CREATE INDEX IF NOT EXISTS idx_artists_mbid ON artists(mbid);

-- ── ALBUMS ────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS albums (
    album_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    artist_id   INTEGER NOT NULL REFERENCES artists(artist_id),
    title       TEXT    NOT NULL,
    title_sort  TEXT,
    year        INTEGER,
    mbid        TEXT    UNIQUE,    -- MusicBrainz Release Group ID
    -- Path to folder.jpg written by the app — never a blob (Constitution §3)
    art_path    TEXT,
    label       TEXT,
    total_tracks INTEGER,
    created_at  TEXT    NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_albums_artist ON albums(artist_id);
CREATE INDEX IF NOT EXISTS idx_albums_mbid   ON albums(mbid);

-- ── TRACKS (Core table) ───────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS tracks (
    track_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    artist_id       INTEGER REFERENCES artists(artist_id),
    album_id        INTEGER REFERENCES albums(album_id),

    -- Identity
    title           TEXT    NOT NULL,
    title_sort      TEXT,
    mbid            TEXT,           -- MusicBrainz Recording ID
    isrc            TEXT,

    -- File (Non-destructive — app NEVER moves/renames files)
    file_path       TEXT    NOT NULL UNIQUE,  -- /music/... Docker path
    file_hash       TEXT,           -- MD5 fingerprint — survives renames
    file_format     TEXT,           -- 'flac', 'mp3', 'm4a', etc.
    file_size_kb    INTEGER,

    -- Media server linkage
    plex_rating_key TEXT,
    emby_id         TEXT,
    jf_id           TEXT,

    -- Metadata (written to physical file via Mutagen — File-First standard)
    year            INTEGER,
    genre_1         TEXT,
    genre_2         TEXT,
    genre_3         TEXT,
    mood_1          TEXT,
    mood_2          TEXT,
    mood_3          TEXT,
    bpm             INTEGER,
    language        TEXT,

    -- Art (path only — Constitution §3)
    art_path        TEXT,

    -- Cache state
    last_scanned    TEXT,           -- when file was last hashed/indexed
    last_updated    TEXT,           -- when metadata was last written
    metadata_source TEXT,           -- 'musicbrainz', 'itunes', 'lastfm', 'manual'

    -- Playback stats (pulled from Plex/Emby)
    play_count      INTEGER DEFAULT 0,
    last_played     TEXT,

    created_at      TEXT    NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_tracks_file_path  ON tracks(file_path);
CREATE INDEX IF NOT EXISTS idx_tracks_file_hash  ON tracks(file_hash);
CREATE INDEX IF NOT EXISTS idx_tracks_mbid       ON tracks(mbid);
CREATE INDEX IF NOT EXISTS idx_tracks_artist     ON tracks(artist_id);
CREATE INDEX IF NOT EXISTS idx_tracks_album      ON tracks(album_id);

-- ── CHART DATA (Peak positions per chart per track) ───────────────────────
-- One row per chart per track. This feeds the COMMENT tag writer.
CREATE TABLE IF NOT EXISTS chart_data (
    chart_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    track_id        INTEGER NOT NULL REFERENCES tracks(track_id) ON DELETE CASCADE,

    -- Chart identity
    -- Codes: hot100 | adultpop | ac | uk | country | rnb | dance | rock |
    --        ccm | ccm-ac | ccm-rock | worship | gospel | sgospel | ugospel | tgospel
    chart_name      TEXT    NOT NULL,
    chart_era       TEXT,           -- '1980s', 'Current', 'All-Time', '1987'

    -- Performance data
    peak_position   INTEGER,        -- 1–100
    weeks_on_chart  INTEGER,        -- 1–52

    -- Derived rating (1–5 stars, from peak_position via peakToStars logic)
    star_rating     INTEGER,

    -- Source confidence
    -- 'high'  = direct match from chart_reference DB (real Billboard data)
    -- 'medium' = fuzzy match from chart_reference DB
    -- 'low'   = estimated from Last.fm listener counts
    confidence      TEXT    DEFAULT 'low',
    listener_count  INTEGER DEFAULT 0,

    -- The formatted string written into the file's COMMENT tag
    -- e.g. "Hot 100: #4 (12 wks) | Adult Pop: #1 (18 wks)"
    comment_string  TEXT,

    fetched_at      TEXT    NOT NULL DEFAULT (datetime('now')),

    UNIQUE(track_id, chart_name)    -- one record per chart per track
);
CREATE INDEX IF NOT EXISTS idx_chart_track ON chart_data(track_id);
CREATE INDEX IF NOT EXISTS idx_chart_name  ON chart_data(chart_name);

-- ── CHART REFERENCE (Local Billboard/UK chart history — The Groomer's source of truth)
-- ─────────────────────────────────────────────────────────────────────────────────────
-- Loaded once from free public datasets. Updated via "Refresh Chart Data" button.
-- One row per chart entry (a song can appear on multiple charts = multiple rows).
-- This table is read-only during scans — never modified by scan logic.
--
-- Chart name codes match chart_data.chart_name:
--   hot100    = Billboard Hot 100 (1958–present)
--   adultpop  = Billboard Adult Pop / Top 40
--   ac        = Billboard Adult Contemporary
--   country   = Billboard Hot Country Songs
--   rnb       = Billboard R&B / Hip-Hop Songs
--   rock      = Billboard Mainstream Rock
--   dance     = Billboard Dance/Electronic Songs
--   uk        = UK Official Singles Chart
--   ccm       = Billboard Christian Songs (CCM, 1996–present)
--   gospel    = Billboard Gospel Songs
--   ccm-ac    = Billboard Christian AC
--   ccm-rock  = Billboard Christian Rock
--
CREATE TABLE IF NOT EXISTS chart_reference (
    ref_id          INTEGER PRIMARY KEY AUTOINCREMENT,

    -- Chart identification
    chart_name      TEXT    NOT NULL,   -- 'hot100', 'ac', 'uk', 'ccm', etc.

    -- Song identity — stored exactly as it appears in the source data
    artist          TEXT    NOT NULL,
    title           TEXT    NOT NULL,

    -- Normalised lowercase versions for fast fuzzy matching
    -- Pre-computed on insert so we don't lowercase on every lookup
    artist_norm     TEXT    NOT NULL,
    title_norm      TEXT    NOT NULL,

    -- Chart performance — REAL data from Billboard/Official Charts
    peak_position   INTEGER NOT NULL,   -- best position achieved, 1 = #1
    weeks_on_chart  INTEGER NOT NULL DEFAULT 1,
    chart_year      INTEGER,            -- year the song first charted

    -- Source tracking
    -- 'hot100_csv'  = loaded from GitHub Hot 100 CSV dataset
    -- 'billboard_scrape' = loaded via billboard-charts Python library
    -- 'uk_csv'      = loaded from Official UK Charts CSV
    -- 'manual'      = manually added
    data_source     TEXT    NOT NULL DEFAULT 'unknown',
    loaded_at       TEXT    NOT NULL DEFAULT (datetime('now')),

    -- Prevent exact duplicates (same song on same chart)
    UNIQUE(chart_name, artist_norm, title_norm)
);

-- Indexes for fast fuzzy lookups during Groomer scan
CREATE INDEX IF NOT EXISTS idx_ref_chart       ON chart_reference(chart_name);
CREATE INDEX IF NOT EXISTS idx_ref_artist_norm ON chart_reference(artist_norm);
CREATE INDEX IF NOT EXISTS idx_ref_title_norm  ON chart_reference(title_norm);
CREATE INDEX IF NOT EXISTS idx_ref_year        ON chart_reference(chart_year);
-- Composite index — the primary lookup pattern is chart + artist + title
CREATE INDEX IF NOT EXISTS idx_ref_lookup      ON chart_reference(chart_name, artist_norm, title_norm);

-- ── CHART REFERENCE META (Tracks which charts have been loaded and when) ───
-- One row per chart source. Used by the UI to show "Hot 100: 340,000 entries
-- last updated April 11 2026" and to know which charts need updating.
CREATE TABLE IF NOT EXISTS chart_reference_meta (
    chart_name      TEXT    PRIMARY KEY,
    display_name    TEXT    NOT NULL,
    entry_count     INTEGER DEFAULT 0,
    first_year      INTEGER,            -- earliest chart year in dataset
    last_year       INTEGER,            -- most recent chart year in dataset
    last_updated    TEXT,               -- ISO datetime of last successful load
    data_source     TEXT,               -- URL or source description
    status          TEXT    DEFAULT 'not_loaded'  -- 'not_loaded'|'loading'|'loaded'|'error'
);

-- Pre-populate with the charts we support so UI can show them even before loading
INSERT OR IGNORE INTO chart_reference_meta
    (chart_name, display_name, data_source, status) VALUES
    ('hot100',   'Billboard Hot 100',              'GitHub: mhollingshead/billboard-hot-100', 'not_loaded'),
    ('ac',       'Billboard Adult Contemporary',    'billboard-charts Python library',          'not_loaded'),
    ('adultpop', 'Billboard Adult Pop / Top 40',   'billboard-charts Python library',          'not_loaded'),
    ('country',  'Billboard Hot Country Songs',    'billboard-charts Python library',          'not_loaded'),
    ('rnb',      'Billboard R&B/Hip-Hop Songs',    'billboard-charts Python library',          'not_loaded'),
    ('rock',     'Billboard Mainstream Rock',      'billboard-charts Python library',          'not_loaded'),
    ('dance',    'Billboard Dance/Electronic',     'billboard-charts Python library',          'not_loaded'),
    ('uk',       'UK Official Singles Chart',      'Official Charts Company CSV',              'not_loaded'),
    ('ccm',      'Billboard Christian Songs',      'billboard-charts Python library',          'not_loaded'),
    ('gospel',   'Billboard Gospel Songs',         'billboard-charts Python library',          'not_loaded'),
    ('ccm-ac',   'Billboard Christian AC',         'billboard-charts Python library',          'not_loaded'),
    ('ccm-rock', 'Billboard Christian Rock',       'billboard-charts Python library',          'not_loaded');

-- ── WRITE LOG (Audit trail for every file tag write) ──────────────────────
-- Lets the user review exactly what was changed and when.
CREATE TABLE IF NOT EXISTS write_log (
    log_id          INTEGER PRIMARY KEY AUTOINCREMENT,
    track_id        INTEGER REFERENCES tracks(track_id),
    file_path       TEXT    NOT NULL,
    field_changed   TEXT    NOT NULL,   -- 'genre', 'mood', 'year', 'comment', etc.
    old_value       TEXT,
    new_value       TEXT,
    write_status    TEXT    NOT NULL,   -- 'success', 'failed', 'skipped'
    error_msg       TEXT,
    written_at      TEXT    NOT NULL DEFAULT (datetime('now'))
);

-- ── SCAN JOBS (For pause/resume on 33k track scans) ───────────────────────
CREATE TABLE IF NOT EXISTS scan_jobs (
    job_id          INTEGER PRIMARY KEY AUTOINCREMENT,
    job_type        TEXT    NOT NULL,   -- 'retriever', 'groomer', 'sniffer', 'index'
    status          TEXT    NOT NULL DEFAULT 'pending',  -- pending|running|paused|done|failed|stopped
    scope           TEXT,               -- 'plex'|'emby'|'jellyfin'|'local' for groomer
    mode            TEXT,               -- 'preview'|'autopilot'|'hybrid'
    total_tracks    INTEGER DEFAULT 0,
    processed       INTEGER DEFAULT 0,
    matched         INTEGER DEFAULT 0,
    failed          INTEGER DEFAULT 0,
    cached          INTEGER DEFAULT 0,
    started_at      TEXT,
    paused_at       TEXT,
    completed_at    TEXT,
    config_json     TEXT    -- serialized job parameters
);

-- ── APP SETTINGS (Key/value store for UI preferences) ─────────────────────
CREATE TABLE IF NOT EXISTS app_settings (
    key     TEXT PRIMARY KEY,
    value   TEXT,
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


# ══════════════════════════════════════════════════════════════════════════════
#  CONNECTION HELPER
# ══════════════════════════════════════════════════════════════════════════════

async def get_db() -> aiosqlite.Connection:
    """Yield an aiosqlite connection with row_factory set."""
    db = await aiosqlite.connect(settings.database_url)
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA journal_mode=WAL")   # Better concurrent read performance
    await db.execute("PRAGMA foreign_keys=ON")
    return db


async def init_db():
    """
    Run on application startup.
    Creates all tables if they do not exist. Safe to call repeatedly.
    Runs column migrations for all milestones.
    """
    log.info(f"Initializing database at {settings.database_url}")
    async with aiosqlite.connect(settings.database_url) as db:
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute("PRAGMA foreign_keys=ON")
        await db.executescript(SCHEMA)
        await db.commit()

        # Migrations — ADD COLUMN is safe to run repeatedly (silently fails if exists)
        migrations = [
            # M4 additions
            "ALTER TABLE tracks ADD COLUMN tag_artist TEXT",
            "ALTER TABLE tracks ADD COLUMN tag_album  TEXT",
            "ALTER TABLE tracks ADD COLUMN confidence TEXT DEFAULT 'low'",
            "ALTER TABLE scan_jobs ADD COLUMN paused_at TEXT",
            # M5 additions — Groomer hybrid scan columns
            "ALTER TABLE scan_jobs ADD COLUMN scope TEXT",
            "ALTER TABLE scan_jobs ADD COLUMN mode TEXT",
            "ALTER TABLE scan_jobs ADD COLUMN cached INTEGER DEFAULT 0",
        ]
        for sql in migrations:
            try:
                await db.execute(sql)
                await db.commit()
            except Exception:
                pass  # Column already exists — safe to ignore

    log.info("Database ready.")


async def get_user_count() -> int:
    """Returns number of registered users. Used for bootstrap lock logic."""
    async with aiosqlite.connect(settings.database_url) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT COUNT(*) FROM users")
        row = await cursor.fetchone()
        return row[0] if row else 0
