"""
ChartHound — Kennel Router (The Vault)
All API connection management: save, test, status, disconnect, path translation.
Tokens ALWAYS encrypted before SQLite. NEVER returned to frontend.
"""

import json
import logging
import aiosqlite
from datetime import datetime, timezone

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from typing import Optional

from app.config import get_settings
from app.deps import require_auth
from app.security import encrypt_token, decrypt_token

log = logging.getLogger("charthound.kennel")
router = APIRouter(prefix="/api/kennel", tags=["kennel"])
settings = get_settings()

VALID_SERVICES = {
    "plex", "emby", "jellyfin", "lastfm", "prowlarr",
    "radarr", "sonarr", "qbittorrent", "deluge", "transmission"
}


class ConnectionSaveRequest(BaseModel):
    service: str
    base_url: Optional[str] = None
    token: Optional[str] = None
    username: Optional[str] = None   # for download clients
    extra_json: Optional[dict] = None


class PathSaveRequest(BaseModel):
    server_prefix: str
    docker_prefix: str = "/music"


class PathTranslateRequest(BaseModel):
    server_path: str


# ── SAVE ──────────────────────────────────────────────────────────────────────

@router.post("/save")
async def save_connection(req: ConnectionSaveRequest, user: dict = Depends(require_auth)):
    if req.service not in VALID_SERVICES:
        raise HTTPException(400, f"Unknown service '{req.service}'")

    token_enc = encrypt_token(req.token or "")
    extra = json.dumps({**(req.extra_json or {}), "username": req.username or ""})
    now = datetime.now(timezone.utc).isoformat()

    async with aiosqlite.connect(settings.database_url) as db:
        await db.execute(
            """INSERT INTO connections (service, base_url, token_enc, extra_json, updated_at)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(service) DO UPDATE SET
                   base_url=excluded.base_url, token_enc=excluded.token_enc,
                   extra_json=excluded.extra_json, updated_at=excluded.updated_at""",
            (req.service, req.base_url, token_enc, extra, now),
        )
        await db.commit()

    log.info(f"Connection saved: {req.service} by {user['sub']}")
    return {"ok": True, "service": req.service, "message": "Connection saved (token encrypted)."}


# ── DISCONNECT ────────────────────────────────────────────────────────────────

@router.delete("/disconnect/{service}")
async def disconnect(service: str, user: dict = Depends(require_auth)):
    """Remove a saved connection entirely from the database."""
    if service not in VALID_SERVICES:
        raise HTTPException(400, f"Unknown service '{service}'")

    async with aiosqlite.connect(settings.database_url) as db:
        await db.execute("DELETE FROM connections WHERE service = ?", (service,))
        await db.commit()

    log.info(f"Connection disconnected: {service} by {user['sub']}")
    return {"ok": True, "service": service, "message": f"{service} disconnected and credentials removed."}


# ── STATUS ────────────────────────────────────────────────────────────────────

@router.get("/status")
async def get_all_statuses(user: dict = Depends(require_auth)):
    async with aiosqlite.connect(settings.database_url) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT service, base_url, token_enc, verified_at FROM connections"
        )
        rows = await cursor.fetchall()

    result = {svc: {"service": svc, "connected": False, "has_token": False,
                    "base_url": None, "verified_at": None}
              for svc in VALID_SERVICES}

    for row in rows:
        svc = row["service"]
        if svc in result:
            result[svc]["base_url"] = row["base_url"]
            result[svc]["has_token"] = bool(row["token_enc"])
            result[svc]["verified_at"] = row["verified_at"]
            result[svc]["connected"] = bool(row["verified_at"])

    return list(result.values())


# ── TEST ──────────────────────────────────────────────────────────────────────

@router.post("/test/{service}")
async def test_connection(service: str, user: dict = Depends(require_auth)):
    if service not in VALID_SERVICES:
        raise HTTPException(400, f"Unknown service '{service}'")

    async with aiosqlite.connect(settings.database_url) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT base_url, token_enc, extra_json FROM connections WHERE service = ?", (service,)
        )
        row = await cursor.fetchone()

    if not row:
        raise HTTPException(404, f"No saved connection for '{service}'. Save it first.")

    base_url = row["base_url"] or ""
    token = decrypt_token(row["token_enc"] or "")
    extra = json.loads(row["extra_json"] or "{}")

    try:
        detail = await _run_test(service, base_url, token, extra)
    except Exception as e:
        log.warning(f"Test failed for {service}: {e}")
        async with aiosqlite.connect(settings.database_url) as db:
            await db.execute(
                "UPDATE connections SET verified_at = NULL WHERE service = ?", (service,)
            )
            await db.commit()
        return {"ok": False, "service": service, "error": str(e)}

    now = datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(settings.database_url) as db:
        await db.execute(
            "UPDATE connections SET verified_at = ? WHERE service = ?", (now, service)
        )
        await db.commit()

    log.info(f"Connection verified: {service} by {user['sub']}")
    return {"ok": True, "service": service, "detail": detail, "verified_at": now}


async def _run_test(service: str, base_url: str, token: str, extra: dict = {}) -> str:
    timeout = httpx.Timeout(10.0)

    if service == "plex":
        if not base_url:
            raise ValueError("No Plex URL configured.")
        async with httpx.AsyncClient(timeout=timeout, verify=False) as client:
            r = await client.get(f"{base_url.rstrip('/')}/identity",
                                 params={"X-Plex-Token": token},
                                 headers={"Accept": "application/json"})
        if r.status_code == 401:
            raise ValueError("Plex token rejected (401). Check your token.")
        r.raise_for_status()
        friendly = r.json().get("MediaContainer", {}).get("friendlyName", "Plex Server")
        return f"Connected to '{friendly}'"

    elif service in ("emby", "jellyfin"):
        if not base_url:
            raise ValueError(f"No {service.title()} URL configured.")
        async with httpx.AsyncClient(timeout=timeout, verify=False) as client:
            r = await client.get(f"{base_url.rstrip('/')}/System/Info",
                                 headers={"X-Emby-Token": token, "X-MediaBrowser-Token": token})
        if r.status_code == 401:
            raise ValueError(f"{service.title()} API key rejected (401).")
        r.raise_for_status()
        data = r.json()
        return f"Connected to '{data.get('ServerName', service.title())}' v{data.get('Version','?')}"

    elif service == "lastfm":
        if not token:
            raise ValueError("No Last.fm API key configured.")
        async with httpx.AsyncClient(timeout=timeout) as client:
            r = await client.get("https://ws.audioscrobbler.com/2.0/",
                                 params={"method": "chart.getTopTracks",
                                         "api_key": token, "format": "json", "limit": "1"})
        data = r.json()
        if "error" in data:
            raise ValueError(f"Last.fm error {data['error']}: {data.get('message','')}")
        return "Last.fm API key valid"

    elif service == "prowlarr":
        if not base_url:
            raise ValueError("No Prowlarr URL configured.")
        async with httpx.AsyncClient(timeout=timeout, verify=False) as client:
            r = await client.get(f"{base_url.rstrip('/')}/api/v1/indexer",
                                 headers={"X-Api-Key": token})
        if r.status_code == 401:
            raise ValueError("Prowlarr API key rejected (401).")
        r.raise_for_status()
        count = len(r.json()) if isinstance(r.json(), list) else "?"
        return f"Prowlarr connected — {count} indexer(s) found"

    elif service == "radarr":
        if not base_url:
            raise ValueError("No Radarr URL configured.")
        async with httpx.AsyncClient(timeout=timeout, verify=False) as client:
            r = await client.get(f"{base_url.rstrip('/')}/api/v3/system/status",
                                 headers={"X-Api-Key": token})
        if r.status_code == 401:
            raise ValueError("Radarr API key rejected (401).")
        r.raise_for_status()
        data = r.json()
        return f"Radarr v{data.get('version','?')} connected"

    elif service == "sonarr":
        if not base_url:
            raise ValueError("No Sonarr URL configured.")
        async with httpx.AsyncClient(timeout=timeout, verify=False) as client:
            r = await client.get(f"{base_url.rstrip('/')}/api/v3/system/status",
                                 headers={"X-Api-Key": token})
        if r.status_code == 401:
            raise ValueError("Sonarr API key rejected (401).")
        r.raise_for_status()
        data = r.json()
        return f"Sonarr v{data.get('version','?')} connected"

    elif service == "qbittorrent":
        if not base_url:
            raise ValueError("No qBittorrent URL configured.")
        username = extra.get("username", "admin")
        async with httpx.AsyncClient(timeout=timeout, verify=False) as client:
            r = await client.post(f"{base_url.rstrip('/')}/api/v2/auth/login",
                                  data={"username": username, "password": token})
        if r.text == "Fails.":
            raise ValueError("qBittorrent login failed. Check username and password.")
        return f"qBittorrent connected"

    elif service in ("deluge", "transmission"):
        # Basic reachability check for now
        if not base_url:
            raise ValueError(f"No {service.title()} URL configured.")
        async with httpx.AsyncClient(timeout=timeout, verify=False) as client:
            r = await client.get(base_url.rstrip('/'))
        if r.status_code < 500:
            return f"{service.title()} reachable"
        raise ValueError(f"{service.title()} returned error {r.status_code}")

    raise ValueError(f"No test defined for '{service}'")


# ── PATH TRANSLATION ──────────────────────────────────────────────────────────

@router.post("/save-path")
async def save_path(req: PathSaveRequest, user: dict = Depends(require_auth)):
    now = datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(settings.database_url) as db:
        await db.executemany(
            "INSERT INTO app_settings (key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            [("path_server_prefix", req.server_prefix, now),
             ("path_docker_prefix", req.docker_prefix, now)],
        )
        await db.commit()
    return {"ok": True, "server_prefix": req.server_prefix, "docker_prefix": req.docker_prefix}


@router.post("/path-translate")
async def path_translate(req: PathTranslateRequest, user: dict = Depends(require_auth)):
    async with aiosqlite.connect(settings.database_url) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT key, value FROM app_settings WHERE key IN ('path_server_prefix','path_docker_prefix')"
        )
        rows = await cursor.fetchall()

    settings_map = {r["key"]: r["value"] for r in rows}
    server_prefix = settings_map.get("path_server_prefix", "")
    docker_prefix = settings_map.get("path_docker_prefix", "/music")

    path = req.server_path
    if server_prefix and path.startswith(server_prefix):
        path = docker_prefix + path[len(server_prefix):]

    return {"original": req.server_path, "translated": path,
            "server_prefix": server_prefix, "docker_prefix": docker_prefix}


@router.get("/path-settings")
async def get_path_settings(user: dict = Depends(require_auth)):
    async with aiosqlite.connect(settings.database_url) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT key, value FROM app_settings WHERE key IN ('path_server_prefix','path_docker_prefix')"
        )
        rows = await cursor.fetchall()
    result = {r["key"]: r["value"] for r in rows}
    return {"server_prefix": result.get("path_server_prefix", ""),
            "docker_prefix": result.get("path_docker_prefix", "/music")}
