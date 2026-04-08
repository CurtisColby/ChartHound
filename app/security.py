"""
ChartHound — Security Module
Handles: JWT session tokens, password hashing, registration lock,
and Fernet encryption for stored API tokens (Constitution §4).
"""

import logging
import bcrypt
from datetime import datetime, timedelta, timezone
from typing import Optional

from cryptography.fernet import Fernet
from jose import JWTError, jwt

from app.config import get_settings

log = logging.getLogger("charthound.security")
settings = get_settings()


# ── Password Hashing (bcrypt direct — no passlib) ─────────────────────────────

def hash_password(plain: str) -> str:
    """Hash a password with bcrypt. Truncates to 72 bytes (bcrypt limit)."""
    return bcrypt.hashpw(plain.encode()[:72], bcrypt.gensalt()).decode()


def verify_password(plain: str, hashed: str) -> bool:
    """Verify a plaintext password against a bcrypt hash."""
    try:
        return bcrypt.checkpw(plain.encode()[:72], hashed.encode())
    except Exception:
        return False


# ── JWT Session Tokens ────────────────────────────────────────────────────────

def create_access_token(data: dict, expires_minutes: Optional[int] = None) -> str:
    to_encode = data.copy()
    expire = datetime.now(timezone.utc) + timedelta(
        minutes=expires_minutes or settings.jwt_expire_minutes
    )
    to_encode["exp"] = expire
    return jwt.encode(to_encode, settings.secret_key, algorithm=settings.jwt_algorithm)


def decode_access_token(token: str) -> Optional[dict]:
    try:
        return jwt.decode(token, settings.secret_key, algorithms=[settings.jwt_algorithm])
    except JWTError:
        return None


# ── Fernet Encryption for Stored API Tokens ───────────────────────────────────

def _get_fernet() -> Fernet:
    import base64
    raw = settings.secret_key.encode()[:32].ljust(32, b"\x00")
    key = base64.urlsafe_b64encode(raw)
    return Fernet(key)


def encrypt_token(plaintext: str) -> str:
    if not plaintext:
        return ""
    return _get_fernet().encrypt(plaintext.encode()).decode()


def decrypt_token(ciphertext: str) -> str:
    if not ciphertext:
        return ""
    try:
        return _get_fernet().decrypt(ciphertext.encode()).decode()
    except Exception:
        log.error("Token decryption failed — SECRET_KEY may have changed.")
        return ""


# ── Registration Lock (Constitution §4) ──────────────────────────────────────

def registration_is_open(user_count: int) -> bool:
    if user_count == 0:
        return True
    return settings.ch_open_registration


def lockdown_status(user_count: int) -> dict:
    open_reg = registration_is_open(user_count)
    return {
        "lockdown_active": not open_reg,
        "user_count": user_count,
        "registration_open": open_reg,
        "env_override": settings.ch_open_registration,
    }
