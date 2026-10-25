"""
ChartHound — The Retriever Router
Genre Engine: one family + up to 3 styles per song (Discogs · MusicBrainz · Last.fm vote),
review/override, folder rules, file-first GENRE writes. Plus browse, cache clear, write log.
"""

import asyncio
import json
import logging
import os
import re
import time
import aiosqlite
import httpx

from typing import Optional, List
from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks, Request
from fastapi.responses import Response, StreamingResponse
import jwt
from jwt import PyJWTError as JWTError
from pydantic import BaseModel

from app.config import get_settings
from app.deps import require_auth

log = logging.getLogger("charthound.retriever")
router = APIRouter(prefix="/api/retriever", tags=["retriever"])
settings = get_settings()


# ── Path containment helper (C2/C3/H1 FIX) ───────────────────────────────────
def _is_within_music_prefix(p: str) -> bool:
    """
    Return True iff `p` resolves to a path inside settings.docker_music_prefix
    (or equals it exactly). Defends against:
      - absolute paths outside /music (e.g. "/etc")
      - sibling-dir bypass via plain startswith (e.g. "/musique" vs "/music")
      - "../" traversal
      - symlinks pointing outside the music tree
    """
    try:
        base = os.path.realpath(settings.docker_music_prefix)
        target = os.path.realpath(p)
    except Exception:
        return False
    if target == base:
        return True
    return target.startswith(base + os.sep)
# ─────────────────────────────────────────────────────────────────────────────

# ── Rate Limiter (MusicBrainz 1 req/s) ─────────────────────────────────────


class MusicBrainzLimiter:
    """MusicBrainz allows ~1 request/second per client; faster gets silently 503'd."""
    def __init__(self, interval: float = 1.1):
        self.interval = interval
        self._lock = asyncio.Lock()
        self._last = 0.0

    async def acquire(self):
        async with self._lock:
            wait = self._last + self.interval - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            self._last = time.monotonic()


mb_limiter = MusicBrainzLimiter()

# ── Chart display map ─────────────────────────────────────────────────────────
CHART_DISPLAY = {
    "hot100":   "Hot 100",       "adultpop": "Adult Pop",
    "ac":       "Adult Contemp", "uk":       "UK Singles",
    "country":  "Country",       "rnb":      "R&B/Hip-Hop",
    "dance":    "Dance",         "rock":     "Mainstream Rock",
    "ccm":      "CCM",           "ccm-ac":   "CCM-AC",
    "ccm-rock": "CCM Rock",      "worship":  "Worship",
    "gospel":   "Gospel",        "sgospel":  "Southern Gospel",
    "ugospel":  "Urban Gospel",  "tgospel":  "Traditional Gospel",
}


# ── Fix B: Hard genre blacklist — these NEVER get written to music files ──────
# These genres are valid on MusicBrainz/Discogs but wrong for 99% of music libraries.

# ── Artist genre fingerprint ──────────────────────────────────────────────────


# ── Fix D: Genre-tag → Mood mapping (covers mainstream Last.fm tags) ──────────
# Last.fm tags for most tracks are genre words, not mood words.
# This maps common genre tags to the mood they imply.

# ── Pydantic Models ───────────────────────────────────────────────────────────


# ══════════════════════════════════════════════════════════════════════════════
#  TAG READING — Read actual file tags first, path fallback second
# ══════════════════════════════════════════════════════════════════════════════


# ══════════════════════════════════════════════════════════════════════════════
#  WATERFALL ENGINE
# ══════════════════════════════════════════════════════════════════════════════


def format_chart_comment(chart_entries: List[dict]) -> str:
    if not chart_entries:
        return ""
    parts = []
    for entry in chart_entries:
        name = CHART_DISPLAY.get(entry.get("chart_name", ""), entry.get("chart_name", ""))
        peak = entry.get("peak_position")
        weeks = entry.get("weeks_on_chart", 0)
        part = f"{name}: #{peak}" if peak else name
        if peak and weeks:
            part += f" ({weeks} wks)"
        parts.append(part)
    return " | ".join(parts)


def peak_to_stars(peak: Optional[int]) -> int:
    if not peak: return 0
    if peak <= 3:  return 5
    if peak <= 10: return 4
    if peak <= 20: return 3
    if peak <= 40: return 2
    return 1


# ══════════════════════════════════════════════════════════════════════════════
#  FILE OPERATIONS
# ══════════════════════════════════════════════════════════════════════════════


# ══════════════════════════════════════════════════════════════════════════════
#  API ENDPOINTS
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/status")
async def retriever_status(user: dict = Depends(require_auth)):
    async with aiosqlite.connect(settings.database_url) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM scan_jobs WHERE job_type='retriever' ORDER BY job_id DESC LIMIT 1"
        )
        job = await cursor.fetchone()
        cursor2 = await db.execute("SELECT COUNT(*) FROM tracks")
        track_count = (await cursor2.fetchone())[0]
    return {
        "active_job": dict(job) if job else None,
        "total_cached_tracks": track_count,
        "itunes_rpm_limit": settings.itunes_max_rpm,
    }


@router.get("/browse")
async def browse_music(path: str = "", user: dict = Depends(require_auth)):
    """
    Browse the /music mount for subfolder selection.
    Returns immediate subdirectories of the given path.
    """
    base = settings.docker_music_prefix
    target = os.path.join(base, path.strip("/")) if path else base
    target = os.path.normpath(target)

    # H1 FIX: realpath + strict containment (handles symlinks + sibling-dir bypass)
    if not _is_within_music_prefix(target):
        raise HTTPException(400, "Path outside music directory")
    if not os.path.exists(target):
        raise HTTPException(404, f"Path not found: {target}")

    try:
        entries = []
        for name in sorted(os.listdir(target)):
            full = os.path.join(target, name)
            if os.path.isdir(full) and not name.startswith("."):
                entries.append({
                    "name": name,
                    "path": full,
                    "rel_path": full.replace(base, "").lstrip("/"),
                })
        return {"path": target, "entries": entries, "base": base}
    except Exception as e:
        raise HTTPException(500, str(e))


# ══════════════════════════════════════════════════════════════════════════════
#  WRITE LOG
# ══════════════════════════════════════════════════════════════════════════════


@router.get("/write-log")
async def get_write_log(limit: int = 50, user: dict = Depends(require_auth)):
    async with aiosqlite.connect(settings.database_url) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM write_log ORDER BY written_at DESC LIMIT ?", (limit,)
        )
        rows = await cursor.fetchall()
    return [dict(r) for r in rows]


# ══════════════════════════════════════════════════════════════════════════════
#  GENRE ENGINE (M5b) — one family + up to 3 styles per song, by weighted vote
# ══════════════════════════════════════════════════════════════════════════════
#  Sources (weight): Discogs album genres/styles (3) · MusicBrainz artist tags (2)
#                    Last.fm song tags (2) · Last.fm artist tags (1)
#  Album batching: one Discogs lookup per album folder; compilations skip Discogs.
#  Every lookup is cached; re-runs only fetch what's new.

GE_FAMILIES = {
    "rock": "Rock", "pop": "Pop", "country": "Country", "rnb": "R&B", "hiphop": "Hip-Hop",
    "metal": "Metal", "dance": "Dance", "folk": "Folk", "jazz": "Jazz", "blues": "Blues",
    "classical": "Classical", "ccm": "Christian", "gospel": "Gospel",
}
# style key → (display, families it belongs to). Two families = a bridge style.
GE_STYLES = {
    "classicrock": ("Classic Rock", ("rock",)), "hardrock": ("Hard Rock", ("rock",)),
    "softrock": ("Soft Rock", ("rock", "pop")), "arenarock": ("Arena Rock", ("rock",)),
    "indierock": ("Indie Rock", ("rock",)), "folkrock": ("Folk Rock", ("rock", "folk")),
    "southernrock": ("Southern Rock", ("rock",)), "punkrock": ("Punk / New Wave", ("rock",)),
    "alternative": ("Alternative Rock", ("rock",)), "poprock": ("Pop Rock", ("rock", "pop")),
    "countryrock": ("Country Rock", ("country", "rock")), "bluesrock": ("Blues Rock", ("blues", "rock")),
    "progrock": ("Progressive Rock", ("rock",)), "psychrock": ("Psychedelic Rock", ("rock",)),
    "rocknroll": ("Rock & Roll", ("rock",)),
    "dancepop": ("Dance-Pop", ("pop", "dance")), "synthpop": ("Synth-Pop", ("pop",)),
    "teenpop": ("Teen Pop", ("pop",)), "powerpop": ("Power Pop", ("pop", "rock")),
    "adultpop": ("Adult Contemporary", ("pop",)), "indipop": ("Indie Pop", ("pop",)),
    "easylistening": ("Easy Listening", ("pop", "jazz")),
    "tradcountry": ("Traditional Country", ("country",)), "countrypop": ("Country Pop", ("country", "pop")),
    "outlawcountry": ("Outlaw Country", ("country",)), "americana": ("Americana", ("country", "folk")),
    "brocountry": ("Bro-Country", ("country",)), "texascountry": ("Texas Country", ("country",)),
    "soul": ("Soul", ("rnb",)), "neosoul": ("Neo-Soul", ("rnb",)), "funk": ("Funk", ("rnb",)),
    "motown": ("Motown", ("rnb",)), "quietstorm": ("Quiet Storm", ("rnb",)),
    "newjack": ("New Jack Swing", ("rnb",)), "doowop": ("Doo-Wop", ("rnb",)),
    "trap": ("Trap", ("hiphop",)), "boombap": ("Boom Bap", ("hiphop",)),
    "gangstarap": ("Gangsta Rap", ("hiphop",)), "conscrap": ("Conscious Rap", ("hiphop",)),
    "altrap": ("Alt Hip-Hop", ("hiphop",)),
    "heavymetal": ("Heavy Metal", ("metal",)), "thrashmetal": ("Thrash Metal", ("metal",)),
    "deathmetal": ("Death Metal", ("metal",)), "doommetal": ("Doom Metal", ("metal",)),
    "glaemmetal": ("Glam Metal", ("metal", "rock")),
    "edm": ("EDM", ("dance",)), "house": ("House", ("dance",)), "techno": ("Techno", ("dance",)),
    "trance": ("Trance", ("dance",)), "disco": ("Disco", ("dance", "rnb")), "eurodance": ("Eurodance", ("dance",)),
    "tradfolk": ("Traditional Folk", ("folk",)), "indifolk": ("Indie Folk", ("folk",)),
    "singersong": ("Singer-Songwriter", ("folk", "pop")),
    "smoothjazz": ("Smooth Jazz", ("jazz",)), "vocaljazz": ("Vocal Jazz", ("jazz",)),
    "bebop": ("Bebop", ("jazz",)), "jazzfusion": ("Jazz Fusion", ("jazz",)),
    "chicagoblues": ("Chicago Blues", ("blues",)), "deltablues": ("Delta Blues", ("blues",)),
    "texasblues": ("Texas Blues", ("blues",)),
    "orchestral": ("Orchestral", ("classical",)), "opera": ("Opera", ("classical",)),
    "baroque": ("Baroque", ("classical",)),
    "ccm-rock": ("Christian Rock", ("ccm", "rock")), "ccm-pop": ("Christian Pop", ("ccm", "pop")),
    "worship": ("Worship", ("ccm",)), "sgospel": ("Southern Gospel", ("gospel",)),
}
# Raw tag text (Discogs genres/styles, MusicBrainz, Last.fm) → family or style key.
GE_TAGS = {
    # families
    "rock": "rock", "pop": "pop", "country": "country", "r&b": "rnb", "rnb": "rnb",
    "rhythm and blues": "rnb", "rhythm & blues": "rnb", "contemporary r&b": "rnb", "funk / soul": "rnb",
    "hip hop": "hiphop", "hip-hop": "hiphop", "hiphop": "hiphop", "rap": "hiphop", "pop rap": "hiphop",
    "metal": "metal", "electronic": "dance", "electronica": "dance", "dance": "dance",
    "folk": "folk", "jazz": "jazz", "blues": "blues", "classical": "classical",
    "christian": "ccm", "ccm": "ccm", "contemporary christian": "ccm", "gospel": "gospel",
    "folk, world, & country": "_folkcountry",
    # rock styles
    "classic rock": "classicrock", "hard rock": "hardrock", "soft rock": "softrock",
    "arena rock": "arenarock", "aor": "arenarock", "indie rock": "indierock", "folk rock": "folkrock",
    "southern rock": "southernrock", "punk": "punkrock", "punk rock": "punkrock", "new wave": "punkrock",
    "post-punk": "punkrock", "alternative": "alternative", "alternative rock": "alternative",
    "grunge": "alternative", "britpop": "alternative", "pop rock": "poprock", "pop/rock": "poprock",
    "country rock": "countryrock", "blues rock": "bluesrock", "progressive rock": "progrock",
    "prog rock": "progrock", "psychedelic rock": "psychrock", "heartland rock": "classicrock",
    "rock & roll": "rocknroll", "rock and roll": "rocknroll", "rock n roll": "rocknroll",
    "rockabilly": "rocknroll",
    # pop styles
    "dance pop": "dancepop", "dance-pop": "dancepop", "europop": "dancepop", "synthpop": "synthpop",
    "synth-pop": "synthpop", "synth pop": "synthpop", "electropop": "synthpop", "teen pop": "teenpop",
    "power pop": "powerpop", "adult contemporary": "adultpop", "indie pop": "indipop",
    "easy listening": "easylistening", "lounge": "easylistening",
    # country
    "classic country": "tradcountry", "traditional country": "tradcountry", "honky tonk": "tradcountry",
    "country pop": "countrypop", "outlaw country": "outlawcountry", "americana": "americana",
    "bluegrass": "americana", "texas country": "texascountry", "red dirt": "texascountry",
    "bro-country": "brocountry",
    # r&b
    "soul": "soul", "neo soul": "neosoul", "neo-soul": "neosoul", "funk": "funk", "motown": "motown",
    "quiet storm": "quietstorm", "new jack swing": "newjack", "doo wop": "doowop", "doo-wop": "doowop",
    # hip-hop
    "trap": "trap", "boom bap": "boombap", "gangsta": "gangstarap", "gangsta rap": "gangstarap",
    "conscious": "conscrap", "conscious hip hop": "conscrap",
    # metal
    "heavy metal": "heavymetal", "thrash": "thrashmetal", "thrash metal": "thrashmetal",
    "death metal": "deathmetal", "doom metal": "doommetal", "stoner rock": "doommetal",
    "glam": "glaemmetal", "glam metal": "glaemmetal", "hair metal": "glaemmetal", "glam rock": "glaemmetal",
    # dance
    "edm": "edm", "house": "house", "techno": "techno", "trance": "trance", "disco": "disco",
    "eurodance": "eurodance", "euro house": "eurodance", "hi nrg": "disco",
    # folk / jazz / blues / classical / christian
    "traditional folk": "tradfolk", "indie folk": "indifolk", "singer-songwriter": "singersong",
    "singer/songwriter": "singersong", "smooth jazz": "smoothjazz", "vocal jazz": "vocaljazz",
    "bebop": "bebop", "fusion": "jazzfusion", "jazz fusion": "jazzfusion",
    "chicago blues": "chicagoblues", "delta blues": "deltablues", "texas blues": "texasblues",
    "orchestral": "orchestral", "opera": "opera", "baroque": "baroque",
    "christian rock": "ccm-rock", "christian pop": "ccm-pop", "worship": "worship",
    "praise & worship": "worship", "southern gospel": "sgospel",
    "christian hip hop": "ccm-pop", "christian rap": "ccm-pop", "christian metal": "ccm-rock",
    "christian alternative": "ccm-rock", "christian alternative rock": "ccm-rock",
    "christian contemporary": "ccm", "worship music": "worship", "religious": "ccm",
}
GE_WEIGHTS = {"discogs": 3.0, "discogs_g": 2.0, "mb": 2.0, "lfm_track": 2.0, "lfm_artist": 1.0,
              "mine": 5.0}   # your own Christian/Gospel tags — the strongest voice
GE_SACRED = {"ccm", "gospel"}
GE_DEFAULT_RULES = [{"prefix": "/music/FULL CHRISTIAN ALBUMS", "family": "ccm"}]


def _ge_family_of(k: str) -> tuple:
    if k in GE_FAMILIES:
        return (k,)
    if k in GE_STYLES:
        return GE_STYLES[k][1]
    return ()


def _ge_apply_rule(family: str, vote: Optional[dict]) -> tuple:
    """Folder rule wins on family; translate the voted sound into fitting styles."""
    vf = vote["family"] if vote else None
    vs = vote["styles"] if vote else []
    keep = [st for st in vs if family in GE_STYLES[st][1]]
    if family == "ccm" and not keep:
        if vf in ("rock", "metal") or any(GE_STYLES[st][1][0] in ("rock", "metal") for st in vs):
            keep = ["ccm-rock"]
        elif vf in ("pop", "dance", "hiphop", "rnb"):
            keep = ["ccm-pop"]
        elif vf == "gospel":
            keep = []
    return family, keep[:3]
# Discogs lists an album's genres alphabetically, so their order carries no meaning
_GE_FLAT = {"discogs_g"}
_ge_job = {"status": "idle", "phase": "", "albums_done": 0, "albums_total": 0,
           "artists_done": 0, "artists_total": 0, "scored": 0, "check": 0, "message": ""}
_ge_stop = {"flag": False}
_ge_revote_only = {"flag": False}   # re-vote from cache, no lookups
_GE_FOLDER_JUNK = re.compile(r"[\(\[\{][^\)\]\}]*[\)\]\}]|\b(cd|disc|disk)\s*\d+\b|\b(19|20)\d{2}\b", re.I)


def _ge_tag_keys(tags) -> list:
    out = []
    for t in tags or []:
        k = GE_TAGS.get(str(t).strip().lower())
        if k and k not in out:
            out.append(k)
    return out


# Families that sit next to each other — disagreement between them isn't a real conflict
GE_NEIGHBORS = {frozenset(p) for p in (
    ("pop", "rnb"), ("rock", "metal"), ("country", "folk"), ("rnb", "hiphop"), ("jazz", "blues"),
    ("rnb", "blues"), ("pop", "dance"), ("rock", "blues"), ("rock", "folk"), ("ccm", "gospel"),
    ("dance", "rnb"), ("pop", "hiphop"),
)}


def _ge_related(f1: str, f2: str) -> bool:
    """Neighbours: same family, joined by a bridge style (Pop ↔ Rock via Pop Rock), or listed above."""
    return (f1 == f2 or frozenset((f1, f2)) in GE_NEIGHBORS
            or any(f1 in fams and f2 in fams for _, fams in GE_STYLES.values()))


def _ge_vote(sources: list) -> Optional[dict]:
    """
    sources: [(source_name, [keys in rank order])] → {family, styles, confidence, sources} or None.
    CHECK only when sources genuinely disagree (unrelated families) or evidence is thin.
    """
    fam_score, style_score, used, tops = {}, {}, [], []
    for name, keys in sources:
        if not keys:
            continue
        w = GE_WEIGHTS[name]
        contrib = {}
        for i, k in enumerate(keys[:6]):
            pos = 1.0 if (i == 0 or name in _GE_FLAT) else (0.7 if i == 1 else 0.5)
            if k == "_folkcountry":
                fams = ("folk", "country")
            elif k in GE_FAMILIES:
                fams = (k,)
            elif k in GE_STYLES:
                fams = GE_STYLES[k][1]
                style_score[k] = style_score.get(k, 0) + w * pos
            else:
                continue
            for f in fams:   # a source can't stack several tags into one family
                contrib[f] = max(contrib.get(f, 0), w * pos / len(fams))
        if contrib:
            if name.replace("_g", "") not in used:
                used.append(name.replace("_g", ""))
            tops.append((name, w, max(contrib.items(), key=lambda kv: kv[1])[0]))
        for f, v in contrib.items():
            fam_score[f] = fam_score.get(f, 0) + v
    if not fam_score:
        return None
    family = max(fam_score.items(), key=lambda kv: kv[1])[0]
    # Disco as the strongest style belongs in Dance, not R&B (Discogs files it under Funk / Soul)
    if family == "rnb" and style_score and max(style_score, key=style_score.get) == "disco":
        family = "dance"

    # Styles: must fit the family and have real support (drops one-off stragglers)
    cands = sorted(((k, v) for k, v in style_score.items() if family in GE_STYLES[k][1]),
                   key=lambda kv: -kv[1])
    best = cands[0][1] if cands else 0
    styles = [k for k, v in cands if v >= 1.0 and v >= 0.4 * best][:3]

    agree = sum(w for _, w, f in tops if _ge_related(f, family))
    clash = any(w >= 2 and not _ge_related(f, family) for _, w, f in tops)
    # conflict = sources point at unrelated families (worth a look)
    # thin     = too little information to be sure (usually fine)
    conf = "conflict" if clash else ("thin" if agree < 3 else "high")
    return {"family": family, "styles": styles, "confidence": conf, "sources": used}


def _ge_display(family: str, styles: list) -> list:
    return [GE_FAMILIES[family]] + [GE_STYLES[s][0] for s in styles]


async def _ge_migrate():
    async with aiosqlite.connect(settings.database_url) as db:
        await db.execute("""CREATE TABLE IF NOT EXISTS ge_discogs (
            lookup_key TEXT PRIMARY KEY, status TEXT NOT NULL, genres TEXT, styles TEXT,
            fetched_at TEXT NOT NULL DEFAULT (datetime('now')))""")
        await db.execute("""CREATE TABLE IF NOT EXISTS ge_mb_artists (
            artist_key TEXT PRIMARY KEY, status TEXT NOT NULL, tags TEXT,
            fetched_at TEXT NOT NULL DEFAULT (datetime('now')))""")
        await db.execute("""CREATE TABLE IF NOT EXISTS track_genres (
            track_id INTEGER PRIMARY KEY, family TEXT NOT NULL, styles TEXT,
            confidence TEXT NOT NULL, sources TEXT,
            updated_at TEXT NOT NULL DEFAULT (datetime('now')))""")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_tg_family ON track_genres(family)")
        cols = [r[1] for r in await (await db.execute("PRAGMA table_info(tracks)")).fetchall()]
        if "missing" not in cols:
            await db.execute("ALTER TABLE tracks ADD COLUMN missing INTEGER DEFAULT 0")
        await db.commit()


def _ge_album_dir(file_path: str) -> str:
    """An album = the song's folder; CD1 / Disc 2 sub-folders count as their parent."""
    d = os.path.dirname(file_path or "")
    if re.fullmatch(r"(cd|disc|disk)\s*\d+", os.path.basename(d).strip(), re.I):
        d = os.path.dirname(d)
    return d


_GE_ALB_FMT = re.compile(r"\b(flac|mp3|aac|m4a|alac|wav|web|cd|vinyl|lossless|hi[- ]?res|\d{2,3}\s*kbps|kbps|"
                         r"(16|24)[- ]?bit(\s*[-/]?\s*\d+(\.\d+)?\s*k?hz)?|\d+(\.\d+)?\s*khz|remaster(ed)?\s*\d{4})\b", re.I)


def _ge_album_display(name: str, artist: str = "") -> str:
    """Display-only tidy-up of an album name: drops [tags], (2021), format words, 'Artist - ' prefix."""
    raw = (name or "").strip()
    t = raw
    if " " not in t and t.count(".") >= 2:
        t = t.replace(".", " ")
    t = re.sub(r"\[[^\]]*\]|\{[^}]*\}", " ", t)
    t = re.sub(r"\(\s*(?:\d{4}|[^)]*\b(?:flac|mp3|bit|khz|kbps|web|hi[- ]?res|remaster(?:ed)?)\b[^)]*)\s*\)", " ", t, flags=re.I)
    t = _GE_ALB_FMT.sub(" ", t)
    t = re.sub(r"^\s*va\s*-\s*", "", t, flags=re.I)
    a = (artist or "").strip().lower()
    if a and t.lower().startswith(a):
        rest = t[len(a):]
        if re.match(r"^\s*-\s*\S", rest):
            t = re.sub(r"^\s*-\s*", "", rest)
    t = re.sub(r"\s+", " ", t).strip(" -_.,")
    return t or raw


def _ge_album_name(album_title: Optional[str], file_path: str) -> str:
    if album_title:
        return album_title
    folder = os.path.basename(os.path.dirname(file_path or ""))
    if re.fullmatch(r"(cd|disc|disk)\s*\d+", folder.strip(), re.I):
        folder = os.path.basename(os.path.dirname(os.path.dirname(file_path)))
    if " - " in folder:
        folder = folder.split(" - ", 1)[1]
    return re.sub(r"\s+", " ", _GE_FOLDER_JUNK.sub(" ", folder)).strip(" -_.")


async def _ge_get_discogs_token() -> str:
    try:
        from app.security import decrypt_token
        async with aiosqlite.connect(settings.database_url) as db:
            row = await (await db.execute(
                "SELECT token_enc FROM connections WHERE service='discogs'")).fetchone()
        return decrypt_token(row[0]) if row and row[0] else ""
    except Exception:
        return ""


async def _ge_discogs_phase(albums: dict, artists_for_fallback: dict, token: str):
    """albums: lookup_key → (artist, album). One search per album; artist-level fallback."""
    hdr = {"User-Agent": "ChartHound/1.0 +https://github.com/CurtisColby/ChartHound",
           "Authorization": f"Discogs token={token}"}
    SQL = ("INSERT INTO ge_discogs (lookup_key, status, genres, styles, fetched_at) "
           "VALUES (?, ?, ?, ?, datetime('now')) ON CONFLICT(lookup_key) DO UPDATE SET "
           "status=excluded.status, genres=excluded.genres, styles=excluded.styles, "
           "fetched_at=excluded.fetched_at")
    todo = list(albums.items()) + [(k, (a, None)) for k, a in artists_for_fallback.items()]
    _ge_job["albums_total"] = len(todo)
    async with httpx.AsyncClient(timeout=20.0) as client, \
               aiosqlite.connect(settings.database_url, timeout=120) as db:
        for i, (key, (artist, album)) in enumerate(todo):
            if _ge_stop["flag"]:
                break
            t0 = time.monotonic()
            results, status = [], "notfound"
            tries = ([{"artist": artist, "release_title": album, "type": "master"},
                      {"artist": artist, "release_title": album, "type": "release"}] if album
                     else [{"artist": artist, "type": "master"}])
            for params in tries:
                for _ in range(3):
                    try:
                        r = await client.get("https://api.discogs.com/database/search",
                                             params={**params, "per_page": 5}, headers=hdr)
                    except Exception:
                        r = None
                    if r is not None and r.status_code == 429:
                        _ge_job["message"] = "Discogs rate limit — pausing 60s…"
                        await asyncio.sleep(60)
                        continue
                    break
                if r is not None and r.is_success:
                    results = r.json().get("results", [])
                    status = "ok" if results else "notfound"
                    if results:
                        break
                elif r is not None and r.status_code in (401, 403):
                    _ge_job["message"] = "Discogs rejected the token — check The Kennel."
                    _ge_stop["flag"] = True
                    break
                else:
                    status = "error"
                await asyncio.sleep(1.05)
                try:
                    if r is not None and int(r.headers.get("X-Discogs-Ratelimit-Remaining", "10")) <= 2:
                        await asyncio.sleep(5)
                except Exception:
                    pass
            if album:
                g, st = (results[0].get("genre", []), results[0].get("style", [])) if results else ([], [])
            else:   # artist profile: most common genres/styles across their top releases
                cg, cs = {}, {}
                for res in results:
                    for x in res.get("genre", []): cg[x] = cg.get(x, 0) + 1
                    for x in res.get("style", []): cs[x] = cs.get(x, 0) + 1
                g = [x for x, _ in sorted(cg.items(), key=lambda kv: -kv[1])]
                st = [x for x, _ in sorted(cs.items(), key=lambda kv: -kv[1])]
            if status != "error":
                await db.execute(SQL, (key, status, json.dumps(g), json.dumps(st)))
                await db.commit()   # short write — never blocks the MusicBrainz side
            _ge_job["albums_done"] = i + 1
            _ge_job["message"] = f"Discogs: {artist}{' — ' + album if album else ''}"
            await asyncio.sleep(max(0.0, 1.05 - (time.monotonic() - t0)))
        await db.commit()


async def _ge_mb_phase(artists: dict):
    SQL = ("INSERT INTO ge_mb_artists (artist_key, status, tags, fetched_at) VALUES (?, ?, ?, datetime('now')) "
           "ON CONFLICT(artist_key) DO UPDATE SET status=excluded.status, tags=excluded.tags, "
           "fetched_at=excluded.fetched_at")
    _ge_job["artists_total"] = len(artists)
    hdr = {"User-Agent": "ChartHound/1.0 (https://github.com/CurtisColby/ChartHound)"}
    async with httpx.AsyncClient(timeout=20.0) as client, \
               aiosqlite.connect(settings.database_url, timeout=120) as db:
        for i, (ak, name) in enumerate(artists.items()):
            if _ge_stop["flag"]:
                break
            await mb_limiter.acquire()
            status, tags = "error", []
            try:
                r = await client.get("https://musicbrainz.org/ws/2/artist/",
                                     params={"query": f'artist:"{name}"', "fmt": "json", "limit": 3},
                                     headers=hdr)
                if r.is_success:
                    arts = [a for a in r.json().get("artists", []) if int(a.get("score", 0)) >= 90]
                    status = "ok" if arts else "notfound"
                    if arts:
                        tl = sorted(arts[0].get("tags", []), key=lambda t: -int(t.get("count", 0)))
                        tags = [t["name"] for t in tl if int(t.get("count", 0)) > 0]
            except Exception:
                pass
            if status != "error":
                await db.execute(SQL, (ak, status, json.dumps(tags)))
                await db.commit()   # short write — never blocks the Discogs side
            _ge_job["artists_done"] = i + 1
        await db.commit()


# Second chance for songs no source knew: a better artist name, taken from the file name
# ("05. Sylvester - You Make Me Feel") or the first of several tagged artists ("Mabel; R3hab").
_GE_FILE_AT = re.compile(r"^\s*(?:\d{1,3}\s*[-._)]*\s*)?(.+?)\s+-\s+(.+?)\s*$")
_GE_SPLIT = re.compile(r"\s*(?:;|,|&|/|\s\+\s|\bfeat\.?(?=\s)|\bft\.?(?=\s)|\bfeaturing\b|\bwith\b|\bx\b|\bvs\.?(?=\s))\s*", re.I)


def _ge_alt_artist(artist: str, title: str, fp: str) -> Optional[str]:
    from app.routers.groomer import _artist_key, _title_key
    ak, tk = _artist_key(artist or ""), _title_key(title or "")
    m = _GE_FILE_AT.match(os.path.splitext(os.path.basename(fp or ""))[0])
    if m and tk:
        a, ft = re.sub(r"^\d{1,3}\s*[-._)]+\s*", "", m.group(1)).strip(), _title_key(m.group(2))
        if a and not re.fullmatch(r"[\d\s._-]+", a) and ft and (ft == tk or tk in ft or ft in tk) and _artist_key(a) and _artist_key(a) != ak:
            return a
    parts = [p.strip() for p in _GE_SPLIT.split(artist or "") if p and p.strip()]
    if len(parts) > 1 and _artist_key(parts[0]) and _artist_key(parts[0]) != ak:
        return parts[0]
    return None


async def _run_genre_engine():
    global _ge_job
    from app.routers.groomer import _artist_key, _title_key, _FEAT_RE, _migrate_chart_data
    _ge_stop["flag"] = False
    _ge_job = {"status": "running", "phase": "preparing", "albums_done": 0, "albums_total": 0,
               "artists_done": 0, "artists_total": 0, "scored": 0, "check": 0, "message": "Preparing…"}
    try:
        await _migrate_chart_data()
        await _ge_migrate()
        token = await _ge_get_discogs_token()
        now = time.time()
        async with aiosqlite.connect(settings.database_url) as db:
            tracks = await (await db.execute("""
                SELECT t.track_id, COALESCE(t.tag_artist, a.name, ''), t.title, t.file_path, t.album_title
                FROM tracks t LEFT JOIN artists a ON t.artist_id = a.artist_id
                WHERE t.file_path IS NOT NULL""")).fetchall()
            dcache = {k: (st, f) for k, st, f in await (await db.execute(
                "SELECT lookup_key, status, fetched_at FROM ge_discogs")).fetchall()}
            mcache = {k: (st, f) for k, st, f in await (await db.execute(
                "SELECT artist_key, status, fetched_at FROM ge_mb_artists")).fetchall()}

        def fresh(c):
            if not c:
                return False
            from app.routers.groomer import _utc_epoch
            age = now - _utc_epoch(c[1])
            return age < (365 if c[0] == "ok" else 90) * 86400

        # Group by album folder; detect compilations
        folders = {}
        for tid, artist, title, fp, alb in tracks:
            folders.setdefault(os.path.dirname(fp), []).append((tid, artist, title, fp, alb))
        track_album, albums, names = {}, {}, {}
        for folder, items in folders.items():
            counts = {}
            for _, artist, *_r in items:
                ak = _artist_key(artist)
                if ak:
                    counts[ak] = counts.get(ak, 0) + 1
                    names.setdefault(ak, _FEAT_RE.sub("", artist))
            if not counts:
                continue
            top_ak, top_n = max(counts.items(), key=lambda kv: kv[1])
            album = _ge_album_name(items[0][4], items[0][3])
            is_comp = (len(counts) >= 4 and top_n / len(items) < 0.5) or "various" in album.lower()
            if is_comp or not album:
                continue
            key = f"album:{top_ak}|{_title_key(album)}"
            for it in items:
                track_album[it[0]] = key
            if not fresh(dcache.get(key)):
                albums[key] = (names[top_ak], album)
        # Artist-level Discogs profile only for artists with songs outside a real album
        # (compilations, loose files) — keeps the first run to roughly one call per album.
        need_art = {_artist_key(artist) for tid, artist, *_r in tracks if tid not in track_album}
        art_fallback = {f"artist:{ak}": names[ak] for ak in need_art
                        if ak in names and not fresh(dcache.get(f"artist:{ak}"))}
        mb_todo = {ak: n for ak, n in names.items() if not fresh(mcache.get(ak))}
        if _ge_revote_only["flag"]:
            albums, art_fallback, mb_todo = {}, {}, {}

        # Discogs (if connected) and MusicBrainz run side by side — different services
        _ge_job["phase"] = "lookups"
        async def _guard(name, coro):
            # One source failing must never kill (or orphan) the other
            try:
                await coro
            except Exception as e:
                log.exception(f"Genre Engine: {name} lookups failed — continuing with other sources")
                _ge_job.setdefault("warnings", []).append(f"{name}: {e}")
        jobs = [_guard("MusicBrainz", _ge_mb_phase(mb_todo))]
        if token:
            jobs.append(_guard("Discogs", _ge_discogs_phase(albums, art_fallback, token)))
        else:
            _ge_job["message"] = "Discogs not connected — using MusicBrainz + Last.fm only."
        await asyncio.gather(*jobs)

        # ── Vote ──
        _ge_job.update({"phase": "voting", "message": "Voting on genres…"})
        async with aiosqlite.connect(settings.database_url, timeout=120) as db:
            disc = {k: (json.loads(g or "[]"), json.loads(s or "[]")) for k, g, s in await (await db.execute(
                "SELECT lookup_key, genres, styles FROM ge_discogs WHERE status='ok'")).fetchall()}
            mb = {k: json.loads(t or "[]") for k, t in await (await db.execute(
                "SELECT artist_key, tags FROM ge_mb_artists WHERE status='ok'")).fetchall()}
            lt = {(a, t): json.loads(g or "[]") for a, t, g in await (await db.execute(
                "SELECT artist_key, title_key, tags FROM lastfm_tracks WHERE status='ok'")).fetchall()}
            la = {a: json.loads(g or "[]") for a, g in await (await db.execute(
                "SELECT artist_key, tags FROM lastfm_artists WHERE status='ok'")).fetchall()}
            # Your own tags: the genre each file had BEFORE ChartHound first wrote it
            mine = {tid: [x.strip() for x in (old or "").split(";") if x.strip()]
                    for tid, old in await (await db.execute(
                        "SELECT track_id, old_value FROM write_log WHERE field_changed='genre' "
                        "AND log_id IN (SELECT MIN(log_id) FROM write_log WHERE field_changed='genre' "
                        "GROUP BY track_id)")).fetchall()}
            for tid, g1, g2, g3 in await (await db.execute(
                    "SELECT track_id, genre_1, genre_2, genre_3 FROM tracks")).fetchall():
                if tid not in mine:
                    mine[tid] = [g for g in (g1, g2, g3) if g]
            rules = sorted(await _ge_get_rules(), key=lambda r: -len(r["prefix"]))
            out, n_check, n_conflict, n_skip, retry = [], 0, 0, 0, []
            for tid, artist, title, fp, alb in tracks:
                ak, tk = _artist_key(artist), _title_key(title)
                d = disc.get(track_album.get(tid)) or disc.get(f"artist:{ak}")
                sources = [
                    ("discogs", _ge_tag_keys(d[1] if d else [])),     # album styles
                    ("discogs_g", _ge_tag_keys(d[0] if d else [])),   # album genres (unordered)
                    ("mb", _ge_tag_keys(mb.get(ak))),
                    ("lfm_track", _ge_tag_keys(lt.get((ak, tk)))),
                    ("lfm_artist", _ge_tag_keys(la.get(ak))),
                ]
                v = _ge_vote(sources)
                # Your own Christian/Gospel tags and folder rules decide the family outright;
                # the online sources then only choose the style.
                my_sacred = [k for k in _ge_tag_keys(mine.get(tid)) if set(_ge_family_of(k)) & GE_SACRED]
                rule = next((r for r in rules if fp and (fp == r["prefix"] or fp.startswith(r["prefix"] + "/"))), None)
                if rule and rule["family"] == "skip":
                    n_skip += 1
                    continue          # never tagged, never reviewed
                forced, why = None, None
                if rule:
                    forced, why = rule["family"], "rule"
                elif my_sacred:
                    forced = next(f for f in _ge_family_of(my_sacred[0]) if f in GE_SACRED)
                    why = "your tags"
                if forced:
                    fam, sty = _ge_apply_rule(forced, v)
                    own = [k for k in my_sacred if k in GE_STYLES and forced in GE_STYLES[k][1]]
                    sty = (own + [x for x in sty if x not in own])[:3]
                    v = {"family": fam, "styles": sty, "confidence": "high",
                         "sources": (v["sources"] if v else []) + [why]}
                if not v:
                    retry.append((tid, artist, title, fp))
                    continue
                n_check += v["confidence"] in ("conflict", "thin")
                n_conflict += v["confidence"] == "conflict"
                out.append((tid, v["family"], json.dumps(v["styles"]), v["confidence"],
                            ",".join(v["sources"])))
            # ── Retry songs no source knew, with a better artist name ──
            alt_for = {}
            for tid, artist, title, fp in retry:
                alt = _ge_alt_artist(artist, title, fp)
                if alt:
                    alt_for[tid] = alt
            n_alt = 0
            if alt_for:
                alt_names = {_artist_key(a): _FEAT_RE.sub("", a) for a in alt_for.values()}
                if not _ge_revote_only["flag"] and not _ge_stop["flag"]:
                    mb2 = {k: n for k, n in alt_names.items() if k not in mb and not fresh(mcache.get(k))}
                    dc2 = {f"artist:{k}": n for k, n in alt_names.items()
                           if f"artist:{k}" not in disc and not fresh(dcache.get(f"artist:{k}"))}
                    if mb2 or dc2:
                        _ge_job.update({"phase": "lookups", "albums_done": 0, "artists_done": 0,
                                        "message": f"Second try for {len(alt_for):,} songs using a better artist name…"})
                        jobs2 = [_guard("MusicBrainz", _ge_mb_phase(mb2))] if mb2 else []
                        if token and dc2:
                            jobs2.append(_guard("Discogs", _ge_discogs_phase({}, dc2, token)))
                        await asyncio.gather(*jobs2)
                        _ge_job.update({"phase": "voting", "message": "Voting on genres…"})
                keys = list(alt_names)
                for i in range(0, len(keys), 500):
                    part = keys[i:i + 500]
                    qm = ",".join("?" * len(part))
                    for k, t in await (await db.execute(
                            f"SELECT artist_key, tags FROM ge_mb_artists WHERE status='ok' AND artist_key IN ({qm})",
                            part)).fetchall():
                        mb[k] = json.loads(t or "[]")
                    akeys = [f"artist:{k}" for k in part]
                    for k, g, st in await (await db.execute(
                            f"SELECT lookup_key, genres, styles FROM ge_discogs WHERE status='ok' AND lookup_key IN ({qm})",
                            akeys)).fetchall():
                        disc[k] = (json.loads(g or "[]"), json.loads(st or "[]"))
                for tid, artist, title, fp in retry:
                    alt = alt_for.get(tid)
                    if not alt:
                        continue
                    k2, tk = _artist_key(alt), _title_key(title)
                    d = disc.get(f"artist:{k2}")
                    v = _ge_vote([
                        ("discogs", _ge_tag_keys(d[1] if d else [])),
                        ("discogs_g", _ge_tag_keys(d[0] if d else [])),
                        ("mb", _ge_tag_keys(mb.get(k2))),
                        ("lfm_track", _ge_tag_keys(lt.get((k2, tk)))),
                        ("lfm_artist", _ge_tag_keys(la.get(k2))),
                    ])
                    if not v:
                        continue
                    n_alt += 1
                    n_check += v["confidence"] in ("conflict", "thin")
                    n_conflict += v["confidence"] == "conflict"
                    out.append((tid, v["family"], json.dumps(v["styles"]), v["confidence"],
                                ",".join(v["sources"] + ["alt"])))
            if alt_for:
                log.info(f"Genre Engine: second try with a better artist name — {n_alt:,} of {len(retry):,} songs decided")
            # Manual edits are never overwritten by a re-run
            await db.execute("DELETE FROM track_genres WHERE confidence != 'manual'")
            await db.executemany(
                "INSERT OR IGNORE INTO track_genres (track_id, family, styles, confidence, sources, updated_at) "
                "VALUES (?, ?, ?, ?, ?, datetime('now'))", out)
            await db.commit()
        stopped = _ge_stop["flag"]
        _ge_job.update({"status": "stopped" if stopped else "done", "phase": "", "scored": len(out),
                        "check": n_check,
                        "message": (f"{'Stopped — partial results saved' if stopped else 'Done'}: "
                                    f"{len(out):,} songs have a genre · ⚠ {n_conflict:,} conflicts · "
                                    f"? {n_check - n_conflict:,} little data"
                                    + (f" · {n_skip:,} skipped by folder rule" if n_skip else "")
                                    + (f" · ⚠ {'; '.join(_ge_job.get('warnings', []))[:200]}"
                                       if _ge_job.get("warnings") else ""))})
        log.info(f"Genre Engine: {_ge_job['message']}")
    except Exception as e:
        log.exception("Genre Engine failed")
        _ge_job.update({"status": "failed", "message": f"Error: {e}"})


_ge_task = {"task": None}


@router.post("/genre-engine/start")
async def genre_engine_start(user: dict = Depends(require_auth)):
    t = _ge_task["task"]
    if t is not None and not t.done():
        raise HTTPException(409, "Already running.")
    _ge_task["task"] = asyncio.create_task(_run_genre_engine())
    return {"ok": True}


@router.post("/genre-engine/stop")
async def genre_engine_stop(user: dict = Depends(require_auth)):
    _ge_stop["flag"] = True
    return {"ok": True}


@router.get("/genre-engine/status")
async def genre_engine_status(user: dict = Depends(require_auth)):
    if _ge_job.get("status") != "idle":
        return _ge_job
    # Idle after a restart — report the last run from the database
    out = dict(_ge_job)
    try:
        async with aiosqlite.connect(settings.database_url) as db:
            row = await (await db.execute(
                "SELECT COUNT(*), MAX(CASE WHEN confidence!='manual' THEN updated_at END) FROM track_genres")).fetchone()
        out["decided"], out["last_run"] = (row[0] or 0), (row[1] or "")
    except Exception:
        out["decided"], out["last_run"] = 0, ""
    return out


def _ge_norm_prefix(p: str) -> str:
    p = (p or "").strip().rstrip("/")
    msp, dmp = settings.media_server_music_prefix, settings.docker_music_prefix
    if msp and dmp and p.startswith(msp):
        p = dmp + p[len(msp):]
    return p


async def _ge_get_rules() -> list:
    async with aiosqlite.connect(settings.database_url) as db:
        row = await (await db.execute("SELECT value FROM app_settings WHERE key='ge_folder_rules'")).fetchone()
    try:
        rules = json.loads(row[0]) if row and row[0] else GE_DEFAULT_RULES
    except Exception:
        rules = GE_DEFAULT_RULES
    return [{"prefix": _ge_norm_prefix(r.get("prefix", "")), "family": r.get("family")}
            for r in rules if r.get("prefix") and (r.get("family") in GE_FAMILIES or r.get("family") == "skip")]


class GenreRules(BaseModel):
    rules: List[dict]


@router.get("/genre-engine/rules")
async def genre_engine_rules_get(user: dict = Depends(require_auth)):
    return {"rules": await _ge_get_rules()}


@router.put("/genre-engine/rules")
async def genre_engine_rules_put(req: GenreRules, user: dict = Depends(require_auth)):
    clean = [{"prefix": _ge_norm_prefix(r.get("prefix", "")), "family": r.get("family")}
             for r in req.rules[:50]
             if r.get("prefix") and (r.get("family") in GE_FAMILIES or r.get("family") == "skip")]
    async with aiosqlite.connect(settings.database_url) as db:
        await db.execute(
            "INSERT INTO app_settings (key, value, updated_at) VALUES ('ge_folder_rules', ?, datetime('now')) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (json.dumps(clean),))
        await db.commit()
    return {"ok": True, "rules": clean}

# ══════════════════════════════════════════════════════════════════════════════
#  ONLINE GENRES (M5a) — write Last.fm genres to files, GENRE field only
# ══════════════════════════════════════════════════════════════════════════════

_GENRE_KEY_DISPLAY = {
    "rock": "Rock", "classicrock": "Classic Rock", "hardrock": "Hard Rock", "softrock": "Soft Rock",
    "arenarock": "Arena Rock", "indierock": "Indie Rock", "folkrock": "Folk Rock",
    "southernrock": "Southern Rock", "punkrock": "Punk / New Wave", "alternative": "Alternative Rock",
    "pop": "Pop", "dancepop": "Dance-Pop", "synthpop": "Synth-Pop", "teenpop": "Teen Pop",
    "powerpop": "Power Pop", "adultpop": "Adult Contemporary", "indipop": "Indie Pop",
    "country": "Country", "tradcountry": "Traditional Country", "countrypop": "Country Pop",
    "outlawcountry": "Outlaw Country", "americana": "Americana", "brocountry": "Bro-Country",
    "texascountry": "Texas Country", "rnb": "R&B", "soul": "Soul", "neosoul": "Neo-Soul",
    "funk": "Funk", "motown": "Motown", "quietstorm": "Quiet Storm", "newjack": "New Jack Swing",
    "hiphop": "Hip-Hop", "trap": "Trap", "boombap": "Boom Bap", "gangstarap": "Gangsta Rap",
    "conscrap": "Conscious Rap", "altrap": "Alt Hip-Hop", "metal": "Metal", "heavymetal": "Heavy Metal",
    "thrashmetal": "Thrash Metal", "deathmetal": "Death Metal", "doommetal": "Doom Metal",
    "glaemmetal": "Glam Metal", "dance": "Dance", "edm": "EDM", "house": "House", "techno": "Techno",
    "trance": "Trance", "disco": "Disco", "eurodance": "Eurodance", "folk": "Folk",
    "tradfolk": "Traditional Folk", "indifolk": "Indie Folk", "singersong": "Singer-Songwriter",
    "jazz": "Jazz", "smoothjazz": "Smooth Jazz", "vocaljazz": "Vocal Jazz", "bebop": "Bebop",
    "jazzfusion": "Jazz Fusion", "blues": "Blues", "chicagoblues": "Chicago Blues",
    "deltablues": "Delta Blues", "texasblues": "Texas Blues", "classical": "Classical",
    "orchestral": "Orchestral", "opera": "Opera", "baroque": "Baroque", "ccm": "Christian",
    "ccm-rock": "Christian Rock", "ccm-pop": "Christian Pop", "worship": "Worship",
    "gospel": "Gospel", "sgospel": "Southern Gospel",
}


def _real_write_path(filepath: str) -> str:
    """Same Docker→server path translation write_tags() uses; falls back to the given path."""
    real = filepath
    if settings.media_server_music_prefix and settings.docker_music_prefix \
            and filepath.startswith(settings.docker_music_prefix):
        real = settings.media_server_music_prefix + filepath[len(settings.docker_music_prefix):]
    return real if os.path.exists(real) else filepath


def write_genre_only(filepath: str, genres: List[str]) -> bool:
    """Replace ONLY the genre tag. Every other tag (title, year, comment, art, MOOD…) is untouched."""
    if not genres:
        return False
    path = _real_write_path(filepath)
    ext = os.path.splitext(path)[1].lower()
    try:
        if ext == ".mp3":
            from mutagen.id3 import ID3, TCON, ID3NoHeaderError
            try:
                tags = ID3(path)
            except ID3NoHeaderError:
                tags = ID3()
            tags.delall("TCON")
            tags.add(TCON(encoding=3, text=["; ".join(genres)]))
            tags.save(path, v2_version=3)
        elif ext == ".flac":
            import subprocess
            cmd = ["metaflac", "--remove-tag=GENRE"] + [f"--set-tag=GENRE={g}" for g in genres] + ["--", path]
            res = subprocess.run(cmd, capture_output=True, text=True)
            if res.returncode != 0:
                log.error(f"metaflac genre write failed for {path}: {res.stderr}")
                return False
        elif ext in (".m4a", ".aac", ".mp4", ".alac"):
            from mutagen.mp4 import MP4
            f = MP4(path)
            f["\xa9gen"] = ["; ".join(genres)]
            f.save()
        else:
            from mutagen import File as MFile
            f = MFile(path, easy=True)
            if f is None:
                return False
            f["genre"] = genres
            f.save()
        return True
    except Exception as e:
        log.error(f"Genre write failed for {path}: {e}")
        return False


_og_job = {"status": "idle", "done": 0, "total": 0, "written": 0, "failed": 0, "not_found": 0, "message": ""}
_og_stop = {"flag": False}


async def _ge_nogenre_rows(db, folder: Optional[str], search: Optional[str]) -> list:
    """Songs the engine couldn't decide (no source knew them). Skip-rule folders excluded."""
    q = ("SELECT t.track_id, t.file_path, COALESCE(t.tag_artist, a.name, ''), t.title, "
         "t.genre_1, t.genre_2, t.genre_3 FROM tracks t "
         "LEFT JOIN track_genres g ON g.track_id = t.track_id "
         "LEFT JOIN artists a ON t.artist_id = a.artist_id "
         "WHERE g.track_id IS NULL AND t.file_path IS NOT NULL "
         "AND COALESCE(t.manually_verified, 0) = 0 AND COALESCE(t.missing, 0) = 0")
    params = []
    if folder:
        q += " AND t.file_path LIKE ?"
        params.append(_ge_norm_prefix(folder) + "/%")
    rows = await (await db.execute(q, params)).fetchall()
    skips = [r["prefix"] for r in await _ge_get_rules() if r["family"] == "skip"]
    needle = (search or "").strip().lower()
    out = []
    for tid, fp, artist, title, g1, g2, g3 in rows:
        if any(fp == p or fp.startswith(p + "/") for p in skips):
            continue
        if needle and needle not in (artist or "").lower() and needle not in (title or "").lower():
            continue
        out.append((tid, fp, artist, title, [g for g in (g1, g2, g3) if g], [], "none", None, [], ""))
    from app.routers.groomer import _artist_key
    out.sort(key=lambda r: (_artist_key(r[2] or ""), (r[2] or "").lower(),
                            _ge_album_dir(r[1]).lower(), (r[3] or "").lower()))
    return out


async def _online_genre_rows(scope: str, folder: Optional[str], search: Optional[str] = None,
                             family: Optional[str] = None, track_ids: Optional[List[int]] = None,
                             hide_same: bool = False) -> list:
    """[(track_id, file_path, artist, title, current, proposed, confidence, family, styles)]."""
    await _ge_migrate()
    async with aiosqlite.connect(settings.database_url) as db:
        if scope == "nogenre" and not track_ids:
            return await _ge_nogenre_rows(db, folder, search)
        q = ("SELECT t.track_id, t.file_path, COALESCE(t.tag_artist, a.name, ''), t.title, "
             "t.genre_1, t.genre_2, t.genre_3, g.family, g.styles, g.confidence, g.sources "
             "FROM track_genres g JOIN tracks t ON t.track_id = g.track_id "
             "LEFT JOIN artists a ON t.artist_id = a.artist_id "
             "WHERE COALESCE(t.manually_verified, 0) = 0 AND COALESCE(t.missing, 0) = 0")
        params = []
        if folder:
            q += " AND t.file_path LIKE ?"
            params.append(_ge_norm_prefix(folder) + "/%")
        if family:
            q += " AND g.family = ?"
            params.append(family)
        if track_ids:
            q += f" AND t.track_id IN ({','.join('?' * len(track_ids))})"
            params += list(track_ids)
        rows = await (await db.execute(q, params)).fetchall()
    needle = (search or "").strip().lower()
    out = []
    for tid, fp, artist, title, g1, g2, g3, fam, styles, conf, src in rows:
        st = json.loads(styles or "[]")
        proposed = _ge_display(fam, st)
        current = [g for g in (g1, g2, g3) if g]
        same = [c.lower() for c in current] == [p.lower() for p in proposed[:3]]
        if needle and needle not in (artist or "").lower() and needle not in (title or "").lower():
            continue
        if hide_same and not track_ids and current and conf != "conflict" and \
                {c.lower() for c in current} == {p.lower() for p in proposed[:3]}:
            continue
        if not track_ids:
            if scope == "empty" and current:
                continue
            if scope == "changed" and same:
                continue
            if scope == "check" and conf not in ("check", "conflict", "thin"):
                continue
            if scope == "conflict" and conf != "conflict":
                continue
            if scope == "thin" and conf not in ("thin", "check"):
                continue
            if scope == "manual" and conf != "manual":
                continue
        out.append((tid, fp, artist, title, current, proposed, conf, fam, st, src or ""))
    from app.routers.groomer import _artist_key
    out.sort(key=lambda r: (_artist_key(r[2] or ""), (r[2] or "").lower(),
                            _ge_album_dir(r[1]).lower(), (r[3] or "").lower()))
    return out


class OnlineGenreRequest(BaseModel):
    scope:     str = "changed"            # empty | changed | all | check | manual
    folder:    Optional[str] = None       # /music/... subfolder, optional
    search:    Optional[str] = None       # artist or title contains
    family:    Optional[str] = None       # family key
    track_ids: Optional[List[int]] = None # explicit selection (overrides scope)
    hide_same: bool = False               # skip songs whose file already has the proposed genres


@router.post("/online-genres/preview")
async def online_genres_preview(req: OnlineGenreRequest, user: dict = Depends(require_auth)):
    rows = await _online_genre_rows(req.scope, req.folder, req.search, req.family, req.track_ids, req.hide_same)
    return {
        "count":  len(rows),
        "check":  sum(1 for r in rows if r[6] == "check"),
        "sample": [{"track_id": r[0], "artist": r[2], "title": r[3], "current": r[4],
                    "proposed": r[5], "confidence": r[6]} for r in rows[:300]],
    }


async def _refresh_media_servers() -> list:
    """Ask Plex / Emby / Jellyfin to rescan music so they pick up the new file tags."""
    from app.security import decrypt_token
    done = []
    async with aiosqlite.connect(settings.database_url) as db:
        conns = await (await db.execute(
            "SELECT service, base_url, token_enc FROM connections "
            "WHERE service IN ('plex','emby','jellyfin')")).fetchall()
    async with httpx.AsyncClient(timeout=20.0) as client:
        for svc, base, tok_enc in conns:
            try:
                tok = decrypt_token(tok_enc) if tok_enc else ""
                base = (base or "").rstrip("/")
                if svc == "plex":
                    r = await client.get(f"{base}/library/sections", params={"X-Plex-Token": tok},
                                         headers={"Accept": "application/json"})
                    for d in r.json().get("MediaContainer", {}).get("Directory", []):
                        if d.get("type") == "artist":
                            await client.get(f"{base}/library/sections/{d['key']}/refresh",
                                             params={"X-Plex-Token": tok})
                else:
                    await client.post(f"{base}/Library/Refresh", params={"api_key": tok})
                done.append(svc)
            except Exception as e:
                log.warning(f"Media refresh failed for {svc}: {e}")
    return done


async def _run_online_genres(req: OnlineGenreRequest):
    global _og_job
    _og_stop["flag"] = False
    _og_job = {"status": "running", "done": 0, "total": 0, "written": 0, "failed": 0,
               "not_found": 0, "message": "Preparing…"}
    try:
        rows = await _online_genre_rows(req.scope, req.folder, req.search, req.family, req.track_ids, req.hide_same)
        _og_job["total"] = len(rows)
        loop = asyncio.get_event_loop()
        async with aiosqlite.connect(settings.database_url, timeout=30) as db:
            for i, (tid, fp, artist, title, current, proposed, _conf, _f, _s, _src) in enumerate(rows):
                if _og_stop["flag"]:
                    break
                if not proposed:          # no decision yet (No genre found) — nothing to write
                    _og_job["done"] = i + 1
                    continue
                if not fp or not os.path.exists(_real_write_path(fp)):
                    # File moved/deleted or on a folder ChartHound can't see — mark it, don't count as a failure
                    await db.execute("UPDATE tracks SET missing=1 WHERE track_id=?", (tid,))
                    await db.execute(
                        "INSERT INTO write_log (track_id, file_path, field_changed, old_value, new_value, "
                        "write_status, error_msg) VALUES (?, ?, 'genre', ?, ?, 'skipped', 'file not found')",
                        (tid, fp or "", "; ".join(current), "; ".join(proposed)))
                    _og_job["not_found"] += 1
                    _og_job["done"] = i + 1
                    continue
                ok = await loop.run_in_executor(None, write_genre_only, fp, proposed)
                if ok:   # File-First: DB only follows a successful file write
                    g = (proposed + [None, None, None])[:3]
                    await db.execute("UPDATE tracks SET genre_1=?, genre_2=?, genre_3=? WHERE track_id=?",
                                     (g[0], g[1], g[2], tid))
                    _og_job["written"] += 1
                else:
                    _og_job["failed"] += 1
                await db.execute(
                    "INSERT INTO write_log (track_id, file_path, field_changed, old_value, new_value, "
                    "write_status, error_msg) VALUES (?, ?, 'genre', ?, ?, ?, ?)",
                    (tid, fp, "; ".join(current), "; ".join(proposed),
                     "success" if ok else "failed", None if ok else "see container log"))
                _og_job["done"] = i + 1
                _og_job["message"] = f"{artist} — {title}"
                if (i + 1) % 100 == 0:
                    await db.commit()
                    await asyncio.sleep(0)
            await db.commit()
        refreshed = await _refresh_media_servers() if _og_job["written"] else []
        stopped = _og_stop["flag"]
        _og_job.update({"status": "stopped" if stopped else "done",
                        "message": (f"{'Stopped' if stopped else 'Done'} — {_og_job['written']:,} files updated, "
                                    f"{_og_job['not_found']:,} not found (hidden until a scan finds them), "
                                    f"{_og_job['failed']:,} failed"
                                    + (f" · refresh sent to {', '.join(refreshed)}" if refreshed else ""))})
        log.info(f"Online genres: {_og_job['message']}")
    except Exception as e:
        log.exception("Online genres job failed")
        _og_job.update({"status": "failed", "message": f"Error: {e}"})


@router.post("/online-genres/apply")
async def online_genres_apply(req: OnlineGenreRequest, background_tasks: BackgroundTasks,
                              user: dict = Depends(require_auth)):
    if _og_job.get("status") == "running":
        raise HTTPException(409, "Already running.")
    background_tasks.add_task(_run_online_genres, req)
    return {"ok": True}


@router.get("/online-genres/status")
async def online_genres_status(user: dict = Depends(require_auth)):
    return _og_job


@router.post("/online-genres/stop")
async def online_genres_stop(user: dict = Depends(require_auth)):
    _og_stop["flag"] = True
    return {"ok": True}


@router.get("/genre-engine/taxonomy")
async def genre_engine_taxonomy(user: dict = Depends(require_auth)):
    return {"families": GE_FAMILIES,
            "styles": {k: {"label": v[0], "families": list(v[1])} for k, v in GE_STYLES.items()}}


@router.post("/genre-engine/review")
async def genre_engine_review(req: OnlineGenreRequest, page: int = 1, size: int = 100,
                              user: dict = Depends(require_auth)):
    rows = await _online_genre_rows(req.scope, req.folder, req.search, req.family, hide_same=req.hide_same)
    size = max(20, min(size, 500))
    host = await _host_path_fn()
    from app.routers.groomer import _artist_key
    page = max(1, page)
    chunk = rows[(page - 1) * size: page * size]
    albums = {}
    if chunk:
        async with aiosqlite.connect(settings.database_url) as db:
            albums = dict(await (await db.execute(
                f"SELECT track_id, album_title FROM tracks WHERE track_id IN ({','.join('?' * len(chunk))})",
                [r[0] for r in chunk])).fetchall())
    return {
        "total": len(rows), "page": page, "size": size,
        "check": sum(1 for r in rows if r[6] in ("check", "conflict", "thin")),
        "conflict": sum(1 for r in rows if r[6] == "conflict"),
        "rows": [{"track_id": r[0], "artist": r[2], "title": r[3], "current": r[4],
                  "proposed": r[5], "confidence": r[6], "family": r[7], "styles": r[8],
                  "ext": os.path.splitext(r[1] or "")[1].lower(), "path": host(r[1] or ""),
                  "checked": "approved" in (r[9] or "").split(","),
                  "akey": _artist_key(r[2] or ""),
                  "album": _ge_album_display(_ge_album_name(albums.get(r[0]), r[1] or ""), r[2] or "") or "(no album)",
                  "alkey": _ge_album_dir(r[1] or "")} for r in chunk],
    }


class GenreOverride(BaseModel):
    family:    str
    styles:    List[str] = []
    track_ids: Optional[List[int]] = None
    artist:    Optional[str] = None      # apply to every song by this artist
    album_of:  Optional[int] = None      # apply to every song in this song's album


@router.put("/genre-engine/override")
async def genre_engine_override(req: GenreOverride, user: dict = Depends(require_auth)):
    from app.routers.groomer import _artist_key
    if req.family not in GE_FAMILIES:
        raise HTTPException(400, "Unknown family.")
    styles = []
    for st in req.styles[:3]:
        if st and st in GE_STYLES and req.family in GE_STYLES[st][1] and st not in styles:
            styles.append(st)
    await _ge_migrate()
    async with aiosqlite.connect(settings.database_url, timeout=60) as db:
        ids = list(req.track_ids or [])
        if req.artist:
            want = _artist_key(req.artist)
            rows = await (await db.execute(
                "SELECT t.track_id, COALESCE(t.tag_artist, a.name, '') FROM tracks t "
                "LEFT JOIN artists a ON t.artist_id = a.artist_id")).fetchall()
            ids += [tid for tid, ar in rows if _artist_key(ar) == want]
        if req.album_of:
            fp = await (await db.execute("SELECT file_path FROM tracks WHERE track_id=?",
                                         (req.album_of,))).fetchone()
            if fp and fp[0]:
                ad = _ge_album_dir(fp[0])
                rows = await (await db.execute(
                    "SELECT track_id, file_path FROM tracks WHERE substr(file_path, 1, ?) = ?",
                    (len(ad) + 1, ad + "/"))).fetchall()
                ids += [tid for tid, p in rows if _ge_album_dir(p) == ad]
        ids = sorted(set(ids))
        if not ids:
            raise HTTPException(400, "No songs selected.")
        await db.executemany(
            "INSERT INTO track_genres (track_id, family, styles, confidence, sources, updated_at) "
            "VALUES (?, ?, ?, 'manual', 'manual', datetime('now')) ON CONFLICT(track_id) DO UPDATE SET "
            "family=excluded.family, styles=excluded.styles, confidence='manual', sources='manual', "
            "updated_at=excluded.updated_at",
            [(tid, req.family, json.dumps(styles)) for tid in ids])
        await db.commit()
    return {"ok": True, "updated": len(ids), "genre": _ge_display(req.family, styles), "track_ids": ids[:5000]}


@router.get("/genre-engine/track/{track_id}")
async def genre_engine_track(track_id: int, user: dict = Depends(require_auth)):
    """One song's current genre decision — for the Top Dogs ✎ editor."""
    await _ge_migrate()
    async with aiosqlite.connect(settings.database_url) as db:
        t = await (await db.execute(
            "SELECT COALESCE(t.tag_artist, a.name, ''), COALESCE(t.title, ''), t.album_title, t.file_path, "
            "t.genre_1, t.genre_2, t.genre_3 FROM tracks t LEFT JOIN artists a ON t.artist_id = a.artist_id "
            "WHERE t.track_id=?", (track_id,))).fetchone()
        g = await (await db.execute("SELECT family, styles, confidence FROM track_genres WHERE track_id=?",
                                    (track_id,))).fetchone()
    if not t:
        raise HTTPException(404, "Song not found.")
    return {"track_id": track_id, "artist": t[0], "title": t[1],
            "album": _ge_album_display(_ge_album_name(t[2], t[3] or ""), t[0]) or "(no album)",
            "current": [x for x in t[4:7] if x],
            "ext": os.path.splitext(t[3] or "")[1].lower(),
            "family": g[0] if g else None, "styles": json.loads(g[1] or "[]") if g else [],
            "confidence": g[2] if g else "none"}


class GenreApprove(BaseModel):
    track_ids: Optional[List[int]] = None
    artist:    Optional[str] = None      # every song by this artist
    filters:   Optional[OnlineGenreRequest] = None   # every flagged song matching the Review filters


@router.post("/genre-engine/approve")
async def genre_engine_approve(req: GenreApprove, user: dict = Depends(require_auth)):
    """✓ Looks right: keep the engine's genre as your own decision (never overwritten, no longer a conflict)."""
    from app.routers.groomer import _artist_key
    await _ge_migrate()
    async with aiosqlite.connect(settings.database_url, timeout=60) as db:
        ids = list(req.track_ids or [])
        if req.artist:
            want = _artist_key(req.artist)
            rows = await (await db.execute(
                "SELECT t.track_id, COALESCE(t.tag_artist, a.name, '') FROM tracks t "
                "LEFT JOIN artists a ON t.artist_id = a.artist_id")).fetchall()
            ids += [tid for tid, ar in rows if _artist_key(ar) == want]
        if req.filters:
            f = req.filters
            rows = await _online_genre_rows(f.scope, f.folder, f.search, f.family, None, f.hide_same)
            ids += [r[0] for r in rows if r[6] in ("conflict", "thin", "check")]
        ids = sorted(set(ids))
        if not ids:
            raise HTTPException(400, "No flagged songs match.")
        n = 0
        for i in range(0, len(ids), 500):
            part = ids[i:i + 500]
            cur = await db.execute(
                f"UPDATE track_genres SET confidence='manual', sources=TRIM(COALESCE(sources, '') || ',approved', ','), updated_at=datetime('now') "
                f"WHERE confidence != 'manual' AND track_id IN ({','.join('?' * len(part))})", part)
            n += cur.rowcount
        await db.commit()
    return {"ok": True, "checked": n}


@router.post("/genre-engine/revote")
async def genre_engine_revote(user: dict = Depends(require_auth)):
    """Recalculate the vote from cached lookups only — no network calls, takes seconds."""
    t = _ge_task["task"]
    if t is not None and not t.done():
        raise HTTPException(409, "The engine is running.")
    _ge_stop["flag"] = False
    _ge_revote_only["flag"] = True
    try:
        await _run_genre_engine()
    finally:
        _ge_revote_only["flag"] = False
    return _ge_job


class GenreReset(BaseModel):
    track_ids: List[int]


@router.post("/genre-engine/reset")
async def genre_engine_reset(req: GenreReset, user: dict = Depends(require_auth)):
    """↻ Drop your edit/approval on these songs and let the engine decide again (cached lookups only)."""
    ids = sorted(set(req.track_ids or []))[:500]
    if not ids:
        raise HTTPException(400, "No songs selected.")
    t = _ge_task["task"]
    if t is not None and not t.done():
        raise HTTPException(409, "The engine is running.")
    qm = ",".join("?" * len(ids))
    async with aiosqlite.connect(settings.database_url, timeout=60) as db:
        await db.execute(f"DELETE FROM track_genres WHERE track_id IN ({qm})", ids)
        await db.commit()
    await genre_engine_revote(user)
    async with aiosqlite.connect(settings.database_url) as db:
        rows = await (await db.execute(
            f"SELECT track_id, family, styles, confidence FROM track_genres WHERE track_id IN ({qm})", ids)).fetchall()
    got = {r[0]: r for r in rows}
    return {"ok": True, "songs": [
        {"track_id": i, "genre": _ge_display(got[i][1], json.loads(got[i][2] or "[]")) if i in got else [],
         "confidence": got[i][3] if i in got else "none"} for i in ids]}


# ══════════════════════════════════════════════════════════════════════════════
#  PREVIEW PLAYER — stream one track to the browser (Genre Review ▶)
#  <audio> can't send the Bearer header, so the page first gets a 10-minute pass
#  for ONE track, signed with a separate key so it can never act as a login token.
# ══════════════════════════════════════════════════════════════════════════════
_PLAY_TYPES = {".mp3": "audio/mpeg", ".flac": "audio/flac", ".m4a": "audio/mp4", ".aac": "audio/aac",
               ".ogg": "audio/ogg", ".oga": "audio/ogg", ".opus": "audio/ogg", ".wav": "audio/wav"}
_PLAY_KEY = settings.secret_key + ":play"


def _play_path_ok(p: str) -> bool:
    """Path comes from the library DB (never the browser); serve only a real audio file at its true location."""
    try:
        real = os.path.realpath(p)
    except Exception:
        return False
    return os.path.isfile(real) and os.path.splitext(real)[1].lower() in _PLAY_TYPES


@router.post("/play-token/{track_id}")
async def play_token(track_id: int, user: dict = Depends(require_auth)):
    exp = int(time.time()) + 600
    return {"t": jwt.encode({"tid": track_id, "exp": exp}, _PLAY_KEY, algorithm="HS256")}


@router.get("/play/{track_id}")
async def play_track(track_id: int, t: str, request: Request):
    try:
        claims = jwt.decode(t, _PLAY_KEY, algorithms=["HS256"])
    except JWTError:
        raise HTTPException(401, "Play link expired — press ▶ again.")
    if claims.get("tid") != track_id:
        raise HTTPException(403, "Play link is for a different track.")
    async with aiosqlite.connect(settings.database_url) as db:
        row = await (await db.execute("SELECT file_path FROM tracks WHERE track_id=?", (track_id,))).fetchone()
    fp = row[0] if row else ""
    mime = _PLAY_TYPES.get(os.path.splitext(fp)[1].lower())
    if not fp or not mime:
        raise HTTPException(415, "This file type can't play in a browser.")
    if not _play_path_ok(fp) or not os.path.isfile(fp):
        raise HTTPException(404, "File not found in the music library.")

    size = os.path.getsize(fp)
    start, end, status = 0, size - 1, 200
    m = re.match(r"bytes=(\d*)-(\d*)$", (request.headers.get("range") or "").strip())
    if m and (m[1] or m[2]):
        if m[1]:
            start, end = int(m[1]), (int(m[2]) if m[2] else size - 1)
        else:
            start = max(0, size - int(m[2]))
        end = min(end, size - 1)
        if start >= size or start > end:
            return Response(status_code=416, headers={"Content-Range": f"bytes */{size}"})
        status = 206

    def chunks():
        with open(fp, "rb") as f:
            f.seek(start)
            left = end - start + 1
            while left > 0:
                buf = f.read(min(65536, left))
                if not buf:
                    break
                left -= len(buf)
                yield buf

    headers = {"Accept-Ranges": "bytes", "Content-Length": str(end - start + 1), "Cache-Control": "no-store"}
    if status == 206:
        headers["Content-Range"] = f"bytes {start}-{end}/{size}"
    return StreamingResponse(chunks(), status_code=status, media_type=mime, headers=headers)


# ══════════════════════════════════════════════════════════════════════════════
#  REVIEW HELPERS — real server path (📋 copy) and per-source "why?" breakdown
# ══════════════════════════════════════════════════════════════════════════════
async def _host_path_fn():
    """Docker path → path on the server (Kennel Path Translator, else env). Unmapped paths pass through."""
    m = {}
    try:
        async with aiosqlite.connect(settings.database_url) as db:
            m = {k: v for k, v in await (await db.execute(
                "SELECT key, value FROM app_settings WHERE key IN ('path_server_prefix','path_docker_prefix')")).fetchall()}
    except Exception:
        pass
    server = (m.get("path_server_prefix") or settings.media_server_music_prefix or "").rstrip("/")
    docker = (m.get("path_docker_prefix") or settings.docker_music_prefix or "/music").rstrip("/")

    def host(fp: str) -> str:
        if server and docker and (fp == docker or fp.startswith(docker + "/")):
            return server + fp[len(docker):]
        return fp
    return host


@router.get("/genre-engine/why/{track_id}")
async def genre_engine_why(track_id: int, user: dict = Depends(require_auth)):
    """What each source said for one song — read from the caches, no lookups."""
    from app.routers.groomer import _artist_key, _title_key
    await _ge_migrate()
    async with aiosqlite.connect(settings.database_url) as db:
        row = await (await db.execute(
            "SELECT COALESCE(t.tag_artist, a.name, ''), t.title, t.file_path, t.album_title "
            "FROM tracks t LEFT JOIN artists a ON t.artist_id = a.artist_id WHERE t.track_id=?",
            (track_id,))).fetchone()
        if not row:
            raise HTTPException(404, "Song not found.")
        artist, title, fp, alb = row
        g = await (await db.execute("SELECT sources, confidence FROM track_genres WHERE track_id=?",
                                    (track_id,))).fetchone()
        used = (g[0] or "").split(",") if g else []
        alt = _ge_alt_artist(artist, title, fp) if "alt" in used else None
        folder = await (await db.execute(
            "SELECT COALESCE(t.tag_artist, a.name, ''), t.file_path, t.album_title "
            "FROM tracks t LEFT JOIN artists a ON t.artist_id = a.artist_id WHERE substr(t.file_path, 1, ?) = ?",
            (len(os.path.dirname(fp)) + 1, os.path.dirname(fp) + "/"))).fetchall()
        folder = [f for f in folder if os.path.dirname(f[1]) == os.path.dirname(fp)] or [(artist, fp, alb)]
        # Same album-key rule as the engine: majority artist + album name; compilations use the artist profile
        counts = {}
        for a, *_ in folder:
            k = _artist_key(a)
            if k:
                counts[k] = counts.get(k, 0) + 1
        ak, tk = _artist_key(alt or artist), _title_key(title)
        keys = []
        if counts and not alt:
            top_ak, top_n = max(counts.items(), key=lambda kv: kv[1])
            album = _ge_album_name(folder[0][2], folder[0][1])
            is_comp = (len(counts) >= 4 and top_n / len(folder) < 0.5) or "various" in album.lower()
            if album and not is_comp:
                keys.append(f"album:{top_ak}|{_title_key(album)}")
        keys.append(f"artist:{ak}")

        async def one(sql, args):
            r = await (await db.execute(sql, args)).fetchone()
            return r
        disc = None
        for k in keys:
            disc = await one("SELECT genres, styles FROM ge_discogs WHERE lookup_key=? AND status='ok'", (k,))
            if disc:
                break
        mb = await one("SELECT tags FROM ge_mb_artists WHERE artist_key=? AND status='ok'", (ak,))
        try:
            lt = await one("SELECT tags FROM lastfm_tracks WHERE artist_key=? AND title_key=? AND status='ok'", (ak, tk))
            la = await one("SELECT tags FROM lastfm_artists WHERE artist_key=? AND status='ok'", (ak,))
        except Exception:
            lt = la = None

    def tags(r, i=0):
        try:
            return [str(x) for x in json.loads(r[i] or "[]")][:5] if r else []
        except Exception:
            return []
    return {
        "sources": [
            {"name": "Discogs album styles", "weight": 3, "tags": tags(disc, 1)},
            {"name": "Discogs album genres", "weight": 2, "tags": tags(disc, 0)},
            {"name": "MusicBrainz artist",   "weight": 2, "tags": tags(mb)},
            {"name": "Last.fm song",         "weight": 2, "tags": tags(lt)},
            {"name": "Last.fm artist",       "weight": 1, "tags": tags(la)},
        ],
        "decided_by": "folder rule" if "rule" in used else ("your own tags" if "your tags" in used else None),
        "alt_artist": alt,
        "checked": "approved" in used,
        "confidence": g[1] if g else None,
    }


# ══════════════════════════════════════════════════════════════════════════════
#  ARTIST CHECK (M10d) — find and fix bad ARTIST tags
#  album/series name as artist · "Artist - Title" run together · several artists
#  in one tag · spelling twins (10cc / 10 Cc). Proposals only; nothing is written
#  until the user accepts. Writes ARTIST (and TITLE only for run-together tags).
# ══════════════════════════════════════════════════════════════════════════════
_AC_SERIES = re.compile(r"\b(greatest|essentials?|hits|songs|collection|compilation|best of|top \d+|vol(ume)?\.?\s*\d+|"
                        r"various artists|now that'?s|anthology|\d{2,4}s)\b|\s-\s\d+\s-\s", re.I)
_AC_REASONS = {"joined": "Artist and title run together", "album": "Album or series name as artist",
               "multi": "Several artists in one tag", "twin": "Spelled differently elsewhere"}
_ac_job = {"status": "idle", "done": 0, "total": 0, "written": 0, "failed": 0, "message": ""}


def _ac_twin_key(a: str) -> str:
    return re.sub(r"[^\w]", "", (a or "").lower())


def _ac_clean_lead(s: str) -> str:
    return re.sub(r"^\s*\d{1,3}\s*[-._)]+\s*", "", s or "").strip()


async def _ac_migrate(db):
    await db.execute("CREATE TABLE IF NOT EXISTS ac_ignore (track_id INTEGER PRIMARY KEY)")


async def _ac_rows() -> list:
    """Every suspect song: dicts with track_id, artist, title, proposed, proposed_title, reason, album, fp."""
    from app.routers.groomer import _artist_key, _title_key
    await _ge_migrate()
    async with aiosqlite.connect(settings.database_url) as db:
        await _ac_migrate(db)
        await db.commit()
        rows = await (await db.execute(
            "SELECT t.track_id, t.file_path, COALESCE(t.tag_artist, a.name, ''), COALESCE(t.title, ''), t.album_title "
            "FROM tracks t LEFT JOIN artists a ON t.artist_id = a.artist_id "
            "WHERE COALESCE(t.missing, 0) = 0 AND t.file_path IS NOT NULL "
            "AND t.track_id NOT IN (SELECT track_id FROM ac_ignore)")).fetchall()
    # library-wide counts: exact spellings per twin key, and artist keys that exist on their own
    spell, solo = {}, set()
    for _tid, _fp, artist, _t, _al in rows:
        if artist:
            k = _ac_twin_key(artist)
            spell.setdefault(k, {})
            spell[k][artist] = spell[k].get(artist, 0) + 1
            solo.add(_artist_key(artist))
    out = []
    for tid, fp, artist, title, alb in rows:
        a = (artist or "").strip()
        if not a:
            continue
        prop, ptitle, reason = None, None, None
        # 1. "Artist - Title" run together (often in the title too)
        if " - " in a:
            left, right = a.split(" - ", 1)
            left = _ac_clean_lead(left)
            tk, rk = _title_key(title), _title_key(right)
            if left and not re.fullmatch(r"[\d\s._-]+", left) and rk and (
                    rk == tk or (tk and (rk in tk or tk in rk)) or _title_key(a) == tk):
                reason, prop = "joined", left
                if _title_key(a) == tk or _title_key(title).startswith(_title_key(left) + " "):
                    ptitle = right.strip()
        # 2. album / series name used as the artist
        if not reason:
            album = _ge_album_display(_ge_album_name(alb, fp or ""), "")
            same = _norm_key(a) in (_norm_key(album), _norm_key(_ge_album_name(None, fp or "")))
            alt = _ge_alt_artist(a, title, fp or "")
            if same and (alt or _AC_SERIES.search(a)):
                reason, prop = "album", alt
            elif _AC_SERIES.search(a) and alt and _artist_key(alt) != _artist_key(a):
                reason, prop = "album", alt
        # 3. several artists in one tag
        if not reason:
            parts = None
            if ";" in a:
                parts = [p.strip() for p in a.split(";") if p.strip()]
            elif " / " in a:
                parts = [p.strip() for p in a.split(" / ") if p.strip()]
            elif "," in a:
                p = [x.strip() for x in a.split(",") if x.strip()]
                if len(p) > 1 and _artist_key(p[0]) in solo and _artist_key(p[0]) != _artist_key(a):
                    parts = p
            if parts and len(parts) > 1:
                reason, prop = "multi", parts[0]
        # 4. spelling twin of a more common spelling
        if not reason:
            sp = spell.get(_ac_twin_key(a), {})
            if len(sp) > 1:
                best = max(sp.items(), key=lambda kv: (kv[1], kv[0] != kv[0].lower(), kv[0]))[0]
                if best != a:
                    reason, prop = "twin", best
        if reason:
            out.append({"track_id": tid, "fp": fp or "", "artist": a, "title": title, "album": alb,
                        "proposed": prop or "", "proposed_title": ptitle or "", "reason": reason})
    out.sort(key=lambda r: (_artist_key(r["artist"]), r["artist"].lower(),
                            _ge_album_dir(r["fp"]).lower(), r["title"].lower()))
    return out


def _norm_key(s: str) -> str:
    return re.sub(r"[^\w]", "", (s or "").lower())


class ArtistCheckRequest(BaseModel):
    reason: str = "all"            # all | joined | album | multi | twin | nofix
    search: Optional[str] = None


@router.post("/artist-check/list")
async def artist_check_list(req: ArtistCheckRequest, page: int = 1, size: int = 100,
                            user: dict = Depends(require_auth)):
    from app.routers.groomer import _artist_key
    rows = await _ac_rows()
    counts = {k: sum(1 for r in rows if r["reason"] == k) for k in _AC_REASONS}
    counts["nofix"] = sum(1 for r in rows if not r["proposed"])
    if req.reason == "nofix":
        rows = [r for r in rows if not r["proposed"]]
    elif req.reason in _AC_REASONS:
        rows = [r for r in rows if r["reason"] == req.reason]
    needle = (req.search or "").strip().lower()
    if needle:
        rows = [r for r in rows if needle in r["artist"].lower() or needle in r["title"].lower()]
    size = max(20, min(size, 500))
    page = max(1, page)
    chunk = rows[(page - 1) * size: page * size]
    return {"total": len(rows), "page": page, "size": size, "counts": counts, "all": sum(counts[k] for k in _AC_REASONS),
            "rows": [{"track_id": r["track_id"], "artist": r["artist"], "title": r["title"],
                      "proposed": r["proposed"], "proposed_title": r["proposed_title"],
                      "reason": r["reason"], "why": _AC_REASONS[r["reason"]],
                      "album": _ge_album_display(_ge_album_name(r["album"], r["fp"]), "") or "(no album)",
                      "akey": _artist_key(r["artist"]) or r["artist"].lower(),
                      "ext": os.path.splitext(r["fp"])[1].lower()} for r in chunk]}


class ArtistIgnore(BaseModel):
    track_ids: List[int]


@router.post("/artist-check/ignore")
async def artist_check_ignore(req: ArtistIgnore, user: dict = Depends(require_auth)):
    """✕ Keep as is — the song is never flagged again."""
    async with aiosqlite.connect(settings.database_url) as db:
        await _ac_migrate(db)
        await db.executemany("INSERT OR IGNORE INTO ac_ignore (track_id) VALUES (?)",
                             [(t,) for t in (req.track_ids or [])[:5000]])
        await db.commit()
    return {"ok": True, "ignored": len(req.track_ids or [])}


def write_artist_title(filepath: str, artist: str, title: Optional[str] = None) -> bool:
    """Replace ONLY the ARTIST tag (and TITLE when given). Everything else is untouched."""
    path = _real_write_path(filepath)
    ext = os.path.splitext(path)[1].lower()
    try:
        if ext == ".mp3":
            from mutagen.id3 import ID3, TPE1, TIT2, ID3NoHeaderError
            try:
                tags = ID3(path)
            except ID3NoHeaderError:
                tags = ID3()
            tags.delall("TPE1")
            tags.add(TPE1(encoding=3, text=[artist]))
            if title:
                tags.delall("TIT2")
                tags.add(TIT2(encoding=3, text=[title]))
            tags.save(path, v2_version=3)
        elif ext == ".flac":
            import subprocess
            cmd = ["metaflac", "--remove-tag=ARTIST", f"--set-tag=ARTIST={artist}"]
            if title:
                cmd += ["--remove-tag=TITLE", f"--set-tag=TITLE={title}"]
            res = subprocess.run(cmd + ["--", path], capture_output=True, text=True)
            if res.returncode != 0:
                log.error(f"metaflac artist write failed for {path}: {res.stderr}")
                return False
        elif ext in (".m4a", ".aac", ".mp4", ".alac"):
            from mutagen.mp4 import MP4
            f = MP4(path)
            f["\xa9ART"] = [artist]
            if title:
                f["\xa9nam"] = [title]
            f.save()
        else:
            from mutagen import File as MFile
            f = MFile(path, easy=True)
            if f is None:
                return False
            f["artist"] = [artist]
            if title:
                f["title"] = [title]
            f.save()
        return True
    except Exception as e:
        log.error(f"Artist write failed for {path}: {e}")
        return False


class ArtistFix(BaseModel):
    track_id: int
    artist: str
    title: Optional[str] = None


class ArtistWriteRequest(BaseModel):
    items: List[ArtistFix] = []
    filters: Optional[ArtistCheckRequest] = None   # accept every proposal matching these filters (all pages)


async def _run_artist_write(items: List[ArtistFix]):
    global _ac_job
    _ac_job = {"status": "running", "done": 0, "total": len(items), "written": 0, "failed": 0,
               "message": "Writing…"}
    loop = asyncio.get_event_loop()
    try:
        async with aiosqlite.connect(settings.database_url, timeout=30) as db:
            for i, it in enumerate(items):
                row = await (await db.execute(
                    "SELECT t.file_path, COALESCE(t.tag_artist, a.name, ''), COALESCE(t.title, '') FROM tracks t "
                    "LEFT JOIN artists a ON t.artist_id = a.artist_id WHERE t.track_id=?", (it.track_id,))).fetchone()
                artist = (it.artist or "").strip()
                title = (it.title or "").strip() or None
                if not row or not artist:
                    _ac_job["failed"] += 1
                    continue
                fp, old_a, old_t = row
                if not fp or not os.path.exists(_real_write_path(fp)):
                    await db.execute("UPDATE tracks SET missing=1 WHERE track_id=?", (it.track_id,))
                    _ac_job["failed"] += 1
                    continue
                ok = await loop.run_in_executor(None, write_artist_title, fp, artist, title)
                if ok:   # File-First: DB only follows a successful file write
                    await db.execute("UPDATE tracks SET tag_artist=? WHERE track_id=?", (artist, it.track_id))
                    if title:
                        await db.execute("UPDATE tracks SET title=? WHERE track_id=?", (title, it.track_id))
                    _ac_job["written"] += 1
                else:
                    _ac_job["failed"] += 1
                for field, old, new in (("artist", old_a, artist), ("title", old_t, title)):
                    if new:
                        await db.execute(
                            "INSERT INTO write_log (track_id, file_path, field_changed, old_value, new_value, "
                            "write_status, error_msg) VALUES (?, ?, ?, ?, ?, ?, ?)",
                            (it.track_id, fp, field, old, new, "success" if ok else "failed",
                             None if ok else "see container log"))
                _ac_job["done"] = i + 1
                _ac_job["message"] = f"{artist} — {title or old_t}"
                if (i + 1) % 100 == 0:
                    await db.commit()
                    await asyncio.sleep(0)
            await db.commit()
        refreshed = await _refresh_media_servers() if _ac_job["written"] else []
        engine = ""
        t = _ge_task["task"]
        if _ac_job["written"] and (t is None or t.done()):
            _ge_task["task"] = asyncio.create_task(_run_genre_engine())
            engine = " · Genre Engine started to look up the corrected artists"
        _ac_job.update({"status": "done", "message": (
            f"Done — {_ac_job['written']:,} files updated, {_ac_job['failed']:,} failed"
            + (f" · refresh sent to {', '.join(refreshed)}" if refreshed else "") + engine
            + ". Next: Top Dogs ① SCAN to restore Hit Scores.")})
        log.info(f"Artist check: {_ac_job['message']}")
    except Exception as e:
        log.exception("Artist write failed")
        _ac_job.update({"status": "failed", "message": f"Error: {e}"})


@router.post("/artist-check/write")
async def artist_check_write(req: ArtistWriteRequest, background_tasks: BackgroundTasks,
                             user: dict = Depends(require_auth)):
    if _ac_job.get("status") == "running":
        raise HTTPException(409, "Already writing.")
    items = [i for i in req.items[:5000] if (i.artist or "").strip()]
    if req.filters:
        f = req.filters
        rows = await _ac_rows()
        if f.reason in _AC_REASONS:
            rows = [r for r in rows if r["reason"] == f.reason]
        needle = (f.search or "").strip().lower()
        if needle:
            rows = [r for r in rows if needle in r["artist"].lower() or needle in r["title"].lower()]
        items += [ArtistFix(track_id=r["track_id"], artist=r["proposed"], title=r["proposed_title"] or None)
                  for r in rows if r["proposed"]][:5000]
    if not items:
        raise HTTPException(400, "Nothing to write — no artist given.")
    background_tasks.add_task(_run_artist_write, items)
    return {"ok": True, "count": len(items)}


@router.get("/artist-check/status")
async def artist_check_status(user: dict = Depends(require_auth)):
    return _ac_job
