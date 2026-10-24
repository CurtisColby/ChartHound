# © 2026 Colby R. Curtis | ChartHound: The New World
# All Rights Reserved.
"""
ChartHound — The Groomer Router
Real Chart Data Playlist Builder

SCAN ARCHITECTURE (scan-everything, filter-at-playlist-time):
  Scan runs against ALL charts simultaneously — no chart filter at scan time.
  Playlist filters (genre/chart, peak, weeks, confidence) apply to stored results.
  One scan → unlimited playlists without rescanning.

Lookup waterfall per track:
  1. chart_data cache     → previously computed this scan, skip instantly
  2. chart_status cache   → confirmed miss within age gate, skip instantly
  3. Static DB UNION extras → chart_reference + billboard_pop + chart_reference_extras
                              (Billboard CSVs, utdata, chart2000, tsort, billboard yearend,
                               LBZ historical) — confidence: high/medium
  4. Comment tag read-back → re-parse previously written ChartHound tags (zero API)
  5. Last.fm listener count → genre-gated popularity estimate — confidence: low

Genre routing:
  - Retriever genre_1/2/3 pulled from DB onto track dict before waterfall
  - Used to route LBZ lookup to correct chart_name when multiple genres present
  - Used as quality gate for Last.fm estimate tier
  - CCM/gospel family: strict gate (no genre tag = reject at Last.fm tier)

Scan modes:
  A) Media Server Mode — Plex / Emby / Jellyfin library pull, playlist push back
  B) Local Folder Mode — walks physical files, M3U download only

Endpoints:
  GET  /api/groomer/charts/status
  POST /api/groomer/scan/start
  GET  /api/groomer/scan/status/{job_id}
  POST /api/groomer/scan/stop
  GET  /api/groomer/results
  GET  /api/groomer/db_stats
  POST /api/groomer/playlist/push
  POST /api/groomer/playlist/m3u
"""

# © 2026 Colby R. Curtis | ChartHound: The New World — All Rights Reserved.

import asyncio
import difflib
import json
import logging
import os
import random
import re
import sqlite3
import time

import aiosqlite
import httpx

from datetime import datetime, timedelta
from typing import Optional, List
from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel

from app.config import get_settings
from app.deps import require_auth
from app.security import decrypt_token

from app.routers.retriever import (
    peak_to_stars,
    format_chart_comment,
)

log      = logging.getLogger("charthound.groomer")
router   = APIRouter(prefix="/api/groomer", tags=["groomer"])
settings = get_settings()

# Paths — both injected via environment / docker-compose
_DYNAMIC_DB = getattr(settings, "database_url",     "/data/charthound.db")
_STATIC_DB  = getattr(settings, "static_db_url",    "/data/charthound_static.db")

# ── Chart display names ───────────────────────────────────────────────────────
CHART_DISPLAY = {
    "hot100":      "Hot 100",       "adultpop":   "Adult Pop",
    "ac":          "Adult Contemp", "uk":         "UK Singles",
    "country":     "Country",       "rnb":        "R&B/Hip-Hop",
    "dance":       "Dance",         "rock":       "Mainstream Rock",
    "classicrock": "Classic Rock",  "classical":  "Classical",
    "ccm":         "CCM",           "ccm-ac":     "CCM-AC",
    "ccm-rock":    "CCM Rock",      "worship":    "Worship",
    "ccm-country": "Christian Country", "ccm-folk": "Christian Folk",
    "ccm-blues":   "Christian Blues",
    "gospel":      "Gospel",        "sgospel":    "Southern Gospel",
    "ugospel":     "Urban Gospel",  "tgospel":    "Traditional Gospel",
    # LBZ-imported genres
    "hiphop":      "Hip-Hop",       "metal":      "Metal",
    "alternative": "Alternative",   "indie":      "Indie",
    "folk":        "Folk",          "jazz":       "Jazz",
    "blues":       "Blues",         "electronic": "Electronic",
}

MATCH_THRESHOLD = 0.82

# ── M1: lookup integrity ──────────────────────────────────────────────────────
# Bump when matching logic changes → every track is re-checked once on next scan.
_LOOKUP_VERSION = "m1-2026-10"
# Rows that are not real chart data (bad April import baked into static DB).
_EXCLUDED_SOURCES = ("lastfm_estimate", "test")
_EXCL_SQL = "AND COALESCE(data_source,'') NOT IN ('lastfm_estimate','test')"

import codecs as _codecs

def _latin1_fallback(err):
    """Decode invalid UTF-8 bytes as Latin-1 (Ç, ü, é…) instead of failing."""
    bad = err.object[err.start:err.end]
    return (bytes(bad).decode("latin-1"), err.end)

_codecs.register_error("ch_latin1", _latin1_fallback)


def _safe_json(r, service: str) -> dict:
    """Status-checked, encoding-tolerant JSON decode for media server responses."""
    if r.status_code == 401:
        raise RuntimeError(f"{service} rejected the token (401) — re-save it in The Kennel.")
    if r.status_code == 404:
        raise RuntimeError(f"{service} returned 404 — library may no longer exist. Re-select it.")
    if not r.is_success:
        raise RuntimeError(f"{service} returned HTTP {r.status_code}.")
    try:
        return json.loads(r.content.decode("utf-8", errors="ch_latin1"))
    except ValueError:
        raise RuntimeError(f"{service} sent a non-JSON response (HTTP {r.status_code}).")


def _utc_epoch(ts: Optional[str]) -> float:
    """Parse 'YYYY-MM-DD HH:MM:SS' or ISO (naive = UTC) → epoch. 0 if missing/bad."""
    if not ts:
        return 0.0
    try:
        import calendar
        t = str(ts).replace("T", " ").split(".")[0].split("+")[0].rstrip("Z")
        return float(calendar.timegm(datetime.strptime(t, "%Y-%m-%d %H:%M:%S").timetuple()))
    except Exception:
        return 0.0

# ── Last.fm listener thresholds by genre bucket ───────────────────────────────
_LFM_THRESHOLDS = {
    "ccm":     (100_000,  40_000,  15_000,  5_000,   5_000),   # loosened: min 5k
    "country": (2_000_000, 1_000_000, 500_000, 200_000, 200_000),
    "dance":   (2_000_000, 1_000_000, 500_000, 200_000, 200_000),
    "rnb":     (3_000_000, 1_500_000, 750_000, 300_000, 300_000),
    "rock":    (5_000_000, 2_000_000, 1_000_000, 500_000, 500_000),
    "default": (10_000_000, 5_000_000, 2_000_000, 1_000_000, 2_000_000),
}

_CCM_KEYWORDS = {
    "christian","ccm","gospel","worship","praise","hymn","religious",
    "inspirational","southern gospel","urban gospel",
    "contemporary christian","spiritual","jesus music",
}
_CCM_CHARTS = {"ccm","gospel","ccm-ac","ccm-rock","worship","sgospel","ugospel","tgospel","ccm-country","ccm-folk","ccm-blues"}

# CCM sub-charts that have no dedicated importer — fan out to parent 'ccm' in DB lookup
_CCM_SUBCHARTS_TO_PARENT = {
    "ccm-ac":      "ccm",
    "ccm-rock":    "ccm",
    "worship":     "ccm",
    "sgospel":     "gospel",
    "ugospel":     "gospel",
    "tgospel":     "gospel",
    "ccm-country": "ccm",
    "ccm-folk":    "ccm",
    "ccm-blues":   "ccm",
}

# Charts that require genre tag confirmation — no tag = reject (not benefit of the doubt)
_STRICT_GENRE_CHARTS = _CCM_CHARTS

# ── Reverse lookup: display name → chart key (for comment tag parsing) ────
_CHART_KEY_FROM_DISPLAY = {v.lower(): k for k, v in CHART_DISPLAY.items()}
# Add common alternates the parser might encounter
_CHART_KEY_FROM_DISPLAY.update({
    "uk singles": "uk",  "uk official": "uk",
    "r&b/hip-hop": "rnb", "r&b": "rnb", "hip-hop": "rnb",
    "mainstream rock": "rock", "adult contemp": "ac",
    "adult pop": "adultpop", "adult contemporary": "ac",
    "southern gospel": "sgospel", "urban gospel": "ugospel",
    "traditional gospel": "tgospel",
})


# ── Genre checkbox → file tag keywords (for get_results filtering) ────────────
# Each checkbox value maps to keywords that match against genre_1/2/3 on the track.
# Broad charts (hot100, ac, adultpop, uk) have no genre restriction — accept all.
# CCM/gospel family checked against CCM keywords (strict).
_GENRE_FILTER_KEYWORDS: dict = {
    # Broad — no file-tag restriction
    "hot100":    None,
    "adultpop":  None,
    "ac":        None,
    "uk":        None,
    # Genre-specific — must match file tag OR chart_name
    "country":   {"country","country pop","country rock","outlaw country","alt-country",
                  "alternative country","americana","bluegrass","honky tonk","honky-tonk",
                  "western","nashville","country folk","cowboy","texas country",
                  "red dirt","cajun","zydeco","bro-country","new country","traditional country"},
    "classicrock":{"classic rock","classic-rock","hard rock","arena rock","blues rock",
                   "roots rock","heartland rock","southern rock","rockabilly",
                   "psychedelic rock","progressive rock","prog rock","art rock",
                   "glam rock","pub rock","rock and roll","rock & roll"},
    "rock":      {"rock","classic rock","hard rock","arena rock","alternative rock",
                  "indie rock","garage rock","punk rock","post-punk","new wave",
                  "power pop","piano rock","blues rock","southern rock","art rock"},
    "rnb":       {"r&b","rnb","rhythm and blues","soul","neo-soul","motown","funk",
                  "urban","quiet storm","new jack swing","hip hop soul","contemporary r&b",
                  "classic soul","southern soul","northern soul","smooth r&b"},
    "hiphop":    {"hip hop","hip-hop","rap","gangsta rap","trap","drill","grime",
                  "boom bap","conscious rap","east coast","west coast","southern hip hop",
                  "crunk","g-funk","mumble rap","cloud rap","lo-fi hip hop",
                  "alternative hip hop","jazz rap"},
    "dance":     {"dance","edm","house","techno","trance","club","disco",
                  "dance-pop","eurodance","hi-nrg","hi nrg","dancehall","garage",
                  "uk garage","speed garage"},
    "electronic":{"electronic","electronica","edm","synth","ambient","industrial",
                  "downtempo","idm","glitch","breakbeat","drum and bass","dnb",
                  "dubstep","jungle","trip hop","chillout","chill out",
                  "new wave","synthwave","retrowave","vaporwave","darkwave",
                  "electro","electropop","synth-pop","synthpop"},
    "metal":     {"metal","heavy metal","death metal","thrash metal","black metal",
                  "doom metal","power metal","speed metal","glam metal","hair metal",
                  "nu-metal","nu metal","metalcore","deathcore","symphonic metal",
                  "progressive metal","groove metal","industrial metal","hard rock"},
    "alternative":{"alternative","alt rock","alternative rock","indie rock",
                   "grunge","post-grunge","britpop","shoegaze","noise rock",
                   "post-rock","math rock","emo","post-hardcore","dream pop",
                   "lo-fi","college rock","jangle pop"},
    "indie":     {"indie","indie rock","indie pop","indie folk","indie electronic",
                  "lo-fi","bedroom pop","chamber pop","twee pop","indiepop"},
    "folk":      {"folk","folk rock","folk pop","indie folk","contemporary folk",
                  "traditional folk","acoustic","singer-songwriter","americana",
                  "celtic folk","folk blues","anti-folk","freak folk"},
    "jazz":      {"jazz","bebop","swing","big band","cool jazz","hard bop",
                  "jazz fusion","fusion","soul jazz","smooth jazz","free jazz",
                  "latin jazz","bossa nova","jazz blues","jazz funk","vocal jazz",
                  "traditional jazz","dixieland"},
    "blues":     {"blues","chicago blues","delta blues","electric blues","texas blues",
                  "rhythm and blues","r&b","soul blues","jump blues","boogie woogie",
                  "boogie","country blues","acoustic blues","swamp blues"},
    "classical": {"classical","classical music","orchestral","symphony","opera",
                  "chamber music","baroque","romantic","contemporary classical",
                  "neo-classical","neoclassical","piano","concerto","sonata",
                  "choral","choir","instrumental","score"},
    # Pop — broad
    "pop":       {"pop","pop rock","synth-pop","synthpop","dance-pop","electropop",
                  "power pop","bubblegum pop","teen pop","art pop","chamber pop",
                  "indie pop","dream pop","k-pop","j-pop","europop"},
    # CCM family — strict: genre tag required
    "ccm":       {"christian","ccm","contemporary christian","gospel","worship","praise",
                  "hymn","religious","inspirational","southern gospel","urban gospel",
                  "traditional gospel","jesus music","spiritual","sacred","christian rock",
                  "christian pop","christian hip hop","christian metal","christian country",
                  "christian r&b","christian soul","new gospel","black gospel"},
    "gospel":    {"gospel","southern gospel","urban gospel","traditional gospel",
                  "black gospel","new gospel","christian","religious","spiritual","sacred"},
    "ccm-ac":    {"christian","ccm","contemporary christian","worship","inspirational",
                  "adult contemporary","christian adult contemporary"},
    "ccm-rock":  {"christian rock","christian metal","christian hardcore","christian punk",
                  "christian","ccm","worship","praise"},
    "ccm-country":{"christian country","gospel country","country gospel",
                   "southern gospel country","christian americana","christian bluegrass"},
    "ccm-folk":  {"christian folk","gospel folk","folk gospel","christian acoustic",
                  "christian singer-songwriter","worship folk"},
    "ccm-blues": {"christian blues","gospel blues","blues gospel","sacred blues",
                  "spiritual blues","christian rhythm and blues","christian r&b"},
    "worship":   {"worship","praise","christian","ccm","gospel","spiritual"},
    "sgospel":   {"southern gospel","gospel","christian","religious"},
    "ugospel":   {"urban gospel","gospel","christian","religious","r&b"},
    "tgospel":   {"traditional gospel","gospel","christian","religious","hymn"},
}

# ── Chart source → genre key fallback (for tracks with no file genre tags) ──
# Only reliable/clean chart sources used — not tsort, chart2000, LBZ extras
_CHART_SOURCE_TO_GENRE: dict = {
    "country":   "country",
    "rock":      "rock",
    "rnb":       "rnb",
    "hiphop":    "hiphop",
    "dance":     "dance",
    "adultpop":  "pop",
    "ac":        "pop",
    "hot100":    None,   # too broad — no genre fallback
    "uk":        None,   # too broad
    "metal":     "metal",
    "alternative":"alternative",
    "indie":     "indie",
    "folk":      "folk",
    "jazz":      "jazz",
    "blues":     "blues",
    "electronic":"electronic",
    "classicrock":"classicrock",
    "ccm":       "ccm",
    "gospel":    "gospel",
    "ccm-ac":    "ccm",
    "ccm-rock":  "ccm",
    "worship":   "ccm",
    "sgospel":   "gospel",
    "ugospel":   "gospel",
    "tgospel":   "gospel",
    "ccm-country":"ccm-country",
    "ccm-folk":  "ccm-folk",
    "ccm-blues": "ccm-blues",
}

# ── Comment tag regex — matches both exact and estimated formats ──────────
# "Hot 100: #4 (22 wks)" or "CCM: ~#12 (1 wks)" or "Country: #1 (30 wks)"
_COMMENT_ENTRY_RE = re.compile(
    r"([^:]+):\s*(~?)#(\d+)\s*\((\d+)\s*wks?\)",
    re.IGNORECASE,
)


def _parse_comment_tag(comment: str) -> list:
    """
    Parse a ChartHound comment string back into chart data dicts.
    Input:  "Hot 100: #4 (22 wks) | Adult Pop: #1 (10 wks)"
    Output: [
        {"chart_name":"hot100", "peak_position":4, "weeks_on_chart":22, "confidence":"high"},
        {"chart_name":"adultpop", "peak_position":1, "weeks_on_chart":10, "confidence":"high"},
    ]
    Returns empty list if comment is not ChartHound format.
    """
    if not comment or "#" not in comment:
        return []
    results = []
    for m in _COMMENT_ENTRY_RE.finditer(comment):
        display_name = m.group(1).strip()
        is_estimate  = bool(m.group(2))   # '~' prefix = low confidence
        peak         = int(m.group(3))
        weeks        = int(m.group(4))
        chart_key    = _CHART_KEY_FROM_DISPLAY.get(display_name.lower())
        if not chart_key:
            continue
        results.append({
            "chart_name":     chart_key,
            "peak_position":  peak,
            "weeks_on_chart": weeks,
            "confidence":     "low" if is_estimate else "high",
            "data_source":    "comment_readback",
            "chart_year":     None,
        })
    return results


def _read_comment_from_file(file_path: str) -> Optional[str]:
    """
    Read the COMMENT tag from a physical audio file via Mutagen.
    Handles MP3 (ID3 COMM frame), FLAC (Vorbis COMMENT), M4A (©cmt atom).
    Returns the raw comment string or None.
    """
    if not file_path or not os.path.exists(file_path):
        return None
    try:
        from mutagen import File as MutagenFile
        mf = MutagenFile(file_path, easy=False)
        if not mf or not mf.tags:
            return None

        comment = None
        ext = os.path.splitext(file_path)[1].lower()

        if ext == ".mp3":
            # ID3: look for COMM frame with desc='ChartHound' first, then any COMM
            for key, val in mf.tags.items():
                if key.startswith("COMM") and "ChartHound" in key:
                    comment = val.text[0] if hasattr(val, 'text') and val.text else str(val)
                    break
            if not comment:
                for key, val in mf.tags.items():
                    if key.startswith("COMM"):
                        text = val.text[0] if hasattr(val, 'text') and val.text else str(val)
                        if text and "#" in text:
                            comment = text
                            break

        elif ext == ".flac":
            raw = mf.tags.get("COMMENT") or mf.tags.get("comment")
            if raw:
                comment = raw[0] if isinstance(raw, list) else str(raw)

        elif ext in (".m4a", ".mp4", ".aac"):
            raw = mf.tags.get("©cmt")
            if raw:
                comment = raw[0] if isinstance(raw, list) else str(raw)

        else:
            # Generic fallback
            raw = mf.tags.get("COMMENT") or mf.tags.get("comment")
            if raw:
                comment = raw[0] if isinstance(raw, list) else str(raw)
            if not comment:
                for key, val in mf.tags.items():
                    if key.startswith("COMM"):
                        comment = val.text[0] if hasattr(val, 'text') and val.text else str(val)
                        break

        return str(comment) if comment and "#" in str(comment) else None
    except Exception:
        return None


# ── Genre → file tag keyword mapping ─────────────────────────────────────────
# Keys = chart_name values stored in chart_data / chart_reference_extras.
# Values = sets of substrings matched against the file's genre tag text (lowercased).
# Design rules:
#   - Prefer substrings so "Contemporary Christian Music" matches "christian"
#   - Order broad→specific; first match wins in _genre_matches_chart
#   - Add new keywords here as new Retriever genre categories are introduced
#   - Charts NOT listed (hot100, adultpop, ac, uk) accept any genre (broad charts)
#   - _STRICT_GENRE_CHARTS members reject files with NO genre tag at all

_CHART_GENRE_KEYWORDS = {
    # ── CCM / Gospel family (strict — no tag = reject) ───────────────────────
    "ccm":      {
        "christian", "ccm", "contemporary christian", "gospel", "worship", "praise",
        "hymn", "religious", "inspirational", "southern gospel", "urban gospel",
        "traditional gospel", "jesus music", "spiritual", "sacred", "christian rock",
        "christian pop", "christian hip hop", "christian metal", "christian country",
        "christian r&b", "christian soul", "new gospel", "black gospel",
    },
    "gospel":   {
        "gospel", "southern gospel", "urban gospel", "traditional gospel",
        "black gospel", "new gospel", "christian", "religious", "spiritual", "sacred",
    },
    "sgospel":  {"southern gospel", "gospel", "christian", "religious"},
    "ugospel":  {"urban gospel", "gospel", "christian", "religious", "r&b"},
    "tgospel":  {"traditional gospel", "gospel", "christian", "religious", "hymn"},
    "ccm-ac":   {
        "christian", "ccm", "contemporary christian", "worship", "inspirational",
        "adult contemporary", "christian adult contemporary",
    },
    "ccm-rock": {
        "christian rock", "christian metal", "christian hardcore", "christian punk",
        "christian", "ccm", "worship", "praise",
    },
    "worship":  {"worship", "praise", "christian", "ccm", "gospel", "spiritual"},

    # ── Country ───────────────────────────────────────────────────────────────
    "country":  {
        "country", "country pop", "country rock", "outlaw country", "alt-country",
        "alternative country", "americana", "bluegrass", "honky tonk", "honky-tonk",
        "western", "nashville", "country folk", "cowboy", "texas country",
        "red dirt", "cajun", "zydeco", "country blues", "country soul",
        "bro-country", "new country", "traditional country",
    },

    # ── Classic Rock ──────────────────────────────────────────────────────────
    "classicrock": {
        "classic rock", "classic-rock", "hard rock", "arena rock", "blues rock",
        "roots rock", "heartland rock", "southern rock", "rockabilly",
        "psychedelic rock", "progressive rock", "prog rock", "art rock",
        "glam rock", "pub rock", "rock and roll", "rock & roll",
    },

    # ── Classical ─────────────────────────────────────────────────────────────
    "classical": {
        "classical", "classical music", "orchestral", "symphony", "opera",
        "chamber music", "baroque", "romantic", "contemporary classical",
        "neo-classical", "neoclassical", "piano", "concerto", "sonata",
        "choral", "choir", "instrumental", "score", "soundtrack",
    },

    # ── Christian Country ─────────────────────────────────────────────────────
    "ccm-country": {
        "christian country", "gospel country", "country gospel",
        "southern gospel country", "christian americana",
        "christian bluegrass", "country christian",
    },

    # ── Christian Folk ────────────────────────────────────────────────────────
    "ccm-folk": {
        "christian folk", "gospel folk", "folk gospel", "christian acoustic",
        "christian singer-songwriter", "worship folk", "christian indie folk",
    },

    # ── Christian Blues ───────────────────────────────────────────────────────
    "ccm-blues": {
        "christian blues", "gospel blues", "blues gospel", "sacred blues",
        "spiritual blues", "christian rhythm and blues", "christian r&b",
    },

    # ── R&B / Hip-Hop ─────────────────────────────────────────────────────────
    "rnb":      {
        "r&b", "rnb", "rhythm and blues", "soul", "neo-soul", "motown", "funk",
        "urban", "quiet storm", "new jack swing", "hip hop soul", "contemporary r&b",
        "classic soul", "southern soul", "northern soul", "smooth r&b",
    },

    # ── Hip-Hop ───────────────────────────────────────────────────────────────
    "hiphop":   {
        "hip hop", "hip-hop", "rap", "gangsta rap", "trap", "drill", "grime",
        "boom bap", "conscious rap", "east coast", "west coast", "southern hip hop",
        "crunk", "g-funk", "mumble rap", "cloud rap", "lo-fi hip hop",
        "alternative hip hop", "jazz rap", "political hip hop",
    },

    # ── Dance / Electronic ────────────────────────────────────────────────────
    "dance":    {
        "dance", "edm", "house", "techno", "trance", "club", "disco",
        "dance-pop", "eurodance", "hi-nrg", "hi nrg", "dance hall", "dancehall",
        "garage", "uk garage", "speed garage",
    },
    "electronic": {
        "electronic", "electronica", "edm", "synth", "ambient", "industrial",
        "downtempo", "idm", "glitch", "breakbeat", "drum and bass", "dnb",
        "dubstep", "jungle", "trip hop", "chillout", "chill out",
        "new wave", "synthwave", "retrowave", "vaporwave", "darkwave",
        "electro", "electropop", "synth-pop", "synthpop",
    },

    # ── Rock (mainstream / billboard rock) ───────────────────────────────────
    "rock":     {
        "rock", "classic rock", "hard rock", "arena rock", "glam rock",
        "garage rock", "psychedelic rock", "progressive rock", "prog rock",
        "southern rock", "surf rock", "heartland rock", "blues rock",
        "jam band", "rockabilly", "roots rock", "pub rock", "new wave",
        "post-punk", "art rock", "piano rock", "power pop",
    },

    # ── Alternative ───────────────────────────────────────────────────────────
    "alternative": {
        "alternative", "alt rock", "alternative rock", "indie rock",
        "grunge", "post-grunge", "britpop", "shoegaze", "noise rock",
        "post-rock", "math rock", "emo", "post-hardcore", "dream pop",
        "lo-fi", "college rock", "jangle pop",
    },

    # ── Indie ─────────────────────────────────────────────────────────────────
    "indie":    {
        "indie", "indie rock", "indie pop", "indie folk", "indie electronic",
        "lo-fi", "bedroom pop", "chamber pop", "twee pop", "indiepop",
    },

    # ── Metal ─────────────────────────────────────────────────────────────────
    "metal":    {
        "metal", "heavy metal", "death metal", "thrash metal", "black metal",
        "doom metal", "power metal", "speed metal", "glam metal", "hair metal",
        "nu-metal", "nu metal", "metalcore", "deathcore", "symphonic metal",
        "progressive metal", "groove metal", "industrial metal", "hard rock",
    },

    # ── Folk ──────────────────────────────────────────────────────────────────
    "folk":     {
        "folk", "folk rock", "folk pop", "indie folk", "contemporary folk",
        "traditional folk", "acoustic", "singer-songwriter", "americana",
        "celtic folk", "folk blues", "anti-folk", "freak folk",
    },

    # ── Jazz ──────────────────────────────────────────────────────────────────
    "jazz":     {
        "jazz", "bebop", "swing", "big band", "cool jazz", "hard bop",
        "jazz fusion", "fusion", "soul jazz", "smooth jazz", "free jazz",
        "latin jazz", "bossa nova", "jazz blues", "jazz funk", "vocal jazz",
        "traditional jazz", "dixieland",
    },

    # ── Blues ─────────────────────────────────────────────────────────────────
    "blues":    {
        "blues", "chicago blues", "delta blues", "electric blues", "texas blues",
        "rhythm and blues", "r&b", "soul blues", "jump blues", "boogie woogie",
        "boogie", "country blues", "acoustic blues", "swamp blues",
    },
}

# ── Genre tag → chart_name routing map ───────────────────────────────────────
# Used to map Retriever genre_1/2/3 values → which chart_name to query in LBZ extras.
# Broader than _CHART_GENRE_KEYWORDS — catches real-world Mutagen/Plex tag strings.
# Keys are lowercased substrings; values are chart_name keys in chart_reference_extras.
# Order matters: checked top-to-bottom, first match wins.
_GENRE_TAG_TO_CHART_NAME: list = [
    # CCM / Gospel — check before rock/pop since "christian rock" contains "rock"
    ("christian country",      "ccm-country"),
    ("christian bluegrass",    "ccm-country"),
    ("christian americana",    "ccm-country"),
    ("christian folk",         "ccm-folk"),
    ("christian acoustic",     "ccm-folk"),
    ("christian blues",        "ccm-blues"),
    ("gospel blues",           "ccm-blues"),
    ("christian",              "ccm"),
    ("ccm",                    "ccm"),
    ("gospel",                 "gospel"),
    ("worship",                "ccm"),
    ("praise",                 "ccm"),
    ("hymn",                   "ccm"),
    ("spiritual",              "ccm"),
    ("religious",              "ccm"),
    ("inspirational",          "ccm"),
    ("jesus music",            "ccm"),
    ("sacred",                 "ccm"),
    # Hip-Hop — before r&b
    ("hip hop",            "hiphop"),
    ("hip-hop",            "hiphop"),
    ("rap",                "hiphop"),
    ("trap",               "hiphop"),
    ("drill",              "hiphop"),
    ("grime",              "hiphop"),
    # R&B
    ("r&b",                "rnb"),
    ("rnb",                "rnb"),
    ("rhythm and blues",   "rnb"),
    ("soul",               "rnb"),
    ("motown",             "rnb"),
    ("funk",               "rnb"),
    # Country
    ("country",            "country"),
    ("bluegrass",          "country"),
    ("americana",          "country"),
    ("honky tonk",         "country"),
    ("outlaw",             "country"),
    ("nashville",          "country"),
    ("western",            "country"),
    # Metal — before rock
    ("metal",              "metal"),
    ("metalcore",          "metal"),
    ("deathcore",          "metal"),
    # Alternative — before rock
    ("grunge",             "alternative"),
    ("shoegaze",           "alternative"),
    ("britpop",            "alternative"),
    ("post-grunge",        "alternative"),
    # Indie — before rock/alternative
    ("indie",              "indie"),
    ("bedroom pop",        "indie"),
    ("lo-fi",              "indie"),
    # Electronic/Dance — before generic dance
    ("synth-pop",          "electronic"),
    ("synthpop",           "electronic"),
    ("synthwave",          "electronic"),
    ("ambient",            "electronic"),
    ("idm",                "electronic"),
    ("dubstep",            "electronic"),
    ("drum and bass",      "electronic"),
    ("dnb",                "electronic"),
    ("trip hop",           "electronic"),
    ("downtempo",          "electronic"),
    ("new wave",           "electronic"),
    ("darkwave",           "electronic"),
    ("industrial",         "electronic"),
    ("electronica",        "electronic"),
    ("electronic",         "electronic"),
    ("edm",                "dance"),
    ("house",              "dance"),
    ("techno",             "dance"),
    ("trance",             "dance"),
    ("disco",              "dance"),
    ("eurodance",          "dance"),
    ("dance",              "dance"),
    # Folk
    ("folk",               "folk"),
    ("singer-songwriter",  "folk"),
    # Jazz
    ("jazz",               "jazz"),
    ("bebop",              "jazz"),
    ("swing",              "jazz"),
    ("bossa nova",         "jazz"),
    ("big band",           "jazz"),
    # Blues
    ("blues",              "blues"),
    ("boogie",             "blues"),
    # Rock — broad, catch-all after specifics
    ("classic rock",       "classicrock"),
    ("classic-rock",       "classicrock"),
    ("arena rock",         "classicrock"),
    ("blues rock",         "classicrock"),
    ("southern rock",      "classicrock"),
    ("heartland rock",     "classicrock"),
    ("rockabilly",         "classicrock"),
    ("rock and roll",      "classicrock"),
    ("rock & roll",        "classicrock"),
    ("rock",               "rock"),
    ("punk",               "alternative"),
    ("post-punk",          "alternative"),
    ("emo",                "alternative"),
    # Classical
    ("classical",          "classical"),
    ("orchestral",         "classical"),
    ("symphony",           "classical"),
    ("opera",              "classical"),
    ("chamber music",      "classical"),
    ("baroque",            "classical"),
    # Pop — maps to adultpop chart data
    ("pop",                "adultpop"),
]


def _genre_matches_chart(genre_tags: list, chart_name: str) -> bool:
    """
    Returns True if the file's genre tags are compatible with the chart filter.
    Charts not in _CHART_GENRE_KEYWORDS (hot100, adultpop, ac, uk) accept
    any genre — they are broad/general charts.
    Strict charts (CCM/gospel family): no genre tag = reject (not benefit of the doubt).
    """
    keywords = _CHART_GENRE_KEYWORDS.get(chart_name)
    if not keywords:
        return True  # hot100, adultpop, ac, uk — no genre restriction
    genre_text = " ".join(g.lower() for g in genre_tags if g)
    if not genre_text:
        # Strict charts require a genre tag to confirm — reject ambiguous files
        if chart_name in _STRICT_GENRE_CHARTS:
            return False
        return True  # non-strict charts: benefit of the doubt
    return any(kw in genre_text for kw in keywords)


def _detect_genre_bucket(genre_tags: list, chart_names: list) -> str:
    """Map genre tags + chart names to a Last.fm threshold bucket."""
    all_text = " ".join((genre_tags or []) + (chart_names or [])).lower()
    if any(kw in all_text for kw in _CCM_KEYWORDS): return "ccm"
    if any(c in _CCM_CHARTS for c in (chart_names or [])): return "ccm"
    if "country" in all_text: return "country"
    if any(k in all_text for k in ("dance","electronic","edm","house","club")): return "dance"
    if any(k in all_text for k in ("r&b","rnb","soul","hip hop","hip-hop","rap")): return "rnb"
    if "rock" in all_text: return "rock"
    return "default"


def _genre_tags_to_chart_names(genre_tags: list) -> list:
    """
    Map Retriever genre_1/2/3 values to chart_name keys for LBZ extras routing.
    Returns a deduplicated list of chart_name strings in priority order.
    Checks all tags against _GENRE_TAG_TO_CHART_NAME ordered rules.
    E.g. ["Christian Rock", "Rock"] → ["ccm", "rock"]
    """
    genre_text = " | ".join(g.lower() for g in genre_tags if g)
    if not genre_text:
        return []
    found = []
    for keyword, chart_name in _GENRE_TAG_TO_CHART_NAME:
        if keyword in genre_text and chart_name not in found:
            found.append(chart_name)
    return found
    all_text = " ".join((genre_tags or []) + (chart_names or [])).lower()
    if any(kw in all_text for kw in _CCM_KEYWORDS): return "ccm"
    if any(c in _CCM_CHARTS for c in (chart_names or [])): return "ccm"
    if "country" in all_text: return "country"
    if any(k in all_text for k in ("dance","electronic","edm","house","club")): return "dance"
    if any(k in all_text for k in ("r&b","rnb","soul","hip hop","hip-hop","rap")): return "rnb"
    if "rock" in all_text: return "rock"
    return "default"


def _listeners_to_stars(listeners: int, bucket: str) -> int:
    t = _LFM_THRESHOLDS.get(bucket, _LFM_THRESHOLDS["default"])
    if listeners >= t[0]: return 5
    if listeners >= t[1]: return 4
    if listeners >= t[2]: return 3
    if listeners >= t[3]: return 2
    return 1


def _listeners_to_est_peak(listeners: int, bucket: str) -> int:
    """
    Estimates a chart peak position from Last.fm listener count.
    All results stored with confidence='low' so users know these
    are popularity estimates, not real Billboard positions.
    CCM uses tighter thresholds since the CCM audience is smaller.
    """
    import random
    t = _LFM_THRESHOLDS.get(bucket, _LFM_THRESHOLDS["default"])
    if bucket == "ccm":
        # CCM-specific tiers — smaller market, tighter thresholds
        if listeners >= t[0]: return random.randint(1, 10)   # >= 100k → top CCM
        if listeners >= t[1]: return random.randint(11, 20)  # >= 40k  → solid
        if listeners >= t[2]: return random.randint(21, 40)  # >= 15k  → known
        return random.randint(41, 100)                        # >= 5k   → minor
    if listeners >= t[0]: return random.randint(1, 5)
    if listeners >= t[1]: return random.randint(6, 15)
    if listeners >= t[2]: return random.randint(16, 30)
    if listeners >= t[3]: return random.randint(31, 50)
    return random.randint(51, 80)


def _meets_min_threshold(listeners: int, bucket: str) -> bool:
    return listeners >= _LFM_THRESHOLDS.get(bucket, _LFM_THRESHOLDS["default"])[4]


# ── In-memory scan job tracker ────────────────────────────────────────────────
_scan_job = {
    "status": "idle", "job_id": None, "message": "",
    "total": 0, "processed": 0, "matched": 0, "failed": 0, "cached": 0,
    "started_at": None, "stop_requested": False,
}


# ══════════════════════════════════════════════════════════════════════════════
#  STATIC DB HELPERS  (synchronous — used in executor threads)
# ══════════════════════════════════════════════════════════════════════════════

def _norm(s: str) -> str:
    """Lowercase, strip punctuation and 'the '/'a ' prefixes for matching."""
    s = s.lower().strip()
    s = re.sub(r"[^\w\s]", "", s)
    s = re.sub(r"^(the|a|an)\s+", "", s)
    return s.strip()


def _fuzzy(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, a, b).ratio()


def _open_static_conn():
    """Open static DB once with dynamic DB attached. Returns (conn, has_extras) or (None, False)."""
    if not os.path.exists(_STATIC_DB):
        return None, False
    conn = sqlite3.connect(_STATIC_DB, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    has_extras = False
    try:
        conn.execute(f"ATTACH DATABASE '{_DYNAMIC_DB}' AS dyn")
        has_extras = bool(conn.execute(
            "SELECT name FROM dyn.sqlite_master WHERE type='table' AND name='chart_reference_extras'"
        ).fetchone())
    except Exception:
        has_extras = False
    return conn, has_extras


def _lookup_static(artist: str, title: str, charts: list, shared=None) -> Optional[dict]:
    """
    Synchronous lookup against charthound_static.db.
    Checks chart_reference first (Billboard CSV data), then billboard_pop
    (historical pop 1890-2015).
    Returns dict with peak_position, weeks_on_chart, chart_name, confidence,
    chart_year, data_source — or None if no match.

    CCM fan-out: sub-charts (ccm-ac, ccm-rock, worship, sgospel, ugospel, tgospel)
    have no dedicated DB rows — they fan out to their parent chart_name ('ccm'/'gospel')
    so the query still finds data. The result chart_name is preserved from the DB row.
    """
    artist_n = _norm(artist)
    title_n  = _norm(title)

    # ── CCM/gospel sub-chart fan-out ─────────────────────────────────────────
    # Expand requested charts to include parent chart names for sub-charts that
    # share a data pool (e.g. ccm-ac → also query 'ccm').
    expanded_charts = list(charts)
    for c in charts:
        parent = _CCM_SUBCHARTS_TO_PARENT.get(c)
        if parent and parent not in expanded_charts:
            expanded_charts.append(parent)

    # shared = (conn, has_extras) reused across a whole scan; else open per call
    own = shared is None
    conn = None
    try:
        if own:
            conn, has_extras = _open_static_conn()
            if conn is None:
                return None
        else:
            conn, has_extras = shared

        def _close():
            if own and conn is not None:
                conn.close()

        # ── Step 1: chart_reference (Billboard CSVs) + extras (user-imported) ─
        chart_filter = ""
        params: list = [artist_n, title_n]
        if expanded_charts:
            placeholders = ",".join("?" * len(expanded_charts))
            chart_filter = f"AND chart_name IN ({placeholders})"
            params += expanded_charts

        if has_extras:
            # UNION ALL — extras rows sit alongside static rows; ORDER BY picks best peak
            union_params = list(params) + list(params)  # params needed twice
            rows = conn.execute(f"""
                SELECT chart_name, peak_position, weeks_on_chart, chart_year,
                       artist_norm, title_norm, data_source
                FROM chart_reference
                WHERE artist_norm = ? AND title_norm = ?
                {chart_filter} {_EXCL_SQL}
                UNION ALL
                SELECT chart_name, peak_position, weeks_on_chart, chart_year,
                       artist_norm, title_norm, data_source
                FROM dyn.chart_reference_extras
                WHERE artist_norm = ? AND title_norm = ?
                {chart_filter} {_EXCL_SQL}
                ORDER BY peak_position ASC
                LIMIT 20
            """, union_params).fetchall()
        else:
            rows = conn.execute(f"""
                SELECT chart_name, peak_position, weeks_on_chart, chart_year,
                       artist_norm, title_norm, data_source
                FROM chart_reference
                WHERE artist_norm = ? AND title_norm = ?
                {chart_filter} {_EXCL_SQL}
                ORDER BY peak_position ASC
                LIMIT 20
            """, params).fetchall()

        # Fuzzy fallback if exact norm match fails
        if not rows:
            like_params = [artist_n[:6] + "%"] + (expanded_charts if expanded_charts else [])
            if has_extras:
                union_like = list(like_params) + list(like_params)
                candidates = conn.execute(f"""
                    SELECT chart_name, peak_position, weeks_on_chart, chart_year,
                           artist_norm, title_norm, data_source
                    FROM chart_reference
                    WHERE artist_norm LIKE ? {chart_filter} {_EXCL_SQL}
                    UNION ALL
                    SELECT chart_name, peak_position, weeks_on_chart, chart_year,
                           artist_norm, title_norm, data_source
                    FROM dyn.chart_reference_extras
                    WHERE artist_norm LIKE ? {chart_filter} {_EXCL_SQL}
                    LIMIT 400
                """, union_like).fetchall()
            else:
                candidates = conn.execute(f"""
                    SELECT chart_name, peak_position, weeks_on_chart, chart_year,
                           artist_norm, title_norm, data_source
                    FROM chart_reference
                    WHERE artist_norm LIKE ? {chart_filter} {_EXCL_SQL}
                    LIMIT 200
                """, like_params).fetchall()

            best = None
            best_score = 0.0
            for c in candidates:
                score = (_fuzzy(artist_n, c["artist_norm"]) * 0.5 +
                         _fuzzy(title_n,  c["title_norm"])  * 0.5)
                if score > best_score and score >= MATCH_THRESHOLD:
                    best_score = score
                    best = c
            if best:
                rows = [best]

        if rows:
            best_row = rows[0]
            _close()
            return {
                "peak_position":  best_row["peak_position"],
                "weeks_on_chart": best_row["weeks_on_chart"] or 1,
                "chart_name":     best_row["chart_name"],
                "chart_year":     best_row["chart_year"],
                "confidence":     "high",
                "data_source":    best_row["data_source"],
                "all_charts":     [dict(r) for r in rows],
            }

        # ── Step 2: billboard_pop (historical pop 1890-2015) ──────────────────
        bp_rows = conn.execute("""
            SELECT artist, title, peak_position, chart_weeks, year, genre
            FROM billboard_pop
            WHERE LOWER(REPLACE(REPLACE(artist, '.', ''), ',', '')) LIKE ?
            LIMIT 100
        """, [artist_n[:8] + "%"]).fetchall()

        best = None
        best_score = 0.0
        for r in bp_rows:
            score = (_fuzzy(artist_n, _norm(r["artist"])) * 0.5 +
                     _fuzzy(title_n,  _norm(r["title"]))  * 0.5)
            if score > best_score and score >= MATCH_THRESHOLD:
                best_score = score
                best = r

        _close()

        if best:
            return {
                "peak_position":  best["peak_position"] or 100,
                "weeks_on_chart": best["chart_weeks"] or 1,
                "chart_name":     "hot100",
                "chart_year":     best["year"],
                "confidence":     "high",
                "data_source":    "billboard_pop_1890_2015",
                "all_charts":     [],
            }

        _close()
        return None

    except Exception as e:
        log.warning(f"Static DB lookup error: {e}")
        try:
            if own and conn is not None:
                conn.close()
        except Exception:
            pass
        return None


# ══════════════════════════════════════════════════════════════════════════════
#  LAST.FM FALLBACK
# ══════════════════════════════════════════════════════════════════════════════

async def _lastfm_listeners(artist: str, title: str, lfm_key: str) -> int:
    if not lfm_key:
        return 0
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            r = await client.get("https://ws.audioscrobbler.com/2.0/", params={
                "method": "track.getInfo", "api_key": lfm_key,
                "artist": artist, "track": title, "format": "json",
                "autocorrect": "1",
            })
            if r.is_success:
                data = r.json()
                listeners = int(data.get("track", {}).get("listeners", 0))
                return listeners
    except Exception:
        pass
    return 0



# ══════════════════════════════════════════════════════════════════════════════
#  LBZ EXTRAS FALLBACK LOOKUP
# ══════════════════════════════════════════════════════════════════════════════

# LBZ genres stored in chart_reference_extras by chart_name
_LBZ_CHART_NAMES = {
    "hiphop", "metal", "alternative", "indie", "folk", "jazz", "blues", "electronic"
}


async def _lookup_lbz_extras(artist: str, title: str, charts: list) -> Optional[dict]:
    """
    Async lookup against chart_reference_extras for LBZ-imported genre data.
    Fires after static DB miss, before Last.fm — zero API calls.
    Only queries chart_names that are LBZ-sourced genres.
    Returns same shape dict as _lookup_static, with confidence='medium'.
    """
    lbz_charts = [c for c in charts if c in _LBZ_CHART_NAMES]
    if not lbz_charts:
        return None

    artist_n = _norm(artist)
    title_n  = _norm(title)

    try:
        async with aiosqlite.connect(_DYNAMIC_DB) as db:
            db.row_factory = aiosqlite.Row
            placeholders = ",".join("?" * len(lbz_charts))

            # Exact match first
            async with db.execute(f"""
                SELECT chart_name, peak_position, weeks_on_chart, chart_year, data_source
                FROM chart_reference_extras
                WHERE artist_norm = ? AND title_norm = ?
                  AND chart_name IN ({placeholders})
                ORDER BY peak_position ASC
                LIMIT 10
            """, [artist_n, title_n] + lbz_charts) as cur:
                rows = await cur.fetchall()

            # Fuzzy fallback
            if not rows:
                async with db.execute(f"""
                    SELECT chart_name, peak_position, weeks_on_chart, chart_year,
                           artist_norm, title_norm, data_source
                    FROM chart_reference_extras
                    WHERE artist_norm LIKE ?
                      AND chart_name IN ({placeholders})
                    LIMIT 300
                """, [artist_n[:6] + "%"] + lbz_charts) as cur:
                    candidates = await cur.fetchall()

                best = None
                best_score = 0.0
                for c in candidates:
                    score = (_fuzzy(artist_n, c["artist_norm"]) * 0.5 +
                             _fuzzy(title_n,  c["title_norm"])  * 0.5)
                    if score > best_score and score >= MATCH_THRESHOLD:
                        best_score = score
                        best = c
                if best:
                    rows = [best]

            if not rows:
                return None

            best_row = rows[0]
            return {
                "peak_position":  best_row["peak_position"],
                "weeks_on_chart": best_row["weeks_on_chart"] or 1,
                "chart_name":     best_row["chart_name"],
                "chart_year":     best_row["chart_year"],
                "confidence":     "medium",
                "data_source":    "listenbrainz_historical",
                "all_charts":     [dict(r) for r in rows],
            }

    except Exception as e:
        log.warning(f"LBZ extras lookup error: {e}")
        return None


# ══════════════════════════════════════════════════════════════════════════════
#  CHART STATUS
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/charts/status")
async def charts_status(_=Depends(require_auth)):
    """Returns chart reference counts from both static and dynamic DBs."""
    result = {}

    # Static DB counts
    if os.path.exists(_STATIC_DB):
        try:
            conn = sqlite3.connect(_STATIC_DB, check_same_thread=False)
            rows = conn.execute(
                "SELECT chart_name, COUNT(*) as cnt FROM chart_reference GROUP BY chart_name"
            ).fetchall()
            for r in rows:
                result[r[0]] = {"count": r[1], "source": "static", "status": "loaded"}
            bp_count = conn.execute("SELECT COUNT(*) FROM billboard_pop").fetchone()[0]
            result["billboard_pop"] = {
                "count": bp_count, "source": "static",
                "status": "loaded", "display": "Billboard Pop 1890-2015"
            }
            conn.close()
        except Exception as e:
            log.warning(f"Static DB status error: {e}")

    # Dynamic DB meta
    try:
        async with aiosqlite.connect(_DYNAMIC_DB) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("SELECT * FROM chart_reference_meta") as cur:
                async for row in cur:
                    name = row["chart_name"]
                    if name not in result:
                        result[name] = {
                            "count": row["entry_count"] or 0,
                            "status": row["status"],
                            "display": row["display_name"],
                            "source": "dynamic",
                        }
    except Exception:
        pass

    return {"charts": result, "static_db": os.path.exists(_STATIC_DB)}


# ══════════════════════════════════════════════════════════════════════════════
#  DB STATS
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/verify-tags")
async def verify_tags(_=Depends(require_auth)):
    """
    Samples up to 5 recently written chart_data rows and verifies the
    COMMENT tag was actually written to the physical file.
    Returns list of {file_path, expected, actual, ok} per file checked.
    """
    import json as _json
    results = []

    # Get path translation settings
    server_prefix = ""
    docker_prefix = "/music"
    try:
        async with aiosqlite.connect(_DYNAMIC_DB) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT key, value FROM app_settings "
                "WHERE key IN ('path_server_prefix','path_docker_prefix')"
            ) as cur:
                rows = await cur.fetchall()
            for r in rows:
                if r["key"] == "path_server_prefix":
                    server_prefix = r["value"] or ""
                elif r["key"] == "path_docker_prefix":
                    docker_prefix = r["value"] or "/music"
    except Exception:
        pass

    # Sample up to 5 chart_data rows with comment_string and file_path
    try:
        async with aiosqlite.connect(_DYNAMIC_DB) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("""
                SELECT cd.comment_string, t.file_path
                FROM chart_data cd
                JOIN tracks t ON cd.track_id = t.track_id
                WHERE cd.comment_string IS NOT NULL
                  AND cd.comment_string != ''
                  AND t.file_path IS NOT NULL
                  AND t.file_path != ''
                ORDER BY cd.fetched_at DESC
                LIMIT 5
            """) as cur:
                rows = await cur.fetchall()
    except Exception as e:
        raise HTTPException(500, f"DB error: {e}")

    for row in rows:
        fp       = row["file_path"]
        expected = row["comment_string"]

        # Translate server path → docker path if needed
        docker_fp = fp
        if server_prefix and fp.startswith(server_prefix):
            docker_fp = docker_prefix + fp[len(server_prefix):]
        elif fp.startswith("/music") or fp.startswith(docker_prefix):
            docker_fp = fp  # already docker path

        if not os.path.exists(docker_fp):
            results.append({
                "file_path": fp,
                "expected":  expected,
                "actual":    None,
                "ok":        False,
                "error":     "File not found at docker path"
            })
            continue

        try:
            from mutagen import File as MutagenFile
            mf = MutagenFile(docker_fp, easy=False)
            actual = None
            if mf:
                # FLAC / Vorbis comment
                if hasattr(mf, 'tags') and mf.tags:
                    actual = (
                        mf.tags.get("COMMENT", [None])[0]
                        or mf.tags.get("comment", [None])[0]
                        # ID3 COMM frame
                        or str(next((v for k,v in mf.tags.items()
                                     if k.startswith("COMM")), None) or "")
                        or None
                    )
                    # Handle ID3 COMM object
                    if actual and hasattr(actual, 'text'):
                        actual = actual.text[0] if actual.text else str(actual)
            results.append({
                "file_path": fp,
                "expected":  expected,
                "actual":    actual,
                "ok":        actual is not None and expected in str(actual),
            })
        except Exception as e:
            results.append({
                "file_path": fp,
                "expected":  expected,
                "actual":    None,
                "ok":        False,
                "error":     str(e)
            })

    return {"verified": results, "count": len(results)}


@router.get("/libraries")
async def get_libraries(server: str, _=Depends(require_auth)):
    """
    Returns music libraries from the selected media server.
    Used to populate the library selector dropdown in the UI.
    """
    try:
        async with aiosqlite.connect(_DYNAMIC_DB) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT base_url, token_enc, extra_json FROM connections WHERE service=?",
                (server,)
            ) as cur:
                conn = await cur.fetchone()
    except Exception:
        conn = None

    if not conn:
        raise HTTPException(400, f"No {server} connection configured")

    base_url = conn["base_url"]
    token    = decrypt_token(conn["token_enc"]) if conn["token_enc"] else ""
    extra    = json.loads(conn["extra_json"] or "{}") if conn["extra_json"] else {}
    libraries = []

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            if server == "plex":
                r = await client.get(f"{base_url}/library/sections?X-Plex-Token={token}",
                    headers={"Accept":"application/json"})
                if r.is_success:
                    sections = r.json().get("MediaContainer",{}).get("Directory",[])
                    libraries = [{"id": s["key"], "name": s.get("title","Unknown"),
                                  "count": s.get("count", 0)}
                                 for s in sections if s.get("type") == "artist"]
            elif server in ("emby", "jellyfin"):
                user_id = extra.get("user_id","")
                hdrs = {"Accept":"application/json"}
                if server == "emby":
                    hdrs["X-Emby-Token"] = token
                r = await client.get(f"{base_url}/Users/{user_id}/Views",
                    params={"api_key": token}, headers=hdrs)
                if r.is_success:
                    items = r.json().get("Items", [])
                    libraries = [{"id": i["Id"], "name": i.get("Name","Unknown")}
                                 for i in items
                                 if i.get("CollectionType") in ("music", "Music") or
                                    "music" in i.get("Name","").lower()]
    except Exception as e:
        log.warning(f"Library fetch error for {server}: {e}")

    return {"libraries": libraries, "server": server}


@router.get("/tagged_count")
async def tagged_count(_=Depends(require_auth)):
    """Returns count of tracks with genre_1 populated (Retriever-tagged)."""
    try:
        async with aiosqlite.connect(_DYNAMIC_DB) as db:
            async with db.execute(
                "SELECT COUNT(*) FROM tracks WHERE genre_1 IS NOT NULL AND genre_1 != ''"
            ) as cur:
                row = await cur.fetchone()
                return {"tagged": row[0] if row else 0}
    except Exception:
        return {"tagged": 0}


@router.get("/db_stats")
async def db_stats(_=Depends(require_auth)):
    static_total = 0
    extras_total = 0
    dynamic_chart_data = 0

    if os.path.exists(_STATIC_DB):
        try:
            conn = sqlite3.connect(_STATIC_DB, check_same_thread=False)
            r1 = conn.execute("SELECT COUNT(*) FROM chart_reference").fetchone()[0]
            r2 = conn.execute("SELECT COUNT(*) FROM billboard_pop").fetchone()[0]
            static_total = r1 + r2
            conn.close()
        except Exception:
            pass

    try:
        async with aiosqlite.connect(_DYNAMIC_DB) as db:
            async with db.execute("SELECT COUNT(*) FROM chart_data") as cur:
                row = await cur.fetchone()
                dynamic_chart_data = row[0] if row else 0
            # User-imported chart entries (chart_reference_extras may not exist yet)
            try:
                async with db.execute("SELECT COUNT(*) FROM chart_reference_extras") as cur:
                    row = await cur.fetchone()
                    extras_total = row[0] if row else 0
            except Exception:
                extras_total = 0
    except Exception:
        pass

    return {
        "static_entries":  static_total,
        "extras_entries":  extras_total,
        "total_entries":   static_total + extras_total,
        "cached_results":  dynamic_chart_data,
        "static_db_ready": os.path.exists(_STATIC_DB),
    }


# ══════════════════════════════════════════════════════════════════════════════
#  SKIP CACHE (chart_status on tracks table)
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/skip_cache/stats")
async def skip_cache_stats(_=Depends(require_auth)):
    """Returns counts of chart_status values: hit, miss, null (unchecked)."""
    try:
        async with aiosqlite.connect(_DYNAMIC_DB) as db:
            rows = {}
            async with db.execute(
                "SELECT chart_status, COUNT(*) FROM tracks GROUP BY chart_status"
            ) as cur:
                async for row in cur:
                    key = row[0] or "unchecked"
                    rows[key] = row[1]
        return {
            "hit":       rows.get("hit", 0),
            "miss":      rows.get("miss", 0),
            "unchecked": rows.get("unchecked", 0),
            "total":     sum(rows.values()),
        }
    except Exception as e:
        raise HTTPException(500, f"DB error: {e}")


@router.post("/skip_cache/reset")
async def skip_cache_reset(_=Depends(require_auth)):
    """Clears all chart_status values — forces full re-scan on next run."""
    try:
        async with aiosqlite.connect(_DYNAMIC_DB) as db:
            result = await db.execute(
                "UPDATE tracks SET chart_status=NULL, chart_last_checked=NULL"
            )
            count = result.rowcount
            await db.commit()
        log.info(f"Skip cache reset: {count} tracks cleared")
        return {"ok": True, "cleared": count}
    except Exception as e:
        raise HTTPException(500, f"DB error: {e}")


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

    global _vet_import_job
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

    global _vet_import_job
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

    global _vet_import_job
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
    global _vet_import_job

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

    global _vet_import_job

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
    global _vet_import_job

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
    global _vet_import_job

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
    global _vet_import_job
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


async def groomer_startup():
    """Called from main.py lifespan to start the weekly refresh scheduler."""
    asyncio.create_task(_scheduler_loop())
    asyncio.create_task(_library_cache_scheduler())
    asyncio.create_task(_hit_score_startup())
    asyncio.create_task(_lastfm_startup())
    asyncio.create_task(_b200_startup())
    log.info("Groomer scheduler started (auto-refresh every 7 days for current-week sources).")
    log.info("Library cache scheduler started (auto-sync every 24h from physical files).")


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

_FEAT_RE  = re.compile(r"\s+(?:feat\.?|featuring|ft\.?|with|duet with|x)\s+.*$", re.I)
_PAREN_RE = re.compile(r"\s*[\(\[][^\)\]]*[\)\]]")
_SUFFIX_RE = re.compile(r"\s+-\s+.*\b(remaster|remastered|version|mix|edit|live|mono|stereo|single|radio)\b.*$", re.I)


def _artist_key(artist: str) -> str:
    return _norm(_FEAT_RE.sub("", artist or ""))


def _title_key(title: str) -> str:
    t = _SUFFIX_RE.sub("", title or "")
    t = _PAREN_RE.sub("", t)
    return _norm(t) or _norm(title or "")


def _artist_match(want: str, *candidates) -> bool:
    """True if the wanted artist matches any candidate (compilation-safe, feat.-tolerant)."""
    wk = _artist_key(want)
    if not wk:
        return False
    for c in candidates:
        if isinstance(c, (list, tuple)):
            if any(_artist_match(want, x) for x in c):
                return True
            continue
        ck = _artist_key(c or "")
        if ck and (wk == ck or wk in ck or ck in wk):
            return True
    return False


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
    "new wave": "punkrock", "post-punk": "punkrock", "alternative": "alternative",
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
    out = []
    for t in tags:
        k = _LFM_TAG_MAP.get(str(t).strip().lower())
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


# ══════════════════════════════════════════════════════════════════════════════
#  END STATIC DB SOURCES
# ══════════════════════════════════════════════════════════════════════════════


class ScanRequest(BaseModel):
    source:        str            # 'plex' | 'emby' | 'jellyfin' | 'local'
    # charts removed — scan now runs against ALL sources simultaneously.
    # Genre/chart filtering happens at playlist-build time via get_results() filters.
    data_source:   str = "auto"   # kept for backward compat
    use_estimates: bool = False   # if True, use Last.fm for tracks not in any DB source
    write_tags:    bool = False
    limit:         Optional[int] = None
    folder_path:   Optional[str] = None
    library_id:    Optional[str] = None   # media server library ID filter


class StopRequest(BaseModel):
    job_id: Optional[int] = None


# ══════════════════════════════════════════════════════════════════════════════
#  SCAN — START
# ══════════════════════════════════════════════════════════════════════════════

async def _migrate_chart_data():
    """
    Schema migrations — safe no-ops if columns already exist.
    1. chart_data.chart_year    — chart year from static DB
    2. tracks.tag_artist        — needed for Groomer-inserted minimal rows
    3. tracks.chart_status      — skip cache: NULL=never checked, 'hit'=charted, 'miss'=not charted
    4. tracks.chart_last_checked — ISO timestamp of last chart lookup (age-gate stale misses)
    """
    try:
        async with aiosqlite.connect(_DYNAMIC_DB) as db:
            # chart_data migrations
            cd_cols = [row[1] async for row in await db.execute("PRAGMA table_info(chart_data)")]
            if "chart_year" not in cd_cols:
                await db.execute("ALTER TABLE chart_data ADD COLUMN chart_year INTEGER")
                log.info("chart_data: chart_year column added via migration")

            # tracks migrations
            tr_cols = [row[1] async for row in await db.execute("PRAGMA table_info(tracks)")]
            if "tag_artist" not in tr_cols:
                await db.execute("ALTER TABLE tracks ADD COLUMN tag_artist TEXT")
                log.info("tracks: tag_artist column added via migration")
            if "chart_status" not in tr_cols:
                await db.execute("ALTER TABLE tracks ADD COLUMN chart_status TEXT")
                log.info("tracks: chart_status column added via migration")
            if "chart_last_checked" not in tr_cols:
                await db.execute("ALTER TABLE tracks ADD COLUMN chart_last_checked TEXT")
                log.info("tracks: chart_last_checked column added via migration")

            if "missing" not in tr_cols:
                await db.execute("ALTER TABLE tracks ADD COLUMN missing INTEGER DEFAULT 0")
            if "album_title" not in tr_cols:
                await db.execute("ALTER TABLE tracks ADD COLUMN album_title TEXT")
            if "manually_verified" not in tr_cols:
                # Used by the Retriever (hand edits) but never created by the schema
                await db.execute("ALTER TABLE tracks ADD COLUMN manually_verified INTEGER DEFAULT 0")
                log.info("tracks: manually_verified column added via migration")
            if "duration_sec" not in tr_cols:
                await db.execute("ALTER TABLE tracks ADD COLUMN duration_sec INTEGER")
                log.info("tracks: duration_sec column added via migration")

            # M2: Hit Score table — one row per charted track, rebuilt after scans
            await db.execute("""
                CREATE TABLE IF NOT EXISTS hit_scores (
                    track_id        INTEGER PRIMARY KEY,
                    hit_score       INTEGER NOT NULL,
                    hit_year        INTEGER,
                    hot100_peak     INTEGER,
                    hot100_weeks    INTEGER,
                    yearend_rank    INTEGER,
                    one_hit_wonder  INTEGER NOT NULL DEFAULT 0,
                    charts_count    INTEGER NOT NULL DEFAULT 0,
                    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
                )
            """)
            hs_cols = [row[1] async for row in await db.execute("PRAGMA table_info(hit_scores)")]
            for col, typ in (("listeners", "INTEGER"), ("main_genre", "TEXT"),
                             ("genre_keys", "TEXT"), ("never_charted", "INTEGER NOT NULL DEFAULT 0"),
                             ("song_key", "TEXT")):
                if col not in hs_cols:
                    await db.execute(f"ALTER TABLE hit_scores ADD COLUMN {col} {typ}")
            await db.execute("""
                CREATE TABLE IF NOT EXISTS lastfm_tracks (
                    artist_key  TEXT NOT NULL,
                    title_key   TEXT NOT NULL,
                    status      TEXT NOT NULL,          -- ok | notfound | error
                    listeners   INTEGER,
                    playcount   INTEGER,
                    tags        TEXT,                   -- JSON list, most popular first
                    fetched_at  TEXT NOT NULL DEFAULT (datetime('now')),
                    PRIMARY KEY (artist_key, title_key)
                )
            """)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS lastfm_artists (
                    artist_key  TEXT PRIMARY KEY,
                    status      TEXT NOT NULL,
                    tags        TEXT,
                    fetched_at  TEXT NOT NULL DEFAULT (datetime('now'))
                )
            """)
            await db.execute("""CREATE TABLE IF NOT EXISTS track_genres (
                track_id INTEGER PRIMARY KEY, family TEXT NOT NULL, styles TEXT,
                confidence TEXT NOT NULL, sources TEXT,
                updated_at TEXT NOT NULL DEFAULT (datetime('now')))""")
            await db.execute("CREATE INDEX IF NOT EXISTS idx_hs_score ON hit_scores(hit_score)")
            await db.execute("CREATE INDEX IF NOT EXISTS idx_hs_year  ON hit_scores(hit_year)")

            # library_cache tables — ownership index used by Sniffer + Bloodhound
            await db.execute("""
                CREATE TABLE IF NOT EXISTS library_cache (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    artist_norm TEXT NOT NULL,
                    title_norm  TEXT NOT NULL,
                    track_id    TEXT,
                    source      TEXT,
                    UNIQUE(artist_norm, title_norm)
                )
            """)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS library_cache_meta (
                    key   TEXT PRIMARY KEY,
                    value TEXT
                )
            """)
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_lc_artist ON library_cache(artist_norm)"
            )

            await db.commit()
    except Exception as e:
        log.warning(f"migration warning: {e}")


@router.post("/scan/start")
async def scan_start(req: ScanRequest, _=Depends(require_auth)):
    if _scan_job["status"] == "running":
        raise HTTPException(409, "A scan is already running. Stop it first.")

    # Migrate schema if needed (safe no-op if column already exists)
    await _migrate_chart_data()

    # M1: results are NO LONGER wiped here. Each track's rows are replaced
    # individually when it is re-checked, so a failed fetch loses nothing.

    _scan_job.update({
        "status": "starting", "message": "Initialising scan...",
        "total": 0, "processed": 0, "matched": 0, "failed": 0, "cached": 0,
        "started_at": time.time(), "stop_requested": False, "job_id": int(time.time()),
    })

    # CRITICAL: asyncio.create_task instead of bg.add_task.
    # bg.add_task (FastAPI BackgroundTasks) does not reliably yield control
    # back to the event loop during heavy I/O on this NAS — it freezes
    # uvicorn and blocks all poll requests. create_task runs as a proper
    # concurrent coroutine on the event loop.
    asyncio.create_task(_run_scan(req))
    return {"ok": True, "job_id": _scan_job["job_id"]}


@router.get("/scan/status/{job_id}")
async def scan_status(job_id: int, _=Depends(require_auth)):
    """
    Return current scan state. If the polled job_id doesn't match the in-memory
    job_id, the client is polling a dead scan (e.g. after container restart or
    a new scan started in another tab) — return status='stale' so the frontend
    can reset its UI instead of polling forever.
    """
    current_id = _scan_job.get("job_id")
    if current_id is None or current_id != job_id:
        return {
            "status": "stale",
            "message": "This scan is no longer active (server restart or new scan started).",
            "job_id": job_id,
            "total": 0, "processed": 0, "matched": 0, "failed": 0, "cached": 0,
        }
    return dict(_scan_job)


@router.post("/scan/stop")
async def scan_stop(_=Depends(require_auth)):
    _scan_job["stop_requested"] = True
    _scan_job["message"] = "Stop requested — finishing current track..."
    return {"ok": True}


# ══════════════════════════════════════════════════════════════════════════════
#  SCAN — BACKGROUND WORKER
# ══════════════════════════════════════════════════════════════════════════════

async def _get_recheck_cutoffs(db) -> tuple:
    """
    Returns (version_cutoff, data_cutoff) as UTC epochs.
      version_cutoff — when matching logic last changed (_LOOKUP_VERSION bump).
                       Hits checked before this are re-checked.
      data_cutoff    — max(version_cutoff, newest chart import). Misses checked
                       before this are re-checked (new data may now match them).
    """
    vals = {}
    async with db.execute(
        "SELECT key, value FROM app_settings "
        "WHERE key IN ('groomer_lookup_version','groomer_recheck_after')"
    ) as cur:
        for k, v in await cur.fetchall():
            vals[k] = v
    if vals.get("groomer_lookup_version") != _LOOKUP_VERSION:
        now_s = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
        await db.executemany(
            "INSERT INTO app_settings (key, value, updated_at) VALUES (?, ?, datetime('now')) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            [("groomer_lookup_version", _LOOKUP_VERSION), ("groomer_recheck_after", now_s)],
        )
        await db.commit()
        vals["groomer_recheck_after"] = now_s
        log.info(f"Lookup version → {_LOOKUP_VERSION}: all tracks will be re-checked once")
    latest_import = None
    try:
        async with db.execute("SELECT MAX(added_at) FROM chart_reference_extras") as cur:
            row = await cur.fetchone()
            latest_import = row[0] if row else None
    except Exception:
        pass
    version_cutoff = _utc_epoch(vals.get("groomer_recheck_after"))
    return version_cutoff, max(version_cutoff, _utc_epoch(latest_import))


async def _run_scan(req: ScanRequest):
    # Early log — ALWAYS hits before any heavy I/O so we have proof the
    # background task started, even if something catastrophic happens next.
    log.info(
        f"Scan task entered — source={req.source} "
        f"folder={req.folder_path or '(n/a)'} library={req.library_id or '(all)'} "
        f"job_id={_scan_job.get('job_id')}"
    )
    _scan_job["status"] = "running"
    req.use_estimates = False   # M5: retired — replaced by the Online Data (Last.fm) pass
    try:
        # Get Last.fm key for fallback
        lfm_key = ""
        try:
            async with aiosqlite.connect(_DYNAMIC_DB) as db:
                db.row_factory = aiosqlite.Row
                async with db.execute(
                    "SELECT token_enc FROM connections WHERE service='lastfm'"
                ) as cur:
                    row = await cur.fetchone()
                    if row and row["token_enc"]:
                        lfm_key = decrypt_token(row["token_enc"]) or ""
        except Exception:
            pass

        # Pull track list
        try:
            tracks = await _fetch_tracks(req)
        except Exception as fe:
            log.error(f"Groomer library fetch failed: {fe}")
            _scan_job.update({"status": "failed",
                              "message": f"{fe} Previous results kept."})
            return
        if _scan_job["stop_requested"]:
            _scan_job.update({"status": "stopped", "message": "Stopped before chart lookups began."})
            return
        if not tracks:
            where = (req.folder_path or "/music") if req.source == "local" else f"{req.source} library"
            _scan_job.update({"status": "failed",
                              "message": f"No tracks returned from {where}. Previous results kept."})
            return

        # Dedupe — 'All Libraries' can return the same file from several libraries
        _seen, _uniq = set(), []
        for t in tracks:
            fp = (t.get("file_path") or "").strip()
            key = fp or (_norm(t.get("artist", "")), _norm(t.get("title", "")), _norm(t.get("album", "")))
            if key in _seen:
                continue
            _seen.add(key)
            _uniq.append(t)
        dupes = len(tracks) - len(_uniq)
        tracks = _uniq
        if dupes:
            log.info(f"Dedupe: {dupes:,} duplicate tracks removed ({len(tracks):,} unique)")

        limit = req.limit or len(tracks)
        tracks = tracks[:limit]
        _scan_job["total"] = len(tracks)
        _scan_job["message"] = f"Scanning {len(tracks):,} tracks against all chart sources..."

        # Scan-everything: no chart filter at scan time.
        # _lookup_static queries chart_reference + extras with no chart restriction.
        # LBZ lookup is routed by Retriever genre tags on the track.
        # Last.fm fires for any unmatched track when use_estimates=True.
        log.info(f"Scan-everything mode: all chart sources active, genre routing via DB tags")

        loop = asyncio.get_event_loop()

        async with aiosqlite.connect(_DYNAMIC_DB) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("PRAGMA journal_mode=WAL")

            # ── SKIP CACHE AGE GATE ──────────────────────────────────────
            _MISS_AGE_SECONDS = 6 * 30 * 24 * 3600   # ~6 months
            skipped_miss = 0
            comment_readback_hits = 0

            # ── PRE-FETCH PATH PREFIXES (once, not per-track) ────────────
            _scan_server_prefix = ""
            _scan_docker_prefix = "/music"
            try:
                async with db.execute(
                    "SELECT key, value FROM app_settings "
                    "WHERE key IN ('path_server_prefix','path_docker_prefix')"
                ) as cur:
                    prows = await cur.fetchall()
                for r in prows:
                    if r[0] == "path_server_prefix":   _scan_server_prefix = r[1] or ""
                    elif r[0] == "path_docker_prefix":  _scan_docker_prefix = r[1] or "/music"
            except Exception:
                pass

            def _to_docker_path(fp: str) -> str:
                if not fp:
                    return ""
                if _scan_server_prefix and fp.startswith(_scan_server_prefix):
                    return _scan_docker_prefix + fp[len(_scan_server_prefix):]
                if fp.startswith(_scan_docker_prefix) or fp.startswith("/music"):
                    return fp
                return ""

            version_cutoff, data_cutoff = await _get_recheck_cutoffs(db)
            static_shared = await loop.run_in_executor(None, _open_static_conn)
            if static_shared[0] is None:
                static_shared = None

            try:
              for idx, track in enumerate(tracks):
                if _scan_job["stop_requested"]:
                    await db.commit()
                    _scan_job["message"] = "Stopped — calculating Hit Scores…"
                    await _rebuild_hit_scores()
                    _scan_job["status"] = "stopped"
                    _scan_job["message"] = f"Stopped at track {idx + 1} of {len(tracks)}"
                    return
                if idx and idx % 200 == 0:
                    await db.commit()   # persist miss/hit status in batches

                artist = track.get("artist", "").strip()
                title  = track.get("title",  "").strip()
                if not artist or not title:
                    _scan_job["processed"] += 1
                    continue

                _scan_job["message"] = f"[{idx+1}/{len(tracks)}] {artist} — {title}"

                # Resolve track_id
                track_id = await _resolve_track_id(db, track)
                if track_id:
                    track["track_id"] = track_id
                    # Keep artist (compilation fix) + duration current, even for skipped tracks
                    try:
                        await db.execute(
                            "UPDATE tracks SET tag_artist=COALESCE(NULLIF(?, ''), tag_artist), "
                            "duration_sec=COALESCE(?, duration_sec), "
                            "plex_rating_key=COALESCE(NULLIF(?, ''), plex_rating_key), "
                            "emby_id=COALESCE(NULLIF(?, ''), emby_id), "
                            "jf_id=COALESCE(NULLIF(?, ''), jf_id), "
                            "year=COALESCE(year, ?), "
                            "album_title=COALESCE(NULLIF(?, ''), album_title), missing=0 WHERE track_id=?",
                            (track.get("tag_artist") or "", track.get("duration_sec"),
                             track.get("plex_rating_key") or "", track.get("emby_id") or "",
                             track.get("jf_id") or "", track.get("year"),
                             track.get("album") or "", track_id))
                    except Exception:
                        pass

                # ── SKIP CACHE (incremental) ──────────────────────────────────
                # hit  → skip if checked after last lookup-logic change and rows exist
                # miss → skip if checked after newest chart import and < 6 months old
                if track_id:
                    cs_row = None
                    try:
                        async with db.execute(
                            "SELECT chart_status, chart_last_checked FROM tracks WHERE track_id=?",
                            (track_id,)
                        ) as cur:
                            cs_row = await cur.fetchone()
                    except Exception:
                        pass
                    if cs_row and cs_row["chart_status"] in ("hit", "miss"):
                        chk  = _utc_epoch(cs_row["chart_last_checked"])
                        skip = False
                        if cs_row["chart_status"] == "hit" and chk >= version_cutoff:
                            async with db.execute(
                                "SELECT 1 FROM chart_data WHERE track_id=? LIMIT 1", (track_id,)
                            ) as cur:
                                skip = await cur.fetchone() is not None
                        elif (cs_row["chart_status"] == "miss" and chk >= data_cutoff
                              and (time.time() - chk) < _MISS_AGE_SECONDS):
                            skip = True
                            skipped_miss += 1
                        if skip:
                            _scan_job["cached"]    += 1
                            _scan_job["processed"] += 1
                            continue
                    # Re-checking → replace this track's old rows (never accumulate)
                    await db.execute("DELETE FROM chart_data WHERE track_id=?", (track_id,))

                # ── PULL RETRIEVER GENRE TAGS FROM DB ────────────────────────
                if track_id and not track.get("genre_1"):
                    try:
                        async with db.execute(
                            "SELECT genre_1, genre_2, genre_3 FROM tracks WHERE track_id=?",
                            (track_id,)
                        ) as cur:
                            gr = await cur.fetchone()
                        if gr:
                            track["genre_1"] = gr[0] or ""
                            track["genre_2"] = gr[1] or ""
                            track["genre_3"] = gr[2] or ""
                    except Exception:
                        pass

                # ── WATERFALL ────────────────────────────────────────────────
                # Step 1: Static DB + extras — ALL charts, no restriction
                static_result = await loop.run_in_executor(
                    None, _lookup_static, artist, title, [], static_shared
                )

                if static_result:
                    await _store_result(db, track, static_result, req)
                    await _enrich_genre_from_file(db, track, loop)
                    _scan_job["matched"] += 1
                    if track_id:
                        try:
                            await db.execute(
                                "UPDATE tracks SET chart_status='hit', "
                                "chart_last_checked=datetime('now') WHERE track_id=?",
                                (track_id,)
                            )
                        except Exception:
                            pass

                else:
                    # Step 1b: LBZ extras — routed by Retriever genre tags
                    # Use genre_1/2/3 from DB to pick which chart_names to query.
                    # Falls back to all LBZ chart names if no genre tag available.
                    genre_tags_db = [
                        track.get("genre_1", ""),
                        track.get("genre_2", ""),
                        track.get("genre_3", ""),
                    ]
                    routed_charts = _genre_tags_to_chart_names(genre_tags_db)
                    # If no genre tags, try all LBZ charts (catch untagged library)
                    lbz_query_charts = routed_charts if routed_charts else list(_LBZ_CHART_NAMES)
                    lbz_result = await _lookup_lbz_extras(artist, title, lbz_query_charts)

                    if lbz_result:
                        await _store_result(db, track, lbz_result, req)
                        await _enrich_genre_from_file(db, track, loop)
                        _scan_job["matched"] += 1
                        if track_id:
                            try:
                                await db.execute(
                                    "UPDATE tracks SET chart_status='hit', "
                                    "chart_last_checked=datetime('now') WHERE track_id=?",
                                    (track_id,)
                                )
                            except Exception:
                                pass
                    else:
                        # Step 2: Comment tag read-back
                        comment_result = None
                        file_path = track.get("file_path", "")
                        docker_fp = _to_docker_path(file_path)
                        if docker_fp:
                            raw_comment = await loop.run_in_executor(
                                None, _read_comment_from_file, docker_fp
                            )
                            if raw_comment:
                                parsed_entries = _parse_comment_tag(raw_comment)
                                if parsed_entries:
                                    best = min(parsed_entries, key=lambda e: e["peak_position"])
                                    best["all_charts"] = parsed_entries
                                    best["star_rating"] = peak_to_stars(best["peak_position"])
                                    comment_result = best
                                    comment_readback_hits += 1
                                    log.debug(f"Comment read-back: {artist} — {title}")

                        if comment_result:
                            await _store_result(db, track, comment_result, req)
                            await _enrich_genre_from_file(db, track, loop)
                            _scan_job["matched"] += 1
                            if track_id:
                                try:
                                    await db.execute(
                                        "UPDATE tracks SET chart_status='hit', "
                                        "chart_last_checked=datetime('now') WHERE track_id=?",
                                        (track_id,)
                                    )
                                except Exception:
                                    pass
                        else:
                            # Step 3: Last.fm popularity estimate (opt-in)
                            # TARGETED: only fires when ALL of these are true:
                            #   1. use_estimates=True (user opted in)
                            #   2. Track has a genre tag (DB or file) — no genre = skip
                            #   3. Genre routes to a LBZ gap chart (hiphop/metal/alt/indie/
                            #      folk/jazz/blues/electronic) — broad charts have full
                            #      static DB coverage so Last.fm adds nothing there
                            matched_via_lfm = False
                            if req.use_estimates and lfm_key:

                                # Get genre tags — prefer DB (Retriever), fall back to file
                                genre_tags = [
                                    track.get("genre_1", ""),
                                    track.get("genre_2", ""),
                                    track.get("genre_3", ""),
                                ]
                                if not any(genre_tags) and docker_fp:
                                    def _quick_genre(fp):
                                        try:
                                            from mutagen import File as MutagenFile
                                            mf = MutagenFile(fp, easy=True)
                                            if mf:
                                                raw = mf.get("genre", [])
                                                return [str(g) for g in raw[:3]]
                                        except Exception:
                                            pass
                                        return []
                                    genre_tags = await loop.run_in_executor(
                                        None, _quick_genre, docker_fp
                                    )

                                # Route genre → chart_name
                                routed = _genre_tags_to_chart_names(genre_tags)
                                chart_for_estimate = routed[0] if routed else None

                                # GATE 1: must have a genre tag — no genre = no estimate
                                # GATE 2: routed chart must be a LBZ gap genre
                                #         (hot100/country/rock/rnb/dance/adultpop/ac/uk
                                #          all have full static DB — Last.fm adds nothing)
                                _LFM_ELIGIBLE_CHARTS = _LBZ_CHART_NAMES | _CCM_CHARTS
                                lfm_eligible = (
                                    any(genre_tags) and
                                    chart_for_estimate is not None and
                                    chart_for_estimate in _LFM_ELIGIBLE_CHARTS
                                )

                                if lfm_eligible:
                                    await asyncio.sleep(0.2)  # max 5 req/sec
                                    listeners = await _lastfm_listeners(artist, title, lfm_key)
                                    bucket = _detect_genre_bucket(genre_tags, [chart_for_estimate])

                                    # Strict gate for CCM/gospel
                                    if chart_for_estimate in _STRICT_GENRE_CHARTS:
                                        genre_ok = _genre_matches_chart(genre_tags, chart_for_estimate)
                                    else:
                                        genre_ok = True

                                    if genre_ok and _meets_min_threshold(listeners, bucket):
                                        est_peak = _listeners_to_est_peak(listeners, bucket)
                                        stars    = _listeners_to_stars(listeners, bucket)
                                        result = {
                                            "peak_position":  est_peak,
                                            "weeks_on_chart": 1,
                                            "chart_name":     chart_for_estimate,
                                            "chart_year":     None,
                                            "confidence":     "low",
                                            "data_source":    "lastfm_estimate",
                                            "all_charts":     [],
                                            "star_rating":    stars,
                                            "listener_count": listeners,
                                        }
                                        await _store_result(db, track, result, req)
                                        await _enrich_genre_from_file(db, track, loop)
                                        _scan_job["matched"] += 1
                                        matched_via_lfm = True
                                    else:
                                        _scan_job["failed"] += 1
                                else:
                                    # Not eligible for Last.fm — skip silently
                                    _scan_job["failed"] += 1
                            else:
                                _scan_job["failed"] += 1

                            if track_id:
                                try:
                                    status = "hit" if matched_via_lfm else "miss"
                                    await db.execute(
                                        "UPDATE tracks SET chart_status=?, "
                                        "chart_last_checked=datetime('now') WHERE track_id=?",
                                        (status, track_id)
                                    )
                                except Exception:
                                    pass

                _scan_job["processed"] += 1
                await asyncio.sleep(0)
            finally:
                await db.commit()
                if static_shared:
                    try:
                        static_shared[0].close()
                    except Exception:
                        pass

        _scan_job["message"] = "Calculating Hit Scores…"
        await _rebuild_hit_scores()
        _maybe_start_lastfm()     # look up any new songs in the background
        elapsed = time.time() - (_scan_job["started_at"] or time.time())
        _scan_job.update({
            "status":  "done",
            "message": (f"Scan complete — {_scan_job['matched']:,} matched, "
                        f"{_scan_job['cached']:,} cached "
                        f"({skipped_miss:,} skip-cached misses, "
                        f"{comment_readback_hits:,} comment read-backs), "
                        f"{_scan_job['failed']:,} unmatched"
                        f"{f', {dupes:,} duplicates skipped' if dupes else ''} "
                        f"in {elapsed:.0f}s"),
        })
        log.info(f"Skip cache stats: {skipped_miss:,} misses skipped, "
                 f"{comment_readback_hits:,} comment read-backs")

    except Exception as e:
        log.exception("Groomer scan error")
        _scan_job.update({"status": "failed", "message": f"Scan error: {e}"})


async def _enrich_genre_from_file(db: aiosqlite.Connection, track: dict, loop) -> None:
    """
    For a matched track, read genre tags from the physical file via Mutagen
    and update the tracks table. Only fires on matched tracks (~800 files),
    not on all 33k scanned tracks.
    Handles server path → docker path translation using app_settings.
    """
    track_id = track.get("track_id")
    if not track_id:
        return

    file_path = track.get("file_path", "")
    if not file_path:
        return

    # Path translation: server path → docker path
    try:
        async with db.execute(
            "SELECT key, value FROM app_settings "
            "WHERE key IN ('path_server_prefix','path_docker_prefix')"
        ) as cur:
            rows = await cur.fetchall()
        server_prefix = ""
        docker_prefix = "/music"
        for r in rows:
            if r[0] == "path_server_prefix":   server_prefix = r[1] or ""
            elif r[0] == "path_docker_prefix":  docker_prefix = r[1] or "/music"
    except Exception:
        server_prefix = ""
        docker_prefix = "/music"

    docker_path = file_path
    if server_prefix and file_path.startswith(server_prefix):
        docker_path = docker_prefix + file_path[len(server_prefix):]
    elif not file_path.startswith(docker_prefix) and not file_path.startswith("/music"):
        return  # can't translate — skip

    if not os.path.exists(docker_path):
        return

    def _read_genre(path: str):
        try:
            from mutagen import File as MutagenFile
            mf = MutagenFile(path, easy=True)
            if not mf:
                return []
            raw = mf.get("genre", [])
            if not raw:
                return []
            # Split on semicolons, slashes, or newlines — common separators
            import re as _re
            genres = []
            for g in raw:
                parts = _re.split('[;/\n]', str(g))
                genres.extend(p.strip() for p in parts if p.strip())
            return genres[:3]  # max 3
        except Exception:
            return []

    genres = await loop.run_in_executor(None, _read_genre, docker_path)
    if not genres:
        return

    try:
        g1 = genres[0] if len(genres) > 0 else None
        g2 = genres[1] if len(genres) > 1 else None
        g3 = genres[2] if len(genres) > 2 else None
        await db.execute(
            "UPDATE tracks SET genre_1=?, genre_2=?, genre_3=? WHERE track_id=?",
            (g1, g2, g3, track_id)
        )
        await db.commit()
    except Exception as e:
        log.debug(f"_enrich_genre_from_file update failed: {e}")


async def _resolve_track_id(db: aiosqlite.Connection, track: dict) -> Optional[int]:
    """
    Returns a valid track_id for chart_data writes.
    Priority:
      1. track dict already has track_id (Retriever-indexed tracks)
      2. Lookup by file_path in tracks table (unique index — fast)
      3. Lookup by tag_artist + title (fuzzy fallback)
      4. Insert minimal tracks row and return new ID
    """
    # Already resolved
    if track.get("track_id"):
        return track["track_id"]

    file_path  = track.get("file_path", "").strip()
    artist     = (track.get("tag_artist") or track.get("artist", "")).strip()
    title      = track.get("title", "").strip()

    # ── 1. Lookup by file_path ───────────────────────────────────────────────
    if file_path:
        async with db.execute(
            "SELECT track_id FROM tracks WHERE file_path = ?", (file_path,)
        ) as cur:
            row = await cur.fetchone()
            if row:
                return row[0]

    # ── 2. Lookup by artist + title ──────────────────────────────────────────
    if artist and title:
        async with db.execute("""
            SELECT t.track_id FROM tracks t
            LEFT JOIN artists a ON t.artist_id = a.artist_id
            WHERE LOWER(t.title) = LOWER(?)
              AND LOWER(COALESCE(t.tag_artist, a.name, '')) = LOWER(?)
            LIMIT 1
        """, (title, artist)) as cur:
            row = await cur.fetchone()
            if row:
                return row[0]

    # ── 3. Insert minimal row so chart_data can reference it ─────────────────
    # Upsert by file_path to avoid duplicates if path already exists
    if not file_path and not title:
        return None  # nothing to anchor on — skip

    # Always use the real file path as anchor — fake paths break M3U generation
    if not file_path:
        log.debug(f"_resolve_track_id: no file_path for '{title}' — skipping insert")
        return None
    anchor_path = file_path

    try:
        # Resolve or create artist row
        artist_id = None
        if artist:
            async with db.execute(
                "SELECT artist_id FROM artists WHERE name = ?", (artist,)
            ) as cur:
                arow = await cur.fetchone()
            if arow:
                artist_id = arow[0]
            else:
                cur2 = await db.execute(
                    "INSERT OR IGNORE INTO artists (name) VALUES (?)", (artist,)
                )
                artist_id = cur2.lastrowid or None

        cur3 = await db.execute("""
            INSERT INTO tracks (file_path, title, tag_artist, artist_id,
                                plex_rating_key, emby_id, jf_id,
                                last_scanned)
            VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now'))
            ON CONFLICT(file_path) DO UPDATE SET
                tag_artist      = excluded.tag_artist,
                plex_rating_key = COALESCE(excluded.plex_rating_key, tracks.plex_rating_key),
                emby_id         = COALESCE(excluded.emby_id,         tracks.emby_id),
                jf_id           = COALESCE(excluded.jf_id,           tracks.jf_id)
        """, (
            anchor_path,
            title,
            artist,
            artist_id,
            track.get("plex_rating_key") or None,
            track.get("emby_id")         or None,
            track.get("jf_id")           or None,
        ))
        await db.commit()

        # Fetch the ID (lastrowid is 0 on ON CONFLICT UPDATE — re-query)
        async with db.execute(
            "SELECT track_id FROM tracks WHERE file_path = ?", (anchor_path,)
        ) as cur:
            row = await cur.fetchone()
            return row[0] if row else None

    except Exception as e:
        log.warning(f"_resolve_track_id insert failed for '{title}': {e}")
        return None


def _get_charts_with_static_data(charts: list) -> set:
    """
    Returns the subset of requested charts that have entries in charthound_static.db.
    Called once at scan start — not per track.
    Result used to decide whether Last.fm fallback is appropriate.
    """
    if not charts or not os.path.exists(_STATIC_DB):
        return set()
    try:
        conn = sqlite3.connect(_STATIC_DB, check_same_thread=False)
        placeholders = ",".join("?" * len(charts))
        rows = conn.execute(
            f"SELECT DISTINCT chart_name FROM chart_reference WHERE chart_name IN ({placeholders})",
            charts
        ).fetchall()
        conn.close()
        return {r[0] for r in rows}
    except Exception as e:
        log.warning(f"_get_charts_with_static_data error: {e}")
        return set()


async def _check_cache(db: aiosqlite.Connection, track: dict, charts: list) -> bool:
    """Returns True if this track already has any chart_data rows (scan-everything mode)."""
    track_id = track.get("track_id")
    if not track_id:
        return False
    if charts:
        placeholders = ",".join("?" * len(charts))
        params = [track_id] + charts
        async with db.execute(
            f"SELECT COUNT(*) FROM chart_data WHERE track_id=? AND chart_name IN ({placeholders})",
            params
        ) as cur:
            row = await cur.fetchone()
    else:
        # Scan-everything: any existing chart_data row = cached
        async with db.execute(
            "SELECT COUNT(*) FROM chart_data WHERE track_id=?", (track_id,)
        ) as cur:
            row = await cur.fetchone()
    return bool(row and row[0] > 0)


def write_chart_tags(file_path: str, comment_string: str, star_rating: int) -> None:
    """
    Groomer-only tag writer. Touches ONLY two fields — leaves all other tags untouched.
      - COMMENT / COMM  — formatted chart string e.g. "Hot 100: #4 (22 wks) | Adult Pop: #1"
      - RATING / POPM   — star rating 1–5 (identifier: 'ChartHound')

    MP3  → ID3 COMM frame + POPM frame (0–255 scaled: 1★=51, 2★=102, 3★=153, 4★=204, 5★=255)
    FLAC → Vorbis COMMENT tag + RATING tag (stored as plain integer string '1'–'5')
    M4A  → ©cmt atom + rated via freeform ----:com.ChartHound:RATING atom
    """
    import mutagen
    from mutagen import File as MutagenFile
    from mutagen.flac import FLAC
    from mutagen.mp3 import MP3
    from mutagen.id3 import ID3, COMM, POPM
    from mutagen.mp4 import MP4

    if not file_path or not os.path.exists(file_path):
        raise FileNotFoundError(f"File not found: {file_path}")

    ext = os.path.splitext(file_path)[1].lower()

    # Star → POPM byte scale (ID3 spec: 0–255)
    popm_scale = {1: 51, 2: 102, 3: 153, 4: 204, 5: 255}
    popm_val   = popm_scale.get(max(1, min(5, star_rating)), 153)

    if ext == ".mp3":
        try:
            tags = ID3(file_path)
        except mutagen.id3.ID3NoHeaderError:
            tags = ID3()

        # COMMENT frame — lang='eng', desc='' keeps it compatible with most players
        tags.delall("COMM:ChartHound:eng")
        tags.add(COMM(encoding=3, lang="eng", desc="ChartHound", text=comment_string))

        # POPM frame — identifier 'ChartHound'
        tags.delall("POPM:ChartHound")
        tags.add(POPM(email="ChartHound", rating=popm_val, count=0))

        tags.save(file_path)

    elif ext == ".flac":
        audio = FLAC(file_path)
        audio["COMMENT"] = comment_string
        audio["RATING"]  = str(star_rating)
        audio.save()

    elif ext in (".m4a", ".mp4", ".aac"):
        audio = MP4(file_path)
        audio.tags["©cmt"] = [comment_string]
        # Store rating as freeform atom
        from mutagen.mp4 import MP4FreeForm
        audio.tags["----:com.ChartHound:RATING"] = [
            MP4FreeForm(str(star_rating).encode("utf-8"))
        ]
        audio.save()

    else:
        # Generic fallback via easy=False Mutagen
        mf = MutagenFile(file_path, easy=False)
        if mf is not None and mf.tags is not None:
            mf.tags["COMMENT"] = comment_string
            mf.save()


async def _store_result(db: aiosqlite.Connection, track: dict, result: dict, req: ScanRequest):
    """Upserts chart_data row and optionally writes COMMENT + RATING tags to file."""
    track_id = track.get("track_id")
    if not track_id:
        return

    peak      = result["peak_position"]
    weeks     = result["weeks_on_chart"]
    chart     = result["chart_name"]
    conf      = result["confidence"]
    stars     = result.get("star_rating") or peak_to_stars(peak)
    listeners = result.get("listener_count", 0)

    # Build all_charts list for multi-chart COMMENT tag
    all_charts = result.get("all_charts") or [result]

    comment_parts = []
    for c in all_charts:
        cname = CHART_DISPLAY.get(c.get("chart_name", chart), c.get("chart_name", chart))
        cpeak = c.get("peak_position", peak)
        cwks  = c.get("weeks_on_chart", weeks)
        if conf == "low" and cpeak:
            comment_parts.append(f"{cname}: ~#{cpeak} ({cwks} wks)")
        elif cpeak:
            comment_parts.append(f"{cname}: #{cpeak} ({cwks} wks)")

    comment_string = " | ".join(comment_parts) if comment_parts else ""
    if conf == "low" and not comment_string:
        chart_display = CHART_DISPLAY.get(chart, chart)
        comment_string = f"{chart_display}: ~#{peak} ({weeks} wks)" if peak else f"{chart_display}: ★★★"

    chart_year = result.get("chart_year")

    await db.execute("""
        INSERT INTO chart_data
            (track_id, chart_name, peak_position, weeks_on_chart,
             star_rating, confidence, listener_count, comment_string,
             chart_year, fetched_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
        ON CONFLICT(track_id, chart_name) DO UPDATE SET
            peak_position  = excluded.peak_position,
            weeks_on_chart = excluded.weeks_on_chart,
            star_rating    = excluded.star_rating,
            confidence     = excluded.confidence,
            listener_count = excluded.listener_count,
            comment_string = excluded.comment_string,
            chart_year     = excluded.chart_year,
            fetched_at     = excluded.fetched_at
    """, (track_id, chart, peak, weeks, stars, conf, listeners, comment_string, chart_year))
    await db.commit()

    # Write COMMENT + RATING tags to physical file
    if req.write_tags:
        file_path = track.get("file_path", "")
        if file_path and os.path.exists(file_path):
            loop = asyncio.get_event_loop()
            try:
                await loop.run_in_executor(
                    None, write_chart_tags, file_path, comment_string, stars
                )
                log.info(f"Chart tags written: {os.path.basename(file_path)} | {comment_string} | ★{stars}")
            except Exception as e:
                log.warning(f"Tag write failed for {file_path}: {e}")


# ══════════════════════════════════════════════════════════════════════════════
#  TRACK FETCHERS
# ══════════════════════════════════════════════════════════════════════════════

async def _fetch_tracks(req: ScanRequest) -> list:
    if req.source == "local":
        return await _fetch_local(req.folder_path or "/music")
    return await _fetch_media_server(req)


async def _fetch_local(folder: str) -> list:
    """
    Walk a local folder tree and collect audio tracks with basic tags.

    Uses a DEDICATED ThreadPoolExecutor (max 2 threads) so Mutagen reads
    cannot starve uvicorn's default executor. Batches of 50 with a real
    asyncio.sleep between each to guarantee the event loop stays responsive
    for /scan/status polls even during a 33k-track NAS walk.
    """
    from concurrent.futures import ThreadPoolExecutor
    from mutagen import File as MutagenFile

    log.info(f"Local scan: enumerating files under {folder}")
    _scan_job["message"] = f"Enumerating files in {folder}..."

    if not os.path.isdir(folder):
        log.warning(f"Local scan: folder does not exist or is not a directory: {folder}")
        _scan_job["message"] = f"Folder not found: {folder}"
        return []

    EXTS = {".flac", ".mp3", ".m4a", ".ogg", ".wav", ".aiff"}
    loop = asyncio.get_event_loop()

    # ── Step 1: fast enumeration (default executor, no file reads) ───────────
    def _collect_paths() -> list:
        paths = []
        for root, _, files in os.walk(folder):
            for f in files:
                if os.path.splitext(f)[1].lower() in EXTS:
                    paths.append(os.path.join(root, f))
        return paths

    try:
        file_paths = await loop.run_in_executor(None, _collect_paths)
    except Exception as e:
        log.exception(f"Local scan: walk failed: {e}")
        _scan_job["message"] = f"Walk failed: {e}"
        return []

    log.info(f"Local scan: found {len(file_paths):,} audio files — now reading tags")
    _scan_job["message"] = f"Found {len(file_paths):,} files — reading tags..."
    _scan_job["total"] = len(file_paths)

    # ── Step 2: read tags in DEDICATED executor, small batches ───────────────
    # Dedicated pool (2 threads) so we never starve uvicorn's default executor.
    # Batch size 50 + real sleep(0.05) between batches = event loop stays alive.
    tag_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="tag_read")

    def _read_one(fp: str) -> Optional[dict]:
        try:
            mf = MutagenFile(fp, easy=True)
            if not mf:
                return None
            return {
                "file_path": fp,
                "artist":    str(mf.get("artist", [""])[0]),
                "title":     str(mf.get("title",  [""])[0]),
                "album":     str(mf.get("album",  [""])[0]),
                "track_id":  None,
                "duration_sec": int(mf.info.length) if getattr(mf, "info", None) and mf.info.length else None,
                "year": (lambda d: int(d[:4]) if d[:4].isdigit() else None)(str((mf.get("date") or [""])[0])),
            }
        except Exception:
            return None

    def _read_batch(paths: list) -> list:
        return [_read_one(fp) for fp in paths]

    tracks: list = []
    BATCH = 50
    total_files = len(file_paths)

    try:
        for i in range(0, total_files, BATCH):
            if _scan_job["stop_requested"]:
                log.info(f"Local scan: stop requested at tag-read {i}/{total_files}")
                _scan_job["message"] = f"Stopped during tag read ({i}/{total_files})"
                return tracks

            batch = file_paths[i:i+BATCH]
            results = await loop.run_in_executor(tag_executor, _read_batch, batch)
            tracks.extend([r for r in results if r is not None])

            # Update message so UI shows progress during tag-read phase
            done = i + len(batch)
            _scan_job["message"] = f"Reading tags: {done:,}/{total_files:,}"

            # Periodic log every 5,000 files so debug log shows progress
            if done % 5000 < BATCH:
                log.info(f"Local scan: tag-read progress {done:,}/{total_files:,} ({len(tracks):,} valid)")

            # Real yield — NOT sleep(0). Gives uvicorn time to serve polls.
            await asyncio.sleep(0.05)
    finally:
        tag_executor.shutdown(wait=False)

    log.info(f"Local scan: collected {len(tracks):,} taggable tracks from {total_files:,} files")
    return tracks


async def _fetch_media_server(req: ScanRequest) -> list:
    """Pull track list from Plex, Emby, or Jellyfin."""
    try:
        async with aiosqlite.connect(_DYNAMIC_DB) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT base_url, token_enc, extra_json FROM connections WHERE service=?",
                (req.source,)
            ) as cur:
                conn = await cur.fetchone()
    except Exception:
        conn = None

    if not conn:
        raise RuntimeError(f"No {req.source} connection configured in The Kennel.")

    base_url   = conn["base_url"]
    token      = decrypt_token(conn["token_enc"]) if conn["token_enc"] else ""
    extra      = json.loads(conn["extra_json"] or "{}") if conn["extra_json"] else {}
    library_id = req.library_id or None

    if req.source == "plex":
        return await _fetch_plex_tracks(base_url, token, library_id=library_id)
    elif req.source == "emby":
        return await _fetch_emby_tracks(base_url, token, extra.get("user_id",""), library_id=library_id)
    else:
        return await _fetch_jellyfin_tracks(base_url, token, extra.get("user_id",""), library_id=library_id)


async def _fetch_plex_tracks(base: str, token: str, library_id: str = None) -> list:
    """Raises RuntimeError with a readable reason on any failure (no silent empty list)."""
    tracks = []
    prefix_server = os.environ.get("MEDIA_SERVER_MUSIC_PREFIX", "")
    prefix_docker = os.environ.get("DOCKER_MUSIC_PREFIX", "/music")
    hdr = {"Accept": "application/json"}
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            r = await client.get(f"{base}/library/sections",
                                 params={"X-Plex-Token": token}, headers=hdr)
            sections = _safe_json(r, "Plex").get("MediaContainer", {}).get("Directory", [])
            music_sections = (
                [library_id] if library_id
                else [s["key"] for s in sections if s.get("type") == "artist"]
            )
            if not music_sections:
                raise RuntimeError("Plex has no music libraries.")

            for sec in music_sections:
                offset = 0
                while True:
                    _scan_job["message"] = f"Reading Plex library {sec}: {len(tracks):,} tracks…"
                    r = await client.get(
                        f"{base}/library/sections/{sec}/all",
                        params={"type": 10, "X-Plex-Token": token,
                                "X-Plex-Container-Start": offset,
                                "X-Plex-Container-Size": 500},
                        headers=hdr)
                    items = _safe_json(r, "Plex").get("MediaContainer", {}).get("Metadata", [])
                    if not items:
                        break
                    for item in items:
                        fp = ""
                        try:
                            fp = item["Media"][0]["Part"][0]["file"]
                            if prefix_server and fp.startswith(prefix_server):
                                fp = prefix_docker + fp[len(prefix_server):]
                        except (KeyError, IndexError):
                            pass
                        # originalTitle = track artist on compilations; grandparent = album artist
                        artist = item.get("originalTitle") or item.get("grandparentTitle", "")
                        tracks.append({
                            "track_id":        None,
                            "duration_sec":    int(item["duration"] / 1000) if item.get("duration") else None,
                            "year":            item.get("parentYear") or item.get("year"),
                            "plex_rating_key": item.get("ratingKey", ""),
                            "artist":          artist,
                            "title":           item.get("title", ""),
                            "album":           item.get("parentTitle", ""),
                            "file_path":       fp,
                            "tag_artist":      artist,
                        })
                    offset += len(items)
                    if len(items) < 500:
                        break
    except RuntimeError:
        raise
    except httpx.HTTPError as e:
        raise RuntimeError(f"Could not reach Plex at {base} ({type(e).__name__}).")
    log.info(f"Plex fetch: {len(tracks):,} tracks from {len(music_sections)} library(s)")
    return tracks


async def _fetch_emby_tracks(base: str, token: str, user_id: str, library_id: str = None) -> list:
    tracks = []
    headers = {"Accept":"application/json","X-Emby-Token":token}
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            start = 0
            while True:
                r = await client.get(f"{base}/Users/{user_id}/Items",
                    params={k:v for k,v in {
                            "IncludeItemTypes":"Audio","Recursive":"true",
                            "Fields":"Path,MediaSources","api_key":token,
                            "StartIndex":start,"Limit":500,
                            "ParentId": library_id if library_id else None,
                        }.items() if v is not None},
                    headers=headers)
                items = _safe_json(r, "Emby").get("Items", [])
                if not items:
                    break
                for item in items:
                    tracks.append({
                        "track_id":  None,
                        "emby_id":   item.get("Id",""),
                        "duration_sec": int(item["RunTimeTicks"] / 10_000_000) if item.get("RunTimeTicks") else None,
                        "year":      item.get("ProductionYear"),
                        "artist":    (item.get("Artists") or [""])[0],
                        "title":     item.get("Name",""),
                        "album":     item.get("Album",""),
                        "file_path": item.get("Path",""),
                        "tag_artist":(item.get("Artists") or [""])[0],
                    })
                start += len(items)
                if len(items) < 500:
                    break
    except RuntimeError:
        raise
    except httpx.HTTPError as e:
        raise RuntimeError(f"Could not reach Emby at {base} ({type(e).__name__}).")
    return tracks


async def _fetch_jellyfin_tracks(base: str, token: str, user_id: str, library_id: str = None) -> list:
    tracks = []
    headers = {"Accept":"application/json"}
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            start = 0
            while True:
                r = await client.get(f"{base}/Users/{user_id}/Items",
                    params={k:v for k,v in {
                            "IncludeItemTypes":"Audio","Recursive":"true",
                            "Fields":"Path,MediaSources","api_key":token,
                            "StartIndex":start,"Limit":500,
                            "ParentId": library_id if library_id else None,
                        }.items() if v is not None},
                    headers=headers)
                items = _safe_json(r, "Jellyfin").get("Items", [])
                if not items:
                    break
                for item in items:
                    tracks.append({
                        "track_id": None,
                        "jf_id":    item.get("Id",""),
                        "duration_sec": int(item["RunTimeTicks"] / 10_000_000) if item.get("RunTimeTicks") else None,
                        "year":     item.get("ProductionYear"),
                        "artist":   (item.get("Artists") or [""])[0],
                        "title":    item.get("Name",""),
                        "album":    item.get("Album",""),
                        "file_path":item.get("Path",""),
                        "tag_artist":(item.get("Artists") or [""])[0],
                    })
                start += len(items)
                if len(items) < 500:
                    break
    except RuntimeError:
        raise
    except httpx.HTTPError as e:
        raise RuntimeError(f"Could not reach Jellyfin at {base} ({type(e).__name__}).")
    return tracks


# ══════════════════════════════════════════════════════════════════════════════
#  RESULTS
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/results")
async def get_results(
    charts:      Optional[str] = None,
    min_peak:    Optional[int] = None,
    max_peak:    Optional[int] = None,
    min_weeks:   Optional[int] = None,
    min_year:    Optional[int] = None,
    max_year:    Optional[int] = None,
    confidence:  Optional[str] = None,
    genre:       Optional[str] = None,
    min_score:   Optional[int] = None,
    decades:     Optional[str] = None,   # "1980,1990"
    ohw:         bool = False,           # one-hit wonders only
    genre_mode:  str = "main",           # main = online genres (engine → Last.fm → file tags); any = file tags
    crossovers:  bool = True,            # bridge styles cross families (Pop Rock in Pop playlists)
    limit:       int = 500,
    offset:      int = 0,
    _=Depends(require_auth),
):
    """
    Query chart_data joined with tracks for Groomer results table.
    Filters: charts (comma-sep), peak range, weeks min, year range, confidence, genre.
    Paginated — default 500 rows per page.
    """
    conditions = []
    params: list = []

    # charts param removed — genre filtering now by file tags via genre param

    if min_peak is not None:
        conditions.append("(cd.peak_position >= ? OR cd.chart_name = 'classic')"); params.append(min_peak)
    if max_peak is not None:
        conditions.append("(cd.peak_position <= ? OR cd.chart_name = 'classic')"); params.append(max_peak)
    if min_weeks is not None:
        conditions.append("cd.weeks_on_chart >= ?"); params.append(min_weeks)
    if confidence:
        conditions.append("cd.confidence = ?"); params.append(confidence)
    conditions.append("COALESCE(t.missing, 0) = 0")
    _yr = "COALESCE(hs.hit_year, cd.chart_year, t.year)"
    if min_year is not None:
        conditions.append(f"{_yr} >= ?"); params.append(min_year)
    if max_year is not None:
        conditions.append(f"{_yr} <= ?"); params.append(max_year)
    if min_score:
        conditions.append("COALESCE(hs.hit_score, 0) >= ?"); params.append(min_score)
    if ohw:
        conditions.append("hs.one_hit_wonder = 1")
    if decades:
        ds = [int(d) for d in decades.split(",") if d.strip().isdigit()]
        if ds:
            conditions.append("(" + " OR ".join([f"({_yr} BETWEEN ? AND ?)"] * len(ds)) + ")")
            for d in ds:
                params += [d, d + 9]
    if genre:
        # genre param is comma-separated genre keys (e.g. "country,rock,ccm")
        # Filter purely by file genre tags (genre_1/2/3) using keyword sets.
        # File tag match OR chart_name fallback for untagged tracks.
        # No active genre filter (ALL) = no condition added = return everything.
        genre_list = [g.strip() for g in genre.split(",") if g.strip()]
        if genre_list:
            _genre_param_start = len(params)
            genre_clauses = []
            for g in genre_list:
                keywords = _GENRE_FILTER_KEYWORDS.get(g, set()) or set()
                kw_list  = list(keywords)
                # Build the file-tag match clause
                if kw_list:
                    kw_placeholders = " OR ".join(
                        ["LOWER(t.genre_1) LIKE ? OR LOWER(t.genre_2) LIKE ? OR LOWER(t.genre_3) LIKE ?"]
                        * len(kw_list)
                    )
                    tag_clause = f"({kw_placeholders})"
                    for kw in kw_list:
                        params += [f"%{kw}%", f"%{kw}%", f"%{kw}%"]
                else:
                    tag_clause = None

                # Chart_name fallback — fires when track has no genre tag
                # Only for clean reliable sources, not tsort/chart2000/LBZ
                fallback_chart = _CHART_SOURCE_TO_GENRE.get(g)
                no_tag = "(t.genre_1 IS NULL OR t.genre_1 = '')"
                if tag_clause and fallback_chart and fallback_chart == g:
                    # Match: file tag matches OR (no tag AND chart_name matches)
                    genre_clauses.append(
                        f"({tag_clause} OR ({no_tag} AND cd.chart_name = ?))"
                    )
                    params.append(g)
                elif tag_clause:
                    genre_clauses.append(tag_clause)
                elif fallback_chart == g:
                    genre_clauses.append(f"({no_tag} AND cd.chart_name = ?)")
                    params.append(g)

            # ── Online genres (TOP GENRES mode) ─────────────────────────────
            # 1. Genre Engine family/styles (tg)  2. Last.fm top-3 (hs)  3. file tags
            from app.routers import retriever as _ret
            sel = set(genre_list)
            fams = [g for g in sel if g in _ret.GE_FAMILIES]
            stys = {g for g in sel if g in _ret.GE_STYLES}
            if crossovers:   # bridge styles let neighbouring families in (Pop ← Rock songs tagged Pop Rock)
                stys |= {k for k, (_l, fs) in _ret.GE_STYLES.items()
                         if len(fs) > 1 and set(fs) & set(fams)}
            if "mellow" in sel:
                stys |= _MELLOW_STYLES
            eng_parts, eng_params = [], []
            if fams:
                eng_parts.append(f"tg.family IN ({','.join('?' * len(fams))})")
                eng_params += fams
            for st in sorted(stys):
                eng_parts.append("tg.styles LIKE ?")
                eng_params.append(f'%"{st}"%')
            engine = ("(tg.family IS NOT NULL AND (" + " OR ".join(eng_parts) + "))") if eng_parts \
                     else "(tg.family IS NOT NULL AND 0)"
            top3 = "(" + " OR ".join(["(',' || hs.genre_keys || ',') LIKE ?"] * len(genre_list)) + ")"
            top3_params = [f"%,{g},%" for g in genre_list]
            if genre_mode == "main":
                loose = ("(" + " OR ".join(genre_clauses) + ")") if genre_clauses else "0"
                conditions.append(
                    f"({engine} OR (tg.family IS NULL AND hs.genre_keys IS NOT NULL AND {top3}) "
                    f"OR (tg.family IS NULL AND hs.genre_keys IS NULL AND {loose}))")
                # order: engine params, top-3 params, then loose params (already appended)
                params[_genre_param_start:_genre_param_start] = eng_params + top3_params
            elif genre_clauses:
                conditions.append("(" + " OR ".join(genre_clauses) + ")")

    where        = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    # Keep filter_params separate — reused for COUNT query without limit/offset
    filter_params = list(params)
    page_params   = list(params) + [limit, offset]

    try:
        async with aiosqlite.connect(_DYNAMIC_DB) as db:
            db.row_factory = aiosqlite.Row

            # Total count (uses filter_params only — no limit/offset)
            async with db.execute(f"""
                SELECT COUNT(*)
                FROM chart_data cd
                JOIN tracks t ON cd.track_id = t.track_id
                LEFT JOIN artists a ON t.artist_id = a.artist_id
                LEFT JOIN hit_scores hs ON hs.track_id = cd.track_id
                LEFT JOIN track_genres tg ON tg.track_id = cd.track_id
                {where}
            """, filter_params) as cur:
                total_row   = await cur.fetchone()
                total_count = total_row[0] if total_row else 0

            # Dedup: one row per title, best non-compilation artist wins.
            #
            # Compilation fix (M4 bug): Previously partitioned by artist+title,
            # which meant "Various Artists - Boogie Wonderland" and
            # "Earth, Wind & Fire - Boogie Wonderland" survived as TWO rows
            # because their artist strings differed. New logic:
            #   1. Partition by title only
            #   2. Rank compilation rows (VA / Various / blank) BELOW real
            #      artist rows for the same title
            #   3. Then rank by audio format (FLAC > M4A > ... > MP3)
            dedup_where = ("AND " + " AND ".join(conditions)) if conditions else ""
            async with db.execute(f"""
                SELECT * FROM (
                    SELECT
                        cd.chart_name, cd.peak_position, cd.weeks_on_chart,
                        cd.star_rating, cd.confidence, cd.comment_string,
                        cd.listener_count, cd.fetched_at,
                        COALESCE(hs.hit_year, cd.chart_year, t.year) AS chart_year,
                        hs.hit_score, hs.one_hit_wonder, hs.hot100_peak,
                        hs.hot100_weeks, hs.yearend_rank, t.duration_sec,
                        hs.listeners, hs.main_genre, hs.never_charted,
                        tg.family AS ge_family, tg.styles AS ge_styles,
                        t.title, t.file_path, t.file_format,
                        t.plex_rating_key, t.emby_id, t.jf_id,
                        t.track_id, t.genre_1, t.genre_2, t.genre_3,
                        COALESCE(t.tag_artist, a.name, '') AS tag_artist,
                        COALESCE(t.tag_album,  al.title, '') AS tag_album,
                        ROW_NUMBER() OVER (
                            PARTITION BY COALESCE(hs.song_key, LOWER(TRIM(t.title)) || '|' ||
                                         LOWER(TRIM(COALESCE(t.tag_artist, a.name, ''))))
                            ORDER BY
                                -- Compilation rows rank below real-artist rows
                                CASE
                                    WHEN LOWER(TRIM(COALESCE(t.tag_artist, a.name, '')))
                                         IN ('', 'various artists', 'various',
                                             'va', 'v.a.', 'compilation') THEN 2
                                    ELSE 1
                                END ASC,
                                -- Then prefer lossless
                                CASE LOWER(t.file_format)
                                    WHEN 'flac' THEN 1 WHEN 'm4a'  THEN 2
                                    WHEN 'wav'  THEN 3 WHEN 'aiff' THEN 4
                                    WHEN 'ogg'  THEN 5 WHEN 'mp3'  THEN 6
                                    ELSE 7
                                END ASC
                        ) AS rn
                    FROM chart_data cd
                    JOIN tracks t   ON cd.track_id = t.track_id
                    LEFT JOIN artists a  ON t.artist_id = a.artist_id
                    LEFT JOIN albums al  ON t.album_id  = al.album_id
                    LEFT JOIN hit_scores hs ON hs.track_id = cd.track_id
                    LEFT JOIN track_genres tg ON tg.track_id = cd.track_id
                    WHERE 1=1 {dedup_where}
                ) WHERE rn = 1
                ORDER BY COALESCE(hit_score, 0) DESC, peak_position ASC
                LIMIT ? OFFSET ?
            """, page_params) as cur:
                rows = await cur.fetchall()

            return {
                "results": [dict(r) for r in rows],
                "count":   len(rows),
                "total":   total_count,
                "offset":  offset,
                "limit":   limit,
            }

    except Exception as e:
        log.exception("Results query error")
        raise HTTPException(500, str(e))


# ══════════════════════════════════════════════════════════════════════════════
#  PLAYLIST PUSH
# ══════════════════════════════════════════════════════════════════════════════

class PlaylistBuildRequest(BaseModel):
    genre:        Optional[str] = None          # same keys as /results genre filter
    genre_mode:   str = "main"
    crossovers:   bool = True
    decades:      List[int] = []                # [1980, 1990]
    min_score:    int = 0
    ohw_only:     bool = False
    minutes:      int = 120                     # per playlist (per day)
    days:         int = 1                       # 1–7 playlists, no song repeated across them
    familiar_pct: int = 90                      # % of runtime from the top-ranked pool
    artist_gap:   int = 12                      # songs before an artist may repeat


@router.post("/playlist/build")
async def playlist_build(req: PlaylistBuildRequest, _=Depends(require_auth)):
    """Build a sequenced playlist of a target length from Hit Scores."""
    res = await get_results(
        charts=None, min_peak=None, max_peak=None, min_weeks=None,
        min_year=None, max_year=None, confidence=None, genre=req.genre,
        min_score=req.min_score or None,
        decades=",".join(str(d) for d in req.decades) or None,
        ohw=req.ohw_only, genre_mode=req.genre_mode, crossovers=req.crossovers,
        limit=50000, offset=0, _=None,
    )
    # One version per song (strip "(Remastered)", "feat." etc.)
    best: dict = {}
    for r in res["results"]:
        k = (_artist_key(r.get("tag_artist", "")), _title_key(r.get("title", "")))
        if k not in best or (r.get("hit_score") or 0) > (best[k].get("hit_score") or 0):
            best[k] = r
    cands = sorted(best.values(), key=lambda r: -(r.get("hit_score") or 0))
    if not cands:
        return {"results": [], "count": 0, "minutes": 0, "candidates": 0,
                "message": "No songs match these filters."}

    days     = max(1, min(req.days, 7))
    per_day  = max(10, min(req.minutes, 24 * 60)) * 60
    target   = per_day * days
    fam_pct  = max(0, min(req.familiar_pct, 100))
    fam_goal = target * fam_pct / 100
    dur      = lambda r: r.get("duration_sec") or 240

    # Familiar: random draw from the top-ranked pool (≈1.5× what's needed → variety per build)
    pool, acc = [], 0
    for r in cands:
        if acc >= fam_goal * 1.5:
            break
        pool.append(r); acc += dur(r)
    random.shuffle(pool)
    picked, total, used = [], 0, set()
    for r in pool:
        if total >= fam_goal:
            break
        picked.append(r); used.add(r["track_id"]); total += dur(r)

    # Discovery: random from everything not yet used
    rest = [r for r in cands if r["track_id"] not in used]
    if fam_pct >= 100:
        rest.sort(key=lambda r: -(r.get("hit_score") or 0))
    else:
        random.shuffle(rest)
    for r in rest:
        if total >= target:
            break
        picked.append(r); used.add(r["track_id"]); total += dur(r)

    # Split across days: snake-deal by score so every day gets an equal share of the biggest hits
    picked.sort(key=lambda r: -(r.get("hit_score") or 0))
    buckets = [[] for _ in range(days)]
    for i, r in enumerate(picked):
        rnd, pos = divmod(i, days)
        buckets[pos if rnd % 2 == 0 else days - 1 - pos].append(r)

    def _sequence(items: list) -> list:
        random.shuffle(items)
        gap, order, recent = max(0, req.artist_gap), [], []
        while items:
            j = next((i for i, r in enumerate(items)
                      if _artist_key(r.get("tag_artist", "")) not in recent), 0)
            r = items.pop(j)
            order.append(r)
            if gap:
                recent.append(_artist_key(r.get("tag_artist", "")))
                recent = recent[-gap:]
        return order

    out_days, results = [], []
    for d, bucket in enumerate(buckets, start=1):
        order = _sequence(bucket)
        for i, r in enumerate(order):
            r = dict(r, _seq=i + 1, _day=d if days > 1 else None)
            results.append(r)
        out_days.append({"day": d, "track_ids": [r["track_id"] for r in order],
                         "minutes": round(sum(dur(r) for r in order) / 60)})

    return {
        "results":    results,
        "days":       out_days,
        "count":      len(results),
        "minutes":    round(total / 60),
        "unknown_durations": sum(1 for r in results if not r.get("duration_sec")),
        "candidates": len(cands),
        "short":      total < target,
    }


# ── Builder presets (stored server-side in app_settings) ─────────────────────
@router.get("/presets")
async def presets_get(_=Depends(require_auth)):
    try:
        async with aiosqlite.connect(_DYNAMIC_DB) as db:
            row = await (await db.execute(
                "SELECT value FROM app_settings WHERE key='groomer_presets'")).fetchone()
        return {"presets": json.loads(row[0]) if row and row[0] else {}}
    except Exception:
        return {"presets": {}}


@router.put("/presets")
async def presets_put(body: dict, _=Depends(require_auth)):
    presets = body.get("presets")
    if not isinstance(presets, dict) or len(presets) > 50:
        raise HTTPException(400, "presets must be an object (max 50)")
    async with aiosqlite.connect(_DYNAMIC_DB) as db:
        await db.execute(
            "INSERT INTO app_settings (key, value, updated_at) VALUES ('groomer_presets', ?, datetime('now')) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (json.dumps(presets),))
        await db.commit()
    return {"ok": True, "count": len(presets)}


class PlaylistPushRequest(BaseModel):
    server:        Optional[str] = None   # Required for push, optional for M3U
    playlist_name: str = "Chart Hits"
    track_ids:     Optional[List[int]] = None
    # Post-scan filters — applied to chart_data at playlist-build time, not at scan time
    charts:        Optional[List[str]] = None      # e.g. ['rock','country'] — None = all
    max_peak:      Optional[int] = None            # e.g. 40 = Top 40 only
    min_weeks:     Optional[int] = None            # e.g. 4 = charted 4+ weeks
    confidence:    Optional[str] = None            # 'high'|'medium'|'low'|None=all
    limit:         int = 5000
    path_mode:     Optional[str] = None            # 'navidrome' = use Navidrome prefix


async def _record_last_playlist(name: str, dest: str, count: int):
    """Start Here: remember the most recent playlist sent anywhere."""
    try:
        val = json.dumps({"name": name or "", "dest": dest, "count": count,
                          "at": datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")})
        async with aiosqlite.connect(_DYNAMIC_DB) as db:
            await db.execute(
                "INSERT INTO app_settings (key, value, updated_at) VALUES ('grm_last_push', ?, datetime('now')) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at", (val,))
            await db.commit()
    except Exception as e:
        log.warning(f"record last playlist: {e}")


@router.post("/playlist/push")
async def playlist_push(req: PlaylistPushRequest, _=Depends(require_auth)):
    if not req.server:
        raise HTTPException(400, "server is required for playlist push (plex/emby/jellyfin)")
    tracks = await _get_playlist_tracks(req)
    if not tracks:
        raise HTTPException(404, "No tracks found for playlist.")

    try:
        async with aiosqlite.connect(_DYNAMIC_DB) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT base_url, token_enc, extra_json FROM connections WHERE service=?",
                (req.server,)
            ) as cur:
                conn = await cur.fetchone()
    except Exception:
        conn = None

    if not conn:
        raise HTTPException(400, f"No {req.server} connection found.")

    base_url = conn["base_url"]
    token    = decrypt_token(conn["token_enc"]) if conn["token_enc"] else ""
    extra    = json.loads(conn["extra_json"] or "{}") if conn["extra_json"] else {}

    try:
        if req.server == "plex":
            res = await _push_to_plex(base_url, token, req.playlist_name, tracks)
        else:
            user_id = extra.get("user_id", "")
            if req.server == "emby":
                res = await _push_to_emby(base_url, token, user_id, req.playlist_name, tracks)
            else:
                res = await _push_to_jellyfin(base_url, token, user_id, req.playlist_name, tracks)
        await _record_last_playlist(req.playlist_name, req.server, len(tracks))
        return res
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"Push failed: {e}")


@router.post("/playlist/m3u")
async def playlist_m3u(req: PlaylistPushRequest, _=Depends(require_auth)):
    tracks = await _get_playlist_tracks(req)
    if not tracks:
        raise HTTPException(404, "No tracks found.")

    # ── Path translation for M3U ─────────────────────────────────────────────
    # Track paths in the DB can be:
    #   a) Docker paths:  /music/FULL ALBUMS/...
    #   b) Server paths:  /media/NAS1/MUSIC TAGGED/FULL ALBUMS/...
    #   c) Desktop paths: /media/colby/NAS1/MUSIC TAGGED/FULL ALBUMS/...
    #
    # Standard M3U: translate to desktop/host path (what the user's machine opens).
    # Navidrome M3U: strip to relative suffix, prepend the Navidrome music mount.
    desktop_prefix   = ""
    docker_prefix    = os.environ.get("DOCKER_MUSIC_PREFIX", "/music")
    navidrome_prefix = ""
    server_raw       = ""
    try:
        async with aiosqlite.connect(_DYNAMIC_DB) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT key, value FROM app_settings "
                "WHERE key IN ('path_server_prefix','path_docker_prefix','path_navidrome_prefix')"
            ) as cur:
                rows = await cur.fetchall()
            ps = {r["key"]: r["value"] for r in rows}
            desktop_prefix   = ps.get("path_server_prefix",    "")
            navidrome_prefix = ps.get("path_navidrome_prefix", "").rstrip("/")
            kp = ps.get("path_docker_prefix", "")
            if kp:
                docker_prefix = kp
    except Exception:
        pass
    server_raw = os.environ.get("MEDIA_SERVER_MUSIC_PREFIX", "")

    # ── Helper: strip any known prefix, return the bare relative suffix ──────
    def _bare_suffix(fp: str) -> str:
        """Return the path component after the music root, e.g. /FULL ALBUMS/..."""
        for prefix in [desktop_prefix, docker_prefix, server_raw]:
            if prefix and fp.startswith(prefix):
                return fp[len(prefix):]
        # Last-resort anchor search
        if desktop_prefix:
            anchor = desktop_prefix.rstrip("/").rsplit("/", 1)[-1]
            idx = fp.find(f"/{anchor}/")
            if idx >= 0:
                return fp[idx + len(anchor):]
        return fp  # give up — return as-is

    def _translate_path(fp: str) -> str:
        """Standard translation: stored path → host/desktop path."""
        if not desktop_prefix or not fp:
            return fp
        if fp.startswith(desktop_prefix):
            return fp
        suffix = _bare_suffix(fp)
        return desktop_prefix + suffix if suffix != fp else fp

    def _translate_navidrome(fp: str) -> str:
        """Navidrome translation: stored path → Navidrome Docker mount path."""
        if not fp:
            return fp
        suffix = _bare_suffix(fp)
        # Ensure leading slash
        if suffix and not suffix.startswith("/"):
            suffix = "/" + suffix
        return navidrome_prefix + suffix

    # ── Choose translator based on path_mode ─────────────────────────────────
    if req.path_mode == "navidrome":
        if not navidrome_prefix:
            raise HTTPException(400, "Navidrome music path is not configured. Set it in The Kennel → Path Translator.")
        translate = _translate_navidrome
    else:
        translate = _translate_path

    lines = ["#EXTM3U", f"#PLAYLIST:{req.playlist_name}"]
    for t in tracks:
        artist = t.get("tag_artist", "")
        title  = t.get("title", "")
        fp     = translate(t.get("file_path", ""))
        lines.append(f"#EXTINF:-1,{artist} - {title}")
        lines.append(fp)

    await _record_last_playlist(req.playlist_name,
                                "navidrome-m3u" if req.path_mode == "navidrome" else "m3u", len(tracks))
    return PlainTextResponse(
        "\n".join(lines),
        media_type="audio/x-mpegurl",
        headers={"Content-Disposition": f'attachment; filename="{req.playlist_name}.m3u"'},
    )


async def _get_playlist_tracks(req: PlaylistPushRequest) -> list:
    """
    Resolve the final ordered track list to push to a media server.

    FIX (M4 bug): When the frontend sends explicit track_ids (user selected
    rows from the already-deduped results table), TRUST them. Previously we
    ran a second global dedup that filtered some of the selected IDs out
    because a higher-ranked dupe existed elsewhere in chart_data — this
    caused push counts to be lower than selected counts (e.g. pushed 900 of
    1500 selected, 977 of 1000).

    Dedup only fires when no track_ids were provided (e.g. "push everything
    matching peak<=N and charts=X" mode). When it does, it uses the same
    compilation-aware ranking as get_results() so the two paths agree.
    """
    conditions = []
    params: list = []

    if req.track_ids:
        conditions.append(f"cd.track_id IN ({','.join('?'*len(req.track_ids))})")
        params += req.track_ids
    if req.charts:
        conditions.append(f"cd.chart_name IN ({','.join('?'*len(req.charts))})")
        params += req.charts
    if req.max_peak:
        conditions.append("cd.peak_position <= ?"); params.append(req.max_peak)
    if req.min_weeks:
        conditions.append("cd.weeks_on_chart >= ?"); params.append(req.min_weeks)
    if req.confidence:
        conditions.append("cd.confidence = ?"); params.append(req.confidence)

    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    params.append(req.limit)

    async with aiosqlite.connect(_DYNAMIC_DB) as db:
        db.row_factory = aiosqlite.Row

        # ── Path A: explicit track_ids — no dedup, trust the frontend ───────
        if req.track_ids:
            async with db.execute(f"""
                SELECT
                    t.track_id, t.title, t.file_path, t.file_format,
                    t.plex_rating_key, t.emby_id, t.jf_id,
                    COALESCE(t.tag_artist, a.name, '') AS tag_artist,
                    cd.peak_position, cd.chart_name
                FROM chart_data cd
                JOIN tracks t  ON cd.track_id = t.track_id
                LEFT JOIN artists a ON t.artist_id = a.artist_id
                {where}
                ORDER BY cd.peak_position ASC
                LIMIT ?
            """, params) as cur:
                rows = await cur.fetchall()
            # Keep the caller's order (built playlists are sequenced); one row per track
            pos = {tid: i for i, tid in enumerate(req.track_ids)}
            seen, out = set(), []
            for r in sorted((dict(r) for r in rows), key=lambda d: pos.get(d["track_id"], 1 << 30)):
                if r["track_id"] not in seen:
                    seen.add(r["track_id"]); out.append(r)
            return out

        # ── Path B: no explicit IDs — run compilation-aware dedup ───────────
        # Format rank: FLAC=1, M4A=2, WAV=3, AIFF=4, OGG=5, MP3=6, other=7
        # Compilation rank: VA/blank=2, real artist=1 (real artist wins)
        async with db.execute(f"""
            SELECT
                t.track_id, t.title, t.file_path, t.file_format,
                t.plex_rating_key, t.emby_id, t.jf_id,
                COALESCE(t.tag_artist, a.name, '') AS tag_artist,
                cd.peak_position, cd.chart_name
            FROM chart_data cd
            JOIN tracks t  ON cd.track_id = t.track_id
            LEFT JOIN artists a ON t.artist_id = a.artist_id
            WHERE cd.track_id IN (
                SELECT track_id FROM (
                    SELECT
                        t2.track_id,
                        ROW_NUMBER() OVER (
                            PARTITION BY LOWER(t2.title)
                            ORDER BY
                                CASE
                                    WHEN LOWER(TRIM(COALESCE(t2.tag_artist, a2.name, '')))
                                         IN ('', 'various artists', 'various',
                                             'va', 'v.a.', 'compilation') THEN 2
                                    ELSE 1
                                END ASC,
                                CASE LOWER(t2.file_format)
                                    WHEN 'flac' THEN 1 WHEN 'm4a'  THEN 2
                                    WHEN 'wav'  THEN 3 WHEN 'aiff' THEN 4
                                    WHEN 'ogg'  THEN 5 WHEN 'mp3'  THEN 6
                                    ELSE 7
                                END ASC
                        ) AS rn
                    FROM chart_data cd2
                    JOIN tracks t2 ON cd2.track_id = t2.track_id
                    LEFT JOIN artists a2 ON t2.artist_id = a2.artist_id
                ) WHERE rn = 1
            )
            {"AND " + " AND ".join(conditions) if conditions else ""}
            ORDER BY cd.peak_position ASC
            LIMIT ?
        """, params) as cur:
            rows = await cur.fetchall()
    return [dict(r) for r in rows]


# ══════════════════════════════════════════════════════════════════════════════
#  PLEX / EMBY / JELLYFIN PUSH
# ══════════════════════════════════════════════════════════════════════════════

async def _push_to_plex(base: str, token: str, name: str, tracks: list) -> dict:
    """Creates/replaces a Plex playlist in EXACT track order. Never guesses a different song."""
    headers = {"Accept": "application/json"}
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.get(f"{base}/", params={"X-Plex-Token": token}, headers=headers)
        if not r.is_success:
            raise HTTPException(502, f"Plex unreachable: HTTP {r.status_code}")
        machine_id = r.json().get("MediaContainer", {}).get("machineIdentifier", "")
        if not machine_id:
            raise HTTPException(502, "Could not get Plex machine ID")

        rating_keys, not_found, missing = [], 0, []
        for track in tracks:
            if track.get("plex_rating_key"):
                rating_keys.append(str(track["plex_rating_key"])); continue
            match = None
            try:
                sr = await client.get(f"{base}/search",
                    params={"query": track.get("title", ""), "type": 10, "X-Plex-Token": token},
                    headers=headers)
                if sr.is_success:
                    want_t = _title_key(track.get("title", ""))
                    match = next((i for i in sr.json().get("MediaContainer", {}).get("Metadata", [])
                                  if _title_key(i.get("title", "")) == want_t
                                  and _artist_match(track.get("tag_artist", ""),
                                                    i.get("originalTitle"), i.get("grandparentTitle"))),
                                 None)
            except Exception:
                pass
            if match and match.get("ratingKey"):
                rating_keys.append(str(match["ratingKey"]))
            else:
                not_found += 1
                missing.append(f"{track.get('tag_artist', '')} — {track.get('title', '')}")
        if missing:
            log.info(f"Plex push '{name}': {len(missing)} not found: {missing[:10]}")
        if not rating_keys:
            raise HTTPException(404, "No tracks found in Plex")

        # Replace existing playlist with the same name
        lr = await client.get(f"{base}/playlists", params={"X-Plex-Token": token}, headers=headers)
        if lr.is_success:
            for p in lr.json().get("MediaContainer", {}).get("Metadata", []):
                if p.get("title") == name:
                    await client.delete(f"{base}/playlists/{p['ratingKey']}",
                                        params={"X-Plex-Token": token})

        def _uri(keys):
            return (f"server://{machine_id}/com.plexapp.plugins.library"
                    f"/library/metadata/{','.join(keys)}")

        BATCH = 100
        cr = await client.post(f"{base}/playlists",
            params={"type": "audio", "title": name, "smart": "0",
                    "uri": _uri(rating_keys[:BATCH]), "X-Plex-Token": token},
            headers=headers)
        if not cr.is_success:
            raise HTTPException(502, f"Plex create failed: HTTP {cr.status_code}")
        pl_id = (cr.json().get("MediaContainer", {}).get("Metadata", [{}])[0] or {}).get("ratingKey")
        if not pl_id:
            raise HTTPException(502, "No playlist ID from Plex")

        added, failed = len(rating_keys[:BATCH]), 0
        for i in range(BATCH, len(rating_keys), BATCH):      # sequential → order preserved
            batch = rating_keys[i:i + BATCH]
            pr = await client.put(f"{base}/playlists/{pl_id}/items",
                params={"uri": _uri(batch), "X-Plex-Token": token}, headers=headers)
            if pr.is_success:
                added += len(batch)
            else:
                failed += len(batch)

    return {"ok": True, "server": "plex", "playlist": name,
            "added": added, "failed": failed, "not_found": not_found, "playlist_id": pl_id}


async def _push_to_emby(base: str, token: str, user_id: str, name: str, tracks: list) -> dict:
    """
    Emby playlist push — batches IDs to avoid 414 URI Too Long error.
    Max 50 IDs per create call.
    """
    ah = {"Accept":"application/json","X-Emby-Token":token,
          "X-Emby-Authorization":f'MediaBrowser Token="{token}"'}

    async with httpx.AsyncClient(timeout=15.0) as client:
        emby_ids = []; not_found = 0

        async def _lk(t):
            if t.get("emby_id"): return t["emby_id"]
            try:
                r = await client.get(f"{base}/Users/{user_id}/Items",
                    params={"searchTerm":t.get("title",""),"IncludeItemTypes":"Audio",
                            "Recursive":"true","api_key":token}, headers=ah)
                items = r.json().get("Items",[]) if r.is_success else []
                want_t = _title_key(t.get("title", ""))
                m = next((i for i in items
                    if _title_key(i.get("Name", "")) == want_t
                    and _artist_match(t.get("tag_artist", ""),
                                      i.get("Artists") or [], i.get("AlbumArtist"))), None)
                return m.get("Id") if m else None
            except:
                return None

        for i in range(0, len(tracks), 5):
            ids = await asyncio.gather(*[_lk(t) for t in tracks[i:i+5]])
            for eid in ids:
                if eid: emby_ids.append(eid)
                else: not_found += 1

        if not emby_ids:
            raise HTTPException(404, "No tracks found in Emby")

        # Delete existing playlist — paginated fetch + exact name match (no fuzzy SearchTerm)
        try:
            pl_start = 0
            while True:
                pr = await client.get(f"{base}/Users/{user_id}/Items",
                    params={"IncludeItemTypes":"Playlist","Recursive":"true",
                            "api_key":token,"StartIndex":pl_start,"Limit":200},
                    headers=ah)
                if not pr.is_success:
                    break
                pl_items = pr.json().get("Items", [])
                matched = next((p for p in pl_items if p.get("Name") == name), None)
                if matched:
                    dr = await client.delete(
                        f"{base}/Items/{matched['Id']}?api_key={token}", headers=ah)
                    log.info(f"Emby playlist delete: {dr.status_code}")
                    await asyncio.sleep(0.5)  # brief pause so Emby registers delete before create
                    break
                if len(pl_items) < 200:
                    break
                pl_start += len(pl_items)
        except Exception as e:
            log.warning(f"Emby playlist delete warning: {e}")

        # Create with first batch of max 50 IDs (fixes 414 error)
        first_batch = emby_ids[:50]
        cr = await client.post(f"{base}/Playlists",
            params={"Name":name,"Ids":",".join(first_batch),"UserId":user_id,
                    "MediaType":"Audio","api_key":token}, headers=ah)
        if not cr.is_success:
            raise HTTPException(502, f"Emby create failed: {cr.status_code}")
        pl = cr.json()
        pl_id = pl.get("Id") or pl.get("id") or pl.get("PlaylistId")
        if not pl_id:
            raise HTTPException(502, "No playlist ID from Emby")

        # Add remaining IDs in batches of 50
        for i in range(50, len(emby_ids), 50):
            batch = emby_ids[i:i+50]
            await client.post(f"{base}/Playlists/{pl_id}/Items",
                params={"Ids":",".join(batch),"api_key":token}, headers=ah)

    return {"ok":True,"server":"emby","playlist":name,
            "added":len(emby_ids),"not_found":not_found,"playlist_id":pl_id}


async def _push_to_jellyfin(base: str, token: str, user_id: str, name: str, tracks: list) -> dict:
    """
    Jellyfin playlist push.

    ID resolution strategy (in priority order):
      1. jf_id already stored on track (from Jellyfin fetch — most reliable)
      2. Path-based lookup: fetch JF library with Path field, match file_path exactly
      3. Artist+title search fallback (last resort, less reliable)

    Delete strategy:
      - Fetch ALL playlists (no SearchTerm — avoids fuzzy match finding wrong playlist)
      - Exact name match
      - DELETE and confirm 200/204 before creating new playlist

    Batch adds:
      - Jellyfin create endpoint accepts all IDs at once (no 414 issue unlike Emby)
      - Add in batches of 200 for reliability on large playlists
    """
    hdrs = {"Accept":"application/json","Content-Type":"application/json"}
    not_found = 0

    async with httpx.AsyncClient(timeout=60.0) as client:

        # ── Build a path→jf_id index from the JF library for fast lookup ────────
        path_index: dict = {}
        try:
            start = 0
            while True:
                r = await client.get(f"{base}/Users/{user_id}/Items",
                    params={"IncludeItemTypes":"Audio","Recursive":"true",
                            "Fields":"Path","api_key":token,
                            "StartIndex":start,"Limit":1000},
                    headers=hdrs)
                if not r.is_success:
                    break
                items = r.json().get("Items", [])
                if not items:
                    break
                for item in items:
                    p = item.get("Path","")
                    if p:
                        path_index[p] = item.get("Id","")
                start += len(items)
                if len(items) < 1000:
                    break
        except Exception as e:
            log.warning(f"JF path index build failed: {e}")

        # ── Resolve JF IDs for each track ────────────────────────────────────────
        jf_ids = []
        for t in tracks:
            # Priority 1: stored jf_id
            if t.get("jf_id"):
                jf_ids.append(t["jf_id"]); continue

            # Priority 2: path match
            fp = t.get("file_path","")
            if fp and fp in path_index:
                jf_ids.append(path_index[fp]); continue

            # Priority 3: artist+title search fallback
            try:
                r = await client.get(f"{base}/Users/{user_id}/Items",
                    params={"searchTerm": t.get("title",""),
                            "IncludeItemTypes":"Audio","Recursive":"true",
                            "Fields":"Path","api_key":token,
                            "Limit":20},
                    headers=hdrs)
                items = r.json().get("Items",[]) if r.is_success else []
                want_t = _title_key(t.get("title", ""))
                # Title + artist must both match — never guess a different song
                match = next((i for i in items
                    if _title_key(i.get("Name", "")) == want_t
                    and _artist_match(t.get("tag_artist", ""),
                                      i.get("Artists") or [], i.get("AlbumArtist"))), None)
                if match and match.get("Id"):
                    jf_ids.append(match["Id"])
                else:
                    not_found += 1
            except Exception:
                not_found += 1

        if not jf_ids:
            raise HTTPException(404, "No tracks found in Jellyfin")

        # ── Delete existing playlist — fetch ALL, exact name match ────────────────
        try:
            # Jellyfin paginates playlists — fetch until exhausted
            pl_start = 0
            while True:
                pr = await client.get(f"{base}/Users/{user_id}/Items",
                    params={"IncludeItemTypes":"Playlist","Recursive":"true",
                            "api_key":token,"StartIndex":pl_start,"Limit":200},
                    headers=hdrs)
                if not pr.is_success:
                    break
                pl_items = pr.json().get("Items",[])
                matched = next((p for p in pl_items if p.get("Name") == name), None)
                if matched:
                    dr = await client.delete(
                        f"{base}/Items/{matched['Id']}?api_key={token}", headers=hdrs)
                    log.info(f"JF playlist delete: {dr.status_code}")
                    break
                if len(pl_items) < 200:
                    break
                pl_start += len(pl_items)
        except Exception as e:
            log.warning(f"JF playlist delete warning: {e}")

        # ── Create playlist with first batch ──────────────────────────────────────
        first_batch = jf_ids[:200]
        cr = await client.post(f"{base}/Playlists?api_key={token}", headers=hdrs,
            json={"Name":name,"Ids":first_batch,"UserId":user_id,"MediaType":"Audio"})
        if not cr.is_success:
            raise HTTPException(502, f"JF create failed: {cr.status_code}")
        pl  = cr.json()
        pl_id = pl.get("Id") or pl.get("id")
        if not pl_id:
            raise HTTPException(502, "No playlist ID from Jellyfin")

        # ── Add remaining in batches of 200 ───────────────────────────────────────
        # NOTE: /Playlists/{id}/Items requires Ids as query param, not JSON body
        for i in range(200, len(jf_ids), 200):
            batch = jf_ids[i:i+200]
            add_r = await client.post(
                f"{base}/Playlists/{pl_id}/Items",
                params={"api_key": token, "Ids": ",".join(batch), "UserId": user_id},
                headers=hdrs
            )
            if not add_r.is_success:
                log.warning(f"JF batch add failed: {add_r.status_code} — {add_r.text[:200]}")

    added = len(jf_ids)
    return {"ok":True,"server":"jellyfin","playlist":name,
            "added":added,"not_found":not_found,"playlist_id":pl_id}


# ══════════════════════════════════════════════════════════════════════════════
#  BIG HITS YOU DON'T OWN (M6) — shared by Top Dogs + The Sniffer
# ══════════════════════════════════════════════════════════════════════════════

class BigHitsRequest(BaseModel):
    decades:    List[int] = []
    genres:     List[str] = []
    min_score:  int = 70
    ohw_only:   bool = False
    crossovers: bool = True
    limit:      int = 500


_bh_lookup = {"running": False, "done": 0, "total": 0}


async def _lfm_lookup_pairs(pairs: list):
    """Background: staying-power lookups for missing songs (cached in lastfm_tracks)."""
    if _bh_lookup["running"] or not pairs:
        return
    key = await _get_lastfm_key()
    if not key:
        return
    _bh_lookup.update(running=True, done=0, total=len(pairs))
    SQL = ("INSERT INTO lastfm_tracks (artist_key, title_key, status, listeners, playcount, tags, fetched_at) "
           "VALUES (?, ?, ?, ?, ?, ?, datetime('now')) ON CONFLICT(artist_key, title_key) DO UPDATE SET "
           "status=excluded.status, listeners=excluded.listeners, playcount=excluded.playcount, "
           "tags=excluded.tags, fetched_at=excluded.fetched_at")
    try:
        async with httpx.AsyncClient(timeout=20.0) as client, \
                   aiosqlite.connect(_DYNAMIC_DB, timeout=60) as db:
            for i, (artist, title) in enumerate(pairs):
                t0 = time.monotonic()
                kind, j = await _lfm_call(client, {"method": "track.getInfo", "api_key": key, "autocorrect": 1,
                                                   "artist": _FEAT_RE.sub("", artist),
                                                   "track": _lfm_clean_title(title)})
                ak, tk = _artist_key(artist), _title_key(title)
                if kind == "ok":
                    tr = j.get("track", {}) or {}
                    await db.execute(SQL, (ak, tk, "ok", int(tr.get("listeners") or 0),
                                           int(tr.get("playcount") or 0),
                                           json.dumps(_lfm_tag_names(tr.get("toptags")))))
                elif kind == "notfound":
                    await db.execute(SQL, (ak, tk, "notfound", None, None, None))
                elif kind == "fatal":
                    break
                await db.commit()
                _bh_lookup["done"] = i + 1
                await asyncio.sleep(max(0.0, _LFM_INTERVAL - (time.monotonic() - t0)))
    except Exception as e:
        log.warning(f"Big-hits Last.fm lookups stopped: {e}")
    finally:
        _bh_lookup["running"] = False


@router.post("/big-hits")
async def big_hits(req: BigHitsRequest, _=Depends(require_auth)):
    await _migrate_chart_data()
    res = await asyncio.get_event_loop().run_in_executor(
        None, _big_hits_sync, req.decades, req.genres, req.min_score, req.ohw_only,
        req.crossovers, max(0, min(req.limit, 2000)))
    pending = res.pop("pending_lookups")
    if pending and not _bh_lookup["running"]:
        asyncio.create_task(_lfm_lookup_pairs(pending))
    res["lookups_pending"] = len(pending)
    res["lookups_running"] = _bh_lookup["running"] or bool(pending)
    return res


@router.get("/big-hits/status")
async def big_hits_status(_=Depends(require_auth)):
    return _bh_lookup


# ══════════════════════════════════════════════════════════════════════════════
#  BILLBOARD 200 ALBUMS (M6) — for The Bloodhound
# ══════════════════════════════════════════════════════════════════════════════
_B200_URL = "https://raw.githubusercontent.com/utdata/rwd-billboard-data/main/data-out/billboard-200-current.csv"
_b200_job = {"status": "idle", "message": ""}


async def _b200_migrate():
    async with aiosqlite.connect(_DYNAMIC_DB) as db:
        await db.execute("""CREATE TABLE IF NOT EXISTS album_charts (
            artist_key TEXT NOT NULL, album_key TEXT NOT NULL, artist TEXT, album TEXT,
            peak INTEGER, weeks INTEGER, first_year INTEGER,
            PRIMARY KEY (artist_key, album_key))""")
        await db.commit()


async def _import_b200() -> int:
    """Download the weekly Billboard 200 history and keep one row per album (best peak, most weeks)."""
    import csv, io
    _b200_job.update(status="running", message="Downloading Billboard 200 history…")
    try:
        async with httpx.AsyncClient(timeout=120.0, follow_redirects=True) as client:
            r = await client.get(_B200_URL)
            r.raise_for_status()
        agg = {}
        for row in csv.DictReader(io.StringIO(r.text)):
            artist, album = row.get("performer") or "", row.get("title") or ""
            if not artist or not album:
                continue
            key = (_artist_key(artist), _title_key(album))
            try:
                peak = int(row.get("peak_pos") or 999)
                wks = int(row.get("wks_on_chart") or 0)
                yr = int((row.get("chart_week") or "0000")[:4])
            except ValueError:
                continue
            cur = agg.get(key)
            if cur is None:
                agg[key] = [artist, album, peak, wks, yr]
            else:
                cur[2] = min(cur[2], peak); cur[3] = max(cur[3], wks); cur[4] = min(cur[4], yr)
        await _b200_migrate()
        async with aiosqlite.connect(_DYNAMIC_DB, timeout=60) as db:
            await db.execute("DELETE FROM album_charts")
            await db.executemany(
                "INSERT INTO album_charts (artist_key, album_key, artist, album, peak, weeks, first_year) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                [(k[0], k[1], *v) for k, v in agg.items()])
            await db.commit()
        await _set_setting("b200_imported_at", datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"))
        _b200_job.update(status="done", message=f"{len(agg):,} albums")
        log.info(f"Billboard 200 imported: {len(agg):,} albums")
        return len(agg)
    except Exception as e:
        log.warning(f"Billboard 200 import failed: {e}")
        _b200_job.update(status="failed", message=str(e))
        return 0


async def _b200_startup():
    """Import on first run and refresh weekly."""
    await asyncio.sleep(240)
    while True:
        try:
            last = await _get_setting("b200_imported_at")
            if not last or time.time() - _utc_epoch(last) > 7 * 86400:
                await _import_b200()
        except Exception:
            pass
        await asyncio.sleep(6 * 3600)


def _album_score(peak, weeks) -> int:
    """Peak matters most; weeks on a log scale so long-running classics keep separating (10 yrs = max)."""
    import math
    wk = min(1.0, math.log1p(weeks or 0) / math.log1p(520))
    return int(round(65 * _peak_factor(peak or 200) ** 0.8 + 35 * wk))


def _owned_album_keys(dconn) -> set:
    from app.routers.retriever import _ge_album_name
    owned = set()
    for ar, alb, fp in dconn.execute(
            "SELECT COALESCE(tag_artist, ''), album_title, file_path FROM tracks "
            "WHERE COALESCE(missing, 0) = 0"):
        name = _ge_album_name(alb, fp or "")
        if name:
            owned.add((_artist_key(ar), _title_key(name)))
    return owned


def album_chart_info_sync(artist: str, albums: list) -> dict:
    """{album title: {peak, weeks, score}} for one artist's albums (Bloodhound)."""
    ak = _artist_key(artist)
    dconn = sqlite3.connect(_DYNAMIC_DB, check_same_thread=False)
    try:
        rows = {k: (p, w) for k, p, w in dconn.execute(
            "SELECT album_key, peak, weeks FROM album_charts WHERE artist_key=?", (ak,))}
    except Exception:
        rows = {}
    finally:
        dconn.close()
    out = {}
    for title in albums:
        hit = rows.get(_title_key(title))
        if hit:
            out[title] = {"b200_peak": hit[0], "b200_weeks": hit[1], "album_score": _album_score(*hit)}
    return out


class EssentialAlbumsRequest(BaseModel):
    decades:       List[int] = []
    include_owned: bool = True
    limit:         int = 300


@router.post("/albums/essential")
async def albums_essential(req: EssentialAlbumsRequest, _=Depends(require_auth)):
    await _b200_migrate()
    await _migrate_chart_data()

    def run():
        dconn = sqlite3.connect(_DYNAMIC_DB, check_same_thread=False)
        try:
            owned = _owned_album_keys(dconn)
            rows = dconn.execute(
                "SELECT artist_key, album_key, artist, album, peak, weeks, first_year FROM album_charts").fetchall()
        finally:
            dconn.close()
        ranges = [(d, d + 9) for d in req.decades]
        out = []
        for ak, alk, artist, album, peak, weeks, yr in rows:
            if ranges and not (yr and any(lo <= yr <= hi for lo, hi in ranges)):
                continue
            is_owned = (ak, alk) in owned
            if is_owned and not req.include_owned:
                continue
            out.append({"artist": artist, "album": album, "year": yr, "b200_peak": peak,
                        "b200_weeks": weeks, "album_score": _album_score(peak, weeks), "in_library": is_owned})
        out.sort(key=lambda r: -r["album_score"])
        return {"total": len(out), "owned": sum(r["in_library"] for r in out), "results": out[:req.limit]}

    return await asyncio.get_event_loop().run_in_executor(None, run)


@router.get("/albums/status")
async def albums_status(_=Depends(require_auth)):
    await _b200_migrate()
    async with aiosqlite.connect(_DYNAMIC_DB) as db:
        n = (await (await db.execute("SELECT COUNT(*) FROM album_charts")).fetchone())[0]
    return {"albums": n, "imported_at": await _get_setting("b200_imported_at"), **_b200_job}


@router.post("/albums/import")
async def albums_import(_=Depends(require_auth)):
    if _b200_job.get("status") == "running":
        raise HTTPException(409, "Already importing.")
    n = await _import_b200()
    if not n:
        raise HTTPException(502, _b200_job.get("message") or "Import failed")
    return {"ok": True, "albums": n}
