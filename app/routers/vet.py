# © 2026 Colby R. Curtis | ChartHound: The New World
# All Rights Reserved.
"""
ChartHound — The Veterinarian (split from groomer.py in M9b)
DB health, backups, chart-source importers, weekly auto-refresh, nightly library-cache sync.
Routes keep their /api/groomer/vet/... paths (included by groomer.router).
"""

import asyncio
import logging
import os
import re
import sqlite3
import time

import aiosqlite
import httpx

from datetime import datetime, timedelta
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks
from pydantic import BaseModel

from app.config import get_settings
from app.deps import require_auth

from app.routers.groomer_common import (
    _DYNAMIC_DB,
    _STATIC_DB,
    _norm,
    _utc_epoch,
)

log      = logging.getLogger("charthound.groomer")
router   = APIRouter()   # tags + /api/groomer prefix come from groomer.router
settings = get_settings()

# ══════════════════════════════════════════════════════════════════════════════
#  VETERINARIAN — DB Health & Maintenance
# ══════════════════════════════════════════════════════════════════════════════

def _fmt_size(path: str) -> str:
    """Format file size as human-readable string."""
    try:
        size = os.path.getsize(path)
        if size < 1024:
            return f"{size} B"
        elif size < 1024 * 1024:
            return f"{size / 1024:.1f} KB"
        else:
            return f"{size / (1024 * 1024):.1f} MB"
    except Exception:
        return "—"


@router.get("/vet/db_health")
async def vet_db_health(_=Depends(require_auth)):
    """Returns row counts and file sizes for both databases."""
    result = {
        "dynamic_size": _fmt_size(_DYNAMIC_DB),
        "static_size":  _fmt_size(_STATIC_DB) if os.path.exists(_STATIC_DB) else "Not found",
        "tracks": 0, "artists": 0, "albums": 0,
        "chart_data": 0, "connections": 0,
        "chart_reference": 0, "billboard_pop": 0,
        "chart_reference_extras": 0,
    }
    try:
        async with aiosqlite.connect(_DYNAMIC_DB) as db:
            for table, key in [
                ("tracks", "tracks"), ("artists", "artists"),
                ("albums", "albums"), ("chart_data", "chart_data"),
                ("connections", "connections"),
                ("chart_reference_extras", "chart_reference_extras"),
            ]:
                try:
                    async with db.execute(f"SELECT COUNT(*) FROM {table}") as cur:
                        row = await cur.fetchone()
                        result[key] = row[0] if row else 0
                except Exception:
                    pass
    except Exception as e:
        log.warning(f"vet_db_health dynamic error: {e}")

    if os.path.exists(_STATIC_DB):
        try:
            conn = sqlite3.connect(_STATIC_DB, check_same_thread=False)
            try:
                result["chart_reference"] = conn.execute(
                    "SELECT COUNT(*) FROM chart_reference"
                ).fetchone()[0]
            except Exception:
                pass
            try:
                result["billboard_pop"] = conn.execute(
                    "SELECT COUNT(*) FROM billboard_pop"
                ).fetchone()[0]
            except Exception:
                pass
            conn.close()
        except Exception as e:
            log.warning(f"vet_db_health static error: {e}")

    return result


@router.post("/vet/vacuum")
async def vet_vacuum(_=Depends(require_auth)):
    """Run VACUUM on the dynamic database to reclaim disk space."""
    before = _fmt_size(_DYNAMIC_DB)
    try:
        conn = sqlite3.connect(_DYNAMIC_DB)
        conn.execute("VACUUM")
        conn.close()
        after = _fmt_size(_DYNAMIC_DB)
        log.info(f"VACUUM complete: {before} → {after}")
        return {"ok": True, "before_size": before, "after_size": after}
    except Exception as e:
        log.error(f"VACUUM failed: {e}")
        raise HTTPException(500, f"VACUUM failed: {e}")


@router.post("/vet/integrity_check")
async def vet_integrity_check(_=Depends(require_auth)):
    """Run PRAGMA integrity_check on the dynamic database."""
    try:
        conn = sqlite3.connect(_DYNAMIC_DB, check_same_thread=False)
        rows = conn.execute("PRAGMA integrity_check").fetchall()
        conn.close()
        result_text = rows[0][0] if rows else "unknown"
        is_ok = result_text.lower() == "ok"
        log.info(f"Integrity check: {result_text}")
        return {"ok": is_ok, "result": result_text}
    except Exception as e:
        log.error(f"Integrity check failed: {e}")
        raise HTTPException(500, f"Integrity check failed: {e}")


# ══════════════════════════════════════════════════════════════════════════════
#  VETERINARIAN — DATABASE BACKUPS
#  VACUUM INTO makes a clean, consistent copy while the app keeps running.
#  Newest 5 kept in /data/backups. Downloads use a 10-minute pass (like the
#  Retriever player) signed with a separate key, so it can never act as a login.
# ══════════════════════════════════════════════════════════════════════════════
_BACKUP_DIR  = os.path.join(os.path.dirname(_DYNAMIC_DB) or "/data", "backups")
_BACKUP_KEEP = 5
_BACKUP_RE   = re.compile(r"^charthound-\d{4}-\d{2}-\d{2}_\d{6}\.db$")
_BACKUP_KEY  = settings.secret_key + ":backup"


def _backup_list() -> list:
    try:
        names = [n for n in os.listdir(_BACKUP_DIR) if _BACKUP_RE.match(n)]
    except FileNotFoundError:
        return []
    out = []
    for n in sorted(names, reverse=True):
        p = os.path.join(_BACKUP_DIR, n)
        try:
            st = os.stat(p)
        except OSError:
            continue
        out.append({"name": n, "bytes": st.st_size, "size": _fmt_size(p),
                    "created": time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(st.st_mtime))})
    return out


def _backup_make() -> dict:
    import shutil
    os.makedirs(_BACKUP_DIR, exist_ok=True)
    need = os.path.getsize(_DYNAMIC_DB)
    free = shutil.disk_usage(_BACKUP_DIR).free
    if free < need * 1.2 + 50 * 1024 * 1024:
        raise HTTPException(507, f"Not enough free disk space for a backup "
                                 f"({free // 1048576:,} MB free, about {need // 1048576:,} MB needed).")
    name = f"charthound-{datetime.now().strftime('%Y-%m-%d_%H%M%S')}.db"
    dest, tmp = os.path.join(_BACKUP_DIR, name), os.path.join(_BACKUP_DIR, name + ".part")
    if os.path.exists(tmp):
        os.remove(tmp)
    conn = sqlite3.connect(_DYNAMIC_DB, timeout=120)
    try:
        conn.execute("VACUUM INTO ?", (tmp,))
    finally:
        conn.close()
    chk = sqlite3.connect(tmp)
    try:
        ok = (chk.execute("PRAGMA quick_check").fetchone() or [""])[0] == "ok"
    finally:
        chk.close()
    if not ok:
        os.remove(tmp)
        raise HTTPException(500, "Backup copy failed its check and was discarded. Run INTEGRITY CHECK.")
    os.replace(tmp, dest)
    for old in _backup_list()[_BACKUP_KEEP:]:
        try:
            os.remove(os.path.join(_BACKUP_DIR, old["name"]))
        except OSError:
            pass
    return {"name": name, "size": _fmt_size(dest)}


_backup_lock = asyncio.Lock()


@router.post("/vet/backup")
async def vet_backup(_=Depends(require_auth)):
    if _backup_lock.locked():
        raise HTTPException(409, "A backup is already running.")
    async with _backup_lock:
        try:
            made = await asyncio.to_thread(_backup_make)
        except HTTPException:
            raise
        except Exception as e:
            log.error(f"Backup failed: {e}")
            raise HTTPException(500, f"Backup failed: {e}")
    log.info(f"Database backup created: {made['name']} ({made['size']})")
    return {"ok": True, **made, "backups": _backup_list()}


@router.get("/vet/backups")
async def vet_backups(_=Depends(require_auth)):
    return {"backups": _backup_list(), "keep": _BACKUP_KEEP, "folder": _BACKUP_DIR}


@router.post("/vet/backups/{name}/token")
async def vet_backup_token(name: str, _=Depends(require_auth)):
    import jwt
    if not _BACKUP_RE.match(name) or not os.path.isfile(os.path.join(_BACKUP_DIR, name)):
        raise HTTPException(404, "Backup not found.")
    return {"t": jwt.encode({"bk": name, "exp": int(time.time()) + 600}, _BACKUP_KEY, algorithm="HS256")}


@router.get("/vet/backups/{name}/download")
async def vet_backup_download(name: str, t: str):
    import jwt
    from jwt import PyJWTError as JWTError
    from fastapi.responses import FileResponse
    try:
        claims = jwt.decode(t, _BACKUP_KEY, algorithms=["HS256"])
    except JWTError:
        raise HTTPException(401, "Download link expired — click ⬇ again.")
    if claims.get("bk") != name or not _BACKUP_RE.match(name):
        raise HTTPException(403, "Download link is for a different backup.")
    path = os.path.join(_BACKUP_DIR, name)
    if not os.path.isfile(path):
        raise HTTPException(404, "Backup not found.")
    return FileResponse(path, media_type="application/octet-stream", filename=name)


# ══════════════════════════════════════════════════════════════════════════════
#  VETERINARIAN — STATIC DB SOURCES (user-imported chart data)
#
#  The shipped static DB (`charthound_static.db`) is mounted read-only, so user
#  imports go into a sibling table in the DYNAMIC DB: `chart_reference_extras`.
#  All chart lookups (Groomer scans + Sniffer Chart Gap Fill) UNION across both.
#
#  First source: utdata Hot 100 post-2018
#    Repo: https://github.com/utdata/rwd-billboard-data
#    Raw:  https://raw.githubusercontent.com/utdata/rwd-billboard-data/main/data-out/hot-100-current.csv
#    Columns: chart_week, current_week, title, performer, last_week,
#             peak_pos, wks_on_chart  (and a few more we ignore)
#  More sources will follow the same pattern in this block.
# ══════════════════════════════════════════════════════════════════════════════

# Registry of importable sources the UI can show.
# id         — stable key used by the API + settings
# label      — human string shown in UI
# data_tag   — value stored in `data_source` column so we can count per-source
# importer   — name of the async function that does the work
_STATIC_SOURCES = [
    {
        "id":       "utdata",
        "label":    "utdata Hot 100 post-2018",
        "data_tag": "utdata_hot100",
        "importer": "_import_utdata_hot100",
    },
    {
        "id":       "chart2000",
        "archive":  True,
        "label":    "Chart2000.com 2000–2024 (global)",
        "data_tag": "chart2000_monthly",
        "importer": "_import_chart2000",
    },
    {
        "id":       "tsort",
        "archive":  True,
        "label":    "tsort.info 1900+ Historical (global)",
        "data_tag": "tsort_historical",
        "importer": "_import_tsort",
    },
    {
        "id":       "kworb_us",
        "label":    "Kworb iTunes US (current)",
        "data_tag": "kworb_itunes_us",
        "importer": "_import_kworb_us",
    },
    {
        "id":       "ccm",
        "label":    "Billboard Christian Songs (year-end 1990–present + current week)",
        "data_tag": "ccm_weekly",
        "importer": "_import_ccm",
    },
    {
        "id":       "country",
        "label":    "Billboard Hot Country Songs (year-end 1990–present + current week)",
        "data_tag": "country_yearend",
        "importer": "_import_country",
    },
    {
        "id":       "rnb",
        "label":    "Billboard R&B/Hip-Hop Songs (year-end 1990–present + current week)",
        "data_tag": "rnb_yearend",
        "importer": "_import_rnb",
    },
    {
        "id":       "rock",
        "label":    "Billboard Hot Rock Songs (year-end 1990–present + current week)",
        "data_tag": "rock_yearend",
        "importer": "_import_rock",
    },
    {
        "id":       "dance",
        "label":    "Billboard Dance/Electronic Songs (year-end 1990–present + current week)",
        "data_tag": "dance_yearend",
        "importer": "_import_dance",
    },
    {
        "id":       "adultpop",
        "label":    "Billboard Pop Songs / Adult Pop (year-end 1990–present + current week)",
        "data_tag": "adultpop_yearend",
        "importer": "_import_adultpop",
    },
    {
        "id":       "uk_official",
        "label":    "UK Official Charts Singles (current week)",
        "data_tag": "uk_official_singles",
        "importer": "_import_uk_official",
    },
    {
        "id":       "listenbrainz_historical",
        "label":    "ListenBrainz Historical (Rock/R&B/Country/Dance pre-1990)",
        "data_tag": "listenbrainz_historical",
        "importer": "_import_listenbrainz_historical",
    },
]

# In-memory state for the import job. Only one import runs at a time.
_vet_import_job = {
    "status":    "idle",   # idle | running | done | error
    "source_id": None,
    "inserted":  0,
    "skipped":   0,
    "message":   "",
    "started_at": None,
    "finished_at": None,
}


async def _ensure_extras_table():
    """Create chart_reference_extras in the dynamic DB if missing. Idempotent."""
    try:
        async with aiosqlite.connect(_DYNAMIC_DB) as db:
            await db.executescript("""
                CREATE TABLE IF NOT EXISTS chart_reference_extras (
                    ref_id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    chart_name      TEXT    NOT NULL,
                    artist          TEXT    NOT NULL,
                    title           TEXT    NOT NULL,
                    artist_norm     TEXT    NOT NULL,
                    title_norm      TEXT    NOT NULL,
                    peak_position   INTEGER,
                    weeks_on_chart  INTEGER,
                    chart_year      INTEGER,
                    data_source     TEXT,
                    added_at        TEXT NOT NULL DEFAULT (datetime('now')),
                    UNIQUE(chart_name, artist_norm, title_norm, chart_year)
                );
                CREATE INDEX IF NOT EXISTS idx_extras_artist_title
                    ON chart_reference_extras(artist_norm, title_norm);
                CREATE INDEX IF NOT EXISTS idx_extras_chart
                    ON chart_reference_extras(chart_name);
                CREATE INDEX IF NOT EXISTS idx_extras_source
                    ON chart_reference_extras(data_source);
            """)
            await db.commit()
    except Exception as e:
        log.warning(f"_ensure_extras_table: {e}")


async def _count_extras(data_tag: Optional[str] = None) -> int:
    """Count rows in chart_reference_extras, optionally filtered by data_source."""
    try:
        async with aiosqlite.connect(_DYNAMIC_DB) as db:
            if data_tag:
                async with db.execute(
                    "SELECT COUNT(*) FROM chart_reference_extras WHERE data_source=?",
                    (data_tag,)
                ) as cur:
                    row = await cur.fetchone()
            else:
                async with db.execute(
                    "SELECT COUNT(*) FROM chart_reference_extras"
                ) as cur:
                    row = await cur.fetchone()
            return row[0] if row else 0
    except Exception:
        return 0


# ── utdata importer ──────────────────────────────────────────────────────────

_UTDATA_CSV_URL = (
    "https://raw.githubusercontent.com/utdata/rwd-billboard-data/main/"
    "data-out/hot-100-current.csv"
)


async def _import_utdata_hot100():
    """
    Fetch the utdata Hot 100 CSV and collapse weekly rows into one row per
    (artist, title) keeping the best (lowest) peak_position and highest
    wks_on_chart we saw. Writes into chart_reference_extras with
    data_source='utdata_hot100' and chart_name='hot100'.
    """
    import csv
    import io

    inserted = 0
    skipped = 0

    try:
        async with httpx.AsyncClient(timeout=120.0, follow_redirects=True) as client:
            r = await client.get(_UTDATA_CSV_URL)
            r.raise_for_status()
            text = r.text
    except Exception as e:
        raise RuntimeError(f"Failed to fetch utdata CSV: {e}")

    reader = csv.DictReader(io.StringIO(text))

    # Collapse weekly rows: key = (artist_norm, title_norm)
    # Value: dict with best peak, max weeks, latest chart_year, display names
    aggregate: dict = {}
    for row in reader:
        try:
            performer = (row.get("performer") or "").strip()
            title     = (row.get("title") or "").strip()
            if not performer or not title:
                skipped += 1
                continue

            a_norm = _norm(performer)
            t_norm = _norm(title)
            if not a_norm or not t_norm:
                skipped += 1
                continue

            try:
                peak = int(row.get("peak_pos") or row.get("current_week") or 100)
            except (TypeError, ValueError):
                peak = 100
            try:
                weeks = int(row.get("wks_on_chart") or 1)
            except (TypeError, ValueError):
                weeks = 1

            chart_week = (row.get("chart_week") or "").strip()
            year = None
            if len(chart_week) >= 4:
                try:
                    year = int(chart_week[:4])
                except ValueError:
                    year = None

            key = (a_norm, t_norm)
            if key in aggregate:
                cur = aggregate[key]
                if peak < cur["peak_position"]:
                    cur["peak_position"] = peak
                if weeks > cur["weeks_on_chart"]:
                    cur["weeks_on_chart"] = weeks
                if year and (cur["chart_year"] is None or year > cur["chart_year"]):
                    cur["chart_year"] = year
            else:
                aggregate[key] = {
                    "artist":         performer,
                    "title":          title,
                    "artist_norm":    a_norm,
                    "title_norm":     t_norm,
                    "peak_position":  peak,
                    "weeks_on_chart": weeks,
                    "chart_year":     year,
                }
        except Exception:
            skipped += 1
            continue

    # Bulk insert (UPSERT-style via INSERT OR IGNORE + UPDATE-better-peak)
    try:
        async with aiosqlite.connect(_DYNAMIC_DB) as db:
            for v in aggregate.values():
                try:
                    cur = await db.execute(
                        """INSERT INTO chart_reference_extras
                           (chart_name, artist, title, artist_norm, title_norm,
                            peak_position, weeks_on_chart, chart_year, data_source)
                           VALUES ('hot100', ?, ?, ?, ?, ?, ?, ?, 'utdata_hot100')
                           ON CONFLICT(chart_name, artist_norm, title_norm, chart_year)
                           DO UPDATE SET
                               peak_position  = MIN(peak_position, excluded.peak_position),
                               weeks_on_chart = MAX(weeks_on_chart, excluded.weeks_on_chart)""",
                        (v["artist"], v["title"], v["artist_norm"], v["title_norm"],
                         v["peak_position"], v["weeks_on_chart"], v["chart_year"])
                    )
                    if cur.rowcount:
                        inserted += 1
                except Exception:
                    skipped += 1
            await db.commit()
    except Exception as e:
        raise RuntimeError(f"DB write failed: {e}")

    _vet_import_job["inserted"] = inserted
    _vet_import_job["skipped"]  = skipped
    _vet_import_job["message"]  = f"Imported {inserted:,} unique tracks ({skipped:,} rows skipped)."
    log.info(f"utdata import complete: +{inserted} inserted, {skipped} skipped")


# ── Shared helpers for new importers ─────────────────────────────────────────

async def _bulk_upsert_extras(rows: list, data_tag: str, chart_name: str) -> tuple:
    """
    Bulk INSERT OR UPSERT into chart_reference_extras.
    `rows` is a list of dicts each with: artist, title, artist_norm, title_norm,
    peak_position, weeks_on_chart (optional, default 1), chart_year (optional).
    Returns (inserted, skipped).
    """
    inserted = 0
    skipped = 0
    if not rows:
        return (0, 0)
    try:
        async with aiosqlite.connect(_DYNAMIC_DB) as db:
            for v in rows:
                try:
                    cur = await db.execute(
                        """INSERT INTO chart_reference_extras
                           (chart_name, artist, title, artist_norm, title_norm,
                            peak_position, weeks_on_chart, chart_year, data_source)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                           ON CONFLICT(chart_name, artist_norm, title_norm, chart_year)
                           DO UPDATE SET
                               peak_position  = MIN(peak_position, excluded.peak_position),
                               weeks_on_chart = MAX(weeks_on_chart, excluded.weeks_on_chart)""",
                        (chart_name, v["artist"], v["title"], v["artist_norm"], v["title_norm"],
                         v.get("peak_position") or 100,
                         v.get("weeks_on_chart") or 1,
                         v.get("chart_year"),
                         data_tag)
                    )
                    if cur.rowcount:
                        inserted += 1
                except Exception:
                    skipped += 1
            # Idempotent delete-then-reinsert was handled by UPSERT; no need for bulk DELETE.
            await db.commit()
    except Exception as e:
        raise RuntimeError(f"DB write failed: {e}")
    return (inserted, skipped)


async def _purge_source(data_tag: str) -> int:
    """Delete all rows with the given data_tag. Used before snapshot-style re-imports."""
    try:
        async with aiosqlite.connect(_DYNAMIC_DB) as db:
            cur = await db.execute(
                "DELETE FROM chart_reference_extras WHERE data_source=?", (data_tag,)
            )
            n = cur.rowcount
            await db.commit()
            return n
    except Exception:
        return 0


def _strip_html_tags(s: str) -> str:
    """Lightweight HTML tag stripper for scraped text cells."""
    return re.sub(r"<[^>]+>", "", s or "").replace("&amp;", "&").replace("&#039;", "'").replace("&quot;", '"').strip()


# ── Chart2000.com importer ───────────────────────────────────────────────────

_CHART2000_CANDIDATE_URLS = [
    # Chart2000.com serves an incomplete HTTPS certificate chain that most non-browser
    # clients reject. Using http:// here is safe: this is public chart data with no auth
    # or PII in transit, and the CSV is validated downstream via our parser + empty-result guard.
    "http://chart2000.com/data/chart2000-month-0-3-0050.csv",
    "http://chart2000.com/data/chart2000-items-0-3-0050.csv",
    "http://chart2000.com/data/chart2000-song-0-3-0050.csv",
]


async def _import_chart2000():
    """
    Fetch a Chart2000.com monthly CSV, collapse (artist, title) across months keeping
    best peak_position and accumulated weeks_on_chart. 2000–2024 global data.
    """
    import csv
    import io

    text = None
    last_err = None
    try:
        async with httpx.AsyncClient(timeout=120.0, follow_redirects=True) as client:
            for url in _CHART2000_CANDIDATE_URLS:
                try:
                    r = await client.get(url)
                    if r.status_code == 200 and r.text:
                        text = r.text
                        log.info(f"chart2000: fetched {url} ({len(text):,} bytes)")
                        break
                except Exception as e:
                    last_err = e
                    continue
    except Exception as e:
        raise RuntimeError(f"Failed to reach chart2000.com: {e}")

    if not text:
        raise RuntimeError(f"No chart2000 CSV reachable. Last error: {last_err}")

    reader = csv.DictReader(io.StringIO(text))
    aggregate: dict = {}
    skipped = 0
    for row in reader:
        try:
            # Chart2000 schema: artist,name,category,position,score,from,to,us,uk,de,fr,ca,au
            # `name` is the title column (NOT `title`). `category` is 'song' or 'album'.
            category = (row.get("category") or "").strip().lower()
            if category and category != "song":
                skipped += 1
                continue

            artist = (row.get("artist") or "").strip()
            title  = (row.get("name")   or "").strip()
            if not artist or not title:
                skipped += 1
                continue
            a_norm = _norm(artist)
            t_norm = _norm(title)
            if not a_norm or not t_norm:
                skipped += 1
                continue

            # Chart2000's 'position' is a global all-time ranking across 2000+ songs.
            # Raw position #1 is Gnarls Barkley "Crazy", #100 is deep chart territory.
            # For CH purposes (peak 1-100 = charted), map position into buckets.
            # Fall back to 'score' for rows without position (common).
            peak = None
            pos_raw = row.get("position")
            if pos_raw not in (None, ""):
                try:
                    p = int(float(pos_raw))
                    if   p <= 50:   peak = 1
                    elif p <= 200:  peak = 5
                    elif p <= 500:  peak = 15
                    elif p <= 1000: peak = 30
                    elif p <= 2000: peak = 60
                    else:           peak = 90
                except (TypeError, ValueError):
                    peak = None
            if peak is None:
                # No position → use score to estimate. Score is an indicative value;
                # real chart-toppers score in the tens of thousands.
                score_raw = row.get("score")
                if score_raw not in (None, ""):
                    try:
                        s = float(score_raw)
                        if   s >= 20000: peak = 5
                        elif s >= 10000: peak = 15
                        elif s >= 5000:  peak = 30
                        elif s >= 1500:  peak = 50
                        elif s >= 500:   peak = 75
                        else:            peak = 95
                    except (TypeError, ValueError):
                        peak = None
            if peak is None:
                peak = 100

            # Year from "from" field, which is "MMM YYYY" (e.g. "Oct 2006")
            year = None
            from_str = (row.get("from") or "").strip()
            m = re.search(r"(\d{4})", from_str)
            if m:
                try:
                    year = int(m.group(1))
                except (TypeError, ValueError):
                    year = None

            # Weeks on chart: months between 'from' and 'to', rough approximation
            weeks = 1
            to_str = (row.get("to") or "").strip()
            if from_str and to_str:
                try:
                    dt_from = datetime.strptime(from_str, "%b %Y")
                    dt_to   = datetime.strptime(to_str,   "%b %Y")
                    months = max(1, (dt_to.year - dt_from.year) * 12 + (dt_to.month - dt_from.month))
                    weeks = months * 4  # approximate weeks from months
                except Exception:
                    weeks = 1

            key = (a_norm, t_norm)
            if key in aggregate:
                cur = aggregate[key]
                if peak < cur["peak_position"]:
                    cur["peak_position"] = peak
                if weeks > cur["weeks_on_chart"]:
                    cur["weeks_on_chart"] = weeks
                if year and (cur["chart_year"] is None or year < cur["chart_year"]):
                    cur["chart_year"] = year  # earliest = chart entry year
            else:
                aggregate[key] = {
                    "artist": artist, "title": title,
                    "artist_norm": a_norm, "title_norm": t_norm,
                    "peak_position": peak, "weeks_on_chart": weeks, "chart_year": year,
                }
        except Exception:
            skipped += 1
            continue

    inserted, skipped_db = await _bulk_upsert_extras(
        list(aggregate.values()), "chart2000_monthly", "chart2000"
    )
    total_skipped = skipped + skipped_db
    _vet_import_job["inserted"] = inserted
    _vet_import_job["skipped"]  = total_skipped
    _vet_import_job["message"]  = f"Imported {inserted:,} unique tracks from Chart2000.com ({total_skipped:,} rows skipped)."
    log.info(f"chart2000 import complete: +{inserted} inserted, {total_skipped} skipped")


# ── tsort.info importer ──────────────────────────────────────────────────────

# tsort.info serves an incomplete HTTPS certificate chain (same issue as chart2000.com)
# that most non-browser clients reject. Using http:// here is safe: public chart data,
# no auth or PII in transit, CSV validated downstream via our parser + empty-result guard.
_TSORT_VERSION_URL = "http://tsort.info/music/faq_version_numbers.htm"
_TSORT_CHART_URL   = "http://tsort.info/tsort-chart-{version}.csv"
# Fallback list: newest first. tsort.info deletes old versions when new ones ship,
# so this needs periodic refresh. As of Apr 2026 the current version is 2-9-0001.
_TSORT_FALLBACK_VERSIONS = ["2-9-0001", "2-8-0050", "2-8-0044"]


async def _tsort_discover_version(client) -> Optional[str]:
    """Scrape the current version suffix from the FAQ page. Returns e.g. '2-8-0044'."""
    try:
        r = await client.get(_TSORT_VERSION_URL)
        if r.status_code != 200 or not r.text:
            return None
        # Look for "CSV File: tsort-chart-X-Y-ZZZZ.csv"
        m = re.search(r"tsort-chart-([0-9]+-[0-9]+-[0-9]+)\.csv", r.text)
        if m:
            return m.group(1)
    except Exception:
        return None
    return None


async def _import_tsort():
    """
    Fetch tsort.info full chart CSV (~71k rows, 1900+). Columns include artist, title,
    year, position, duration and more. Use position as peak; year as chart_year.
    """
    import csv
    import io

    text = None
    version_used = None
    try:
        async with httpx.AsyncClient(timeout=180.0, follow_redirects=True) as client:
            # Discover version, fall back to known-good if scrape fails
            versions_to_try = []
            v = await _tsort_discover_version(client)
            if v:
                versions_to_try.append(v)
            versions_to_try.extend([x for x in _TSORT_FALLBACK_VERSIONS if x not in versions_to_try])

            for ver in versions_to_try:
                try:
                    url = _TSORT_CHART_URL.format(version=ver)
                    r = await client.get(url)
                    if r.status_code == 200 and r.text:
                        text = r.text
                        version_used = ver
                        log.info(f"tsort: fetched version {ver} ({len(text):,} bytes)")
                        break
                except Exception:
                    continue
    except Exception as e:
        raise RuntimeError(f"Failed to reach tsort.info: {e}")

    if not text:
        raise RuntimeError("No tsort CSV reachable with any known version.")

    reader = csv.DictReader(io.StringIO(text))
    aggregate: dict = {}
    skipped = 0
    # Pre-compile regex for the notes field: "... peak 94 - Mar 2019 (2 weeks)"
    _peak_re  = re.compile(r"peak\s+(\d+)", re.IGNORECASE)
    _weeks_re = re.compile(r"\((\d+)\s*weeks?\)", re.IGNORECASE)
    for row in reader:
        try:
            # tsort schema: artist, name, type, year, score, songentry_pos, ..., notes
            # Skip albums early — save work
            rtype = (row.get("type") or "").strip().lower()
            if rtype == "album":
                skipped += 1
                continue

            artist = (row.get("artist") or "").strip()
            title  = (row.get("name")   or "").strip()  # tsort uses 'name' not 'title'
            if not artist or not title:
                skipped += 1
                continue
            a_norm = _norm(artist)
            t_norm = _norm(title)
            if not a_norm or not t_norm:
                skipped += 1
                continue

            notes = row.get("notes") or ""

            # Peak: prefer notes "peak NN", fall back to score bucketing
            peak = None
            m = _peak_re.search(notes)
            if m:
                try:
                    p = int(m.group(1))
                    if 1 <= p <= 100:
                        peak = p
                    elif p > 100:
                        peak = 100
                except (TypeError, ValueError):
                    peak = None
            if peak is None:
                # tsort 'score' is a cumulative chart-success value. Higher = more successful.
                score_raw = (row.get("score") or "").strip()
                if score_raw:
                    try:
                        s = float(score_raw)
                        if   s >= 100: peak = 1
                        elif s >= 30:  peak = 5
                        elif s >= 10:  peak = 15
                        elif s >= 3:   peak = 40
                        elif s >= 1:   peak = 70
                        else:          peak = 90
                    except (TypeError, ValueError):
                        peak = None
            if peak is None:
                peak = 100

            # Weeks from notes "(N weeks)"
            weeks = 1
            m = _weeks_re.search(notes)
            if m:
                try:
                    weeks = max(1, int(m.group(1)))
                except (TypeError, ValueError):
                    weeks = 1

            # Year: tsort has a 'year' column; value can be 'unknown'
            year = None
            yr_raw = (row.get("year") or "").strip()
            if yr_raw and yr_raw.lower() != "unknown":
                try:
                    year = int(yr_raw[:4])
                except (TypeError, ValueError):
                    year = None

            key = (a_norm, t_norm)
            if key in aggregate:
                cur = aggregate[key]
                if peak < cur["peak_position"]:
                    cur["peak_position"] = peak
                if weeks > cur["weeks_on_chart"]:
                    cur["weeks_on_chart"] = weeks
            else:
                aggregate[key] = {
                    "artist": artist, "title": title,
                    "artist_norm": a_norm, "title_norm": t_norm,
                    "peak_position": peak, "weeks_on_chart": weeks, "chart_year": year,
                }
        except Exception:
            skipped += 1
            continue

    inserted, skipped_db = await _bulk_upsert_extras(
        list(aggregate.values()), "tsort_historical", "tsort"
    )
    total_skipped = skipped + skipped_db
    _vet_import_job["inserted"] = inserted
    _vet_import_job["skipped"]  = total_skipped
    vers = f" (version {version_used})" if version_used else ""
    _vet_import_job["message"]  = f"Imported {inserted:,} unique tracks from tsort.info{vers} ({total_skipped:,} rows skipped)."
    log.info(f"tsort import complete: version={version_used}, +{inserted} inserted, {total_skipped} skipped")


# ── Kworb iTunes US importer (HTML scrape, current week only) ────────────────

_KWORB_US_URL = "https://kworb.net/charts/itunes/us.html"


async def _import_kworb_us():
    """
    Scrape Kworb's iTunes US top 100 page. Current snapshot only — wipes previous
    Kworb rows before inserting so each import reflects the latest chart.
    """

    try:
        async with httpx.AsyncClient(
            timeout=60.0, follow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0 (ChartHound music metadata tool)"}
        ) as client:
            r = await client.get(_KWORB_US_URL)
            r.raise_for_status()
            html = r.text
    except Exception as e:
        raise RuntimeError(f"Failed to fetch Kworb US: {e}")

    # Parse rows: each data row in a Kworb chart table has cells for rank, title, artist.
    # Anchor on <tr>…<td>…</td>…</tr> blocks inside the main table.
    rows_raw = re.findall(r"<tr[^>]*>(.*?)</tr>", html, flags=re.DOTALL | re.IGNORECASE)
    entries = []
    skipped = 0
    seen = set()
    rank = 0
    for tr in rows_raw:
        cells = re.findall(r"<td[^>]*>(.*?)</td>", tr, flags=re.DOTALL | re.IGNORECASE)
        # Kworb iTunes page layout: 3 cells per row:
        #   cell[0] = Pos (e.g. "1"), cell[1] = P+ change ("+1"/"NEW"/"-2"),
        #   cell[2] = "Artist - Title" (wrapped in <div>)
        if len(cells) < 3:
            continue
        combined = _strip_html_tags(cells[2])
        if not combined:
            continue

        artist = None
        title  = None
        # "Artist - Title" split. Use the FIRST " - " only; titles can contain " - " themselves.
        if " - " in combined:
            parts = combined.split(" - ", 1)
            artist = parts[0].strip()
            title  = parts[1].strip()
        else:
            skipped += 1
            continue

        if not artist or not title:
            skipped += 1
            continue
        a_norm = _norm(artist)
        t_norm = _norm(title)
        if not a_norm or not t_norm:
            skipped += 1
            continue

        key = (a_norm, t_norm)
        if key in seen:
            continue
        seen.add(key)
        rank += 1
        if rank > 100:
            break
        entries.append({
            "artist": artist, "title": title,
            "artist_norm": a_norm, "title_norm": t_norm,
            "peak_position": rank, "weeks_on_chart": 1,
            "chart_year": datetime.utcnow().year,
        })

    # Snapshot-style: wipe prior rows so count reflects current chart exactly
    await _purge_source("kworb_itunes_us")

    if not entries:
        raise RuntimeError(
            "Kworb HTML parsed but 0 chart entries extracted. "
            "Page structure may have changed — please open an issue with the current HTML."
        )

    inserted, skipped_db = await _bulk_upsert_extras(entries, "kworb_itunes_us", "kworb_us")
    total_skipped = skipped + skipped_db

    _vet_import_job["inserted"] = inserted
    _vet_import_job["skipped"]  = total_skipped
    _vet_import_job["message"]  = f"Imported {inserted:,} entries from Kworb iTunes US ({total_skipped:,} rows skipped)."
    log.info(f"kworb_us import complete: +{inserted} inserted, {total_skipped} skipped")


# ── Billboard year-end importers (CCM, Country, R&B, Rock, Dance, Adult Pop) ──

async def _import_billboard_yearend(
    billboard_slug: str,
    data_tag: str,
    chart_name: str,
    label: str,
    start_year: int = 1990,
    fallback_slug: str = "",
) -> None:
    """
    Shared helper: fetch Billboard year-end charts (start_year → current) + current
    week via billboard.py, collapse to one row per (artist, title) keeping best peak
    and max weeks, then upsert into chart_reference_extras.

    billboard.py is sync I/O — every call is wrapped in asyncio.to_thread().
    Rate-limited to 1 req/sec to be polite. Skips years that 404 silently.
    """
    import billboard as bb  # type: ignore


    current_year = datetime.utcnow().year
    songs: dict = {}

    slugs = [s for s in (billboard_slug, fallback_slug) if s]   # Billboard renamed some charts in 2026

    def _fetch(path_fmt: str):
        for s in slugs:
            try:
                c = bb.ChartData(path_fmt.format(s), timeout=25)
                if c and len(c):
                    return c
            except Exception:
                pass
        return None

    def _fetch_yearend(year: int):
        return _fetch(f"year-end/{year}/{{}}")

    def _fetch_current():
        return _fetch("{}")

    # ── Year-end loop ─────────────────────────────────────────────────────────
    for year in range(start_year, current_year + 1):
        chart = await asyncio.to_thread(_fetch_yearend, year)
        if not chart:
            await asyncio.sleep(0.5)
            continue
        count = 0
        for entry in chart:
            artist = (entry.artist or "").strip()
            title  = (entry.title  or "").strip()
            if not artist or not title:
                continue
            a_n = _norm(artist)
            t_n = _norm(title)
            if not a_n or not t_n:
                continue
            peak  = entry.peakPos or entry.rank or 100
            weeks = entry.weeks   or 1
            key   = (a_n, t_n, year)  # per-year key: one row per song per year
            if key not in songs:
                songs[key] = {
                    "artist": artist, "title": title,
                    "artist_norm": a_n, "title_norm": t_n,
                    "peak_position": peak, "weeks_on_chart": weeks,
                    "chart_year": year,
                }
            else:
                if peak  < songs[key]["peak_position"]:  songs[key]["peak_position"]  = peak
                if weeks > songs[key]["weeks_on_chart"]: songs[key]["weeks_on_chart"] = weeks
            count += 1
        log.info(f"billboard yearend {label} {year}: {count} entries")
        await asyncio.sleep(1.0)

    # ── Current week ──────────────────────────────────────────────────────────
    chart = await asyncio.to_thread(_fetch_current)
    if chart:
        for entry in chart:
            artist = (entry.artist or "").strip()
            title  = (entry.title  or "").strip()
            if not artist or not title:
                continue
            a_n = _norm(artist)
            t_n = _norm(title)
            if not a_n or not t_n:
                continue
            peak  = entry.rank or 100
            weeks = entry.weeks or 1
            key   = (a_n, t_n, current_year)  # per-year key
            if key not in songs:
                songs[key] = {
                    "artist": artist, "title": title,
                    "artist_norm": a_n, "title_norm": t_n,
                    "peak_position": peak, "weeks_on_chart": weeks,
                    "chart_year": current_year,
                }
            else:
                if peak < songs[key]["peak_position"]: songs[key]["peak_position"] = peak
        log.info(f"billboard current {label}: {len(chart)} entries")

    if not songs:
        raise RuntimeError(f"billboard.py returned 0 entries for {label} ({billboard_slug}). Check slug or network.")

    await _purge_source(data_tag)
    entries = list(songs.values())
    inserted, skipped = await _bulk_upsert_extras(entries, data_tag, chart_name)
    total_rows = len(entries)

    _vet_import_job["inserted"] = total_rows
    _vet_import_job["skipped"]  = skipped
    _vet_import_job["message"]  = (
        f"Imported {total_rows:,} entries from {label} "
        f"({start_year}–{current_year} year-end + current week). "
        f"{skipped:,} rows skipped."
    )
    log.info(f"{data_tag} import complete: {total_rows} total rows, {skipped} skipped")


async def _import_ccm():
    await _import_billboard_yearend(
        billboard_slug="hot-christian-songs",
        fallback_slug="christian-songs",
        data_tag="ccm_weekly",
        chart_name="ccm",
        label="Billboard Christian Songs",
        start_year=1990,
    )


async def _import_country():
    await _import_billboard_yearend(
        billboard_slug="hot-country-songs",
        fallback_slug="country-songs",
        data_tag="country_yearend",
        chart_name="country",
        label="Billboard Hot Country Songs",
        start_year=1990,
    )


async def _import_rnb():
    await _import_billboard_yearend(
        billboard_slug="hot-r-and-b-hip-hop-songs",
        fallback_slug="r-b-hip-hop-songs",
        data_tag="rnb_yearend",
        chart_name="rnb",
        label="Billboard R&B/Hip-Hop Songs",
        start_year=1990,
    )


async def _import_rock():
    await _import_billboard_yearend(
        billboard_slug="hot-rock-songs",
        data_tag="rock_yearend",
        chart_name="rock",
        label="Billboard Hot Rock Songs",
        start_year=1990,
    )


async def _import_dance():
    await _import_billboard_yearend(
        billboard_slug="hot-dance-electronic-songs",
        fallback_slug="dance-electronic-songs",
        data_tag="dance_yearend",
        chart_name="dance",
        label="Billboard Dance/Electronic Songs",
        start_year=1990,
    )


async def _import_adultpop():
    await _import_billboard_yearend(
        billboard_slug="pop-songs",
        data_tag="adultpop_yearend",
        chart_name="adultpop",
        label="Billboard Pop Songs (Adult Pop)",
        start_year=1990,
    )





# ── UK Official Charts scraper ───────────────────────────────────────────────

_UK_OFFICIAL_URL = "https://www.officialcharts.com/charts/singles-chart/"


async def _import_uk_official():
    """
    Scrape OfficialCharts.com top 100 UK singles for the current week.
    Snapshot-style: wipes prior rows before insert.
    """

    try:
        async with httpx.AsyncClient(
            timeout=60.0, follow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0 (ChartHound music metadata tool)"}
        ) as client:
            r = await client.get(_UK_OFFICIAL_URL)
            r.raise_for_status()
            html = r.text
    except Exception as e:
        raise RuntimeError(f"Failed to fetch UK Official Charts: {e}")

    # OfficialCharts.com chart pages contain alternating <a href> links in document order:
    #   /songs/ARTIST-SLUG-TITLE-SLUG/      (links to the song page)
    #   /artist/[ID/]ARTIST-SLUG/           (links to the artist page)
    # Each chart row emits this pair. Href slugs are more stable than visible-HTML layout
    # (which has been redesigned multiple times) so we parse slugs directly.
    entries = []
    skipped = 0

    # Extract all song-slug and artist-slug href values in document order.
    # Match in a single scan so ordering is preserved.
    href_pattern = re.compile(
        r'href="(/songs/([^/"]+)/|/artist/(?:\d+/)?([^/"]+)/)"',
        flags=re.IGNORECASE
    )

    # Pair them up: walk linearly, when we see a song slug save it,
    # on the next artist slug emit (song, artist) and reset.
    pending_song = None
    pairs = []
    for m in href_pattern.finditer(html):
        song_slug   = m.group(2)
        artist_slug = m.group(3)
        if song_slug:
            pending_song = song_slug
        elif artist_slug and pending_song:
            pairs.append((pending_song, artist_slug))
            pending_song = None

    def _deslug(s: str) -> str:
        """'justin-bieber-ft-nicki-minaj' -> 'Justin Bieber Ft Nicki Minaj'."""
        if not s:
            return ""
        words = [w for w in s.replace("_", "-").split("-") if w]
        return " ".join(w.capitalize() for w in words)

    rank = 0
    seen = set()
    for song_slug, artist_slug in pairs:
        # Title is the song slug with the artist slug prefix stripped.
        # e.g. song="justin-bieber-daisies", artist="justin-bieber" -> title_slug="daisies"
        title_slug = song_slug
        if song_slug.lower().startswith(artist_slug.lower() + "-"):
            title_slug = song_slug[len(artist_slug) + 1:]
        elif song_slug.lower() == artist_slug.lower():
            # Title equals artist slug? skip — malformed pair
            skipped += 1
            continue

        artist = _deslug(artist_slug)
        title  = _deslug(title_slug)
        if not artist or not title:
            skipped += 1
            continue

        a_norm = _norm(artist)
        t_norm = _norm(title)
        if not a_norm or not t_norm:
            skipped += 1
            continue

        key = (a_norm, t_norm)
        if key in seen:
            continue
        seen.add(key)
        rank += 1
        if rank > 100:
            break
        entries.append({
            "artist": artist, "title": title,
            "artist_norm": a_norm, "title_norm": t_norm,
            "peak_position": rank, "weeks_on_chart": 1,
            "chart_year": datetime.utcnow().year,
        })

    await _purge_source("uk_official_singles")

    if not entries:
        raise RuntimeError(
            "UK Official HTML parsed but 0 chart entries extracted. "
            "Page structure may have changed — please open an issue with the current HTML."
        )

    inserted, skipped_db = await _bulk_upsert_extras(entries, "uk_official_singles", "uk_official")
    total_skipped = skipped + skipped_db

    _vet_import_job["inserted"] = inserted
    _vet_import_job["skipped"]  = total_skipped
    _vet_import_job["message"]  = f"Imported {inserted:,} entries from UK Official Charts ({total_skipped:,} rows skipped)."
    log.info(f"uk_official import complete: +{inserted} inserted, {total_skipped} skipped")


# ── ListenBrainz Historical importer ─────────────────────────────────────────
# Pipeline per genre/decade:
#   1. MusicBrainz search tag:{genre} AND date:[YYYY TO YYYY] → mbid, artist, title, year
#   2. Batch MBIDs → ListenBrainz /1/popularity/recording → total_listen_count
#   3. Sort desc by listen_count, keep top _LBZ_TOP_N per genre/decade
#   4. Upsert into chart_reference_extras (chart_name=genre, chart_year=release year)
# Covers pre-1990 only — post-1990 handled by billboard year-end importers.
# No API key required. Rate-limited: 1 req/sec MBZ, 0.5s LBZ.

_MBZ_BASE           = "https://musicbrainz.org/ws/2/recording"
_LBZ_POP_BASE       = "https://api.listenbrainz.org/1/popularity/recording"
_MBZ_HEADERS        = {"User-Agent": "ChartHound/2.0 (self-hosted music library; charthound@localhost)"}
_MBZ_MAX_PER_DECADE = 500   # max recordings to fetch from MBZ per genre/decade
_LBZ_BATCH_SIZE     = 50    # LBZ batch size (keep URLs short)
_LBZ_TOP_N          = 150   # top N to store per genre/decade

_LBZ_GENRE_TAGS = {
    # Billboard-covered genres — pre-1990 only (post-1990 handled by year-end importers)
    "rock":        ("rock",        [(1950,1959),(1960,1969),(1970,1979),(1980,1989)]),
    "rnb":         ("r&b",         [(1950,1959),(1960,1969),(1970,1979),(1980,1989)]),
    "country":     ("country",     [(1950,1959),(1960,1969),(1970,1979),(1980,1989)]),
    "dance":       ("electronic",  [(1950,1959),(1960,1969),(1970,1979),(1980,1989)]),
    # Last.fm-only genres — all applicable decades (no Billboard coverage)
    "hiphop":      ("hip hop",     [(1970,1979),(1980,1989),(1990,1999),(2000,2009),(2010,2019),(2020,2026)]),
    "metal":       ("heavy metal", [(1970,1979),(1980,1989),(1990,1999),(2000,2009),(2010,2019),(2020,2026)]),
    "alternative": ("alternative", [(1980,1989),(1990,1999),(2000,2009),(2010,2019),(2020,2026)]),
    "indie":       ("indie",       [(1990,1999),(2000,2009),(2010,2019),(2020,2026)]),
    "folk":        ("folk",        [(1950,1959),(1960,1969),(1970,1979),(1980,1989),(1990,1999),(2000,2009),(2010,2019),(2020,2026)]),
    "jazz":        ("jazz",        [(1920,1929),(1930,1939),(1940,1949),(1950,1959),(1960,1969),(1970,1979),(1980,1989),(1990,1999),(2000,2009),(2010,2019)]),
    "blues":       ("blues",       [(1920,1929),(1930,1939),(1940,1949),(1950,1959),(1960,1969),(1970,1979),(1980,1989),(1990,1999),(2000,2009)]),
    "electronic":  ("electronic",  [(1980,1989),(1990,1999),(2000,2009),(2010,2019),(2020,2026)]),
}


async def _mbz_search_decade(client: httpx.AsyncClient, tag: str, year_from: int, year_to: int) -> list:
    """Page MusicBrainz recording search for genre tag + decade. Returns list of dicts."""
    results = []
    offset  = 0
    page_sz = 100
    while len(results) < _MBZ_MAX_PER_DECADE:
        try:
            r = await client.get(
                _MBZ_BASE,
                params={
                    "query":  f"tag:{tag} AND date:[{year_from} TO {year_to}]",
                    "limit":  page_sz,
                    "offset": offset,
                    "fmt":    "json",
                },
                timeout=30,
            )
            if r.status_code == 503:
                await asyncio.sleep(5)
                continue
            r.raise_for_status()
            data       = r.json()
            recordings = data.get("recordings", [])
            if not recordings:
                break
            for rec in recordings:
                mbid  = rec.get("id", "")
                title = (rec.get("title") or "").strip()
                date  = (rec.get("first-release-date") or "").strip()
                credits = rec.get("artist-credit", [])
                artist  = " ".join(
                    a.get("name", "") for a in credits
                    if isinstance(a, dict) and "name" in a
                ).strip()
                if not mbid or not title or not artist:
                    continue
                year = None
                if date and len(date) >= 4 and date[:4].isdigit():
                    year = int(date[:4])
                results.append({"mbid": mbid, "artist": artist, "title": title, "year": year})
            offset += len(recordings)
            if offset >= data.get("count", 0):
                break
            await asyncio.sleep(1.0)
        except Exception as e:
            log.warning(f"MBZ search error (tag={tag} {year_from}-{year_to} offset={offset}): {e}")
            break
    return results


async def _lbz_listen_counts(client: httpx.AsyncClient, mbids: list) -> dict:
    """Batch-fetch ListenBrainz listen counts. Returns {mbid: total_listen_count}."""
    counts = {}
    for i in range(0, len(mbids), _LBZ_BATCH_SIZE):
        batch = mbids[i : i + _LBZ_BATCH_SIZE]
        try:
            r = await client.post(
                _LBZ_POP_BASE,
                json={"recording_mbids": batch},
                timeout=30,
            )
            r.raise_for_status()
            for item in r.json():
                mid = item.get("recording_mbid")
                lc  = item.get("total_listen_count")
                if mid and lc is not None:
                    counts[mid] = lc
        except Exception as e:
            log.warning(f"LBZ batch error (batch {i}): {e}")
        await asyncio.sleep(0.5)
    return counts


async def _import_listenbrainz_historical():
    """
    Fetch pre-1990 popularity data for rock/rnb/country/dance via MusicBrainz +
    ListenBrainz. Stores top 150 tracks per genre/decade by listen count.
    Fully automated — runs on weekly scheduler, no user config needed.
    """

    entries_by_genre: dict = {g: [] for g in _LBZ_GENRE_TAGS}

    async with httpx.AsyncClient(headers=_MBZ_HEADERS, follow_redirects=True) as client:
        for genre, (tag, decades) in _LBZ_GENRE_TAGS.items():
            for year_from, year_to in decades:
                label = f"{genre} {year_from}s"
                _vet_import_job["message"] = f"Fetching {label} from MusicBrainz..."
                log.info(f"listenbrainz_historical: querying {label}")

                recordings = await _mbz_search_decade(client, tag, year_from, year_to)
                if not recordings:
                    log.warning(f"listenbrainz_historical: 0 MBZ results for {label}")
                    continue
                log.info(f"listenbrainz_historical: {len(recordings)} MBZ results for {label}")

                _vet_import_job["message"] = f"Fetching listen counts for {label}..."
                mbids  = [r["mbid"] for r in recordings]
                counts = await _lbz_listen_counts(client, mbids)

                ranked = [
                    {**rec, "listen_count": counts.get(rec["mbid"], 0)}
                    for rec in recordings
                    if counts.get(rec["mbid"], 0) > 0
                ]
                ranked.sort(key=lambda x: x["listen_count"], reverse=True)
                top = ranked[:_LBZ_TOP_N]
                log.info(f"listenbrainz_historical: {len(top)} kept for {label} "
                         f"(top={top[0]['listen_count'] if top else 0:,} listens)")

                for rank, rec in enumerate(top, start=1):
                    a_n = _norm(rec["artist"])
                    t_n = _norm(rec["title"])
                    if not a_n or not t_n:
                        continue
                    year = rec.get("year")
                    if not year or not (year_from <= year <= year_to):
                        year = (year_from + year_to) // 2
                    entries_by_genre[genre].append({
                        "artist":        rec["artist"],
                        "title":         rec["title"],
                        "artist_norm":   a_n,
                        "title_norm":    t_n,
                        "peak_position": rank,
                        "weeks_on_chart": 1,
                        "chart_year":    year,
                    })

    total_count = sum(len(v) for v in entries_by_genre.values())
    if total_count == 0:
        raise RuntimeError("ListenBrainz historical import: 0 entries — check MBZ/LBZ connectivity.")

    await _purge_source("listenbrainz_historical")

    inserted_total = 0
    skipped_total  = 0
    for genre_name, entries in entries_by_genre.items():
        if not entries:
            continue
        ins, skp = await _bulk_upsert_extras(entries, "listenbrainz_historical", genre_name)
        inserted_total += ins
        skipped_total  += skp

    _vet_import_job["inserted"] = inserted_total
    _vet_import_job["skipped"]  = skipped_total
    _vet_import_job["message"]  = (
        f"ListenBrainz historical complete: {inserted_total:,} entries across "
        f"{len(_LBZ_GENRE_TAGS)} genres. "
        f"{skipped_total:,} skipped."
    )
    log.info(f"listenbrainz_historical complete: {inserted_total} inserted, {skipped_total} skipped")


# Map id → importer function (populated AFTER functions are defined)
_IMPORTER_MAP = {
    "utdata":                  _import_utdata_hot100,
    "chart2000":               _import_chart2000,
    "tsort":                   _import_tsort,
    "kworb_us":                _import_kworb_us,
    "ccm":                     _import_ccm,
    "country":                 _import_country,
    "rnb":                     _import_rnb,
    "rock":                    _import_rock,
    "dance":                   _import_dance,
    "adultpop":                _import_adultpop,
    "uk_official":             _import_uk_official,
    "listenbrainz_historical": _import_listenbrainz_historical,
}


async def _run_import(source_id: str):
    """Background task — updates _vet_import_job as it runs."""
    importer = _IMPORTER_MAP.get(source_id)
    if not importer:
        _vet_import_job.update({
            "status": "error",
            "message": f"No importer registered for '{source_id}'.",
            "finished_at": datetime.utcnow().isoformat(),
        })
        return
    try:
        await _ensure_extras_table()
        await importer()
        _vet_import_job.update({
            "status": "done",
            "finished_at": datetime.utcnow().isoformat(),
        })
    except Exception as e:
        log.error(f"Import '{source_id}' failed: {e}")
        _vet_import_job.update({
            "status": "error",
            "message": f"Import failed: {e}",
            "finished_at": datetime.utcnow().isoformat(),
        })


# ── ENDPOINTS ────────────────────────────────────────────────────────────────

async def _latest_import_at(data_tag: str) -> Optional[str]:
    """Return ISO timestamp of most recently added row for this data_tag, or None."""
    try:
        async with aiosqlite.connect(_DYNAMIC_DB) as db:
            async with db.execute(
                "SELECT MAX(added_at) FROM chart_reference_extras WHERE data_source=?",
                (data_tag,)
            ) as cur:
                row = await cur.fetchone()
                return row[0] if row and row[0] else None
    except Exception:
        return None


async def _get_setting(key: str) -> Optional[str]:
    try:
        async with aiosqlite.connect(_DYNAMIC_DB) as db:
            row = await (await db.execute("SELECT value FROM app_settings WHERE key=?", (key,))).fetchone()
            return row[0] if row else None
    except Exception:
        return None


async def _set_setting(key: str, value: Optional[str]) -> None:
    try:
        async with aiosqlite.connect(_DYNAMIC_DB) as db:
            if value is None:
                await db.execute("DELETE FROM app_settings WHERE key=?", (key,))
            else:
                await db.execute(
                    "INSERT INTO app_settings (key, value, updated_at) VALUES (?, ?, datetime('now')) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                    (key, value))
            await db.commit()
    except Exception as e:
        log.warning(f"setting write failed ({key}): {e}")


def _is_stale(iso_ts: Optional[str], days: int = 30) -> bool:
    """True if the timestamp is older than `days` days. Returns False if timestamp missing."""
    if not iso_ts:
        return False
    try:
        # Handle both 'YYYY-MM-DD HH:MM:SS' (SQLite default) and ISO-8601 with 'T'
        ts = iso_ts.replace("T", " ").split(".")[0]
        dt = datetime.strptime(ts, "%Y-%m-%d %H:%M:%S")
        age = datetime.utcnow() - dt
        return age.days > days
    except Exception:
        return False


@router.get("/vet/static-sources")
async def vet_static_sources(_=Depends(require_auth)):
    """Return catalog of importable sources with loaded status + entry counts + staleness."""
    await _ensure_extras_table()
    out = []
    for src in _STATIC_SOURCES:
        count = await _count_extras(src["data_tag"])
        last_at = await _latest_import_at(src["data_tag"]) if count > 0 else None
        archive = bool(src.get("archive"))
        fail_at = await _get_setting(f"refresh_fail_{src['id']}")
        failing = bool(fail_at) and _utc_epoch(fail_at) > _utc_epoch(last_at)
        out.append({
            "id":            src["id"],
            "label":         src["label"],
            "loaded":        count > 0,
            "entries":       count,
            "last_imported": last_at,
            "archive":       archive,
            "failing":       failing,
            "last_failed":   fail_at if failing else None,
            "stale":         (not archive) and (not failing) and _is_stale(last_at, days=30),
        })
    total_extras = await _count_extras()
    return {"sources": out, "total_extras": total_extras}


class StaticImportRequest(BaseModel):
    source_id: str


@router.post("/vet/static-sources/import")
async def vet_static_sources_import(
    req: StaticImportRequest,
    background_tasks: BackgroundTasks,
    _=Depends(require_auth)
):
    """Kick off an import. Only one runs at a time."""
    global _vet_import_job
    if _vet_import_job["status"] == "running":
        raise HTTPException(409, "An import is already running. Wait for it to finish.")
    if req.source_id not in _IMPORTER_MAP:
        raise HTTPException(400, f"Unknown source_id '{req.source_id}'.")
    _vet_import_job = {
        "status":      "running",
        "source_id":   req.source_id,
        "inserted":    0,
        "skipped":     0,
        "message":     "Starting import…",
        "started_at":  datetime.utcnow().isoformat(),
        "finished_at": None,
    }
    # Fire-and-forget background task (same pattern as Sniffer/Tracker)
    asyncio.create_task(_run_import(req.source_id))
    return {"ok": True, "source_id": req.source_id, "message": "Import started."}


@router.get("/vet/static-sources/status")
async def vet_static_sources_status(_=Depends(require_auth)):
    """Poll current import job."""
    return dict(_vet_import_job)


class StaticSourceDeleteRequest(BaseModel):
    source_id: str


@router.post("/vet/static-sources/delete")
async def vet_static_sources_delete(
    req: StaticSourceDeleteRequest,
    _=Depends(require_auth)
):
    """Wipe all rows for a given imported source. Safety valve if an import went bad."""
    src = next((s for s in _STATIC_SOURCES if s["id"] == req.source_id), None)
    if not src:
        raise HTTPException(400, f"Unknown source_id '{req.source_id}'.")
    try:
        async with aiosqlite.connect(_DYNAMIC_DB) as db:
            cur = await db.execute(
                "DELETE FROM chart_reference_extras WHERE data_source=?",
                (src["data_tag"],)
            )
            deleted = cur.rowcount
            await db.commit()
        log.info(f"Deleted {deleted} rows for source '{req.source_id}'")
        return {"ok": True, "deleted": deleted}
    except Exception as e:
        raise HTTPException(500, f"Delete failed: {e}")


# ══════════════════════════════════════════════════════════════════════════════
#  WEEKLY AUTO-REFRESH SCHEDULER
#
#  On startup, checks each "snapshot-style" source (current-week scrapers).
#  If the source has never been imported, or was last imported more than 7 days
#  ago, it re-runs the import automatically in the background.
#
#  Sources included in auto-refresh (current-week snapshots that go stale):
#    - kworb_us      (iTunes US current week)
#    - ccm           (Billboard Christian Songs current week)
#    - country       (Billboard Hot Country Songs current week)
#    - uk_official   (UK Official Charts current week)
#
#  Historical bulk sources (utdata, chart2000, tsort) are NOT auto-refreshed —
#  they are large downloads and only need to be re-run manually when new data
#  has been published (roughly monthly/quarterly).
# ══════════════════════════════════════════════════════════════════════════════

_AUTO_REFRESH_SOURCES = ["kworb_us", "ccm", "country", "rnb", "rock", "dance", "adultpop", "uk_official", "listenbrainz_historical"]
_REFRESH_INTERVAL_DAYS = 7


async def _scheduler_loop():
    """
    Background loop: checks auto-refresh sources every 6 hours.
    If a source is missing or older than _REFRESH_INTERVAL_DAYS, re-imports it.
    Skips if another import is already running.
    """
    global _vet_import_job
    await asyncio.sleep(30)  # brief startup delay so the app is fully ready

    while True:
        try:
            for source_id in _AUTO_REFRESH_SOURCES:
                src = next((s for s in _STATIC_SOURCES if s["id"] == source_id), None)
                if not src:
                    continue

                count = await _count_extras(src["data_tag"])
                last_at = await _latest_import_at(src["data_tag"]) if count > 0 else None
                needs_refresh = (count == 0) or _is_stale(last_at, days=_REFRESH_INTERVAL_DAYS)

                if not needs_refresh:
                    continue
                # Backoff: a failed auto-refresh waits a full interval before retrying
                fail_at = await _get_setting(f"refresh_fail_{source_id}")
                if fail_at and (time.time() - _utc_epoch(fail_at)) < _REFRESH_INTERVAL_DAYS * 86400:
                    continue

                # Don't pile on top of a running manual import
                if _vet_import_job.get("status") == "running":
                    log.info(f"Scheduler: skipping auto-refresh of '{source_id}' — import already running")
                    continue

                log.info(f"Scheduler: auto-refreshing '{source_id}' (last import: {last_at or 'never'})")
                _vet_import_job = {
                    "status":      "running",
                    "source_id":   source_id,
                    "inserted":    0,
                    "skipped":     0,
                    "message":     f"Auto-refresh: {src['label']}",
                    "started_at":  datetime.utcnow().isoformat(),
                    "finished_at": None,
                }
                await _run_import(source_id)
                if _vet_import_job.get("status") == "done":
                    await _set_setting(f"refresh_fail_{source_id}", None)
                else:
                    await _set_setting(f"refresh_fail_{source_id}",
                                       datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"))
                    log.info(f"Scheduler: '{source_id}' failed — next retry in {_REFRESH_INTERVAL_DAYS} days")

                # Small gap between sources so we don't hammer scrapers back-to-back
                await asyncio.sleep(10)

        except Exception as e:
            log.warning(f"Scheduler loop error: {e}")

        # Check again in 6 hours
        await asyncio.sleep(6 * 60 * 60)


async def _library_cache_scheduler():
    """
    Auto-populate and refresh library_cache from physical files.
    - On startup: if cache is empty run a full sync immediately (first install).
    - Otherwise: sleep until next 3am local time, then run incremental sync.
    - Repeats every 24h at 3am.
    """
    from app.routers.sniffer import _do_sync_library, _sync_state

    await asyncio.sleep(60)  # wait for app to be fully ready

    # ── First-run check: if cache is empty do a full sync now ─────────────────
    try:
        cache_empty = True
        async with aiosqlite.connect(_DYNAMIC_DB) as db:
            async with db.execute("SELECT COUNT(*) FROM library_cache") as cur:
                row = await cur.fetchone()
                cache_empty = (not row or row[0] == 0)
        if cache_empty:
            log.info("Library cache scheduler: cache empty on startup — running full sync")
            await _do_sync_library(incremental=False)
        else:
            log.info("Library cache scheduler: cache populated, will sync at next 3am")
    except Exception as e:
        log.warning(f"Library cache scheduler startup check error: {e}")

    # ── Nightly 3am loop ──────────────────────────────────────────────────────
    while True:
        try:
            # Calculate seconds until next 3:00am local time
            now = datetime.now()
            next_3am = now.replace(hour=3, minute=0, second=0, microsecond=0)
            if now >= next_3am:
                next_3am = next_3am + timedelta(days=1)
            sleep_secs = (next_3am - now).total_seconds()
            log.info(f"Library cache scheduler: next sync at 3am ({sleep_secs/3600:.1f}h away)")
            await asyncio.sleep(sleep_secs)

            # Run incremental sync at 3am
            if _sync_state.get("status") != "running":
                log.info("Library cache scheduler: running nightly incremental sync at 3am")
                await _do_sync_library(incremental=True)
            else:
                log.info("Library cache scheduler: sync already running, skipping nightly")

        except Exception as e:
            log.warning(f"Library cache scheduler error: {e}")
            await asyncio.sleep(3600)  # retry in 1h if something goes wrong


