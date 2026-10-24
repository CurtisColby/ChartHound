# © 2026 Colby R. Curtis | ChartHound: The New World
# All Rights Reserved.
"""
ChartHound — Start Here
GET /api/start/status — one read-only snapshot of where the user is in the flow.
"""
import json
import logging

import aiosqlite
from fastapi import APIRouter, Depends

from app.config import get_settings
from app.deps import require_auth

log = logging.getLogger("charthound.start")
router = APIRouter(prefix="/api/start", tags=["start"])
settings = get_settings()
_DB = getattr(settings, "database_url", "/data/charthound.db")

_MEDIA = ("plex", "emby", "jellyfin")


async def _one(db, sql, args=(), default=None):
    try:
        row = await (await db.execute(sql, args)).fetchone()
        return row[0] if row and row[0] is not None else default
    except Exception:
        return default


async def _setting(db, key, default=""):
    return await _one(db, "SELECT value FROM app_settings WHERE key=?", (key,), default)


@router.get("/status")
async def start_status(_=Depends(require_auth)):
    out: dict = {}
    async with aiosqlite.connect(_DB) as db:
        # ① Connections
        verified = []
        try:
            async for svc, ok in await db.execute(
                    "SELECT service, verified_at FROM connections WHERE base_url IS NOT NULL OR token_enc IS NOT NULL"):
                if ok:
                    verified.append(svc)
        except Exception:
            pass
        out["connect"] = {"verified": verified,
                          "media": [s for s in _MEDIA if s in verified]}

        # ② Library scan
        out["scan"] = {
            "tracks":  await _one(db, "SELECT COUNT(*) FROM tracks WHERE COALESCE(missing,0)=0", default=0),
            "charted": await _one(db, "SELECT COUNT(*) FROM hit_scores", default=0),
            "newest":  await _one(db, "SELECT MAX(last_scanned) FROM tracks", default=""),
        }

        # ③ Genres — songs with no Genre Engine decision yet (Skip folders excluded)
        skip = []
        try:
            raw = await _setting(db, "ge_folder_rules", "")
            from app.routers.retriever import _ge_norm_prefix
            skip = [_ge_norm_prefix(r.get("prefix", "")) for r in json.loads(raw or "[]")
                    if r.get("family") == "skip" and r.get("prefix")]
        except Exception:
            pass
        where = " AND ".join(["t.file_path NOT LIKE ?"] * len(skip))
        pending = await _one(db,
            "SELECT COUNT(*) FROM tracks t LEFT JOIN track_genres g ON g.track_id=t.track_id "
            "WHERE g.track_id IS NULL AND COALESCE(t.missing,0)=0" + (" AND " + where if where else ""),
            tuple(p.rstrip("/") + "/%" for p in skip), default=0)
        out["genres"] = {
            "decided": await _one(db, "SELECT COUNT(*) FROM track_genres", default=0),
            "last_run": await _one(db, "SELECT MAX(updated_at) FROM track_genres WHERE confidence!='manual'", default=""),
            "pending": pending,
            "conflicts": await _one(db, "SELECT COUNT(*) FROM track_genres WHERE confidence='conflict'", default=0),
        }

        # ④ Playlists
        try:
            last_push = json.loads(await _setting(db, "grm_last_push", "") or "null")
        except Exception:
            last_push = None
        out["playlists"] = {"last": last_push}

        # ⑤ Fill gaps (readiness only)
        out["gaps"] = {
            "prowlarr": "prowlarr" in verified,
            "downloader": any(s in verified for s in ("qbittorrent", "deluge", "transmission", "sabnzbd")),
            "path": await _setting(db, "sniffer_download_path", ""),
        }

        # ⑥ Tracker
        out["tracker"] = {
            "enabled": (await _setting(db, "tracker_enabled", "false")) == "true",
            "missing": await _one(db, "SELECT COUNT(*) FROM tracker_items WHERE status='missing'", default=0),
            "last_grab": await _one(db, "SELECT MAX(created_at) FROM tracker_log WHERE action='found'", default=""),
            "radarr": "radarr" in verified, "sonarr": "sonarr" in verified,
        }
    return out
