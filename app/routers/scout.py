# © 2026 Colby R. Curtis | ChartHound: The New World
# All Rights Reserved.
"""
ChartHound — The Scout Router
YouTube Music Video Curator (Shape B: deeplink output, no OAuth)

Searches YouTube Data API v3 for music videos based on user query or
batch input from other tabs (Bloodhound exports, Tracker missing list).
Filters by HD, duration, channel name, view count. Outputs are
deeplinks (youtube.com/watch_videos?video_ids=...) so the user can
save the playlist into their own YouTube account with one click — no
ChartHound-side OAuth, no account access.

Caching: scout_cache table stores search results keyed by (query +
filter hash) for 30 days. Cache hits cost zero API quota.
Quota: scout_quota tracks daily unit consumption against the 10K free
tier. Search costs 100u, video details 1u/video. Hard stop at 9500u.

CYCLE 1B STATUS: skeleton with mock data. Cycle 2 plugs in real
httpx calls to googleapis.com/youtube/v3.

Endpoints:
  POST /api/scout/search          — Single query → top N filtered results
  POST /api/scout/search-batch    — List of {artist,title} → best match each
  POST /api/scout/build-playlist  — List of video_ids → deeplink + exports
  GET  /api/scout/quota-status    — Today's quota usage
"""

import asyncio
import hashlib
import json
import logging
import re
import time
from datetime import datetime, timezone
from typing import List, Optional

import aiosqlite
import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.config import get_settings
from app.deps import require_auth
from app.security import decrypt_token

log      = logging.getLogger("charthound.scout")
router   = APIRouter(prefix="/api/scout", tags=["scout"])
settings = get_settings()

_DYNAMIC_DB    = getattr(settings, "database_url", "/data/charthound.db")
_QUOTA_DAILY   = 10_000   # YouTube Data API v3 free tier
_QUOTA_SAFETY  = 500      # leave headroom — hard-stop at 9500
_CACHE_TTL_SEC = 30 * 24 * 60 * 60  # 30 days


# ─────────────────────────── SCHEMA ───────────────────────────

async def _ensure_scout_tables():
    """Create scout_cache and scout_quota in the dynamic DB. Idempotent."""
    try:
        async with aiosqlite.connect(_DYNAMIC_DB) as db:
            await db.executescript("""
                CREATE TABLE IF NOT EXISTS scout_cache (
                    cache_key       TEXT PRIMARY KEY,
                    query           TEXT NOT NULL,
                    filters_hash    TEXT NOT NULL,
                    results_json    TEXT NOT NULL,
                    cached_at       INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_scout_cache_query
                    ON scout_cache(query);
                CREATE INDEX IF NOT EXISTS idx_scout_cache_age
                    ON scout_cache(cached_at);

                CREATE TABLE IF NOT EXISTS scout_quota (
                    date_utc        TEXT PRIMARY KEY,
                    units_used      INTEGER NOT NULL DEFAULT 0
                );
            """)
            await db.commit()
    except Exception as e:
        log.warning(f"_ensure_scout_tables: {e}")


# Run schema setup at module import — same lazy pattern as other routers
asyncio.get_event_loop_policy()  # ensure policy exists; actual create happens on first call


# ─────────────────────────── YOUTUBE API CONSTANTS ───────────────────────────

_YT_BASE         = "https://www.googleapis.com/youtube/v3"
_YT_TIMEOUT      = 15.0
_COST_SEARCH     = 100  # YouTube Data API v3: search.list costs 100 units
_COST_VIDEO      = 1    # videos.list costs 1 unit per video returned
_BATCH_DELAY_SEC = 0.2  # space out batch calls to avoid burst limits


# ─────────────────────────── MODELS ───────────────────────────

class ScoutFilters(BaseModel):
    min_duration_seconds:     int  = Field(60, ge=0,  le=3600)
    min_view_count:           int  = Field(0,  ge=0)
    definition:               str  = Field("any", pattern="^(any|high)$")
    channel_must_contain:     str  = ""
    channel_must_not_contain: str  = ""

    def hash(self) -> str:
        s = json.dumps(self.dict(), sort_keys=True)
        return hashlib.sha1(s.encode()).hexdigest()[:12]


class ScoutSearchRequest(BaseModel):
    query:       str  = Field(..., min_length=1, max_length=200)
    max_results: int  = Field(10, ge=1, le=20)
    filters:     ScoutFilters = Field(default_factory=ScoutFilters)


class ScoutSearchBatchItem(BaseModel):
    artist: str
    title:  str


class ScoutSearchBatchRequest(BaseModel):
    items:       List[ScoutSearchBatchItem]
    filters:     ScoutFilters = Field(default_factory=ScoutFilters)


class ScoutBuildPlaylistRequest(BaseModel):
    video_ids: List[str] = Field(..., min_length=1, max_length=50)
    title:     str       = Field("ChartHound Playlist", max_length=120)


# ─────────────────────────── HELPERS ───────────────────────────

def _today_utc_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


async def _get_quota_used() -> int:
    """Return today's units used (0 if no row yet)."""
    try:
        async with aiosqlite.connect(_DYNAMIC_DB) as db:
            row = await (await db.execute(
                "SELECT units_used FROM scout_quota WHERE date_utc = ?",
                (_today_utc_str(),)
            )).fetchone()
            return int(row[0]) if row else 0
    except Exception as e:
        log.warning(f"_get_quota_used: {e}")
        return 0


async def _add_quota(units: int) -> None:
    """Add to today's quota counter. Creates row if missing."""
    try:
        async with aiosqlite.connect(_DYNAMIC_DB) as db:
            await db.execute(
                """INSERT INTO scout_quota (date_utc, units_used) VALUES (?, ?)
                   ON CONFLICT(date_utc) DO UPDATE SET units_used = units_used + excluded.units_used""",
                (_today_utc_str(), units)
            )
            await db.commit()
    except Exception as e:
        log.warning(f"_add_quota: {e}")


async def _check_quota_or_raise(needed: int):
    """Raise 429 if this call would exceed daily safety threshold."""
    used = await _get_quota_used()
    if used + needed > (_QUOTA_DAILY - _QUOTA_SAFETY):
        raise HTTPException(
            status_code=429,
            detail=(f"YouTube API daily quota would be exceeded "
                    f"({used} used + {needed} needed > "
                    f"{_QUOTA_DAILY - _QUOTA_SAFETY} cap). "
                    "Try again tomorrow (00:00 UTC reset).")
        )


async def _get_youtube_key() -> str:
    """Fetch + decrypt the YouTube API key from connections. Raises 503 if absent."""
    try:
        async with aiosqlite.connect(_DYNAMIC_DB) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT token_enc FROM connections WHERE service='youtube'"
            ) as cur:
                row = await cur.fetchone()
                if not row or not row["token_enc"]:
                    raise HTTPException(503, "No YouTube API key configured. Connect YouTube in The Kennel first.")
                key = decrypt_token(row["token_enc"]) or ""
                if not key:
                    raise HTTPException(503, "YouTube API key is stored but failed to decrypt. Re-save it in The Kennel.")
                return key
    except HTTPException:
        raise
    except Exception as e:
        log.warning(f"_get_youtube_key: {e}")
        raise HTTPException(500, f"Failed to read YouTube API key: {e}")


_ISO_DUR_RE = re.compile(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?")

def _iso_duration_to_sec(iso: str) -> int:
    """Parse YouTube ISO 8601 duration (PT3M42S) to seconds. Returns 0 on parse fail."""
    if not iso:
        return 0
    m = _ISO_DUR_RE.match(iso)
    if not m:
        return 0
    h, mn, s = m.groups()
    return int(h or 0) * 3600 + int(mn or 0) * 60 + int(s or 0)


def _pick_thumbnail(thumbs: dict) -> str:
    """YouTube returns thumbnails as {default, medium, high, standard, maxres} dict.
    We prefer medium (320x180) — sized right for grid display, always present."""
    if not thumbs:
        return ""
    for key in ("medium", "high", "default"):
        if key in thumbs and isinstance(thumbs[key], dict):
            return thumbs[key].get("url", "") or ""
    return ""


def _map_youtube_error(r: httpx.Response) -> HTTPException:
    """Translate YouTube error responses to actionable HTTPExceptions."""
    try:
        data = r.json() or {}
        err  = data.get("error", {})
        msg  = err.get("message", "Unknown error")
        reason = ""
        for e in err.get("errors", []) or []:
            if e.get("reason"):
                reason = e["reason"]
                break
    except Exception:
        msg, reason = r.text[:200] or "Unknown error", ""

    if r.status_code == 400:
        return HTTPException(400, f"YouTube rejected the query: {msg}")
    if r.status_code == 403:
        if reason in ("quotaExceeded", "dailyLimitExceeded", "rateLimitExceeded"):
            return HTTPException(429, "YouTube quota exhausted on Google's side. Resets at 00:00 PT.")
        if reason in ("keyInvalid", "ipRefererBlocked", "forbidden"):
            return HTTPException(401, f"YouTube API key rejected ({reason}). Re-test in The Kennel.")
        return HTTPException(403, f"YouTube refused the request: {msg}")
    if r.status_code == 503:
        return HTTPException(503, "YouTube temporarily unavailable. Try again in a moment.")
    return HTTPException(500, f"YouTube API error ({r.status_code}): {msg}")


async def _yt_search_ids(client: httpx.AsyncClient, key: str, query: str,
                         max_results: int, definition: str) -> List[str]:
    """search.list call → list of video IDs. Cost: 100 units regardless of result count."""
    params = {
        "key": key,
        "part": "id",
        "q": query,
        "type": "video",
        "videoCategoryId": "10",  # Music category
        "maxResults": min(max(max_results, 1), 50),
    }
    if definition == "high":
        params["videoDefinition"] = "high"
    r = await client.get(f"{_YT_BASE}/search", params=params)
    if r.status_code != 200:
        raise _map_youtube_error(r)
    data = r.json() or {}
    return [item["id"]["videoId"] for item in (data.get("items") or [])
            if item.get("id", {}).get("videoId")]


async def _yt_video_details(client: httpx.AsyncClient, key: str,
                            video_ids: List[str]) -> List[dict]:
    """videos.list call → full details for given IDs. Cost: 1 unit per ID."""
    if not video_ids:
        return []
    r = await client.get(f"{_YT_BASE}/videos", params={
        "key": key,
        "part": "snippet,contentDetails,statistics",
        "id": ",".join(video_ids),
        "maxResults": 50,
    })
    if r.status_code != 200:
        raise _map_youtube_error(r)
    data = r.json() or {}
    out = []
    for item in (data.get("items") or []):
        snip = item.get("snippet") or {}
        details = item.get("contentDetails") or {}
        stats = item.get("statistics") or {}
        out.append({
            "video_id":     item.get("id", ""),
            "title":        snip.get("title", "") or "",
            "channel":      snip.get("channelTitle", "") or "",
            "channel_id":   snip.get("channelId", "") or "",
            "duration_sec": _iso_duration_to_sec(details.get("duration", "")),
            "view_count":   int(stats.get("viewCount", 0) or 0),
            "published_at": snip.get("publishedAt", "") or "",
            "definition":   (details.get("definition", "") or "sd").lower(),
            "thumbnail":    _pick_thumbnail(snip.get("thumbnails")),
            "url":          f"https://www.youtube.com/watch?v={item.get('id', '')}",
        })
    return out


def _apply_local_filters(videos: List[dict], f: "ScoutFilters") -> List[dict]:
    """Post-fetch filters that YouTube can't enforce server-side (duration/channel)."""
    out = []
    inc = (f.channel_must_contain or "").lower().strip()
    exc = (f.channel_must_not_contain or "").lower().strip()
    for v in videos:
        if v["duration_sec"] < f.min_duration_seconds:
            continue
        if v["view_count"] < f.min_view_count:
            continue
        if f.definition == "high" and v.get("definition") != "hd":
            continue
        ch = (v.get("channel") or "").lower()
        if inc and inc not in ch:
            continue
        if exc and exc in ch:
            continue
        out.append(v)
    return out


async def _cache_lookup(cache_key: str) -> Optional[List[dict]]:
    """Return cached results if still fresh, else None."""
    try:
        async with aiosqlite.connect(_DYNAMIC_DB) as db:
            db.row_factory = aiosqlite.Row
            row = await (await db.execute(
                "SELECT results_json, cached_at FROM scout_cache WHERE cache_key = ?",
                (cache_key,)
            )).fetchone()
            if not row:
                return None
            age = int(time.time()) - int(row["cached_at"])
            if age > _CACHE_TTL_SEC:
                return None
            return json.loads(row["results_json"])
    except Exception as e:
        log.warning(f"_cache_lookup: {e}")
        return None


async def _cache_write(cache_key: str, query: str, filters_hash: str, results: List[dict]):
    try:
        async with aiosqlite.connect(_DYNAMIC_DB) as db:
            await db.execute(
                """INSERT INTO scout_cache (cache_key, query, filters_hash, results_json, cached_at)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(cache_key) DO UPDATE SET
                     results_json = excluded.results_json,
                     cached_at    = excluded.cached_at""",
                (cache_key, query, filters_hash, json.dumps(results), int(time.time()))
            )
            await db.commit()
    except Exception as e:
        log.warning(f"_cache_write: {e}")


# ─────────────────────────── ENDPOINTS ───────────────────────────

@router.post("/search")
async def scout_search(req: ScoutSearchRequest, _user=Depends(require_auth)):
    """Search YouTube for music videos matching query. Cache-first, quota-aware."""
    await _ensure_scout_tables()

    fhash = req.filters.hash()
    cache_key = hashlib.sha1(f"{req.query.strip().lower()}|{fhash}|n={req.max_results}".encode()).hexdigest()

    # Cache hit → return immediately, no quota burn
    cached = await _cache_lookup(cache_key)
    if cached is not None:
        return {
            "query":            req.query,
            "filters":          req.filters.dict(),
            "results":          cached,
            "from_cache":       True,
            "quota_used_today": await _get_quota_used(),
            "_cycle":           "2-live",
        }

    # Quota pre-check — search costs 100, details cost up to max_results
    needed = _COST_SEARCH + req.max_results * _COST_VIDEO
    await _check_quota_or_raise(needed)

    key = await _get_youtube_key()
    async with httpx.AsyncClient(timeout=_YT_TIMEOUT) as client:
        ids = await _yt_search_ids(client, key, req.query, req.max_results, req.filters.definition)
        await _add_quota(_COST_SEARCH)
        if not ids:
            await _cache_write(cache_key, req.query, fhash, [])
            return {
                "query":            req.query,
                "filters":          req.filters.dict(),
                "results":          [],
                "from_cache":       False,
                "quota_used_today": await _get_quota_used(),
                "_cycle":           "2-live",
            }
        details = await _yt_video_details(client, key, ids)
        await _add_quota(len(ids) * _COST_VIDEO)

    filtered = _apply_local_filters(details, req.filters)
    await _cache_write(cache_key, req.query, fhash, filtered)

    return {
        "query":            req.query,
        "filters":          req.filters.dict(),
        "results":          filtered,
        "from_cache":       False,
        "quota_used_today": await _get_quota_used(),
        "_cycle":           "2-live",
    }


@router.post("/search-batch")
async def scout_search_batch(req: ScoutSearchBatchRequest, _user=Depends(require_auth)):
    """Best match per {artist,title} pair. Each item costs ~101 units (100 search + 1 details).
    Cache-aware — repeated batch items hit cache, not API."""
    await _ensure_scout_tables()
    if not req.items:
        return {"matches": [], "filters": req.filters.dict(),
                "quota_used_today": await _get_quota_used(), "_cycle": "2-live"}

    fhash = req.filters.hash()

    # Pre-check the total potential burn (worst case, no cache hits)
    worst_case = len(req.items) * (_COST_SEARCH + _COST_VIDEO)
    await _check_quota_or_raise(worst_case)

    key = await _get_youtube_key()
    matches = []
    async with httpx.AsyncClient(timeout=_YT_TIMEOUT) as client:
        for idx, item in enumerate(req.items):
            q = f"{item.artist} {item.title}".strip()
            cache_key = hashlib.sha1(f"batch|{q.lower()}|{fhash}".encode()).hexdigest()

            cached = await _cache_lookup(cache_key)
            if cached is not None:
                top = cached[0] if cached else None
                matches.append({"artist": item.artist, "title": item.title, "query": q,
                                "match": top, "from_cache": True})
                continue

            try:
                ids = await _yt_search_ids(client, key, q, 1, req.filters.definition)
                await _add_quota(_COST_SEARCH)
                if not ids:
                    await _cache_write(cache_key, q, fhash, [])
                    matches.append({"artist": item.artist, "title": item.title, "query": q,
                                    "match": None, "from_cache": False})
                    continue
                details = await _yt_video_details(client, key, ids)
                await _add_quota(len(ids) * _COST_VIDEO)
                filtered = _apply_local_filters(details, req.filters)
                await _cache_write(cache_key, q, fhash, filtered)
                top = filtered[0] if filtered else None
                matches.append({"artist": item.artist, "title": item.title, "query": q,
                                "match": top, "from_cache": False})
            except HTTPException as he:
                # Hard stop on quota/auth errors so we don't keep burning
                if he.status_code in (401, 429):
                    raise
                # Soft fail per-item for transient errors — record null match, continue
                log.warning(f"batch item failed: {q}: {he.detail}")
                matches.append({"artist": item.artist, "title": item.title, "query": q,
                                "match": None, "error": he.detail, "from_cache": False})

            if idx < len(req.items) - 1:
                await asyncio.sleep(_BATCH_DELAY_SEC)

    return {
        "matches":          matches,
        "filters":          req.filters.dict(),
        "quota_used_today": await _get_quota_used(),
        "_cycle":           "2-live",
    }


@router.post("/build-playlist")
async def scout_build_playlist(req: ScoutBuildPlaylistRequest, _user=Depends(require_auth)):
    """Convert list of video_ids to a YouTube deeplink + plain/markdown exports.
    Note: deeplink uses youtube.com/watch_videos?video_ids=... which YouTube
    interprets as a temporary playlist; user clicks Save in YouTube to commit.
    Hard cap at 50 IDs (YouTube's deeplink limit)."""
    if len(req.video_ids) > 50:
        raise HTTPException(400, "YouTube deeplinks support max 50 videos. Split into multiple playlists.")

    deeplink = "https://www.youtube.com/watch_videos?video_ids=" + ",".join(req.video_ids)

    plain_text = "\n".join(f"https://www.youtube.com/watch?v={vid}" for vid in req.video_ids)

    markdown = f"# {req.title}\n\n" + "\n".join(
        f"- [Video {i+1}](https://www.youtube.com/watch?v={vid})"
        for i, vid in enumerate(req.video_ids)
    )

    return {
        "title":      req.title,
        "video_ids":  req.video_ids,
        "deeplink":   deeplink,
        "plain_text": plain_text,
        "markdown":   markdown,
        "count":      len(req.video_ids),
    }


@router.get("/quota-status")
async def scout_quota_status(_user=Depends(require_auth)):
    """Report today's local quota tally. We track our own count — Google does not expose it."""
    await _ensure_scout_tables()
    used = await _get_quota_used()
    return {
        "date_utc":      _today_utc_str(),
        "units_used":    used,
        "units_cap":     _QUOTA_DAILY,
        "units_safety":  _QUOTA_SAFETY,
        "remaining":     max(0, (_QUOTA_DAILY - _QUOTA_SAFETY) - used),
        "percent_used":  round(100 * used / _QUOTA_DAILY, 1),
    }
