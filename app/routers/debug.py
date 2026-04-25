# © 2026 Colby R. Curtis | ChartHound: The New World
# All Rights Reserved.
"""
ChartHound — Debug Log Router
Captures all charthound.* logger output into an in-memory ring buffer.
Provides download and live-view endpoints for troubleshooting.

Endpoints:
  GET /api/debug/lines        — last N log lines as JSON (live view)
  GET /api/debug/download     — full buffer as plain text download
  POST /api/debug/clear       — clear the buffer
  GET /api/debug/status       — buffer stats

The DebugHandler is registered on the root 'charthound' logger at startup
so all child loggers (charthound.groomer, charthound.retriever, etc.)
flow into it automatically with zero changes to other routers.
"""

import logging
import threading
from collections import deque
from datetime import datetime, timezone
from fastapi import APIRouter, Depends
from fastapi.responses import PlainTextResponse

from app.deps import require_auth

# ── Ring buffer — thread-safe, max 5000 entries ───────────────────────────────
_BUFFER_MAX  = 5000
_log_buffer: deque = deque(maxlen=_BUFFER_MAX)
_buffer_lock = threading.Lock()

# ── Custom logging handler ────────────────────────────────────────────────────
class DebugHandler(logging.Handler):
    """
    Intercepts all charthound.* log records and appends them to _log_buffer.
    Installed once at app startup. Thread-safe via deque maxlen + lock.
    """
    LEVEL_EMOJI = {
        "DEBUG":    "🔵",
        "INFO":     "⚪",
        "WARNING":  "🟡",
        "ERROR":    "🔴",
        "CRITICAL": "🔴",
    }

    def emit(self, record: logging.LogRecord):
        try:
            ts  = datetime.now(timezone.utc).strftime("%H:%M:%S.%f")[:-3]
            lvl = record.levelname
            # Shorten logger name: charthound.groomer → groomer
            name = record.name.replace("charthound.", "").replace("charthound", "app")
            msg  = self.format(record)
            # Strip the default formatter prefix since we build our own
            if "] " in msg:
                msg = msg.split("] ", 1)[-1] if msg.startswith("[") else msg

            entry = {
                "ts":    ts,
                "level": lvl,
                "name":  name,
                "msg":   msg,
                "emoji": self.LEVEL_EMOJI.get(lvl, "⚪"),
            }
            with _buffer_lock:
                _log_buffer.append(entry)
        except Exception:
            pass  # Never let debug logging crash the app


def install_debug_handler():
    """
    Call once at app startup. Attaches DebugHandler to root charthound logger.
    All child loggers inherit it automatically.
    """
    root_logger = logging.getLogger("charthound")
    # Avoid duplicate handlers if called multiple times
    if not any(isinstance(h, DebugHandler) for h in root_logger.handlers):
        handler = DebugHandler()
        handler.setLevel(logging.DEBUG)
        formatter = logging.Formatter("%(message)s")
        handler.setFormatter(formatter)
        root_logger.addHandler(handler)
        root_logger.setLevel(logging.DEBUG)

        # Also capture uvicorn access logs for endpoint tracking
        uvicorn_logger = logging.getLogger("uvicorn.access")
        if not any(isinstance(h, DebugHandler) for h in uvicorn_logger.handlers):
            uvicorn_logger.addHandler(handler)

    # Add a startup entry
    with _buffer_lock:
        _log_buffer.append({
            "ts":    datetime.now(timezone.utc).strftime("%H:%M:%S.%f")[:-3],
            "level": "INFO",
            "name":  "debug",
            "msg":   "🐾 ChartHound debug log initialized — capturing all charthound.* events",
            "emoji": "⚪",
        })


# ── Router ────────────────────────────────────────────────────────────────────
router = APIRouter(prefix="/api/debug", tags=["debug"])


@router.get("/lines")
async def get_log_lines(
    n:     int = 200,
    level: str = "",        # filter: DEBUG | INFO | WARNING | ERROR
    name:  str = "",        # filter by logger name e.g. 'groomer'
    _=Depends(require_auth)
):
    """Returns last N log lines from the buffer, newest last."""
    with _buffer_lock:
        entries = list(_log_buffer)

    if level:
        entries = [e for e in entries if e["level"] == level.upper()]
    if name:
        entries = [e for e in entries if name.lower() in e["name"].lower()]

    return {
        "lines":  entries[-n:],
        "total":  len(_log_buffer),
        "filtered": len(entries),
    }


@router.get("/download")
async def download_log(
    level: str = "",
    _=Depends(require_auth)
):
    """Downloads the full log buffer as a plain text file."""
    with _buffer_lock:
        entries = list(_log_buffer)

    if level:
        entries = [e for e in entries if e["level"] == level.upper()]

    lines = []
    lines.append(f"ChartHound Debug Log — {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}")
    lines.append(f"Entries: {len(entries)} | Buffer max: {_BUFFER_MAX}")
    lines.append("=" * 80)

    for e in entries:
        lines.append(f"[{e['ts']}] [{e['level']:8s}] [{e['name']:12s}] {e['msg']}")

    filename = f"charthound_debug_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.txt"
    return PlainTextResponse(
        "\n".join(lines),
        headers={"Content-Disposition": f'attachment; filename="{filename}"'}
    )


@router.post("/clear")
async def clear_log(_=Depends(require_auth)):
    """Clears the in-memory log buffer."""
    with _buffer_lock:
        count = len(_log_buffer)
        _log_buffer.clear()
        _log_buffer.append({
            "ts":    datetime.now(timezone.utc).strftime("%H:%M:%S.%f")[:-3],
            "level": "INFO",
            "name":  "debug",
            "msg":   f"Log cleared — {count} entries removed",
            "emoji": "⚪",
        })
    return {"ok": True, "cleared": count}


@router.get("/status")
async def debug_status(_=Depends(require_auth)):
    """Returns buffer stats."""
    with _buffer_lock:
        total = len(_log_buffer)
        by_level = {}
        for e in _log_buffer:
            by_level[e["level"]] = by_level.get(e["level"], 0) + 1
    return {
        "total":    total,
        "max":      _BUFFER_MAX,
        "pct_full": round(total / _BUFFER_MAX * 100, 1),
        "by_level": by_level,
    }
