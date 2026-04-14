# © 2026 Colby R. Curtis | ChartHound: The New World
# All Rights Reserved.
"""
ChartHound — The Groomer Router
Real Chart Data Playlist Builder

Lookup waterfall per track:
  1. charthound_static.db → chart_reference   (real Billboard data, confidence: high)
  2. charthound_static.db → billboard_pop      (historical pop 1890-2015, confidence: high)
  3. charthound.db        → chart_data cache   (previously computed, skip re-scan)
  4. Last.fm listener count                    (pseudo peak estimate, confidence: low)

Scan modes:
  A) Media Server Mode — Plex / Emby / Jellyfin library pull, playlist push back
  B) Local Folder Mode — walks physical files, M3U download only

Endpoints:
  GET  /api/groomer/charts/status
  POST /api/groomer/scan/start
  GET  /api/groomer/scan/status/{job_id}
  POST /api/groomer/scan/stop
  GET  /api/groomer/results
  GET  /api/groomer/db_stats
  POST /api/groomer/playlist/push
  POST /api/groomer/playlist/m3u
"""

# © 2026 Colby R. Curtis | ChartHound: The New World — All Rights Reserved.

import asyncio
import difflib
import json
import logging
import os
import re
import sqlite3
import time

import aiosqlite
import httpx

from datetime import datetime
from typing import Optional, List
from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel

from app.config import get_settings
from app.deps import require_auth
from app.security import decrypt_token

from app.routers.retriever import (
    write_tags as _write_tags_fn,
    peak_to_stars,
    format_chart_comment,
)

log      = logging.getLogger("charthound.groomer")
router   = APIRouter(prefix="/api/groomer", tags=["groomer"])
settings = get_settings()

# Paths — both injected via environment / docker-compose
_DYNAMIC_DB = getattr(settings, "database_url",     "/data/charthound.db")
_STATIC_DB  = getattr(settings, "static_db_url",    "/data/charthound_static.db")

# ── Chart display names ───────────────────────────────────────────────────────
CHART_DISPLAY = {
    "hot100":   "Hot 100",       "adultpop": "Adult Pop",
    "ac":       "Adult Contemp", "uk":       "UK Singles",
    "country":  "Country",       "rnb":      "R&B/Hip-Hop",
    "dance":    "Dance",         "rock":     "Mainstream Rock",
    "ccm":      "CCM",           "ccm-ac":   "CCM-AC",
    "ccm-rock": "CCM Rock",      "worship":  "Worship",
    "gospel":   "Gospel",        "sgospel":  "Southern Gospel",
    "ugospel":  "Urban Gospel",  "tgospel":  "Traditional Gospel",
}

MATCH_THRESHOLD = 0.82

# ── Last.fm listener thresholds by genre bucket ───────────────────────────────
_LFM_THRESHOLDS = {
    "ccm":     (200_000,   75_000,  25_000,  10_000,  10_000),
    "country": (2_000_000, 1_000_000, 500_000, 200_000, 200_000),
    "dance":   (2_000_000, 1_000_000, 500_000, 200_000, 200_000),
    "rnb":     (3_000_000, 1_500_000, 750_000, 300_000, 300_000),
    "rock":    (5_000_000, 2_000_000, 1_000_000, 500_000, 500_000),
    "default": (10_000_000, 5_000_000, 2_000_000, 1_000_000, 2_000_000),
}

_CCM_KEYWORDS = {
    "christian","ccm","gospel","worship","praise","hymn","religious",
    "inspirational","southern gospel","urban gospel",
    "contemporary christian","spiritual","jesus music",
}
_CCM_CHARTS = {"ccm","gospel","ccm-ac","ccm-rock","worship","sgospel","ugospel","tgospel"}


def _detect_genre_bucket(genre_tags: list, chart_names: list) -> str:
    all_text = " ".join((genre_tags or []) + (chart_names or [])).lower()
    if any(kw in all_text for kw in _CCM_KEYWORDS): return "ccm"
    if any(c in _CCM_CHARTS for c in (chart_names or [])): return "ccm"
    if "country" in all_text: return "country"
    if any(k in all_text for k in ("dance","electronic","edm","house","club")): return "dance"
    if any(k in all_text for k in ("r&b","rnb","soul","hip hop","hip-hop","rap")): return "rnb"
    if "rock" in all_text: return "rock"
    return "default"


def _listeners_to_stars(listeners: int, bucket: str) -> int:
    t = _LFM_THRESHOLDS.get(bucket, _LFM_THRESHOLDS["default"])
    if listeners >= t[0]: return 5
    if listeners >= t[1]: return 4
    if listeners >= t[2]: return 3
    if listeners >= t[3]: return 2
    return 1


def _listeners_to_est_peak(listeners: int, bucket: str) -> int:
    if bucket == "ccm": return 0
    import random
    t = _LFM_THRESHOLDS.get(bucket, _LFM_THRESHOLDS["default"])
    if listeners >= t[0]: return random.randint(1, 5)
    if listeners >= t[1]: return random.randint(6, 15)
    if listeners >= t[2]: return random.randint(16, 30)
    if listeners >= t[3]: return random.randint(31, 50)
    return random.randint(51, 80)


def _meets_min_threshold(listeners: int, bucket: str) -> bool:
    return listeners >= _LFM_THRESHOLDS.get(bucket, _LFM_THRESHOLDS["default"])[4]


# ── In-memory scan job tracker ────────────────────────────────────────────────
_scan_job = {
    "status": "idle", "job_id": None, "message": "",
    "total": 0, "processed": 0, "matched": 0, "failed": 0, "cached": 0,
    "started_at": None, "stop_requested": False,
}


# ══════════════════════════════════════════════════════════════════════════════
#  STATIC DB HELPERS  (synchronous — used in executor threads)
# ══════════════════════════════════════════════════════════════════════════════

def _norm(s: str) -> str:
    """Lowercase, strip punctuation and 'the '/'a ' prefixes for matching."""
    s = s.lower().strip()
    s = re.sub(r"[^\w\s]", "", s)
    s = re.sub(r"^(the|a|an)\s+", "", s)
    return s.strip()


def _fuzzy(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, a, b).ratio()


def _lookup_static(artist: str, title: str, charts: list) -> Optional[dict]:
    """
    Synchronous lookup against charthound_static.db.
    Checks chart_reference first (Billboard CSV data), then billboard_pop
    (historical pop 1890-2015).
    Returns dict with peak_position, weeks_on_chart, chart_name, confidence,
    chart_year, data_source — or None if no match.
    """
    if not os.path.exists(_STATIC_DB):
        return None

    artist_n = _norm(artist)
    title_n  = _norm(title)

    try:
        conn = sqlite3.connect(_STATIC_DB, check_same_thread=False)
        conn.row_factory = sqlite3.Row

        # ── Step 1: chart_reference (Billboard CSVs) ──────────────────────────
        chart_filter = ""
        params: list = [artist_n, title_n]
        if charts:
            placeholders = ",".join("?" * len(charts))
            chart_filter = f"AND chart_name IN ({placeholders})"
            params += charts

        rows = conn.execute(f"""
            SELECT chart_name, peak_position, weeks_on_chart, chart_year,
                   artist_norm, title_norm, data_source
            FROM chart_reference
            WHERE artist_norm = ? AND title_norm = ?
            {chart_filter}
            ORDER BY peak_position ASC
            LIMIT 20
        """, params).fetchall()

        # Fuzzy fallback if exact norm match fails
        if not rows:
            candidates = conn.execute(f"""
                SELECT chart_name, peak_position, weeks_on_chart, chart_year,
                       artist_norm, title_norm, data_source
                FROM chart_reference
                WHERE artist_norm LIKE ? {chart_filter}
                LIMIT 200
            """, [artist_n[:6] + "%"] + (charts if charts else [])).fetchall()

            best = None
            best_score = 0.0
            for c in candidates:
                score = (_fuzzy(artist_n, c["artist_norm"]) * 0.5 +
                         _fuzzy(title_n,  c["title_norm"])  * 0.5)
                if score > best_score and score >= MATCH_THRESHOLD:
                    best_score = score
                    best = c
            if best:
                rows = [best]

        if rows:
            best_row = rows[0]
            conn.close()
            return {
                "peak_position":  best_row["peak_position"],
                "weeks_on_chart": best_row["weeks_on_chart"] or 1,
                "chart_name":     best_row["chart_name"],
                "chart_year":     best_row["chart_year"],
                "confidence":     "high",
                "data_source":    best_row["data_source"],
                "all_charts":     [dict(r) for r in rows],
            }

        # ── Step 2: billboard_pop (historical pop 1890-2015) ──────────────────
        bp_rows = conn.execute("""
            SELECT artist, title, peak_position, chart_weeks, year, genre
            FROM billboard_pop
            WHERE LOWER(REPLACE(REPLACE(artist, '.', ''), ',', '')) LIKE ?
            LIMIT 100
        """, [artist_n[:8] + "%"]).fetchall()

        best = None
        best_score = 0.0
        for r in bp_rows:
            score = (_fuzzy(artist_n, _norm(r["artist"])) * 0.5 +
                     _fuzzy(title_n,  _norm(r["title"]))  * 0.5)
            if score > best_score and score >= MATCH_THRESHOLD:
                best_score = score
                best = r

        conn.close()

        if best:
            return {
                "peak_position":  best["peak_position"] or 100,
                "weeks_on_chart": best["chart_weeks"] or 1,
                "chart_name":     "hot100",
                "chart_year":     best["year"],
                "confidence":     "high",
                "data_source":    "billboard_pop_1890_2015",
                "all_charts":     [],
            }

        conn.close()
        return None

    except Exception as e:
        log.warning(f"Static DB lookup error: {e}")
        try:
            conn.close()
        except Exception:
            pass
        return None


# ══════════════════════════════════════════════════════════════════════════════
#  LAST.FM FALLBACK
# ══════════════════════════════════════════════════════════════════════════════

async def _lastfm_listeners(artist: str, title: str, lfm_key: str) -> int:
    if not lfm_key:
        return 0
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            r = await client.get("https://ws.audioscrobbler.com/2.0/", params={
                "method": "track.getInfo", "api_key": lfm_key,
                "artist": artist, "track": title, "format": "json",
                "autocorrect": "1",
            })
            if r.is_success:
                data = r.json()
                listeners = int(data.get("track", {}).get("listeners", 0))
                return listeners
    except Exception:
        pass
    return 0


# ══════════════════════════════════════════════════════════════════════════════
#  CHART STATUS
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/charts/status")
async def charts_status(_=Depends(require_auth)):
    """Returns chart reference counts from both static and dynamic DBs."""
    result = {}

    # Static DB counts
    if os.path.exists(_STATIC_DB):
        try:
            conn = sqlite3.connect(_STATIC_DB, check_same_thread=False)
            rows = conn.execute(
                "SELECT chart_name, COUNT(*) as cnt FROM chart_reference GROUP BY chart_name"
            ).fetchall()
            for r in rows:
                result[r[0]] = {"count": r[1], "source": "static", "status": "loaded"}
            bp_count = conn.execute("SELECT COUNT(*) FROM billboard_pop").fetchone()[0]
            result["billboard_pop"] = {
                "count": bp_count, "source": "static",
                "status": "loaded", "display": "Billboard Pop 1890-2015"
            }
            conn.close()
        except Exception as e:
            log.warning(f"Static DB status error: {e}")

    # Dynamic DB meta
    try:
        async with aiosqlite.connect(_DYNAMIC_DB) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("SELECT * FROM chart_reference_meta") as cur:
                async for row in cur:
                    name = row["chart_name"]
                    if name not in result:
                        result[name] = {
                            "count": row["entry_count"] or 0,
                            "status": row["status"],
                            "display": row["display_name"],
                            "source": "dynamic",
                        }
    except Exception:
        pass

    return {"charts": result, "static_db": os.path.exists(_STATIC_DB)}


# ══════════════════════════════════════════════════════════════════════════════
#  DB STATS
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/db_stats")
async def db_stats(_=Depends(require_auth)):
    static_total = 0
    dynamic_chart_data = 0

    if os.path.exists(_STATIC_DB):
        try:
            conn = sqlite3.connect(_STATIC_DB, check_same_thread=False)
            r1 = conn.execute("SELECT COUNT(*) FROM chart_reference").fetchone()[0]
            r2 = conn.execute("SELECT COUNT(*) FROM billboard_pop").fetchone()[0]
            static_total = r1 + r2
            conn.close()
        except Exception:
            pass

    try:
        async with aiosqlite.connect(_DYNAMIC_DB) as db:
            async with db.execute("SELECT COUNT(*) FROM chart_data") as cur:
                row = await cur.fetchone()
                dynamic_chart_data = row[0] if row else 0
    except Exception:
        pass

    return {
        "static_entries":  static_total,
        "cached_results":  dynamic_chart_data,
        "static_db_ready": os.path.exists(_STATIC_DB),
    }


# ══════════════════════════════════════════════════════════════════════════════
#  SCAN — REQUEST MODELS
# ══════════════════════════════════════════════════════════════════════════════

class ScanRequest(BaseModel):
    source:        str            # 'plex' | 'emby' | 'jellyfin' | 'local'
    charts:        List[str]      # ['hot100', 'country', ...]
    data_source:   str = "verified"   # 'verified' | 'all_matched' | 'estimates'
    write_tags:    bool = False
    limit:         Optional[int] = None
    folder_path:   Optional[str] = None


class StopRequest(BaseModel):
    job_id: Optional[int] = None


# ══════════════════════════════════════════════════════════════════════════════
#  SCAN — START
# ══════════════════════════════════════════════════════════════════════════════

@router.post("/scan/start")
async def scan_start(req: ScanRequest, bg: BackgroundTasks, _=Depends(require_auth)):
    if _scan_job["status"] == "running":
        raise HTTPException(409, "A scan is already running. Stop it first.")

    _scan_job.update({
        "status": "starting", "message": "Initialising scan...",
        "total": 0, "processed": 0, "matched": 0, "failed": 0, "cached": 0,
        "started_at": time.time(), "stop_requested": False, "job_id": int(time.time()),
    })

    bg.add_task(_run_scan, req)
    return {"ok": True, "job_id": _scan_job["job_id"]}


@router.get("/scan/status/{job_id}")
async def scan_status(job_id: int, _=Depends(require_auth)):
    return dict(_scan_job)


@router.post("/scan/stop")
async def scan_stop(_=Depends(require_auth)):
    _scan_job["stop_requested"] = True
    _scan_job["message"] = "Stop requested — finishing current track..."
    return {"ok": True}


# ══════════════════════════════════════════════════════════════════════════════
#  SCAN — BACKGROUND WORKER
# ══════════════════════════════════════════════════════════════════════════════

async def _run_scan(req: ScanRequest):
    _scan_job["status"] = "running"
    try:
        # Get Last.fm key for fallback
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

        # Pull track list
        tracks = await _fetch_tracks(req)
        if not tracks:
            _scan_job.update({"status": "done", "message": "No tracks found to scan."})
            return

        limit = req.limit or len(tracks)
        tracks = tracks[:limit]
        _scan_job["total"] = len(tracks)
        _scan_job["message"] = f"Scanning {len(tracks):,} tracks..."

        loop = asyncio.get_event_loop()

        async with aiosqlite.connect(_DYNAMIC_DB) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("PRAGMA journal_mode=WAL")

            for idx, track in enumerate(tracks):
                if _scan_job["stop_requested"]:
                    _scan_job["status"] = "stopped"
                    _scan_job["message"] = f"Stopped at track {idx + 1} of {len(tracks)}"
                    return

                artist = track.get("artist", "").strip()
                title  = track.get("title",  "").strip()
                if not artist or not title:
                    _scan_job["processed"] += 1
                    continue

                _scan_job["message"] = f"[{idx+1}/{len(tracks)}] {artist} — {title}"

                # Check dynamic cache first
                cached = await _check_cache(db, track, req.charts)
                if cached:
                    _scan_job["cached"]    += 1
                    _scan_job["processed"] += 1
                    continue

                # Static DB lookup (runs in thread — synchronous sqlite3)
                static_result = await loop.run_in_executor(
                    None, _lookup_static, artist, title, req.charts
                )

                if static_result and req.data_source in ("verified", "all_matched"):
                    await _store_result(db, track, static_result, req)
                    _scan_job["matched"] += 1

                elif req.data_source == "estimates":
                    # Last.fm fallback
                    listeners = await _lastfm_listeners(artist, title, lfm_key)
                    genre_tags = [track.get("genre_1",""), track.get("genre_2","")]
                    bucket = _detect_genre_bucket(genre_tags, req.charts)

                    if _meets_min_threshold(listeners, bucket):
                        est_peak = _listeners_to_est_peak(listeners, bucket)
                        stars    = _listeners_to_stars(listeners, bucket)
                        result = {
                            "peak_position":  est_peak,
                            "weeks_on_chart": 1,
                            "chart_name":     req.charts[0] if req.charts else "hot100",
                            "chart_year":     None,
                            "confidence":     "low",
                            "data_source":    "lastfm_estimate",
                            "all_charts":     [],
                            "star_rating":    stars,
                            "listener_count": listeners,
                        }
                        await _store_result(db, track, result, req)
                        _scan_job["matched"] += 1
                    else:
                        _scan_job["failed"] += 1
                else:
                    _scan_job["failed"] += 1

                _scan_job["processed"] += 1
                await asyncio.sleep(0)  # yield to event loop

        elapsed = time.time() - (_scan_job["started_at"] or time.time())
        _scan_job.update({
            "status":  "done",
            "message": (f"Scan complete — {_scan_job['matched']:,} matched, "
                        f"{_scan_job['cached']:,} cached, "
                        f"{_scan_job['failed']:,} unmatched "
                        f"in {elapsed:.0f}s"),
        })

    except Exception as e:
        log.exception("Groomer scan error")
        _scan_job.update({"status": "error", "message": str(e)})


async def _check_cache(db: aiosqlite.Connection, track: dict, charts: list) -> bool:
    """Returns True if this track already has chart_data rows for the requested charts."""
    track_id = track.get("track_id")
    if not track_id:
        return False
    placeholders = ",".join("?" * len(charts)) if charts else "'hot100'"
    params = [track_id] + (charts if charts else [])
    async with db.execute(
        f"SELECT COUNT(*) FROM chart_data WHERE track_id=? AND chart_name IN ({placeholders})",
        params
    ) as cur:
        row = await cur.fetchone()
        return bool(row and row[0] > 0)


async def _store_result(db: aiosqlite.Connection, track: dict, result: dict, req: ScanRequest):
    """Upserts chart_data row and optionally writes COMMENT tag to file."""
    track_id = track.get("track_id")
    if not track_id:
        return

    peak      = result["peak_position"]
    weeks     = result["weeks_on_chart"]
    chart     = result["chart_name"]
    conf      = result["confidence"]
    stars     = result.get("star_rating") or peak_to_stars(peak)
    listeners = result.get("listener_count", 0)

    # Build all_charts list for multi-chart COMMENT tag
    all_charts = result.get("all_charts") or [result]

    comment_parts = []
    for c in all_charts:
        cname = CHART_DISPLAY.get(c.get("chart_name", chart), c.get("chart_name", chart))
        cpeak = c.get("peak_position", peak)
        cwks  = c.get("weeks_on_chart", weeks)
        if conf == "low" and cpeak:
            comment_parts.append(f"{cname}: ~#{cpeak} ({cwks} wks)")
        elif cpeak:
            comment_parts.append(f"{cname}: #{cpeak} ({cwks} wks)")

    comment_string = " | ".join(comment_parts) if comment_parts else ""
    if conf == "low" and not comment_string:
        chart_display = CHART_DISPLAY.get(chart, chart)
        comment_string = f"{chart_display}: ~#{peak} ({weeks} wks)" if peak else f"{chart_display}: ★★★"

    await db.execute("""
        INSERT INTO chart_data
            (track_id, chart_name, peak_position, weeks_on_chart,
             star_rating, confidence, listener_count, comment_string, fetched_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
        ON CONFLICT(track_id, chart_name) DO UPDATE SET
            peak_position  = excluded.peak_position,
            weeks_on_chart = excluded.weeks_on_chart,
            star_rating    = excluded.star_rating,
            confidence     = excluded.confidence,
            listener_count = excluded.listener_count,
            comment_string = excluded.comment_string,
            fetched_at     = excluded.fetched_at
    """, (track_id, chart, peak, weeks, stars, conf, listeners, comment_string))
    await db.commit()

    # Write COMMENT tag to physical file
    if req.write_tags:
        file_path = track.get("file_path", "")
        if file_path and os.path.exists(file_path):
            loop = asyncio.get_event_loop()
            try:
                await loop.run_in_executor(
                    None, _write_tags_fn, file_path, {"comment": comment_string}
                )
            except Exception as e:
                log.warning(f"Tag write failed for {file_path}: {e}")


# ══════════════════════════════════════════════════════════════════════════════
#  TRACK FETCHERS
# ══════════════════════════════════════════════════════════════════════════════

async def _fetch_tracks(req: ScanRequest) -> list:
    if req.source == "local":
        return await _fetch_local(req.folder_path or "/music")
    return await _fetch_media_server(req)


async def _fetch_local(folder: str) -> list:
    tracks = []
    EXTS = {".flac", ".mp3", ".m4a", ".ogg", ".wav", ".aiff"}
    for root, _, files in os.walk(folder):
        for f in files:
            if os.path.splitext(f)[1].lower() in EXTS:
                fp = os.path.join(root, f)
                # Read basic tags
                try:
                    from mutagen import File as MutagenFile
                    mf = MutagenFile(fp, easy=True)
                    if mf:
                        tracks.append({
                            "file_path": fp,
                            "artist":    str(mf.get("artist", [""])[0]),
                            "title":     str(mf.get("title",  [""])[0]),
                            "album":     str(mf.get("album",  [""])[0]),
                            "track_id":  None,
                        })
                except Exception:
                    pass
    return tracks


async def _fetch_media_server(req: ScanRequest) -> list:
    """Pull track list from Plex, Emby, or Jellyfin."""
    try:
        async with aiosqlite.connect(_DYNAMIC_DB) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT base_url, token_enc, extra_json FROM connections WHERE service=?",
                (req.source,)
            ) as cur:
                conn = await cur.fetchone()
    except Exception:
        conn = None

    if not conn:
        raise HTTPException(400, f"No {req.source} connection configured in The Kennel.")

    base_url = conn["base_url"]
    token    = decrypt_token(conn["token_enc"]) if conn["token_enc"] else ""
    extra    = json.loads(conn["extra_json"] or "{}") if conn["extra_json"] else {}

    if req.source == "plex":
        return await _fetch_plex_tracks(base_url, token)
    elif req.source == "emby":
        return await _fetch_emby_tracks(base_url, token, extra.get("user_id",""))
    else:
        return await _fetch_jellyfin_tracks(base_url, token, extra.get("user_id",""))


async def _fetch_plex_tracks(base: str, token: str) -> list:
    tracks = []
    prefix_server = os.environ.get("MEDIA_SERVER_MUSIC_PREFIX", "")
    prefix_docker = os.environ.get("DOCKER_MUSIC_PREFIX", "/music")
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            # Get music library section
            r = await client.get(f"{base}/library/sections?X-Plex-Token={token}",
                                 headers={"Accept":"application/json"})
            sections = r.json().get("MediaContainer",{}).get("Directory",[])
            music_sections = [s["key"] for s in sections if s.get("type") == "artist"]

            for sec in music_sections:
                offset = 0
                while True:
                    r = await client.get(
                        f"{base}/library/sections/{sec}/all",
                        params={"type":10,"X-Plex-Token":token,
                                "X-Plex-Container-Start":offset,
                                "X-Plex-Container-Size":500},
                        headers={"Accept":"application/json"})
                    items = r.json().get("MediaContainer",{}).get("Metadata",[])
                    if not items:
                        break
                    for item in items:
                        fp = ""
                        try:
                            fp = item["Media"][0]["Part"][0]["file"]
                            if prefix_server and fp.startswith(prefix_server):
                                fp = prefix_docker + fp[len(prefix_server):]
                        except (KeyError, IndexError):
                            pass
                        tracks.append({
                            "track_id":       None,
                            "plex_rating_key": item.get("ratingKey",""),
                            "artist":          item.get("grandparentTitle",""),
                            "title":           item.get("title",""),
                            "album":           item.get("parentTitle",""),
                            "file_path":       fp,
                            "tag_artist":      item.get("grandparentTitle",""),
                        })
                    offset += len(items)
                    if len(items) < 500:
                        break
    except Exception as e:
        log.error(f"Plex fetch error: {e}")
    return tracks


async def _fetch_emby_tracks(base: str, token: str, user_id: str) -> list:
    tracks = []
    headers = {"Accept":"application/json","X-Emby-Token":token}
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            start = 0
            while True:
                r = await client.get(f"{base}/Users/{user_id}/Items",
                    params={"IncludeItemTypes":"Audio","Recursive":"true",
                            "Fields":"Path,MediaSources","api_key":token,
                            "StartIndex":start,"Limit":500},
                    headers=headers)
                items = r.json().get("Items",[]) if r.is_success else []
                if not items:
                    break
                for item in items:
                    tracks.append({
                        "track_id":  None,
                        "emby_id":   item.get("Id",""),
                        "artist":    (item.get("Artists") or [""])[0],
                        "title":     item.get("Name",""),
                        "album":     item.get("Album",""),
                        "file_path": item.get("Path",""),
                        "tag_artist":(item.get("Artists") or [""])[0],
                    })
                start += len(items)
                if len(items) < 500:
                    break
    except Exception as e:
        log.error(f"Emby fetch error: {e}")
    return tracks


async def _fetch_jellyfin_tracks(base: str, token: str, user_id: str) -> list:
    tracks = []
    headers = {"Accept":"application/json"}
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            start = 0
            while True:
                r = await client.get(f"{base}/Users/{user_id}/Items",
                    params={"IncludeItemTypes":"Audio","Recursive":"true",
                            "Fields":"Path,MediaSources","api_key":token,
                            "StartIndex":start,"Limit":500},
                    headers=headers)
                items = r.json().get("Items",[]) if r.is_success else []
                if not items:
                    break
                for item in items:
                    tracks.append({
                        "track_id": None,
                        "jf_id":    item.get("Id",""),
                        "artist":   (item.get("Artists") or [""])[0],
                        "title":    item.get("Name",""),
                        "album":    item.get("Album",""),
                        "file_path":item.get("Path",""),
                        "tag_artist":(item.get("Artists") or [""])[0],
                    })
                start += len(items)
                if len(items) < 500:
                    break
    except Exception as e:
        log.error(f"Jellyfin fetch error: {e}")
    return tracks


# ══════════════════════════════════════════════════════════════════════════════
#  RESULTS
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/results")
async def get_results(
    charts:      Optional[str] = None,
    min_peak:    Optional[int] = None,
    max_peak:    Optional[int] = None,
    min_year:    Optional[int] = None,
    max_year:    Optional[int] = None,
    confidence:  Optional[str] = None,
    limit:       int = 5000,
    offset:      int = 0,
    _=Depends(require_auth),
):
    """
    Query chart_data joined with tracks for Groomer results table.
    Filters: charts (comma-sep), peak range, year range, confidence level.
    """
    conditions = []
    params: list = []

    if charts:
        chart_list = [c.strip() for c in charts.split(",") if c.strip()]
        if chart_list:
            conditions.append(f"cd.chart_name IN ({','.join('?'*len(chart_list))})")
            params += chart_list

    if min_peak is not None:
        conditions.append("cd.peak_position >= ?"); params.append(min_peak)
    if max_peak is not None:
        conditions.append("cd.peak_position <= ?"); params.append(max_peak)
    if confidence:
        conditions.append("cd.confidence = ?"); params.append(confidence)

    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""

    params += [limit, offset]

    try:
        async with aiosqlite.connect(_DYNAMIC_DB) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(f"""
                SELECT
                    cd.chart_name, cd.peak_position, cd.weeks_on_chart,
                    cd.star_rating, cd.confidence, cd.comment_string,
                    cd.listener_count, cd.fetched_at,
                    t.title, t.file_path, t.file_format,
                    t.plex_rating_key, t.emby_id, t.jf_id,
                    t.track_id,
                    COALESCE(t.tag_artist, a.name, '') AS tag_artist,
                    COALESCE(t.tag_album,  al.title, '') AS tag_album
                FROM chart_data cd
                JOIN tracks t   ON cd.track_id = t.track_id
                LEFT JOIN artists a  ON t.artist_id = a.artist_id
                LEFT JOIN albums al  ON t.album_id  = al.album_id
                {where}
                ORDER BY cd.peak_position ASC, cd.chart_name
                LIMIT ? OFFSET ?
            """, params) as cur:
                rows = await cur.fetchall()

            return {"results": [dict(r) for r in rows], "count": len(rows)}

    except Exception as e:
        log.exception("Results query error")
        raise HTTPException(500, str(e))


# ══════════════════════════════════════════════════════════════════════════════
#  PLAYLIST PUSH
# ══════════════════════════════════════════════════════════════════════════════

class PlaylistPushRequest(BaseModel):
    server:        str
    playlist_name: str = "Chart Hits"
    track_ids:     Optional[List[int]] = None
    charts:        Optional[List[str]] = None
    max_peak:      Optional[int] = None
    limit:         int = 5000


@router.post("/playlist/push")
async def playlist_push(req: PlaylistPushRequest, _=Depends(require_auth)):
    tracks = await _get_playlist_tracks(req)
    if not tracks:
        raise HTTPException(404, "No tracks found for playlist.")

    try:
        async with aiosqlite.connect(_DYNAMIC_DB) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT base_url, token_enc, extra_json FROM connections WHERE service=?",
                (req.server,)
            ) as cur:
                conn = await cur.fetchone()
    except Exception:
        conn = None

    if not conn:
        raise HTTPException(400, f"No {req.server} connection found.")

    base_url = conn["base_url"]
    token    = decrypt_token(conn["token_enc"]) if conn["token_enc"] else ""
    extra    = json.loads(conn["extra_json"] or "{}") if conn["extra_json"] else {}

    try:
        if req.server == "plex":
            return await _push_to_plex(base_url, token, req.playlist_name, tracks)
        user_id = extra.get("user_id", "")
        if req.server == "emby":
            return await _push_to_emby(base_url, token, user_id, req.playlist_name, tracks)
        return await _push_to_jellyfin(base_url, token, user_id, req.playlist_name, tracks)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"Push failed: {e}")


@router.post("/playlist/m3u")
async def playlist_m3u(req: PlaylistPushRequest, _=Depends(require_auth)):
    tracks = await _get_playlist_tracks(req)
    if not tracks:
        raise HTTPException(404, "No tracks found.")

    lines = ["#EXTM3U", f"#PLAYLIST:{req.playlist_name}"]
    for t in tracks:
        artist = t.get("tag_artist", "")
        title  = t.get("title", "")
        fp     = t.get("file_path", "")
        lines.append(f"#EXTINF:-1,{artist} - {title}")
        lines.append(fp)

    return PlainTextResponse(
        "\n".join(lines),
        media_type="audio/x-mpegurl",
        headers={"Content-Disposition": f'attachment; filename="{req.playlist_name}.m3u"'},
    )


async def _get_playlist_tracks(req: PlaylistPushRequest) -> list:
    conditions = []
    params: list = []

    if req.track_ids:
        conditions.append(f"cd.track_id IN ({','.join('?'*len(req.track_ids))})")
        params += req.track_ids
    if req.charts:
        conditions.append(f"cd.chart_name IN ({','.join('?'*len(req.charts))})")
        params += req.charts
    if req.max_peak:
        conditions.append("cd.peak_position <= ?"); params.append(req.max_peak)

    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    params.append(req.limit)

    async with aiosqlite.connect(_DYNAMIC_DB) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(f"""
            SELECT
                t.track_id, t.title, t.file_path,
                t.plex_rating_key, t.emby_id, t.jf_id,
                COALESCE(t.tag_artist, a.name, '') AS tag_artist,
                cd.peak_position, cd.chart_name
            FROM chart_data cd
            JOIN tracks t  ON cd.track_id = t.track_id
            LEFT JOIN artists a ON t.artist_id = a.artist_id
            {where}
            ORDER BY cd.peak_position ASC
            LIMIT ?
        """, params) as cur:
            rows = await cur.fetchall()
    return [dict(r) for r in rows]


# ══════════════════════════════════════════════════════════════════════════════
#  PLEX / EMBY / JELLYFIN PUSH
# ══════════════════════════════════════════════════════════════════════════════

async def _push_to_plex(base: str, token: str, name: str, tracks: list) -> dict:
    headers = {"Accept": "application/json"}
    async with httpx.AsyncClient(timeout=15.0) as client:
        r = await client.get(f"{base}/?X-Plex-Token={token}", headers=headers)
        if not r.is_success:
            raise HTTPException(502, f"Plex unreachable: HTTP {r.status_code}")
        machine_id = r.json().get("MediaContainer", {}).get("machineIdentifier", "")
        if not machine_id:
            raise HTTPException(502, "Could not get Plex machine ID")

        rating_keys = []
        not_found   = 0
        for track in tracks:
            if track.get("plex_rating_key"):
                rating_keys.append(track["plex_rating_key"]); continue
            try:
                sr = await client.get(f"{base}/search",
                    params={"query": track.get("title",""), "type": 10,
                            "X-Plex-Token": token}, headers=headers)
                if sr.is_success:
                    items = sr.json().get("MediaContainer",{}).get("Metadata",[])
                    match = next(
                        (i for i in items
                         if i.get("title","").lower() == track.get("title","").lower()
                         and track.get("tag_artist","").lower()[:8]
                             in i.get("grandparentTitle","").lower()),
                        items[0] if items else None)
                    if match and match.get("ratingKey"):
                        rating_keys.append(match["ratingKey"])
                    else:
                        not_found += 1
                else:
                    not_found += 1
            except Exception:
                not_found += 1

        if not rating_keys:
            raise HTTPException(404, "No tracks found in Plex")

        # Delete existing playlist with same name
        lr = await client.get(f"{base}/playlists?X-Plex-Token={token}", headers=headers)
        if lr.is_success:
            existing = next(
                (p for p in lr.json().get("MediaContainer",{}).get("Metadata",[])
                 if p.get("title") == name), None)
            if existing:
                await client.delete(
                    f"{base}/playlists/{existing['ratingKey']}?X-Plex-Token={token}")

        first_uri = (f"server://{machine_id}/com.plexapp.plugins.library"
                     f"/library/metadata/{rating_keys[0]}")
        cr = await client.post(f"{base}/playlists",
            params={"type":"audio","title":name,"smart":"0",
                    "uri":first_uri,"X-Plex-Token":token},
            headers=headers)
        if not cr.is_success:
            raise HTTPException(502, f"Plex create failed: HTTP {cr.status_code}")
        pl_id = (cr.json().get("MediaContainer",{}).get("Metadata",[{}])[0] or {}).get("ratingKey")
        if not pl_id:
            raise HTTPException(502, "No playlist ID from Plex")

        added = 1; failed = 0
        for i in range(0, len(rating_keys[1:]), 8):
            batch = rating_keys[1:][i:i+8]
            results = await asyncio.gather(*[
                client.put(f"{base}/playlists/{pl_id}/items",
                    params={"uri": f"server://{machine_id}/com.plexapp.plugins.library"
                                   f"/library/metadata/{k}",
                            "X-Plex-Token": token},
                    headers=headers)
                for k in batch], return_exceptions=True)
            for res in results:
                if isinstance(res, Exception) or not res.is_success: failed += 1
                else: added += 1

    return {"ok":True,"server":"plex","playlist":name,
            "added":added,"failed":failed,"not_found":not_found,"playlist_id":pl_id}


async def _push_to_emby(base: str, token: str, user_id: str, name: str, tracks: list) -> dict:
    """
    Emby playlist push — batches IDs to avoid 414 URI Too Long error.
    Max 50 IDs per create call.
    """
    ah = {"Accept":"application/json","X-Emby-Token":token,
          "X-Emby-Authorization":f'MediaBrowser Token="{token}"'}

    async with httpx.AsyncClient(timeout=15.0) as client:
        emby_ids = []; not_found = 0

        async def _lk(t):
            if t.get("emby_id"): return t["emby_id"]
            try:
                r = await client.get(f"{base}/Users/{user_id}/Items",
                    params={"searchTerm":t.get("title",""),"IncludeItemTypes":"Audio",
                            "Recursive":"true","api_key":token}, headers=ah)
                items = r.json().get("Items",[]) if r.is_success else []
                a = t.get("tag_artist","")
                m = next((i for i in items
                    if i.get("Name","").lower()==t.get("title","").lower()
                    and a.lower()[:8] in
                        (i.get("AlbumArtist") or (i.get("Artists") or [""])[0]).lower()),
                    items[0] if items else None)
                return m.get("Id") if m else None
            except:
                return None

        for i in range(0, len(tracks), 5):
            ids = await asyncio.gather(*[_lk(t) for t in tracks[i:i+5]])
            for eid in ids:
                if eid: emby_ids.append(eid)
                else: not_found += 1

        if not emby_ids:
            raise HTTPException(404, "No tracks found in Emby")

        # Delete existing playlist
        sr = await client.get(f"{base}/Users/{user_id}/Items",
            params={"SearchTerm":name,"IncludeItemTypes":"Playlist","api_key":token},
            headers=ah)
        if sr.is_success:
            ex = next((p for p in sr.json().get("Items",[]) if p.get("Name")==name),None)
            if ex:
                await client.delete(f"{base}/Items/{ex['Id']}?api_key={token}", headers=ah)

        # Create with first batch of max 50 IDs (fixes 414 error)
        first_batch = emby_ids[:50]
        cr = await client.post(f"{base}/Playlists",
            params={"Name":name,"Ids":",".join(first_batch),"UserId":user_id,
                    "MediaType":"Audio","api_key":token}, headers=ah)
        if not cr.is_success:
            raise HTTPException(502, f"Emby create failed: {cr.status_code}")
        pl = cr.json()
        pl_id = pl.get("Id") or pl.get("id") or pl.get("PlaylistId")
        if not pl_id:
            raise HTTPException(502, "No playlist ID from Emby")

        # Add remaining IDs in batches of 50
        for i in range(50, len(emby_ids), 50):
            batch = emby_ids[i:i+50]
            await client.post(f"{base}/Playlists/{pl_id}/Items",
                params={"Ids":",".join(batch),"api_key":token}, headers=ah)

    return {"ok":True,"server":"emby","playlist":name,
            "added":len(emby_ids),"not_found":not_found,"playlist_id":pl_id}


async def _push_to_jellyfin(base: str, token: str, user_id: str, name: str, tracks: list) -> dict:
    headers = {"Accept":"application/json","Content-Type":"application/json"}
    async with httpx.AsyncClient(timeout=15.0) as client:
        jf_ids = []; not_found = 0

        async def _lk(t):
            if t.get("jf_id"): return t["jf_id"]
            try:
                r = await client.get(f"{base}/Users/{user_id}/Items",
                    params={"searchTerm":t.get("title",""),"IncludeItemTypes":"Audio",
                            "Recursive":"true","api_key":token}, headers=headers)
                items = r.json().get("Items",[]) if r.is_success else []
                a = t.get("tag_artist","")
                m = next((i for i in items
                    if i.get("Name","").lower()==t.get("title","").lower()
                    and a.lower()[:8] in
                        (i.get("AlbumArtist") or (i.get("Artists") or [""])[0]).lower()),
                    items[0] if items else None)
                return m.get("Id") if m else None
            except:
                return None

        for i in range(0, len(tracks), 5):
            ids = await asyncio.gather(*[_lk(t) for t in tracks[i:i+5]])
            for jid in ids:
                if jid: jf_ids.append(jid)
                else: not_found += 1

        if not jf_ids:
            raise HTTPException(404, "No tracks found in Jellyfin")

        sr = await client.get(f"{base}/Users/{user_id}/Items",
            params={"SearchTerm":name,"IncludeItemTypes":"Playlist","api_key":token},
            headers=headers)
        if sr.is_success:
            ex = next((p for p in sr.json().get("Items",[]) if p.get("Name")==name),None)
            if ex:
                await client.delete(f"{base}/Items/{ex['Id']}?api_key={token}",headers=headers)

        cr = await client.post(f"{base}/Playlists?api_key={token}", headers=headers,
            json={"Name":name,"Ids":jf_ids,"UserId":user_id,"MediaType":"Audio"})
        if not cr.is_success:
            raise HTTPException(502, f"JF create failed: {cr.status_code}")
        pl = cr.json()
        pl_id = pl.get("Id") or pl.get("id")
        if not pl_id:
            raise HTTPException(502, "No playlist ID from Jellyfin")

    return {"ok":True,"server":"jellyfin","playlist":name,
            "added":len(jf_ids),"not_found":not_found,"playlist_id":pl_id}
