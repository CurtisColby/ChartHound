# © 2026 Colby R. Curtis | ChartHound: The New World
# All Rights Reserved.
"""
ChartHound — The Sniffer Router
Chart-Hit Finder & Grabber

Search: Album-first via Torznab per-indexer endpoints (same method as Lidarr).
Download: Add to qBit → background task retries file priority checkmark
          for up to 60 seconds to ensure download starts.

Endpoints:
  POST /api/sniffer/gap-analysis     — Cross-ref static DB vs user library
  POST /api/sniffer/trending         — Last.fm trending tracks (paginated)
  POST /api/sniffer/search           — Search Prowlarr (album-first)
  POST /api/sniffer/grab             — Push to qBit + background checkmark
  GET  /api/sniffer/year-range       — Min/max year from static DB
  GET  /api/sniffer/genres           — Distinct genres from tracks table
"""

# © 2026 Colby R. Curtis | ChartHound: The New World — All Rights Reserved.

import asyncio
import json
import logging
import os
import re
import sqlite3
import xml.etree.ElementTree as ET

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
        total = conn.execute(f"SELECT COUNT(*) FROM chart_reference {where}", params).fetchone()[0]
        rows = conn.execute(f"""
            SELECT ref_id, chart_name, artist, title, artist_norm, title_norm,
                   peak_position, weeks_on_chart, chart_year, data_source
            FROM chart_reference {where}
            ORDER BY peak_position ASC, chart_year DESC
            LIMIT ? OFFSET ?
        """, params + [req.limit, req.offset]).fetchall()
        entries = [dict(r) for r in rows]
        conn.close()
    except Exception as e:
        raise HTTPException(500, f"Static DB error: {e}")

    if not entries:
        return {"results": [], "total": 0, "offset": req.offset, "limit": req.limit}

    lib = await _build_library_index()
    results = []
    for e in entries:
        owned, tid = _check_library(e["artist"], e["title"], lib)
        results.append({
            "ref_id": e["ref_id"], "artist": e["artist"], "title": e["title"],
            "chart_name": e["chart_name"],
            "chart_display": CHART_DISPLAY.get(e["chart_name"], e["chart_name"]),
            "peak_position": e["peak_position"], "weeks_on_chart": e["weeks_on_chart"],
            "chart_year": e["chart_year"], "in_library": owned, "track_id": tid,
        })
    owned_ct = sum(1 for r in results if r["in_library"])
    return {"results": results, "total": total, "owned": owned_ct,
            "missing": len(results) - owned_ct, "offset": req.offset, "limit": req.limit}


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
