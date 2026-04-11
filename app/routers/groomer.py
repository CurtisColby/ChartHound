"""
ChartHound — The Groomer Router
Real Chart Data Playlist Builder

Uses a LOCAL chart reference database (real Billboard/UK historical data)
instead of guessing from Last.fm listener counts.

Two scan modes:
  A) Media Server Mode — pulls library from Plex/Emby/Jellyfin, pushes playlists back
  B) Local Folder Mode — walks physical files, M3U download only

Chart lookup:
  - Fuzzy match artist + title against chart_reference table
  - Real peak positions and weeks on chart from Billboard/UK datasets
  - No API calls during scan — pure local SQLite lookup

Chart data loading:
  - Hot 100: GitHub CSV (HipsterVizNinja dataset), 1958–present
  - Other charts: billboard-charts Python library (scrapes Billboard.com)
  - One-time load, weekly refresh via button or APScheduler

Endpoints:
  Chart data management:
  - GET  /api/groomer/charts/status        — status of all chart datasets
  - POST /api/groomer/charts/load          — download/refresh chart data (background)
  - GET  /api/groomer/charts/load/status   — poll load progress

  Scan:
  - POST /api/groomer/scan/start           — start scan (media server or local folder)
  - GET  /api/groomer/scan/status/{id}     — poll scan progress
  - POST /api/groomer/scan/stop            — stop running scan

  Results:
  - GET  /api/groomer/results              — query chart_data with filters
  - GET  /api/groomer/db_stats             — DB stats for UI header
  - POST /api/groomer/playlist/push        — push to Plex/Emby/Jellyfin
  - POST /api/groomer/playlist/m3u         — download as M3U file
"""

import asyncio
import csv
import difflib
import io
import json
import logging
import os
import re
import aiosqlite
import httpx

from datetime import datetime, timezone
from typing import Optional, List
from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel

from app.config import get_settings
from app.deps import require_auth
from app.security import decrypt_token

# Shared tag writer from Retriever (Constitution: File-First)
from app.routers.retriever import (
    write_tags as _write_tags_fn,
    peak_to_stars,
    format_chart_comment,
)

log = logging.getLogger("charthound.groomer")
router = APIRouter(prefix="/api/groomer", tags=["groomer"])
settings = get_settings()

# ── Chart display names ───────────────────────────────────────────────────────
CHART_DISPLAY = {
    "hot100":   "Hot 100",        "adultpop": "Adult Pop",
    "ac":       "Adult Contemp",  "uk":       "UK Singles",
    "country":  "Country",        "rnb":      "R&B/Hip-Hop",
    "dance":    "Dance",          "rock":     "Mainstream Rock",
    "ccm":      "CCM",            "ccm-ac":   "CCM-AC",
    "ccm-rock": "CCM Rock",       "worship":  "Worship",
    "gospel":   "Gospel",         "sgospel":  "Southern Gospel",
    "ugospel":  "Urban Gospel",   "tgospel":  "Traditional Gospel",
}

# Billboard chart name → billboard-charts library slug
BILLBOARD_SLUGS = {
    "ac":       "adult-contemporary",
    "adultpop": "pop-songs",
    "country":  "hot-country-songs",
    "rnb":      "hot-r-and-b-hip-hop-songs",
    "rock":     "mainstream-rock-tracks",
    "dance":    "dance-club-songs",
    "ccm":      "christian-songs",
    "gospel":   "gospel-songs",
    "ccm-ac":   "christian-ac-tips",
    "ccm-rock": "christian-ac-tips",
}

# Fuzzy match threshold
MATCH_THRESHOLD = 0.82

# Hot 100 CSV — free GitHub dataset, updated weekly
HOT100_CSV_URL = (
    "https://raw.githubusercontent.com/HipsterVizNinja/"
    "random-data/main/Music/hot-100/Hot%20100.csv"
)

# ── In-memory load job tracker ────────────────────────────────────────────────
_load_job = {"status": "idle", "message": "", "loaded": 0, "total": 0}


# ══════════════════════════════════════════════════════════════════════════════
#  PYDANTIC MODELS
# ══════════════════════════════════════════════════════════════════════════════

class GroomerScanRequest(BaseModel):
    mode:          str            # "server" | "local"
    server:        str = ""       # "plex" | "emby" | "jellyfin"
    local_path:    str = ""       # Docker path for local mode
    charts:        List[str] = []
    peak_max:      int = 40
    weeks_min:     int = 1
    moods:         List[str] = []
    playlist_name: str = "Top 40 Hits"
    write_tags:    bool = False
    chunk_size:    int = 50


class PlaylistPushRequest(BaseModel):
    server:        str
    playlist_name: str
    track_ids:     List[int]


class M3URequest(BaseModel):
    track_ids:     List[int]
    playlist_name: str = "ChartHound Playlist"


class ChartLoadRequest(BaseModel):
    charts: List[str] = []


# ══════════════════════════════════════════════════════════════════════════════
#  CHART REFERENCE STATUS & LOAD
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/charts/status")
async def chart_status(user: dict = Depends(require_auth)):
    async with aiosqlite.connect(settings.database_url) as db:
        db.row_factory = aiosqlite.Row
        cur  = await db.execute("SELECT * FROM chart_reference_meta ORDER BY chart_name")
        rows = await cur.fetchall()
        cur2 = await db.execute("SELECT COUNT(*) AS n FROM chart_reference")
        total = (await cur2.fetchone())["n"]
    return {"charts": [dict(r) for r in rows], "total_entries": total, "load_job": _load_job}


@router.get("/charts/load/status")
async def chart_load_status(user: dict = Depends(require_auth)):
    return _load_job


@router.post("/charts/load")
async def chart_load(
    req: ChartLoadRequest,
    background_tasks: BackgroundTasks,
    user: dict = Depends(require_auth),
):
    if _load_job["status"] == "running":
        raise HTTPException(409, "A chart load is already running.")
    charts_to_load = req.charts if req.charts else list(BILLBOARD_SLUGS.keys()) + ["hot100"]
    background_tasks.add_task(run_chart_load, charts_to_load)
    return {"ok": True, "message": f"Loading {len(charts_to_load)} chart(s) in background."}


async def run_chart_load(charts: List[str]):
    global _load_job
    _load_job = {"status": "running", "message": "Starting…", "loaded": 0, "total": len(charts)}
    total_inserted = 0

    for chart_name in charts:
        _load_job["message"] = f"Loading {CHART_DISPLAY.get(chart_name, chart_name)}…"
        log.info(f"[ChartLoad] Loading: {chart_name}")
        try:
            if chart_name == "hot100":
                inserted = await _load_hot100_csv()
            else:
                inserted = await _load_billboard_chart(chart_name)
            total_inserted += inserted
            _load_job["loaded"] += 1

            now = datetime.now(timezone.utc).isoformat()
            async with aiosqlite.connect(settings.database_url) as db:
                db.row_factory = aiosqlite.Row
                cur  = await db.execute(
                    "SELECT COUNT(*) AS n FROM chart_reference WHERE chart_name=?", (chart_name,))
                count = (await cur.fetchone())["n"]
                cur2 = await db.execute(
                    "SELECT MIN(chart_year) AS mn, MAX(chart_year) AS mx "
                    "FROM chart_reference WHERE chart_name=?", (chart_name,))
                yr = await cur2.fetchone()
                await db.execute(
                    "UPDATE chart_reference_meta SET status='loaded', entry_count=?, "
                    "first_year=?, last_year=?, last_updated=? WHERE chart_name=?",
                    (count, yr["mn"], yr["mx"], now, chart_name))
                await db.commit()
        except Exception as e:
            log.error(f"[ChartLoad] Failed {chart_name}: {e}")
            _load_job["message"] = f"Error: {chart_name}: {e}"
            async with aiosqlite.connect(settings.database_url) as db:
                await db.execute(
                    "UPDATE chart_reference_meta SET status='error' WHERE chart_name=?",
                    (chart_name,))
                await db.commit()
        await asyncio.sleep(0.5)

    _load_job = {
        "status": "done",
        "message": f"Done — {total_inserted:,} entries across {len(charts)} chart(s).",
        "loaded": len(charts), "total": len(charts),
    }


async def _load_hot100_csv() -> int:
    log.info("[ChartLoad] Fetching Hot 100 CSV…")
    async with httpx.AsyncClient(timeout=60.0) as client:
        r = await client.get(HOT100_CSV_URL)
    if not r.is_success:
        raise Exception(f"HTTP {r.status_code}")
    content = r.content.decode("utf-8", errors="replace")
    reader  = csv.DictReader(io.StringIO(content))

    songs: dict = {}
    for row in reader:
        try:
            artist    = (row.get("performer") or "").strip()
            title     = (row.get("song") or "").strip()
            peak_str  = (row.get("peak_position") or "").strip()
            weeks_str = (row.get("time_on_chart") or "").strip()
            date_str  = (row.get("chart_date") or row.get("chart_debut") or "").strip()
            if not artist or not title:
                continue
            peak  = int(peak_str)  if peak_str.isdigit()  else None
            weeks = int(weeks_str) if weeks_str.isdigit() else 1
            year  = int(date_str[:4]) if date_str and date_str[:4].isdigit() else None
            if not peak:
                continue
            key = (_norm(artist), _norm(title))
            if key not in songs:
                songs[key] = {"artist": artist, "title": title,
                              "peak": peak, "weeks": weeks, "year": year}
            else:
                if peak < songs[key]["peak"]:   songs[key]["peak"]  = peak
                if weeks > songs[key]["weeks"]: songs[key]["weeks"] = weeks
        except Exception:
            continue

    inserted = 0
    now = datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(settings.database_url) as db:
        for (an, tn), d in songs.items():
            try:
                await db.execute(
                    """INSERT INTO chart_reference
                       (chart_name,artist,title,artist_norm,title_norm,
                        peak_position,weeks_on_chart,chart_year,data_source,loaded_at)
                       VALUES ('hot100',?,?,?,?,?,?,'hot100_csv',?)
                       ON CONFLICT(chart_name,artist_norm,title_norm) DO UPDATE SET
                           peak_position =MIN(peak_position,excluded.peak_position),
                           weeks_on_chart=MAX(weeks_on_chart,excluded.weeks_on_chart),
                           loaded_at=excluded.loaded_at""",
                    (d["artist"], d["title"], an, tn,
                     d["peak"], d["weeks"], d["year"], now))
                inserted += 1
            except Exception:
                pass
        await db.commit()
    return inserted


async def _load_billboard_chart(chart_name: str) -> int:
    slug = BILLBOARD_SLUGS.get(chart_name)
    if not slug:
        return 0
    try:
        import billboard  # type: ignore
    except ImportError:
        raise Exception("billboard-charts not installed. Run: pip install billboard.py")

    songs: dict = {}
    current_year = datetime.now().year

    for year in range(current_year, 1990, -1):
        try:
            chart = await asyncio.get_event_loop().run_in_executor(None, lambda: billboard.ChartData(f"year-end/{year}/{slug}", timeout=15))
            for entry in chart:
                artist = (entry.artist or "").strip()
                title  = (entry.title  or "").strip()
                if not artist or not title:
                    continue
                peak  = entry.peakPos or entry.rank or 100
                weeks = entry.weeks or 1
                key   = (_norm(artist), _norm(title))
                if key not in songs:
                    songs[key] = {"artist": artist, "title": title,
                                  "peak": peak, "weeks": weeks, "year": year}
                else:
                    if peak  < songs[key]["peak"]:  songs[key]["peak"]  = peak
                    if weeks > songs[key]["weeks"]: songs[key]["weeks"] = weeks
            await asyncio.sleep(0.3)
        except Exception:
            continue

    try:
        chart = await asyncio.get_event_loop().run_in_executor(None, lambda: billboard.ChartData(slug, timeout=15))
        for entry in chart:
            artist = (entry.artist or "").strip()
            title  = (entry.title  or "").strip()
            if not artist or not title:
                continue
            key = (_norm(artist), _norm(title))
            peak = entry.peakPos or entry.rank or 100
            if key not in songs:
                songs[key] = {"artist": artist, "title": title,
                              "peak": peak, "weeks": entry.weeks or 1,
                              "year": current_year}
            elif peak < songs[key]["peak"]:
                songs[key]["peak"] = peak
    except Exception:
        pass

    inserted = 0
    now = datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(settings.database_url) as db:
        for (an, tn), d in songs.items():
            try:
                await db.execute(
                    """INSERT INTO chart_reference
                       (chart_name,artist,title,artist_norm,title_norm,
                        peak_position,weeks_on_chart,chart_year,data_source,loaded_at)
                       VALUES (?,?,?,?,?,?,?,'billboard_scrape',?)
                       ON CONFLICT(chart_name,artist_norm,title_norm) DO UPDATE SET
                           peak_position =MIN(peak_position,excluded.peak_position),
                           weeks_on_chart=MAX(weeks_on_chart,excluded.weeks_on_chart),
                           loaded_at=excluded.loaded_at""",
                    (chart_name, d["artist"], d["title"], an, tn,
                     d["peak"], d["weeks"], d["year"], now))
                inserted += 1
            except Exception:
                pass
        await db.commit()
    return inserted


# ══════════════════════════════════════════════════════════════════════════════
#  FUZZY CHART LOOKUP
# ══════════════════════════════════════════════════════════════════════════════

def _norm(text: str) -> str:
    if not text:
        return ""
    t = text.lower().strip()
    t = re.sub(r'^the\s+', '', t)
    t = re.sub(r'^a\s+', '', t)
    t = re.sub(r"[^\w\s]", "", t)
    t = re.sub(r'\s+', ' ', t).strip()
    return t


async def lookup_chart_hits(
    artist: str, title: str,
    charts: List[str], peak_max: int, weeks_min: int,
) -> List[dict]:
    if not artist or not title:
        return []
    artist_n = _norm(artist)
    title_n  = _norm(title)
    if not artist_n or not title_n:
        return []

    chart_filter = ""
    params: list = [artist_n, title_n]
    if charts:
        placeholders = ",".join(["?" for _ in charts])
        chart_filter = f"AND chart_name IN ({placeholders})"
        params += charts
    params += [peak_max, weeks_min]

    async with aiosqlite.connect(settings.database_url) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            f"""SELECT chart_name, artist, title, peak_position, weeks_on_chart, chart_year
                FROM chart_reference
                WHERE artist_norm=? AND title_norm=?
                {chart_filter}
                AND peak_position<=? AND weeks_on_chart>=?""",
            params)
        exact = await cur.fetchall()
    if exact:
        return [dict(r) for r in exact]

    # Fuzzy fallback
    title_prefix = title_n[:4] if len(title_n) >= 4 else title_n
    fp = [f"%{title_prefix}%"] + (charts if charts else []) + [peak_max, weeks_min]
    async with aiosqlite.connect(settings.database_url) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            f"""SELECT chart_name, artist, title, artist_norm, title_norm,
                       peak_position, weeks_on_chart, chart_year
                FROM chart_reference
                WHERE title_norm LIKE ?
                {chart_filter}
                AND peak_position<=? AND weeks_on_chart>=?
                LIMIT 200""",
            fp)
        candidates = await cur.fetchall()

    hits = []
    for row in candidates:
        a_score = difflib.SequenceMatcher(None, artist_n, row["artist_norm"]).ratio()
        t_score = difflib.SequenceMatcher(None, title_n,  row["title_norm"]).ratio()
        if a_score >= MATCH_THRESHOLD and t_score >= MATCH_THRESHOLD:
            hits.append({
                "chart_name":     row["chart_name"],
                "artist":         row["artist"],
                "title":          row["title"],
                "peak_position":  row["peak_position"],
                "weeks_on_chart": row["weeks_on_chart"],
                "chart_year":     row["chart_year"],
                "match_score":    round((a_score * 0.45) + (t_score * 0.55), 3),
            })
    return hits


# ══════════════════════════════════════════════════════════════════════════════
#  SCAN ENDPOINTS
# ══════════════════════════════════════════════════════════════════════════════

@router.post("/scan/start")
async def groomer_scan_start(
    req: GroomerScanRequest,
    background_tasks: BackgroundTasks,
    user: dict = Depends(require_auth),
):
    if req.mode not in ("server", "local"):
        raise HTTPException(400, "mode must be 'server' or 'local'")
    if req.mode == "server" and req.server not in ("plex", "emby", "jellyfin"):
        raise HTTPException(400, "server must be 'plex', 'emby', or 'jellyfin'")
    if req.mode == "local" and not req.local_path:
        raise HTTPException(400, "local_path required for local mode")

    async with aiosqlite.connect(settings.database_url) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT COUNT(*) AS n FROM chart_reference")
        ref_count = (await cur.fetchone())["n"]
    if ref_count == 0:
        raise HTTPException(400,
            "Chart reference database is empty. Load chart data first.")

    async with aiosqlite.connect(settings.database_url) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT job_id FROM scan_jobs WHERE job_type='groomer' AND status='running'")
        if await cur.fetchone():
            raise HTTPException(409, "A Groomer scan is already running.")

    valid_charts = [c for c in req.charts if c in CHART_DISPLAY]
    now = datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(settings.database_url) as db:
        cur = await db.execute(
            "INSERT INTO scan_jobs "
            "(job_type,status,scope,mode,started_at,total_tracks,processed,matched,failed,cached) "
            "VALUES ('groomer','running',?,?,?,0,0,0,0,0)",
            (req.server or req.local_path, req.mode, now))
        await db.commit()
        job_id = cur.lastrowid

    background_tasks.add_task(run_groomer_scan, job_id=job_id, req=req, valid_charts=valid_charts)
    return {"ok": True, "job_id": job_id, "mode": req.mode,
            "server": req.server, "ref_entries": ref_count}


@router.get("/scan/status/{job_id}")
async def groomer_scan_status(job_id: int, user: dict = Depends(require_auth)):
    async with aiosqlite.connect(settings.database_url) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM scan_jobs WHERE job_id=?", (job_id,))
        job = await cur.fetchone()
    if not job:
        raise HTTPException(404, f"Job {job_id} not found")
    return dict(job)


@router.post("/scan/stop")
async def groomer_scan_stop(user: dict = Depends(require_auth)):
    now = datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(settings.database_url) as db:
        await db.execute(
            "UPDATE scan_jobs SET status='stopped', completed_at=? "
            "WHERE job_type='groomer' AND status='running'", (now,))
        await db.commit()
    return {"ok": True, "status": "stopped"}


# ══════════════════════════════════════════════════════════════════════════════
#  BACKGROUND SCAN JOB
# ══════════════════════════════════════════════════════════════════════════════

async def run_groomer_scan(job_id: int, req: GroomerScanRequest, valid_charts: List[str]):
    log.info(f"[Groomer {job_id}] Starting — mode={req.mode} scope={req.server or req.local_path}")

    token = base_url = user_id = ""
    if req.mode == "server":
        async with aiosqlite.connect(settings.database_url) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT base_url, token_enc, extra_json FROM connections WHERE service=?",
                (req.server,))
            conn = await cur.fetchone()
        if not conn:
            await _fail_job(job_id, f"{req.server} not connected")
            return
        token    = decrypt_token(conn["token_enc"]) if conn["token_enc"] else ""
        base_url = (conn["base_url"] or "").rstrip("/")
        extra    = json.loads(conn["extra_json"] or "{}") if conn["extra_json"] else {}
        user_id  = extra.get("user_id", "")

    try:
        if req.mode == "server":
            if req.server == "plex":
                tracks = await _fetch_plex_library(base_url, token)
            elif req.server == "emby":
                tracks = await _fetch_emby_library(base_url, token, user_id)
            else:
                tracks = await _fetch_jellyfin_library(base_url, token, user_id)
        else:
            tracks = _walk_local_folder(req.local_path)
    except Exception as e:
        await _fail_job(job_id, f"Failed to get tracks: {e}")
        return

    total = len(tracks)
    log.info(f"[Groomer {job_id}] {total} tracks to process")
    if total == 0:
        await _fail_job(job_id, "No tracks found")
        return

    async with aiosqlite.connect(settings.database_url) as db:
        await db.execute("UPDATE scan_jobs SET total_tracks=? WHERE job_id=?", (total, job_id))
        await db.commit()

    processed = matched = failed = cached = 0
    chunks = [tracks[i:i+req.chunk_size] for i in range(0, total, req.chunk_size)]

    for chunk in chunks:
        async with aiosqlite.connect(settings.database_url) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute("SELECT status FROM scan_jobs WHERE job_id=?", (job_id,))
            job = await cur.fetchone()
            if job and job["status"] == "stopped":
                return

        for track in chunk:
            try:
                result = await _process_track(
                    track=track, mode=req.mode, server=req.server,
                    charts=valid_charts, peak_max=req.peak_max,
                    weeks_min=req.weeks_min, write_tags=req.write_tags)
                processed += 1
                if result == "hit":    matched += 1
                elif result == "cache": cached += 1; matched += 1
            except Exception as e:
                log.debug(f"[Groomer {job_id}] Error: {e}")
                failed += 1; processed += 1

        async with aiosqlite.connect(settings.database_url) as db:
            await db.execute(
                "UPDATE scan_jobs SET processed=?,matched=?,failed=?,cached=? WHERE job_id=?",
                (processed, matched, failed, cached, job_id))
            await db.commit()

    now = datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(settings.database_url) as db:
        await db.execute(
            "UPDATE scan_jobs SET status='done',completed_at=?,"
            "processed=?,matched=?,failed=?,cached=? WHERE job_id=?",
            (now, processed, matched, failed, cached, job_id))
        await db.commit()

    log.info(f"[Groomer {job_id}] Done — {matched} hits, {cached} cached, {failed} failed")


async def _process_track(
    track: dict, mode: str, server: str,
    charts: List[str], peak_max: int, weeks_min: int, write_tags: bool,
) -> str:
    artist    = (track.get("artist") or "").strip()
    title     = (track.get("title") or "").strip()
    file_path = (track.get("file_path") or "").strip()
    server_id = track.get("server_id", "")
    if not artist or not title:
        return "miss"

    # Cache check
    async with aiosqlite.connect(settings.database_url) as db:
        db.row_factory = aiosqlite.Row
        if file_path:
            cur = await db.execute(
                "SELECT track_id FROM tracks WHERE file_path=?", (file_path,))
        else:
            cur = await db.execute(
                "SELECT track_id FROM tracks "
                "WHERE LOWER(tag_artist)=LOWER(?) AND LOWER(title)=LOWER(?)",
                (artist, title))
        existing = await cur.fetchone()
        track_id = existing["track_id"] if existing else None

        if track_id:
            cur2 = await db.execute(
                "SELECT COUNT(*) AS n FROM chart_data WHERE track_id=?", (track_id,))
            if (await cur2.fetchone())["n"] > 0:
                await _update_server_id(track_id, server, server_id)
                return "cache"

    # Chart lookup
    hits = await lookup_chart_hits(artist, title, charts, peak_max, weeks_min)
    if not hits:
        return "miss"

    now = datetime.now(timezone.utc).isoformat()
    if not track_id:
        async with aiosqlite.connect(settings.database_url) as db:
            cur = await db.execute(
                "INSERT INTO tracks (file_path,title,tag_artist,metadata_source,"
                "last_scanned,last_updated) VALUES (?,?,?,'groomer_chart',?,?) "
                "ON CONFLICT(file_path) DO UPDATE SET last_scanned=excluded.last_scanned",
                (file_path or f"__groomer__{_norm(artist)}__{_norm(title)}",
                 title, artist, now, now))
            track_id = cur.lastrowid
            await db.commit()

    async with aiosqlite.connect(settings.database_url) as db:
        for hit in hits:
            try:
                await db.execute(
                    "INSERT INTO chart_data "
                    "(track_id,chart_name,peak_position,weeks_on_chart,star_rating,"
                    "confidence,fetched_at) VALUES (?,?,?,?,?,'high',?) "
                    "ON CONFLICT(track_id,chart_name) DO UPDATE SET "
                    "peak_position=MIN(peak_position,excluded.peak_position),"
                    "weeks_on_chart=MAX(weeks_on_chart,excluded.weeks_on_chart),"
                    "star_rating=excluded.star_rating,confidence='high',"
                    "fetched_at=excluded.fetched_at",
                    (track_id, hit["chart_name"], hit["peak_position"],
                     hit["weeks_on_chart"], peak_to_stars(hit["peak_position"]), now))
            except Exception:
                pass
        await db.commit()

    await _update_server_id(track_id, server, server_id)

    if write_tags and file_path and os.path.exists(file_path) and hits:
        try:
            comment = format_chart_comment([{
                "chart_name": h["chart_name"], "peak_position": h["peak_position"],
                "weeks_on_chart": h["weeks_on_chart"],
                "star_rating": peak_to_stars(h["peak_position"]),
                "confidence": "high", "listener_count": 0,
            } for h in hits])
            _write_tags_fn(file_path, [], [], None, comment)
        except Exception as e:
            log.warning(f"Tag write failed {file_path}: {e}")

    return "hit"


async def _update_server_id(track_id: int, server: str, server_id: str):
    if not server_id or not track_id or not server:
        return
    col = {"plex": "plex_rating_key", "emby": "emby_id", "jellyfin": "jf_id"}.get(server)
    if not col:
        return
    async with aiosqlite.connect(settings.database_url) as db:
        await db.execute(
            f"UPDATE tracks SET {col}=? WHERE track_id=?", (server_id, track_id))
        await db.commit()


async def _fail_job(job_id: int, reason: str):
    log.error(f"[Groomer {job_id}] FAILED: {reason}")
    now = datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(settings.database_url) as db:
        await db.execute(
            "UPDATE scan_jobs SET status='failed',completed_at=? WHERE job_id=?",
            (now, job_id))
        await db.commit()


# ══════════════════════════════════════════════════════════════════════════════
#  MEDIA SERVER LIBRARY FETCHERS
# ══════════════════════════════════════════════════════════════════════════════

async def _fetch_plex_library(base: str, token: str) -> List[dict]:
    tracks = []
    headers = {"Accept": "application/json"}
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.get(f"{base}/library/sections?X-Plex-Token={token}", headers=headers)
        if not r.is_success:
            raise Exception(f"Plex unreachable: HTTP {r.status_code}")
        sections = r.json().get("MediaContainer", {}).get("Directory", [])
        music_sections = [s for s in sections if s.get("type") == "artist"]
        if not music_sections:
            raise Exception("No music library in Plex")
        for section in music_sections:
            offset = 0
            PAGE = 500
            while True:
                tr = await client.get(
                    f"{base}/library/sections/{section['key']}/all",
                    params={"type": 10, "X-Plex-Token": token,
                            "X-Plex-Container-Start": offset,
                            "X-Plex-Container-Size": PAGE},
                    headers=headers)
                if not tr.is_success:
                    break
                try:
                    items = json.loads(
                        tr.content.decode("utf-8", errors="replace")
                    ).get("MediaContainer", {}).get("Metadata", [])
                except Exception:
                    break
                if not items:
                    break
                for item in items:
                    file_path = ""
                    media = item.get("Media", [{}])
                    if media:
                        parts = media[0].get("Part", [{}])
                        if parts:
                            file_path = parts[0].get("file", "")
                    if (file_path and settings.media_server_music_prefix
                            and settings.docker_music_prefix
                            and file_path.startswith(settings.media_server_music_prefix)):
                        file_path = (settings.docker_music_prefix
                                     + file_path[len(settings.media_server_music_prefix):])
                    tracks.append({
                        "title":     item.get("title", ""),
                        "artist":    item.get("originalTitle") or item.get("grandparentTitle", ""),
                        "album":     item.get("parentTitle", ""),
                        "file_path": file_path,
                        "server_id": item.get("ratingKey", ""),
                    })
                offset += PAGE
                if len(items) < PAGE:
                    break
                await asyncio.sleep(0.05)
    log.info(f"Plex: {len(tracks)} tracks")
    return tracks


async def _fetch_emby_library(base: str, token: str, user_id: str) -> List[dict]:
    tracks = []
    headers = {"Accept": "application/json", "X-Emby-Token": token}
    async with httpx.AsyncClient(timeout=30.0) as client:
        offset = 0
        PAGE = 500
        while True:
            r = await client.get(
                f"{base}/Users/{user_id}/Items",
                params={"IncludeItemTypes": "Audio", "Recursive": "true",
                        "Fields": "Path", "StartIndex": offset,
                        "Limit": PAGE, "api_key": token},
                headers=headers)
            if not r.is_success:
                break
            items = r.json().get("Items", [])
            if not items:
                break
            for item in items:
                file_path = item.get("Path", "")
                if (file_path and settings.media_server_music_prefix
                        and settings.docker_music_prefix
                        and file_path.startswith(settings.media_server_music_prefix)):
                    file_path = (settings.docker_music_prefix
                                 + file_path[len(settings.media_server_music_prefix):])
                tracks.append({
                    "title":     item.get("Name", ""),
                    "artist":    item.get("AlbumArtist") or (item.get("Artists") or [""])[0],
                    "album":     item.get("Album", ""),
                    "file_path": file_path,
                    "server_id": item.get("Id", ""),
                })
            offset += PAGE
            if len(items) < PAGE:
                break
            await asyncio.sleep(0.05)
    log.info(f"Emby: {len(tracks)} tracks")
    return tracks


async def _fetch_jellyfin_library(base: str, token: str, user_id: str) -> List[dict]:
    tracks = []
    headers = {"Accept": "application/json"}
    async with httpx.AsyncClient(timeout=30.0) as client:
        offset = 0
        PAGE = 500
        while True:
            r = await client.get(
                f"{base}/Users/{user_id}/Items",
                params={"IncludeItemTypes": "Audio", "Recursive": "true",
                        "Fields": "Path", "StartIndex": offset,
                        "Limit": PAGE, "api_key": token},
                headers=headers)
            if not r.is_success:
                break
            items = r.json().get("Items", [])
            if not items:
                break
            for item in items:
                file_path = item.get("Path", "")
                if (file_path and settings.media_server_music_prefix
                        and settings.docker_music_prefix
                        and file_path.startswith(settings.media_server_music_prefix)):
                    file_path = (settings.docker_music_prefix
                                 + file_path[len(settings.media_server_music_prefix):])
                tracks.append({
                    "title":     item.get("Name", ""),
                    "artist":    item.get("AlbumArtist") or (item.get("Artists") or [""])[0],
                    "album":     item.get("Album", ""),
                    "file_path": file_path,
                    "server_id": item.get("Id", ""),
                })
            offset += PAGE
            if len(items) < PAGE:
                break
            await asyncio.sleep(0.05)
    log.info(f"Jellyfin: {len(tracks)} tracks")
    return tracks


def _walk_local_folder(root_path: str) -> List[dict]:
    from mutagen import File as MFile
    AUDIO_EXTS = {".mp3", ".flac", ".m4a", ".aac", ".ogg", ".opus",
                  ".wma", ".wav", ".aiff", ".ape", ".wv"}
    tracks = []
    if not os.path.exists(root_path):
        return tracks
    for dirpath, _, fnames in os.walk(root_path):
        for fname in fnames:
            if os.path.splitext(fname)[1].lower() not in AUDIO_EXTS:
                continue
            fpath = os.path.join(dirpath, fname)
            try:
                f = MFile(fpath, easy=True)
                if f:
                    artist = (f.get("artist", [""])[0] or
                              f.get("albumartist", [""])[0] or "").strip()
                    title  = (f.get("title", [""])[0] or
                              os.path.splitext(fname)[0]).strip()
                    tracks.append({
                        "title": title, "artist": artist,
                        "album": (f.get("album", [""])[0] or "").strip(),
                        "file_path": fpath, "server_id": "",
                    })
            except Exception:
                pass
    log.info(f"Local: {len(tracks)} tracks in {root_path}")
    return tracks


# ══════════════════════════════════════════════════════════════════════════════
#  RESULTS & DB STATS
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/db_stats")
async def db_stats(user: dict = Depends(require_auth)):
    async with aiosqlite.connect(settings.database_url) as db:
        db.row_factory = aiosqlite.Row
        cur  = await db.execute("SELECT COUNT(*) AS n FROM tracks")
        total = (await cur.fetchone())["n"]
        cur2 = await db.execute("SELECT COUNT(DISTINCT track_id) AS n FROM chart_data")
        with_charts = (await cur2.fetchone())["n"]
        cur3 = await db.execute("SELECT COUNT(*) AS n FROM chart_reference")
        ref_total = (await cur3.fetchone())["n"]
        cur4 = await db.execute(
            "SELECT * FROM scan_jobs WHERE job_type='groomer' AND status='running' "
            "ORDER BY job_id DESC LIMIT 1")
        active_job = await cur4.fetchone()
    return {
        "total_tracks": total, "tracks_with_charts": with_charts,
        "chart_ref_entries": ref_total,
        "active_job": dict(active_job) if active_job else None,
    }


@router.get("/results")
async def groomer_results(
    charts: str = "", peak_max: int = 100, weeks_min: int = 1,
    moods: str = "", limit: int = 5000,
    user: dict = Depends(require_auth),
):
    req_charts = [c.strip() for c in charts.split(",")
                  if c.strip() and c.strip() in CHART_DISPLAY]
    req_moods  = [m.strip() for m in moods.split(",") if m.strip()]

    async with aiosqlite.connect(settings.database_url) as db:
        db.row_factory = aiosqlite.Row
        conds = [f"cd.peak_position<={peak_max}", f"cd.weeks_on_chart>={weeks_min}",
                 "cd.peak_position IS NOT NULL"]
        params = []
        if req_charts:
            ph = ",".join(["?" for _ in req_charts])
            conds.append(f"cd.chart_name IN ({ph})")
            params = req_charts[:]
        where = " AND ".join(conds)
        params.append(limit)
        cur = await db.execute(
            f"""SELECT t.track_id, t.file_path, t.tag_artist AS artist,
                       t.tag_album AS album, t.title, t.year,
                       t.genre_1, t.genre_2, t.genre_3,
                       t.mood_1, t.mood_2, t.mood_3,
                       t.plex_rating_key, t.emby_id, t.jf_id,
                       cd.chart_name, cd.peak_position, cd.weeks_on_chart,
                       cd.star_rating, cd.confidence AS chart_confidence,
                       cd.comment_string
                FROM chart_data cd JOIN tracks t ON cd.track_id=t.track_id
                WHERE {where}
                ORDER BY cd.peak_position ASC, cd.weeks_on_chart DESC
                LIMIT ?""",
            params)
        rows = await cur.fetchall()

    results = []
    seen = {}
    for row in rows:
        r = dict(row)
        tid = r["track_id"]
        if req_moods:
            tm = {r.get("mood_1"), r.get("mood_2"), r.get("mood_3")} - {None}
            if not any(m in tm for m in req_moods):
                continue
        r["genres"]        = [g for g in [r.get("genre_1"), r.get("genre_2"), r.get("genre_3")] if g]
        r["moods_list"]    = [m for m in [r.get("mood_1"), r.get("mood_2"), r.get("mood_3")] if m]
        r["chart_display"] = CHART_DISPLAY.get(r["chart_name"], r["chart_name"])
        r["stars"]         = r.get("star_rating", 0) or 0
        if tid in seen:
            ex = results[seen[tid]]
            if r["chart_display"] not in ex.get("all_charts", []):
                ex.setdefault("all_charts", [ex["chart_display"]]).append(r["chart_display"])
            if (r["peak_position"] or 999) < (ex["peak_position"] or 999):
                ex["peak_position"] = r["peak_position"]
                ex["weeks_on_chart"] = r["weeks_on_chart"]
                ex["stars"] = r["stars"]
        else:
            r["all_charts"] = [r["chart_display"]]
            seen[tid] = len(results)
            results.append(r)

    return {"count": len(results), "results": results}


# ══════════════════════════════════════════════════════════════════════════════
#  M3U DOWNLOAD
# ══════════════════════════════════════════════════════════════════════════════

@router.post("/playlist/m3u")
async def download_m3u(req: M3URequest, user: dict = Depends(require_auth)):
    if not req.track_ids:
        raise HTTPException(400, "No tracks provided")
    async with aiosqlite.connect(settings.database_url) as db:
        db.row_factory = aiosqlite.Row
        ph  = ",".join(["?" for _ in req.track_ids])
        cur = await db.execute(
            f"SELECT file_path, tag_artist, title FROM tracks WHERE track_id IN ({ph})",
            req.track_ids)
        rows = await cur.fetchall()
    if not rows:
        raise HTTPException(404, "No tracks found")
    lines = ["#EXTM3U", f"#PLAYLIST:{req.playlist_name}", ""]
    for row in rows:
        path = row["file_path"] or ""
        if (path and settings.docker_music_prefix and settings.media_server_music_prefix
                and path.startswith(settings.docker_music_prefix)):
            path = settings.media_server_music_prefix + path[len(settings.docker_music_prefix):]
        lines.append(f"#EXTINF:-1,{row['tag_artist'] or ''} - {row['title'] or ''}")
        lines.append(path)
    filename = req.playlist_name.replace(" ", "_") + ".m3u"
    return PlainTextResponse(
        content="\n".join(lines), media_type="audio/x-mpegurl",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'})


# ══════════════════════════════════════════════════════════════════════════════
#  PLAYLIST PUSH
# ══════════════════════════════════════════════════════════════════════════════

@router.post("/playlist/push")
async def push_playlist(req: PlaylistPushRequest, user: dict = Depends(require_auth)):
    if req.server not in ("plex", "emby", "jellyfin"):
        raise HTTPException(400, "Invalid server")
    if not req.track_ids:
        raise HTTPException(400, "No tracks provided")
    if not req.playlist_name.strip():
        raise HTTPException(400, "Playlist name cannot be empty")

    async with aiosqlite.connect(settings.database_url) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT base_url, token_enc, extra_json FROM connections WHERE service=?",
            (req.server,))
        conn = await cur.fetchone()
        if not conn:
            raise HTTPException(404, f"{req.server} not connected in The Kennel")
        token    = decrypt_token(conn["token_enc"]) if conn["token_enc"] else ""
        base_url = (conn["base_url"] or "").rstrip("/")
        ph  = ",".join(["?" for _ in req.track_ids])
        cur2 = await db.execute(
            f"SELECT track_id, tag_artist, title, plex_rating_key, emby_id, jf_id "
            f"FROM tracks WHERE track_id IN ({ph})", req.track_ids)
        tracks = [dict(r) for r in await cur2.fetchall()]

    if not tracks:
        raise HTTPException(404, "No tracks found")

    try:
        if req.server == "plex":
            return await _push_to_plex(base_url, token, req.playlist_name, tracks)
        extra   = json.loads(conn["extra_json"] or "{}") if conn["extra_json"] else {}
        user_id = extra.get("user_id", "")
        if req.server == "emby":
            return await _push_to_emby(base_url, token, user_id, req.playlist_name, tracks)
        return await _push_to_jellyfin(base_url, token, user_id, req.playlist_name, tracks)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"Push failed: {e}")


# ══════════════════════════════════════════════════════════════════════════════
#  PLEX / EMBY / JELLYFIN PUSH (preserved from original)
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
        not_found = 0
        for track in tracks:
            if track.get("plex_rating_key"):
                rating_keys.append(track["plex_rating_key"]); continue
            try:
                sr = await client.get(
                    f"{base}/search",
                    params={"query": track.get("title",""), "type": 10,
                            "X-Plex-Token": token}, headers=headers)
                if sr.is_success:
                    items = sr.json().get("MediaContainer", {}).get("Metadata", [])
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
        cr = await client.post(
            f"{base}/playlists",
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
                client.put(
                    f"{base}/playlists/{pl_id}/items",
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
            except: return None
        for i in range(0,len(tracks),5):
            ids = await asyncio.gather(*[_lk(t) for t in tracks[i:i+5]])
            for eid in ids:
                if eid: emby_ids.append(eid)
                else: not_found += 1
        if not emby_ids: raise HTTPException(404,"No tracks in Emby")
        sr = await client.get(f"{base}/Users/{user_id}/Items",
            params={"SearchTerm":name,"IncludeItemTypes":"Playlist","api_key":token},headers=ah)
        if sr.is_success:
            ex = next((p for p in sr.json().get("Items",[]) if p.get("Name")==name),None)
            if ex: await client.delete(f"{base}/Items/{ex['Id']}?api_key={token}",headers=ah)
        cr = await client.post(f"{base}/Playlists",
            params={"Name":name,"Ids":",".join(emby_ids),"UserId":user_id,
                    "MediaType":"Audio","api_key":token},headers=ah)
        if not cr.is_success: raise HTTPException(502,f"Emby create failed: {cr.status_code}")
        pl = cr.json(); pl_id = pl.get("Id") or pl.get("id") or pl.get("PlaylistId")
        if not pl_id: raise HTTPException(502,"No playlist ID from Emby")
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
                            "Recursive":"true","api_key":token},headers=headers)
                items = r.json().get("Items",[]) if r.is_success else []
                a = t.get("tag_artist","")
                m = next((i for i in items
                    if i.get("Name","").lower()==t.get("title","").lower()
                    and a.lower()[:8] in
                        (i.get("AlbumArtist") or (i.get("Artists") or [""])[0]).lower()),
                    items[0] if items else None)
                return m.get("Id") if m else None
            except: return None
        for i in range(0,len(tracks),5):
            ids = await asyncio.gather(*[_lk(t) for t in tracks[i:i+5]])
            for jid in ids:
                if jid: jf_ids.append(jid)
                else: not_found += 1
        if not jf_ids: raise HTTPException(404,"No tracks in Jellyfin")
        sr = await client.get(f"{base}/Users/{user_id}/Items",
            params={"SearchTerm":name,"IncludeItemTypes":"Playlist","api_key":token},headers=headers)
        if sr.is_success:
            ex = next((p for p in sr.json().get("Items",[]) if p.get("Name")==name),None)
            if ex: await client.delete(f"{base}/Items/{ex['Id']}?api_key={token}",headers=headers)
        cr = await client.post(f"{base}/Playlists?api_key={token}",headers=headers,
            json={"Name":name,"Ids":jf_ids,"UserId":user_id,"MediaType":"Audio"})
        if not cr.is_success: raise HTTPException(502,f"JF create failed: {cr.status_code}")
        pl = cr.json(); pl_id = pl.get("Id") or pl.get("id")
        if not pl_id: raise HTTPException(502,"No playlist ID from Jellyfin")
    return {"ok":True,"server":"jellyfin","playlist":name,
            "added":len(jf_ids),"not_found":not_found,"playlist_id":pl_id}
