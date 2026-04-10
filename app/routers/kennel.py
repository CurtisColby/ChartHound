"""
ChartHound — Kennel Router (The Vault)
All API connection management: save, test, status, disconnect, path translation.

BUG FIXES in M4:
- SAVE never overwrites token if field is blank (BUG-001)
- SAVE never resets URL to default (BUG-002)
- qBittorrent test checks for "Ok." strictly (BUG-006)
- TEST auto-retries on 503 (BUG-010)
- YouTube API connection added
"""

import json
import logging
import asyncio
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
    "radarr", "sonarr", "qbittorrent", "deluge", "transmission",
    "youtube", "discogs"
}

# Default URLs per service
DEFAULT_URLS = {
    "plex":         "http://YOUR-SERVER-IP:32400",
    "emby":         "http://YOUR-SERVER-IP:8096",
    "jellyfin":     "http://YOUR-SERVER-IP:8096",
    "prowlarr":     "http://YOUR-SERVER-IP:9696",
    "radarr":       "http://YOUR-SERVER-IP:7878",
    "sonarr":       "http://YOUR-SERVER-IP:8989",
    "qbittorrent":  "http://YOUR-SERVER-IP:8080",
    "deluge":       "http://YOUR-SERVER-IP:8112",
    "transmission": "http://YOUR-SERVER-IP:9091",
    "lastfm":       None,
    "youtube":      None,
    "discogs":      None,
}


class ConnectionSaveRequest(BaseModel):
    service: str
    base_url: Optional[str] = None
    token: Optional[str] = None
    username: Optional[str] = None
    extra_json: Optional[dict] = None


class PathSaveRequest(BaseModel):
    server_prefix: str
    docker_prefix: str = "/music"


class PathTranslateRequest(BaseModel):
    server_path: str


# ── SAVE (BUG-001 + BUG-002 fixed) ───────────────────────────────────────────

@router.post("/save")
async def save_connection(req: ConnectionSaveRequest, user: dict = Depends(require_auth)):
    if req.service not in VALID_SERVICES:
        raise HTTPException(400, f"Unknown service '{req.service}'")

    now = datetime.now(timezone.utc).isoformat()

    async with aiosqlite.connect(settings.database_url) as db:
        db.row_factory = aiosqlite.Row

        # Get existing record if any
        cursor = await db.execute(
            "SELECT base_url, token_enc, extra_json FROM connections WHERE service = ?",
            (req.service,)
        )
        existing = await cursor.fetchone()

        # BUG-001 FIX: Only update token if a new non-empty one is provided
        if req.token and req.token.strip():
            token_enc = encrypt_token(req.token.strip())
        elif existing:
            token_enc = existing["token_enc"]  # Keep existing encrypted token
        else:
            token_enc = ""

        # BUG-002 FIX: Only update URL if a new non-empty one is provided
        # and it's not the placeholder text
        placeholder_texts = ["YOUR-SERVER-IP", "CHANGE_ME", ""]
        new_url = req.base_url.strip() if req.base_url else ""
        is_placeholder = any(p in new_url for p in placeholder_texts)

        if new_url and not is_placeholder:
            base_url = new_url
        elif existing and existing["base_url"]:
            base_url = existing["base_url"]  # Keep existing URL
        else:
            base_url = new_url or None

        # Merge extra_json
        existing_extra = json.loads(existing["extra_json"] or "{}") if existing else {}
        new_extra = req.extra_json or {}
        if req.username and req.username.strip():
            new_extra["username"] = req.username.strip()
        merged_extra = {**existing_extra, **new_extra}
        extra = json.dumps(merged_extra) if merged_extra else None

        await db.execute(
            """INSERT INTO connections (service, base_url, token_enc, extra_json, updated_at)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(service) DO UPDATE SET
                   base_url=excluded.base_url,
                   token_enc=excluded.token_enc,
                   extra_json=excluded.extra_json,
                   updated_at=excluded.updated_at""",
            (req.service, base_url, token_enc, extra, now),
        )
        await db.commit()

    log.info(f"Connection saved: {req.service} by {user['sub']}")
    return {"ok": True, "service": req.service, "message": "Connection saved (token encrypted)."}


# ── DISCONNECT ────────────────────────────────────────────────────────────────

@router.delete("/disconnect/{service}")
async def disconnect(service: str, user: dict = Depends(require_auth)):
    if service not in VALID_SERVICES:
        raise HTTPException(400, f"Unknown service '{service}'")

    async with aiosqlite.connect(settings.database_url) as db:
        await db.execute("DELETE FROM connections WHERE service = ?", (service,))
        await db.commit()

    log.info(f"Connection disconnected: {service} by {user['sub']}")
    return {
        "ok": True,
        "service": service,
        "default_url": DEFAULT_URLS.get(service),
        "message": f"{service} disconnected and credentials removed."
    }


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


# ── TEST (BUG-010: auto-retry on 503) ────────────────────────────────────────

@router.post("/test/{service}")
async def test_connection(service: str, user: dict = Depends(require_auth)):
    if service not in VALID_SERVICES:
        raise HTTPException(400, f"Unknown service '{service}'")

    async with aiosqlite.connect(settings.database_url) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT base_url, token_enc, extra_json FROM connections WHERE service = ?",
            (service,)
        )
        row = await cursor.fetchone()

    if not row:
        raise HTTPException(404, f"No saved connection for '{service}'. Save it first.")

    base_url = row["base_url"] or ""
    token = decrypt_token(row["token_enc"] or "")
    extra = json.loads(row["extra_json"] or "{}")

    # BUG-010 FIX: Auto-retry once on 503 after 5 seconds
    last_error = None
    for attempt in range(2):
        try:
            detail = await _run_test(service, base_url, token, extra)
            # Success — update verified_at
            now = datetime.now(timezone.utc).isoformat()
            async with aiosqlite.connect(settings.database_url) as db:
                await db.execute(
                    "UPDATE connections SET verified_at = ? WHERE service = ?", (now, service)
                )
                await db.commit()
            log.info(f"Connection verified: {service} by {user['sub']}")
            return {"ok": True, "service": service, "detail": detail, "verified_at": now}
        except Exception as e:
            last_error = str(e)
            if "503" in str(e) and attempt == 0:
                log.info(f"503 on {service} — retrying in 5 seconds...")
                await asyncio.sleep(5)
                continue
            break

    # All attempts failed — clear verified_at
    async with aiosqlite.connect(settings.database_url) as db:
        await db.execute(
            "UPDATE connections SET verified_at = NULL WHERE service = ?", (service,)
        )
        await db.commit()
    log.warning(f"Test failed for {service}: {last_error}")
    return {"ok": False, "service": service, "error": last_error}


async def _run_test(service: str, base_url: str, token: str, extra: dict = {}) -> str:
    timeout = httpx.Timeout(12.0)

    if service == "plex":
        if not base_url:
            raise ValueError("No Plex URL configured.")
        async with httpx.AsyncClient(timeout=timeout, verify=False) as client:
            r = await client.get(f"{base_url.rstrip('/')}/identity",
                                 params={"X-Plex-Token": token},
                                 headers={"Accept": "application/json"})
        if r.status_code == 401:
            raise ValueError("Plex token rejected (401). Check your token.")
        if r.status_code == 503:
            raise ValueError("503 Service Unavailable — Plex may be starting up.")
        r.raise_for_status()
        friendly = r.json().get("MediaContainer", {}).get("friendlyName", "Plex Server")
        return f"Connected to '{friendly}'"

    elif service in ("emby", "jellyfin"):
        if not base_url:
            raise ValueError(f"No {service.title()} URL configured.")
        async with httpx.AsyncClient(timeout=timeout, verify=False) as client:
            r = await client.get(f"{base_url.rstrip('/')}/System/Info",
                                 headers={"X-Emby-Token": token,
                                          "X-MediaBrowser-Token": token})
        if r.status_code == 401:
            raise ValueError(f"{service.title()} API key rejected (401). Check your API key.")
        if r.status_code == 503:
            raise ValueError(f"503 Service Unavailable — {service.title()} may be starting up.")
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
        if r.status_code == 503:
            raise ValueError("503 — Prowlarr may be starting up.")
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
        return f"Radarr v{r.json().get('version','?')} connected"

    elif service == "sonarr":
        if not base_url:
            raise ValueError("No Sonarr URL configured.")
        async with httpx.AsyncClient(timeout=timeout, verify=False) as client:
            r = await client.get(f"{base_url.rstrip('/')}/api/v3/system/status",
                                 headers={"X-Api-Key": token})
        if r.status_code == 401:
            raise ValueError("Sonarr API key rejected (401).")
        r.raise_for_status()
        return f"Sonarr v{r.json().get('version','?')} connected"

    elif service == "qbittorrent":
        if not base_url:
            raise ValueError("No qBittorrent URL configured.")
        username = extra.get("username", "admin")
        async with httpx.AsyncClient(timeout=timeout, verify=False) as client:
            r = await client.post(f"{base_url.rstrip('/')}/api/v2/auth/login",
                                  data={"username": username, "password": token})
        # BUG-006 FIX: Check for exact "Ok." response
        if r.text.strip() == "Ok.":
            return "qBittorrent connected"
        elif r.text.strip() == "Fails.":
            raise ValueError("qBittorrent login failed. Check username and password.")
        else:
            raise ValueError(f"Unexpected qBittorrent response: {r.text[:100]}")

    elif service == "deluge":
        if not base_url:
            raise ValueError("No Deluge URL configured.")
        async with httpx.AsyncClient(timeout=timeout, verify=False) as client:
            r = await client.get(base_url.rstrip('/'))
        if r.status_code < 500:
            return "Deluge reachable"
        raise ValueError(f"Deluge returned error {r.status_code}")

    elif service == "transmission":
        if not base_url:
            raise ValueError("No Transmission URL configured.")
        async with httpx.AsyncClient(timeout=timeout, verify=False) as client:
            r = await client.get(f"{base_url.rstrip('/')}/transmission/rpc")
        if r.status_code in (200, 409):
            return "Transmission reachable"
        raise ValueError(f"Transmission returned error {r.status_code}")

    elif service == "youtube":
        if not token:
            raise ValueError("No YouTube API key configured.")
        async with httpx.AsyncClient(timeout=timeout) as client:
            r = await client.get(
                "https://www.googleapis.com/youtube/v3/channels",
                params={"part": "id", "mine": "true", "key": token}
            )
        if r.status_code == 400:
            data = r.json()
            err = data.get("error", {}).get("message", "Invalid API key")
            raise ValueError(f"YouTube API error: {err}")
        if r.status_code == 403:
            raise ValueError("YouTube API key rejected (403). Check key and enabled APIs.")
        return "YouTube API key valid"

    elif service == "discogs":
        if not token:
            raise ValueError("No Discogs token configured.")
        async with httpx.AsyncClient(timeout=timeout) as client:
            r = await client.get(
                "https://api.discogs.com/oauth/identity",
                headers={
                    "User-Agent": "ChartHound/1.0 +https://github.com/CurtisColby/ChartHound",
                    "Authorization": f"Discogs token={token}"
                }
            )
        if r.status_code == 401:
            raise ValueError("Discogs token rejected (401). Check your token.")
        if r.status_code == 200:
            data = r.json()
            username = data.get("username", "unknown")
            return f"Discogs connected as '{username}'"
        raise ValueError(f"Discogs returned status {r.status_code}")

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
            "SELECT key, value FROM app_settings "
            "WHERE key IN ('path_server_prefix','path_docker_prefix')"
        )
        rows = await cursor.fetchall()

    m = {r["key"]: r["value"] for r in rows}
    server_prefix = m.get("path_server_prefix", "")
    docker_prefix = m.get("path_docker_prefix", "/music")
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
            "SELECT key, value FROM app_settings "
            "WHERE key IN ('path_server_prefix','path_docker_prefix')"
        )
        rows = await cursor.fetchall()
    result = {r["key"]: r["value"] for r in rows}
    return {"server_prefix": result.get("path_server_prefix", ""),
            "docker_prefix": result.get("path_docker_prefix", "/music")}


# ── SECRET KEY STATUS ─────────────────────────────────────────────────────────

@router.get("/secret-key-status")
async def secret_key_status():
    """Check if SECRET_KEY is still the placeholder value."""
    is_placeholder = settings.secret_key == "CHANGE_ME_GENERATE_A_STRONG_RANDOM_KEY"
    return {"placeholder": is_placeholder}
