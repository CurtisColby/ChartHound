"""
ChartHound — Application Settings
Reads all configuration from environment variables (set in docker-compose.yml).
No secrets are ever hard-coded here.
"""

from pydantic_settings import BaseSettings
from functools import lru_cache


class Settings(BaseSettings):
    # ── Core Security ─────────────────────────────────────────────
    # Used to encrypt stored API tokens and sign JWT session tokens.
    # MUST be set in docker-compose.yml. App will refuse to start without it.
    secret_key: str

    # ── Registration Lock (Constitution §4) ───────────────────────
    # Default: False (Lockdown Mode). Set to "true" in docker-compose.yml
    # only when you need to add a new user. Remove afterward.
    ch_open_registration: bool = False

    # ── Database ──────────────────────────────────────────────────
    database_url: str = "/data/charthound.db"

    # ── Path Translation ──────────────────────────────────────────
    # Maps media server paths to Docker /music mount paths.
    # Example: Plex says /media/nas/MUSIC → app reads /music
    media_server_music_prefix: str = ""
    docker_music_prefix: str = "/music"

    # ── Rate Limits (Constitution §3) ─────────────────────────────
    # iTunes: hard cap at 20 requests/minute (Leaky Bucket enforced in service layer)
    itunes_max_rpm: int = 20

    # ── JWT Session Tokens ────────────────────────────────────────
    jwt_algorithm: str = "HS256"
    jwt_expire_minutes: int = 1440  # 24 hours

    class Config:
        env_file = ".env"
        case_sensitive = False


@lru_cache()
def get_settings() -> Settings:
    """Cached settings — only parsed once per process."""
    return Settings()
