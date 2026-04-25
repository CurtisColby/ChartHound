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
from datetime import datetime, timezone
from typing import List, Optional

import aiosqlite
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.config import get_settings
from app.deps import require_auth

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


def _mock_video(idx: int, query: str) -> dict:
    """Mock YouTube video object. Cycle 2 replaces with real API parsing."""
    return {
        "video_id":     f"mock_{idx:04d}",
        "title":        f"{query} — Mock Result {idx}",
        "channel":      "Mock VEVO" if idx % 3 == 0 else "Mock Channel",
        "channel_id":   f"UC_mock_{idx}",
        "duration_sec": 200 + idx * 7,
        "view_count":   1_000_000 - idx * 50_000,
        "published_at": "2024-01-01T00:00:00Z",
        "definition":   "hd",
        "thumbnail":    f"https://i.ytimg.com/vi/mock_{idx:04d}/mqdefault.jpg",
        "url":          f"https://www.youtube.com/watch?v=mock_{idx:04d}",
    }


# ─────────────────────────── ENDPOINTS ───────────────────────────

@router.post("/search")
async def scout_search(req: ScoutSearchRequest, _user=Depends(require_auth)):
    """Search YouTube for music videos matching query. CYCLE 1B: returns mock data."""
    await _ensure_scout_tables()

    # Cycle 2 will: check cache → call YouTube search.list → call videos.list for details
    # → apply post-fetch filters (min_duration, channel filters) → cache results → return.
    mock_results = [_mock_video(i + 1, req.query) for i in range(req.max_results)]

    # Apply local filters to mock data so the contract is testable now
    filtered = [
        v for v in mock_results
        if v["duration_sec"] >= req.filters.min_duration_seconds
        and v["view_count"] >= req.filters.min_view_count
        and (req.filters.definition == "any" or v["definition"] == req.filters.definition)
        and (not req.filters.channel_must_contain
             or req.filters.channel_must_contain.lower() in v["channel"].lower())
        and (not req.filters.channel_must_not_contain
             or req.filters.channel_must_not_contain.lower() not in v["channel"].lower())
    ]

    return {
        "query":     req.query,
        "filters":   req.filters.dict(),
        "results":   filtered,
        "from_cache": False,
        "quota_used_today": await _get_quota_used(),
        "_cycle":    "1B-mock",
    }


@router.post("/search-batch")
async def scout_search_batch(req: ScoutSearchBatchRequest, _user=Depends(require_auth)):
    """Best match per {artist,title} pair. CYCLE 1B: returns mock data."""
    await _ensure_scout_tables()

    matches = []
    for item in req.items:
        q = f"{item.artist} {item.title}"
        matches.append({
            "artist":  item.artist,
            "title":   item.title,
            "query":   q,
            "match":   _mock_video(1, q),
        })

    return {
        "matches":  matches,
        "filters":  req.filters.dict(),
        "quota_used_today": await _get_quota_used(),
        "_cycle":   "1B-mock",
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
