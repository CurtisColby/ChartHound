# © 2026 Colby R. Curtis | ChartHound: The New World
# All Rights Reserved.
"""
ChartHound — The Bloodhound Router
Artist & Album Hunter

Search MusicBrainz for artists/albums/compilations → browse results →
search Prowlarr → grab via qBittorrent.

MusicBrainz rate limit: 1 request/second (enforced via asyncio.sleep).
User-Agent required per MB policy.

Endpoints:
  POST /api/bloodhound/artist-search     — Search MB for artists
  POST /api/bloodhound/artist-releases   — Get releases for an MB artist
  POST /api/bloodhound/album-search      — Search MB for albums/releases
  POST /api/bloodhound/search-prowlarr   — Search Prowlarr for a release
  POST /api/bloodhound/grab              — Push to qBit + background checkmark
"""

# © 2026 Colby R. Curtis | ChartHound: The New World — All Rights Reserved.

import asyncio
import json
import logging

import aiosqlite
import httpx

from typing import Optional, List
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.config import get_settings
from app.deps import require_auth
from app.security import decrypt_token

# Reuse sniffer helpers — no code duplication
from app.routers.sniffer import (
    _get_connection, _qbt_login, _background_checkmark,
    _build_library_index, _check_library,
)

log      = logging.getLogger("charthound.bloodhound")
router   = APIRouter(prefix="/api/bloodhound", tags=["bloodhound"])
settings = get_settings()

_DYNAMIC_DB = getattr(settings, "database_url",  "/data/charthound.db")
_MB_BASE    = "https://musicbrainz.org/ws/2"
_MB_UA      = "ChartHound/1.0.0 (charthound.duckdns.org)"
_AUDIO_CATS = "3000,3010,3030,3040,3050"


# ══════════════════════════════════════════════════════════════════════════════
#  REQUEST MODELS
# ══════════════════════════════════════════════════════════════════════════════

class ArtistSearchRequest(BaseModel):
    query:  str
    limit:  int = 10

class ArtistReleasesRequest(BaseModel):
    artist_mbid:  str
    artist_name:  str = ""
    release_type: str = "album"   # album | compilation | single | all

class AlbumSearchRequest(BaseModel):
    query:  str
    limit:  int = 100
    offset: int = 0

class ProwlarrSearchRequest(BaseModel):
    query:    str
    artist:   str = ""

class BHGrabRequest(BaseModel):
    download_url:  str
    title:         str
    indexer:       str = ""


# ══════════════════════════════════════════════════════════════════════════════
#  MUSICBRAINZ HELPERS
# ══════════════════════════════════════════════════════════════════════════════

_mb_lock = asyncio.Lock()
_mb_last_call = 0.0

async def _mb_get(path: str, params: dict) -> dict:
    """Rate-limited MusicBrainz API GET. Max 1 req/sec."""
    import time
    global _mb_last_call

    async with _mb_lock:
        now = time.monotonic()
        elapsed = now - _mb_last_call
        if elapsed < 1.1:
            await asyncio.sleep(1.1 - elapsed)
        _mb_last_call = time.monotonic()

    params["fmt"] = "json"
    headers = {"User-Agent": _MB_UA, "Accept": "application/json"}

    async with httpx.AsyncClient(timeout=15.0) as client:
        r = await client.get(f"{_MB_BASE}/{path}", params=params, headers=headers)
        if r.status_code == 503:
            # Rate limited — wait and retry once
            await asyncio.sleep(2)
            r = await client.get(f"{_MB_BASE}/{path}", params=params, headers=headers)
        if not r.is_success:
            raise HTTPException(502, f"MusicBrainz error: HTTP {r.status_code}")
        return r.json()


# ══════════════════════════════════════════════════════════════════════════════
#  ARTIST SEARCH
# ══════════════════════════════════════════════════════════════════════════════

@router.post("/artist-search")
async def artist_search(req: ArtistSearchRequest, _=Depends(require_auth)):
    """Search MusicBrainz for artists by name."""
    if not req.query.strip():
        raise HTTPException(400, "Search query is empty.")

    data = await _mb_get("artist", {"query": req.query, "limit": str(req.limit)})
    artists = []
    for a in data.get("artists", []):
        score = a.get("score", 0)
        artists.append({
            "mbid":      a.get("id", ""),
            "name":      a.get("name", ""),
            "sort_name": a.get("sort-name", ""),
            "country":   a.get("country", ""),
            "type":      a.get("type", ""),
            "score":     score,
            "begin":     (a.get("life-span") or {}).get("begin", ""),
            "end":       (a.get("life-span") or {}).get("end", ""),
            "ended":     (a.get("life-span") or {}).get("ended", False),
            "tags":      [t.get("name", "") for t in (a.get("tags") or [])[:5]],
        })
    return {"results": artists, "total": len(artists), "query": req.query}


# ══════════════════════════════════════════════════════════════════════════════
#  ARTIST RELEASES (albums, compilations, singles)
# ══════════════════════════════════════════════════════════════════════════════

@router.post("/artist-releases")
async def artist_releases(req: ArtistReleasesRequest, _=Depends(require_auth)):
    """Get release groups for a MusicBrainz artist by MBID."""
    if not req.artist_mbid:
        raise HTTPException(400, "Artist MBID is required.")

    type_filter = ""
    if req.release_type == "album":
        type_filter = "album"
    elif req.release_type == "compilation":
        type_filter = "compilation"
    elif req.release_type == "single":
        type_filter = "single"
    # "all" = no filter

    params = {"artist": req.artist_mbid, "limit": "100"}
    if type_filter:
        params["type"] = type_filter

    data = await _mb_get("release-group", params)
    releases = []
    for rg in data.get("release-groups", []):
        primary = rg.get("primary-type", "")
        secondary = rg.get("secondary-types", [])
        releases.append({
            "mbid":          rg.get("id", ""),
            "title":         rg.get("title", ""),
            "primary_type":  primary,
            "secondary_types": secondary,
            "first_release":  rg.get("first-release-date", ""),
        })

    # Sort by first release date
    releases.sort(key=lambda x: x.get("first_release", "") or "9999")

    # Check library ownership
    lib, _ = await _build_library_index()
    artist_name = req.artist_name or ""
    for rel in releases:
        owned, tid = _check_library(artist_name, rel["title"], lib)
        rel["in_library"] = owned
        rel["track_id"] = tid

    owned_ct = sum(1 for r in releases if r["in_library"])
    return {
        "results": releases,
        "total": len(releases),
        "owned": owned_ct,
        "missing": len(releases) - owned_ct,
        "artist_mbid": req.artist_mbid,
        "artist_name": req.artist_name,
        "release_type": req.release_type,
    }


# ══════════════════════════════════════════════════════════════════════════════
#  ALBUM SEARCH (direct search by album name)
# ══════════════════════════════════════════════════════════════════════════════

@router.post("/album-search")
async def album_search(req: AlbumSearchRequest, _=Depends(require_auth)):
    """Search MusicBrainz for release groups by name."""
    if not req.query.strip():
        raise HTTPException(400, "Search query is empty.")

    data = await _mb_get("release-group", {
        "query": req.query, "limit": str(req.limit), "offset": str(req.offset)
    })
    mb_count = data.get("count", 0)
    results = []
    for rg in data.get("release-groups", []):
        artist_credit = ""
        ac = rg.get("artist-credit", [])
        if ac:
            artist_credit = " ".join(
                c.get("artist", {}).get("name", "") + (c.get("joinphrase", "") or "")
                for c in ac
            ).strip()

        results.append({
            "mbid":          rg.get("id", ""),
            "title":         rg.get("title", ""),
            "artist":        artist_credit,
            "primary_type":  rg.get("primary-type", ""),
            "secondary_types": rg.get("secondary-types", []),
            "first_release":  rg.get("first-release-date", ""),
            "score":          rg.get("score", 0),
        })

    # Check library ownership
    lib, _ = await _build_library_index()
    for rel in results:
        owned, tid = _check_library(rel.get("artist", ""), rel["title"], lib)
        rel["in_library"] = owned
        rel["track_id"] = tid

    owned_ct = sum(1 for r in results if r["in_library"])
    return {"results": results, "total": len(results), "query": req.query,
            "mb_count": mb_count, "offset": req.offset, "limit": req.limit,
            "owned": owned_ct, "missing": len(results) - owned_ct}


# ══════════════════════════════════════════════════════════════════════════════
#  PROWLARR SEARCH (reuses sniffer torznab logic)
# ══════════════════════════════════════════════════════════════════════════════

@router.post("/search-prowlarr")
async def search_prowlarr(req: ProwlarrSearchRequest, _=Depends(require_auth)):
    """Search Prowlarr for a release via Torznab (same method as Sniffer)."""
    if not req.query.strip():
        raise HTTPException(400, "Search query is empty.")

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

    import xml.etree.ElementTree as ET

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
        log.info(f"BH Torznab '{query}': {len(all_items)} items from {len(indexer_ids)} indexers")
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

    # Primary search
    results = await _torznab_search(req.query)
    parsed = _parse(results)

    # If artist provided, also search artist name for broader album results
    if req.artist and req.artist.lower().strip() != req.query.lower().strip():
        more = await _torznab_search(req.artist)
        seen = {r["title"].lower().strip() for r in parsed}
        for r in _parse(more):
            if r["title"].lower().strip() not in seen:
                parsed.append(r)
                seen.add(r["title"].lower().strip())
        parsed.sort(key=lambda x: x["seeders"], reverse=True)
        parsed = parsed[:25]

    return {"results": parsed, "total": len(parsed), "query": req.query}


# ══════════════════════════════════════════════════════════════════════════════
#  GRAB — Add to qBit + background checkmark (reuses sniffer logic)
# ══════════════════════════════════════════════════════════════════════════════

@router.post("/grab")
async def grab(req: BHGrabRequest, _=Depends(require_auth)):
    """Send torrent to qBittorrent with charthound-music category."""
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

    # Read configured download path from app_settings (Kennel → ChartHound Download Path)
    save_path = ""
    try:
        async with aiosqlite.connect(_DYNAMIC_DB) as db:
            async with db.execute(
                "SELECT value FROM app_settings WHERE key='sniffer_download_path'"
            ) as cur:
                row = await cur.fetchone()
                if row:
                    save_path = row[0] or ""
    except Exception:
        pass

    async with httpx.AsyncClient(timeout=30.0, verify=False) as client:
        sid = await _qbt_login(client, base, extra, pwd)
        cookies = {"SID": sid}

        r = await client.post(f"{base}/api/v2/torrents/add",
            data={"urls": req.download_url, "category": "charthound-music",
                  "savepath": save_path},
            cookies=cookies)
        if not r.is_success:
            raise HTTPException(502, f"qBittorrent add failed: {r.status_code}")

        # Grab hash of the torrent we just added
        await asyncio.sleep(1)
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
