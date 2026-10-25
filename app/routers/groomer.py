# © 2026 Colby R. Curtis | ChartHound: The New World
# All Rights Reserved.
"""
ChartHound — The Groomer Router
Real Chart Data Playlist Builder

SCAN ARCHITECTURE (scan-everything, filter-at-playlist-time):
  Scan runs against ALL charts simultaneously — no chart filter at scan time.
  Playlist filters (genre/chart, peak, weeks, confidence) apply to stored results.
  One scan → unlimited playlists without rescanning.

Lookup waterfall per track:
  1. chart_data cache     → previously computed this scan, skip instantly
  2. chart_status cache   → confirmed miss within age gate, skip instantly
  3. Static DB UNION extras → chart_reference + billboard_pop + chart_reference_extras
                              (Billboard CSVs, utdata, chart2000, tsort, billboard yearend,
                               LBZ historical) — confidence: high/medium
  4. Comment tag read-back → re-parse previously written ChartHound tags (zero API)
  5. Last.fm listener count → genre-gated popularity estimate — confidence: low

Genre routing:
  - Retriever genre_1/2/3 pulled from DB onto track dict before waterfall
  - Used to route LBZ lookup to correct chart_name when multiple genres present
  - Used as quality gate for Last.fm estimate tier
  - CCM/gospel family: strict gate (no genre tag = reject at Last.fm tier)

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

# M9b: shared helpers → groomer_common.py · Veterinarian → vet.py · Hit Score/Last.fm → hitscore.py.
# Their routes are included below at their original positions, so paths and order are unchanged.

import asyncio
import json
import logging
import os
import random
import sqlite3
import time

import aiosqlite
import httpx

from datetime import datetime
from typing import Optional, List
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel

from app.config import get_settings
from app.deps import require_auth
from app.security import decrypt_token

from app.routers.retriever import peak_to_stars

from app.routers.groomer_common import (
    CHART_DISPLAY,
    MATCH_THRESHOLD,
    _CCM_CHARTS,
    _CHART_SOURCE_TO_GENRE,
    _DYNAMIC_DB,
    _FEAT_RE,
    _GENRE_FILTER_KEYWORDS,
    _LBZ_CHART_NAMES,
    _LOOKUP_VERSION,
    _STATIC_DB,
    _STRICT_GENRE_CHARTS,
    _artist_key,
    _artist_match,
    _detect_genre_bucket,
    _fuzzy,
    _genre_matches_chart,
    _genre_tags_to_chart_names,
    _lastfm_listeners,
    _listeners_to_est_peak,
    _listeners_to_stars,
    _lookup_lbz_extras,
    _lookup_static,
    _meets_min_threshold,
    _migrate_chart_data,
    _norm,
    _open_static_conn,
    _parse_comment_tag,
    _read_comment_from_file,
    _safe_json,
    _scan_job,
    _title_key,
    _utc_epoch,
)

from app.routers.vet import (
    _get_setting,
    _library_cache_scheduler,
    _scheduler_loop,
    _set_setting,
)

from app.routers.hitscore import (
    _LFM_INTERVAL,
    _MELLOW_STYLES,
    _big_hits_sync,
    _get_lastfm_key,
    _hit_score_startup,
    _lastfm_startup,
    _lfm_call,
    _lfm_clean_title,
    _lfm_tag_names,
    _maybe_start_lastfm,
    _peak_factor,
    _rebuild_hit_scores,
)

from app.routers import vet, hitscore

log      = logging.getLogger("charthound.groomer")
router   = APIRouter(prefix="/api/groomer", tags=["groomer"])
settings = get_settings()


# Names other routers import from app.routers.groomer (sniffer, retriever, bloodhound)
_REEXPORTED = (_norm, _fuzzy, CHART_DISPLAY, MATCH_THRESHOLD,
               _artist_key, _title_key, _FEAT_RE, _migrate_chart_data, _utc_epoch)

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

@router.get("/verify-tags")
async def verify_tags(_=Depends(require_auth)):
    """
    Samples up to 5 recently written chart_data rows and verifies the
    COMMENT tag was actually written to the physical file.
    Returns list of {file_path, expected, actual, ok} per file checked.
    """
    results = []

    # Get path translation settings
    server_prefix = ""
    docker_prefix = "/music"
    try:
        async with aiosqlite.connect(_DYNAMIC_DB) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT key, value FROM app_settings "
                "WHERE key IN ('path_server_prefix','path_docker_prefix')"
            ) as cur:
                rows = await cur.fetchall()
            for r in rows:
                if r["key"] == "path_server_prefix":
                    server_prefix = r["value"] or ""
                elif r["key"] == "path_docker_prefix":
                    docker_prefix = r["value"] or "/music"
    except Exception:
        pass

    # Sample up to 5 chart_data rows with comment_string and file_path
    try:
        async with aiosqlite.connect(_DYNAMIC_DB) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("""
                SELECT cd.comment_string, t.file_path
                FROM chart_data cd
                JOIN tracks t ON cd.track_id = t.track_id
                WHERE cd.comment_string IS NOT NULL
                  AND cd.comment_string != ''
                  AND t.file_path IS NOT NULL
                  AND t.file_path != ''
                ORDER BY cd.fetched_at DESC
                LIMIT 5
            """) as cur:
                rows = await cur.fetchall()
    except Exception as e:
        raise HTTPException(500, f"DB error: {e}")

    for row in rows:
        fp       = row["file_path"]
        expected = row["comment_string"]

        # Translate server path → docker path if needed
        docker_fp = fp
        if server_prefix and fp.startswith(server_prefix):
            docker_fp = docker_prefix + fp[len(server_prefix):]
        elif fp.startswith("/music") or fp.startswith(docker_prefix):
            docker_fp = fp  # already docker path

        if not os.path.exists(docker_fp):
            results.append({
                "file_path": fp,
                "expected":  expected,
                "actual":    None,
                "ok":        False,
                "error":     "File not found at docker path"
            })
            continue

        try:
            from mutagen import File as MutagenFile
            mf = MutagenFile(docker_fp, easy=False)
            actual = None
            if mf:
                # FLAC / Vorbis comment
                if hasattr(mf, 'tags') and mf.tags:
                    actual = (
                        mf.tags.get("COMMENT", [None])[0]
                        or mf.tags.get("comment", [None])[0]
                        # ID3 COMM frame
                        or str(next((v for k,v in mf.tags.items()
                                     if k.startswith("COMM")), None) or "")
                        or None
                    )
                    # Handle ID3 COMM object
                    if actual and hasattr(actual, 'text'):
                        actual = actual.text[0] if actual.text else str(actual)
            results.append({
                "file_path": fp,
                "expected":  expected,
                "actual":    actual,
                "ok":        actual is not None and expected in str(actual),
            })
        except Exception as e:
            results.append({
                "file_path": fp,
                "expected":  expected,
                "actual":    None,
                "ok":        False,
                "error":     str(e)
            })

    return {"verified": results, "count": len(results)}


@router.get("/libraries")
async def get_libraries(server: str, _=Depends(require_auth)):
    """
    Returns music libraries from the selected media server.
    Used to populate the library selector dropdown in the UI.
    """
    try:
        async with aiosqlite.connect(_DYNAMIC_DB) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT base_url, token_enc, extra_json FROM connections WHERE service=?",
                (server,)
            ) as cur:
                conn = await cur.fetchone()
    except Exception:
        conn = None

    if not conn:
        raise HTTPException(400, f"No {server} connection configured")

    base_url = conn["base_url"]
    token    = decrypt_token(conn["token_enc"]) if conn["token_enc"] else ""
    extra    = json.loads(conn["extra_json"] or "{}") if conn["extra_json"] else {}
    libraries = []

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            if server == "plex":
                r = await client.get(f"{base_url}/library/sections?X-Plex-Token={token}",
                    headers={"Accept":"application/json"})
                if r.is_success:
                    sections = r.json().get("MediaContainer",{}).get("Directory",[])
                    libraries = [{"id": s["key"], "name": s.get("title","Unknown"),
                                  "count": s.get("count", 0)}
                                 for s in sections if s.get("type") == "artist"]
            elif server in ("emby", "jellyfin"):
                user_id = extra.get("user_id","")
                hdrs = {"Accept":"application/json"}
                if server == "emby":
                    hdrs["X-Emby-Token"] = token
                r = await client.get(f"{base_url}/Users/{user_id}/Views",
                    params={"api_key": token}, headers=hdrs)
                if r.is_success:
                    items = r.json().get("Items", [])
                    libraries = [{"id": i["Id"], "name": i.get("Name","Unknown")}
                                 for i in items
                                 if i.get("CollectionType") in ("music", "Music") or
                                    "music" in i.get("Name","").lower()]
    except Exception as e:
        log.warning(f"Library fetch error for {server}: {e}")

    return {"libraries": libraries, "server": server}


@router.get("/tagged_count")
async def tagged_count(_=Depends(require_auth)):
    """Returns count of tracks with genre_1 populated (Retriever-tagged)."""
    try:
        async with aiosqlite.connect(_DYNAMIC_DB) as db:
            async with db.execute(
                "SELECT COUNT(*) FROM tracks WHERE genre_1 IS NOT NULL AND genre_1 != ''"
            ) as cur:
                row = await cur.fetchone()
                return {"tagged": row[0] if row else 0}
    except Exception:
        return {"tagged": 0}


@router.get("/db_stats")
async def db_stats(_=Depends(require_auth)):
    static_total = 0
    extras_total = 0
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
            # User-imported chart entries (chart_reference_extras may not exist yet)
            try:
                async with db.execute("SELECT COUNT(*) FROM chart_reference_extras") as cur:
                    row = await cur.fetchone()
                    extras_total = row[0] if row else 0
            except Exception:
                extras_total = 0
    except Exception:
        pass

    return {
        "static_entries":  static_total,
        "extras_entries":  extras_total,
        "total_entries":   static_total + extras_total,
        "cached_results":  dynamic_chart_data,
        "static_db_ready": os.path.exists(_STATIC_DB),
    }


# ══════════════════════════════════════════════════════════════════════════════
#  SKIP CACHE (chart_status on tracks table)
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/skip_cache/stats")
async def skip_cache_stats(_=Depends(require_auth)):
    """Returns counts of chart_status values: hit, miss, null (unchecked)."""
    try:
        async with aiosqlite.connect(_DYNAMIC_DB) as db:
            rows = {}
            async with db.execute(
                "SELECT chart_status, COUNT(*) FROM tracks GROUP BY chart_status"
            ) as cur:
                async for row in cur:
                    key = row[0] or "unchecked"
                    rows[key] = row[1]
        return {
            "hit":       rows.get("hit", 0),
            "miss":      rows.get("miss", 0),
            "unchecked": rows.get("unchecked", 0),
            "total":     sum(rows.values()),
        }
    except Exception as e:
        raise HTTPException(500, f"DB error: {e}")


@router.post("/skip_cache/reset")
async def skip_cache_reset(_=Depends(require_auth)):
    """Clears all chart_status values — forces full re-scan on next run."""
    try:
        async with aiosqlite.connect(_DYNAMIC_DB) as db:
            result = await db.execute(
                "UPDATE tracks SET chart_status=NULL, chart_last_checked=NULL"
            )
            count = result.rowcount
            await db.commit()
        log.info(f"Skip cache reset: {count} tracks cleared")
        return {"ok": True, "cleared": count}
    except Exception as e:
        raise HTTPException(500, f"DB error: {e}")


# ══════════════════════════════════════════════════════════════════════════════
#  VETERINARIAN — moved to vet.py (M9b)
# ══════════════════════════════════════════════════════════════════════════════
router.include_router(vet.router)


async def groomer_startup():
    """Called from main.py lifespan to start the weekly refresh scheduler."""
    asyncio.create_task(_scheduler_loop())
    asyncio.create_task(_library_cache_scheduler())
    asyncio.create_task(_hit_score_startup())
    asyncio.create_task(_lastfm_startup())
    asyncio.create_task(_b200_startup())
    log.info("Groomer scheduler started (auto-refresh every 7 days for current-week sources).")
    log.info("Library cache scheduler started (auto-sync every 24h from physical files).")


# ══════════════════════════════════════════════════════════════════════════════
#  HIT SCORE + LAST.FM PASS — moved to hitscore.py (M9b)
# ══════════════════════════════════════════════════════════════════════════════
router.include_router(hitscore.router)


# ══════════════════════════════════════════════════════════════════════════════
#  END STATIC DB SOURCES
# ══════════════════════════════════════════════════════════════════════════════


class ScanRequest(BaseModel):
    source:        str            # 'plex' | 'emby' | 'jellyfin' | 'local'
    # charts removed — scan now runs against ALL sources simultaneously.
    # Genre/chart filtering happens at playlist-build time via get_results() filters.
    data_source:   str = "auto"   # kept for backward compat
    use_estimates: bool = False   # if True, use Last.fm for tracks not in any DB source
    write_tags:    bool = False
    limit:         Optional[int] = None
    folder_path:   Optional[str] = None
    library_id:    Optional[str] = None   # media server library ID filter


class StopRequest(BaseModel):
    job_id: Optional[int] = None


# ══════════════════════════════════════════════════════════════════════════════
#  SCAN — START
# ══════════════════════════════════════════════════════════════════════════════

@router.post("/scan/start")
async def scan_start(req: ScanRequest, _=Depends(require_auth)):
    if _scan_job["status"] == "running":
        raise HTTPException(409, "A scan is already running. Stop it first.")

    # Migrate schema if needed (safe no-op if column already exists)
    await _migrate_chart_data()

    # M1: results are NO LONGER wiped here. Each track's rows are replaced
    # individually when it is re-checked, so a failed fetch loses nothing.

    _scan_job.update({
        "status": "starting", "message": "Initialising scan...",
        "total": 0, "processed": 0, "matched": 0, "failed": 0, "cached": 0,
        "started_at": time.time(), "stop_requested": False, "job_id": int(time.time()),
    })

    # CRITICAL: asyncio.create_task instead of bg.add_task.
    # bg.add_task (FastAPI BackgroundTasks) does not reliably yield control
    # back to the event loop during heavy I/O on this NAS — it freezes
    # uvicorn and blocks all poll requests. create_task runs as a proper
    # concurrent coroutine on the event loop.
    asyncio.create_task(_run_scan(req))
    return {"ok": True, "job_id": _scan_job["job_id"]}


@router.get("/scan/status/{job_id}")
async def scan_status(job_id: int, _=Depends(require_auth)):
    """
    Return current scan state. If the polled job_id doesn't match the in-memory
    job_id, the client is polling a dead scan (e.g. after container restart or
    a new scan started in another tab) — return status='stale' so the frontend
    can reset its UI instead of polling forever.
    """
    current_id = _scan_job.get("job_id")
    if current_id is None or current_id != job_id:
        return {
            "status": "stale",
            "message": "This scan is no longer active (server restart or new scan started).",
            "job_id": job_id,
            "total": 0, "processed": 0, "matched": 0, "failed": 0, "cached": 0,
        }
    return dict(_scan_job)


@router.post("/scan/stop")
async def scan_stop(_=Depends(require_auth)):
    _scan_job["stop_requested"] = True
    _scan_job["message"] = "Stop requested — finishing current track..."
    return {"ok": True}


# ══════════════════════════════════════════════════════════════════════════════
#  SCAN — BACKGROUND WORKER
# ══════════════════════════════════════════════════════════════════════════════

async def _get_recheck_cutoffs(db) -> tuple:
    """
    Returns (version_cutoff, data_cutoff) as UTC epochs.
      version_cutoff — when matching logic last changed (_LOOKUP_VERSION bump).
                       Hits checked before this are re-checked.
      data_cutoff    — max(version_cutoff, newest chart import). Misses checked
                       before this are re-checked (new data may now match them).
    """
    vals = {}
    async with db.execute(
        "SELECT key, value FROM app_settings "
        "WHERE key IN ('groomer_lookup_version','groomer_recheck_after')"
    ) as cur:
        for k, v in await cur.fetchall():
            vals[k] = v
    if vals.get("groomer_lookup_version") != _LOOKUP_VERSION:
        now_s = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
        await db.executemany(
            "INSERT INTO app_settings (key, value, updated_at) VALUES (?, ?, datetime('now')) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            [("groomer_lookup_version", _LOOKUP_VERSION), ("groomer_recheck_after", now_s)],
        )
        await db.commit()
        vals["groomer_recheck_after"] = now_s
        log.info(f"Lookup version → {_LOOKUP_VERSION}: all tracks will be re-checked once")
    latest_import = None
    try:
        async with db.execute("SELECT MAX(added_at) FROM chart_reference_extras") as cur:
            row = await cur.fetchone()
            latest_import = row[0] if row else None
    except Exception:
        pass
    version_cutoff = _utc_epoch(vals.get("groomer_recheck_after"))
    return version_cutoff, max(version_cutoff, _utc_epoch(latest_import))


async def _run_scan(req: ScanRequest):
    # Early log — ALWAYS hits before any heavy I/O so we have proof the
    # background task started, even if something catastrophic happens next.
    log.info(
        f"Scan task entered — source={req.source} "
        f"folder={req.folder_path or '(n/a)'} library={req.library_id or '(all)'} "
        f"job_id={_scan_job.get('job_id')}"
    )
    _scan_job["status"] = "running"
    req.use_estimates = False   # M5: retired — replaced by the Online Data (Last.fm) pass
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
        try:
            tracks = await _fetch_tracks(req)
        except Exception as fe:
            log.error(f"Groomer library fetch failed: {fe}")
            _scan_job.update({"status": "failed",
                              "message": f"{fe} Previous results kept."})
            return
        if _scan_job["stop_requested"]:
            _scan_job.update({"status": "stopped", "message": "Stopped before chart lookups began."})
            return
        if not tracks:
            where = (req.folder_path or "/music") if req.source == "local" else f"{req.source} library"
            _scan_job.update({"status": "failed",
                              "message": f"No tracks returned from {where}. Previous results kept."})
            return

        # Dedupe — 'All Libraries' can return the same file from several libraries
        _seen, _uniq = set(), []
        for t in tracks:
            fp = (t.get("file_path") or "").strip()
            key = fp or (_norm(t.get("artist", "")), _norm(t.get("title", "")), _norm(t.get("album", "")))
            if key in _seen:
                continue
            _seen.add(key)
            _uniq.append(t)
        dupes = len(tracks) - len(_uniq)
        tracks = _uniq
        if dupes:
            log.info(f"Dedupe: {dupes:,} duplicate tracks removed ({len(tracks):,} unique)")

        limit = req.limit or len(tracks)
        tracks = tracks[:limit]
        _scan_job["total"] = len(tracks)
        _scan_job["message"] = f"Scanning {len(tracks):,} tracks against all chart sources..."

        # Scan-everything: no chart filter at scan time.
        # _lookup_static queries chart_reference + extras with no chart restriction.
        # LBZ lookup is routed by Retriever genre tags on the track.
        # Last.fm fires for any unmatched track when use_estimates=True.
        log.info("Scan-everything mode: all chart sources active, genre routing via DB tags")

        loop = asyncio.get_event_loop()

        async with aiosqlite.connect(_DYNAMIC_DB) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("PRAGMA journal_mode=WAL")

            # ── SKIP CACHE AGE GATE ──────────────────────────────────────
            _MISS_AGE_SECONDS = 6 * 30 * 24 * 3600   # ~6 months
            skipped_miss = 0
            comment_readback_hits = 0

            # ── PRE-FETCH PATH PREFIXES (once, not per-track) ────────────
            _scan_server_prefix = ""
            _scan_docker_prefix = "/music"
            try:
                async with db.execute(
                    "SELECT key, value FROM app_settings "
                    "WHERE key IN ('path_server_prefix','path_docker_prefix')"
                ) as cur:
                    prows = await cur.fetchall()
                for r in prows:
                    if r[0] == "path_server_prefix":   _scan_server_prefix = r[1] or ""
                    elif r[0] == "path_docker_prefix":  _scan_docker_prefix = r[1] or "/music"
            except Exception:
                pass

            def _to_docker_path(fp: str) -> str:
                if not fp:
                    return ""
                if _scan_server_prefix and fp.startswith(_scan_server_prefix):
                    return _scan_docker_prefix + fp[len(_scan_server_prefix):]
                if fp.startswith(_scan_docker_prefix) or fp.startswith("/music"):
                    return fp
                return ""

            version_cutoff, data_cutoff = await _get_recheck_cutoffs(db)
            static_shared = await loop.run_in_executor(None, _open_static_conn)
            if static_shared[0] is None:
                static_shared = None

            try:
              for idx, track in enumerate(tracks):
                if _scan_job["stop_requested"]:
                    await db.commit()
                    _scan_job["message"] = "Stopped — calculating Hit Scores…"
                    await _rebuild_hit_scores()
                    _scan_job["status"] = "stopped"
                    _scan_job["message"] = f"Stopped at track {idx + 1} of {len(tracks)}"
                    return
                if idx and idx % 200 == 0:
                    await db.commit()   # persist miss/hit status in batches

                artist = track.get("artist", "").strip()
                title  = track.get("title",  "").strip()
                if not artist or not title:
                    _scan_job["processed"] += 1
                    continue

                _scan_job["message"] = f"[{idx+1}/{len(tracks)}] {artist} — {title}"

                # Resolve track_id
                track_id = await _resolve_track_id(db, track)
                if track_id:
                    track["track_id"] = track_id
                    # Keep artist (compilation fix) + duration current, even for skipped tracks
                    try:
                        await db.execute(
                            "UPDATE tracks SET tag_artist=COALESCE(NULLIF(?, ''), tag_artist), "
                            "duration_sec=COALESCE(?, duration_sec), "
                            "plex_rating_key=COALESCE(NULLIF(?, ''), plex_rating_key), "
                            "emby_id=COALESCE(NULLIF(?, ''), emby_id), "
                            "jf_id=COALESCE(NULLIF(?, ''), jf_id), "
                            "year=COALESCE(year, ?), "
                            "album_title=COALESCE(NULLIF(?, ''), album_title), missing=0 WHERE track_id=?",
                            (track.get("tag_artist") or "", track.get("duration_sec"),
                             track.get("plex_rating_key") or "", track.get("emby_id") or "",
                             track.get("jf_id") or "", track.get("year"),
                             track.get("album") or "", track_id))
                    except Exception:
                        pass

                # ── SKIP CACHE (incremental) ──────────────────────────────────
                # hit  → skip if checked after last lookup-logic change and rows exist
                # miss → skip if checked after newest chart import and < 6 months old
                if track_id:
                    cs_row = None
                    try:
                        async with db.execute(
                            "SELECT chart_status, chart_last_checked FROM tracks WHERE track_id=?",
                            (track_id,)
                        ) as cur:
                            cs_row = await cur.fetchone()
                    except Exception:
                        pass
                    if cs_row and cs_row["chart_status"] in ("hit", "miss"):
                        chk  = _utc_epoch(cs_row["chart_last_checked"])
                        skip = False
                        if cs_row["chart_status"] == "hit" and chk >= version_cutoff:
                            async with db.execute(
                                "SELECT 1 FROM chart_data WHERE track_id=? LIMIT 1", (track_id,)
                            ) as cur:
                                skip = await cur.fetchone() is not None
                        elif (cs_row["chart_status"] == "miss" and chk >= data_cutoff
                              and (time.time() - chk) < _MISS_AGE_SECONDS):
                            skip = True
                            skipped_miss += 1
                        if skip:
                            _scan_job["cached"]    += 1
                            _scan_job["processed"] += 1
                            continue
                    # Re-checking → replace this track's old rows (never accumulate)
                    await db.execute("DELETE FROM chart_data WHERE track_id=?", (track_id,))

                # ── PULL RETRIEVER GENRE TAGS FROM DB ────────────────────────
                if track_id and not track.get("genre_1"):
                    try:
                        async with db.execute(
                            "SELECT genre_1, genre_2, genre_3 FROM tracks WHERE track_id=?",
                            (track_id,)
                        ) as cur:
                            gr = await cur.fetchone()
                        if gr:
                            track["genre_1"] = gr[0] or ""
                            track["genre_2"] = gr[1] or ""
                            track["genre_3"] = gr[2] or ""
                    except Exception:
                        pass

                # ── WATERFALL ────────────────────────────────────────────────
                # Step 1: Static DB + extras — ALL charts, no restriction
                static_result = await loop.run_in_executor(
                    None, _lookup_static, artist, title, [], static_shared
                )

                if static_result:
                    await _store_result(db, track, static_result, req)
                    await _enrich_genre_from_file(db, track, loop)
                    _scan_job["matched"] += 1
                    if track_id:
                        try:
                            await db.execute(
                                "UPDATE tracks SET chart_status='hit', "
                                "chart_last_checked=datetime('now') WHERE track_id=?",
                                (track_id,)
                            )
                        except Exception:
                            pass

                else:
                    # Step 1b: LBZ extras — routed by Retriever genre tags
                    # Use genre_1/2/3 from DB to pick which chart_names to query.
                    # Falls back to all LBZ chart names if no genre tag available.
                    genre_tags_db = [
                        track.get("genre_1", ""),
                        track.get("genre_2", ""),
                        track.get("genre_3", ""),
                    ]
                    routed_charts = _genre_tags_to_chart_names(genre_tags_db)
                    # If no genre tags, try all LBZ charts (catch untagged library)
                    lbz_query_charts = routed_charts if routed_charts else list(_LBZ_CHART_NAMES)
                    lbz_result = await _lookup_lbz_extras(artist, title, lbz_query_charts)

                    if lbz_result:
                        await _store_result(db, track, lbz_result, req)
                        await _enrich_genre_from_file(db, track, loop)
                        _scan_job["matched"] += 1
                        if track_id:
                            try:
                                await db.execute(
                                    "UPDATE tracks SET chart_status='hit', "
                                    "chart_last_checked=datetime('now') WHERE track_id=?",
                                    (track_id,)
                                )
                            except Exception:
                                pass
                    else:
                        # Step 2: Comment tag read-back
                        comment_result = None
                        file_path = track.get("file_path", "")
                        docker_fp = _to_docker_path(file_path)
                        if docker_fp:
                            raw_comment = await loop.run_in_executor(
                                None, _read_comment_from_file, docker_fp
                            )
                            if raw_comment:
                                parsed_entries = _parse_comment_tag(raw_comment)
                                if parsed_entries:
                                    best = min(parsed_entries, key=lambda e: e["peak_position"])
                                    best["all_charts"] = parsed_entries
                                    best["star_rating"] = peak_to_stars(best["peak_position"])
                                    comment_result = best
                                    comment_readback_hits += 1
                                    log.debug(f"Comment read-back: {artist} — {title}")

                        if comment_result:
                            await _store_result(db, track, comment_result, req)
                            await _enrich_genre_from_file(db, track, loop)
                            _scan_job["matched"] += 1
                            if track_id:
                                try:
                                    await db.execute(
                                        "UPDATE tracks SET chart_status='hit', "
                                        "chart_last_checked=datetime('now') WHERE track_id=?",
                                        (track_id,)
                                    )
                                except Exception:
                                    pass
                        else:
                            # Step 3: Last.fm popularity estimate (opt-in)
                            # TARGETED: only fires when ALL of these are true:
                            #   1. use_estimates=True (user opted in)
                            #   2. Track has a genre tag (DB or file) — no genre = skip
                            #   3. Genre routes to a LBZ gap chart (hiphop/metal/alt/indie/
                            #      folk/jazz/blues/electronic) — broad charts have full
                            #      static DB coverage so Last.fm adds nothing there
                            matched_via_lfm = False
                            if req.use_estimates and lfm_key:

                                # Get genre tags — prefer DB (Retriever), fall back to file
                                genre_tags = [
                                    track.get("genre_1", ""),
                                    track.get("genre_2", ""),
                                    track.get("genre_3", ""),
                                ]
                                if not any(genre_tags) and docker_fp:
                                    def _quick_genre(fp):
                                        try:
                                            from mutagen import File as MutagenFile
                                            mf = MutagenFile(fp, easy=True)
                                            if mf:
                                                raw = mf.get("genre", [])
                                                return [str(g) for g in raw[:3]]
                                        except Exception:
                                            pass
                                        return []
                                    genre_tags = await loop.run_in_executor(
                                        None, _quick_genre, docker_fp
                                    )

                                # Route genre → chart_name
                                routed = _genre_tags_to_chart_names(genre_tags)
                                chart_for_estimate = routed[0] if routed else None

                                # GATE 1: must have a genre tag — no genre = no estimate
                                # GATE 2: routed chart must be a LBZ gap genre
                                #         (hot100/country/rock/rnb/dance/adultpop/ac/uk
                                #          all have full static DB — Last.fm adds nothing)
                                _LFM_ELIGIBLE_CHARTS = _LBZ_CHART_NAMES | _CCM_CHARTS
                                lfm_eligible = (
                                    any(genre_tags) and
                                    chart_for_estimate is not None and
                                    chart_for_estimate in _LFM_ELIGIBLE_CHARTS
                                )

                                if lfm_eligible:
                                    await asyncio.sleep(0.2)  # max 5 req/sec
                                    listeners = await _lastfm_listeners(artist, title, lfm_key)
                                    bucket = _detect_genre_bucket(genre_tags, [chart_for_estimate])

                                    # Strict gate for CCM/gospel
                                    if chart_for_estimate in _STRICT_GENRE_CHARTS:
                                        genre_ok = _genre_matches_chart(genre_tags, chart_for_estimate)
                                    else:
                                        genre_ok = True

                                    if genre_ok and _meets_min_threshold(listeners, bucket):
                                        est_peak = _listeners_to_est_peak(listeners, bucket)
                                        stars    = _listeners_to_stars(listeners, bucket)
                                        result = {
                                            "peak_position":  est_peak,
                                            "weeks_on_chart": 1,
                                            "chart_name":     chart_for_estimate,
                                            "chart_year":     None,
                                            "confidence":     "low",
                                            "data_source":    "lastfm_estimate",
                                            "all_charts":     [],
                                            "star_rating":    stars,
                                            "listener_count": listeners,
                                        }
                                        await _store_result(db, track, result, req)
                                        await _enrich_genre_from_file(db, track, loop)
                                        _scan_job["matched"] += 1
                                        matched_via_lfm = True
                                    else:
                                        _scan_job["failed"] += 1
                                else:
                                    # Not eligible for Last.fm — skip silently
                                    _scan_job["failed"] += 1
                            else:
                                _scan_job["failed"] += 1

                            if track_id:
                                try:
                                    status = "hit" if matched_via_lfm else "miss"
                                    await db.execute(
                                        "UPDATE tracks SET chart_status=?, "
                                        "chart_last_checked=datetime('now') WHERE track_id=?",
                                        (status, track_id)
                                    )
                                except Exception:
                                    pass

                _scan_job["processed"] += 1
                await asyncio.sleep(0)
            finally:
                await db.commit()
                if static_shared:
                    try:
                        static_shared[0].close()
                    except Exception:
                        pass

        _scan_job["message"] = "Calculating Hit Scores…"
        await _rebuild_hit_scores()
        _maybe_start_lastfm()     # look up any new songs in the background
        elapsed = time.time() - (_scan_job["started_at"] or time.time())
        _scan_job.update({
            "status":  "done",
            "message": (f"Scan complete — {_scan_job['matched']:,} matched, "
                        f"{_scan_job['cached']:,} cached "
                        f"({skipped_miss:,} skip-cached misses, "
                        f"{comment_readback_hits:,} comment read-backs), "
                        f"{_scan_job['failed']:,} unmatched"
                        f"{f', {dupes:,} duplicates skipped' if dupes else ''} "
                        f"in {elapsed:.0f}s"),
        })
        log.info(f"Skip cache stats: {skipped_miss:,} misses skipped, "
                 f"{comment_readback_hits:,} comment read-backs")

    except Exception as e:
        log.exception("Groomer scan error")
        _scan_job.update({"status": "failed", "message": f"Scan error: {e}"})


async def _enrich_genre_from_file(db: aiosqlite.Connection, track: dict, loop) -> None:
    """
    For a matched track, read genre tags from the physical file via Mutagen
    and update the tracks table. Only fires on matched tracks (~800 files),
    not on all 33k scanned tracks.
    Handles server path → docker path translation using app_settings.
    """
    track_id = track.get("track_id")
    if not track_id:
        return

    file_path = track.get("file_path", "")
    if not file_path:
        return

    # Path translation: server path → docker path
    try:
        async with db.execute(
            "SELECT key, value FROM app_settings "
            "WHERE key IN ('path_server_prefix','path_docker_prefix')"
        ) as cur:
            rows = await cur.fetchall()
        server_prefix = ""
        docker_prefix = "/music"
        for r in rows:
            if r[0] == "path_server_prefix":   server_prefix = r[1] or ""
            elif r[0] == "path_docker_prefix":  docker_prefix = r[1] or "/music"
    except Exception:
        server_prefix = ""
        docker_prefix = "/music"

    docker_path = file_path
    if server_prefix and file_path.startswith(server_prefix):
        docker_path = docker_prefix + file_path[len(server_prefix):]
    elif not file_path.startswith(docker_prefix) and not file_path.startswith("/music"):
        return  # can't translate — skip

    if not os.path.exists(docker_path):
        return

    def _read_genre(path: str):
        try:
            from mutagen import File as MutagenFile
            mf = MutagenFile(path, easy=True)
            if not mf:
                return []
            raw = mf.get("genre", [])
            if not raw:
                return []
            # Split on semicolons, slashes, or newlines — common separators
            import re as _re
            genres = []
            for g in raw:
                parts = _re.split('[;/\n]', str(g))
                genres.extend(p.strip() for p in parts if p.strip())
            return genres[:3]  # max 3
        except Exception:
            return []

    genres = await loop.run_in_executor(None, _read_genre, docker_path)
    if not genres:
        return

    try:
        g1 = genres[0] if len(genres) > 0 else None
        g2 = genres[1] if len(genres) > 1 else None
        g3 = genres[2] if len(genres) > 2 else None
        await db.execute(
            "UPDATE tracks SET genre_1=?, genre_2=?, genre_3=? WHERE track_id=?",
            (g1, g2, g3, track_id)
        )
        await db.commit()
    except Exception as e:
        log.debug(f"_enrich_genre_from_file update failed: {e}")


async def _resolve_track_id(db: aiosqlite.Connection, track: dict) -> Optional[int]:
    """
    Returns a valid track_id for chart_data writes.
    Priority:
      1. track dict already has track_id (Retriever-indexed tracks)
      2. Lookup by file_path in tracks table (unique index — fast)
      3. Lookup by tag_artist + title (fuzzy fallback)
      4. Insert minimal tracks row and return new ID
    """
    # Already resolved
    if track.get("track_id"):
        return track["track_id"]

    file_path  = track.get("file_path", "").strip()
    artist     = (track.get("tag_artist") or track.get("artist", "")).strip()
    title      = track.get("title", "").strip()

    # ── 1. Lookup by file_path ───────────────────────────────────────────────
    if file_path:
        async with db.execute(
            "SELECT track_id FROM tracks WHERE file_path = ?", (file_path,)
        ) as cur:
            row = await cur.fetchone()
            if row:
                return row[0]

    # ── 2. Lookup by artist + title ──────────────────────────────────────────
    if artist and title:
        async with db.execute("""
            SELECT t.track_id FROM tracks t
            LEFT JOIN artists a ON t.artist_id = a.artist_id
            WHERE LOWER(t.title) = LOWER(?)
              AND LOWER(COALESCE(t.tag_artist, a.name, '')) = LOWER(?)
            LIMIT 1
        """, (title, artist)) as cur:
            row = await cur.fetchone()
            if row:
                return row[0]

    # ── 3. Insert minimal row so chart_data can reference it ─────────────────
    # Upsert by file_path to avoid duplicates if path already exists
    if not file_path and not title:
        return None  # nothing to anchor on — skip

    # Always use the real file path as anchor — fake paths break M3U generation
    if not file_path:
        log.debug(f"_resolve_track_id: no file_path for '{title}' — skipping insert")
        return None
    anchor_path = file_path

    try:
        # Resolve or create artist row
        artist_id = None
        if artist:
            async with db.execute(
                "SELECT artist_id FROM artists WHERE name = ?", (artist,)
            ) as cur:
                arow = await cur.fetchone()
            if arow:
                artist_id = arow[0]
            else:
                cur2 = await db.execute(
                    "INSERT OR IGNORE INTO artists (name) VALUES (?)", (artist,)
                )
                artist_id = cur2.lastrowid or None

        await db.execute("""
            INSERT INTO tracks (file_path, title, tag_artist, artist_id,
                                plex_rating_key, emby_id, jf_id,
                                last_scanned)
            VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now'))
            ON CONFLICT(file_path) DO UPDATE SET
                tag_artist      = excluded.tag_artist,
                plex_rating_key = COALESCE(excluded.plex_rating_key, tracks.plex_rating_key),
                emby_id         = COALESCE(excluded.emby_id,         tracks.emby_id),
                jf_id           = COALESCE(excluded.jf_id,           tracks.jf_id)
        """, (
            anchor_path,
            title,
            artist,
            artist_id,
            track.get("plex_rating_key") or None,
            track.get("emby_id")         or None,
            track.get("jf_id")           or None,
        ))
        await db.commit()

        # Fetch the ID (lastrowid is 0 on ON CONFLICT UPDATE — re-query)
        async with db.execute(
            "SELECT track_id FROM tracks WHERE file_path = ?", (anchor_path,)
        ) as cur:
            row = await cur.fetchone()
            return row[0] if row else None

    except Exception as e:
        log.warning(f"_resolve_track_id insert failed for '{title}': {e}")
        return None


def _get_charts_with_static_data(charts: list) -> set:
    """
    Returns the subset of requested charts that have entries in charthound_static.db.
    Called once at scan start — not per track.
    Result used to decide whether Last.fm fallback is appropriate.
    """
    if not charts or not os.path.exists(_STATIC_DB):
        return set()
    try:
        conn = sqlite3.connect(_STATIC_DB, check_same_thread=False)
        placeholders = ",".join("?" * len(charts))
        rows = conn.execute(
            f"SELECT DISTINCT chart_name FROM chart_reference WHERE chart_name IN ({placeholders})",
            charts
        ).fetchall()
        conn.close()
        return {r[0] for r in rows}
    except Exception as e:
        log.warning(f"_get_charts_with_static_data error: {e}")
        return set()


async def _check_cache(db: aiosqlite.Connection, track: dict, charts: list) -> bool:
    """Returns True if this track already has any chart_data rows (scan-everything mode)."""
    track_id = track.get("track_id")
    if not track_id:
        return False
    if charts:
        placeholders = ",".join("?" * len(charts))
        params = [track_id] + charts
        async with db.execute(
            f"SELECT COUNT(*) FROM chart_data WHERE track_id=? AND chart_name IN ({placeholders})",
            params
        ) as cur:
            row = await cur.fetchone()
    else:
        # Scan-everything: any existing chart_data row = cached
        async with db.execute(
            "SELECT COUNT(*) FROM chart_data WHERE track_id=?", (track_id,)
        ) as cur:
            row = await cur.fetchone()
    return bool(row and row[0] > 0)


def write_chart_tags(file_path: str, comment_string: str, star_rating: int) -> None:
    """
    Groomer-only tag writer. Touches ONLY two fields — leaves all other tags untouched.
      - COMMENT / COMM  — formatted chart string e.g. "Hot 100: #4 (22 wks) | Adult Pop: #1"
      - RATING / POPM   — star rating 1–5 (identifier: 'ChartHound')

    MP3  → ID3 COMM frame + POPM frame (0–255 scaled: 1★=51, 2★=102, 3★=153, 4★=204, 5★=255)
    FLAC → Vorbis COMMENT tag + RATING tag (stored as plain integer string '1'–'5')
    M4A  → ©cmt atom + rated via freeform ----:com.ChartHound:RATING atom
    """
    import mutagen
    from mutagen import File as MutagenFile
    from mutagen.flac import FLAC
    from mutagen.id3 import ID3, COMM, POPM
    from mutagen.mp4 import MP4

    if not file_path or not os.path.exists(file_path):
        raise FileNotFoundError(f"File not found: {file_path}")

    ext = os.path.splitext(file_path)[1].lower()

    # Star → POPM byte scale (ID3 spec: 0–255)
    popm_scale = {1: 51, 2: 102, 3: 153, 4: 204, 5: 255}
    popm_val   = popm_scale.get(max(1, min(5, star_rating)), 153)

    if ext == ".mp3":
        try:
            tags = ID3(file_path)
        except mutagen.id3.ID3NoHeaderError:
            tags = ID3()

        # COMMENT frame — lang='eng', desc='' keeps it compatible with most players
        tags.delall("COMM:ChartHound:eng")
        tags.add(COMM(encoding=3, lang="eng", desc="ChartHound", text=comment_string))

        # POPM frame — identifier 'ChartHound'
        tags.delall("POPM:ChartHound")
        tags.add(POPM(email="ChartHound", rating=popm_val, count=0))

        tags.save(file_path)

    elif ext == ".flac":
        audio = FLAC(file_path)
        audio["COMMENT"] = comment_string
        audio["RATING"]  = str(star_rating)
        audio.save()

    elif ext in (".m4a", ".mp4", ".aac"):
        audio = MP4(file_path)
        audio.tags["©cmt"] = [comment_string]
        # Store rating as freeform atom
        from mutagen.mp4 import MP4FreeForm
        audio.tags["----:com.ChartHound:RATING"] = [
            MP4FreeForm(str(star_rating).encode("utf-8"))
        ]
        audio.save()

    else:
        # Generic fallback via easy=False Mutagen
        mf = MutagenFile(file_path, easy=False)
        if mf is not None and mf.tags is not None:
            mf.tags["COMMENT"] = comment_string
            mf.save()


async def _store_result(db: aiosqlite.Connection, track: dict, result: dict, req: ScanRequest):
    """Upserts chart_data row and optionally writes COMMENT + RATING tags to file."""
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

    chart_year = result.get("chart_year")

    await db.execute("""
        INSERT INTO chart_data
            (track_id, chart_name, peak_position, weeks_on_chart,
             star_rating, confidence, listener_count, comment_string,
             chart_year, fetched_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
        ON CONFLICT(track_id, chart_name) DO UPDATE SET
            peak_position  = excluded.peak_position,
            weeks_on_chart = excluded.weeks_on_chart,
            star_rating    = excluded.star_rating,
            confidence     = excluded.confidence,
            listener_count = excluded.listener_count,
            comment_string = excluded.comment_string,
            chart_year     = excluded.chart_year,
            fetched_at     = excluded.fetched_at
    """, (track_id, chart, peak, weeks, stars, conf, listeners, comment_string, chart_year))
    await db.commit()

    # Write COMMENT + RATING tags to physical file
    if req.write_tags:
        file_path = track.get("file_path", "")
        if file_path and os.path.exists(file_path):
            loop = asyncio.get_event_loop()
            try:
                await loop.run_in_executor(
                    None, write_chart_tags, file_path, comment_string, stars
                )
                log.info(f"Chart tags written: {os.path.basename(file_path)} | {comment_string} | ★{stars}")
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
    """
    Walk a local folder tree and collect audio tracks with basic tags.

    Uses a DEDICATED ThreadPoolExecutor (max 2 threads) so Mutagen reads
    cannot starve uvicorn's default executor. Batches of 50 with a real
    asyncio.sleep between each to guarantee the event loop stays responsive
    for /scan/status polls even during a 33k-track NAS walk.
    """
    from concurrent.futures import ThreadPoolExecutor
    from mutagen import File as MutagenFile

    log.info(f"Local scan: enumerating files under {folder}")
    _scan_job["message"] = f"Enumerating files in {folder}..."

    if not os.path.isdir(folder):
        log.warning(f"Local scan: folder does not exist or is not a directory: {folder}")
        _scan_job["message"] = f"Folder not found: {folder}"
        return []

    EXTS = {".flac", ".mp3", ".m4a", ".ogg", ".wav", ".aiff"}
    loop = asyncio.get_event_loop()

    # ── Step 1: fast enumeration (default executor, no file reads) ───────────
    def _collect_paths() -> list:
        paths = []
        for root, _, files in os.walk(folder):
            for f in files:
                if os.path.splitext(f)[1].lower() in EXTS:
                    paths.append(os.path.join(root, f))
        return paths

    try:
        file_paths = await loop.run_in_executor(None, _collect_paths)
    except Exception as e:
        log.exception(f"Local scan: walk failed: {e}")
        _scan_job["message"] = f"Walk failed: {e}"
        return []

    log.info(f"Local scan: found {len(file_paths):,} audio files — now reading tags")
    _scan_job["message"] = f"Found {len(file_paths):,} files — reading tags..."
    _scan_job["total"] = len(file_paths)

    # ── Step 2: read tags in DEDICATED executor, small batches ───────────────
    # Dedicated pool (2 threads) so we never starve uvicorn's default executor.
    # Batch size 50 + real sleep(0.05) between batches = event loop stays alive.
    tag_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="tag_read")

    def _read_one(fp: str) -> Optional[dict]:
        try:
            mf = MutagenFile(fp, easy=True)
            if not mf:
                return None
            return {
                "file_path": fp,
                "artist":    str(mf.get("artist", [""])[0]),
                "title":     str(mf.get("title",  [""])[0]),
                "album":     str(mf.get("album",  [""])[0]),
                "track_id":  None,
                "duration_sec": int(mf.info.length) if getattr(mf, "info", None) and mf.info.length else None,
                "year": (lambda d: int(d[:4]) if d[:4].isdigit() else None)(str((mf.get("date") or [""])[0])),
            }
        except Exception:
            return None

    def _read_batch(paths: list) -> list:
        return [_read_one(fp) for fp in paths]

    tracks: list = []
    BATCH = 50
    total_files = len(file_paths)

    try:
        for i in range(0, total_files, BATCH):
            if _scan_job["stop_requested"]:
                log.info(f"Local scan: stop requested at tag-read {i}/{total_files}")
                _scan_job["message"] = f"Stopped during tag read ({i}/{total_files})"
                return tracks

            batch = file_paths[i:i+BATCH]
            results = await loop.run_in_executor(tag_executor, _read_batch, batch)
            tracks.extend([r for r in results if r is not None])

            # Update message so UI shows progress during tag-read phase
            done = i + len(batch)
            _scan_job["message"] = f"Reading tags: {done:,}/{total_files:,}"

            # Periodic log every 5,000 files so debug log shows progress
            if done % 5000 < BATCH:
                log.info(f"Local scan: tag-read progress {done:,}/{total_files:,} ({len(tracks):,} valid)")

            # Real yield — NOT sleep(0). Gives uvicorn time to serve polls.
            await asyncio.sleep(0.05)
    finally:
        tag_executor.shutdown(wait=False)

    log.info(f"Local scan: collected {len(tracks):,} taggable tracks from {total_files:,} files")
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
        raise RuntimeError(f"No {req.source} connection configured in The Kennel.")

    base_url   = conn["base_url"]
    token      = decrypt_token(conn["token_enc"]) if conn["token_enc"] else ""
    extra      = json.loads(conn["extra_json"] or "{}") if conn["extra_json"] else {}
    library_id = req.library_id or None

    if req.source == "plex":
        return await _fetch_plex_tracks(base_url, token, library_id=library_id)
    elif req.source == "emby":
        return await _fetch_emby_tracks(base_url, token, extra.get("user_id",""), library_id=library_id)
    else:
        return await _fetch_jellyfin_tracks(base_url, token, extra.get("user_id",""), library_id=library_id)


async def _fetch_plex_tracks(base: str, token: str, library_id: str = None) -> list:
    """Raises RuntimeError with a readable reason on any failure (no silent empty list)."""
    tracks = []
    prefix_server = os.environ.get("MEDIA_SERVER_MUSIC_PREFIX", "")
    prefix_docker = os.environ.get("DOCKER_MUSIC_PREFIX", "/music")
    hdr = {"Accept": "application/json"}
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            r = await client.get(f"{base}/library/sections",
                                 params={"X-Plex-Token": token}, headers=hdr)
            sections = _safe_json(r, "Plex").get("MediaContainer", {}).get("Directory", [])
            music_sections = (
                [library_id] if library_id
                else [s["key"] for s in sections if s.get("type") == "artist"]
            )
            if not music_sections:
                raise RuntimeError("Plex has no music libraries.")

            for sec in music_sections:
                offset = 0
                while True:
                    _scan_job["message"] = f"Reading Plex library {sec}: {len(tracks):,} tracks…"
                    r = await client.get(
                        f"{base}/library/sections/{sec}/all",
                        params={"type": 10, "X-Plex-Token": token,
                                "X-Plex-Container-Start": offset,
                                "X-Plex-Container-Size": 500},
                        headers=hdr)
                    items = _safe_json(r, "Plex").get("MediaContainer", {}).get("Metadata", [])
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
                        # originalTitle = track artist on compilations; grandparent = album artist
                        artist = item.get("originalTitle") or item.get("grandparentTitle", "")
                        tracks.append({
                            "track_id":        None,
                            "duration_sec":    int(item["duration"] / 1000) if item.get("duration") else None,
                            "year":            item.get("parentYear") or item.get("year"),
                            "plex_rating_key": item.get("ratingKey", ""),
                            "artist":          artist,
                            "title":           item.get("title", ""),
                            "album":           item.get("parentTitle", ""),
                            "file_path":       fp,
                            "tag_artist":      artist,
                        })
                    offset += len(items)
                    if len(items) < 500:
                        break
    except RuntimeError:
        raise
    except httpx.HTTPError as e:
        raise RuntimeError(f"Could not reach Plex at {base} ({type(e).__name__}).")
    log.info(f"Plex fetch: {len(tracks):,} tracks from {len(music_sections)} library(s)")
    return tracks


async def _fetch_emby_tracks(base: str, token: str, user_id: str, library_id: str = None) -> list:
    tracks = []
    headers = {"Accept":"application/json","X-Emby-Token":token}
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            start = 0
            while True:
                r = await client.get(f"{base}/Users/{user_id}/Items",
                    params={k:v for k,v in {
                            "IncludeItemTypes":"Audio","Recursive":"true",
                            "Fields":"Path,MediaSources","api_key":token,
                            "StartIndex":start,"Limit":500,
                            "ParentId": library_id if library_id else None,
                        }.items() if v is not None},
                    headers=headers)
                items = _safe_json(r, "Emby").get("Items", [])
                if not items:
                    break
                for item in items:
                    tracks.append({
                        "track_id":  None,
                        "emby_id":   item.get("Id",""),
                        "duration_sec": int(item["RunTimeTicks"] / 10_000_000) if item.get("RunTimeTicks") else None,
                        "year":      item.get("ProductionYear"),
                        "artist":    (item.get("Artists") or [""])[0],
                        "title":     item.get("Name",""),
                        "album":     item.get("Album",""),
                        "file_path": item.get("Path",""),
                        "tag_artist":(item.get("Artists") or [""])[0],
                    })
                start += len(items)
                if len(items) < 500:
                    break
    except RuntimeError:
        raise
    except httpx.HTTPError as e:
        raise RuntimeError(f"Could not reach Emby at {base} ({type(e).__name__}).")
    return tracks


async def _fetch_jellyfin_tracks(base: str, token: str, user_id: str, library_id: str = None) -> list:
    tracks = []
    headers = {"Accept":"application/json"}
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            start = 0
            while True:
                r = await client.get(f"{base}/Users/{user_id}/Items",
                    params={k:v for k,v in {
                            "IncludeItemTypes":"Audio","Recursive":"true",
                            "Fields":"Path,MediaSources","api_key":token,
                            "StartIndex":start,"Limit":500,
                            "ParentId": library_id if library_id else None,
                        }.items() if v is not None},
                    headers=headers)
                items = _safe_json(r, "Jellyfin").get("Items", [])
                if not items:
                    break
                for item in items:
                    tracks.append({
                        "track_id": None,
                        "jf_id":    item.get("Id",""),
                        "duration_sec": int(item["RunTimeTicks"] / 10_000_000) if item.get("RunTimeTicks") else None,
                        "year":     item.get("ProductionYear"),
                        "artist":   (item.get("Artists") or [""])[0],
                        "title":    item.get("Name",""),
                        "album":    item.get("Album",""),
                        "file_path":item.get("Path",""),
                        "tag_artist":(item.get("Artists") or [""])[0],
                    })
                start += len(items)
                if len(items) < 500:
                    break
    except RuntimeError:
        raise
    except httpx.HTTPError as e:
        raise RuntimeError(f"Could not reach Jellyfin at {base} ({type(e).__name__}).")
    return tracks


# ══════════════════════════════════════════════════════════════════════════════
#  RESULTS
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/results")
async def get_results(
    charts:      Optional[str] = None,
    min_peak:    Optional[int] = None,
    max_peak:    Optional[int] = None,
    min_weeks:   Optional[int] = None,
    min_year:    Optional[int] = None,
    max_year:    Optional[int] = None,
    confidence:  Optional[str] = None,
    genre:       Optional[str] = None,
    min_score:   Optional[int] = None,
    decades:     Optional[str] = None,   # "1980,1990"
    ohw:         bool = False,           # one-hit wonders only
    genre_mode:  str = "main",           # main = online genres (engine → Last.fm → file tags); any = file tags
    crossovers:  bool = True,            # bridge styles cross families (Pop Rock in Pop playlists)
    limit:       int = 500,
    offset:      int = 0,
    _=Depends(require_auth),
):
    """
    Query chart_data joined with tracks for Groomer results table.
    Filters: charts (comma-sep), peak range, weeks min, year range, confidence, genre.
    Paginated — default 500 rows per page.
    """
    conditions = []
    params: list = []

    # charts param removed — genre filtering now by file tags via genre param

    if min_peak is not None:
        conditions.append("(cd.peak_position >= ? OR cd.chart_name = 'classic')"); params.append(min_peak)
    if max_peak is not None:
        conditions.append("(cd.peak_position <= ? OR cd.chart_name = 'classic')"); params.append(max_peak)
    if min_weeks is not None:
        conditions.append("cd.weeks_on_chart >= ?"); params.append(min_weeks)
    if confidence:
        conditions.append("cd.confidence = ?"); params.append(confidence)
    conditions.append("COALESCE(t.missing, 0) = 0")
    _yr = "COALESCE(hs.hit_year, cd.chart_year, t.year)"
    if min_year is not None:
        conditions.append(f"{_yr} >= ?"); params.append(min_year)
    if max_year is not None:
        conditions.append(f"{_yr} <= ?"); params.append(max_year)
    if min_score:
        conditions.append("COALESCE(hs.hit_score, 0) >= ?"); params.append(min_score)
    if ohw:
        conditions.append("hs.one_hit_wonder = 1")
    if decades:
        ds = [int(d) for d in decades.split(",") if d.strip().isdigit()]
        if ds:
            conditions.append("(" + " OR ".join([f"({_yr} BETWEEN ? AND ?)"] * len(ds)) + ")")
            for d in ds:
                params += [d, d + 9]
    if genre:
        # genre param is comma-separated genre keys (e.g. "country,rock,ccm")
        # Filter purely by file genre tags (genre_1/2/3) using keyword sets.
        # File tag match OR chart_name fallback for untagged tracks.
        # No active genre filter (ALL) = no condition added = return everything.
        genre_list = [g.strip() for g in genre.split(",") if g.strip()]
        if genre_list:
            _genre_param_start = len(params)
            genre_clauses = []
            for g in genre_list:
                keywords = _GENRE_FILTER_KEYWORDS.get(g, set()) or set()
                kw_list  = list(keywords)
                # Build the file-tag match clause
                if kw_list:
                    kw_placeholders = " OR ".join(
                        ["LOWER(t.genre_1) LIKE ? OR LOWER(t.genre_2) LIKE ? OR LOWER(t.genre_3) LIKE ?"]
                        * len(kw_list)
                    )
                    tag_clause = f"({kw_placeholders})"
                    for kw in kw_list:
                        params += [f"%{kw}%", f"%{kw}%", f"%{kw}%"]
                else:
                    tag_clause = None

                # Chart_name fallback — fires when track has no genre tag
                # Only for clean reliable sources, not tsort/chart2000/LBZ
                fallback_chart = _CHART_SOURCE_TO_GENRE.get(g)
                no_tag = "(t.genre_1 IS NULL OR t.genre_1 = '')"
                if tag_clause and fallback_chart and fallback_chart == g:
                    # Match: file tag matches OR (no tag AND chart_name matches)
                    genre_clauses.append(
                        f"({tag_clause} OR ({no_tag} AND cd.chart_name = ?))"
                    )
                    params.append(g)
                elif tag_clause:
                    genre_clauses.append(tag_clause)
                elif fallback_chart == g:
                    genre_clauses.append(f"({no_tag} AND cd.chart_name = ?)")
                    params.append(g)

            # ── Online genres (TOP GENRES mode) ─────────────────────────────
            # 1. Genre Engine family/styles (tg)  2. Last.fm top-3 (hs)  3. file tags
            from app.routers import retriever as _ret
            sel = set(genre_list)
            fams = [g for g in sel if g in _ret.GE_FAMILIES]
            stys = {g for g in sel if g in _ret.GE_STYLES}
            if crossovers:   # bridge styles let neighbouring families in (Pop ← Rock songs tagged Pop Rock)
                stys |= {k for k, (_l, fs) in _ret.GE_STYLES.items()
                         if len(fs) > 1 and set(fs) & set(fams)}
            if "mellow" in sel:
                stys |= _MELLOW_STYLES
            eng_parts, eng_params = [], []
            if fams:
                eng_parts.append(f"tg.family IN ({','.join('?' * len(fams))})")
                eng_params += fams
            for st in sorted(stys):
                eng_parts.append("tg.styles LIKE ?")
                eng_params.append(f'%"{st}"%')
            engine = ("(tg.family IS NOT NULL AND (" + " OR ".join(eng_parts) + "))") if eng_parts \
                     else "(tg.family IS NOT NULL AND 0)"
            top3 = "(" + " OR ".join(["(',' || hs.genre_keys || ',') LIKE ?"] * len(genre_list)) + ")"
            top3_params = [f"%,{g},%" for g in genre_list]
            if genre_mode == "main":
                loose = ("(" + " OR ".join(genre_clauses) + ")") if genre_clauses else "0"
                conditions.append(
                    f"({engine} OR (tg.family IS NULL AND hs.genre_keys IS NOT NULL AND {top3}) "
                    f"OR (tg.family IS NULL AND hs.genre_keys IS NULL AND {loose}))")
                # order: engine params, top-3 params, then loose params (already appended)
                params[_genre_param_start:_genre_param_start] = eng_params + top3_params
            elif genre_clauses:
                conditions.append("(" + " OR ".join(genre_clauses) + ")")

    where        = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    # Keep filter_params separate — reused for COUNT query without limit/offset
    filter_params = list(params)
    page_params   = list(params) + [limit, offset]

    try:
        async with aiosqlite.connect(_DYNAMIC_DB) as db:
            db.row_factory = aiosqlite.Row

            # Total count (uses filter_params only — no limit/offset)
            async with db.execute(f"""
                SELECT COUNT(*)
                FROM chart_data cd
                JOIN tracks t ON cd.track_id = t.track_id
                LEFT JOIN artists a ON t.artist_id = a.artist_id
                LEFT JOIN hit_scores hs ON hs.track_id = cd.track_id
                LEFT JOIN track_genres tg ON tg.track_id = cd.track_id
                {where}
            """, filter_params) as cur:
                total_row   = await cur.fetchone()
                total_count = total_row[0] if total_row else 0

            # Dedup: one row per title, best non-compilation artist wins.
            #
            # Compilation fix (M4 bug): Previously partitioned by artist+title,
            # which meant "Various Artists - Boogie Wonderland" and
            # "Earth, Wind & Fire - Boogie Wonderland" survived as TWO rows
            # because their artist strings differed. New logic:
            #   1. Partition by title only
            #   2. Rank compilation rows (VA / Various / blank) BELOW real
            #      artist rows for the same title
            #   3. Then rank by audio format (FLAC > M4A > ... > MP3)
            dedup_where = ("AND " + " AND ".join(conditions)) if conditions else ""
            async with db.execute(f"""
                SELECT * FROM (
                    SELECT
                        cd.chart_name, cd.peak_position, cd.weeks_on_chart,
                        cd.star_rating, cd.confidence, cd.comment_string,
                        cd.listener_count, cd.fetched_at,
                        COALESCE(hs.hit_year, cd.chart_year, t.year) AS chart_year,
                        hs.hit_score, hs.one_hit_wonder, hs.hot100_peak,
                        hs.hot100_weeks, hs.yearend_rank, t.duration_sec,
                        hs.listeners, hs.main_genre, hs.never_charted,
                        tg.family AS ge_family, tg.styles AS ge_styles,
                        t.title, t.file_path, t.file_format,
                        t.plex_rating_key, t.emby_id, t.jf_id,
                        t.track_id, t.genre_1, t.genre_2, t.genre_3,
                        COALESCE(t.tag_artist, a.name, '') AS tag_artist,
                        COALESCE(t.tag_album,  al.title, '') AS tag_album,
                        ROW_NUMBER() OVER (
                            PARTITION BY COALESCE(hs.song_key, LOWER(TRIM(t.title)) || '|' ||
                                         LOWER(TRIM(COALESCE(t.tag_artist, a.name, ''))))
                            ORDER BY
                                -- Compilation rows rank below real-artist rows
                                CASE
                                    WHEN LOWER(TRIM(COALESCE(t.tag_artist, a.name, '')))
                                         IN ('', 'various artists', 'various',
                                             'va', 'v.a.', 'compilation') THEN 2
                                    ELSE 1
                                END ASC,
                                -- Then prefer lossless
                                CASE LOWER(t.file_format)
                                    WHEN 'flac' THEN 1 WHEN 'm4a'  THEN 2
                                    WHEN 'wav'  THEN 3 WHEN 'aiff' THEN 4
                                    WHEN 'ogg'  THEN 5 WHEN 'mp3'  THEN 6
                                    ELSE 7
                                END ASC
                        ) AS rn
                    FROM chart_data cd
                    JOIN tracks t   ON cd.track_id = t.track_id
                    LEFT JOIN artists a  ON t.artist_id = a.artist_id
                    LEFT JOIN albums al  ON t.album_id  = al.album_id
                    LEFT JOIN hit_scores hs ON hs.track_id = cd.track_id
                    LEFT JOIN track_genres tg ON tg.track_id = cd.track_id
                    WHERE 1=1 {dedup_where}
                ) WHERE rn = 1
                ORDER BY COALESCE(hit_score, 0) DESC, peak_position ASC
                LIMIT ? OFFSET ?
            """, page_params) as cur:
                rows = await cur.fetchall()

            return {
                "results": [dict(r) for r in rows],
                "count":   len(rows),
                "total":   total_count,
                "offset":  offset,
                "limit":   limit,
            }

    except Exception as e:
        log.exception("Results query error")
        raise HTTPException(500, str(e))


# ══════════════════════════════════════════════════════════════════════════════
#  PLAYLIST PUSH
# ══════════════════════════════════════════════════════════════════════════════

class PlaylistBuildRequest(BaseModel):
    genre:        Optional[str] = None          # same keys as /results genre filter
    genre_mode:   str = "main"
    crossovers:   bool = True
    decades:      List[int] = []                # [1980, 1990]
    min_score:    int = 0
    ohw_only:     bool = False
    minutes:      int = 120                     # per playlist (per day)
    days:         int = 1                       # 1–7 playlists, no song repeated across them
    familiar_pct: int = 90                      # % of runtime from the top-ranked pool
    artist_gap:   int = 12                      # songs before an artist may repeat


@router.post("/playlist/build")
async def playlist_build(req: PlaylistBuildRequest, _=Depends(require_auth)):
    """Build a sequenced playlist of a target length from Hit Scores."""
    res = await get_results(
        charts=None, min_peak=None, max_peak=None, min_weeks=None,
        min_year=None, max_year=None, confidence=None, genre=req.genre,
        min_score=req.min_score or None,
        decades=",".join(str(d) for d in req.decades) or None,
        ohw=req.ohw_only, genre_mode=req.genre_mode, crossovers=req.crossovers,
        limit=50000, offset=0, _=None,
    )
    # One version per song (strip "(Remastered)", "feat." etc.)
    best: dict = {}
    for r in res["results"]:
        k = (_artist_key(r.get("tag_artist", "")), _title_key(r.get("title", "")))
        if k not in best or (r.get("hit_score") or 0) > (best[k].get("hit_score") or 0):
            best[k] = r
    cands = sorted(best.values(), key=lambda r: -(r.get("hit_score") or 0))
    if not cands:
        return {"results": [], "count": 0, "minutes": 0, "candidates": 0,
                "message": "No songs match these filters."}

    days     = max(1, min(req.days, 7))
    per_day  = max(10, min(req.minutes, 24 * 60)) * 60
    target   = per_day * days
    fam_pct  = max(0, min(req.familiar_pct, 100))
    fam_goal = target * fam_pct / 100
    dur      = lambda r: r.get("duration_sec") or 240

    # Familiar: random draw from the top-ranked pool (≈1.5× what's needed → variety per build)
    pool, acc = [], 0
    for r in cands:
        if acc >= fam_goal * 1.5:
            break
        pool.append(r); acc += dur(r)
    random.shuffle(pool)
    picked, total, used = [], 0, set()
    for r in pool:
        if total >= fam_goal:
            break
        picked.append(r); used.add(r["track_id"]); total += dur(r)

    # Discovery: random from everything not yet used
    rest = [r for r in cands if r["track_id"] not in used]
    if fam_pct >= 100:
        rest.sort(key=lambda r: -(r.get("hit_score") or 0))
    else:
        random.shuffle(rest)
    for r in rest:
        if total >= target:
            break
        picked.append(r); used.add(r["track_id"]); total += dur(r)

    # Split across days: snake-deal by score so every day gets an equal share of the biggest hits
    picked.sort(key=lambda r: -(r.get("hit_score") or 0))
    buckets = [[] for _ in range(days)]
    for i, r in enumerate(picked):
        rnd, pos = divmod(i, days)
        buckets[pos if rnd % 2 == 0 else days - 1 - pos].append(r)

    def _sequence(items: list) -> list:
        random.shuffle(items)
        gap, order, recent = max(0, req.artist_gap), [], []
        while items:
            j = next((i for i, r in enumerate(items)
                      if _artist_key(r.get("tag_artist", "")) not in recent), 0)
            r = items.pop(j)
            order.append(r)
            if gap:
                recent.append(_artist_key(r.get("tag_artist", "")))
                recent = recent[-gap:]
        return order

    out_days, results = [], []
    for d, bucket in enumerate(buckets, start=1):
        order = _sequence(bucket)
        for i, r in enumerate(order):
            r = dict(r, _seq=i + 1, _day=d if days > 1 else None)
            results.append(r)
        out_days.append({"day": d, "track_ids": [r["track_id"] for r in order],
                         "minutes": round(sum(dur(r) for r in order) / 60)})

    return {
        "results":    results,
        "days":       out_days,
        "count":      len(results),
        "minutes":    round(total / 60),
        "unknown_durations": sum(1 for r in results if not r.get("duration_sec")),
        "candidates": len(cands),
        "short":      total < target,
    }


# ── Builder presets (stored server-side in app_settings) ─────────────────────
@router.get("/presets")
async def presets_get(_=Depends(require_auth)):
    try:
        async with aiosqlite.connect(_DYNAMIC_DB) as db:
            row = await (await db.execute(
                "SELECT value FROM app_settings WHERE key='groomer_presets'")).fetchone()
        return {"presets": json.loads(row[0]) if row and row[0] else {}}
    except Exception:
        return {"presets": {}}


@router.put("/presets")
async def presets_put(body: dict, _=Depends(require_auth)):
    presets = body.get("presets")
    if not isinstance(presets, dict) or len(presets) > 50:
        raise HTTPException(400, "presets must be an object (max 50)")
    async with aiosqlite.connect(_DYNAMIC_DB) as db:
        await db.execute(
            "INSERT INTO app_settings (key, value, updated_at) VALUES ('groomer_presets', ?, datetime('now')) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (json.dumps(presets),))
        await db.commit()
    return {"ok": True, "count": len(presets)}


class PlaylistPushRequest(BaseModel):
    server:        Optional[str] = None   # Required for push, optional for M3U
    playlist_name: str = "Chart Hits"
    track_ids:     Optional[List[int]] = None
    # Post-scan filters — applied to chart_data at playlist-build time, not at scan time
    charts:        Optional[List[str]] = None      # e.g. ['rock','country'] — None = all
    max_peak:      Optional[int] = None            # e.g. 40 = Top 40 only
    min_weeks:     Optional[int] = None            # e.g. 4 = charted 4+ weeks
    confidence:    Optional[str] = None            # 'high'|'medium'|'low'|None=all
    limit:         int = 5000
    path_mode:     Optional[str] = None            # 'navidrome' = use Navidrome prefix


async def _record_last_playlist(name: str, dest: str, count: int):
    """Start Here: remember the most recent playlist sent anywhere."""
    try:
        val = json.dumps({"name": name or "", "dest": dest, "count": count,
                          "at": datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")})
        async with aiosqlite.connect(_DYNAMIC_DB) as db:
            await db.execute(
                "INSERT INTO app_settings (key, value, updated_at) VALUES ('grm_last_push', ?, datetime('now')) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at", (val,))
            await db.commit()
    except Exception as e:
        log.warning(f"record last playlist: {e}")


@router.post("/playlist/push")
async def playlist_push(req: PlaylistPushRequest, _=Depends(require_auth)):
    if not req.server:
        raise HTTPException(400, "server is required for playlist push (plex/emby/jellyfin)")
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
            res = await _push_to_plex(base_url, token, req.playlist_name, tracks)
        else:
            user_id = extra.get("user_id", "")
            if req.server == "emby":
                res = await _push_to_emby(base_url, token, user_id, req.playlist_name, tracks)
            else:
                res = await _push_to_jellyfin(base_url, token, user_id, req.playlist_name, tracks)
        await _record_last_playlist(req.playlist_name, req.server, len(tracks))
        return res
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"Push failed: {e}")


@router.post("/playlist/m3u")
async def playlist_m3u(req: PlaylistPushRequest, _=Depends(require_auth)):
    tracks = await _get_playlist_tracks(req)
    if not tracks:
        raise HTTPException(404, "No tracks found.")

    # ── Path translation for M3U ─────────────────────────────────────────────
    # Track paths in the DB can be:
    #   a) Docker paths:  /music/FULL ALBUMS/...
    #   b) Server paths:  /media/NAS1/MUSIC TAGGED/FULL ALBUMS/...
    #   c) Desktop paths: /media/colby/NAS1/MUSIC TAGGED/FULL ALBUMS/...
    #
    # Standard M3U: translate to desktop/host path (what the user's machine opens).
    # Navidrome M3U: strip to relative suffix, prepend the Navidrome music mount.
    desktop_prefix   = ""
    docker_prefix    = os.environ.get("DOCKER_MUSIC_PREFIX", "/music")
    navidrome_prefix = ""
    server_raw       = ""
    try:
        async with aiosqlite.connect(_DYNAMIC_DB) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT key, value FROM app_settings "
                "WHERE key IN ('path_server_prefix','path_docker_prefix','path_navidrome_prefix')"
            ) as cur:
                rows = await cur.fetchall()
            ps = {r["key"]: r["value"] for r in rows}
            desktop_prefix   = ps.get("path_server_prefix",    "")
            navidrome_prefix = ps.get("path_navidrome_prefix", "").rstrip("/")
            kp = ps.get("path_docker_prefix", "")
            if kp:
                docker_prefix = kp
    except Exception:
        pass
    server_raw = os.environ.get("MEDIA_SERVER_MUSIC_PREFIX", "")

    # ── Helper: strip any known prefix, return the bare relative suffix ──────
    def _bare_suffix(fp: str) -> str:
        """Return the path component after the music root, e.g. /FULL ALBUMS/..."""
        for prefix in [desktop_prefix, docker_prefix, server_raw]:
            if prefix and fp.startswith(prefix):
                return fp[len(prefix):]
        # Last-resort anchor search
        if desktop_prefix:
            anchor = desktop_prefix.rstrip("/").rsplit("/", 1)[-1]
            idx = fp.find(f"/{anchor}/")
            if idx >= 0:
                return fp[idx + len(anchor):]
        return fp  # give up — return as-is

    def _translate_path(fp: str) -> str:
        """Standard translation: stored path → host/desktop path."""
        if not desktop_prefix or not fp:
            return fp
        if fp.startswith(desktop_prefix):
            return fp
        suffix = _bare_suffix(fp)
        return desktop_prefix + suffix if suffix != fp else fp

    def _translate_navidrome(fp: str) -> str:
        """Navidrome translation: stored path → Navidrome Docker mount path."""
        if not fp:
            return fp
        suffix = _bare_suffix(fp)
        # Ensure leading slash
        if suffix and not suffix.startswith("/"):
            suffix = "/" + suffix
        return navidrome_prefix + suffix

    # ── Choose translator based on path_mode ─────────────────────────────────
    if req.path_mode == "navidrome":
        if not navidrome_prefix:
            raise HTTPException(400, "Navidrome music path is not configured. Set it in The Kennel → Path Translator.")
        translate = _translate_navidrome
    else:
        translate = _translate_path

    lines = ["#EXTM3U", f"#PLAYLIST:{req.playlist_name}"]
    for t in tracks:
        artist = t.get("tag_artist", "")
        title  = t.get("title", "")
        fp     = translate(t.get("file_path", ""))
        lines.append(f"#EXTINF:-1,{artist} - {title}")
        lines.append(fp)

    await _record_last_playlist(req.playlist_name,
                                "navidrome-m3u" if req.path_mode == "navidrome" else "m3u", len(tracks))
    return PlainTextResponse(
        "\n".join(lines),
        media_type="audio/x-mpegurl",
        headers={"Content-Disposition": f'attachment; filename="{req.playlist_name}.m3u"'},
    )


async def _get_playlist_tracks(req: PlaylistPushRequest) -> list:
    """
    Resolve the final ordered track list to push to a media server.

    FIX (M4 bug): When the frontend sends explicit track_ids (user selected
    rows from the already-deduped results table), TRUST them. Previously we
    ran a second global dedup that filtered some of the selected IDs out
    because a higher-ranked dupe existed elsewhere in chart_data — this
    caused push counts to be lower than selected counts (e.g. pushed 900 of
    1500 selected, 977 of 1000).

    Dedup only fires when no track_ids were provided (e.g. "push everything
    matching peak<=N and charts=X" mode). When it does, it uses the same
    compilation-aware ranking as get_results() so the two paths agree.
    """
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
    if req.min_weeks:
        conditions.append("cd.weeks_on_chart >= ?"); params.append(req.min_weeks)
    if req.confidence:
        conditions.append("cd.confidence = ?"); params.append(req.confidence)

    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    params.append(req.limit)

    async with aiosqlite.connect(_DYNAMIC_DB) as db:
        db.row_factory = aiosqlite.Row

        # ── Path A: explicit track_ids — no dedup, trust the frontend ───────
        if req.track_ids:
            async with db.execute(f"""
                SELECT
                    t.track_id, t.title, t.file_path, t.file_format,
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
            # Keep the caller's order (built playlists are sequenced); one row per track
            pos = {tid: i for i, tid in enumerate(req.track_ids)}
            seen, out = set(), []
            for r in sorted((dict(r) for r in rows), key=lambda d: pos.get(d["track_id"], 1 << 30)):
                if r["track_id"] not in seen:
                    seen.add(r["track_id"]); out.append(r)
            return out

        # ── Path B: no explicit IDs — run compilation-aware dedup ───────────
        # Format rank: FLAC=1, M4A=2, WAV=3, AIFF=4, OGG=5, MP3=6, other=7
        # Compilation rank: VA/blank=2, real artist=1 (real artist wins)
        async with db.execute(f"""
            SELECT
                t.track_id, t.title, t.file_path, t.file_format,
                t.plex_rating_key, t.emby_id, t.jf_id,
                COALESCE(t.tag_artist, a.name, '') AS tag_artist,
                cd.peak_position, cd.chart_name
            FROM chart_data cd
            JOIN tracks t  ON cd.track_id = t.track_id
            LEFT JOIN artists a ON t.artist_id = a.artist_id
            WHERE cd.track_id IN (
                SELECT track_id FROM (
                    SELECT
                        t2.track_id,
                        ROW_NUMBER() OVER (
                            PARTITION BY LOWER(t2.title)
                            ORDER BY
                                CASE
                                    WHEN LOWER(TRIM(COALESCE(t2.tag_artist, a2.name, '')))
                                         IN ('', 'various artists', 'various',
                                             'va', 'v.a.', 'compilation') THEN 2
                                    ELSE 1
                                END ASC,
                                CASE LOWER(t2.file_format)
                                    WHEN 'flac' THEN 1 WHEN 'm4a'  THEN 2
                                    WHEN 'wav'  THEN 3 WHEN 'aiff' THEN 4
                                    WHEN 'ogg'  THEN 5 WHEN 'mp3'  THEN 6
                                    ELSE 7
                                END ASC
                        ) AS rn
                    FROM chart_data cd2
                    JOIN tracks t2 ON cd2.track_id = t2.track_id
                    LEFT JOIN artists a2 ON t2.artist_id = a2.artist_id
                ) WHERE rn = 1
            )
            {"AND " + " AND ".join(conditions) if conditions else ""}
            ORDER BY cd.peak_position ASC
            LIMIT ?
        """, params) as cur:
            rows = await cur.fetchall()
    return [dict(r) for r in rows]


# ══════════════════════════════════════════════════════════════════════════════
#  PLEX / EMBY / JELLYFIN PUSH
# ══════════════════════════════════════════════════════════════════════════════

async def _push_to_plex(base: str, token: str, name: str, tracks: list) -> dict:
    """Creates/replaces a Plex playlist in EXACT track order. Never guesses a different song."""
    headers = {"Accept": "application/json"}
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.get(f"{base}/", params={"X-Plex-Token": token}, headers=headers)
        if not r.is_success:
            raise HTTPException(502, f"Plex unreachable: HTTP {r.status_code}")
        machine_id = r.json().get("MediaContainer", {}).get("machineIdentifier", "")
        if not machine_id:
            raise HTTPException(502, "Could not get Plex machine ID")

        rating_keys, not_found, missing = [], 0, []
        for track in tracks:
            if track.get("plex_rating_key"):
                rating_keys.append(str(track["plex_rating_key"])); continue
            match = None
            try:
                sr = await client.get(f"{base}/search",
                    params={"query": track.get("title", ""), "type": 10, "X-Plex-Token": token},
                    headers=headers)
                if sr.is_success:
                    want_t = _title_key(track.get("title", ""))
                    match = next((i for i in sr.json().get("MediaContainer", {}).get("Metadata", [])
                                  if _title_key(i.get("title", "")) == want_t
                                  and _artist_match(track.get("tag_artist", ""),
                                                    i.get("originalTitle"), i.get("grandparentTitle"))),
                                 None)
            except Exception:
                pass
            if match and match.get("ratingKey"):
                rating_keys.append(str(match["ratingKey"]))
            else:
                not_found += 1
                missing.append(f"{track.get('tag_artist', '')} — {track.get('title', '')}")
        if missing:
            log.info(f"Plex push '{name}': {len(missing)} not found: {missing[:10]}")
        if not rating_keys:
            raise HTTPException(404, "No tracks found in Plex")

        # Replace existing playlist with the same name
        lr = await client.get(f"{base}/playlists", params={"X-Plex-Token": token}, headers=headers)
        if lr.is_success:
            for p in lr.json().get("MediaContainer", {}).get("Metadata", []):
                if p.get("title") == name:
                    await client.delete(f"{base}/playlists/{p['ratingKey']}",
                                        params={"X-Plex-Token": token})

        def _uri(keys):
            return (f"server://{machine_id}/com.plexapp.plugins.library"
                    f"/library/metadata/{','.join(keys)}")

        BATCH = 100
        cr = await client.post(f"{base}/playlists",
            params={"type": "audio", "title": name, "smart": "0",
                    "uri": _uri(rating_keys[:BATCH]), "X-Plex-Token": token},
            headers=headers)
        if not cr.is_success:
            raise HTTPException(502, f"Plex create failed: HTTP {cr.status_code}")
        pl_id = (cr.json().get("MediaContainer", {}).get("Metadata", [{}])[0] or {}).get("ratingKey")
        if not pl_id:
            raise HTTPException(502, "No playlist ID from Plex")

        added, failed = len(rating_keys[:BATCH]), 0
        for i in range(BATCH, len(rating_keys), BATCH):      # sequential → order preserved
            batch = rating_keys[i:i + BATCH]
            pr = await client.put(f"{base}/playlists/{pl_id}/items",
                params={"uri": _uri(batch), "X-Plex-Token": token}, headers=headers)
            if pr.is_success:
                added += len(batch)
            else:
                failed += len(batch)

    return {"ok": True, "server": "plex", "playlist": name,
            "added": added, "failed": failed, "not_found": not_found, "playlist_id": pl_id}


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
                want_t = _title_key(t.get("title", ""))
                m = next((i for i in items
                    if _title_key(i.get("Name", "")) == want_t
                    and _artist_match(t.get("tag_artist", ""),
                                      i.get("Artists") or [], i.get("AlbumArtist"))), None)
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

        # Delete existing playlist — paginated fetch + exact name match (no fuzzy SearchTerm)
        try:
            pl_start = 0
            while True:
                pr = await client.get(f"{base}/Users/{user_id}/Items",
                    params={"IncludeItemTypes":"Playlist","Recursive":"true",
                            "api_key":token,"StartIndex":pl_start,"Limit":200},
                    headers=ah)
                if not pr.is_success:
                    break
                pl_items = pr.json().get("Items", [])
                matched = next((p for p in pl_items if p.get("Name") == name), None)
                if matched:
                    dr = await client.delete(
                        f"{base}/Items/{matched['Id']}?api_key={token}", headers=ah)
                    log.info(f"Emby playlist delete: {dr.status_code}")
                    await asyncio.sleep(0.5)  # brief pause so Emby registers delete before create
                    break
                if len(pl_items) < 200:
                    break
                pl_start += len(pl_items)
        except Exception as e:
            log.warning(f"Emby playlist delete warning: {e}")

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
    """
    Jellyfin playlist push.

    ID resolution strategy (in priority order):
      1. jf_id already stored on track (from Jellyfin fetch — most reliable)
      2. Path-based lookup: fetch JF library with Path field, match file_path exactly
      3. Artist+title search fallback (last resort, less reliable)

    Delete strategy:
      - Fetch ALL playlists (no SearchTerm — avoids fuzzy match finding wrong playlist)
      - Exact name match
      - DELETE and confirm 200/204 before creating new playlist

    Batch adds:
      - Jellyfin create endpoint accepts all IDs at once (no 414 issue unlike Emby)
      - Add in batches of 200 for reliability on large playlists
    """
    hdrs = {"Accept":"application/json","Content-Type":"application/json"}
    not_found = 0

    async with httpx.AsyncClient(timeout=60.0) as client:

        # ── Build a path→jf_id index from the JF library for fast lookup ────────
        path_index: dict = {}
        try:
            start = 0
            while True:
                r = await client.get(f"{base}/Users/{user_id}/Items",
                    params={"IncludeItemTypes":"Audio","Recursive":"true",
                            "Fields":"Path","api_key":token,
                            "StartIndex":start,"Limit":1000},
                    headers=hdrs)
                if not r.is_success:
                    break
                items = r.json().get("Items", [])
                if not items:
                    break
                for item in items:
                    p = item.get("Path","")
                    if p:
                        path_index[p] = item.get("Id","")
                start += len(items)
                if len(items) < 1000:
                    break
        except Exception as e:
            log.warning(f"JF path index build failed: {e}")

        # ── Resolve JF IDs for each track ────────────────────────────────────────
        jf_ids = []
        for t in tracks:
            # Priority 1: stored jf_id
            if t.get("jf_id"):
                jf_ids.append(t["jf_id"]); continue

            # Priority 2: path match
            fp = t.get("file_path","")
            if fp and fp in path_index:
                jf_ids.append(path_index[fp]); continue

            # Priority 3: artist+title search fallback
            try:
                r = await client.get(f"{base}/Users/{user_id}/Items",
                    params={"searchTerm": t.get("title",""),
                            "IncludeItemTypes":"Audio","Recursive":"true",
                            "Fields":"Path","api_key":token,
                            "Limit":20},
                    headers=hdrs)
                items = r.json().get("Items",[]) if r.is_success else []
                want_t = _title_key(t.get("title", ""))
                # Title + artist must both match — never guess a different song
                match = next((i for i in items
                    if _title_key(i.get("Name", "")) == want_t
                    and _artist_match(t.get("tag_artist", ""),
                                      i.get("Artists") or [], i.get("AlbumArtist"))), None)
                if match and match.get("Id"):
                    jf_ids.append(match["Id"])
                else:
                    not_found += 1
            except Exception:
                not_found += 1

        if not jf_ids:
            raise HTTPException(404, "No tracks found in Jellyfin")

        # ── Delete existing playlist — fetch ALL, exact name match ────────────────
        try:
            # Jellyfin paginates playlists — fetch until exhausted
            pl_start = 0
            while True:
                pr = await client.get(f"{base}/Users/{user_id}/Items",
                    params={"IncludeItemTypes":"Playlist","Recursive":"true",
                            "api_key":token,"StartIndex":pl_start,"Limit":200},
                    headers=hdrs)
                if not pr.is_success:
                    break
                pl_items = pr.json().get("Items",[])
                matched = next((p for p in pl_items if p.get("Name") == name), None)
                if matched:
                    dr = await client.delete(
                        f"{base}/Items/{matched['Id']}?api_key={token}", headers=hdrs)
                    log.info(f"JF playlist delete: {dr.status_code}")
                    break
                if len(pl_items) < 200:
                    break
                pl_start += len(pl_items)
        except Exception as e:
            log.warning(f"JF playlist delete warning: {e}")

        # ── Create playlist with first batch ──────────────────────────────────────
        first_batch = jf_ids[:200]
        cr = await client.post(f"{base}/Playlists?api_key={token}", headers=hdrs,
            json={"Name":name,"Ids":first_batch,"UserId":user_id,"MediaType":"Audio"})
        if not cr.is_success:
            raise HTTPException(502, f"JF create failed: {cr.status_code}")
        pl  = cr.json()
        pl_id = pl.get("Id") or pl.get("id")
        if not pl_id:
            raise HTTPException(502, "No playlist ID from Jellyfin")

        # ── Add remaining in batches of 200 ───────────────────────────────────────
        # NOTE: /Playlists/{id}/Items requires Ids as query param, not JSON body
        for i in range(200, len(jf_ids), 200):
            batch = jf_ids[i:i+200]
            add_r = await client.post(
                f"{base}/Playlists/{pl_id}/Items",
                params={"api_key": token, "Ids": ",".join(batch), "UserId": user_id},
                headers=hdrs
            )
            if not add_r.is_success:
                log.warning(f"JF batch add failed: {add_r.status_code} — {add_r.text[:200]}")

    added = len(jf_ids)
    return {"ok":True,"server":"jellyfin","playlist":name,
            "added":added,"not_found":not_found,"playlist_id":pl_id}


# ══════════════════════════════════════════════════════════════════════════════
#  BIG HITS YOU DON'T OWN (M6) — shared by Top Dogs + The Sniffer
# ══════════════════════════════════════════════════════════════════════════════

class BigHitsRequest(BaseModel):
    decades:    List[int] = []
    genres:     List[str] = []
    min_score:  int = 70
    ohw_only:   bool = False
    crossovers: bool = True
    limit:      int = 500


_bh_lookup = {"running": False, "done": 0, "total": 0}


async def _lfm_lookup_pairs(pairs: list):
    """Background: staying-power lookups for missing songs (cached in lastfm_tracks)."""
    if _bh_lookup["running"] or not pairs:
        return
    key = await _get_lastfm_key()
    if not key:
        return
    _bh_lookup.update(running=True, done=0, total=len(pairs))
    SQL = ("INSERT INTO lastfm_tracks (artist_key, title_key, status, listeners, playcount, tags, fetched_at) "
           "VALUES (?, ?, ?, ?, ?, ?, datetime('now')) ON CONFLICT(artist_key, title_key) DO UPDATE SET "
           "status=excluded.status, listeners=excluded.listeners, playcount=excluded.playcount, "
           "tags=excluded.tags, fetched_at=excluded.fetched_at")
    try:
        async with httpx.AsyncClient(timeout=20.0) as client, \
                   aiosqlite.connect(_DYNAMIC_DB, timeout=60) as db:
            for i, (artist, title) in enumerate(pairs):
                t0 = time.monotonic()
                kind, j = await _lfm_call(client, {"method": "track.getInfo", "api_key": key, "autocorrect": 1,
                                                   "artist": _FEAT_RE.sub("", artist),
                                                   "track": _lfm_clean_title(title)})
                ak, tk = _artist_key(artist), _title_key(title)
                if kind == "ok":
                    tr = j.get("track", {}) or {}
                    await db.execute(SQL, (ak, tk, "ok", int(tr.get("listeners") or 0),
                                           int(tr.get("playcount") or 0),
                                           json.dumps(_lfm_tag_names(tr.get("toptags")))))
                elif kind == "notfound":
                    await db.execute(SQL, (ak, tk, "notfound", None, None, None))
                elif kind == "fatal":
                    break
                await db.commit()
                _bh_lookup["done"] = i + 1
                await asyncio.sleep(max(0.0, _LFM_INTERVAL - (time.monotonic() - t0)))
    except Exception as e:
        log.warning(f"Big-hits Last.fm lookups stopped: {e}")
    finally:
        _bh_lookup["running"] = False


@router.post("/big-hits")
async def big_hits(req: BigHitsRequest, _=Depends(require_auth)):
    await _migrate_chart_data()
    res = await asyncio.get_event_loop().run_in_executor(
        None, _big_hits_sync, req.decades, req.genres, req.min_score, req.ohw_only,
        req.crossovers, max(0, min(req.limit, 2000)))
    pending = res.pop("pending_lookups")
    if pending and not _bh_lookup["running"]:
        asyncio.create_task(_lfm_lookup_pairs(pending))
    res["lookups_pending"] = len(pending)
    res["lookups_running"] = _bh_lookup["running"] or bool(pending)
    return res


@router.get("/big-hits/status")
async def big_hits_status(_=Depends(require_auth)):
    return _bh_lookup


# ══════════════════════════════════════════════════════════════════════════════
#  BILLBOARD 200 ALBUMS (M6) — for The Bloodhound
# ══════════════════════════════════════════════════════════════════════════════
_B200_URL = "https://raw.githubusercontent.com/utdata/rwd-billboard-data/main/data-out/billboard-200-current.csv"
_b200_job = {"status": "idle", "message": ""}


async def _b200_migrate():
    async with aiosqlite.connect(_DYNAMIC_DB) as db:
        await db.execute("""CREATE TABLE IF NOT EXISTS album_charts (
            artist_key TEXT NOT NULL, album_key TEXT NOT NULL, artist TEXT, album TEXT,
            peak INTEGER, weeks INTEGER, first_year INTEGER,
            PRIMARY KEY (artist_key, album_key))""")
        await db.commit()


async def _import_b200() -> int:
    """Download the weekly Billboard 200 history and keep one row per album (best peak, most weeks)."""
    import csv, io
    _b200_job.update(status="running", message="Downloading Billboard 200 history…")
    try:
        async with httpx.AsyncClient(timeout=120.0, follow_redirects=True) as client:
            r = await client.get(_B200_URL)
            r.raise_for_status()
        agg = {}
        for row in csv.DictReader(io.StringIO(r.text)):
            artist, album = row.get("performer") or "", row.get("title") or ""
            if not artist or not album:
                continue
            key = (_artist_key(artist), _title_key(album))
            try:
                peak = int(row.get("peak_pos") or 999)
                wks = int(row.get("wks_on_chart") or 0)
                yr = int((row.get("chart_week") or "0000")[:4])
            except ValueError:
                continue
            cur = agg.get(key)
            if cur is None:
                agg[key] = [artist, album, peak, wks, yr]
            else:
                cur[2] = min(cur[2], peak); cur[3] = max(cur[3], wks); cur[4] = min(cur[4], yr)
        await _b200_migrate()
        async with aiosqlite.connect(_DYNAMIC_DB, timeout=60) as db:
            await db.execute("DELETE FROM album_charts")
            await db.executemany(
                "INSERT INTO album_charts (artist_key, album_key, artist, album, peak, weeks, first_year) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                [(k[0], k[1], *v) for k, v in agg.items()])
            await db.commit()
        await _set_setting("b200_imported_at", datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"))
        _b200_job.update(status="done", message=f"{len(agg):,} albums")
        log.info(f"Billboard 200 imported: {len(agg):,} albums")
        return len(agg)
    except Exception as e:
        log.warning(f"Billboard 200 import failed: {e}")
        _b200_job.update(status="failed", message=str(e))
        return 0


async def _b200_startup():
    """Import on first run and refresh weekly."""
    await asyncio.sleep(240)
    while True:
        try:
            last = await _get_setting("b200_imported_at")
            if not last or time.time() - _utc_epoch(last) > 7 * 86400:
                await _import_b200()
        except Exception:
            pass
        await asyncio.sleep(6 * 3600)


def _album_score(peak, weeks) -> int:
    """Peak matters most; weeks on a log scale so long-running classics keep separating (10 yrs = max)."""
    import math
    wk = min(1.0, math.log1p(weeks or 0) / math.log1p(520))
    return int(round(65 * _peak_factor(peak or 200) ** 0.8 + 35 * wk))


def _owned_album_keys(dconn) -> set:
    from app.routers.retriever import _ge_album_name
    owned = set()
    for ar, alb, fp in dconn.execute(
            "SELECT COALESCE(tag_artist, ''), album_title, file_path FROM tracks "
            "WHERE COALESCE(missing, 0) = 0"):
        name = _ge_album_name(alb, fp or "")
        if name:
            owned.add((_artist_key(ar), _title_key(name)))
    return owned


def album_chart_info_sync(artist: str, albums: list) -> dict:
    """{album title: {peak, weeks, score}} for one artist's albums (Bloodhound)."""
    ak = _artist_key(artist)
    dconn = sqlite3.connect(_DYNAMIC_DB, check_same_thread=False)
    try:
        rows = {k: (p, w) for k, p, w in dconn.execute(
            "SELECT album_key, peak, weeks FROM album_charts WHERE artist_key=?", (ak,))}
    except Exception:
        rows = {}
    finally:
        dconn.close()
    out = {}
    for title in albums:
        hit = rows.get(_title_key(title))
        if hit:
            out[title] = {"b200_peak": hit[0], "b200_weeks": hit[1], "album_score": _album_score(*hit)}
    return out


class EssentialAlbumsRequest(BaseModel):
    decades:       List[int] = []
    include_owned: bool = True
    limit:         int = 300


@router.post("/albums/essential")
async def albums_essential(req: EssentialAlbumsRequest, _=Depends(require_auth)):
    await _b200_migrate()
    await _migrate_chart_data()

    def run():
        dconn = sqlite3.connect(_DYNAMIC_DB, check_same_thread=False)
        try:
            owned = _owned_album_keys(dconn)
            rows = dconn.execute(
                "SELECT artist_key, album_key, artist, album, peak, weeks, first_year FROM album_charts").fetchall()
        finally:
            dconn.close()
        ranges = [(d, d + 9) for d in req.decades]
        out = []
        for ak, alk, artist, album, peak, weeks, yr in rows:
            if ranges and not (yr and any(lo <= yr <= hi for lo, hi in ranges)):
                continue
            is_owned = (ak, alk) in owned
            if is_owned and not req.include_owned:
                continue
            out.append({"artist": artist, "album": album, "year": yr, "b200_peak": peak,
                        "b200_weeks": weeks, "album_score": _album_score(peak, weeks), "in_library": is_owned})
        out.sort(key=lambda r: -r["album_score"])
        return {"total": len(out), "owned": sum(r["in_library"] for r in out), "results": out[:req.limit]}

    return await asyncio.get_event_loop().run_in_executor(None, run)


@router.get("/albums/status")
async def albums_status(_=Depends(require_auth)):
    await _b200_migrate()
    async with aiosqlite.connect(_DYNAMIC_DB) as db:
        n = (await (await db.execute("SELECT COUNT(*) FROM album_charts")).fetchone())[0]
    return {"albums": n, "imported_at": await _get_setting("b200_imported_at"), **_b200_job}


@router.post("/albums/import")
async def albums_import(_=Depends(require_auth)):
    if _b200_job.get("status") == "running":
        raise HTTPException(409, "Already importing.")
    n = await _import_b200()
    if not n:
        raise HTTPException(502, _b200_job.get("message") or "Import failed")
    return {"ok": True, "albums": n}

