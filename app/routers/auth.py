"""
ChartHound — Auth Router
/auth/register  — Bootstrap lock enforced (Constitution §4)
/auth/login     — Returns JWT session token
/auth/status    — Returns current lockdown state for UI
"""

import logging
import aiosqlite
from fastapi import APIRouter, HTTPException, status

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
async def login(req: LoginRequest):
    async with aiosqlite.connect(settings.database_url) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT user_id, password_hash, is_admin FROM users WHERE username = ?",
            (req.username,)
        )
        row = await cursor.fetchone()

    if not row or not verify_password(req.password, row["password_hash"]):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid username or password."
        )

    token = create_access_token({"sub": req.username, "admin": bool(row["is_admin"])})
    log.info(f"User '{req.username}' logged in.")
    return TokenResponse(access_token=token, username=req.username, is_admin=bool(row["is_admin"]))
