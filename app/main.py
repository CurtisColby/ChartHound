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
from app.routers import auth, kennel, retriever

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  [%(levelname)s]  %(name)s — %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("charthound")
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
    description="Dockerized music metadata engine — MusicBrainz > iTunes > Last.fm",
    version="1.0.0",
    lifespan=lifespan,
    docs_url="/api/docs",
    redoc_url=None,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth.router, prefix="/api")
app.include_router(kennel.router)
app.include_router(retriever.router)
# app.include_router(sniffer.router)    # M5
# app.include_router(groomer.router)    # M6
# app.include_router(bloodhound.router) # M7
# app.include_router(tracker.router)    # M8
# app.include_router(scout.router)      # M9
# app.include_router(lookout.router)    # M10


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
        secret_key_placeholder=(settings.secret_key == PLACEHOLDER_KEY),
    )


app.mount("/static", StaticFiles(directory="frontend"), name="static")


@app.get("/{full_path:path}", include_in_schema=False)
async def serve_frontend(full_path: str):
    return FileResponse("frontend/index.html")
