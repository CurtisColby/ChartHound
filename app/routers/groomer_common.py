# © 2026 Colby R. Curtis | ChartHound: The New World
# All Rights Reserved.
"""
ChartHound — Top Dogs shared helpers (split from groomer.py in M9b)
Chart maps, genre routing, song/artist keys, static-DB lookups, chart_data migration.
No routes. Imported by groomer.py, hitscore.py and vet.py.
"""

import difflib
import json
import logging
import os
import re
import sqlite3

import aiosqlite
import httpx

from datetime import datetime
from typing import Optional

from app.config import get_settings

log      = logging.getLogger("charthound.groomer")
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


