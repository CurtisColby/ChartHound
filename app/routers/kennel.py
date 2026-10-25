"""
ChartHound — Kennel Router (The Vault)
All API connection management: save, test, status, disconnect, path translation.

BUG FIXES in M4:
- SAVE never overwrites token if field is blank (BUG-001)
- SAVE never resets URL to default (BUG-002)
- qBittorrent test checks for "Ok." strictly (BUG-006)
- TEST auto-retries on 503 (BUG-010)
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
    "discogs", "sabnzbd", "nzbget", "acoustid"
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
    "discogs":      None,
    "acoustid":     None,
}


class ConnectionSaveRequest(BaseModel):
    service: str
    base_url: Optional[str] = None
    token: Optional[str] = None
    username: Optional[str] = None
    extra_json: Optional[dict] = None


class PathSaveRequest(BaseModel):
    server_prefix:     str
    docker_prefix:     str = "/music"
    navidrome_prefix:  str = ""


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


async def _run_test(service: str, base_url: str, token: str, extra: dict | None = None) -> str:
    extra = extra or {}
    timeout = httpx.Timeout(12.0)

    if service == "plex":
        if not base_url:
            raise ValueError("No Plex URL configured.")
        async with httpx.AsyncClient(timeout=timeout, verify=False) as client:
            # /library/sections requires a valid token (/identity does not)
            r = await client.get(f"{base_url.rstrip('/')}/library/sections",
                                 params={"X-Plex-Token": token},
                                 headers={"Accept": "application/json"})
        if r.status_code == 401:
            raise ValueError("Plex token rejected (401). Check your token.")
        if r.status_code == 503:
            raise ValueError("503 Service Unavailable — Plex may be starting up.")
        r.raise_for_status()
        mc = r.json().get("MediaContainer", {})
        music = sum(1 for d in mc.get("Directory", []) if d.get("type") == "artist")
        friendly = mc.get("title1") or "Plex Server"
        return f"Connected to '{friendly}' — token valid, {music} music library(s)"

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
        # 204 = whitelisted IP bypass (no body); 200+empty = same via some configs
        body = r.text.strip()
        if body == "Ok." or r.status_code == 204 or (r.status_code == 200 and body == ""):
            return "qBittorrent connected"
        elif body == "Fails.":
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

    elif service == "acoustid":
        if not token:
            raise ValueError("No AcoustID API key configured.")
        async with httpx.AsyncClient(timeout=timeout) as client:
            r = await client.post("https://api.acoustid.org/v2/lookup", data={
                "client": token, "duration": 200, "fingerprint": "AQAAAA", "format": "json"})
        err = (r.json() if r.content else {}).get("error") or {}
        if err.get("code") == 4 or "api key" in str(err.get("message", "")).lower():
            raise ValueError("AcoustID key rejected. Use the APPLICATION key from acoustid.org → Your applications.")
        if r.status_code >= 500:
            raise ValueError(f"AcoustID returned {r.status_code} — try again later.")
        return "AcoustID application key valid"

    raise ValueError(f"No test defined for '{service}'")


# ── PATH TRANSLATION ──────────────────────────────────────────────────────────

@router.post("/save-path")
async def save_path(req: PathSaveRequest, user: dict = Depends(require_auth)):
    now = datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(settings.database_url) as db:
        await db.executemany(
            "INSERT INTO app_settings (key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            [("path_server_prefix",    req.server_prefix,    now),
             ("path_docker_prefix",    req.docker_prefix,    now),
             ("path_navidrome_prefix", req.navidrome_prefix, now)],
        )
        await db.commit()
    return {"ok": True, "server_prefix": req.server_prefix,
            "docker_prefix": req.docker_prefix,
            "navidrome_prefix": req.navidrome_prefix}


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
            "WHERE key IN ('path_server_prefix','path_docker_prefix','path_navidrome_prefix')"
        )
        rows = await cursor.fetchall()
    result = {r["key"]: r["value"] for r in rows}
    return {"server_prefix":    result.get("path_server_prefix",    ""),
            "docker_prefix":    result.get("path_docker_prefix",    "/music"),
            "navidrome_prefix": result.get("path_navidrome_prefix", "")}


# ── SECRET KEY STATUS ─────────────────────────────────────────────────────────

# ── SNIFFER DOWNLOAD PATH ────────────────────────────────────────────────────

class SnifferPathRequest(BaseModel):
    download_path: str


@router.post("/save-sniffer-path")
async def save_sniffer_path(req: SnifferPathRequest, user: dict = Depends(require_auth)):
    """Save the ChartHound download path for The Sniffer's grabbed files."""
    now = datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(settings.database_url) as db:
        await db.execute(
            "INSERT INTO app_settings (key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            ("sniffer_download_path", req.download_path, now),
        )
        await db.commit()
    log.info(f"Sniffer download path saved: {req.download_path}")
    return {"ok": True, "download_path": req.download_path}


@router.get("/sniffer-path")
async def get_sniffer_path(user: dict = Depends(require_auth)):
    """Get the saved ChartHound download path."""
    try:
        async with aiosqlite.connect(settings.database_url) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT value FROM app_settings WHERE key='sniffer_download_path'"
            )
            row = await cursor.fetchone()
        return {"download_path": row["value"] if row else ""}
    except Exception:
        return {"download_path": ""}


# ── DOWNLOAD PATH CHECK (M8d) ────────────────────────────────────────────────
# ChartHound can't see inside a download client's container, but it can ask the
# client which folders it already saves to. A path inside one of those is almost
# certainly visible to the client. Read-only; never blocks saving.

def _dl_inside(path: str, folder: str) -> bool:
    p, f = path.rstrip("/"), (folder or "").rstrip("/")
    return bool(f) and (p == f or p.startswith(f + "/"))


def _dl_join(base: str, sub: str) -> str:
    if not sub:
        return ""
    return sub if sub.startswith("/") or not base else base.rstrip("/") + "/" + sub


async def _dl_conn(db, svc: str):
    row = await (await db.execute(
        "SELECT base_url, token_enc, extra_json FROM connections WHERE service=? AND base_url IS NOT NULL",
        (svc,))).fetchone()
    if not row or not row["base_url"]:
        return None
    return (row["base_url"].rstrip("/"), decrypt_token(row["token_enc"] or ""),
            json.loads(row["extra_json"] or "{}"))


async def _dl_qbittorrent(c, base, token, extra) -> list:
    r = await c.post(f"{base}/api/v2/auth/login",
                     data={"username": extra.get("username", "admin"), "password": token})
    cookies = {"SID": r.cookies.get("SID")} if r.cookies.get("SID") else {}
    p = (await c.get(f"{base}/api/v2/app/preferences", cookies=cookies)).json()
    root = p.get("save_path", "")
    out = [root]
    if p.get("temp_path_enabled"):
        out.append(p.get("temp_path", ""))
    try:
        cats = (await c.get(f"{base}/api/v2/torrents/categories", cookies=cookies)).json()
        out += [_dl_join(root, v.get("savePath", "")) for v in cats.values()]
    except Exception:
        pass
    try:   # folders existing torrents already use (covers extra mounts like /newmusic)
        tors = (await c.get(f"{base}/api/v2/torrents/info", cookies=cookies)).json()
        out += list({t.get("save_path", "") for t in tors})[:200]
    except Exception:
        pass
    return out


async def _dl_deluge(c, base, token, extra) -> list:
    rpc = f"{base}/json"
    await c.post(rpc, json={"method": "auth.login", "params": [token], "id": 1})
    r = (await c.post(rpc, json={"method": "core.get_config_values",
                                 "params": [["download_location", "move_completed_path"]], "id": 2})).json()
    res = r.get("result") or {}
    if not res:
        raise ValueError((r.get("error") or {}).get("message") or "Deluge did not report its folders")
    return [res.get("download_location", ""), res.get("move_completed_path", "")]


async def _dl_transmission(c, base, token, extra) -> list:
    rpc = f"{base}/transmission/rpc"
    user = extra.get("username", "")
    auth = (user, token) if user else None
    sid = (await c.get(rpc, auth=auth)).headers.get("X-Transmission-Session-Id", "")
    r = (await c.post(rpc, auth=auth, headers={"X-Transmission-Session-Id": sid},
                      json={"method": "session-get"})).json()
    a = r.get("arguments") or {}
    return [a.get("download-dir", ""), a.get("incomplete-dir", "") if a.get("incomplete-dir-enabled") else ""]


async def _dl_sabnzbd(c, base, token, extra) -> tuple:
    r = (await c.get(f"{base}/api", params={"mode": "get_config", "apikey": token, "output": "json"})).json()
    cfg = r.get("config") or {}
    misc = cfg.get("misc") or {}
    root = misc.get("complete_dir", "")
    cats = {x.get("name"): _dl_join(root, x.get("dir", "")) for x in (cfg.get("categories") or [])}
    return [root, misc.get("download_dir", "")] + [v for v in cats.values() if v], cats.get("charthound-music")


_DL_NAMES = {"qbittorrent": "qBittorrent", "deluge": "Deluge", "transmission": "Transmission", "sabnzbd": "SABnzbd"}


@router.get("/download-path-check")
async def download_path_check(user: dict = Depends(require_auth)):
    async with aiosqlite.connect(settings.database_url) as db:
        db.row_factory = aiosqlite.Row
        row = await (await db.execute(
            "SELECT value FROM app_settings WHERE key='sniffer_download_path'")).fetchone()
        path = (row["value"] if row else "") or ""
        torrent = None
        for svc in ("qbittorrent", "deluge", "transmission"):   # same order The Sniffer grabs with
            conn = await _dl_conn(db, svc)
            if conn:
                torrent = (svc, conn)
                break
        sab = await _dl_conn(db, "sabnzbd")

    clients = []
    async with httpx.AsyncClient(timeout=httpx.Timeout(10.0), verify=False) as c:
        if torrent:
            svc, (base, token, extra) = torrent
            item = {"service": svc, "name": _DL_NAMES[svc], "folders": [], "error": None, "ok": None}
            try:
                fn = {"qbittorrent": _dl_qbittorrent, "deluge": _dl_deluge, "transmission": _dl_transmission}[svc]
                item["folders"] = sorted({f.rstrip("/") for f in await fn(c, base, token, extra) if f})
                if path:
                    item["ok"] = any(_dl_inside(path, f) for f in item["folders"])
            except Exception as e:
                item["error"] = f"Couldn't ask {_DL_NAMES[svc]} for its folders ({type(e).__name__})."
            clients.append(item)
        if sab:
            base, token, extra = sab
            item = {"service": "sabnzbd", "name": "SABnzbd", "folders": [], "error": None, "ok": None,
                    "category_dir": None}
            try:
                folders, cat_dir = await _dl_sabnzbd(c, base, token, extra)
                item["folders"] = sorted({f.rstrip("/") for f in folders if f})
                item["category_dir"] = cat_dir.rstrip("/") if cat_dir else None
                # SABnzbd ignores the path; its 'charthound-music' category decides where grabs go
                if path:
                    item["ok"] = bool(cat_dir) and cat_dir.rstrip("/") == path.rstrip("/")
            except Exception as e:
                item["error"] = f"Couldn't ask SABnzbd for its folders ({type(e).__name__})."
            clients.append(item)
    return {"download_path": path, "clients": clients}


@router.get("/secret-key-status")
async def secret_key_status(user: dict = Depends(require_auth)):
    """
    M2 FIX: Now requires auth. Was previously public, leaking encryption-config
    state to unauthenticated callers. Frontend should call this post-login.
    """
    is_placeholder = settings.secret_key == "CHANGE_ME_GENERATE_A_STRONG_RANDOM_KEY"
    return {"placeholder": is_placeholder}
