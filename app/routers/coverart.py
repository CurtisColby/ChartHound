# © 2026 Colby R. Curtis | ChartHound: The New World
# All Rights Reserved.
"""
ChartHound — 🖼 Cover Art (M12c, The Veterinarian)
CHECK finds album folders with no cover Plex can read and artist folders with no artist photo.
Art already on disk comes first (folder, parent, Artwork/Scans/Covers subfolder), then the
Cover Art Archive (MusicBrainz), then iTunes (20/min fixed window), Deezer for artist photos.
Matches must agree on artist AND album. Various Artists albums with nothing found get a
generic titled cover. Nothing is written until you SAVE; existing art is never overwritten
(except a generic cover ChartHound made itself). Only paths are stored — never image blobs.
"""

import asyncio
import hashlib
import io
import json
import logging
import os
import re
import time
from collections import Counter
from typing import List, Optional

import aiosqlite
import httpx
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from app.config import get_settings
from app.deps import require_auth

log = logging.getLogger("charthound.coverart")
router = APIRouter(prefix="/api/art", tags=["coverart"])
settings = get_settings()

_CACHE = os.path.join(os.path.dirname(settings.database_url) or "/data", "art_cache")
_IMG_KEY = settings.secret_key + ":art"
_AUDIO = (".mp3", ".flac", ".m4a", ".mp4", ".aac", ".ogg", ".opus", ".wma", ".wav", ".aiff", ".ape", ".wv")
_IMG = (".jpg", ".jpeg", ".png")
_ALBUM_ART = {"cover", "folder", "album", "front", "poster"}
_ARTIST_ART = {"artist", "poster", "folder"}
_DISC_RE = re.compile(r"^(cd|disc|disk)\s*\d+$", re.I)
_ART_DIRS = re.compile(r"^(artwork|art|scans?|covers?|images?|pics?|jpg|booklet)$", re.I)
_BAD_IMG = re.compile(r"back|inlay|inside|tray|booklet|spine|matrix|disc|\bcd\b|label|rear", re.I)
_UA = {"User-Agent": "ChartHound/1.0 (https://github.com/CurtisColby/ChartHound)"}
_VA = {"variousartists", "various", "va"}

_job = {"status": "idle", "phase": "", "done": 0, "total": 0, "found": 0, "message": ""}
_stop = {"flag": False}
_task = {"task": None}

# Generic cover colours per genre family (top, bottom)
_FAM_COL = {
    "rock": ("#7f1d1d", "#1c1917"), "pop": ("#9d174d", "#312e81"), "country": ("#92400e", "#3f2a14"),
    "rnb": ("#5b21b6", "#1e1b4b"), "hiphop": ("#a16207", "#171717"), "metal": ("#3f3f46", "#09090b"),
    "dance": ("#0e7490", "#4c1d95"), "folk": ("#3f6212", "#292524"), "jazz": ("#1e3a8a", "#0f172a"),
    "blues": ("#1d4ed8", "#172554"), "classical": ("#78716c", "#1c1917"), "ccm": ("#0369a1", "#f8fafc"),
    "world": ("#047857", "#78350f"), "holiday": ("#b91c1c", "#14532d"),
}


# ══════════════════════════════════════════════════════════════════════════════
#  DATABASE
# ══════════════════════════════════════════════════════════════════════════════

async def _migrate(db):
    await db.execute("""CREATE TABLE IF NOT EXISTS art_items (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        kind TEXT NOT NULL,              -- 'album' | 'artist'
        root TEXT NOT NULL,              -- album root (parent of CD1/CD2) or artist folder
        targets TEXT,                    -- JSON list of folders that get the file
        artist TEXT, album TEXT, family TEXT,
        source TEXT,                     -- local | caa | itunes | deezer | generic
        src TEXT,                        -- local image path or remote URL
        cache TEXT,                      -- prepared JPEG under /data/art_cache
        status TEXT NOT NULL,            -- found | none | written | skipped
        generic INTEGER DEFAULT 0,       -- 1 = the folder holds a generic cover ChartHound saved (may be replaced)
        checked_at TEXT NOT NULL DEFAULT (datetime('now')),
        UNIQUE(kind, root))""")
    await db.commit()


# ══════════════════════════════════════════════════════════════════════════════
#  FILESYSTEM CHECK (runs in a worker thread)
# ══════════════════════════════════════════════════════════════════════════════

def _listdir(d: str) -> list:
    try:
        return os.listdir(d)
    except OSError:
        return []


def _has_named(folder: str, names: set) -> Optional[str]:
    for f in _listdir(folder):
        stem, ext = os.path.splitext(f)
        if ext.lower() in _IMG and stem.lower() in names:
            return os.path.join(folder, f)
    return None


def _embedded(folder: str) -> bool:
    """True if the first audio file in the folder carries a picture."""
    import mutagen
    for f in sorted(_listdir(folder)):
        if f.lower().endswith(_AUDIO):
            try:
                m = mutagen.File(os.path.join(folder, f))
            except Exception:
                return False
            if m is None:
                return False
            if getattr(m, "pictures", None):
                return True
            tags = getattr(m, "tags", None) or {}
            try:
                keys = list(tags.keys())
            except Exception:
                keys = []
            return any(str(k).startswith("APIC") or k == "covr" or str(k).lower() == "metadata_block_picture"
                       for k in keys)
    return False


def _local_candidate(root: str, targets: list) -> Optional[str]:
    """Best front-cover image already on disk for this album, or None."""
    places = list(dict.fromkeys(targets + [root]))
    for d in [root] + targets:
        for f in _listdir(d):
            p = os.path.join(d, f)
            if _ART_DIRS.match(f) and os.path.isdir(p):
                places.append(p)
    best = None
    for d in places:
        for f in _listdir(d):
            stem, ext = os.path.splitext(f)
            if ext.lower() not in _IMG:
                continue
            p = os.path.join(d, f)
            try:
                size = os.path.getsize(p)
            except OSError:
                continue
            if size < 10_000:
                continue
            s = stem.lower()
            score = 3 if "front" in s else 2 if ("cover" in s or "folder" in s) else 0 if _BAD_IMG.search(s) else 1
            if score == 0:
                continue
            key = (score, size)
            if best is None or key > best[0]:
                best = (key, p)
    return best[1] if best else None


def _scan_sync(rows: list, items: dict) -> dict:
    """rows [(file_path, artist, album, family, holiday)] → {'albums': [...], 'artists': [...]}"""
    from app.routers.groomer import _artist_key
    from app.routers.retriever import _ge_album_name
    groups = {}
    for fp, artist, album, fam, hol in rows:
        folder = os.path.dirname(fp)
        root = os.path.dirname(folder) if _DISC_RE.match(os.path.basename(folder).strip()) else folder
        g = groups.setdefault(root, {"targets": set(), "artists": Counter(), "albums": Counter(),
                                     "fams": Counter(), "hol": 0, "n": 0, "fp": fp})
        g["targets"].add(folder)
        g["n"] += 1
        if artist:
            g["artists"][artist] += 1
        name = _ge_album_name(album, fp)
        if name:
            g["albums"][name] += 1
        if fam:
            g["fams"][fam] += 1
        g["hol"] += 1 if hol else 0
    roots = {os.path.realpath(p) for p in (settings.docker_music_prefix, "/media", "/music", "/") if p}
    albums, artist_dirs = [], {}
    for i, (root, g) in enumerate(groups.items()):
        if _stop["flag"]:
            break
        if i % 200 == 0:
            _job.update({"done": i, "total": len(groups)})
        targets = sorted(g["targets"])
        keys = Counter()
        for a, n in g["artists"].items():
            keys[_artist_key(a)] += n
        top_ak, top_n = keys.most_common(1)[0] if keys else ("", 0)
        va = (len(keys) >= 4 and top_n / max(g["n"], 1) < 0.5) or top_ak in _VA
        artist = "Various Artists" if va else next((a for a, _n in g["artists"].most_common() if _artist_key(a) == top_ak), "")
        album = g["albums"].most_common(1)[0][0] if g["albums"] else os.path.basename(root)
        fam = "holiday" if g["hol"] * 2 >= g["n"] else (g["fams"].most_common(1)[0][0] if g["fams"] else "")
        it = items.get(("album", root))
        has = all(_has_named(t, _ALBUM_ART) for t in targets) or _embedded(targets[0])
        gen_only = bool(it and it["generic"])
        if has and not gen_only:
            continue
        folder_name = re.sub(r"\s+", " ", re.sub(r"[\(\[][^\)\]]*[\)\]]", " ", os.path.basename(root))).strip()
        albums.append({"root": root, "targets": targets, "artist": artist, "album": album, "va": va,
                       "names": list(dict.fromkeys([album, folder_name])),
                       "family": fam, "local": None if gen_only else _local_candidate(root, targets),
                       "generic_written": gen_only})
        # artist folder = parent of the album root, only when its name IS the artist
        if not va and artist:
            parent = os.path.dirname(root)
            if os.path.realpath(parent) not in roots and _artist_key(os.path.basename(parent)) == top_ak:
                artist_dirs.setdefault(parent, artist)
    # every artist folder (also for albums that already have covers)
    for root, g in groups.items():
        parent = os.path.dirname(root)
        if parent in artist_dirs or os.path.realpath(parent) in roots or not g["artists"]:
            continue
        a, _n = g["artists"].most_common(1)[0]
        if _artist_key(os.path.basename(parent)) == _artist_key(a) and _artist_key(a) not in _VA:
            artist_dirs[parent] = a
    artists = [{"root": d, "artist": a} for d, a in artist_dirs.items() if not _has_named(d, _ARTIST_ART)]
    return {"albums": albums, "artists": artists}


# ══════════════════════════════════════════════════════════════════════════════
#  IMAGE PREPARATION + GENERIC COVERS (Pillow)
# ══════════════════════════════════════════════════════════════════════════════

def _prepare(data: bytes, dest: str) -> bool:
    """Any image bytes → RGB JPEG (max 1000 px) at dest. False if not a usable image."""
    from PIL import Image
    try:
        im = Image.open(io.BytesIO(data))
        im.load()
        if min(im.size) < 150:
            return False
        im = im.convert("RGB")
        im.thumbnail((1000, 1000))
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        im.save(dest, "JPEG", quality=88)
        return True
    except Exception:
        return False


def _hex(c: str) -> tuple:
    return tuple(int(c[i:i + 2], 16) for i in (1, 3, 5))


def _generic(album: str, family: str, dest: str) -> bool:
    from PIL import Image, ImageDraw, ImageFont
    from app.routers.retriever import GE_FAMILIES
    S = 800
    top, bot = (_hex(c) for c in _FAM_COL.get(family, ("#334155", "#0f172a")))
    im = Image.new("RGB", (S, S))
    px = ImageDraw.Draw(im)
    for y in range(S):   # vertical gradient
        t = y / (S - 1)
        px.line([(0, y), (S, y)], fill=tuple(int(top[k] + (bot[k] - top[k]) * t) for k in range(3)))
    ov = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    od = ImageDraw.Draw(ov)
    for x in range(-S, S, 56):   # faint diagonal stripes
        od.line([(x, S), (x + S, 0)], fill=(255, 255, 255, 18), width=14)
    im = Image.alpha_composite(im.convert("RGBA"), ov).convert("RGB")
    d = ImageDraw.Draw(im)
    d.rectangle([28, 28, S - 28, S - 28], outline=(255, 255, 255), width=3)

    def font(sz):
        try:
            return ImageFont.load_default(size=sz)
        except Exception:
            return ImageFont.load_default()

    small = font(30)
    d.text((S // 2, 80), "VARIOUS ARTISTS", font=small, fill=(255, 255, 255), anchor="mm")
    label = "HOLIDAY" if family == "holiday" else GE_FAMILIES.get(family, "").upper()
    if label:
        d.text((S // 2, S - 80), label, font=small, fill=(255, 255, 255), anchor="mm")
    title = re.sub(r"\s+", " ", album or "Compilation").strip()
    for sz in (96, 84, 72, 62, 54, 46, 40):
        f = font(sz)
        words, lines, cur = title.split(" "), [], ""
        for w in words:
            test = (cur + " " + w).strip()
            if d.textlength(test, font=f) <= S - 140:
                cur = test
            else:
                if cur:
                    lines.append(cur)
                cur = w
        lines.append(cur)
        if len(lines) * sz * 1.15 <= S - 300 and all(d.textlength(ln, font=f) <= S - 140 for ln in lines):
            break
    h = len(lines) * sz * 1.15
    y = (S - h) / 2 + sz * 0.55
    for ln in lines:
        d.text((S // 2 + 3, y + 3), ln, font=f, fill=(0, 0, 0), anchor="mm")
        d.text((S // 2, y), ln, font=f, fill=(255, 255, 255), anchor="mm")
        y += sz * 1.15
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    im.save(dest, "JPEG", quality=90)
    return True


# ══════════════════════════════════════════════════════════════════════════════
#  ONLINE LOOKUPS
# ══════════════════════════════════════════════════════════════════════════════

def _akey(s: str) -> str:
    """Album match key: lowercase, no brackets, 'volume/vol.' dropped, letters+digits only."""
    s = re.sub(r"[\(\[][^\)\]]*[\)\]]", " ", (s or "").lower())
    s = re.sub(r"^\s*(19|20)\d{2}\s*-\s*", "", s)
    s = re.sub(r"\b(vol(ume)?\.?|deluxe|edition|remaster(ed)?|expanded)\b", " ", s)
    s = re.sub(r"^the\s+", "", s.strip())
    return re.sub(r"[^a-z0-9]", "", s)


def _search_title(s: str) -> str:
    s = re.sub(r"[\(\[][^\)\]]*[\)\]]", " ", s or "")
    s = re.sub(r"^\s*(19|20)\d{2}\s*-\s*", "", s)
    return re.sub(r"\s+", " ", s).strip(" -_.")


class _ITunesWindow:
    """Constitution §3: iTunes fixed window, 20 requests per minute."""
    def __init__(self):
        self.start, self.n = 0.0, 0

    async def acquire(self):
        now = time.monotonic()
        if now - self.start >= 60:
            self.start, self.n = now, 0
        if self.n >= 20:
            await asyncio.sleep(60 - (now - self.start) + 0.5)
            self.start, self.n = time.monotonic(), 0
        self.n += 1


_itunes = _ITunesWindow()


async def _get_img(client, url: str) -> Optional[bytes]:
    try:
        r = await client.get(url, follow_redirects=True, timeout=30.0)
        if r.is_success and r.headers.get("content-type", "").startswith("image/"):
            return r.content
    except Exception:
        pass
    return None


async def _find_caa(client, artist: str, names: list, va: bool) -> Optional[tuple]:
    from app.routers.groomer import _artist_key
    from app.routers.retriever import mb_limiter
    want_a, want_t = _artist_key(artist), {_akey(n) for n in names if _akey(n)}
    rgs = []
    for name in names:
        t = _search_title(name)
        if not t:
            continue
        q = f'releasegroup:"{t}" AND artist:"{"Various Artists" if va else artist}"'
        await mb_limiter.acquire()
        try:
            r = await client.get("https://musicbrainz.org/ws/2/release-group/",
                                 params={"query": q, "fmt": "json", "limit": 5}, headers=_UA)
            rgs = r.json().get("release-groups", []) if r.is_success else []
        except Exception:
            rgs = []
        if any(_akey(rg.get("title", "")) in want_t for rg in rgs):
            break
    for rg in rgs:
        if int(rg.get("score", 0)) < 85 or _akey(rg.get("title", "")) not in want_t:
            continue
        names = [_artist_key((c or {}).get("name", "")) for c in rg.get("artist-credit", []) if isinstance(c, dict)]
        if va and not any(n in _VA for n in names):
            continue
        if not va and want_a not in names:
            continue
        url = f"https://coverartarchive.org/release-group/{rg['id']}/front-500"
        data = await _get_img(client, url)
        if data:
            return url, data
    return None


async def _find_itunes(client, artist: str, names: list, va: bool) -> Optional[tuple]:
    from app.routers.groomer import _artist_key
    t = _search_title(names[0])
    await _itunes.acquire()
    try:
        r = await client.get("https://itunes.apple.com/search",
                             params={"term": t if va else f"{artist} {t}", "entity": "album", "limit": 10})
        res = r.json().get("results", []) if r.is_success else []
    except Exception:
        return None
    want_a, want_t = _artist_key(artist), {_akey(n) for n in names if _akey(n)}
    for x in res:
        if _akey(x.get("collectionName", "")) not in want_t:
            continue
        ak = _artist_key(x.get("artistName", ""))
        if (va and ak not in _VA) or (not va and ak != want_a):
            continue
        url = (x.get("artworkUrl100") or "").replace("100x100bb", "600x600bb")
        data = await _get_img(client, url) if url else None
        if data:
            return url, data
    return None


async def _find_deezer(client, artist: str) -> Optional[tuple]:
    from app.routers.groomer import _artist_key
    await asyncio.sleep(0.25)
    try:
        r = await client.get("https://api.deezer.com/search/artist", params={"q": artist, "limit": 5})
        res = r.json().get("data", []) if r.is_success else []
    except Exception:
        return None
    want = _artist_key(artist)
    for x in res:
        url = x.get("picture_xl") or ""
        if _artist_key(x.get("name", "")) == want and url and "/artist//" not in url:
            data = await _get_img(client, url)
            if data:
                return url, data
    return None


# ══════════════════════════════════════════════════════════════════════════════
#  CHECK JOB
# ══════════════════════════════════════════════════════════════════════════════

async def _run_check():
    global _job
    _stop["flag"] = False
    _job = {"status": "running", "phase": "scan", "done": 0, "total": 0, "found": 0, "message": "Looking at every album folder…"}
    try:
        os.makedirs(_CACHE, exist_ok=True)
        async with aiosqlite.connect(settings.database_url, timeout=60) as db:
            await _migrate(db)
            has_tg = await (await db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='track_genres'")).fetchone()
            rows = await (await db.execute(
                "SELECT t.file_path, COALESCE(t.tag_artist, a.name, ''), t.album_title, "
                + ("g.family, CASE WHEN g.styles LIKE '%\"holiday\"%' THEN 1 ELSE 0 END " if has_tg else "NULL, 0 ")
                + "FROM tracks t LEFT JOIN artists a ON t.artist_id = a.artist_id "
                + ("LEFT JOIN track_genres g ON g.track_id = t.track_id " if has_tg else "")
                + "WHERE t.file_path IS NOT NULL AND COALESCE(t.missing, 0) = 0")).fetchall()
            items = {}
            for kind, root, status, gen, ck in await (await db.execute(
                    "SELECT kind, root, status, generic, checked_at FROM art_items")).fetchall():
                items[(kind, root)] = {"status": status, "generic": gen, "checked_at": ck}
        loop = asyncio.get_event_loop()
        plan = await loop.run_in_executor(None, _scan_sync, rows, items)
        if _stop["flag"]:
            _job.update({"status": "stopped", "phase": "", "message": "Stopped."})
            return

        from app.routers.groomer import _utc_epoch
        now = time.time()

        def recent_none(kind, root):
            it = items.get((kind, root))
            if not it:
                return False
            age = now - _utc_epoch(it["checked_at"])
            if it["generic"] and it["status"] == "written":   # generic cover saved: look for real art monthly
                return age < 30 * 86400
            return it["status"] in ("none", "skipped") and age < 60 * 86400

        todo = [("album", a) for a in plan["albums"] if not (items.get(("album", a["root"]), {}).get("status") == "found")
                and not recent_none("album", a["root"])]
        todo += [("artist", a) for a in plan["artists"] if not (items.get(("artist", a["root"]), {}).get("status") == "found")
                 and not recent_none("artist", a["root"])]
        _job.update({"phase": "lookups", "done": 0, "total": len(todo),
                     "message": f"{len(plan['albums']):,} albums and {len(plan['artists']):,} artists without art — finding covers…"})
        SQL = ("INSERT INTO art_items (kind, root, targets, artist, album, family, source, src, cache, status, checked_at) "
               "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now')) ON CONFLICT(kind, root) DO UPDATE SET "
               "targets=excluded.targets, artist=excluded.artist, album=excluded.album, family=excluded.family, "
               "source=excluded.source, src=excluded.src, cache=excluded.cache, status=excluded.status, "
               "checked_at=excluded.checked_at")
        async with httpx.AsyncClient(timeout=20.0, headers=_UA) as client, \
                aiosqlite.connect(settings.database_url, timeout=60) as db:
            for i, (kind, a) in enumerate(todo):
                if _stop["flag"]:
                    break
                cache = os.path.join(_CACHE, f"{kind}-{hashlib.md5(a['root'].encode()).hexdigest()}.jpg")
                source, src, ok = None, None, False
                if kind == "album":
                    if a["local"]:
                        try:
                            with open(a["local"], "rb") as f:
                                ok = await loop.run_in_executor(None, _prepare, f.read(), cache)
                        except OSError:
                            ok = False
                        source, src = "local", a["local"]
                    if not ok:
                        for name, fn in (("caa", _find_caa), ("itunes", _find_itunes)):
                            hit = await fn(client, a["artist"], a["names"], a["va"])
                            if hit and await loop.run_in_executor(None, _prepare, hit[1], cache):
                                source, src, ok = name, hit[0], True
                                break
                    if not ok and a["va"] and not a["generic_written"]:
                        ok = await loop.run_in_executor(None, _generic, a["album"], a["family"], cache)
                        source, src = "generic", ""
                    status = "found" if ok else "none"
                    st_written = a["generic_written"] and not ok
                    await db.execute(SQL, ("album", a["root"], json.dumps(a["targets"]), a["artist"], a["album"],
                                           a["family"], source if ok else None, src if ok else None, cache if ok else None,
                                           "written" if st_written else status))
                    _job["message"] = f"{a['artist']} — {a['album']}: " + (
                        {"local": "found in the folder", "caa": "Cover Art Archive", "itunes": "iTunes",
                         "generic": "generic cover"}.get(source, "") if ok else "nothing found")
                else:
                    hit = await _find_deezer(client, a["artist"])
                    if hit:
                        ok = await loop.run_in_executor(None, _prepare, hit[1], cache)
                    await db.execute(SQL, ("artist", a["root"], json.dumps([a["root"]]), a["artist"], "", "",
                                           "deezer" if ok else None, hit[0] if ok else None, cache if ok else None,
                                           "found" if ok else "none"))
                    _job["message"] = f"{a['artist']}: " + ("artist photo found" if ok else "no photo")
                _job["found"] += ok
                _job["done"] = i + 1
                await db.commit()
            # forget items whose folders now have art (written outside ChartHound, or gone)
            live = {("album", a["root"]) for a in plan["albums"]} | {("artist", a["root"]) for a in plan["artists"]}
            for (kind, root), it in items.items():
                if it["status"] == "found" and (kind, root) not in live:
                    await db.execute("DELETE FROM art_items WHERE kind=? AND root=?", (kind, root))
            await db.commit()
            cnt = dict(await (await db.execute(
                "SELECT status, COUNT(*) FROM art_items GROUP BY status")).fetchall())
        stopped = _stop["flag"]
        _job.update({"status": "stopped" if stopped else "done", "phase": "",
                     "message": (f"{'Stopped' if stopped else 'Done'} — {cnt.get('found', 0):,} covers ready to save · "
                                 f"{cnt.get('none', 0):,} nothing found · {cnt.get('written', 0):,} saved before")})
        log.info(f"Cover art check: {_job['message']}")
    except Exception as e:
        log.exception("Cover art check failed")
        _job.update({"status": "failed", "phase": "", "message": f"Error: {e}"})


# ══════════════════════════════════════════════════════════════════════════════
#  ENDPOINTS
# ══════════════════════════════════════════════════════════════════════════════

@router.post("/check")
async def art_check(user: dict = Depends(require_auth)):
    t = _task["task"]
    if t is not None and not t.done():
        raise HTTPException(409, "Already checking.")
    _task["task"] = asyncio.create_task(_run_check())
    return {"ok": True}


@router.post("/stop")
async def art_stop(user: dict = Depends(require_auth)):
    _stop["flag"] = True
    return {"ok": True}


@router.get("/status")
async def art_status(user: dict = Depends(require_auth)):
    return _job


@router.get("/list")
async def art_list(page: int = 1, size: int = 60, kind: str = "", user: dict = Depends(require_auth)):
    import jwt
    size = max(12, min(size, 200))
    page = max(1, page)
    async with aiosqlite.connect(settings.database_url) as db:
        await _migrate(db)
        where, args = "status='found'", []
        if kind in ("album", "artist"):
            where += " AND kind=?"
            args.append(kind)
        elif kind == "generic":
            where += " AND source='generic'"
        total = (await (await db.execute(f"SELECT COUNT(*) FROM art_items WHERE {where}", args)).fetchone())[0]
        rows = await (await db.execute(
            f"SELECT id, kind, root, artist, album, source, targets FROM art_items WHERE {where} "
            "ORDER BY kind, artist COLLATE NOCASE, album COLLATE NOCASE LIMIT ? OFFSET ?",
            args + [size, (page - 1) * size])).fetchall()
        counts = dict(await (await db.execute("SELECT status, COUNT(*) FROM art_items GROUP BY status")).fetchall())
    t = jwt.encode({"art": 1, "exp": int(time.time()) + 1800}, _IMG_KEY, algorithm="HS256")
    return {"total": total, "page": page, "size": size, "t": t, "counts": counts,
            "rows": [{"id": i, "kind": k, "folder": os.path.basename(r) or r, "artist": a, "album": al,
                      "source": s, "discs": len(json.loads(tg or "[]"))} for i, k, r, a, al, s, tg in rows]}


@router.get("/img/{item_id}")
async def art_img(item_id: int, t: str):
    import jwt
    from jwt import PyJWTError
    try:
        jwt.decode(t, _IMG_KEY, algorithms=["HS256"])
    except PyJWTError:
        raise HTTPException(401, "Preview expired — reload the list.")
    async with aiosqlite.connect(settings.database_url) as db:
        row = await (await db.execute("SELECT cache FROM art_items WHERE id=?", (item_id,))).fetchone()
    p = row[0] if row else None
    if not p or not os.path.realpath(p).startswith(os.path.realpath(_CACHE) + os.sep) or not os.path.isfile(p):
        raise HTTPException(404, "No preview.")
    return FileResponse(p, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=600"})


class ArtSave(BaseModel):
    ids: List[int] = []
    all: bool = False
    kind: str = ""


def _save_sync(rows: list) -> tuple:
    """rows [(id, kind, targets, cache, generic_before)] → (saved ids, skipped ids, failed)."""
    import shutil
    saved, skipped, failed = [], [], 0
    for iid, kind, targets, cache, gen_before in rows:
        name = "artist.jpg" if kind == "artist" else "cover.jpg"
        wrote = False
        try:
            for d in json.loads(targets or "[]"):
                dest = os.path.join(d, name)
                if os.path.exists(dest) and not gen_before:
                    continue   # never overwrite art that's already there
                tmp = dest + ".charthound.tmp"
                shutil.copyfile(cache, tmp)
                os.replace(tmp, dest)
                wrote = True
            (saved if wrote else skipped).append(iid)
        except Exception as e:
            log.warning(f"Cover art save failed for {targets}: {e}")
            failed += 1
    return saved, skipped, failed


@router.post("/save")
async def art_save(req: ArtSave, user: dict = Depends(require_auth)):
    t = _task["task"]
    if t is not None and not t.done():
        raise HTTPException(409, "Wait for CHECK to finish.")
    async with aiosqlite.connect(settings.database_url, timeout=60) as db:
        await _migrate(db)
        if req.all:
            q, args = "SELECT id, kind, targets, cache, generic, source FROM art_items WHERE status='found'", []
            if req.kind in ("album", "artist"):
                q += " AND kind=?"
                args.append(req.kind)
            elif req.kind == "generic":
                q += " AND source='generic'"
        else:
            ids = list(req.ids)[:5000]
            if not ids:
                raise HTTPException(400, "Nothing selected.")
            q = f"SELECT id, kind, targets, cache, generic, source FROM art_items WHERE status='found' AND id IN ({','.join('?' * len(ids))})"
            args = ids
        rows = await (await db.execute(q, args)).fetchall()
        # generic=1: the folder holds a generic cover ChartHound saved — a real cover may replace it
        work = [(i, k, tg, c, g) for i, k, tg, c, g, _src in rows if c and os.path.isfile(c)]
        saved, skipped, failed = await asyncio.get_event_loop().run_in_executor(None, _save_sync, work)
        gen = {r[0] for r in rows if r[5] == "generic"}
        for iid in saved:
            await db.execute("UPDATE art_items SET status='written', generic=?, checked_at=datetime('now') WHERE id=?",
                             (1 if iid in gen else 0, iid))
        for iid in skipped:
            await db.execute("UPDATE art_items SET status='skipped' WHERE id=?", (iid,))
        await db.commit()
    for _i, _k, _t, c, _g in work:
        try:
            os.remove(c)
        except OSError:
            pass
    if saved:
        from app.routers.retriever import _refresh_media_servers
        asyncio.create_task(_refresh_media_servers())
    msg = f"{len(saved):,} covers saved" + (f" · {len(skipped):,} skipped (art already there)" if skipped else "") \
          + (f" · {failed:,} failed" if failed else "") + (" — media servers refreshing" if saved else "")
    log.info(f"Cover art: {msg}")
    return {"ok": True, "saved": len(saved), "skipped": len(skipped), "failed": failed, "message": msg}


class ArtDrop(BaseModel):
    ids: List[int]


@router.post("/drop")
async def art_drop(req: ArtDrop, user: dict = Depends(require_auth)):
    """✕ Don't use these — never offered again until you clear them (re-checked after 60 days)."""
    ids = list(req.ids)[:5000]
    if not ids:
        raise HTTPException(400, "Nothing selected.")
    async with aiosqlite.connect(settings.database_url) as db:
        rows = await (await db.execute(
            f"SELECT cache FROM art_items WHERE id IN ({','.join('?' * len(ids))})", ids)).fetchall()
        await db.execute(f"UPDATE art_items SET status='skipped', cache=NULL, checked_at=datetime('now') "
                         f"WHERE id IN ({','.join('?' * len(ids))})", ids)
        await db.commit()
    for (c,) in rows:
        if c:
            try:
                os.remove(c)
            except OSError:
                pass
    return {"ok": True, "dropped": len(ids)}
