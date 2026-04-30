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
import re
import sqlite3
import time

import aiosqlite
import httpx

from datetime import datetime
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
    # ── Broad charts — no file-tag restriction ────────────────────────────────
    "hot100":    None,
    "adultpop":  None,
    "ac":        None,
    "uk":        None,

    # ── Rock (parent) — matches any rock tag; sub-genres are more specific ────
    # Rule: parent key contains ALL sub-genre terms so selecting parent = superset.
    "rock":         {"rock","classic rock","hard rock","arena rock","alternative rock",
                     "indie rock","garage rock","punk rock","post-punk","new wave",
                     "power pop","piano rock","blues rock","southern rock","art rock",
                     "soft rock","folk rock","roots rock","heartland rock","rockabilly",
                     "psychedelic rock","progressive rock","prog rock","glam rock",
                     "pub rock","rock and roll","rock & roll"},
    # Sub-genres — discriminating terms ONLY, no bare "rock" to avoid bleed
    "classicrock":  {"classic rock","classic-rock","arena rock","blues rock","roots rock",
                     "heartland rock","southern rock","rockabilly","psychedelic rock",
                     "progressive rock","prog rock","art rock","glam rock","pub rock",
                     "rock and roll","rock & roll"},
    "hardrock":     {"hard rock","heavy rock","hard-rock"},
    "softrock":     {"soft rock","soft-rock","adult rock","mellow rock"},
    "arenarock":    {"arena rock","stadium rock","arena-rock"},
    "indierock":    {"indie rock","indie-rock"},
    "folkrock":     {"folk rock","folk-rock"},
    "southernrock": {"southern rock","southern-rock"},
    "punkrock":     {"punk rock","punk-rock","new wave","post-punk","post punk"},
    "alternative":  {"alternative rock","alternative","alt rock","alt-rock","grunge",
                     "post-grunge","britpop","shoegaze","noise rock","post-rock",
                     "math rock","emo","post-hardcore","dream pop","college rock",
                     "jangle pop"},

    # ── Pop (parent + subs) ───────────────────────────────────────────────────
    "pop":          {"pop","pop rock","synth-pop","synthpop","dance-pop","electropop",
                     "power pop","bubblegum pop","teen pop","art pop","chamber pop",
                     "indie pop","dream pop","k-pop","j-pop","europop"},
    "dancepop":     {"dance-pop","dance pop"},
    "synthpop":     {"synth-pop","synthpop","electropop"},
    "teenpop":      {"teen pop","bubblegum pop"},
    "powerpop":     {"power pop","power-pop"},
    "adultpop":     {"adult contemporary","adult pop","soft pop"},
    "indipop":      {"indie pop","indie-pop","chamber pop"},

    # ── Country (parent + subs) ───────────────────────────────────────────────
    "country":      {"country","country pop","outlaw country","alt-country",
                     "alternative country","americana","bluegrass","honky tonk",
                     "honky-tonk","western","nashville","country folk","cowboy",
                     "texas country","red dirt","cajun","zydeco","bro-country",
                     "new country","traditional country","country rock","country blues",
                     "country soul"},
    "tradcountry":  {"traditional country","classic country","honky tonk","honky-tonk",
                     "western","nashville sound"},
    "countrypop":   {"country pop","country-pop","nashville pop"},
    "outlawcountry":{"outlaw country","outlaw-country","red dirt","texas country"},
    "americana":    {"americana","bluegrass","alt-country","alternative country",
                     "country folk","cowboy"},
    "brocountry":   {"bro-country","bro country","new country"},
    "texascountry": {"texas country","red dirt","tex-mex"},

    # ── R&B / Soul (parent + subs) ────────────────────────────────────────────
    "rnb":          {"r&b","rnb","rhythm and blues","soul","neo-soul","motown","funk",
                     "quiet storm","new jack swing","hip hop soul","contemporary r&b",
                     "classic soul","southern soul","northern soul","smooth r&b"},
    "soul":         {"soul","classic soul","southern soul","northern soul","deep soul"},
    "neosoul":      {"neo-soul","neo soul"},
    "funk":         {"funk","g-funk","p-funk"},
    "motown":       {"motown","classic soul","northern soul"},
    "quietstorm":   {"quiet storm"},
    "newjack":      {"new jack swing"},

    # ── Hip-Hop (parent + subs) ───────────────────────────────────────────────
    "hiphop":       {"hip hop","hip-hop","rap","gangsta rap","trap","drill","grime",
                     "boom bap","conscious rap","east coast","west coast","southern hip hop",
                     "crunk","mumble rap","cloud rap","lo-fi hip hop","alternative hip hop",
                     "jazz rap"},
    "trap":         {"trap"},
    "boombap":      {"boom bap","boom-bap","east coast","west coast","jazz rap"},
    "gangstarap":   {"gangsta rap","gangster rap","g-rap"},
    "conscrap":     {"conscious rap","alternative hip hop","political rap"},
    "altrap":       {"alternative hip hop","lo-fi hip hop","cloud rap"},

    # ── Metal (parent + subs) ─────────────────────────────────────────────────
    "metal":        {"metal","heavy metal","death metal","thrash metal","black metal",
                     "doom metal","power metal","speed metal","glam metal","hair metal",
                     "nu-metal","nu metal","metalcore","deathcore","symphonic metal",
                     "progressive metal","groove metal","industrial metal"},
    "heavymetal":   {"heavy metal"},
    "thrashmetal":  {"thrash metal","speed metal"},
    "deathmetal":   {"death metal","deathcore"},
    "doommetal":    {"doom metal","stoner metal","sludge metal"},
    "glaemmetal":   {"glam metal","hair metal","sleaze rock"},

    # ── Dance / Electronic (parent + subs) ───────────────────────────────────
    "dance":        {"dance","edm","house","techno","trance","club","disco","dance-pop",
                     "eurodance","hi-nrg","hi nrg","dancehall","garage","uk garage",
                     "speed garage"},
    "edm":          {"edm","electronic dance"},
    "house":        {"house","deep house","tech house","progressive house"},
    "techno":       {"techno"},
    "trance":       {"trance","progressive trance"},
    "disco":        {"disco","post-disco"},
    "eurodance":    {"eurodance","euro dance","hi-nrg","hi nrg"},
    "electronic":   {"electronic","electronica","synth","ambient","industrial",
                     "downtempo","idm","glitch","breakbeat","drum and bass","dnb",
                     "dubstep","jungle","trip hop","chillout","chill out",
                     "synthwave","retrowave","vaporwave","darkwave","electro",
                     "synth-pop","synthpop"},

    # ── Folk (parent + subs) ─────────────────────────────────────────────────
    "folk":         {"folk","folk pop","contemporary folk","traditional folk","acoustic",
                     "singer-songwriter","celtic folk","folk blues","anti-folk","freak folk"},
    "tradfolk":     {"traditional folk","celtic folk","acoustic folk"},
    "indifolk":     {"indie folk","indie-folk"},
    "singersong":   {"singer-songwriter","acoustic"},

    # ── Jazz (parent + subs) ─────────────────────────────────────────────────
    "jazz":         {"jazz","bebop","swing","big band","cool jazz","hard bop",
                     "jazz fusion","fusion","soul jazz","smooth jazz","free jazz",
                     "latin jazz","bossa nova","jazz blues","jazz funk","vocal jazz",
                     "traditional jazz","dixieland"},
    "smoothjazz":   {"smooth jazz"},
    "vocaljazz":    {"vocal jazz"},
    "bebop":        {"bebop","hard bop","cool jazz"},
    "jazzfusion":   {"jazz fusion","fusion"},

    # ── Blues (parent + subs) ────────────────────────────────────────────────
    "blues":        {"blues","chicago blues","delta blues","electric blues","texas blues",
                     "soul blues","jump blues","boogie woogie","boogie","acoustic blues",
                     "swamp blues"},
    "chicagoblues": {"chicago blues","electric blues"},
    "deltablues":   {"delta blues","acoustic blues"},
    "texasblues":   {"texas blues"},

    # ── Classical (parent + subs) ─────────────────────────────────────────────
    "classical":    {"classical","classical music","orchestral","symphony","opera",
                     "chamber music","baroque","romantic","contemporary classical",
                     "neo-classical","neoclassical","concerto","sonata","choral",
                     "choir","score"},
    "orchestral":   {"orchestral","symphony","concerto","symphonic"},
    "opera":        {"opera","operatic"},
    "baroque":      {"baroque"},

    # ── Indie (standalone) ────────────────────────────────────────────────────
    "indie":        {"indie","indie rock","indie pop","indie folk","indie electronic",
                     "lo-fi","bedroom pop","twee pop","indiepop"},

    # ════════════════════════════════════════════════════════════════════════
    # CCM / Gospel family — STRICT: sub-genres use ONLY their specific terms.
    # "christian" alone does NOT appear in sub-genre sets — it only lives in
    # the parent "ccm" key. This is the Amy Grant fix: selecting "Christian Rock"
    # sends key "ccm-rock" → only matches tags containing "christian rock".
    # ════════════════════════════════════════════════════════════════════════
    "ccm":          {"christian","ccm","contemporary christian","jesus music",
                     "christian rock","christian pop","christian hip hop","christian metal",
                     "christian country","christian r&b","christian soul","christian folk",
                     "christian ac","christian adult contemporary","christian blues",
                     "worship","praise","hymn","inspirational","spiritual","sacred",
                     "gospel","southern gospel","urban gospel","traditional gospel",
                     "black gospel","new gospel","religious"},
    "gospel":       {"gospel","southern gospel","urban gospel","traditional gospel",
                     "black gospel","new gospel"},
    "ccm-ac":       {"christian ac","christian adult contemporary","contemporary christian",
                     "christian inspirational"},
    "ccm-rock":     {"christian rock","christian metal","christian hardcore",
                     "christian punk","christian hard rock"},
    "ccm-country":  {"christian country","gospel country","country gospel",
                     "christian bluegrass","christian americana"},
    "ccm-folk":     {"christian folk","gospel folk","folk gospel",
                     "christian singer-songwriter","worship folk"},
    "ccm-pop":      {"christian pop","christian pop music"},
    "ccm-hiphop":   {"christian hip hop","christian rap","gospel rap","holy hip hop"},
    "ccm-blues":    {"christian blues","gospel blues","blues gospel","sacred blues"},
    "worship":      {"worship","praise","praise and worship","praise & worship"},
    "sgospel":      {"southern gospel"},
    "ugospel":      {"urban gospel"},
    "tgospel":      {"traditional gospel","black gospel","gospel choir"},

    # ── Untagged — tracks with no genre tags ─────────────────────────────────
    "untagged":     None,  # handled specially in SQL
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


def _lookup_static(artist: str, title: str, charts: list) -> Optional[dict]:
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
    if not os.path.exists(_STATIC_DB):
        return None

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

    try:
        conn = sqlite3.connect(_STATIC_DB, check_same_thread=False)
        conn.row_factory = sqlite3.Row

        # Attach dynamic DB read-only so we can UNION chart_reference_extras.
        # Safe no-op if extras table doesn't exist yet — we wrap extras queries in try/except.
        has_extras = False
        try:
            conn.execute(f"ATTACH DATABASE '{_DYNAMIC_DB}' AS dyn")
            # Probe for table existence once per call
            probe = conn.execute(
                "SELECT name FROM dyn.sqlite_master WHERE type='table' AND name='chart_reference_extras'"
            ).fetchone()
            has_extras = bool(probe)
        except Exception:
            has_extras = False

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
                {chart_filter}
                UNION ALL
                SELECT chart_name, peak_position, weeks_on_chart, chart_year,
                       artist_norm, title_norm, data_source
                FROM dyn.chart_reference_extras
                WHERE artist_norm = ? AND title_norm = ?
                {chart_filter}
                ORDER BY peak_position ASC
                LIMIT 20
            """, union_params).fetchall()
        else:
            rows = conn.execute(f"""
                SELECT chart_name, peak_position, weeks_on_chart, chart_year,
                       artist_norm, title_norm, data_source
                FROM chart_reference
                WHERE artist_norm = ? AND title_norm = ?
                {chart_filter}
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
                    WHERE artist_norm LIKE ? {chart_filter}
                    UNION ALL
                    SELECT chart_name, peak_position, weeks_on_chart, chart_year,
                           artist_norm, title_norm, data_source
                    FROM dyn.chart_reference_extras
                    WHERE artist_norm LIKE ? {chart_filter}
                    LIMIT 400
                """, union_like).fetchall()
            else:
                candidates = conn.execute(f"""
                    SELECT chart_name, peak_position, weeks_on_chart, chart_year,
                           artist_norm, title_norm, data_source
                    FROM chart_reference
                    WHERE artist_norm LIKE ? {chart_filter}
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
            conn.close()
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

        conn.close()

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

        conn.close()
        return None

    except Exception as e:
        log.warning(f"Static DB lookup error: {e}")
        try:
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
        "label":    "Chart2000.com 2000–2024 (global)",
        "data_tag": "chart2000_monthly",
        "importer": "_import_chart2000",
    },
    {
        "id":       "tsort",
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

    def _fetch_yearend(year: int):
        try:
            return bb.ChartData(f"year-end/{year}/{billboard_slug}", timeout=25)
        except Exception:
            return None

    def _fetch_current():
        try:
            return bb.ChartData(billboard_slug, timeout=25)
        except Exception:
            return None

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
        billboard_slug="christian-songs",
        data_tag="ccm_weekly",
        chart_name="ccm",
        label="Billboard Christian Songs",
        start_year=1990,
    )


async def _import_country():
    await _import_billboard_yearend(
        billboard_slug="country-songs",
        data_tag="country_yearend",
        chart_name="country",
        label="Billboard Hot Country Songs",
        start_year=1990,
    )


async def _import_rnb():
    await _import_billboard_yearend(
        billboard_slug="r-b-hip-hop-songs",
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
        billboard_slug="dance-electronic-songs",
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
        out.append({
            "id":            src["id"],
            "label":         src["label"],
            "loaded":        count > 0,
            "entries":       count,
            "last_imported": last_at,
            "stale":         _is_stale(last_at, days=30),
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

                # Small gap between sources so we don't hammer scrapers back-to-back
                await asyncio.sleep(10)

        except Exception as e:
            log.warning(f"Scheduler loop error: {e}")

        # Check again in 6 hours
        await asyncio.sleep(6 * 60 * 60)


async def groomer_startup():
    """Called from main.py lifespan to start the weekly refresh scheduler."""
    asyncio.create_task(_scheduler_loop())
    log.info("Groomer scheduler started (auto-refresh every 7 days for current-week sources).")


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

            await db.commit()
    except Exception as e:
        log.warning(f"migration warning: {e}")


@router.post("/scan/start")
async def scan_start(req: ScanRequest, _=Depends(require_auth)):
    if _scan_job["status"] == "running":
        raise HTTPException(409, "A scan is already running. Stop it first.")

    # Migrate schema if needed (safe no-op if column already exists)
    await _migrate_chart_data()

    # Wipe previous scan results so stale data never bleeds into a new scan
    try:
        async with aiosqlite.connect(_DYNAMIC_DB) as db:
            await db.execute("DELETE FROM chart_data")
            await db.commit()
        log.info("chart_data cleared — starting fresh scan")
    except Exception as e:
        log.warning(f"Could not clear chart_data before scan: {e}")

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

async def _run_scan(req: ScanRequest):
    # Early log — ALWAYS hits before any heavy I/O so we have proof the
    # background task started, even if something catastrophic happens next.
    log.info(
        f"Scan task entered — source={req.source} "
        f"folder={req.folder_path or '(n/a)'} library={req.library_id or '(all)'} "
        f"job_id={_scan_job.get('job_id')}"
    )
    _scan_job["status"] = "running"
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
        tracks = await _fetch_tracks(req)
        if not tracks:
            _scan_job.update({"status": "done", "message": "No tracks found to scan."})
            return

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

            for idx, track in enumerate(tracks):
                if _scan_job["stop_requested"]:
                    _scan_job["status"] = "stopped"
                    _scan_job["message"] = f"Stopped at track {idx + 1} of {len(tracks)}"
                    return

                artist = track.get("artist", "").strip()
                title  = track.get("title",  "").strip()
                if not artist or not title:
                    _scan_job["processed"] += 1
                    continue

                _scan_job["message"] = f"[{idx+1}/{len(tracks)}] {artist} — {title}"

                # Check dynamic cache (chart_data rows from THIS scan)
                cached = await _check_cache(db, track, [])  # [] = any chart
                if cached:
                    _scan_job["cached"]    += 1
                    _scan_job["processed"] += 1
                    continue

                # Resolve track_id
                track_id = await _resolve_track_id(db, track)
                if track_id:
                    track["track_id"] = track_id

                # ── PULL RETRIEVER GENRE TAGS FROM DB ────────────────────────
                # genre_1/2/3 written by the Retriever live in the tracks table.
                # Media server fetch dicts don't include these — pull them now
                # so the LBZ router and Last.fm genre gate use real data.
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

                # ── CHART STATUS SKIP CACHE ───────────────────────────────────
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

                    if cs_row and cs_row["chart_status"] == "miss":
                        last_checked = cs_row["chart_last_checked"] or ""
                        is_fresh = False
                        if last_checked:
                            try:
                                checked_ts = datetime.fromisoformat(last_checked)
                                age = time.time() - checked_ts.timestamp()
                                is_fresh = age < _MISS_AGE_SECONDS
                            except Exception:
                                pass
                        if is_fresh:
                            skipped_miss += 1
                            _scan_job["cached"]    += 1
                            _scan_job["processed"] += 1
                            continue

                # ── WATERFALL ────────────────────────────────────────────────
                # Step 1: Static DB + extras — ALL charts, no restriction
                static_result = await loop.run_in_executor(
                    None, _lookup_static, artist, title, []   # [] = all charts
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

        elapsed = time.time() - (_scan_job["started_at"] or time.time())
        _scan_job.update({
            "status":  "done",
            "message": (f"Scan complete — {_scan_job['matched']:,} matched, "
                        f"{_scan_job['cached']:,} cached "
                        f"({skipped_miss:,} skip-cached misses, "
                        f"{comment_readback_hits:,} comment read-backs), "
                        f"{_scan_job['failed']:,} unmatched "
                        f"in {elapsed:.0f}s"),
        })
        log.info(f"Skip cache stats: {skipped_miss:,} misses skipped, "
                 f"{comment_readback_hits:,} comment read-backs")

    except Exception as e:
        log.exception("Groomer scan error")
        _scan_job.update({"status": "error", "message": str(e)})


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
        raise HTTPException(400, f"No {req.source} connection configured in The Kennel.")

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
    tracks = []
    prefix_server = os.environ.get("MEDIA_SERVER_MUSIC_PREFIX", "")
    prefix_docker = os.environ.get("DOCKER_MUSIC_PREFIX", "/music")
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            # Get music library section
            r = await client.get(f"{base}/library/sections?X-Plex-Token={token}",
                                 headers={"Accept":"application/json"})
            sections = r.json().get("MediaContainer",{}).get("Directory",[])
            music_sections = (
                [library_id] if library_id
                else [s["key"] for s in sections if s.get("type") == "artist"]
            )

            for sec in music_sections:
                offset = 0
                while True:
                    r = await client.get(
                        f"{base}/library/sections/{sec}/all",
                        params={"type":10,"X-Plex-Token":token,
                                "X-Plex-Container-Start":offset,
                                "X-Plex-Container-Size":500},
                        headers={"Accept":"application/json"})
                    items = r.json().get("MediaContainer",{}).get("Metadata",[])
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
                        tracks.append({
                            "track_id":       None,
                            "plex_rating_key": item.get("ratingKey",""),
                            "artist":          item.get("grandparentTitle",""),
                            "title":           item.get("title",""),
                            "album":           item.get("parentTitle",""),
                            "file_path":       fp,
                            "tag_artist":      item.get("grandparentTitle",""),
                        })
                    offset += len(items)
                    if len(items) < 500:
                        break
    except Exception as e:
        log.error(f"Plex fetch error: {e}")
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
                items = r.json().get("Items",[]) if r.is_success else []
                if not items:
                    break
                for item in items:
                    tracks.append({
                        "track_id":  None,
                        "emby_id":   item.get("Id",""),
                        "artist":    (item.get("Artists") or [""])[0],
                        "title":     item.get("Name",""),
                        "album":     item.get("Album",""),
                        "file_path": item.get("Path",""),
                        "tag_artist":(item.get("Artists") or [""])[0],
                    })
                start += len(items)
                if len(items) < 500:
                    break
    except Exception as e:
        log.error(f"Emby fetch error: {e}")
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
                items = r.json().get("Items",[]) if r.is_success else []
                if not items:
                    break
                for item in items:
                    tracks.append({
                        "track_id": None,
                        "jf_id":    item.get("Id",""),
                        "artist":   (item.get("Artists") or [""])[0],
                        "title":    item.get("Name",""),
                        "album":    item.get("Album",""),
                        "file_path":item.get("Path",""),
                        "tag_artist":(item.get("Artists") or [""])[0],
                    })
                start += len(items)
                if len(items) < 500:
                    break
    except Exception as e:
        log.error(f"Jellyfin fetch error: {e}")
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
        conditions.append("cd.peak_position >= ?"); params.append(min_peak)
    if max_peak is not None:
        conditions.append("cd.peak_position <= ?"); params.append(max_peak)
    if min_weeks is not None:
        conditions.append("cd.weeks_on_chart >= ?"); params.append(min_weeks)
    if confidence:
        conditions.append("cd.confidence = ?"); params.append(confidence)
    if genre:
        # genre param is comma-separated tree leaf keys (e.g. "ccm-rock,sgospel")
        # Empty / omitted = ALL tracks (no condition added).
        # "untagged" = special key: match tracks with no genre tag on file.
        # All other keys: match against genre_1/2/3 using whole-word boundary check
        # so "christian rock" does NOT match a tag that just says "christian".
        genre_list = [g.strip() for g in genre.split(",") if g.strip()]
        if genre_list:
            genre_clauses = []
            for g in genre_list:
                # ── Special case: untagged ────────────────────────────────
                if g == "untagged":
                    genre_clauses.append(
                        "(t.genre_1 IS NULL OR t.genre_1 = '') "
                        "AND (t.genre_2 IS NULL OR t.genre_2 = '') "
                        "AND (t.genre_3 IS NULL OR t.genre_3 = '')"
                    )
                    continue

                keywords = _GENRE_FILTER_KEYWORDS.get(g)

                # None = broad key (hot100 / ac / uk / adultpop) → no tag filter
                if keywords is None:
                    continue

                kw_list = list(keywords)
                if not kw_list:
                    continue

                # Build per-keyword whole-word LIKE clauses.
                # Strategy: pad the genre column with spaces on both sides so
                # every term (including those at start/end) has a word boundary.
                # Match pattern: '% keyword %' against ' ' || genre || ' '
                # This prevents "christian" matching inside "christian rock" etc.
                col_exprs = [
                    "(' ' || LOWER(COALESCE(t.genre_1,'')) || ' ')",
                    "(' ' || LOWER(COALESCE(t.genre_2,'')) || ' ')",
                    "(' ' || LOWER(COALESCE(t.genre_3,'')) || ' ')",
                ]
                per_kw = []
                for kw in kw_list:
                    pattern = f"% {kw} %"
                    col_parts = " OR ".join(f"{col} LIKE ?" for col in col_exprs)
                    per_kw.append(f"({col_parts})")
                    params += [pattern, pattern, pattern]

                tag_clause = "(" + " OR ".join(per_kw) + ")"

                # Chart_name fallback for untagged tracks (reliable sources only)
                fallback_chart = _CHART_SOURCE_TO_GENRE.get(g)
                no_tag = ("(t.genre_1 IS NULL OR t.genre_1 = '') "
                          "AND (t.genre_2 IS NULL OR t.genre_2 = '') "
                          "AND (t.genre_3 IS NULL OR t.genre_3 = '')")

                # CCM/gospel sub-genres: strict — no chart_name fallback
                _ccm_keys = {"ccm","ccm-ac","ccm-rock","ccm-country","ccm-folk",
                             "ccm-pop","ccm-hiphop","ccm-blues","worship",
                             "gospel","sgospel","ugospel","tgospel"}
                if g in _ccm_keys:
                    genre_clauses.append(tag_clause)
                elif fallback_chart and fallback_chart == g:
                    genre_clauses.append(
                        f"({tag_clause} OR (({no_tag}) AND cd.chart_name = ?))"
                    )
                    params.append(g)
                else:
                    genre_clauses.append(tag_clause)

            if genre_clauses:
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
                        COALESCE(cd.chart_year, NULL) AS chart_year,
                        t.title, t.file_path, t.file_format,
                        t.plex_rating_key, t.emby_id, t.jf_id,
                        t.track_id, t.genre_1, t.genre_2, t.genre_3,
                        COALESCE(t.tag_artist, a.name, '') AS tag_artist,
                        COALESCE(t.tag_album,  al.title, '') AS tag_album,
                        ROW_NUMBER() OVER (
                            PARTITION BY LOWER(t.title)
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
                    WHERE 1=1 {dedup_where}
                ) WHERE rn = 1
                ORDER BY peak_position ASC, chart_name
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
            return await _push_to_plex(base_url, token, req.playlist_name, tracks)
        user_id = extra.get("user_id", "")
        if req.server == "emby":
            return await _push_to_emby(base_url, token, user_id, req.playlist_name, tracks)
        return await _push_to_jellyfin(base_url, token, user_id, req.playlist_name, tracks)
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
    # The M3U needs the DESKTOP path (what the user's machine can open).
    # We read the Kennel path settings (server_prefix = desktop path) and
    # the Docker env vars (MEDIA_SERVER_MUSIC_PREFIX = server's raw mount).
    # Then we try each known prefix and replace with the desktop path.
    desktop_prefix = ""
    docker_prefix  = os.environ.get("DOCKER_MUSIC_PREFIX", "/music")
    server_raw     = ""  # server's own mount (from env, e.g. /media/colby/NAS1/MUSIC TAGGED)
    try:
        async with aiosqlite.connect(_DYNAMIC_DB) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT key, value FROM app_settings "
                "WHERE key IN ('path_server_prefix','path_docker_prefix')"
            ) as cur:
                rows = await cur.fetchall()
            ps = {r["key"]: r["value"] for r in rows}
            desktop_prefix = ps.get("path_server_prefix", "")
            # The docker_prefix from Kennel settings (if set) overrides env
            kp = ps.get("path_docker_prefix", "")
            if kp:
                docker_prefix = kp
    except Exception:
        pass
    # The env var MEDIA_SERVER_MUSIC_PREFIX is the path the media server
    # reports — also the second Docker volume mount's real path
    server_raw = os.environ.get("MEDIA_SERVER_MUSIC_PREFIX", "")

    def _translate_path(fp: str) -> str:
        if not desktop_prefix or not fp:
            return fp
        # Already the desktop path — no translation needed
        if fp.startswith(desktop_prefix):
            return fp
        # Docker path (/music/...) → desktop path
        if fp.startswith(docker_prefix):
            return desktop_prefix + fp[len(docker_prefix):]
        # Server raw path (from media server, may differ from desktop mount)
        # e.g. /media/NAS1/... vs /media/colby/NAS1/...
        if server_raw and fp.startswith(server_raw):
            return desktop_prefix + fp[len(server_raw):]
        # Last resort: try to find common suffix
        # e.g. stored = /media/NAS1/MUSIC TAGGED/FULL ALBUMS/...
        #      desktop = /media/colby/NAS1/MUSIC TAGGED
        # Look for the music root folder name in the stored path
        if desktop_prefix:
            # Extract the last folder name from desktop_prefix as anchor
            anchor = desktop_prefix.rstrip("/").rsplit("/", 1)[-1]
            idx = fp.find(f"/{anchor}/")
            if idx >= 0:
                return desktop_prefix + fp[idx + len(anchor) + 1:]
        return fp

    lines = ["#EXTM3U", f"#PLAYLIST:{req.playlist_name}"]
    for t in tracks:
        artist = t.get("tag_artist", "")
        title  = t.get("title", "")
        fp     = _translate_path(t.get("file_path", ""))
        lines.append(f"#EXTINF:-1,{artist} - {title}")
        lines.append(fp)

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
            return [dict(r) for r in rows]

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
    headers = {"Accept": "application/json"}
    async with httpx.AsyncClient(timeout=15.0) as client:
        r = await client.get(f"{base}/?X-Plex-Token={token}", headers=headers)
        if not r.is_success:
            raise HTTPException(502, f"Plex unreachable: HTTP {r.status_code}")
        machine_id = r.json().get("MediaContainer", {}).get("machineIdentifier", "")
        if not machine_id:
            raise HTTPException(502, "Could not get Plex machine ID")

        rating_keys = []
        not_found   = 0
        for track in tracks:
            if track.get("plex_rating_key"):
                rating_keys.append(track["plex_rating_key"]); continue
            try:
                sr = await client.get(f"{base}/search",
                    params={"query": track.get("title",""), "type": 10,
                            "X-Plex-Token": token}, headers=headers)
                if sr.is_success:
                    items = sr.json().get("MediaContainer",{}).get("Metadata",[])
                    match = next(
                        (i for i in items
                         if i.get("title","").lower() == track.get("title","").lower()
                         and track.get("tag_artist","").lower()[:8]
                             in i.get("grandparentTitle","").lower()),
                        items[0] if items else None)
                    if match and match.get("ratingKey"):
                        rating_keys.append(match["ratingKey"])
                    else:
                        not_found += 1
                else:
                    not_found += 1
            except Exception:
                not_found += 1

        if not rating_keys:
            raise HTTPException(404, "No tracks found in Plex")

        # Delete existing playlist with same name
        lr = await client.get(f"{base}/playlists?X-Plex-Token={token}", headers=headers)
        if lr.is_success:
            existing = next(
                (p for p in lr.json().get("MediaContainer",{}).get("Metadata",[])
                 if p.get("title") == name), None)
            if existing:
                await client.delete(
                    f"{base}/playlists/{existing['ratingKey']}?X-Plex-Token={token}")

        first_uri = (f"server://{machine_id}/com.plexapp.plugins.library"
                     f"/library/metadata/{rating_keys[0]}")
        cr = await client.post(f"{base}/playlists",
            params={"type":"audio","title":name,"smart":"0",
                    "uri":first_uri,"X-Plex-Token":token},
            headers=headers)
        if not cr.is_success:
            raise HTTPException(502, f"Plex create failed: HTTP {cr.status_code}")
        pl_id = (cr.json().get("MediaContainer",{}).get("Metadata",[{}])[0] or {}).get("ratingKey")
        if not pl_id:
            raise HTTPException(502, "No playlist ID from Plex")

        added = 1; failed = 0
        for i in range(0, len(rating_keys[1:]), 8):
            batch = rating_keys[1:][i:i+8]
            results = await asyncio.gather(*[
                client.put(f"{base}/playlists/{pl_id}/items",
                    params={"uri": f"server://{machine_id}/com.plexapp.plugins.library"
                                   f"/library/metadata/{k}",
                            "X-Plex-Token": token},
                    headers=headers)
                for k in batch], return_exceptions=True)
            for res in results:
                if isinstance(res, Exception) or not res.is_success: failed += 1
                else: added += 1

    return {"ok":True,"server":"plex","playlist":name,
            "added":added,"failed":failed,"not_found":not_found,"playlist_id":pl_id}


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
                a = t.get("tag_artist","")
                m = next((i for i in items
                    if i.get("Name","").lower()==t.get("title","").lower()
                    and a.lower()[:8] in
                        (i.get("AlbumArtist") or (i.get("Artists") or [""])[0]).lower()),
                    items[0] if items else None)
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
                artist = (t.get("tag_artist") or "").lower()
                title  = t.get("title","").lower()
                # Exact title + artist match first
                match = next((i for i in items
                    if i.get("Name","").lower() == title
                    and artist[:10] in
                        (i.get("AlbumArtist") or
                         (i.get("Artists") or [""])[0]).lower()), None)
                # Loose title-only fallback
                if not match:
                    match = next((i for i in items
                        if i.get("Name","").lower() == title), None)
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
