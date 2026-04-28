# © 2026 Colby R. Curtis | ChartHound: The New World
# All Rights Reserved.
"""
ChartHound — The Sniffer Router
Chart-Hit Finder & Grabber

Search: Album-first via Torznab per-indexer endpoints (same method as Lidarr).
Download: Add to qBit → background task retries file priority checkmark
          for up to 60 seconds to ensure download starts.

Endpoints:
  POST /api/sniffer/gap-analysis     — Cross-ref static DB vs user library (legacy chart-first)
  POST /api/sniffer/gap-by-genre     — Library-first master list, genre+decade filters
  POST /api/sniffer/trending         — Last.fm trending tracks (paginated)
  POST /api/sniffer/search           — Search Prowlarr (album-first)
  POST /api/sniffer/grab             — Push to qBit + background checkmark
  GET  /api/sniffer/year-range       — Min/max year from static DB
  GET  /api/sniffer/genres           — Distinct genres from tracks table
"""

# © 2026 Colby R. Curtis | ChartHound: The New World — All Rights Reserved.

import asyncio
import hashlib
import json
import logging
import os
import re
import sqlite3
import xml.etree.ElementTree as ET

from datetime import datetime, timezone, timedelta

import aiosqlite
import httpx

from typing import Optional, List
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.config import get_settings
from app.deps import require_auth
from app.security import decrypt_token

from app.routers.groomer import (
    _norm, _fuzzy,
    CHART_DISPLAY, MATCH_THRESHOLD,
)

log      = logging.getLogger("charthound.sniffer")
router   = APIRouter(prefix="/api/sniffer", tags=["sniffer"])
settings = get_settings()

_DYNAMIC_DB = getattr(settings, "database_url",  "/data/charthound.db")
_STATIC_DB  = getattr(settings, "static_db_url", "/data/charthound_static.db")

_AUDIO_CATS = "3000,3010,3030,3040,3050"


# ══════════════════════════════════════════════════════════════════════════════
#  REQUEST MODELS
# ══════════════════════════════════════════════════════════════════════════════

class GapAnalysisRequest(BaseModel):
    charts:    List[str] = ["hot100"]
    min_year:  Optional[int] = None
    max_year:  Optional[int] = None
    min_peak:  int = 1
    max_peak:  int = 40
    limit:     int = 500
    offset:    int = 0

class GapByGenreRequest(BaseModel):
    genres:    List[str] = []                # e.g. ["country","rock"] — empty = no genre filter
    decades:   List[str] = []                # e.g. ["1980s","1990s"] — empty = all decades
    tier:      str       = "notable"         # "essential" | "notable" | "deep"
    limit:     int       = 500
    offset:    int       = 0
    include_owned: bool  = True              # if False, skip tracks user already owns
    bypass_cache:  bool  = False             # set True to force a rebuild

class TrendingRequest(BaseModel):
    tag:       str = "pop"
    limit:     int = 200
    page:      int = 1

class SearchRequest(BaseModel):
    artist:    str
    title:     str
    album:     Optional[str] = None

class GrabRequest(BaseModel):
    download_url:    str
    title:           str
    indexer:         str = ""


# ══════════════════════════════════════════════════════════════════════════════
#  HELPERS
# ══════════════════════════════════════════════════════════════════════════════

async def _get_connection(service: str) -> dict:
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

async def _build_library_index() -> dict:
    library_index: dict = {}
    try:
        async with aiosqlite.connect(_DYNAMIC_DB) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("""
                SELECT t.track_id,
                       LOWER(COALESCE(t.tag_artist, a.name, '')) as artist_name,
                       LOWER(t.title) as title_name
                FROM tracks t
                LEFT JOIN artists a ON t.artist_id = a.artist_id
                WHERE t.title IS NOT NULL AND t.title != ''
            """) as cur:
                async for row in cur:
                    a_n = _norm(row["artist_name"] or "")
                    t_n = _norm(row["title_name"] or "")
                    if a_n and t_n:
                        library_index[(a_n, t_n)] = row["track_id"]
    except Exception as e:
        log.warning(f"Could not load library index: {e}")
    return library_index

def _check_library(artist: str, title: str, library_index: dict) -> tuple:
    a_n = _norm(artist)
    t_n = _norm(title)
    tid = library_index.get((a_n, t_n))
    if tid is not None:
        return True, tid
    if library_index:
        for (lib_a, lib_t), ltid in library_index.items():
            if not lib_a.startswith(a_n[:3]):
                continue
            if (_fuzzy(a_n, lib_a) * 0.5 + _fuzzy(t_n, lib_t) * 0.5) >= MATCH_THRESHOLD:
                return True, ltid
    return False, None


# ══════════════════════════════════════════════════════════════════════════════
#  GAP ANALYSIS
# ══════════════════════════════════════════════════════════════════════════════

@router.post("/gap-analysis")
async def gap_analysis(req: GapAnalysisRequest, _=Depends(require_auth)):
    if not os.path.exists(_STATIC_DB):
        raise HTTPException(404, "Static database not found.")
    try:
        conn = sqlite3.connect(_STATIC_DB, check_same_thread=False)
        conn.row_factory = sqlite3.Row

        # Attach dynamic DB so we can UNION chart_reference_extras (user-imported data)
        has_extras = False
        try:
            conn.execute(f"ATTACH DATABASE '{_DYNAMIC_DB}' AS dyn")
            probe = conn.execute(
                "SELECT name FROM dyn.sqlite_master WHERE type='table' AND name='chart_reference_extras'"
            ).fetchone()
            has_extras = bool(probe)
        except Exception:
            has_extras = False

        conds, params = [], []
        if req.charts:
            conds.append(f"chart_name IN ({','.join('?' * len(req.charts))})")
            params += req.charts
        conds.append("peak_position >= ?"); params.append(req.min_peak)
        conds.append("peak_position <= ?"); params.append(req.max_peak)
        if req.min_year is not None:
            conds.append("chart_year >= ?"); params.append(req.min_year)
        if req.max_year is not None:
            conds.append("chart_year <= ?"); params.append(req.max_year)
        where = "WHERE " + " AND ".join(conds)

        if has_extras:
            # UNION static chart_reference with dynamic chart_reference_extras,
            # deduplicate by (artist_norm, title_norm, chart_name) keeping best peak.
            union_params = params + params
            combined = conn.execute(f"""
                SELECT chart_name, artist, title, artist_norm, title_norm,
                       MIN(peak_position) as peak_position,
                       MAX(weeks_on_chart) as weeks_on_chart,
                       chart_year, data_source
                FROM (
                    SELECT chart_name, artist, title, artist_norm, title_norm,
                           peak_position, weeks_on_chart, chart_year, data_source
                    FROM chart_reference {where}
                    UNION ALL
                    SELECT chart_name, artist, title, artist_norm, title_norm,
                           peak_position, weeks_on_chart, chart_year, data_source
                    FROM dyn.chart_reference_extras {where}
                )
                GROUP BY artist_norm, title_norm, chart_name
                ORDER BY peak_position ASC, chart_year DESC
            """, union_params).fetchall()
        else:
            combined = conn.execute(f"""
                SELECT chart_name, artist, title, artist_norm, title_norm,
                       peak_position, weeks_on_chart, chart_year, data_source
                FROM chart_reference {where}
                ORDER BY peak_position ASC, chart_year DESC
            """, params).fetchall()

        total = len(combined)
        rows = combined[req.offset: req.offset + req.limit]
        entries = [dict(r) for r in rows]
        conn.close()
    except Exception as e:
        raise HTTPException(500, f"DB error: {e}")

    if not entries:
        return {"results": [], "total": 0, "offset": req.offset, "limit": req.limit}

    lib = await _build_library_index()
    results = []
    for e in entries:
        owned, tid = _check_library(e["artist"], e["title"], lib)
        results.append({
            "artist": e["artist"], "title": e["title"],
            "chart_name": e["chart_name"],
            "chart_display": CHART_DISPLAY.get(e["chart_name"], e["chart_name"]),
            "peak_position": e["peak_position"], "weeks_on_chart": e["weeks_on_chart"],
            "chart_year": e["chart_year"], "in_library": owned, "track_id": tid,
        })
    owned_ct = sum(1 for r in results if r["in_library"])
    return {"results": results, "total": total, "owned": owned_ct,
            "missing": len(results) - owned_ct, "offset": req.offset, "limit": req.limit}


# ══════════════════════════════════════════════════════════════════════════════
#  GAP BY GENRE — Library-first master list (Phase 1: chart sources only)
# ══════════════════════════════════════════════════════════════════════════════
#
#  This is the new replacement for chart-filter-driven Gap Fill. Instead of
#  asking the user "which chart?" we ask "which genres + decades?" and build
#  a deduped, scored master list across every chart source we have.
#
#  Phase 1 uses chart sources only. Phase 2 will add Last.fm tag pulls to
#  fill genres where chart coverage is thin (CCM, indie, jazz, etc.).
#
#  Source weights (Phase 1):
#    - Billboard Hot 100 (chart_reference)         1.00
#    - Billboard genre charts (country/rnb/rock/   0.90
#      dance/adultpop in chart_reference)
#    - utdata Hot 100 post-2018 (extras)           1.00
#    - Chart2000 global (extras)                   0.85
#    - tsort.info historical (extras)              0.80
#    - Billboard Christian / CCM (extras)          0.90
#    - UK Official (extras)                        0.85
#    - Kworb iTunes US (extras)                    0.70
#
#  Score per row = weight × ((101 - peak_position) / 100). Rows for the same
#  (artist_norm, title_norm) are merged across sources by summing scores.
#
#  Decade filter: NULL chart_year rows pass through ONLY when no decade is
#  selected. This is honest — we don't fake years.
# ══════════════════════════════════════════════════════════════════════════════

# Genre → list of chart_name keys that imply membership in that genre.
# Genre-agnostic charts (hot100, tsort, chart2000, kworb_us, uk_official, adultpop)
# are NOT listed here — they only contribute to a genre when the same track also
# appears in a genre-tagged source (Phase 2 will let Last.fm tags expand this).
_GENRE_TO_CHART_KEYS = {
    "country": {"country"},
    "rock":    {"rock"},
    "rnb":     {"rnb"},
    "dance":   {"dance"},
    "ccm":     {"ccm", "ccm-ac", "ccm-rock", "worship", "gospel",
                "sgospel", "ugospel", "tgospel"},
    "pop":     {"adultpop"},   # Adult Pop is the closest chart-based proxy for "pop"
}

# Per-source weights for scoring. Anything not listed defaults to 0.5.
_SOURCE_WEIGHTS = {
    # data_source values written by the importers — extend as new sources arrive
    "billboard_hot100":    1.00,
    "billboard_country":   0.90,
    "billboard_rnb":       0.90,
    "billboard_rock":      0.90,
    "billboard_dance":     0.90,
    "billboard_adultpop":  0.90,
    "billboard_christian": 0.90,
    "billboard_pop":       0.90,
    "utdata":              1.00,
    "chart2000":           0.85,
    "tsort":               0.80,
    "uk_official":         0.85,
    "kworb_us":            0.70,
}

# Fallback when data_source isn't in the weights table — derive from chart_name.
_CHART_NAME_FALLBACK_WEIGHTS = {
    "hot100":    1.00,
    "country":   0.90,
    "rnb":       0.90,
    "rock":      0.90,
    "dance":     0.90,
    "adultpop":  0.90,
    "ccm":       0.90,
    "uk":        0.85,
    "uk_official": 0.85,
    "chart2000": 0.85,
    "tsort":     0.80,
    "kworb_us":  0.70,
}

_TIER_LIMITS = {
    "essential": 250,
    "notable":   1000,
    "deep":      5000,
}

_CACHE_TTL_HOURS = 24


def _decades_to_year_ranges(decades: list) -> list:
    """
    Translate ['1980s','1990s'] → [(1980,1989),(1990,1999)].
    Accepts '50s', '1950s', '2000s', '00s', '10s', '20s'. Unknown values skipped.
    """
    ranges = []
    for d in decades or []:
        s = str(d).strip().lower().rstrip("s")
        # Normalize "80" → "1980", "00" → "2000", etc.
        if len(s) == 2:
            n = int(s) if s.isdigit() else None
            if n is None: continue
            year = 2000 + n if n < 30 else 1900 + n
        elif len(s) == 4 and s.isdigit():
            year = int(s)
        else:
            continue
        ranges.append((year, year + 9))
    return ranges


def _row_weight(data_source: str, chart_name: str) -> float:
    """Pick the weight for a row based on its data_source, falling back to chart_name."""
    if data_source and data_source in _SOURCE_WEIGHTS:
        return _SOURCE_WEIGHTS[data_source]
    if chart_name and chart_name in _CHART_NAME_FALLBACK_WEIGHTS:
        return _CHART_NAME_FALLBACK_WEIGHTS[chart_name]
    return 0.5


def _row_score(weight: float, peak_position) -> float:
    """Score = weight × ((101 - peak) / 100). Missing peak → 0.5 mid-tier."""
    try:
        peak = int(peak_position) if peak_position else 50
    except (TypeError, ValueError):
        peak = 50
    peak = max(1, min(100, peak))
    return weight * ((101 - peak) / 100.0)


def _cache_key(payload: dict) -> str:
    """SHA1 of canonicalized JSON of cache-relevant params."""
    keep = {
        "genres":  sorted([g.lower() for g in (payload.get("genres") or [])]),
        "decades": sorted([str(d).lower() for d in (payload.get("decades") or [])]),
        "tier":    (payload.get("tier") or "notable").lower(),
    }
    blob = json.dumps(keep, sort_keys=True, separators=(",", ":"))
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()


async def _ensure_phase1_schema():
    """
    Idempotent schema setup for gap-by-genre.
      1. master_list_cache table (dynamic DB)
      2. genre_tags column on chart_reference_extras (dynamic DB) — Phase 2 will use it
    Both are safe to re-run on every endpoint call.
    """
    async with aiosqlite.connect(_DYNAMIC_DB) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS master_list_cache (
                cache_key   TEXT PRIMARY KEY,
                payload_json TEXT NOT NULL,
                built_at    TEXT NOT NULL,
                expires_at  TEXT NOT NULL
            )
        """)
        # Add genre_tags column to chart_reference_extras if missing.
        # SQLite has no IF NOT EXISTS for columns, so we probe first.
        try:
            async with db.execute("PRAGMA table_info(chart_reference_extras)") as cur:
                cols = {r[1] for r in await cur.fetchall()}
            if cols and "genre_tags" not in cols:
                await db.execute("ALTER TABLE chart_reference_extras ADD COLUMN genre_tags TEXT")
                log.info("Added genre_tags column to chart_reference_extras")
        except Exception as e:
            # Table may not exist yet on a brand-new install — not fatal
            log.debug(f"genre_tags column probe skipped: {e}")
        await db.commit()


async def _cache_get(key: str) -> Optional[dict]:
    try:
        async with aiosqlite.connect(_DYNAMIC_DB) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT payload_json, expires_at FROM master_list_cache WHERE cache_key=?",
                (key,)
            ) as cur:
                row = await cur.fetchone()
        if not row:
            return None
        # Compare ISO strings safely
        expires = datetime.fromisoformat(row["expires_at"])
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        if expires <= datetime.now(timezone.utc):
            return None
        return json.loads(row["payload_json"])
    except Exception as e:
        log.debug(f"cache_get miss: {e}")
        return None


async def _cache_put(key: str, payload: dict):
    try:
        now    = datetime.now(timezone.utc)
        expires = now + timedelta(hours=_CACHE_TTL_HOURS)
        async with aiosqlite.connect(_DYNAMIC_DB) as db:
            await db.execute(
                """INSERT INTO master_list_cache
                   (cache_key, payload_json, built_at, expires_at)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(cache_key) DO UPDATE SET
                       payload_json=excluded.payload_json,
                       built_at=excluded.built_at,
                       expires_at=excluded.expires_at""",
                (key, json.dumps(payload), now.isoformat(), expires.isoformat())
            )
            await db.commit()
    except Exception as e:
        log.warning(f"cache_put failed (non-fatal): {e}")


def _build_master_list(req: GapByGenreRequest) -> list:
    """
    Synchronous DB work: query chart_reference + chart_reference_extras,
    apply genre + decade filters, dedupe, score. Returns sorted list.

    Phase 1: chart sources only. No Last.fm.
    """
    if not os.path.exists(_STATIC_DB):
        raise HTTPException(404, "Static database not found.")

    conn = sqlite3.connect(_STATIC_DB, check_same_thread=False)
    conn.row_factory = sqlite3.Row

    # Attach dynamic DB read-only-ish for chart_reference_extras
    has_extras = False
    try:
        conn.execute(f"ATTACH DATABASE '{_DYNAMIC_DB}' AS dyn")
        probe = conn.execute(
            "SELECT name FROM dyn.sqlite_master WHERE type='table' AND name='chart_reference_extras'"
        ).fetchone()
        has_extras = bool(probe)
    except Exception:
        has_extras = False

    # ── Build the genre-driven chart_name filter ────────────────────────────
    # If the user picked genres, we restrict to chart_names that imply those
    # genres. If they picked NO genres, we accept all chart_names (master list
    # across the entire universe).
    genre_chart_keys: set = set()
    for g in (req.genres or []):
        keys = _GENRE_TO_CHART_KEYS.get(g.lower())
        if keys:
            genre_chart_keys |= keys

    where_clauses, params = [], []
    if req.genres and genre_chart_keys:
        placeholders = ",".join("?" * len(genre_chart_keys))
        where_clauses.append(f"chart_name IN ({placeholders})")
        params.extend(sorted(genre_chart_keys))
    elif req.genres and not genre_chart_keys:
        # User picked genres but none map to any chart-source key (e.g. "indie",
        # "jazz", "folk" — Phase 2 will handle those via Last.fm).
        # Phase 1: return empty rather than dump the whole DB.
        conn.close()
        return []

    # ── Decade filter ───────────────────────────────────────────────────────
    decade_ranges = _decades_to_year_ranges(req.decades or [])
    if decade_ranges:
        ors = []
        for (lo, hi) in decade_ranges:
            ors.append("(chart_year BETWEEN ? AND ?)")
            params.extend([lo, hi])
        where_clauses.append("(" + " OR ".join(ors) + ")")
    # If no decade picked, NULL years pass through. That's intentional —
    # the 36k NULL-year genre-chart rows still contribute to the master list.

    where_sql = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""

    select_cols = ("chart_name, artist, title, artist_norm, title_norm, "
                   "peak_position, weeks_on_chart, chart_year, data_source")

    if has_extras:
        union_params = list(params) + list(params)
        sql = f"""
            SELECT {select_cols}
            FROM chart_reference {where_sql}
            UNION ALL
            SELECT {select_cols}
            FROM dyn.chart_reference_extras {where_sql}
        """
        rows = conn.execute(sql, union_params).fetchall()
    else:
        sql = f"SELECT {select_cols} FROM chart_reference {where_sql}"
        rows = conn.execute(sql, params).fetchall()

    conn.close()

    # ── Dedupe + score ──────────────────────────────────────────────────────
    # Key = (artist_norm, title_norm). Aggregate sources, sum scores,
    # keep best peak across all sources, keep earliest known year.
    merged: dict = {}
    for r in rows:
        a_n = (r["artist_norm"] or "").strip()
        t_n = (r["title_norm"]  or "").strip()
        if not a_n or not t_n:
            continue
        key = (a_n, t_n)
        weight = _row_weight(r["data_source"] or "", r["chart_name"] or "")
        score  = _row_score(weight, r["peak_position"])
        m = merged.get(key)
        if m is None:
            m = {
                "artist":      r["artist"]   or "",
                "title":       r["title"]    or "",
                "artist_norm": a_n,
                "title_norm":  t_n,
                "best_peak":   r["peak_position"],
                "best_chart":  r["chart_name"] or "",
                "best_year":   r["chart_year"],
                "score":       0.0,
                "sources":     set(),
                "primary_chart_label": "",
            }
            merged[key] = m

        # Aggregate
        m["score"] += score
        if r["data_source"]:
            m["sources"].add(r["data_source"])
        elif r["chart_name"]:
            m["sources"].add(r["chart_name"])

        # Best peak (lowest number wins)
        try:
            cur_peak = int(m["best_peak"]) if m["best_peak"] is not None else 999
        except (TypeError, ValueError):
            cur_peak = 999
        try:
            new_peak = int(r["peak_position"]) if r["peak_position"] is not None else 999
        except (TypeError, ValueError):
            new_peak = 999
        if new_peak < cur_peak:
            m["best_peak"]  = r["peak_position"]
            m["best_chart"] = r["chart_name"] or m["best_chart"]
            m["best_year"]  = r["chart_year"] if r["chart_year"] is not None else m["best_year"]
        elif m["best_year"] is None and r["chart_year"] is not None:
            m["best_year"] = r["chart_year"]

    # Sort by score descending, slice to tier limit
    items = list(merged.values())
    items.sort(key=lambda x: x["score"], reverse=True)
    cap = _TIER_LIMITS.get((req.tier or "notable").lower(), _TIER_LIMITS["notable"])
    items = items[:cap]

    # Build display label (e.g. "Hot 100 #4 (1985)" or "Country #1")
    for m in items:
        chart_disp = CHART_DISPLAY.get(m["best_chart"], m["best_chart"] or "Chart")
        peak       = m["best_peak"]
        year       = m["best_year"]
        if peak and year:
            m["primary_chart_label"] = f"{chart_disp} #{peak} ({year})"
        elif peak:
            m["primary_chart_label"] = f"{chart_disp} #{peak}"
        else:
            m["primary_chart_label"] = chart_disp
        # Convert sources set → sorted list for JSON
        m["sources"] = sorted(m["sources"])

    return items


@router.post("/gap-by-genre")
async def gap_by_genre(req: GapByGenreRequest, _=Depends(require_auth)):
    """
    Library-first Gap Fill. Pick genres + decades + tier, get a deduped,
    scored master list with owned/missing flags. Phase 1 = chart sources only.
    """
    # Make sure cache table + genre_tags column exist
    await _ensure_phase1_schema()

    cache_payload = req.model_dump() if hasattr(req, "model_dump") else req.dict()
    key = _cache_key(cache_payload)

    cached = None
    if not req.bypass_cache:
        cached = await _cache_get(key)

    if cached is None:
        # Build (sync DB work) — wrap in try/except so a build error returns 500 cleanly
        try:
            items = await asyncio.to_thread(_build_master_list, req)
        except HTTPException:
            raise
        except Exception as e:
            log.exception("gap_by_genre build failed")
            raise HTTPException(500, f"Master list build failed: {e}")

        cached = {"items": items, "built_at": datetime.now(timezone.utc).isoformat()}
        await _cache_put(key, cached)

    items = cached.get("items") or []
    total_master = len(items)

    # Library cross-reference (always live — library state changes more often than master list)
    lib = await _build_library_index()

    # Apply include_owned filter, then paginate
    enriched = []
    for m in items:
        owned, tid = _check_library(m["artist"], m["title"], lib)
        if (not req.include_owned) and owned:
            continue
        enriched.append({
            "artist":         m["artist"],
            "title":          m["title"],
            "chart_name":     m.get("best_chart") or "",
            "chart_display":  CHART_DISPLAY.get(m.get("best_chart") or "", m.get("best_chart") or ""),
            "peak_position":  m.get("best_peak"),
            "weeks_on_chart": None,             # Phase 2 may surface this
            "chart_year":     m.get("best_year"),
            "score":          round(float(m.get("score") or 0.0), 4),
            "sources":        m.get("sources") or [],
            "primary_chart_label": m.get("primary_chart_label") or "",
            "in_library":     owned,
            "track_id":       tid,
        })

    total_filtered = len(enriched)
    page = enriched[req.offset: req.offset + req.limit]
    owned_ct = sum(1 for r in enriched if r["in_library"])
    return {
        "results":     page,
        "total":       total_filtered,        # filtered total (after include_owned)
        "total_master": total_master,         # raw master list size before owned filter
        "owned":       owned_ct,
        "missing":     total_filtered - owned_ct,
        "offset":      req.offset,
        "limit":       req.limit,
        "tier":        req.tier,
        "genres":      req.genres,
        "decades":     req.decades,
        "cache_built": cached.get("built_at"),
    }


# ══════════════════════════════════════════════════════════════════════════════
#  TRENDING
# ══════════════════════════════════════════════════════════════════════════════

@router.post("/trending")
async def trending(req: TrendingRequest, _=Depends(require_auth)):
    lfm_key = ""
    try:
        async with aiosqlite.connect(_DYNAMIC_DB) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT token_enc FROM connections WHERE service='lastfm'"
            ) as cur:
                row = await cur.fetchone()
                if row and row["token_enc"]:
                    lfm_key = decrypt_token(row["token_enc"]) or ""
    except Exception:
        pass
    if not lfm_key:
        raise HTTPException(400, "No Last.fm API key configured.")

    tracks = []
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.get("https://ws.audioscrobbler.com/2.0/", params={
                "method": "tag.getTopTracks", "tag": req.tag,
                "api_key": lfm_key, "format": "json",
                "limit": str(min(req.limit, 200)), "page": str(req.page),
            })
            if r.is_success:
                for t in r.json().get("tracks", {}).get("track", []):
                    tracks.append({
                        "artist": t.get("artist", {}).get("name", ""),
                        "title":  t.get("name", ""),
                        "listeners": int(t.get("listeners", 0)),
                    })
    except Exception as e:
        raise HTTPException(502, f"Last.fm error: {e}")

    if not tracks:
        return {"results": [], "total": 0, "tag": req.tag, "page": req.page}

    lib = await _build_library_index()
    results = []
    for t in tracks:
        owned, tid = _check_library(t["artist"], t["title"], lib)
        results.append({**t, "in_library": owned, "track_id": tid})
    owned_ct = sum(1 for r in results if r["in_library"])
    return {"results": results, "total": len(results), "owned": owned_ct,
            "missing": len(results) - owned_ct, "tag": req.tag, "page": req.page}


# ══════════════════════════════════════════════════════════════════════════════
#  SEARCH — Prowlarr via Torznab (album-first)
# ══════════════════════════════════════════════════════════════════════════════

@router.post("/search")
async def search_prowlarr(req: SearchRequest, _=Depends(require_auth)):
    """Album-first search via Torznab per-indexer endpoints."""
    conn = await _get_connection("prowlarr")
    base = conn["base_url"].rstrip("/")
    token = conn["token"]

    # Fetch enabled indexer IDs
    indexer_ids = []
    try:
        async with httpx.AsyncClient(timeout=10.0, verify=False) as client:
            r = await client.get(f"{base}/api/v1/indexer", headers={"X-Api-Key": token})
            if r.is_success and isinstance(r.json(), list):
                indexer_ids = [ix["id"] for ix in r.json() if ix.get("enable")]
    except Exception as e:
        log.warning(f"Failed to fetch indexers: {e}")
    if not indexer_ids:
        raise HTTPException(502, "No enabled indexers in Prowlarr.")

    async def _torznab_search(query: str) -> list:
        all_items = []
        async def _query_one(idx_id: int, cl: httpx.AsyncClient) -> list:
            try:
                r = await cl.get(f"{base}/{idx_id}/api", params={
                    "t": "search", "q": query, "apikey": token,
                    "cat": _AUDIO_CATS, "limit": "50",
                })
                if not r.is_success:
                    return []
                items = []
                root = ET.fromstring(r.text)
                ns = {"torznab": "http://torznab.com/schemas/2015/feed"}
                for el in root.iter("item"):
                    title = el.findtext("title", "")
                    size = int(el.findtext("size", "0") or "0")
                    link = el.findtext("link", "")
                    guid = el.findtext("guid", "")
                    seeders, leechers = 0, 0
                    for a in el.findall("torznab:attr", ns):
                        n, v = a.get("name", ""), a.get("value", "0")
                        if n == "seeders": seeders = int(v or "0")
                        elif n == "peers": leechers = int(v or "0") - seeders
                        elif n == "leechers": leechers = int(v or "0")
                    enc = el.find("enclosure")
                    dl_url = enc.get("url", link) if enc is not None and enc.get("url") else link
                    info_url = guid if guid and guid.startswith("http") else ""
                    items.append({"title": title, "size": size, "seeders": seeders,
                                  "leechers": leechers, "dl_url": dl_url,
                                  "info_url": info_url, "indexer_id": idx_id})
                return items
            except Exception:
                return []

        async with httpx.AsyncClient(timeout=25.0, verify=False) as cl:
            results = await asyncio.gather(*[_query_one(i, cl) for i in indexer_ids],
                                           return_exceptions=True)
        for r in results:
            if isinstance(r, list):
                all_items.extend(r)
        log.info(f"Torznab '{query}': {len(all_items)} items from {len(indexer_ids)} indexers")
        return all_items

    def _parse(raw: list) -> list:
        parsed = []
        for item in raw:
            s = item.get("seeders", 0) or 0
            if s < 1:
                continue
            sz = item.get("size", 0) or 0
            parsed.append({
                "title": item["title"], "indexer": f"Indexer #{item.get('indexer_id','?')}",
                "size_mb": round(sz / (1024*1024), 1) if sz else 0,
                "seeders": s, "leechers": item.get("leechers", 0) or 0,
                "download_url": item.get("dl_url", ""),
                "info_url": item.get("info_url", ""),
            })
        parsed.sort(key=lambda x: x["seeders"], reverse=True)
        return parsed[:25]

    # Album-first: search by artist name (gets albums with best seeds)
    results = await _torznab_search(req.artist)
    parsed = _parse(results)

    # If album provided, also search artist+album
    if req.album:
        more = await _torznab_search(f"{req.artist} {req.album}")
        parsed_more = _parse(more)
        # Merge deduplicated
        seen = {r["title"].lower().strip() for r in parsed}
        for r in parsed_more:
            if r["title"].lower().strip() not in seen:
                parsed.append(r)
                seen.add(r["title"].lower().strip())
        parsed.sort(key=lambda x: x["seeders"], reverse=True)
        parsed = parsed[:25]

    return {"results": parsed, "total": len(parsed), "artist_query": req.artist}


# ══════════════════════════════════════════════════════════════════════════════
#  GRAB — Add to qBit + background checkmark task
# ══════════════════════════════════════════════════════════════════════════════

@router.post("/grab")
async def grab(req: GrabRequest, _=Depends(require_auth)):
    if not req.download_url:
        raise HTTPException(400, "No download URL provided.")

    client_type = "qbittorrent"
    try:
        async with aiosqlite.connect(_DYNAMIC_DB) as db:
            db.row_factory = aiosqlite.Row
            for svc in ("qbittorrent", "deluge", "transmission"):
                async with db.execute(
                    "SELECT base_url FROM connections WHERE service=? AND base_url IS NOT NULL",
                    (svc,)
                ) as cur:
                    row = await cur.fetchone()
                    if row and row["base_url"]:
                        client_type = svc
                        break
    except Exception:
        pass

    if client_type != "qbittorrent":
        return {"ok": True, "client": client_type,
                "message": f"Use OPEN or MAGNET for {client_type} — direct grab only supports qBittorrent."}

    conn = await _get_connection("qbittorrent")
    base = conn["base_url"].rstrip("/")
    pwd = conn["token"]
    extra = conn["extra"]

    async with httpx.AsyncClient(timeout=30.0, verify=False) as client:
        sid = await _qbt_login(client, base, extra, pwd)
        cookies = {"SID": sid}

        r = await client.post(f"{base}/api/v2/torrents/add",
            data={"urls": req.download_url, "category": "charthound-music"},
            cookies=cookies)
        if not r.is_success:
            raise HTTPException(502, f"qBittorrent add failed: {r.status_code}")

        # Grab the hash of the torrent we just added
        await asyncio.sleep(1)  # give qBit a moment to register it
        tr = await client.get(f"{base}/api/v2/torrents/info",
            params={"category": "charthound-music", "sort": "added_on",
                    "reverse": "true", "limit": "1"},
            cookies=cookies)
        t_hash = ""
        if tr.is_success and tr.json():
            t_hash = tr.json()[0].get("hash", "")

    # Fire-and-forget background task with specific hash
    asyncio.create_task(_background_checkmark(base, pwd, extra, t_hash, req.title or ""))

    return {"ok": True, "client": "qbittorrent", "title": req.title,
            "category": "charthound-music",
            "message": "Torrent added to qBittorrent — files will start downloading shortly"}


async def _background_checkmark(base: str, password: str, extra: dict,
                                t_hash: str = "", title: str = ""):
    """
    Background task: targets a specific torrent hash.
    Phase 1 — Metadata wait: if files list is empty, force-resume to trigger
              metadata fetch from peers, retry until files appear.
    Phase 2 — Checkmark: set all files to priority 1 (Normal) and force-resume.
    Retries every 5 seconds for up to 60 seconds (12 attempts).
    If metadata never resolves, logs a warning so the user knows it's dead.
    """
    label = title or t_hash[:12] or "unknown"

    if not t_hash:
        log.warning(f"Checkmark: no hash captured for '{label}' — cannot track")
        return

    for attempt in range(12):
        await asyncio.sleep(5)
        try:
            async with httpx.AsyncClient(timeout=15.0, verify=False) as client:
                sid = await _qbt_login(client, base, extra, password)
                cookies = {"SID": sid}

                # Check torrent progress directly by hash
                tr = await client.get(f"{base}/api/v2/torrents/info",
                    params={"hashes": t_hash}, cookies=cookies)
                if not (tr.is_success and tr.json()):
                    log.info(f"Checkmark: torrent {t_hash[:8]} not found, attempt {attempt+1}/12")
                    continue

                torrent = tr.json()[0]
                progress = torrent.get("progress", 0)

                # Already downloading/seeding with actual data — done
                if progress > 0.01:
                    log.info(f"Checkmark OK: '{label}' ({t_hash[:8]}) at {progress*100:.0f}%")
                    return

                # Get file list
                fr = await client.get(f"{base}/api/v2/torrents/files",
                    params={"hash": t_hash}, cookies=cookies)
                files = fr.json() if (fr.is_success and isinstance(fr.json(), list)) else []

                if not files:
                    # No metadata yet — force resume to trigger peer connection
                    await client.post(f"{base}/api/v2/torrents/resume",
                        data={"hashes": t_hash}, cookies=cookies)
                    log.info(f"Checkmark: '{label}' ({t_hash[:8]}) no files yet (metadata pending), "
                             f"force-resumed, attempt {attempt+1}/12")
                    continue

                # Files exist — set all to priority 1
                has_unchecked = any(f.get("priority", 0) == 0 for f in files)

                if has_unchecked:
                    all_ids = "|".join(str(i) for i in range(len(files)))
                    pr = await client.post(f"{base}/api/v2/torrents/filePrio",
                        data={"hash": t_hash, "id": all_ids, "priority": "1"},
                        cookies=cookies)
                    log.info(f"Checkmark: '{label}' set {len(files)} files to Normal — {pr.status_code}")

                # Force resume
                await client.post(f"{base}/api/v2/torrents/resume",
                    data={"hashes": t_hash}, cookies=cookies)
                log.info(f"Checkmark: '{label}' ({t_hash[:8]}) started on attempt {attempt+1}")
                return

        except Exception as e:
            log.warning(f"Checkmark attempt {attempt+1} error for '{label}': {e}")

    log.warning(f"Checkmark: '{label}' ({t_hash[:8]}) failed to resolve metadata after 60s "
                f"— torrent may be dead/fake. Remove it from qBittorrent manually.")


async def _qbt_login(client: httpx.AsyncClient, base: str, extra: dict, password: str) -> str:
    username = extra.get("username", "admin")
    r = await client.post(f"{base}/api/v2/auth/login",
        data={"username": username, "password": password})
    if r.text.strip() != "Ok.":
        raise HTTPException(502, "qBittorrent login failed.")
    return r.cookies.get("SID", "")


# ══════════════════════════════════════════════════════════════════════════════
#  YEAR RANGE
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/year-range")
async def year_range(_=Depends(require_auth)):
    if not os.path.exists(_STATIC_DB):
        return {"min_year": 1950, "max_year": 2025}
    try:
        conn = sqlite3.connect(_STATIC_DB, check_same_thread=False)
        row = conn.execute(
            "SELECT MIN(chart_year), MAX(chart_year) FROM chart_reference WHERE chart_year IS NOT NULL"
        ).fetchone()
        conn.close()
        return {"min_year": row[0] or 1950, "max_year": row[1] or 2025}
    except Exception:
        return {"min_year": 1950, "max_year": 2025}


# ══════════════════════════════════════════════════════════════════════════════
#  GENRES
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/genres")
async def genres(_=Depends(require_auth)):
    try:
        async with aiosqlite.connect(_DYNAMIC_DB) as db:
            gs = set()
            async with db.execute("""
                SELECT DISTINCT genre_1 FROM tracks WHERE genre_1 IS NOT NULL AND genre_1 != ''
                UNION
                SELECT DISTINCT genre_2 FROM tracks WHERE genre_2 IS NOT NULL AND genre_2 != ''
                UNION
                SELECT DISTINCT genre_3 FROM tracks WHERE genre_3 IS NOT NULL AND genre_3 != ''
            """) as cur:
                async for row in cur:
                    if row[0]: gs.add(row[0])
        return {"genres": sorted(gs)}
    except Exception:
        return {"genres": []}
