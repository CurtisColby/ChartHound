# © 2026 Colby R. Curtis | ChartHound: The New World
# All Rights Reserved.
"""
ChartHound — The Tracker Router
Radarr / Sonarr Missing Media Hunter

Syncs missing movies and TV episodes from Radarr/Sonarr into a local queue,
then fires search commands back to Radarr/Sonarr at a controlled pace.
ChartHound never touches Prowlarr — Radarr/Sonarr handle their own indexer
searches, categories, and download client handoff internally.

Endpoints:
  GET  /api/tracker/status         — Current state (on/off, stats, settings)
  POST /api/tracker/toggle         — Turn hunting on/off
  POST /api/tracker/sync           — Pull missing lists from Radarr/Sonarr
  POST /api/tracker/search-now     — Manual search for a specific item
  POST /api/tracker/skip           — Skip an item (stop auto-searching)
  POST /api/tracker/unskip         — Un-skip an item
  POST /api/tracker/skip-season    — Skip a stuck season (allow later seasons)
  POST /api/tracker/unskip-season  — Un-skip a season
  GET  /api/tracker/items          — Paginated item list
  GET  /api/tracker/log            — Activity log
  POST /api/tracker/clear-log      — Clear activity log
  POST /api/tracker/settings       — Update settings (interval, cooldown, etc.)
"""

# © 2026 Colby R. Curtis | ChartHound: The New World — All Rights Reserved.

import asyncio
import json
import logging
import random
import time
from datetime import datetime, timezone, timedelta
from typing import Optional

import aiosqlite
import httpx

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.config import get_settings
from app.deps import require_auth
from app.security import decrypt_token

log      = logging.getLogger("charthound.tracker")
router   = APIRouter(prefix="/api/tracker", tags=["tracker"])
settings = get_settings()

_DYNAMIC_DB = getattr(settings, "database_url", "/data/charthound.db")

# Background loop handle — so we can cancel on toggle-off
_hunt_task: Optional[asyncio.Task] = None
_hunt_running = False

# ── Defaults & Mode Presets ──
_LOG_CAP = 5000

# Two modes. Base interval in seconds; jitter is ±50% of base.
# Gentle ≈ 30–90 min between searches, Moderate ≈ 10–30 min between searches.
_MODES = {
    "gentle":   {"base_interval": 3600, "max_daily": 20},
    "moderate": {"base_interval": 1200, "max_daily": 60},
}
_DEFAULT_MODE     = "gentle"
_DEFAULT_COOLDOWN = 7
_JITTER_PCT       = 0.5    # ±50% randomization on every sleep
_FLOOR_INTERVAL   = 300    # absolute minimum sleep (5 min) — safety net


async def _get_cooldown_days(source: str) -> int:
    """
    Return cooldown-days for the given source ('radarr' or 'sonarr').
    Per-source keys take precedence; falls back to legacy single key, then default.
    Clamped 1–30.
    """
    per_key = f"tracker_cooldown_{source}"
    v = await _get_setting(per_key, "")
    if not v or not v.isdigit():
        # Legacy single-key fallback (pre-split behaviour)
        v = await _get_setting("tracker_cooldown", str(_DEFAULT_COOLDOWN))
    try:
        n = int(v)
    except (TypeError, ValueError):
        n = _DEFAULT_COOLDOWN
    return max(1, min(30, n))


async def _migrate_cooldown_setting():
    """
    One-time migration: if legacy `tracker_cooldown` exists but per-source keys
    don't, copy the legacy value into both per-source keys. Idempotent.
    """
    try:
        legacy = await _get_setting("tracker_cooldown", "")
        radarr = await _get_setting("tracker_cooldown_radarr", "")
        sonarr = await _get_setting("tracker_cooldown_sonarr", "")
        if legacy and legacy.isdigit():
            if not radarr:
                await _set_setting("tracker_cooldown_radarr", legacy)
            if not sonarr:
                await _set_setting("tracker_cooldown_sonarr", legacy)
        else:
            # No legacy — seed defaults if missing (first-run users)
            if not radarr:
                await _set_setting("tracker_cooldown_radarr", str(_DEFAULT_COOLDOWN))
            if not sonarr:
                await _set_setting("tracker_cooldown_sonarr", str(_DEFAULT_COOLDOWN))
    except Exception as e:
        log.warning(f"cooldown migration warning: {e}")

# ══════════════════════════════════════════════════════════════════════════════
#  DB MIGRATIONS — run on import (safe to repeat)
# ══════════════════════════════════════════════════════════════════════════════

async def _ensure_tracker_tables():
    """Create tracker tables if they don't exist. Safe to call repeatedly."""
    async with aiosqlite.connect(_DYNAMIC_DB) as db:
        await db.execute("PRAGMA journal_mode=WAL")
        await db.executescript("""
            CREATE TABLE IF NOT EXISTS tracker_items (
                item_id         INTEGER PRIMARY KEY AUTOINCREMENT,
                source          TEXT    NOT NULL,       -- 'radarr' or 'sonarr'
                external_id     INTEGER NOT NULL,       -- Radarr movieId or Sonarr episodeId
                series_id       INTEGER,                -- Sonarr seriesId (NULL for movies)
                title           TEXT    NOT NULL,
                year            INTEGER,
                season_number   INTEGER,                -- NULL for movies
                episode_number  INTEGER,                -- NULL for movies & season searches
                search_type     TEXT    NOT NULL,        -- 'movie','season','episode'
                status          TEXT    NOT NULL DEFAULT 'missing',  -- missing|searching|found|skipped
                last_searched   TEXT,
                search_count    INTEGER DEFAULT 0,
                cooldown_until  TEXT,
                skipped_season  INTEGER DEFAULT 0,       -- 1 = user skipped this season
                added_at        TEXT    NOT NULL DEFAULT (datetime('now')),
                UNIQUE(source, external_id, search_type)
            );
            CREATE INDEX IF NOT EXISTS idx_tracker_source   ON tracker_items(source);
            CREATE INDEX IF NOT EXISTS idx_tracker_status   ON tracker_items(status);
            CREATE INDEX IF NOT EXISTS idx_tracker_series   ON tracker_items(series_id);
            CREATE INDEX IF NOT EXISTS idx_tracker_cooldown ON tracker_items(cooldown_until);

            CREATE TABLE IF NOT EXISTS tracker_log (
                log_id      INTEGER PRIMARY KEY AUTOINCREMENT,
                source      TEXT,
                title       TEXT,
                action      TEXT    NOT NULL,    -- sync|search|found|skip|error|toggle
                detail      TEXT,
                created_at  TEXT    NOT NULL DEFAULT (datetime('now'))
            );
            CREATE INDEX IF NOT EXISTS idx_tlog_created ON tracker_log(created_at);
        """)
        # Additive migration — add release_date column if missing
        try:
            await db.execute("ALTER TABLE tracker_items ADD COLUMN release_date TEXT")
        except Exception:
            pass  # column already exists
        await db.execute("CREATE INDEX IF NOT EXISTS idx_tracker_release ON tracker_items(release_date)")
        await db.commit()
    log.info("Tracker tables ready.")


# ══════════════════════════════════════════════════════════════════════════════
#  HELPERS
# ══════════════════════════════════════════════════════════════════════════════

async def _get_connection(service: str) -> dict:
    """Get decrypted connection info from Kennel. Raises HTTPException if missing."""
    try:
        async with aiosqlite.connect(_DYNAMIC_DB) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT base_url, token_enc, extra_json FROM connections WHERE service=?",
                (service,)
            ) as cur:
                row = await cur.fetchone()
    except Exception:
        row = None
    if not row:
        raise HTTPException(400, f"No {service} connection configured in The Kennel.")
    return {
        "base_url": row["base_url"],
        "token":    decrypt_token(row["token_enc"]) if row["token_enc"] else "",
        "extra":    json.loads(row["extra_json"] or "{}") if row["extra_json"] else {},
    }


async def _get_setting(key: str, default: str = "") -> str:
    try:
        async with aiosqlite.connect(_DYNAMIC_DB) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT value FROM app_settings WHERE key=?", (key,)
            ) as cur:
                row = await cur.fetchone()
                return row["value"] if row else default
    except Exception:
        return default


async def _set_setting(key: str, value: str):
    async with aiosqlite.connect(_DYNAMIC_DB) as db:
        await db.execute(
            """INSERT INTO app_settings (key, value, updated_at) VALUES (?, ?, datetime('now'))
               ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at""",
            (key, value)
        )
        await db.commit()


async def _add_log(source: str, title: str, action: str, detail: str = ""):
    """Write to tracker_log, pruning oldest if over cap."""
    try:
        async with aiosqlite.connect(_DYNAMIC_DB) as db:
            await db.execute(
                "INSERT INTO tracker_log (source, title, action, detail) VALUES (?,?,?,?)",
                (source, title, action, detail)
            )
            # Prune oldest beyond cap
            await db.execute(f"""
                DELETE FROM tracker_log WHERE log_id NOT IN (
                    SELECT log_id FROM tracker_log ORDER BY created_at DESC LIMIT {_LOG_CAP}
                )
            """)
            await db.commit()
    except Exception as e:
        log.warning(f"Tracker log write failed: {e}")


async def _count_searches_today() -> int:
    """Count how many search actions logged in the last 24h."""
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    try:
        async with aiosqlite.connect(_DYNAMIC_DB) as db:
            async with db.execute(
                "SELECT COUNT(*) FROM tracker_log WHERE action='search' AND created_at>=?",
                (cutoff,)
            ) as cur:
                row = await cur.fetchone()
                return row[0] if row else 0
    except Exception:
        return 0


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


async def _get_mode_config() -> dict:
    """Read current mode from settings and return its config dict."""
    mode = await _get_setting("tracker_mode", _DEFAULT_MODE)
    if mode not in _MODES:
        mode = _DEFAULT_MODE
    cfg = dict(_MODES[mode])
    cfg["mode"] = mode
    return cfg


def _jittered_sleep(base_seconds: int) -> int:
    """Return base ± JITTER_PCT%, floored at _FLOOR_INTERVAL."""
    multiplier = 1.0 + random.uniform(-_JITTER_PCT, _JITTER_PCT)
    sleep_for = max(_FLOOR_INTERVAL, int(base_seconds * multiplier))
    return sleep_for


# ══════════════════════════════════════════════════════════════════════════════
#  REQUEST MODELS
# ══════════════════════════════════════════════════════════════════════════════

class ItemIdRequest(BaseModel):
    item_id: int

class SkipSeasonRequest(BaseModel):
    series_id: int
    season_number: int

class SettingsRequest(BaseModel):
    mode:                   Optional[str]  = None   # 'gentle' or 'moderate'
    cooldown_days:          Optional[int]  = None   # back-compat — writes BOTH per-source keys
    cooldown_days_radarr:   Optional[int]  = None
    cooldown_days_sonarr:   Optional[int]  = None
    max_daily:              Optional[int]  = None
    logging_enabled:        Optional[bool] = None


# ══════════════════════════════════════════════════════════════════════════════
#  STARTUP — called from main.py lifespan
# ══════════════════════════════════════════════════════════════════════════════

async def tracker_startup():
    """Initialize tables and resume hunt loop if it was enabled."""
    await _ensure_tracker_tables()
    await _migrate_cooldown_setting()
    enabled = await _get_setting("tracker_enabled", "false")
    if enabled == "true":
        log.info("Tracker was enabled — resuming hunt loop.")
        _start_hunt_loop()
    else:
        log.info("Tracker is OFF (default). Enable from The Tracker tab.")


# ══════════════════════════════════════════════════════════════════════════════
#  SYNC — Pull missing items from Radarr/Sonarr
# ══════════════════════════════════════════════════════════════════════════════

@router.post("/sync")
async def sync_missing(user: dict = Depends(require_auth)):
    """Pull current missing lists from Radarr and Sonarr, update local table."""
    radarr_count = 0
    sonarr_count = 0
    errors = []

    # ── RADARR ──
    try:
        conn = await _get_connection("radarr")
        base = conn["base_url"].rstrip("/")
        token = conn["token"]
        async with httpx.AsyncClient(timeout=30.0, verify=False) as client:
            r = await client.get(
                f"{base}/api/v3/movie",
                headers={"X-Api-Key": token}
            )
            r.raise_for_status()
            movies = r.json()

        async with aiosqlite.connect(_DYNAMIC_DB) as db:
            # Mark all current radarr items as potentially stale
            existing_ext_ids = set()
            async with db.execute(
                "SELECT external_id FROM tracker_items WHERE source='radarr' AND search_type='movie'"
            ) as cur:
                async for row in cur:
                    existing_ext_ids.add(row[0])

            for m in movies:
                if not m.get("monitored", False):
                    continue
                has_file = m.get("hasFile", False)
                movie_id = m.get("id")
                title = m.get("title", "Unknown")
                year = m.get("year", 0)

                # Release date — prefer physical, then digital, then cinema
                rel = (m.get("physicalRelease")
                       or m.get("digitalRelease")
                       or m.get("inCinemas")
                       or "")
                rel_date = rel[:10] if rel else None

                if not has_file:
                    # Missing — upsert
                    if movie_id not in existing_ext_ids:
                        await db.execute(
                            """INSERT OR IGNORE INTO tracker_items
                               (source, external_id, title, year, search_type, status, release_date)
                               VALUES ('radarr', ?, ?, ?, 'movie', 'missing', ?)""",
                            (movie_id, title, year, rel_date)
                        )
                        radarr_count += 1
                    else:
                        # Keep release_date fresh on existing rows
                        await db.execute(
                            """UPDATE tracker_items SET release_date=?
                               WHERE source='radarr' AND external_id=? AND search_type='movie'""",
                            (rel_date, movie_id)
                        )
                    existing_ext_ids.discard(movie_id)
                else:
                    # Found — mark if it was in our table
                    if movie_id in existing_ext_ids:
                        await db.execute(
                            "UPDATE tracker_items SET status='found' WHERE source='radarr' AND external_id=? AND search_type='movie'",
                            (movie_id,)
                        )
                        existing_ext_ids.discard(movie_id)

            # Clean up items that are no longer in Radarr at all (removed by user)
            for orphan_id in existing_ext_ids:
                await db.execute(
                    "DELETE FROM tracker_items WHERE source='radarr' AND external_id=? AND search_type='movie'",
                    (orphan_id,)
                )

            await db.commit()

    except HTTPException:
        errors.append("Radarr not configured in The Kennel")
    except Exception as e:
        errors.append(f"Radarr sync error: {str(e)[:200]}")

    # ── SONARR ──
    try:
        conn = await _get_connection("sonarr")
        base = conn["base_url"].rstrip("/")
        token = conn["token"]
        async with httpx.AsyncClient(timeout=30.0, verify=False) as client:
            # Get all series
            r = await client.get(
                f"{base}/api/v3/series",
                headers={"X-Api-Key": token}
            )
            r.raise_for_status()
            all_series = r.json()

        # For each monitored series, get episodes
        async with httpx.AsyncClient(timeout=30.0, verify=False) as client:
            async with aiosqlite.connect(_DYNAMIC_DB) as db:
                existing_ep_ids = set()
                async with db.execute(
                    "SELECT external_id FROM tracker_items WHERE source='sonarr'"
                ) as cur:
                    async for row in cur:
                        existing_ep_ids.add(row[0])

                for series in all_series:
                    if not series.get("monitored", False):
                        continue
                    series_id = series.get("id")
                    series_title = series.get("title", "Unknown")

                    # Get episodes for this series
                    try:
                        er = await client.get(
                            f"{base}/api/v3/episode",
                            params={"seriesId": series_id},
                            headers={"X-Api-Key": token}
                        )
                        er.raise_for_status()
                        episodes = er.json()
                    except Exception:
                        continue

                    for ep in episodes:
                        if not ep.get("monitored", False):
                            continue
                        has_file = ep.get("hasFile", False)
                        ep_id = ep.get("id")
                        season_num = ep.get("seasonNumber", 0)
                        ep_num = ep.get("episodeNumber", 0)
                        ep_title = ep.get("title", "")

                        # Skip specials (season 0)
                        if season_num == 0:
                            continue

                        # Air date — prefer UTC, fall back to airDate string
                        air = ep.get("airDateUtc") or ep.get("airDate") or ""
                        rel_date = air[:10] if air else None

                        full_title = f"{series_title} S{season_num:02d}E{ep_num:02d}"
                        if ep_title:
                            full_title += f" - {ep_title}"

                        if not has_file:
                            if ep_id not in existing_ep_ids:
                                await db.execute(
                                    """INSERT OR IGNORE INTO tracker_items
                                       (source, external_id, series_id, title, season_number,
                                        episode_number, search_type, status, release_date)
                                       VALUES ('sonarr', ?, ?, ?, ?, ?, 'episode', 'missing', ?)""",
                                    (ep_id, series_id, full_title, season_num, ep_num, rel_date)
                                )
                                sonarr_count += 1
                            else:
                                await db.execute(
                                    """UPDATE tracker_items SET release_date=?
                                       WHERE source='sonarr' AND external_id=?""",
                                    (rel_date, ep_id)
                                )
                            existing_ep_ids.discard(ep_id)
                        else:
                            if ep_id in existing_ep_ids:
                                await db.execute(
                                    "UPDATE tracker_items SET status='found' WHERE source='sonarr' AND external_id=?",
                                    (ep_id,)
                                )
                                existing_ep_ids.discard(ep_id)

                # Remove orphans that are no longer in Sonarr
                for orphan_id in existing_ep_ids:
                    await db.execute(
                        "DELETE FROM tracker_items WHERE source='sonarr' AND external_id=?",
                        (orphan_id,)
                    )

                await db.commit()

    except HTTPException:
        errors.append("Sonarr not configured in The Kennel")
    except Exception as e:
        errors.append(f"Sonarr sync error: {str(e)[:200]}")

    detail = f"Radarr: +{radarr_count} new missing. Sonarr: +{sonarr_count} new missing."
    if errors:
        detail += " Errors: " + "; ".join(errors)

    await _add_log("system", "Sync", "sync", detail)
    log.info(f"Tracker sync: {detail}")

    return {"ok": True, "radarr_new": radarr_count, "sonarr_new": sonarr_count,
            "errors": errors, "detail": detail}


# ══════════════════════════════════════════════════════════════════════════════
#  TOGGLE — Turn the hunt loop on/off
# ══════════════════════════════════════════════════════════════════════════════

@router.post("/toggle")
async def toggle_tracker(user: dict = Depends(require_auth)):
    global _hunt_task, _hunt_running
    current = await _get_setting("tracker_enabled", "false")
    if current == "true":
        # Turn off
        await _set_setting("tracker_enabled", "false")
        _hunt_running = False
        if _hunt_task and not _hunt_task.done():
            _hunt_task.cancel()
            _hunt_task = None
        await _add_log("system", "Tracker", "toggle", "Tracker turned OFF")
        log.info("Tracker turned OFF")
        return {"ok": True, "enabled": False}
    else:
        # Turn on
        await _set_setting("tracker_enabled", "true")
        _start_hunt_loop()
        await _add_log("system", "Tracker", "toggle", "Tracker turned ON")
        log.info("Tracker turned ON")
        return {"ok": True, "enabled": True}


def _start_hunt_loop():
    global _hunt_task, _hunt_running
    if _hunt_task and not _hunt_task.done():
        return  # Already running
    _hunt_running = True
    _hunt_task = asyncio.create_task(_hunt_loop())


# ══════════════════════════════════════════════════════════════════════════════
#  HUNT LOOP — Background task that picks & searches missing items
# ══════════════════════════════════════════════════════════════════════════════

async def _hunt_loop():
    """
    Background loop. Alternates Radarr ↔ Sonarr each tick. Uses jittered sleep
    based on the current mode (gentle / moderate). Skips unreleased items.
    """
    global _hunt_running
    log.info("Hunt loop started.")

    # Small initial delay so startup settles
    await asyncio.sleep(5)

    # Alternator: 0 = try radarr first this tick, 1 = try sonarr first
    alt_toggle = 0

    while _hunt_running:
        try:
            # Check if still enabled
            enabled = await _get_setting("tracker_enabled", "false")
            if enabled != "true":
                _hunt_running = False
                break

            cfg = await _get_mode_config()
            # Allow user to override max_daily; otherwise use mode default
            max_daily_override = await _get_setting("tracker_max_daily", "")
            max_daily = int(max_daily_override) if max_daily_override.isdigit() else cfg["max_daily"]

            # Check daily cap
            today_count = await _count_searches_today()
            if today_count >= max_daily:
                log.info(f"Tracker: daily cap reached ({today_count}/{max_daily}), sleeping 1h")
                await asyncio.sleep(3600)
                continue

            # Alternate: try preferred source first, fall back to the other
            preferred = "radarr" if alt_toggle == 0 else "sonarr"
            fallback  = "sonarr" if alt_toggle == 0 else "radarr"
            alt_toggle = 1 - alt_toggle

            item = await _pick_next_item(source=preferred)
            if not item:
                item = await _pick_next_item(source=fallback)

            if not item:
                # Nothing searchable — sleep a jittered interval and try again
                sleep_for = _jittered_sleep(cfg["base_interval"])
                log.debug(f"Tracker: queue empty, sleeping {sleep_for}s")
                await asyncio.sleep(sleep_for)
                continue

            # Fire the search
            await _execute_search(item)

            # Jittered sleep before next tick
            sleep_for = _jittered_sleep(cfg["base_interval"])
            log.info(f"Tracker: next tick in {sleep_for}s (mode={cfg['mode']})")
            await asyncio.sleep(sleep_for)

        except asyncio.CancelledError:
            log.info("Hunt loop cancelled.")
            break
        except Exception as e:
            log.error(f"Hunt loop error: {e}")
            await asyncio.sleep(60)

    log.info("Hunt loop stopped.")


async def _pick_next_item(source: str) -> Optional[dict]:
    """
    Pick next eligible item for the given source ('radarr' or 'sonarr').

    Ordering:
      1. Never-searched first (search_count ASC — 0 before 1+)
      2. Newest release first (release_date DESC, NULLs last)
      3. Oldest last_searched first (longest-ago gets next shot)

    Filters:
      - status = 'missing'
      - cooldown_until past OR NULL
      - release_date NULL OR <= today  (skip unreleased)
      - For sonarr: earlier-season-blocks-later-season logic preserved
    """
    now_iso   = _now_iso()
    today_ymd = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    async with aiosqlite.connect(_DYNAMIC_DB) as db:
        db.row_factory = aiosqlite.Row

        if source == "radarr":
            async with db.execute("""
                SELECT * FROM tracker_items
                WHERE source='radarr' AND status='missing'
                  AND (cooldown_until IS NULL OR cooldown_until <= ?)
                  AND (release_date IS NULL OR release_date <= ?)
                ORDER BY
                    search_count ASC,
                    CASE WHEN release_date IS NULL THEN 1 ELSE 0 END,
                    release_date DESC,
                    COALESCE(last_searched, '0000') ASC
                LIMIT 1
            """, (now_iso, today_ymd)) as cur:
                row = await cur.fetchone()
                return dict(row) if row else None

        # Sonarr — respect season-blocking
        async with db.execute("""
            SELECT * FROM tracker_items
            WHERE source='sonarr' AND status='missing'
              AND (cooldown_until IS NULL OR cooldown_until <= ?)
              AND (release_date IS NULL OR release_date <= ?)
            ORDER BY
                search_count ASC,
                CASE WHEN release_date IS NULL THEN 1 ELSE 0 END,
                release_date DESC,
                COALESCE(last_searched, '0000') ASC
        """, (now_iso, today_ymd)) as cur:
            all_eps = [dict(r) async for r in cur]

        if not all_eps:
            return None

        # Group by series to enforce season order within each series
        series_map: dict[int, list] = {}
        for ep in all_eps:
            sid = ep["series_id"]
            series_map.setdefault(sid, []).append(ep)

        # Walk the already-sorted list and return the first ep that isn't
        # blocked by an earlier missing season in its own series
        for ep in all_eps:
            sid = ep["series_id"]
            target_season = ep["season_number"]
            eps_in_series = series_map[sid]

            earlier_missing = [
                e for e in eps_in_series
                if e["season_number"] < target_season
                and e["status"] == "missing"
                and not e.get("skipped_season", 0)
            ]
            if not earlier_missing:
                return ep

        return None


async def _execute_search(item: dict):
    """Fire a search command to Radarr or Sonarr for the given item."""
    source = item["source"]
    item_id = item["item_id"]
    ext_id = item["external_id"]
    title = item["title"]
    cooldown_days = await _get_cooldown_days(source)
    cooldown_until = (datetime.now(timezone.utc) + timedelta(days=cooldown_days)).strftime("%Y-%m-%d %H:%M:%S")

    try:
        conn = await _get_connection(source)
        base = conn["base_url"].rstrip("/")
        token = conn["token"]

        if source == "radarr":
            # MoviesSearch command
            payload = {"name": "MoviesSearch", "movieIds": [ext_id]}
        else:
            # For Sonarr: always emit SeasonSearch and let Sonarr handle
            # the pack-vs-episode decision internally. This avoids brittle
            # count-and-decide logic and an extra Sonarr API round-trip.
            series_id = item.get("series_id")
            season_num = item.get("season_number")
            payload = {"name": "SeasonSearch", "seriesId": series_id, "seasonNumber": season_num}
            log.info(f"Tracker: SeasonSearch for {title} (season {season_num})")

        # Fire the command
        async with httpx.AsyncClient(timeout=20.0, verify=False) as client:
            r = await client.post(
                f"{base}/api/v3/command",
                json=payload,
                headers={"X-Api-Key": token, "Content-Type": "application/json"}
            )
            r.raise_for_status()

        # Update item
        async with aiosqlite.connect(_DYNAMIC_DB) as db:
            if source == "sonarr" and payload.get("name") == "SeasonSearch":
                # Mark all episodes in this season as searched
                await db.execute(
                    """UPDATE tracker_items
                       SET last_searched=?, search_count=search_count+1,
                           cooldown_until=?, status='missing'
                       WHERE source='sonarr' AND series_id=? AND season_number=?
                       AND status='missing'""",
                    (_now_iso(), cooldown_until, item["series_id"], item["season_number"])
                )
            else:
                await db.execute(
                    """UPDATE tracker_items
                       SET last_searched=?, search_count=search_count+1,
                           cooldown_until=?
                       WHERE item_id=?""",
                    (_now_iso(), cooldown_until, item_id)
                )
            await db.commit()

        cmd_name = payload.get("name", "Search")
        logging_on = await _get_setting("tracker_logging", "true")
        if logging_on == "true":
            await _add_log(source, title, "search", f"{cmd_name} fired → {source.title()} handling download")
        log.info(f"Tracker: {cmd_name} for '{title}' (ext_id={ext_id})")

    except HTTPException as e:
        await _add_log(source, title, "error", f"Connection error: {e.detail}")
        log.warning(f"Tracker search error for '{title}': {e.detail}")
    except Exception as e:
        await _add_log(source, title, "error", str(e)[:300])
        log.warning(f"Tracker search error for '{title}': {e}")


# ══════════════════════════════════════════════════════════════════════════════
#  STATUS
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/status")
async def tracker_status(user: dict = Depends(require_auth)):
    enabled = await _get_setting("tracker_enabled", "false") == "true"
    cfg = await _get_mode_config()
    cooldown_radarr = await _get_cooldown_days("radarr")
    cooldown_sonarr = await _get_cooldown_days("sonarr")
    max_daily_override = await _get_setting("tracker_max_daily", "")
    max_daily = int(max_daily_override) if max_daily_override.isdigit() else cfg["max_daily"]
    logging_on = await _get_setting("tracker_logging", "true") == "true"
    today_count = await _count_searches_today()

    # Counts
    stats = {"movies_missing": 0, "movies_found": 0, "tv_missing": 0, "tv_found": 0,
             "skipped": 0, "total": 0}
    try:
        async with aiosqlite.connect(_DYNAMIC_DB) as db:
            async with db.execute(
                "SELECT source, status, COUNT(*) FROM tracker_items GROUP BY source, status"
            ) as cur:
                async for row in cur:
                    src, st, ct = row[0], row[1], row[2]
                    stats["total"] += ct
                    if src == "radarr":
                        if st == "missing": stats["movies_missing"] += ct
                        elif st == "found": stats["movies_found"] += ct
                        elif st == "skipped": stats["skipped"] += ct
                    else:
                        if st == "missing": stats["tv_missing"] += ct
                        elif st == "found": stats["tv_found"] += ct
                        elif st == "skipped": stats["skipped"] += ct
    except Exception:
        pass

    # Check connections
    radarr_ok = False
    sonarr_ok = False
    try:
        async with aiosqlite.connect(_DYNAMIC_DB) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT service, verified_at FROM connections WHERE service IN ('radarr','sonarr')"
            ) as cur:
                async for row in cur:
                    if row["service"] == "radarr" and row["verified_at"]:
                        radarr_ok = True
                    elif row["service"] == "sonarr" and row["verified_at"]:
                        sonarr_ok = True
    except Exception:
        pass

    return {
        "enabled": enabled,
        "hunt_loop_active": _hunt_running,
        "mode": cfg["mode"],
        "base_interval": cfg["base_interval"],
        "cooldown_days":         cooldown_radarr,   # back-compat: old UI binds to this
        "cooldown_days_radarr":  cooldown_radarr,
        "cooldown_days_sonarr":  cooldown_sonarr,
        "max_daily": max_daily,
        "searches_today": today_count,
        "logging_enabled": logging_on,
        "radarr_connected": radarr_ok,
        "sonarr_connected": sonarr_ok,
        **stats,
    }


# ══════════════════════════════════════════════════════════════════════════════
#  ITEMS — Paginated list
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/items")
async def get_items(
    source: Optional[str] = None,
    status: Optional[str] = None,
    limit: int = 200,
    offset: int = 0,
    user: dict = Depends(require_auth)
):
    conds, params = [], []
    if source:
        conds.append("source=?"); params.append(source)
    if status:
        conds.append("status=?"); params.append(status)

    where = ("WHERE " + " AND ".join(conds)) if conds else ""

    async with aiosqlite.connect(_DYNAMIC_DB) as db:
        db.row_factory = aiosqlite.Row

        async with db.execute(
            f"SELECT COUNT(*) FROM tracker_items {where}", params
        ) as cur:
            total = (await cur.fetchone())[0]

        async with db.execute(f"""
            SELECT * FROM tracker_items {where}
            ORDER BY
                CASE status
                    WHEN 'missing' THEN 0
                    WHEN 'searching' THEN 1
                    WHEN 'skipped' THEN 2
                    WHEN 'found' THEN 3
                END,
                source ASC,
                season_number ASC,
                episode_number ASC,
                added_at ASC
            LIMIT ? OFFSET ?
        """, params + [limit, offset]) as cur:
            items = [dict(r) async for r in cur]

    return {"items": items, "total": total, "offset": offset, "limit": limit}


# ══════════════════════════════════════════════════════════════════════════════
#  MANUAL ACTIONS
# ══════════════════════════════════════════════════════════════════════════════

@router.post("/search-now")
async def search_now(req: ItemIdRequest, user: dict = Depends(require_auth)):
    """Manual search for a specific item — jumps the queue."""
    async with aiosqlite.connect(_DYNAMIC_DB) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM tracker_items WHERE item_id=?", (req.item_id,)
        ) as cur:
            row = await cur.fetchone()
    if not row:
        raise HTTPException(404, "Item not found.")
    item = dict(row)

    # Clear cooldown so it runs immediately
    async with aiosqlite.connect(_DYNAMIC_DB) as db:
        await db.execute(
            "UPDATE tracker_items SET cooldown_until=NULL, status='missing' WHERE item_id=?",
            (req.item_id,)
        )
        await db.commit()

    # Execute search immediately
    await _execute_search(item)
    return {"ok": True, "message": f"Search fired for '{item['title']}'"}


@router.post("/skip")
async def skip_item(req: ItemIdRequest, user: dict = Depends(require_auth)):
    async with aiosqlite.connect(_DYNAMIC_DB) as db:
        await db.execute(
            "UPDATE tracker_items SET status='skipped' WHERE item_id=?", (req.item_id,)
        )
        await db.commit()
    await _add_log("system", f"item #{req.item_id}", "skip", "User skipped item")
    return {"ok": True}


@router.post("/unskip")
async def unskip_item(req: ItemIdRequest, user: dict = Depends(require_auth)):
    async with aiosqlite.connect(_DYNAMIC_DB) as db:
        await db.execute(
            "UPDATE tracker_items SET status='missing', cooldown_until=NULL WHERE item_id=?",
            (req.item_id,)
        )
        await db.commit()
    return {"ok": True}


@router.post("/skip-season")
async def skip_season(req: SkipSeasonRequest, user: dict = Depends(require_auth)):
    """Skip an entire season — allows later seasons to be searched."""
    async with aiosqlite.connect(_DYNAMIC_DB) as db:
        await db.execute(
            """UPDATE tracker_items SET skipped_season=1, status='skipped'
               WHERE source='sonarr' AND series_id=? AND season_number=?""",
            (req.series_id, req.season_number)
        )
        await db.commit()
    await _add_log("sonarr", f"Series {req.series_id}", "skip",
                   f"User skipped season {req.season_number}")
    return {"ok": True}


@router.post("/unskip-season")
async def unskip_season(req: SkipSeasonRequest, user: dict = Depends(require_auth)):
    async with aiosqlite.connect(_DYNAMIC_DB) as db:
        await db.execute(
            """UPDATE tracker_items SET skipped_season=0, status='missing', cooldown_until=NULL
               WHERE source='sonarr' AND series_id=? AND season_number=?""",
            (req.series_id, req.season_number)
        )
        await db.commit()
    return {"ok": True}


# ══════════════════════════════════════════════════════════════════════════════
#  LOG
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/log")
async def get_log(limit: int = 100, offset: int = 0, user: dict = Depends(require_auth)):
    async with aiosqlite.connect(_DYNAMIC_DB) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM tracker_log ORDER BY created_at DESC LIMIT ? OFFSET ?",
            (limit, offset)
        ) as cur:
            entries = [dict(r) async for r in cur]
    return {"entries": entries, "total": len(entries)}


@router.post("/clear-log")
async def clear_log(user: dict = Depends(require_auth)):
    async with aiosqlite.connect(_DYNAMIC_DB) as db:
        await db.execute("DELETE FROM tracker_log")
        await db.commit()
    return {"ok": True}


# ══════════════════════════════════════════════════════════════════════════════
#  SETTINGS
# ══════════════════════════════════════════════════════════════════════════════

@router.post("/settings")
async def update_settings(req: SettingsRequest, user: dict = Depends(require_auth)):
    if req.mode is not None:
        m = req.mode.strip().lower()
        if m not in _MODES:
            raise HTTPException(400, f"Invalid mode '{m}'. Must be 'gentle' or 'moderate'.")
        await _set_setting("tracker_mode", m)
    # Per-source cooldowns (preferred)
    if req.cooldown_days_radarr is not None:
        v = max(1, min(30, req.cooldown_days_radarr))
        await _set_setting("tracker_cooldown_radarr", str(v))
    if req.cooldown_days_sonarr is not None:
        v = max(1, min(30, req.cooldown_days_sonarr))
        await _set_setting("tracker_cooldown_sonarr", str(v))
    # Back-compat: old UI posts a single `cooldown_days` — apply to both sources
    if req.cooldown_days is not None and req.cooldown_days_radarr is None and req.cooldown_days_sonarr is None:
        v = max(1, min(30, req.cooldown_days))
        await _set_setting("tracker_cooldown_radarr", str(v))
        await _set_setting("tracker_cooldown_sonarr", str(v))
        await _set_setting("tracker_cooldown", str(v))  # keep legacy key synced
    if req.max_daily is not None:
        v = max(5, min(200, req.max_daily))  # Clamp 5–200
        await _set_setting("tracker_max_daily", str(v))
    if req.logging_enabled is not None:
        await _set_setting("tracker_logging", "true" if req.logging_enabled else "false")
    return {"ok": True, "message": "Settings updated."}
