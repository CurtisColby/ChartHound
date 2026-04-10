"""
ChartHound — Pydantic Data Models
Request/response schemas for all API endpoints.
"""

from typing import Optional, List
from pydantic import BaseModel, Field


# ── Auth ──────────────────────────────────────────────────────────────────────

class RegisterRequest(BaseModel):
    username: str = Field(..., min_length=3, max_length=50)
    password: str = Field(..., min_length=8)


class LoginRequest(BaseModel):
    username: str
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    username: str
    is_admin: bool


class LockdownStatus(BaseModel):
    lockdown_active: bool
    user_count: int
    registration_open: bool
    env_override: bool


# ── Connections (Kennel tab) ──────────────────────────────────────────────────

class ConnectionSave(BaseModel):
    service: str        # 'plex', 'emby', 'jellyfin', 'lastfm', 'prowlarr'
    base_url: Optional[str] = None
    token: Optional[str] = None     # stored encrypted
    extra_json: Optional[str] = None


class ConnectionStatus(BaseModel):
    service: str
    connected: bool
    base_url: Optional[str] = None
    verified_at: Optional[str] = None


# ── Chart Data ────────────────────────────────────────────────────────────────

class ChartEntry(BaseModel):
    chart_name: str
    chart_era: Optional[str] = None
    peak_position: Optional[int] = None
    weeks_on_chart: Optional[int] = None
    star_rating: Optional[int] = None
    confidence: str = "low"
    listener_count: int = 0
    comment_string: Optional[str] = None


# ── Tracks ────────────────────────────────────────────────────────────────────

class TrackMeta(BaseModel):
    track_id: Optional[int] = None
    file_path: str
    artist: Optional[str] = None
    album: Optional[str] = None
    title: Optional[str] = None
    year: Optional[int] = None
    genre_1: Optional[str] = None
    genre_2: Optional[str] = None
    genre_3: Optional[str] = None
    mood_1: Optional[str] = None
    mood_2: Optional[str] = None
    mood_3: Optional[str] = None
    bpm: Optional[int] = None
    mbid: Optional[str] = None
    art_path: Optional[str] = None
    chart_entries: List[ChartEntry] = []


class WriteResult(BaseModel):
    file_path: str
    success: bool
    fields_written: List[str] = []
    error: Optional[str] = None


# ── Scan Jobs ─────────────────────────────────────────────────────────────────

class ScanJobStatus(BaseModel):
    job_id: int
    job_type: str
    status: str
    total_tracks: int
    processed: int
    matched: int
    failed: int
    started_at: Optional[str] = None
    paused_at: Optional[str] = None
    completed_at: Optional[str] = None


# ── System ────────────────────────────────────────────────────────────────────

class HealthResponse(BaseModel):
    status: str = "ok"
    version: str = "1.0.0"
    db_path: str
    lockdown_active: bool
    secret_key_placeholder: bool = False
