# © 2026 Colby R. Curtis | ChartHound: The New World
# All Rights Reserved.
"""
ChartHound — Hit Score + Last.fm pass (split from groomer.py in M9b)
Routes keep their /api/groomer/... paths (included by groomer.router).
"""

import asyncio
import json
import logging
import os
import re
import sqlite3
import time

import aiosqlite
import httpx

from datetime import datetime
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException

from app.config import get_settings
from app.deps import require_auth
from app.security import decrypt_token

from app.routers.groomer_common import (
    _DYNAMIC_DB,
    _EXCLUDED_SOURCES,
    _FEAT_RE,
    _STATIC_DB,
    _SUFFIX_RE,
    _artist_key,
    _migrate_chart_data,
    _scan_job,
    _title_key,
    _utc_epoch,
)

log      = logging.getLogger("charthound.groomer")
router   = APIRouter()   # tags + /api/groomer prefix come from groomer.router
settings = get_settings()

# ══════════════════════════════════════════════════════════════════════════════
#  HIT SCORE (M2) — one 0–100 number per charted track
# ══════════════════════════════════════════════════════════════════════════════
#  Tier A = Billboard Hot 100 (reliable weekly-derived data + Whitburn year-end ranks)
#  Tier B = trusted genre charts (country, R&B, rock, AC, dance, UK…)
#  Tier C = sources with unreliable peaks (many rows are #1 by default)
#  Score built mostly from Tier A so "everything is #1" sources can't dominate.

_TIER_A_SOURCES = {"hot100_csv", "utdata_hot100"}
_TIER_C_SOURCES = {
    "adult_charts_1961_2018", "whitburn_pop_1890_2023", "cw_annual_1944_2004",
    "chart2000_monthly", "tsort_historical", "kworb_itunes_us", "listenbrainz_historical",
}
_SKIP_CHARTS = {"The Billboard 200", "A40 Working", "AC Working", "Chart Runs"}
_hit_score_job = {"status": "idle", "message": "", "count": 0}

def _peak_factor(peak) -> float:
    try:
        p = max(1, int(peak))
    except Exception:
        return 0.0
    return max(0.0, 1 - (p - 1) / 100) ** 1.6


_YEAREND_SOURCES = {"ccm_weekly", "country_yearend", "rnb_yearend", "rock_yearend",
                    "dance_yearend", "adultpop_yearend"}


def _source_tier(data_source: str, chart_name: str) -> int:
    if data_source in _EXCLUDED_SOURCES or chart_name in _SKIP_CHARTS:
        return 0
    if data_source in _TIER_A_SOURCES or chart_name == "The Billboard Hot 100":
        return 1
    if data_source in _TIER_C_SOURCES or chart_name == "Bubbling Under Hot 100":
        return 3
    return 2


def _score_track(rows: list, cd_peak, cd_conf: str) -> tuple:
    """rows: [(tier, chart_name, peak, weeks, year, yrank)] → (score, year, h100_peak, h100_wks, yrank, n_charts)."""
    t1 = [r for r in rows if r[0] == 1]
    a  = [r for r in t1 if r[2]]
    b  = [r for r in rows if r[0] == 2 and r[2]]
    c  = [r for r in rows if r[0] == 3 and r[2]]
    h_peak = h_wks = yrank = year = None
    ranks = [r[5] for r in t1 if r[5]]
    yrank = min(ranks) if ranks else None
    ye_pts = 15 * (101 - yrank) / 100 if yrank and yrank <= 100 else 0
    if t1:
        # Earliest Hot 100 year = the song's original era (re-releases chart later)
        yrs  = [r[4] for r in t1 if r[4]]
        year = min(yrs) if yrs else None
        h_wks = max((r[3] or 0) for r in t1) or None
    if a:
        h_peak = min(r[2] for r in a)
        score  = 60 * _peak_factor(h_peak) + 15 * min(h_wks or 0, 30) / 30 + ye_pts
    elif yrank:
        # Pre-1958 / year-end-only songs: no reliable peak, rank carries it
        score  = 35 + 15 * min(h_wks or 0, 30) / 30 + ye_pts
    elif b:
        best  = min(b, key=lambda r: r[2])
        year  = year or best[4]
        score = 45 * _peak_factor(best[2]) + 10 * min(max((r[3] or 0) for r in b), 30) / 30
    elif c:
        best  = min(c, key=lambda r: r[2])
        year  = year or best[4]
        score = 25 * _peak_factor(best[2])
    else:
        score = (15 if cd_conf == "low" else 25) * _peak_factor(cd_peak or 100)
    if not year:
        year = next((r[4] for r in b + c if r[4]), None)
    strong_genre = {r[1] for r in b if r[2] <= 20}
    score += min(10, 3 * len(strong_genre))
    if cd_conf == "low":
        score = min(score, 25)
    n_charts = len({r[1] for r in rows if r[0]})
    return int(round(max(0, min(100, score)))), year, h_peak, h_wks, yrank, n_charts


# ── Last.fm tag → genre tree key (exact tag match; subjective tags ignored) ──
_LFM_TAG_MAP = {
    "rock": "rock", "classic rock": "classicrock", "hard rock": "hardrock", "soft rock": "softrock",
    "arena rock": "arenarock", "indie rock": "indierock", "folk rock": "folkrock",
    "southern rock": "southernrock", "punk": "punkrock", "punk rock": "punkrock",
    "new wave": "newwave", "post-punk": "punkrock", "alternative": "alternative",
    "alternative rock": "alternative", "grunge": "alternative", "britpop": "alternative",
    "blues rock": "classicrock", "heartland rock": "classicrock", "progressive rock": "classicrock",
    "psychedelic rock": "classicrock", "pop rock": "rock", "country rock": "country",
    "pop": "pop", "dance pop": "dancepop", "dance-pop": "dancepop", "synthpop": "synthpop",
    "synth pop": "synthpop", "synth-pop": "synthpop", "electropop": "synthpop",
    "teen pop": "teenpop", "power pop": "powerpop", "adult contemporary": "adultpop",
    "easy listening": "adultpop", "indie pop": "indipop",
    "country": "country", "classic country": "tradcountry", "traditional country": "tradcountry",
    "country pop": "countrypop", "outlaw country": "outlawcountry", "americana": "americana",
    "bluegrass": "americana", "red dirt": "texascountry", "texas country": "texascountry",
    "bro-country": "brocountry",
    "rnb": "rnb", "r&b": "rnb", "rhythm and blues": "rnb", "contemporary r&b": "rnb",
    "soul": "soul", "neo-soul": "neosoul", "neo soul": "neosoul", "funk": "funk",
    "motown": "motown", "quiet storm": "quietstorm", "new jack swing": "newjack",
    "hip-hop": "hiphop", "hip hop": "hiphop", "hiphop": "hiphop", "rap": "hiphop",
    "trap": "trap", "gangsta rap": "gangstarap", "boom bap": "boombap",
    "metal": "metal", "heavy metal": "heavymetal", "thrash metal": "thrashmetal",
    "death metal": "deathmetal", "doom metal": "doommetal", "stoner rock": "doommetal",
    "glam metal": "glaemmetal", "hair metal": "glaemmetal", "glam rock": "glaemmetal",
    "dance": "dance", "electronic": "dance", "electronica": "dance", "edm": "edm",
    "house": "house", "techno": "techno", "trance": "trance", "disco": "disco", "eurodance": "eurodance",
    "folk": "folk", "indie folk": "indifolk", "singer-songwriter": "singersong",
    "jazz": "jazz", "smooth jazz": "smoothjazz", "vocal jazz": "vocaljazz", "bebop": "bebop",
    "jazz fusion": "jazzfusion", "fusion": "jazzfusion",
    "blues": "blues", "chicago blues": "chicagoblues", "delta blues": "deltablues", "texas blues": "texasblues",
    "classical": "classical", "opera": "opera", "baroque": "baroque", "orchestral": "orchestral",
    "christian": "ccm", "ccm": "ccm", "contemporary christian": "ccm", "christian rock": "ccm-rock",
    "christian pop": "ccm-pop", "worship": "worship", "praise & worship": "worship",
    "gospel": "gospel", "southern gospel": "sgospel",
}
_CLASSIC_MIN_LISTENERS = 300_000
# 🌙 Mellow / Easy Listening vibe — any family with one of these styles
_MELLOW_STYLES = {"softrock", "adultpop", "easylistening", "smoothjazz", "quietstorm", "singersong"}
_NOT_CLASSIC_RE = re.compile(r"\b(live|demo|remix|karaoke|instrumental|acoustic|rehearsal|session|edit)\b", re.I)


def _genres_from_tags(tags_json: Optional[str]) -> list:
    try:
        tags = json.loads(tags_json or "[]")
    except Exception:
        return []
    from app.routers.retriever import GE_TAGS   # M10c: the Genre Engine's wider tag list as a fallback
    out = []
    for t in tags:
        t = str(t).strip().lower()
        k = _LFM_TAG_MAP.get(t) or GE_TAGS.get(t)
        if k == "_folkcountry":
            k = "folk"
        from app.routers.retriever import GE_FOLD
        k = GE_FOLD.get(k, k) if k else k
        if k and k not in out:
            out.append(k)
        if len(out) == 3:
            break
    return out


def _popularity_score(listeners: Optional[int]) -> float:
    """Score from Last.fm listeners alone (capped at 85 so real chart smashes stay on top)."""
    import math
    if not listeners or listeners < 40_000:
        return 0.0
    return min(85.0, 30 * math.log10(listeners / 40_000) + 25)


def _staying_boost(listeners: Optional[int]) -> float:
    import math
    if not listeners or listeners < 100_000:
        return 0.0
    return min(15.0, 7.5 * math.log10(listeners / 100_000))


_CHART_INDEX = {"stamp": None, "data": None}


def _chart_index_sync(force: bool = False) -> tuple:
    """
    Every chart song in one dict: (artist_key, title_key) → [(tier, chart, peak, weeks, year, yrank)].
    Also top40 (artist_key → set of Top-40 Hot 100 titles) and display names.
    Cached in memory; rebuilt when chart data changes (or force=True).
    """
    stamp = None
    try:
        dconn0 = sqlite3.connect(_DYNAMIC_DB, check_same_thread=False)
        try:
            stamp = dconn0.execute(
                "SELECT COUNT(*), MAX(added_at) FROM chart_reference_extras").fetchone()
        except Exception:
            stamp = (0, None)
        finally:
            dconn0.close()
        stamp = (stamp, os.path.getmtime(_STATIC_DB) if os.path.exists(_STATIC_DB) else 0)
    except Exception:
        pass
    if not force and _CHART_INDEX["data"] is not None and _CHART_INDEX["stamp"] == stamp:
        return _CHART_INDEX["data"]

    index: dict = {}
    top40: dict = {}
    names: dict = {}

    def _add(artist, title, tier, chart, peak, weeks, year, yrank=None):
        if not tier or not artist or not title:
            return
        ak, tk = _artist_key(artist), _title_key(title)
        key = (ak, tk)
        index.setdefault(key, []).append((tier, chart, peak, weeks, year, yrank))
        if key not in names or tier == 1:
            names[key] = (artist, title)
        if tier == 1 and peak and peak <= 40:
            top40.setdefault(ak, set()).add(tk)

    if os.path.exists(_STATIC_DB):
        sconn = sqlite3.connect(_STATIC_DB, check_same_thread=False)
        try:
            for artist, title, chart, peak, wks, yr, src in sconn.execute(
                "SELECT artist, title, chart_name, peak_position, weeks_on_chart, chart_year, data_source "
                "FROM chart_reference"
            ):
                _add(artist, title, _source_tier(src or "", chart or ""), chart, peak, wks, yr)
            try:
                for artist, title, peak, wks, yr, yrank in sconn.execute(
                    "SELECT artist, title, peak_position, chart_weeks, year, yearly_rank FROM billboard_pop"
                ):
                    # billboard_pop.peak_position is unreliable (not a Hot 100 peak) —
                    # use it only for year-end rank, weeks and year.
                    _add(artist, title, 1, "billboard_pop", None, wks, yr, yrank)
            except Exception:
                pass
        finally:
            sconn.close()

    dconn = sqlite3.connect(_DYNAMIC_DB, check_same_thread=False)
    try:
        try:
            for artist, title, chart, peak, wks, yr, src in dconn.execute(
                "SELECT artist, title, chart_name, peak_position, weeks_on_chart, chart_year, data_source "
                "FROM chart_reference_extras"
            ):
                # Billboard year-end imports stored the YEAR-END RANK as "peak".
                # A year-end #r song typically peaked around #r/2 on the weekly chart.
                if src in _YEAREND_SOURCES and peak:
                    peak = max(1, (int(peak) + 1) // 2)
                _add(artist, title, _source_tier(src or "", chart or ""), chart, peak, wks, yr)
        except Exception:
            pass
    finally:
        dconn.close()
    _CHART_INDEX.update(stamp=stamp, data=(index, top40, names))
    return index, top40, names


# Chart name → family, for songs you don't own (no Genre Engine data for those)
_CHART_FAMILY_HINTS = (
    ("christian", "ccm"), ("ccm", "ccm"), ("gospel", "gospel"), ("country", "country"),
    ("c&w", "country"), ("r&b", "rnb"), ("rnb", "rnb"), ("soul", "rnb"), ("rap", "hiphop"),
    ("hip", "hiphop"), ("rock", "rock"), ("alternative", "rock"), ("dance", "dance"),
    ("club", "dance"), ("electronic", "dance"), ("adult", "pop"), ("pop", "pop"),
)


def _chart_families(rows: list) -> set:
    fams = set()
    for r in rows:
        c = str(r[1] or "").lower()
        if r[0] == 1:          # Hot 100 says nothing about genre
            continue
        for hint, fam in _CHART_FAMILY_HINTS:
            if hint in c:
                fams.add(fam)
                break
    return fams


def _big_hits_sync(decades: list, genres: list, min_score: int, ohw_only: bool,
                   crossovers: bool, limit: int) -> dict:
    """
    Chart songs you DON'T own, ranked by the same Hit Score Top Dogs uses.
    Genre for unowned songs: Last.fm tags (if cached) + which genre charts the song was on.
    """
    from app.routers import retriever as _ret
    index, top40, names = _chart_index_sync()
    dconn = sqlite3.connect(_DYNAMIC_DB, check_same_thread=False)
    try:
        owned = set()
        for ar, ti in dconn.execute(
                "SELECT COALESCE(tag_artist, ''), title FROM tracks WHERE COALESCE(missing, 0) = 0"):
            owned.add((_artist_key(ar), _title_key(ti)))
        try:
            for ar, ti in dconn.execute("SELECT artist_norm, title_norm FROM library_cache"):
                owned.add((_artist_key(ar or ""), _title_key(ti or "")))
        except Exception:
            pass
        lfm = {}
        try:
            for ak_, tk_, lis, tags in dconn.execute(
                    "SELECT artist_key, title_key, listeners, tags FROM lastfm_tracks WHERE status='ok'"):
                lfm[(ak_, tk_)] = (lis or 0, tags)
        except Exception:
            pass
        looked = set()
        try:
            looked = {(a, t) for a, t in dconn.execute("SELECT artist_key, title_key FROM lastfm_tracks")}
        except Exception:
            pass
    finally:
        dconn.close()

    ranges = [(int(d), int(d) + 9) for d in decades or []]
    sel = set(genres or [])
    sel_fams = {g for g in sel if g in _ret.GE_FAMILIES}
    for g in sel:   # a style picked → its family counts for unowned songs
        if g in _ret.GE_STYLES:
            sel_fams |= set(_ret.GE_STYLES[g][1][:1 if not crossovers else 2])
    if "mellow" in sel:
        sel_fams |= {"pop", "jazz"}

    out = []
    for key, rows in index.items():
        if key in owned or not any(r[0] == 1 and r[2] for r in rows) and not any(r[0] == 2 for r in rows):
            continue
        score, year, h_peak, h_wks, yrank, n = _score_track(rows, None, "high")
        lis, ttags = lfm.get(key, (0, None))
        if lis:
            room = max(0.0, 100 - score) / 50
            score = int(round(min(100, max(score + _staying_boost(lis) * min(1.0, room),
                                           _popularity_score(lis)))))
        if score < (min_score or 0):
            continue
        if ranges and not (year and any(lo <= int(year) <= hi for lo, hi in ranges)):
            continue
        ak, tk = key
        ohw = ak in top40 and len(top40[ak]) == 1 and tk in top40[ak]
        if ohw_only and not ohw:
            continue
        fams = _chart_families(rows)
        tag_keys = _genres_from_tags(ttags)
        for k in tag_keys:
            fams |= set(_ret.GE_STYLES[k][1]) if k in _ret.GE_STYLES else ({k} if k in _ret.GE_FAMILIES else set())
        if sel and not (fams & sel_fams or set(tag_keys) & sel):
            continue
        artist, title = names.get(key, key)
        out.append({"artist": artist, "title": title, "year": year, "hit_score": score,
                    "hot100_peak": h_peak, "hot100_weeks": h_wks, "yearend_rank": yrank,
                    "one_hit_wonder": 1 if ohw else 0, "listeners": lis or None,
                    "families": sorted(fams), "needs_lookup": key not in looked})
    out.sort(key=lambda r: -r["hit_score"])
    need = [(r["artist"], r["title"]) for r in out[:300] if r["needs_lookup"]]
    return {"total": len(out), "results": out[:max(0, limit)], "pending_lookups": need}


def _rebuild_hit_scores_sync() -> int:
    """Load chart sources into memory once, score every charted track, rewrite hit_scores."""
    t0 = time.time()
    index, top40, _names = _chart_index_sync(force=True)

    dconn = sqlite3.connect(_DYNAMIC_DB, check_same_thread=False)
    try:
        # Last.fm data (staying power + genres), keyed like the chart index
        lfm, lfm_art = {}, {}
        try:
            for ak_, tk_, lis, tags in dconn.execute(
                    "SELECT artist_key, title_key, listeners, tags FROM lastfm_tracks WHERE status='ok'"):
                lfm[(ak_, tk_)] = (lis or 0, tags)
            for ak_, tags in dconn.execute("SELECT artist_key, tags FROM lastfm_artists WHERE status='ok'"):
                lfm_art[ak_] = tags
        except Exception:
            pass

        # Earliest known year per song (compilations carry later album years)
        all_tracks = dconn.execute("""
            SELECT t.track_id, COALESCE(t.tag_artist, a.name, ''), t.title, t.year
            FROM tracks t LEFT JOIN artists a ON t.artist_id = a.artist_id
        """).fetchall()
        song_year: dict = {}
        for _tid, ar, ti, yr in all_tracks:
            if yr and 1900 < int(yr) <= 2100:
                k = (_artist_key(ar), _title_key(ti))
                song_year[k] = min(song_year.get(k, 9999), int(yr))

        # Never-charted classics: rebuild their placeholder chart_data rows
        dconn.execute("BEGIN")
        dconn.execute("DELETE FROM chart_data WHERE chart_name='classic'")
        charted = {r[0] for r in dconn.execute("SELECT DISTINCT track_id FROM chart_data")}
        classics = []
        for tid, ar, ti, _yr in all_tracks:
            if tid in charted or _NOT_CLASSIC_RE.search(ti or ""):
                continue
            hit = lfm.get((_artist_key(ar), _title_key(ti)))
            if hit and hit[0] >= _CLASSIC_MIN_LISTENERS:
                classics.append((tid,))
        dconn.executemany(
            "INSERT INTO chart_data (track_id, chart_name, peak_position, weeks_on_chart, star_rating, "
            "confidence, listener_count, comment_string, fetched_at) "
            "VALUES (?, 'classic', NULL, NULL, 3, 'medium', 0, '', datetime('now'))", classics)
        dconn.commit()

        tracks = dconn.execute("""
            SELECT cd.track_id, COALESCE(t.tag_artist, a.name, ''), t.title,
                   MIN(cd.peak_position), MAX(cd.confidence = 'low'), MAX(cd.chart_year), t.year,
                   MAX(cd.chart_name = 'classic')
            FROM chart_data cd
            JOIN tracks t ON cd.track_id = t.track_id
            LEFT JOIN artists a ON t.artist_id = a.artist_id
            GROUP BY cd.track_id
        """).fetchall()

        out = []
        for tid, artist, title, cd_peak, is_low, cd_year, t_year, is_classic in tracks:
            ak, tk = _artist_key(artist), _title_key(title)
            lis, ttags = lfm.get((ak, tk), (0, None))
            genres = _genres_from_tags(ttags) or _genres_from_tags(lfm_art.get(ak))
            if is_classic:
                score, year, h_peak, h_wks, yrank, n, ohw = (
                    int(round(_popularity_score(lis))), None, None, None, None, 0, 0)
            else:
                rows = index.get((ak, tk), [])
                score, year, h_peak, h_wks, yrank, n = _score_track(rows, cd_peak, "low" if is_low else "high")
                if lis:
                    # Staying power: the boost shrinks as the chart score nears 100 (keeps 100 rare),
                    # plus a floor for heavily played songs
                    room = max(0.0, 100 - score) / 50
                    score = int(round(min(100, max(score + _staying_boost(lis) * min(1.0, room),
                                                   _popularity_score(lis)))))
                ohw = 1 if (ak in top40 and len(top40[ak]) == 1 and tk in top40[ak]) else 0
            yr = year or (song_year.get((ak, tk)) if is_classic else None) or cd_year or t_year
            out.append((tid, score, yr, h_peak, h_wks, yrank, ohw, n, lis or None,
                        genres[0] if genres else None, ",".join(genres) or None, 1 if is_classic else 0,
                        f"{ak}|{tk}"))

        dconn.execute("BEGIN")
        dconn.execute("DELETE FROM hit_scores")
        dconn.executemany(
            "INSERT INTO hit_scores (track_id, hit_score, hit_year, hot100_peak, hot100_weeks, "
            "yearend_rank, one_hit_wonder, charts_count, listeners, main_genre, genre_keys, "
            "never_charted, song_key, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))", out)
        dconn.commit()
    finally:
        dconn.close()
    log.info(f"Hit Scores rebuilt: {len(out):,} tracks in {time.time() - t0:.1f}s "
             f"({len(index):,} chart songs indexed)")
    return len(out)


async def _rebuild_hit_scores() -> int:
    _hit_score_job.update({"status": "running", "message": "Calculating Hit Scores…"})
    try:
        n = await asyncio.get_event_loop().run_in_executor(None, _rebuild_hit_scores_sync)
        _hit_score_job.update({"status": "done", "message": f"{n:,} tracks scored", "count": n})
        return n
    except Exception as e:
        log.exception("Hit Score rebuild failed")
        _hit_score_job.update({"status": "failed", "message": str(e)})
        return 0


async def _hit_score_startup():
    """On boot: run migrations, build scores if chart data exists but scores don't."""
    await _migrate_chart_data()
    try:
        async with aiosqlite.connect(_DYNAMIC_DB) as db:
            hs = (await (await db.execute("SELECT COUNT(*) FROM hit_scores")).fetchone())[0]
            cd = (await (await db.execute("SELECT COUNT(*) FROM chart_data")).fetchone())[0]
            old = (await (await db.execute(
                "SELECT COUNT(*) FROM hit_scores WHERE song_key IS NULL")).fetchone())[0]
        if cd and (not hs or old):
            await _rebuild_hit_scores()
    except Exception as e:
        log.warning(f"Hit Score startup check failed: {e}")


# ══════════════════════════════════════════════════════════════════════════════
#  LAST.FM PASS (M4) — listener counts (staying power) + genre tags, whole library
# ══════════════════════════════════════════════════════════════════════════════
#  Cache-first: each song is looked up once, refreshed after 180 days (not-found
#  after 60). Fixed pacing of 4 requests/second. Resumable — a restart simply
#  continues with the songs not yet cached.

_LFM_URL       = "https://ws.audioscrobbler.com/2.0/"
_LFM_INTERVAL  = 0.25          # seconds between requests (4/s)
_LFM_OK_DAYS   = 180
_LFM_MISS_DAYS = 60
_lfm_job = {"status": "idle", "phase": "", "done": 0, "total": 0, "found": 0,
            "notfound": 0, "errors": 0, "message": "", "started_at": None}
_lfm_stop = {"flag": False}
_LFM_VERSION_RE = re.compile(
    r"\s*[\(\[][^\)\]]*\b(remaster(ed)?|version|mix|edit|mono|stereo|single|radio|live|demo|bonus|deluxe|feat\.?|featuring)\b[^\)\]]*[\)\]]", re.I)


def _lfm_clean_title(title: str) -> str:
    t = _SUFFIX_RE.sub("", title or "")
    return _LFM_VERSION_RE.sub("", t).strip() or (title or "")


async def _get_lastfm_key() -> str:
    try:
        async with aiosqlite.connect(_DYNAMIC_DB) as db:
            row = await (await db.execute(
                "SELECT token_enc FROM connections WHERE service='lastfm'")).fetchone()
        return (decrypt_token(row[0]) or "") if row and row[0] else ""
    except Exception:
        return ""


def _lfm_tag_names(container) -> list:
    tags = (container or {}).get("tag", [])
    if isinstance(tags, dict):
        tags = [tags]
    return [str(t.get("name", "")).strip() for t in tags if t.get("name")]


async def _lfm_call(client, params: dict) -> tuple:
    """Returns (kind, json) — kind: ok | notfound | ratelimit | fatal | error."""
    try:
        r = await client.get(_LFM_URL, params={**params, "format": "json"})
        j = r.json()
    except Exception:
        return "error", None
    if isinstance(j, dict) and "error" in j:
        code = j.get("error")
        if code == 6:
            return "notfound", j
        if code == 29:
            return "ratelimit", j
        if code in (10, 26):
            return "fatal", j
        return "error", j
    return ("ok", j) if r.status_code == 200 else ("error", j)


async def _run_lastfm_job():
    global _lfm_job
    _lfm_stop["flag"] = False
    _lfm_job = {"status": "running", "phase": "tracks", "done": 0, "total": 0, "found": 0,
                "notfound": 0, "errors": 0, "message": "Preparing…",
                "started_at": datetime.utcnow().isoformat()}
    key = await _get_lastfm_key()
    if not key:
        _lfm_job.update({"status": "failed", "message": "Last.fm is not connected in The Kennel."})
        return
    await _migrate_chart_data()
    now = time.time()

    async with aiosqlite.connect(_DYNAMIC_DB, timeout=30) as db:
        cache = {(a, t): (st, _utc_epoch(f)) for a, t, st, f in await (await db.execute(
            "SELECT artist_key, title_key, status, fetched_at FROM lastfm_tracks")).fetchall()}
        rows = await (await db.execute("""
            SELECT COALESCE(t.tag_artist, a.name, ''), t.title
            FROM tracks t LEFT JOIN artists a ON t.artist_id = a.artist_id
        """)).fetchall()

    work, seen = [], set()
    for artist, title in rows:
        ak, tk = _artist_key(artist), _title_key(title)
        if not ak or not tk or (ak, tk) in seen:
            continue
        seen.add((ak, tk))
        st, at = cache.get((ak, tk), (None, 0))
        if st == "ok" and now - at < _LFM_OK_DAYS * 86400:
            continue
        if st == "notfound" and now - at < _LFM_MISS_DAYS * 86400:
            continue
        work.append((ak, tk, artist, title))
    _lfm_job["total"] = len(work)
    log.info(f"Last.fm pass: {len(work):,} songs to look up ({len(seen):,} unique in library)")

    async def _flush(db, buf, sql):
        if buf:
            await db.executemany(sql, buf)
            await db.commit()
            buf.clear()

    T_SQL = ("INSERT INTO lastfm_tracks (artist_key, title_key, status, listeners, playcount, tags, fetched_at) "
             "VALUES (?, ?, ?, ?, ?, ?, datetime('now')) "
             "ON CONFLICT(artist_key, title_key) DO UPDATE SET status=excluded.status, "
             "listeners=excluded.listeners, playcount=excluded.playcount, tags=excluded.tags, "
             "fetched_at=excluded.fetched_at")
    consecutive_err = 0
    try:
        async with httpx.AsyncClient(timeout=20.0) as client, \
                   aiosqlite.connect(_DYNAMIC_DB, timeout=30) as db:
            buf = []
            for i, (ak, tk, artist, title) in enumerate(work):
                if _lfm_stop["flag"]:
                    break
                t0 = time.monotonic()
                for attempt in range(3):
                    kind, j = await _lfm_call(client, {
                        "method": "track.getInfo", "api_key": key, "autocorrect": 1,
                        "artist": _FEAT_RE.sub("", artist), "track": _lfm_clean_title(title)})
                    if kind != "ratelimit":
                        break
                    _lfm_job["message"] = "Last.fm rate limit — pausing 60s…"
                    await asyncio.sleep(60)
                if kind == "fatal":
                    _lfm_job.update({"status": "failed",
                                     "message": "Last.fm rejected the API key — check The Kennel."})
                    await _flush(db, buf, T_SQL)
                    return
                if kind == "ok":
                    tr = j.get("track", {}) or {}
                    buf.append((ak, tk, "ok", int(tr.get("listeners") or 0), int(tr.get("playcount") or 0),
                                json.dumps(_lfm_tag_names(tr.get("toptags")))))
                    _lfm_job["found"] += 1; consecutive_err = 0
                elif kind == "notfound":
                    buf.append((ak, tk, "notfound", None, None, None))
                    _lfm_job["notfound"] += 1; consecutive_err = 0
                else:
                    _lfm_job["errors"] += 1; consecutive_err += 1
                    if consecutive_err >= 20:
                        _lfm_job.update({"status": "failed",
                                         "message": "Last.fm unreachable (20 errors in a row) — will resume later."})
                        await _flush(db, buf, T_SQL)
                        return
                _lfm_job["done"] = i + 1
                left = (len(work) - i - 1) * _LFM_INTERVAL / 60
                _lfm_job["message"] = f"{artist} — {title}  ·  ~{left:.0f} min left"
                if len(buf) >= 100:
                    await _flush(db, buf, T_SQL)
                if (i + 1) % 5000 == 0 and _scan_job.get("status") != "running":
                    await _flush(db, buf, T_SQL)
                    await _rebuild_hit_scores()
                await asyncio.sleep(max(0.0, _LFM_INTERVAL - (time.monotonic() - t0)))
            await _flush(db, buf, T_SQL)

            # ── Phase 2: artist tags for songs that have no usable song-level genre ──
            if not _lfm_stop["flag"]:
                _lfm_job.update({"phase": "artists", "message": "Looking up artist genres…"})
                acache = {a: (st, _utc_epoch(f)) for a, st, f in await (await db.execute(
                    "SELECT artist_key, status, fetched_at FROM lastfm_artists")).fetchall()}
                need = {}
                for a, t, st, tags in await (await db.execute(
                        "SELECT artist_key, title_key, status, tags FROM lastfm_tracks")).fetchall():
                    if st != "ok" or not _genres_from_tags(tags):
                        need.setdefault(a, None)
                names = {}
                for artist, _t in rows:
                    names.setdefault(_artist_key(artist), _FEAT_RE.sub("", artist))
                todo = [a for a in need
                        if a in names and not (acache.get(a, (None, 0))[0]
                                               and now - acache[a][1] < _LFM_OK_DAYS * 86400)]
                _lfm_job.update({"done": 0, "total": len(todo)})
                A_SQL = ("INSERT INTO lastfm_artists (artist_key, status, tags, fetched_at) "
                         "VALUES (?, ?, ?, datetime('now')) ON CONFLICT(artist_key) DO UPDATE SET "
                         "status=excluded.status, tags=excluded.tags, fetched_at=excluded.fetched_at")
                for i, a in enumerate(todo):
                    if _lfm_stop["flag"]:
                        break
                    t0 = time.monotonic()
                    kind, j = await _lfm_call(client, {"method": "artist.getTopTags", "api_key": key,
                                                       "autocorrect": 1, "artist": names[a]})
                    if kind == "ok":
                        buf.append((a, "ok", json.dumps(_lfm_tag_names(j.get("toptags")))))
                    elif kind == "notfound":
                        buf.append((a, "notfound", None))
                    _lfm_job["done"] = i + 1
                    _lfm_job["message"] = f"Artist genres: {names[a]}  ·  ~{(len(todo)-i-1)*_LFM_INTERVAL/60:.0f} min left"
                    if len(buf) >= 100:
                        await _flush(db, buf, A_SQL)
                    await asyncio.sleep(max(0.0, _LFM_INTERVAL - (time.monotonic() - t0)))
                await _flush(db, buf, A_SQL)
    except Exception as e:
        log.exception("Last.fm pass failed")
        _lfm_job.update({"status": "failed", "message": f"Error: {e}"})
        return

    stopped = _lfm_stop["flag"]
    if _scan_job.get("status") != "running":
        _lfm_job["message"] = "Updating Hit Scores…"
        await _rebuild_hit_scores()
    _lfm_job.update({"status": "stopped" if stopped else "done", "phase": "",
                     "message": (f"{'Stopped' if stopped else 'Done'} — {_lfm_job['found']:,} found, "
                                 f"{_lfm_job['notfound']:,} not on Last.fm, {_lfm_job['errors']:,} errors")})
    log.info(f"Last.fm pass {_lfm_job['status']}: {_lfm_job['message']}")


def _maybe_start_lastfm():
    if _lfm_job.get("status") != "running":
        asyncio.create_task(_run_lastfm_job())


async def _lastfm_startup():
    await asyncio.sleep(180)   # let the app (and any scheduler imports) settle first
    if await _get_lastfm_key():
        _maybe_start_lastfm()


@router.get("/lastfm/status")
async def lastfm_status(_=Depends(require_auth)):
    cov = {"songs": 0, "ok": 0}
    try:
        async with aiosqlite.connect(_DYNAMIC_DB) as db:
            cov["ok"] = (await (await db.execute(
                "SELECT COUNT(*) FROM lastfm_tracks WHERE status='ok'")).fetchone())[0]
            cov["cached"] = (await (await db.execute("SELECT COUNT(*) FROM lastfm_tracks")).fetchone())[0]
    except Exception:
        pass
    return {**_lfm_job, "coverage": cov}


@router.post("/lastfm/start")
async def lastfm_start(_=Depends(require_auth)):
    if _lfm_job.get("status") == "running":
        raise HTTPException(409, "Already running.")
    if not await _get_lastfm_key():
        raise HTTPException(400, "Connect Last.fm in The Kennel first.")
    _maybe_start_lastfm()
    return {"ok": True}


@router.post("/lastfm/stop")
async def lastfm_stop(_=Depends(require_auth)):
    _lfm_stop["flag"] = True
    return {"ok": True}


@router.post("/hitscore/rebuild")
async def hitscore_rebuild(_=Depends(require_auth)):
    if _hit_score_job["status"] == "running":
        raise HTTPException(409, "Hit Scores are already being calculated.")
    if _scan_job.get("status") == "running":
        raise HTTPException(409, "A scan is running — scores update automatically when it finishes.")
    n = await _rebuild_hit_scores()
    if _hit_score_job["status"] == "failed":
        raise HTTPException(500, _hit_score_job["message"])
    return {"ok": True, "count": n}


