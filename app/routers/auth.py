"""
ChartHound — Auth Router
/auth/register  — Bootstrap lock enforced (Constitution §4)
/auth/login     — Returns JWT session token (rate-limited)
/auth/status    — Returns current lockdown state for UI

Security Audit Fix (May 2026):
  - Login endpoint rate-limited: 5 failed attempts per IP → 30s cooldown.
"""

import logging
import time
import aiosqlite
from collections import defaultdict
from fastapi import APIRouter, HTTPException, Request, status

from app.config import get_settings
from app.database import get_user_count
from app.security import (
    hash_password, verify_password,
    create_access_token, registration_is_open, lockdown_status
)
from app.models import RegisterRequest, LoginRequest, TokenResponse, LockdownStatus

log = logging.getLogger("charthound.auth")
router = APIRouter(prefix="/auth", tags=["auth"])
settings = get_settings()

# ── Login Rate Limiter (Security Audit May 2026) ─────────────────────────────
# Tracks failed login attempts per IP. After MAX_FAILURES within WINDOW_SEC,
# all login attempts from that IP are blocked for COOLDOWN_SEC.
_MAX_FAILURES  = 5
_WINDOW_SEC    = 120    # count failures within this window
_COOLDOWN_SEC  = 30     # lockout duration after hitting max
_fail_log: dict = defaultdict(list)   # ip → [timestamp, ...]
_cooldown_until: dict = {}            # ip → unlock_timestamp


def _check_rate_limit(ip: str):
    """Raise 429 if this IP has exceeded the failure threshold."""
    now = time.time()

    # Check active cooldown
    if ip in _cooldown_until and now < _cooldown_until[ip]:
        remaining = int(_cooldown_until[ip] - now)
        log.warning(f"Login rate-limited: {ip} — {remaining}s remaining.")
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Too many failed login attempts. Try again in {remaining} seconds."
        )

    # Expire old entries outside the window
    _fail_log[ip] = [t for t in _fail_log[ip] if now - t < _WINDOW_SEC]

    if len(_fail_log[ip]) >= _MAX_FAILURES:
        _cooldown_until[ip] = now + _COOLDOWN_SEC
        _fail_log[ip] = []
        log.warning(f"Login rate limit triggered for {ip} — locked for {_COOLDOWN_SEC}s.")
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Too many failed login attempts. Try again in {_COOLDOWN_SEC} seconds."
        )


def _record_failure(ip: str):
    _fail_log[ip].append(time.time())


def _clear_failures(ip: str):
    _fail_log.pop(ip, None)
    _cooldown_until.pop(ip, None)


# ── Endpoints ────────────────────────────────────────────────────────────────

@router.get("/status", response_model=LockdownStatus)
async def get_lockdown_status():
    count = await get_user_count()
    return lockdown_status(count)


@router.post("/register", response_model=TokenResponse, status_code=status.HTTP_201_CREATED)
async def register(req: RegisterRequest):
    count = await get_user_count()

    if not registration_is_open(count):
        log.warning(f"Blocked registration attempt for '{req.username}' — Lockdown Mode.")
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "Registration is disabled. ChartHound is in Lockdown Mode. "
                "Set CH_OPEN_REGISTRATION=true in docker-compose.yml and restart."
            )
        )

    async with aiosqlite.connect(settings.database_url) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT user_id FROM users WHERE username = ?", (req.username,))
        if await cursor.fetchone():
            raise HTTPException(status_code=400, detail="Username already taken.")

        is_admin = 1 if count == 0 else 0
        pw_hash = hash_password(req.password)
        await db.execute(
            "INSERT INTO users (username, password_hash, is_admin) VALUES (?, ?, ?)",
            (req.username, pw_hash, is_admin)
        )
        await db.commit()
        log.info(f"User '{req.username}' registered. Admin={bool(is_admin)}. Lockdown re-engaged.")

    token = create_access_token({"sub": req.username, "admin": bool(is_admin)})
    return TokenResponse(access_token=token, username=req.username, is_admin=bool(is_admin))


@router.post("/login", response_model=TokenResponse)
async def login(req: LoginRequest, request: Request):
    # Security Audit Fix: rate-limit failed login attempts per IP
    client_ip = request.client.host if request.client else "unknown"
    _check_rate_limit(client_ip)

    async with aiosqlite.connect(settings.database_url) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT user_id, password_hash, is_admin FROM users WHERE username = ?",
            (req.username,)
        )
        row = await cursor.fetchone()

    if not row or not verify_password(req.password, row["password_hash"]):
        _record_failure(client_ip)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid username or password."
        )

    # Successful login — clear any accumulated failures for this IP
    _clear_failures(client_ip)
    token = create_access_token({"sub": req.username, "admin": bool(row["is_admin"])})
    log.info(f"User '{req.username}' logged in.")
    return TokenResponse(access_token=token, username=req.username, is_admin=bool(row["is_admin"]))
