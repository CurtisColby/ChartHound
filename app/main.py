"""
ChartHound — Main FastAPI Application
Port: 8585 (host) → 8000 (container)
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
from app.routers import auth, kennel

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  [%(levelname)s]  %(name)s — %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("charthound")
settings = get_settings()


# ── Startup / Shutdown ────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    # ── STARTUP ──
    log.info("=" * 60)
    log.info("  ChartHound — The New World  |  v1.0.0")
    log.info("  Designed by Colby Curtis")
    log.info("=" * 60)

    # Validate critical config
    if settings.secret_key == "CHANGE_ME_GENERATE_A_STRONG_RANDOM_KEY":
        log.critical("SECRET_KEY is set to the default placeholder value!")
        log.critical("Generate a real key: python3 -c \"import secrets; print(secrets.token_hex(32))\"")
        log.critical("Set it in docker-compose.yml and restart. Refusing to start.")
        sys.exit(1)

    await init_db()

    count = await get_user_count()
    status = lockdown_status(count)
    if status["lockdown_active"]:
        log.info(f"🔒 LOCKDOWN MODE ACTIVE — {count} user(s) registered. /register is DISABLED.")
    else:
        log.info(f"🔓 Registration OPEN — {count} user(s) exist. CH_OPEN_REGISTRATION=true")

    log.info(f"Database : {settings.database_url}")
    log.info(f"Music vol: {settings.docker_music_prefix}")
    log.info("ChartHound is ready. http://localhost:8585")
    log.info("=" * 60)

    yield

    # ── SHUTDOWN ──
    log.info("ChartHound shutting down.")


# ── App ───────────────────────────────────────────────────────────────────────
app = FastAPI(
    title="ChartHound",
    description="Dockerized music metadata engine — MusicBrainz > iTunes > Last.fm",
    version="1.0.0",
    lifespan=lifespan,
    docs_url="/api/docs",
    redoc_url=None,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],    # Tightened in production via env var if needed
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Routers ───────────────────────────────────────────────────────────────────
app.include_router(auth.router, prefix="/api")
app.include_router(kennel.router)                        # Milestone 2 — prefix already set in router
# Future milestones will add:
# app.include_router(retriever.router, prefix="/api")   # Milestone 3
# app.include_router(sniffer.router,   prefix="/api")   # Milestone 4
# app.include_router(lookout.router,   prefix="/api")   # Milestone 5
# app.include_router(scout.router,     prefix="/api")   # Milestone 6


# ── Health Check ──────────────────────────────────────────────────────────────
@app.get("/api/health", response_model=HealthResponse, tags=["system"])
async def health():
    try:
        count = await get_user_count()
        ls = lockdown_status(count)
        locked = ls["lockdown_active"]
    except Exception:
        locked = True
    return HealthResponse(
        status="ok",
        version="1.0.0",
        db_path=settings.database_url,
        lockdown_active=locked,
    )


# ── Serve Frontend ────────────────────────────────────────────────────────────
app.mount("/static", StaticFiles(directory="frontend"), name="static")


@app.get("/{full_path:path}", include_in_schema=False)
async def serve_frontend(full_path: str):
    """Catch-all: serve the SPA for any non-API route."""
    return FileResponse("frontend/index.html")
