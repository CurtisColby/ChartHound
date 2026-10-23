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
  POST /api/tracker/settings       — Update settings (cooldown, logging)
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
_next_tick_utc: Optional[str] = None   # ISO timestamp of next scheduled search

# ── Defaults ──
_LOG_CAP = 5000

# Fixed interval: 40 minutes base, ±25% jitter → 30–50 min range.
# No more gentle/moderate presets.
_BASE_INTERVAL    = 2400   # 40 minutes in seconds
_DEFAULT_COOLDOWN = 7
_JITTER_PCT       = 0.25   # ±25% randomization on every sleep
_FLOOR_INTERVAL   = 300    # absolute minimum sleep (5 min) — safety net
_MAX_DAILY_SAFETY = 999    # effectively unlimited — safety net only

# Smart priority / backoff
_TIER0_DAYS       = 90     # released within 90d  → top priority, flat cooldown
_TIER1_DAYS       = 730    # released within 2y   → second priority
_MAX_COOLDOWN     = 30     # backoff ceiling (days)
_DORMANT_TRIES    = 6      # old titles with this many misses → monthly only
_TV_WEIGHT        = 3      # TV ticks per movie tick when both have eligible items
_GRAB_CHECK_DELAY = 900    # seconds after a search before checking arr history
_GRAB_HOLD_DAYS   = 3      # cooldown after a confirmed grab (lets download land)
_verify_tasks: set = set()


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
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).strftime("%Y-%m-%d %H:%M:%S")
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


def _tier_bounds() -> tuple:
    now = datetime.now(timezone.utc)
    return ((now - timedelta(days=_TIER0_DAYS)).strftime("%Y-%m-%d"),
            (now - timedelta(days=_TIER1_DAYS)).strftime("%Y-%m-%d"))


def _tier(release_date: Optional[str]) -> int:
    if not release_date:
        return 2
    t0, t1 = _tier_bounds()
    if release_date >= t0:
        return 0
    if release_date >= t1:
        return 1
    return 2


def _backoff_days(base: int, tries: int, release_date: Optional[str]) -> int:
    """Cooldown grows with each miss: base, 2x, 4x, 8x (cap 30d).
    New releases keep the flat base. Old titles with many misses go monthly."""
    tier = _tier(release_date)
    if tier == 0:
        return base
    days = base * (2 ** max(0, min(tries - 1, 3)))
    if tier == 2 and tries >= _DORMANT_TRIES:
        days = _MAX_COOLDOWN
    return max(base, min(_MAX_COOLDOWN, days))


async def _migrate_backoff():
    """One-time: re-space existing cooldowns using the new backoff curve."""
    if await _get_setting("tracker_backoff_v2", "") == "1":
        return
    try:
        base = {"radarr": await _get_cooldown_days("radarr"),
                "sonarr": await _get_cooldown_days("sonarr")}
        updates = []
        async with aiosqlite.connect(_DYNAMIC_DB) as db:
            async with db.execute(
                "SELECT item_id, source, search_count, release_date, last_searched "
                "FROM tracker_items WHERE status='missing' AND last_searched IS NOT NULL"
            ) as cur:
                async for iid, src, cnt, rel, last in cur:
                    try:
                        ls = datetime.strptime(last[:19], "%Y-%m-%d %H:%M:%S")
                    except Exception:
                        continue
                    days = _backoff_days(base.get(src, _DEFAULT_COOLDOWN), cnt or 0, rel)
                    updates.append(((ls + timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S"), iid))
            await db.executemany("UPDATE tracker_items SET cooldown_until=? WHERE item_id=?", updates)
            await db.commit()
        await _set_setting("tracker_backoff_v2", "1")
        log.info(f"Tracker: backoff migration applied to {len(updates)} items")
    except Exception as e:
        log.warning(f"backoff migration warning: {e}")


async def _get_mode_config() -> dict:
    """Return fixed interval config. No more mode presets."""
    return {
        "mode": "smart",
        "base_interval": _BASE_INTERVAL,
        "max_daily": _MAX_DAILY_SAFETY,
    }


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
    cooldown_days:          Optional[int]  = None   # back-compat — writes BOTH per-source keys
    cooldown_days_radarr:   Optional[int]  = None
    cooldown_days_sonarr:   Optional[int]  = None
    logging_enabled:        Optional[bool] = None


# ══════════════════════════════════════════════════════════════════════════════
#  STARTUP — called from main.py lifespan
# ══════════════════════════════════════════════════════════════════════════════

async def tracker_startup():
    """Initialize tables and resume hunt loop if it was enabled."""
    await _ensure_tracker_tables()
    await _migrate_cooldown_setting()
    await _migrate_backoff()
    enabled = await _get_setting("tracker_enabled", "false")
    if enabled == "true":
        log.info("Tracker was enabled — resuming hunt loop.")
        _start_hunt_loop()
    else:
        log.info("Tracker is OFF (default). Enable from The Tracker tab.")


# ══════════════════════════════════════════════════════════════════════════════
#  SYNC — Pull missing items from Radarr/Sonarr
# ══════════════════════════════════════════════════════════════════════════════

_AUTO_SYNC_INTERVAL = 6 * 3600   # 6 hours in seconds
_last_sync_utc: Optional[float] = None   # epoch timestamp of last sync


async def _do_sync() -> dict:
    """Internal sync logic — pull missing lists from Radarr/Sonarr, update local table.
    Returns result dict. Can be called from endpoint or hunt loop."""
    global _last_sync_utc
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
                        # Keep release_date fresh; re-open if it was 'found' but file is gone
                        await db.execute(
                            """UPDATE tracker_items SET release_date=?,
                                   status=CASE WHEN status='found' THEN 'missing' ELSE status END
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
                                    """UPDATE tracker_items SET release_date=?,
                                           status=CASE WHEN status='found' THEN 'missing' ELSE status END
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

    _last_sync_utc = time.time()

    return {"ok": True, "radarr_new": radarr_count, "sonarr_new": sonarr_count,
            "errors": errors, "detail": detail}


@router.post("/sync")
async def sync_missing(user: dict = Depends(require_auth)):
    """Endpoint wrapper — pull current missing lists from Radarr and Sonarr."""
    return await _do_sync()


# ══════════════════════════════════════════════════════════════════════════════
#  TOGGLE — Turn the hunt loop on/off
# ══════════════════════════════════════════════════════════════════════════════

@router.post("/toggle")
async def toggle_tracker(user: dict = Depends(require_auth)):
    global _hunt_task, _hunt_running, _next_tick_utc
    current = await _get_setting("tracker_enabled", "false")
    if current == "true":
        # Turn off
        await _set_setting("tracker_enabled", "false")
        _hunt_running = False
        _next_tick_utc = None
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
    Background loop. Same pace as before (40 min ±25% jitter) — indexer load unchanged.

    Every tick:
      1. Choose source: TV gets 3 ticks per movie tick (TV backlog is far larger).
         If the chosen source has nothing eligible, fall back to the other.
      2. Pick the best eligible item: newest releases first, then fewest tries.
      3. Fire the search, then verify via arr history ~15 min later.
    """
    global _hunt_running, _next_tick_utc
    log.info("Hunt loop started (smart mode, TV-weighted, 40min ±25% jitter).")

    _next_tick_utc = None
    await asyncio.sleep(5)

    tick = 0

    while _hunt_running:
        try:
            enabled = await _get_setting("tracker_enabled", "false")
            if enabled != "true":
                _hunt_running = False
                break

            if _last_sync_utc is None or (time.time() - _last_sync_utc) >= _AUTO_SYNC_INTERVAL:
                log.info("Tracker: auto-sync triggered (every 6h)")
                try:
                    await _do_sync()
                except Exception as e:
                    log.warning(f"Tracker: auto-sync failed: {e}")

            cfg = await _get_mode_config()

            today_count = await _count_searches_today()
            if today_count >= cfg["max_daily"]:
                log.info(f"Tracker: safety cap reached ({today_count}), sleeping 1h")
                _next_tick_utc = (datetime.now(timezone.utc) + timedelta(seconds=3600)).strftime("%Y-%m-%d %H:%M:%S")
                await asyncio.sleep(3600)
                continue

            preferred = "radarr" if tick % (_TV_WEIGHT + 1) == 0 else "sonarr"
            fallback  = "sonarr" if preferred == "radarr" else "radarr"
            tick += 1

            item = await _pick_next_item(source=preferred)
            if not item:
                item = await _pick_next_item(source=fallback)
            if item:
                await _execute_search(item)
            else:
                log.debug("Tracker: nothing eligible on either source, sleeping")

            sleep_for = _jittered_sleep(cfg["base_interval"])
            _next_tick_utc = (datetime.now(timezone.utc) + timedelta(seconds=sleep_for)).strftime("%Y-%m-%d %H:%M:%S")
            log.info(f"Tracker: next tick in {sleep_for}s ({sleep_for // 60}min)")
            await asyncio.sleep(sleep_for)

        except asyncio.CancelledError:
            log.info("Hunt loop cancelled.")
            break
        except Exception as e:
            log.error(f"Hunt loop error: {e}")
            await asyncio.sleep(60)

    _next_tick_utc = None
    log.info("Hunt loop stopped.")


_PICK_SQL = """
    SELECT *,
        CASE WHEN release_date >= ? THEN 0
             WHEN release_date >= ? THEN 1
             ELSE 2 END AS tier
    FROM tracker_items
    WHERE source=? AND status='missing'
      AND (release_date IS NULL OR release_date <= ?)
      AND (cooldown_until IS NULL OR cooldown_until <= ?)
    ORDER BY
        tier ASC,
        CASE WHEN release_date >= ? THEN release_date END DESC,
        search_count ASC,
        release_date DESC,
        COALESCE(last_searched, '0000') ASC
"""


async def _pick_next_item(source: str) -> Optional[dict]:
    """
    Eligible = missing, already released, cooldown expired.
    Order: last 90 days (newest first) → last 2 years (newest first)
           → older (fewest tries, then newest).
    Sonarr: an earlier missing season blocks later ones EXCEPT for the
    series' newest season and anything released in the last 90 days.
    """
    t0, t1 = _tier_bounds()
    today_ymd = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    now = _now_iso()
    args = (t0, t1, source, today_ymd, now, t1)

    async with aiosqlite.connect(_DYNAMIC_DB) as db:
        db.row_factory = aiosqlite.Row

        if source == "radarr":
            async with db.execute(_PICK_SQL + " LIMIT 1", args) as cur:
                row = await cur.fetchone()
                return dict(row) if row else None

        async with db.execute(_PICK_SQL, args) as cur:
            all_eps = [dict(r) async for r in cur]
        if not all_eps:
            return None

        latest: dict = {}
        async with db.execute(
            "SELECT series_id, MAX(season_number) FROM tracker_items "
            "WHERE source='sonarr' GROUP BY series_id"
        ) as cur:
            async for sid, mx in cur:
                latest[sid] = mx

    earliest_missing: dict = {}
    for ep in all_eps:
        if ep.get("skipped_season"):
            continue
        sid = ep["series_id"]
        if sid not in earliest_missing or ep["season_number"] < earliest_missing[sid]:
            earliest_missing[sid] = ep["season_number"]

    for ep in all_eps:
        sid = ep["series_id"]
        sn = ep["season_number"]
        if ep["tier"] == 0 or sn == latest.get(sid) or sn <= earliest_missing.get(sid, sn):
            return ep
    return None


async def _execute_search(item: dict):
    """Fire a search command to Radarr or Sonarr, apply backoff, schedule grab check."""
    source = item["source"]
    item_id = item["item_id"]
    ext_id = item["external_id"]
    title = item["title"]
    tries = (item.get("search_count") or 0) + 1
    base = await _get_cooldown_days(source)
    days = _backoff_days(base, tries, item.get("release_date"))
    cooldown_until = (datetime.now(timezone.utc) + timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    started = datetime.now(timezone.utc)

    try:
        conn = await _get_connection(source)
        base_url = conn["base_url"].rstrip("/")
        token = conn["token"]

        if source == "radarr":
            payload = {"name": "MoviesSearch", "movieIds": [ext_id]}
        else:
            payload = {"name": "SeasonSearch", "seriesId": item.get("series_id"),
                       "seasonNumber": item.get("season_number")}

        async with httpx.AsyncClient(timeout=20.0, verify=False) as client:
            r = await client.post(
                f"{base_url}/api/v3/command",
                json=payload,
                headers={"X-Api-Key": token, "Content-Type": "application/json"}
            )
            r.raise_for_status()

        async with aiosqlite.connect(_DYNAMIC_DB) as db:
            if source == "sonarr":
                await db.execute(
                    """UPDATE tracker_items
                       SET last_searched=?, search_count=search_count+1, cooldown_until=?
                       WHERE source='sonarr' AND series_id=? AND season_number=?
                       AND status='missing'""",
                    (_now_iso(), cooldown_until, item["series_id"], item["season_number"])
                )
            else:
                await db.execute(
                    """UPDATE tracker_items
                       SET last_searched=?, search_count=search_count+1, cooldown_until=?
                       WHERE item_id=?""",
                    (_now_iso(), cooldown_until, item_id)
                )
            await db.commit()

        cmd_name = payload["name"]
        if await _get_setting("tracker_logging", "true") == "true":
            await _add_log(source, title, "search",
                           f"{cmd_name} sent (try {tries}) — checking result in {_GRAB_CHECK_DELAY // 60}min")
        log.info(f"Tracker: {cmd_name} for '{title}' (ext_id={ext_id}, try {tries}, next in {days}d)")

        t = asyncio.create_task(_verify_grab(item, started, days))
        _verify_tasks.add(t)
        t.add_done_callback(_verify_tasks.discard)

    except HTTPException as e:
        await _add_log(source, title, "error", f"Connection error: {e.detail}")
        log.warning(f"Tracker search error for '{title}': {e.detail}")
    except Exception as e:
        await _add_log(source, title, "error", str(e)[:300])
        log.warning(f"Tracker search error for '{title}': {e}")


def _parse_arr_date(s: str) -> Optional[datetime]:
    try:
        d = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except Exception:
        return None


async def _verify_grab(item: dict, started: datetime, next_days: int):
    """Ask Radarr/Sonarr history (local API, no indexer hits) whether the search grabbed anything."""
    await asyncio.sleep(_GRAB_CHECK_DELAY)
    source, title = item["source"], item["title"]
    try:
        conn = await _get_connection(source)
        base_url = conn["base_url"].rstrip("/")
        hdr = {"X-Api-Key": conn["token"]}
        if source == "radarr":
            url, params = f"{base_url}/api/v3/history/movie", {"movieId": item["external_id"]}
        else:
            url, params = f"{base_url}/api/v3/history/series", {
                "seriesId": item["series_id"], "seasonNumber": item["season_number"]}
        async with httpx.AsyncClient(timeout=20.0, verify=False) as client:
            r = await client.get(url, params=params, headers=hdr)
            r.raise_for_status()
            data = r.json()
        records = data.get("records", data) if isinstance(data, dict) else data
        cutoff = started - timedelta(minutes=1)
        grabs = [h for h in (records or [])
                 if str(h.get("eventType", "")).lower() == "grabbed"
                 and (_parse_arr_date(h.get("date", "")) or cutoff) > cutoff]

        logging_on = await _get_setting("tracker_logging", "true") == "true"
        if grabs:
            hold = (datetime.now(timezone.utc) + timedelta(days=_GRAB_HOLD_DAYS)).strftime("%Y-%m-%d %H:%M:%S")
            async with aiosqlite.connect(_DYNAMIC_DB) as db:
                if source == "sonarr":
                    await db.execute(
                        "UPDATE tracker_items SET cooldown_until=? WHERE source='sonarr' "
                        "AND series_id=? AND season_number=? AND status='missing'",
                        (hold, item["series_id"], item["season_number"]))
                else:
                    await db.execute("UPDATE tracker_items SET cooldown_until=? WHERE item_id=?",
                                     (hold, item["item_id"]))
                await db.commit()
            if logging_on:
                rel = grabs[0].get("sourceTitle") or ""
                await _add_log(source, title, "found",
                               f"GRABBED {len(grabs)} release(s){' — ' + rel[:120] if rel else ''}")
        elif logging_on:
            await _add_log(source, title, "miss", f"Nothing grabbed — next try in {next_days}d")
    except Exception as e:
        log.warning(f"Tracker grab-check failed for '{title}': {e}")


# ══════════════════════════════════════════════════════════════════════════════
#  STATUS
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/status")
async def tracker_status(user: dict = Depends(require_auth)):
    enabled = await _get_setting("tracker_enabled", "false") == "true"
    cfg = await _get_mode_config()
    cooldown_radarr = await _get_cooldown_days("radarr")
    cooldown_sonarr = await _get_cooldown_days("sonarr")
    max_daily = cfg["max_daily"]
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

    # Next auto-sync
    next_sync_in = None
    if _last_sync_utc is not None:
        elapsed = time.time() - _last_sync_utc
        remaining = max(0, _AUTO_SYNC_INTERVAL - elapsed)
        next_sync_in = int(remaining)

    return {
        "enabled": enabled,
        "hunt_loop_active": _hunt_running,
        "next_tick_utc": _next_tick_utc,
        "last_sync_utc": datetime.fromtimestamp(_last_sync_utc, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S") if _last_sync_utc else None,
        "next_sync_in_seconds": next_sync_in,
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
    if req.logging_enabled is not None:
        await _set_setting("tracker_logging", "true" if req.logging_enabled else "false")
    return {"ok": True, "message": "Settings updated."}
