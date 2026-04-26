"""
ChartHound — Main FastAPI Application
Port: 8585 (host) → 8000 (container)

M4 Change: App now starts even with placeholder SECRET_KEY.
Shows warning banner via /api/health response instead of refusing to start.
"""

import logging
import sys
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

from app.config import get_settings
from app.database import init_db, get_user_count
from app.security import lockdown_status
from app.models import HealthResponse
from app.routers import auth, kennel, retriever, groomer, debug, sniffer, bloodhound, tracker

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  [%(levelname)s]  %(name)s — %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("charthound")

# Install in-memory debug log handler — captures all charthound.* output
from app.routers.debug import install_debug_handler
install_debug_handler()
settings = get_settings()

PLACEHOLDER_KEY = "CHANGE_ME_GENERATE_A_STRONG_RANDOM_KEY"


@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("=" * 60)
    log.info("  ChartHound — The New World  |  v1.0.0")
    log.info("  Developed by Colby R. Curtis · Claude.ai code support")
    log.info("=" * 60)

    # M4: Warn about placeholder key but don't exit
    if settings.secret_key == PLACEHOLDER_KEY:
        log.warning("⚠  SECRET_KEY is set to the placeholder value!")
        log.warning("⚠  Connection saving is DISABLED until a real key is set.")
        log.warning("⚠  Generate one: python3 -c \"import secrets; print(secrets.token_hex(32))\"")
        log.warning("⚠  Add it to docker-compose.yml and restart the container.")
    else:
        log.info("✅ SECRET_KEY configured.")

    await init_db()

    # Start Tracker background loop if it was enabled
    await tracker.tracker_startup()

    # Start Groomer weekly auto-refresh scheduler
    await groomer.groomer_startup()

    # Start Groomer weekly auto-refresh scheduler

    count = await get_user_count()
    status = lockdown_status(count)
    if status["lockdown_active"]:
        log.info(f"🔒 LOCKDOWN MODE — {count} user(s) registered. /register DISABLED.")
    else:
        log.info(f"🔓 Registration OPEN — {count} user(s). CH_OPEN_REGISTRATION=true")

    log.info(f"Database : {settings.database_url}")
    log.info(f"Music vol: {settings.docker_music_prefix}")
    log.info("ChartHound is ready.")
    log.info("=" * 60)

    yield

    log.info("ChartHound shutting down.")


app = FastAPI(
    title="ChartHound",
    description="Dockerized music metadata engine — The New World",
    version="1.0.0",
    lifespan=lifespan,
    docs_url="/api/docs",
    redoc_url=None,
)

# C1 FIX: CORS allowlist from CH_ALLOWED_ORIGINS env var.
# Falls back to localhost-only if unset. Wildcard origins + credentials
# is a textbook CSRF enabler; never re-enable both.
_raw_origins = getattr(settings, "ch_allowed_origins", "") or ""
_allowed_origins = [o.strip() for o in _raw_origins.split(",") if o.strip()]
if not _allowed_origins:
    _allowed_origins = [
        "http://localhost:8585",
        "http://127.0.0.1:8585",
    ]
    log.warning(
        "⚠  CH_ALLOWED_ORIGINS not set — defaulting to localhost only. "
        "Add your LAN/domain origins to docker-compose.yml for browser access."
    )
log.info(f"CORS allowed origins: {_allowed_origins}")

app.add_middleware(
    CORSMiddleware,
    allow_origins=_allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth.router, prefix="/api")
app.include_router(kennel.router)
app.include_router(retriever.router)
app.include_router(groomer.router)
app.include_router(debug.router)
app.include_router(sniffer.router)
app.include_router(bloodhound.router)
app.include_router(tracker.router)


@app.get("/api/health", response_model=HealthResponse, tags=["system"])
async def health():
    """
    M1 FIX: Public health endpoint returns minimum info needed for liveness checks.
    Internal-disclosure fields (db_path, secret_key_placeholder) moved to
    authed-only equivalents in kennel.py.
    """
    try:
        count = await get_user_count()
        ls = lockdown_status(count)
        locked = ls["lockdown_active"]
    except Exception:
        locked = True
    return HealthResponse(
        status="ok",
        version="1.0.0",
        db_path="",  # M1: redacted from public response
        lockdown_active=locked,
        secret_key_placeholder=False,  # M1: redacted; see /api/kennel/secret-key-status (authed)
    )


app.mount("/static", StaticFiles(directory="frontend"), name="static")


@app.get("/{full_path:path}", include_in_schema=False)
async def serve_frontend(full_path: str):
    return FileResponse("frontend/index.html")
