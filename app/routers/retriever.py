"""
ChartHound — The Retriever Router
Full Waterfall Metadata Engine: MusicBrainz → iTunes → Last.fm

M4 Polish Fixes:
- process_single_file() reads actual file tags first (Mutagen), falls back to path only
- Subfolder path validated before scan starts — clear error if not found
- Browse endpoint added for subfolder picker
- Incremental preview — tracks stored to SQLite and available during scan
- Pause toggles correctly in backend
- Artist/album parsed correctly regardless of folder depth
"""

import asyncio
import difflib
import hashlib
import json
import logging
import os
import re
import time
import aiosqlite
import httpx

from datetime import datetime, timezone
from typing import Optional, List
from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks
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

# ── Rate Limiter (iTunes Leaky Bucket — Constitution §3) ─────────────────────
class ItunesRateLimiter:
    """Hard cap: max 20 iTunes requests per minute."""
    def __init__(self, max_per_minute: int = 20):
        self.max_per_minute = max_per_minute
        self.requests = []

    async def acquire(self):
        now = time.time()
        self.requests = [t for t in self.requests if now - t < 60]
        if len(self.requests) >= self.max_per_minute:
            sleep_time = 60 - (now - self.requests[0])
            if sleep_time > 0:
                log.info(f"iTunes rate limit reached — waiting {sleep_time:.1f}s")
                await asyncio.sleep(sleep_time)
            self.requests = [t for t in self.requests if time.time() - t < 60]
        self.requests.append(time.time())

itunes_limiter = ItunesRateLimiter(max_per_minute=20)

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

CCM_MOOD_MAP = {
    "praise & worship": ["Uplifting","Worshipful","Spiritual","Energetic"],
    "worship":          ["Worshipful","Peaceful","Spiritual","Uplifting"],
    "gospel":           ["Joyful","Uplifting","Soulful","Energetic"],
    "christian rock":   ["Energetic","Powerful","Uplifting","Anthemic"],
    "ccm":              ["Uplifting","Inspirational","Positive","Peaceful"],
    "christian pop":    ["Uplifting","Positive","Joyful","Inspirational"],
    "southern gospel":  ["Joyful","Soulful","Warm","Uplifting"],
    "urban gospel":     ["Energetic","Joyful","Soulful","Powerful"],
}

FILTER_TAGS = {
    "seen live","awesome","favorite","love","amazing","beautiful","cool",
    "great","best","good","favorite songs","my favorites","favourite",
    "favourites","favorites","owned","have","want",
}

AUDIO_EXTS = {".mp3",".flac",".m4a",".aac",".ogg",".opus",".wma",".wav",".aiff",".ape",".wv"}

# ── Fix B: Hard genre blacklist — these NEVER get written to music files ──────
# These genres are valid on MusicBrainz/Discogs but wrong for 99% of music libraries.
GENRE_BLACKLIST = {
    "musical", "stage & screen", "soundtrack", "children's", "children",
    "holiday", "comedy", "spoken word", "audio drama", "karaoke",
    "new age", "field recording", "ringtone", "advertising",
    "non-music", "interview", "live score",
    "album rock", "composed", "music", "ballad", "cover", "tribute",
}

# ── Artist genre fingerprint ──────────────────────────────────────────────────
_artist_genre_cache: dict = {}

def _norm_artist(s: str) -> str:
    s = s.lower().strip()
    s = re.sub(r"[^\w\s]", "", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s

def update_artist_fingerprint(artist: str, genres: list):
    key = _norm_artist(artist)
    if key not in _artist_genre_cache:
        _artist_genre_cache[key] = {}
    for g in (genres or []):
        norm = g.lower().strip()
        _artist_genre_cache[key][norm] = _artist_genre_cache[key].get(norm, 0) + 1

def apply_artist_fingerprint(artist: str, proposed: list) -> list:
    key = _norm_artist(artist)
    cache = _artist_genre_cache.get(key, {})
    total = sum(cache.values()) or 1
    if total < 3 or not proposed:
        return proposed
    top_count = max(cache.values()) if cache else 0
    if top_count / total < 0.5:
        return proposed
    fp_set = set(cache.keys())
    result = [g for g in proposed if g.lower().strip() in fp_set]
    for g, _ in sorted(cache.items(), key=lambda x: -x[1]):
        if len(result) >= 3: break
        if g not in {x.lower() for x in result}:
            result.append(g.title())
    return result[:3]

# ── Fix D: Genre-tag → Mood mapping (covers mainstream Last.fm tags) ──────────
# Last.fm tags for most tracks are genre words, not mood words.
# This maps common genre tags to the mood they imply.
GENRE_TO_MOOD = {
    # Rock family
    "classic rock":      "Energetic",
    "hard rock":         "Intense",
    "soft rock":         "Chill",
    "alternative rock":  "Energetic",
    "indie rock":        "Energetic",
    "psychedelic rock":  "Dreamy",
    "progressive rock":  "Epic",
    "punk rock":         "Intense",
    "glam rock":         "Upbeat",
    "blues rock":        "Soulful",
    "southern rock":     "Energetic",
    "arena rock":        "Energetic",
    "rock":              "Energetic",
    # Pop family
    "pop":               "Upbeat",
    "pop rock":          "Upbeat",
    "synth pop":         "Upbeat",
    "power pop":         "Upbeat",
    "teen pop":          "Upbeat",
    "indie pop":         "Upbeat",
    "dream pop":         "Dreamy",
    "bubblegum":         "Happy",
    # Country family
    "country":           "Warm",
    "country pop":       "Upbeat",
    "country rock":      "Energetic",
    "bluegrass":         "Joyful",
    "americana":         "Warm",
    "outlaw country":    "Intense",
    # R&B / Soul / Funk
    "r&b":               "Soulful",
    "soul":              "Soulful",
    "funk":              "Upbeat",
    "neo soul":          "Chill",
    "motown":            "Joyful",
    "rhythm and blues":  "Soulful",
    # Hip-Hop / Rap
    "hip hop":           "Energetic",
    "hip-hop":           "Energetic",
    "rap":               "Energetic",
    "trap":              "Intense",
    "gangsta rap":       "Intense",
    # Electronic / Dance
    "electronic":        "Energetic",
    "edm":               "Energetic",
    "house":             "Energetic",
    "techno":            "Intense",
    "trance":            "Euphoric",
    "dubstep":           "Intense",
    "synthwave":         "Nostalgic",
    "chillwave":         "Chill",
    # Jazz / Blues
    "jazz":              "Chill",
    "blues":             "Soulful",
    "smooth jazz":       "Chill",
    "bebop":             "Energetic",
    # Metal
    "metal":             "Intense",
    "heavy metal":       "Intense",
    "death metal":       "Intense",
    "black metal":       "Dark",
    "power metal":       "Epic",
    # Folk / Acoustic
    "folk":              "Peaceful",
    "acoustic":          "Peaceful",
    "singer-songwriter": "Emotional",
    "folk rock":         "Warm",
    # Classical
    "classical":         "Peaceful",
    "orchestral":        "Epic",
    "chamber music":     "Peaceful",
    # General mood boosters
    "80s":               "Nostalgic",
    "90s":               "Nostalgic",
    "70s":               "Nostalgic",
    "60s":               "Nostalgic",
    "oldies":            "Nostalgic",
}

# ── Pydantic Models ───────────────────────────────────────────────────────────
class ScanRequest(BaseModel):
    scope: str = "library"
    subfolder: Optional[str] = None
    subfolders: Optional[List[str]] = None
    mode: str = "preview"
    chunk_size: int = 20
    media_server: str = "plex"

class ApproveRequest(BaseModel):
    job_id: int
    track_ids: List[int]
    write_art: bool = True

class ReadTagsRequest(BaseModel):
    """Request to read physical file tags — no DB, no APIs, instant."""
    paths: List[str]        # list of folder paths to read
    page_size: int = 20     # how many folder-cards to return per call
    offset: int = 0         # pagination offset — start at 0, increment by page_size

class AlbumTagRequest(BaseModel):
    """For album-level lookup — one MusicBrainz release-group call per album."""
    artist: str
    album: str
    year: Optional[int] = None

class ManualOverrideRequest(BaseModel):
    """Manual genre/mood override for selected track IDs or file paths."""
    track_ids: List[int] = []
    file_paths: Optional[List[str]] = None  # from album tagger direct writes
    genres: List[str]
    moods: List[str] = []
    year: Optional[int] = None

class AlbumOverrideRequest(BaseModel):
    """
    NEW — Album Tagger enhanced override.
    Writes Album, Album Artist, Year, Compilation flag, Genre, Mood
    and optionally clears MusicBrainz IDs — all in one pass.
    Per-track file_paths list controls which files in the folder are written.
    """
    file_paths: List[str]                   # tracks to write (subset of folder)
    album_name: Optional[str] = None        # new Album tag value
    album_artist: Optional[str] = None      # new Album Artist tag value
    year: Optional[int] = None
    is_compilation: bool = False            # write COMPILATION=1 flag
    clear_mbids: bool = True               # wipe MusicBrainz ID tags
    genres: List[str] = []
    moods: List[str] = []


# ══════════════════════════════════════════════════════════════════════════════
#  TAG READING — Read actual file tags first, path fallback second
# ══════════════════════════════════════════════════════════════════════════════

def read_tags_from_file(filepath: str) -> dict:
    """
    Read existing tags from a music file using Mutagen.
    Returns dict with artist, album, title, year, genre, mood, comment.
    This is the PRIMARY source for artist/title — path is fallback only.
    """
    result = {
        "artist": "", "albumartist": "", "album": "",
        "title": "", "year": None, "genre": "", "mood": "", "comment": ""
    }
    try:
        ext = os.path.splitext(filepath)[1].lower()

        if ext == ".mp3":
            from mutagen.id3 import ID3, ID3NoHeaderError
            try:
                tags = ID3(filepath)
                result["artist"]      = str(tags.get("TPE1", [""])[0]).strip()
                result["albumartist"] = str(tags.get("TPE2", [""])[0]).strip()
                result["album"]       = str(tags.get("TALB", [""])[0]).strip()
                result["title"]       = str(tags.get("TIT2", [""])[0]).strip()
                result["genre"]       = str(tags.get("TCON", [""])[0]).strip()
                comm = tags.get("COMM::eng")
                if comm: result["comment"] = str(comm)[0].strip()
                # Year: prefer TDOR (original release) over TDRC (recording/reissue date)
                year = None
                for frame in ["TDOR", "TORY", "TDRC"]:
                    val = tags.get(frame)
                    if val:
                        yr_str = str(val)[0][:4] if hasattr(val, '__iter__') else str(val)[:4]
                        if yr_str.isdigit() and int(yr_str) > 1900:
                            year = int(yr_str)
                            break
                result["year"] = year
            except (ID3NoHeaderError, Exception):
                pass

        elif ext == ".flac":
            from mutagen.flac import FLAC
            f = FLAC(filepath)
            result["artist"]      = "; ".join(f.get("artist", [])).strip()
            result["albumartist"] = "; ".join(f.get("albumartist", [])).strip()
            result["album"]       = "; ".join(f.get("album", [])).strip()
            result["title"]       = "; ".join(f.get("title", [])).strip()
            result["genre"]       = "; ".join(f.get("genre", [])).strip()
            result["comment"]     = "; ".join(f.get("comment", [])).strip()
            # Prefer originalyear/originaldate over date (avoids reissue dates)
            orig_year = "; ".join(f.get("originalyear", []))
            orig_date = "; ".join(f.get("originaldate", []))
            date = "; ".join(f.get("date", []))
            year_str = orig_year or (orig_date[:4] if orig_date else "") or date
            if year_str and year_str[:4].isdigit():
                result["year"] = int(year_str[:4])

        elif ext in (".m4a", ".aac", ".mp4"):
            from mutagen.mp4 import MP4
            f = MP4(filepath)
            result["artist"]  = "; ".join(f.get("\xa9ART", [])).strip()
            result["album"]   = "; ".join(f.get("\xa9alb", [])).strip()
            result["title"]   = "; ".join(f.get("\xa9nam", [])).strip()
            result["genre"]   = "; ".join(f.get("\xa9gen", [])).strip()
            day = "; ".join(f.get("\xa9day", []))
            if day and day[:4].isdigit():
                result["year"] = int(day[:4])

        elif ext in (".ogg", ".opus"):
            from mutagen import File as MFile
            f = MFile(filepath)
            if f:
                result["artist"] = "; ".join(f.get("artist", [])).strip()
                result["album"]  = "; ".join(f.get("album", [])).strip()
                result["title"]  = "; ".join(f.get("title", [])).strip()
                result["genre"]  = "; ".join(f.get("genre", [])).strip()
                date = "; ".join(f.get("date", []))
                if date and date[:4].isdigit():
                    result["year"] = int(date[:4])

        else:
            # Try easy=True for WMA and other formats
            from mutagen import File as MFile
            f = MFile(filepath, easy=True)
            if f:
                result["artist"] = "; ".join(f.get("artist", [])).strip()
                result["album"]  = "; ".join(f.get("album", [])).strip()
                result["title"]  = "; ".join(f.get("title", [])).strip()

    except Exception as e:
        log.debug(f"Tag read failed for {filepath}: {e}")

    return result


def parse_artist_title_from_path(filepath: str, music_prefix: str) -> dict:
    """
    Fallback: parse artist/album/title from folder structure.
    Handles variable depth — looks for deepest reasonable artist/album/title.
    Structure assumed: /music/[category/]/Artist/Album/track.ext
    """
    rel = filepath.replace(music_prefix, "").strip("/")
    parts = rel.split("/")
    fname = os.path.splitext(parts[-1])[0] if parts else ""

    # Clean track number from filename
    title = re.sub(r"^\d+[\s\-_.]+", "", fname).strip()

    # Work backwards from the file: title=file, album=parent, artist=grandparent
    if len(parts) >= 3:
        artist = parts[-3]
        album  = parts[-2]
    elif len(parts) == 2:
        artist = parts[-2]
        album  = ""
    else:
        artist = ""
        album  = ""

    return {"artist": artist.strip(), "album": album.strip(), "title": title}


# ══════════════════════════════════════════════════════════════════════════════
#  WATERFALL ENGINE
# ══════════════════════════════════════════════════════════════════════════════

async def fetch_musicbrainz(artist: str, title: str, album: str = "") -> dict:
    """Primary: MusicBrainz — genres, year, MBID."""
    result = {"year": None, "genres": [], "mbid": None, "confidence": "low"}
    if not artist or not title:
        return result
    try:
        # Simple query returns better tag data than strict field qualifiers
        query = f"{artist} {title}"
        headers = {"User-Agent": "ChartHound/1.0 (https://github.com/CurtisColby/ChartHound)"}
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get("https://musicbrainz.org/ws/2/recording/",
                                 params={"query": query, "fmt": "json", "limit": 5},
                                 headers=headers)
        if not r.is_success:
            return result
        data = r.json()
        recordings = data.get("recordings", [])
        if not recordings:
            return result

        # Fix A: Fuzzy artist matching — hard-reject wrong artists
        # Strip punctuation for fairer comparison (AC/DC → acdc, R.E.M. → rem)
        def _norm(s: str) -> str:
            s = s.lower().strip()
            s = re.sub(r"[^\w\s]", "", s)   # remove punctuation
            s = re.sub(r"\s+", " ", s)       # collapse whitespace
            s = re.sub(r"^the\s+", "", s)    # strip leading "the"
            return s.strip()

        def _artist_score(query: str, candidate: str) -> float:
            q, c = _norm(query), _norm(candidate)
            if not q or not c:
                return 0.0
            # Exact match after normalisation → perfect score
            if q == c:
                return 1.0
            # One is a substring of the other (handles "AC DC" in "AC DC feat. Bon Scott")
            if q in c or c in q:
                return 0.85
            return difflib.SequenceMatcher(None, q, c).ratio()

        ARTIST_THRESHOLD = 0.55   # Below this → wrong artist, reject tags

        best_rec = None
        best_tags = []
        best_score = 0.0
        artist_lower = artist.lower()

        for rec in recordings:
            rec_artists = [a.get("name", "") for a in rec.get("artist-credit", [])
                          if isinstance(a, dict)]
            if not rec_artists:
                continue
            score = max(_artist_score(artist, ra) for ra in rec_artists)
            if score < ARTIST_THRESHOLD:
                log.debug(f"MB artist mismatch ({score:.2f}): '{artist}' vs {rec_artists} — skipped")
                continue
            tags = rec.get("tags", [])
            # Prefer higher artist match score; break ties by tag count
            if score > best_score or (score == best_score and len(tags) > len(best_tags)):
                best_score = score
                best_tags = tags
                best_rec = rec

        # If nothing passed the threshold, use first result but discard its tags entirely
        if not best_rec:
            best_rec = recordings[0]
            best_tags = []
            log.debug(f"No artist match above threshold for '{artist}' — using first result, tags discarded")

        # Phase 2: Direct lookup with genres included (Gemini recommendation)
        # Release Group level has better genre coverage than Recording level
        if best_rec and not best_tags:
            rec_id = best_rec.get("id")
            if rec_id:
                try:
                    async with httpx.AsyncClient(timeout=10.0) as client2:
                        r2 = await client2.get(
                            f"https://musicbrainz.org/ws/2/recording/{rec_id}",
                            params={"inc": "tags+genres+release-groups", "fmt": "json"},
                            headers=headers)
                    if r2.is_success:
                        full_data = r2.json()
                        best_tags = full_data.get("genres", []) or full_data.get("tags", [])
                        # Also check release group tags
                        if not best_tags:
                            rgs = full_data.get("release-group", {})
                            best_tags = rgs.get("genres", []) or rgs.get("tags", [])
                except Exception:
                    pass
        result["mbid"] = best_rec.get("id")
        result["confidence"] = "high" if best_rec.get("score", 0) >= 90 else "medium"

        # Fix C: Year — prefer original official Album release, not singles or reissues
        releases = best_rec.get("releases", [])

        def _release_year(rel: dict) -> Optional[int]:
            d = rel.get("date", "")
            if d and re.match(r'\d{4}', d):
                return int(d[:4])
            return None

        # First pass: official studio albums only
        album_years = [
            _release_year(rel) for rel in releases
            if rel.get("status", "").lower() == "official"
            and rel.get("release-group", {}).get("primary-type", "").lower() == "album"
            and _release_year(rel)
        ]
        # Second pass: any official release (catches singles that predate the album)
        if not album_years:
            album_years = [
                _release_year(rel) for rel in releases
                if rel.get("status", "").lower() == "official"
                and _release_year(rel)
            ]
        # Last resort: anything with a date
        if not album_years:
            album_years = [_release_year(rel) for rel in releases if _release_year(rel)]

        if album_years:
            result["year"] = min(yr for yr in album_years if yr and 1900 < yr < 2030)

        # Tags — also check release group tags
        if not best_tags and releases:
            best_tags = releases[0].get("release-group", {}).get("tags", [])
        best_tags.sort(key=lambda t: t.get("count", 0), reverse=True)
        result["genres"] = [t["name"].title() for t in best_tags[:5] if t.get("count", 0) >= 0]

        await asyncio.sleep(0.1)
    except Exception as e:
        log.debug(f"MusicBrainz error for {artist} - {title}: {e}")
    return result


async def fetch_itunes(artist: str, title: str, album: str = "") -> dict:
    """Secondary: iTunes — artwork, genre, year."""
    result = {"year": None, "genres": [], "art_url": None}
    if not artist or not title:
        return result
    try:
        await itunes_limiter.acquire()
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get("https://itunes.apple.com/search", params={
                "term": f"{artist} {title}", "media": "music",
                "entity": "song", "limit": 5
            })
        if not r.is_success:
            return result
        results = r.json().get("results", [])
        if not results:
            return result

        best = None
        for item in results:
            ia = item.get("artistName", "").lower()
            it = item.get("trackName", "").lower()
            if (artist.lower() in ia or ia in artist.lower()) and \
               (title.lower() in it or it in title.lower()):
                best = item
                break
        if not best:
            best = results[0]

        date = best.get("releaseDate", "")
        if date:
            result["year"] = int(date[:4])
        genre = best.get("primaryGenreName", "")
        if genre:
            result["genres"] = [genre]
        art = best.get("artworkUrl100", "")
        if art:
            result["art_url"] = art.replace("100x100bb", "600x600bb")
    except Exception as e:
        log.debug(f"iTunes error for {artist} - {title}: {e}")
    return result


async def fetch_lastfm(artist: str, title: str, lastfm_key: str) -> dict:
    """Tertiary: Last.fm — mood tags, chart detection."""
    result = {"moods": [], "charts": [], "peak": None, "weeks": 0, "listeners": 0}
    if not lastfm_key or not artist or not title:
        return result
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get("https://ws.audioscrobbler.com/2.0/", params={
                "method": "track.getInfo", "api_key": lastfm_key,
                "artist": artist, "track": title, "format": "json", "autocorrect": "1"
            })
        if not r.is_success:
            return result
        data = r.json()
        if data.get("error"):
            return result

        track = data.get("track", {})
        result["listeners"] = int(track.get("listeners", 0))
        raw_tags = track.get("toptags", {}).get("tag", [])

        moods = []
        charts = []
        for tag in raw_tags[:15]:
            name = tag.get("name", "").lower().strip()
            if name in FILTER_TAGS or len(name) < 2:
                continue
            # Chart detection
            chart_map = {
                "hot100":  ["hot 100","hot100","billboard"],
                "adultpop":["adult pop","top 40"],
                "uk":      ["uk chart","uk single","british chart"],
                "country": ["country chart","hot country"],
                "rnb":     ["r&b","rnb","rhythm and blues"],
                "dance":   ["dance chart","electronic chart"],
                "rock":    ["rock chart","mainstream rock"],
                "ccm":     ["christian","ccm","contemporary christian"],
                "worship": ["worship","praise"],
                "gospel":  ["gospel"],
            }
            for chart_key, keywords in chart_map.items():
                if any(kw in name for kw in keywords):
                    if chart_key not in charts:
                        charts.append(chart_key)
            # Mood extraction — two passes:
            # Pass 1: direct mood keywords in the tag name
            mood_map = {
                "happy":        "Happy",      "sad":          "Melancholic",
                "melancholic":  "Melancholic","energetic":    "Energetic",
                "chill":        "Chill",      "relaxing":     "Relaxing",
                "upbeat":       "Upbeat",     "romantic":     "Romantic",
                "angry":        "Intense",    "peaceful":     "Peaceful",
                "uplifting":    "Uplifting",  "dark":         "Dark",
                "nostalgic":    "Nostalgic",  "party":        "Party",
                "workout":      "Energetic",  "calm":         "Calm",
                "epic":         "Epic",       "emotional":    "Emotional",
                "soulful":      "Soulful",    "joyful":       "Joyful",
                "powerful":     "Powerful",   "disco":        "Energetic",
                "dance":        "Energetic",  "fun":          "Happy",
                "feel good":    "Happy",      "feelgood":     "Happy",
                "groovy":       "Upbeat",     "summer":       "Upbeat",
                "driving":      "Energetic",  "sexy":         "Romantic",
                "sensual":      "Romantic",   "mellow":       "Chill",
                "laid back":    "Chill",      "aggressive":   "Intense",
                "heavy":        "Intense",    "depressing":   "Melancholic",
                "heartbreak":   "Melancholic","love":         "Romantic",
                "cheerful":     "Happy",      "atmospheric":  "Peaceful",
                "ambient":      "Peaceful",   "dreamy":       "Dreamy",
                "euphoric":     "Uplifting",  "inspirational":"Uplifting",
                "motivating":   "Uplifting",  "anthemic":     "Epic",
                "intense":      "Intense",    "warm":         "Warm",
                "bittersweet":  "Melancholic","haunting":     "Dark",
                "triumphant":   "Uplifting",  "rebellious":   "Intense",
            }
            for kw, mood in mood_map.items():
                if kw in name and mood not in moods:
                    moods.append(mood)
                    break  # one mood per tag is enough

            # Pass 2: Fix D — genre-style tags → mood (covers "classic rock", "hard rock", etc.)
            if not any(kw in name for kw in mood_map):
                genre_mood = GENRE_TO_MOOD.get(name)
                if genre_mood and genre_mood not in moods:
                    moods.append(genre_mood)

        result["moods"] = moods[:3]
        result["charts"] = charts

        listeners = result["listeners"]
        if listeners >= 5000000 and "hot100" not in result["charts"]:
            result["charts"].append("hot100")
    except Exception as e:
        log.debug(f"Last.fm error for {artist} - {title}: {e}")
    return result


async def fetch_discogs(artist: str, title: str, discogs_token: str) -> dict:
    """Quaternary: Discogs — excellent genre/style data, especially for classic artists."""
    result = {"year": None, "genres": [], "styles": []}
    if not discogs_token or not artist or not title:
        return result
    try:
        headers = {
            "User-Agent": "ChartHound/1.0 +https://github.com/CurtisColby/ChartHound",
            "Authorization": f"Discogs token={discogs_token}"
        }
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get("https://api.discogs.com/database/search", 
                params={"artist": artist, "track": title, "type": "release", "per_page": 3},
                headers=headers)
        if not r.is_success:
            return result
        results = r.json().get("results", [])
        if not results:
            # Try broader search
            async with httpx.AsyncClient(timeout=10.0) as client:
                r = await client.get("https://api.discogs.com/database/search",
                    params={"q": f"{artist} {title}", "type": "release", "per_page": 3},
                    headers=headers)
            if not r.is_success:
                return result
            results = r.json().get("results", [])
        if not results:
            return result

        best = results[0]
        # Genres and styles
        genres = best.get("genre", [])
        styles = best.get("style", [])
        result["genres"] = genres[:3]
        result["styles"] = styles[:3]
        # Year
        year = best.get("year")
        if year:
            try:
                yr = int(str(year)[:4])
                if 1900 < yr < 2030:
                    result["year"] = yr
            except Exception:
                pass
        await asyncio.sleep(0.2)  # Discogs rate limit courtesy
    except Exception as e:
        log.debug(f"Discogs error for {artist} - {title}: {e}")
    return result


async def fetch_deezer(artist: str, title: str) -> dict:
    """Fallback: Deezer — no API key needed, good genre coverage."""
    result = {"year": None, "genres": []}
    if not artist or not title:
        return result
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get("https://api.deezer.com/search",
                params={"q": f'artist:"{artist}" track:"{title}"', "limit": 3})
        if not r.is_success:
            return result
        data = r.json().get("data", [])
        if not data:
            return result

        track = data[0]
        # Get album for genre info
        album_id = track.get("album", {}).get("id")
        if album_id:
            async with httpx.AsyncClient(timeout=10.0) as client:
                r2 = await client.get(f"https://api.deezer.com/album/{album_id}")
            if r2.is_success:
                album_data = r2.json()
                genres = album_data.get("genres", {}).get("data", [])
                result["genres"] = [g["name"] for g in genres[:3]]
                # Year from release date
                release = album_data.get("release_date", "")
                if release and release[:4].isdigit():
                    yr = int(release[:4])
                    if 1900 < yr < 2030:
                        result["year"] = yr
    except Exception as e:
        log.debug(f"Deezer error for {artist} - {title}: {e}")
    return result


async def fetch_listenbrainz(mbid: Optional[str], artist: str, title: str) -> dict:
    """
    Fix E: ListenBrainz — free, no API key, mood tags from real listeners.
    Uses MBID from MusicBrainz for precise lookup; falls back to name search.
    Returns: {"moods": [...up to 3 mood strings...]}
    """
    result = {"moods": []}
    if not mbid and (not artist or not title):
        return result

    # ListenBrainz mood tags are community-applied — very reliable for popular tracks
    LBZ_MOOD_TAGS = {
        "happy", "sad", "energetic", "chill", "relaxing", "upbeat", "romantic",
        "angry", "peaceful", "uplifting", "dark", "nostalgic", "party", "calm",
        "epic", "emotional", "soulful", "joyful", "powerful", "groovy", "dreamy",
        "intense", "warm", "bittersweet", "haunting", "triumphant", "rebellious",
        "melancholic", "atmospheric", "anthemic", "fun", "motivating", "inspirational",
    }
    # Map ListenBrainz raw tags to our capitalised mood labels
    LBZ_TAG_MAP = {t: t.title() for t in LBZ_MOOD_TAGS}
    # Add some synonyms they use
    LBZ_TAG_MAP.update({
        "feel good": "Happy", "feelgood": "Happy", "mellow": "Chill",
        "laid back": "Chill", "driving": "Energetic", "workout": "Energetic",
        "heartbreak": "Melancholic", "depressing": "Melancholic",
        "euphoric": "Uplifting", "summer": "Upbeat",
    })

    try:
        headers = {"User-Agent": "ChartHound/1.0 (https://github.com/CurtisColby/ChartHound)"}
        moods = []

        if mbid:
            # Correct endpoint: recording_mbids (plural), no inc= param needed
            # Response structure: { "<mbid>": { "recording": {...}, "tag": { "recording": [...] } } }
            async with httpx.AsyncClient(timeout=8.0) as client:
                r = await client.get(
                    "https://api.listenbrainz.org/1/metadata/recording/",
                    params={"recording_mbids": mbid},
                    headers=headers
                )
            if r.is_success:
                data = r.json()
                # Tags are nested under the MBID key
                recording_data = data.get(mbid, {})
                tags = recording_data.get("tag", {}).get("recording", [])
                for tag in sorted(tags, key=lambda t: t.get("count", 0), reverse=True)[:20]:
                    name = tag.get("tag", "").lower().strip()
                    mood = LBZ_TAG_MAP.get(name)
                    if mood and mood not in moods:
                        moods.append(mood)
                    if len(moods) >= 3:
                        break
            else:
                log.debug(f"ListenBrainz {r.status_code} for mbid={mbid}")

        result["moods"] = moods[:3]
        if moods:
            log.debug(f"ListenBrainz moods for {artist} - {title}: {moods}")
    except Exception as e:
        log.debug(f"ListenBrainz error for {artist} - {title}: {e}")
    return result



def merge_genres(sources: List[List[str]], top_n: int = 3) -> List[str]:
    BROAD = {
        "rock","pop","country","r&b","jazz","classical","electronic",
        "dance","folk","blues","soul","reggae","metal","alternative","indie",
        "punk","gospel","christian","worship","hard rock","soft rock",
        "adult contemporary","ccm",
    }
    # Genres that are suspicious when mixed with clearly different genres
    # e.g. Hip Hop appearing on a Soft Rock track
    SUSPICIOUS_CROSS = {
        "hip hop": {"rock", "soft rock", "folk rock", "country", "classical"},
        "rap": {"rock", "soft rock", "folk rock", "country", "classical"},
        "metal": {"pop", "country", "r&b", "soul", "gospel"},
        "classical": {"hip hop", "rap", "metal", "punk"},
    }
    votes = {}
    for src_idx, source_genres in enumerate(sources):
        for genre in (source_genres or []):
            if not genre or len(genre) < 2:
                continue
            norm = genre.lower().strip()
            # Fix B: Hard blacklist — drop these before they can ever be voted in
            if norm in GENRE_BLACKLIST:
                log.debug(f"Blacklisted genre dropped: '{genre}'")
                continue
            if norm not in votes:
                votes[norm] = {"display": genre, "count": 0,
                               "first_src": src_idx, "broad": norm in BROAD}
            votes[norm]["count"] += 1

    # Filter out suspicious cross-genre tags
    all_norms = set(votes.keys())
    filtered = {}
    for norm, v in votes.items():
        suspicious = False
        if norm in SUSPICIOUS_CROSS:
            conflicting = SUSPICIOUS_CROSS[norm]
            if any(c in all_norms for c in conflicting) and v["count"] == 1:
                suspicious = True
        if not suspicious:
            filtered[norm] = v

    sorted_genres = sorted(
        filtered.values(),
        key=lambda v: (0 if v["broad"] else 1, -v["count"], v["first_src"])
    )
    return [v["display"] for v in sorted_genres[:top_n]]


def get_ccm_moods(genres: List[str]) -> List[str]:
    """Return mood tags for Christian/Gospel/Worship genres."""
    CCM_MOOD_MAP = {
        "worship":   ["Reverent", "Peaceful", "Uplifting"],
        "gospel":    ["Joyful", "Uplifting", "Soulful"],
        "christian": ["Uplifting", "Peaceful"],
        "ccm":       ["Uplifting", "Joyful"],
        "praise":    ["Joyful", "Reverent", "Uplifting"],
    }
    for genre in genres:
        for keyword, moods in CCM_MOOD_MAP.items():
            if keyword in genre.lower():
                return moods[:3]
    return []


async def fetch_musicbrainz_album(artist: str, album: str) -> dict:
    """
    Album-level MusicBrainz release-group lookup.
    More reliable than per-track — returns genres agreed on at album level.
    """
    result = {"genres": [], "year": None, "confidence": "low", "album_id": None}
    if not artist or not album:
        return result
    try:
        headers = {"User-Agent": "ChartHound/1.0 (https://github.com/CurtisColby/ChartHound)"}
        query = f'artist:"{artist}" release:"{album}"'
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(
                "https://musicbrainz.org/ws/2/release-group/",
                params={"query": query, "fmt": "json", "limit": 3},
                headers=headers)
        if not r.is_success:
            return result
        data = r.json()
        rgs = data.get("release-groups", [])
        if not rgs:
            return result

        best = rgs[0]
        result["album_id"]  = best.get("id")
        result["confidence"] = "high" if best.get("score", 0) >= 85 else "medium"

        # Fetch full release-group with tags
        rg_id = best.get("id")
        if rg_id:
            async with httpx.AsyncClient(timeout=10.0) as client:
                r2 = await client.get(
                    f"https://musicbrainz.org/ws/2/release-group/{rg_id}",
                    params={"inc": "tags+genres", "fmt": "json"},
                    headers=headers)
            if r2.is_success:
                rg_data = r2.json()
                tags = rg_data.get("genres", []) or rg_data.get("tags", [])
                tags.sort(key=lambda t: t.get("count", 0), reverse=True)
                raw = [t.get("name", "").title() for t in tags[:6]]
                result["genres"] = merge_genres([raw], top_n=3)

        # Year from first-release-date
        frd = best.get("first-release-date", "")
        if frd and len(frd) >= 4 and frd[:4].isdigit():
            yr = int(frd[:4])
            if 1900 < yr < 2030:
                result["year"] = yr

        await asyncio.sleep(0.1)
    except Exception as e:
        log.debug(f"MB album lookup error for {artist}/{album}: {e}")
    return result


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

def get_file_hash(filepath: str) -> str:
    try:
        h = hashlib.md5()
        with open(filepath, "rb") as f:
            for chunk in iter(lambda: f.read(8192), b""):
                h.update(chunk)
        return h.hexdigest()
    except Exception:
        return ""


def write_tags(filepath: str, genres: List[str], moods: List[str],
               year: Optional[int], comment: str, star_rating: int = 0) -> bool:
    """
    Write metadata to physical file via Mutagen.
    WIPE and REPLACE — never append. Strip APE tags from MP3.
    File-First standard — Constitution §2.
    Uses real server path via second volume mount for reliable writes.
    """
    try:
        ext = os.path.splitext(filepath)[1].lower()

        # Translate Docker /music path to real server path
        # The real server path is mounted at itself inside Docker
        real_path = filepath
        if settings.media_server_music_prefix and settings.docker_music_prefix:
            if filepath.startswith(settings.docker_music_prefix):
                real_path = settings.media_server_music_prefix + \
                            filepath[len(settings.docker_music_prefix):]

        if ext == ".mp3":
            from mutagen.id3 import (ID3, TCON, TDRC, TXXX, COMM, POPM, ID3NoHeaderError)
            try:
                try:
                    from mutagen.apev2 import delete as ape_delete
                    ape_delete(real_path)
                except Exception:
                    pass
                tags = ID3(real_path)
            except ID3NoHeaderError:
                tags = ID3()

            for key in list(tags.keys()):
                if any(key.startswith(p) for p in ["TCON","TDRC","COMM","POPM"]) or \
                   (key.startswith("TXXX") and any(x in key.lower() for x in ["mood","genre","chart"])):
                    del tags[key]

            if genres:  tags["TCON"] = TCON(encoding=3, text=["; ".join(genres)])
            if moods:   tags["TXXX:MOOD"] = TXXX(encoding=3, desc="MOOD", text=["; ".join(moods)])
            if year:    tags["TDRC"] = TDRC(encoding=3, text=[str(year)])
            if comment: tags["COMM::eng"] = COMM(encoding=3, lang="eng", desc="", text=[comment])
            if star_rating > 0:
                tags["POPM:Windows Media Player 9 Series"] = POPM(
                    email="Windows Media Player 9 Series", rating=star_rating * 51, count=0)
            tags.save(real_path, v2_version=3)
            os.sync()

        elif ext == ".flac":
            import subprocess
            # Translate Docker path to real server path
            real_path = filepath
            if settings.media_server_music_prefix and settings.docker_music_prefix:
                if filepath.startswith(settings.docker_music_prefix):
                    real_path = settings.media_server_music_prefix + \
                                filepath[len(settings.docker_music_prefix):]
            # Use metaflac — the official FLAC tool, handles all FLAC versions reliably
            cmd = ["metaflac"]
            # Remove tags we're going to set
            if genres:  cmd += ["--remove-tag=GENRE"]
            if moods:   cmd += ["--remove-tag=MOOD"]
            if year:    cmd += ["--remove-tag=DATE"]
            if comment: cmd += ["--remove-tag=COMMENT"]
            # Set new values
            for g in (genres or []):   cmd += [f"--set-tag=GENRE={g}"]
            for m in (moods or []):    cmd += [f"--set-tag=MOOD={m}"]
            if year:    cmd += [f"--set-tag=DATE={year}"]
            if comment: cmd += [f"--set-tag=COMMENT={comment}"]
            cmd += [real_path]
            result = subprocess.run(cmd, capture_output=True, text=True)
            if result.returncode != 0:
                log.error(f"metaflac error for {real_path}: {result.stderr}")
                return False

        elif ext in (".m4a", ".aac", ".mp4"):
            from mutagen.mp4 import MP4
            f = MP4(filepath)
            if genres:  f["\xa9gen"] = ["; ".join(genres)]
            if moods:   f["----:com.apple.iTunes:MOOD"] = [m.encode() for m in moods]
            if year:    f["\xa9day"] = [str(year)]
            if comment: f["\xa9cmt"] = [comment]
            f.save()

        elif ext in (".ogg", ".opus"):
            from mutagen import File as MFile
            f = MFile(filepath)
            if f:
                if genres:  f["genre"]   = genres
                if moods:   f["mood"]    = moods
                if year:    f["date"]    = [str(year)]
                if comment: f["comment"] = [comment]
                f.save()

        else:
            from mutagen import File as MFile
            f = MFile(filepath, easy=True)
            if f:
                if genres: f["genre"] = genres
                if year:
                    try: f["date"] = [str(year)]
                    except Exception: pass
                f.save()

        return True
    except Exception as e:
        log.error(f"Failed to write tags to {filepath}: {e}")
        return False


def write_tags_extended(filepath: str, genres: List[str], moods: List[str],
                        year: Optional[int], album_name: Optional[str],
                        album_artist: Optional[str], is_compilation: bool,
                        clear_mbids: bool) -> bool:
    """
    NEW — Extended tag writer for Album Tagger override panel.
    Writes Genre, Mood, Year, Album, Album Artist, Compilation flag.
    Optionally clears MusicBrainz ID tags left by Picard/beets.
    Constitution §2: File-First, Non-Destructive (no move/rename/delete).
    """
    try:
        ext = os.path.splitext(filepath)[1].lower()

        # Translate Docker /music path to real server path
        real_path = filepath
        if settings.media_server_music_prefix and settings.docker_music_prefix:
            if filepath.startswith(settings.docker_music_prefix):
                real_path = settings.media_server_music_prefix + \
                            filepath[len(settings.docker_music_prefix):]

        if ext == ".mp3":
            from mutagen.id3 import (ID3, TCON, TDRC, TXXX, TALB, TPE2,
                                     TCMP, ID3NoHeaderError)
            try:
                from mutagen.apev2 import delete as ape_delete
                ape_delete(real_path)
            except Exception:
                pass
            try:
                tags = ID3(real_path)
            except ID3NoHeaderError:
                tags = ID3()

            # Clear fields we're replacing
            for key in list(tags.keys()):
                if any(key.startswith(p) for p in ["TCON", "TDRC", "TALB", "TPE2", "TCMP"]) or \
                   (key.startswith("TXXX") and any(x in key.lower()
                    for x in ["mood", "genre", "musicbrainz"])):
                    del tags[key]

            # Clear MusicBrainz ID frames if requested
            if clear_mbids:
                mbid_frames = [k for k in list(tags.keys())
                               if "musicbrainz" in k.lower() or k in
                               ("TXXX:MusicBrainz Track Id",
                                "TXXX:MusicBrainz Album Id",
                                "TXXX:MusicBrainz Artist Id",
                                "TXXX:MusicBrainz Release Group Id",
                                "UFID:http://musicbrainz.org")]
                for k in mbid_frames:
                    try: del tags[k]
                    except Exception: pass

            if genres:      tags["TCON"]      = TCON(encoding=3, text=["; ".join(genres)])
            if moods:       tags["TXXX:MOOD"] = TXXX(encoding=3, desc="MOOD",
                                                      text=["; ".join(moods)])
            if year:        tags["TDRC"]      = TDRC(encoding=3, text=[str(year)])
            if album_name:  tags["TALB"]      = TALB(encoding=3, text=[album_name])
            if album_artist:tags["TPE2"]      = TPE2(encoding=3, text=[album_artist])
            if is_compilation:
                tags["TCMP"] = TCMP(encoding=3, text=["1"])
            tags.save(real_path, v2_version=3)
            os.sync()

        elif ext == ".flac":
            import subprocess
            real_path = filepath
            if settings.media_server_music_prefix and settings.docker_music_prefix:
                if filepath.startswith(settings.docker_music_prefix):
                    real_path = settings.media_server_music_prefix + \
                                filepath[len(settings.docker_music_prefix):]
            cmd = ["metaflac"]
            # Remove tags we're setting
            for tag in ["GENRE", "MOOD", "DATE", "ALBUM", "ALBUMARTIST",
                        "COMPILATION"]:
                cmd += [f"--remove-tag={tag}"]
            if clear_mbids:
                for tag in ["MUSICBRAINZ_TRACKID", "MUSICBRAINZ_ALBUMID",
                            "MUSICBRAINZ_ARTISTID", "MUSICBRAINZ_RELEASEGROUPID",
                            "MUSICBRAINZ_RELEASETRACKID"]:
                    cmd += [f"--remove-tag={tag}"]
            # Set new values
            for g in (genres or []):    cmd += [f"--set-tag=GENRE={g}"]
            for m in (moods or []):     cmd += [f"--set-tag=MOOD={m}"]
            if year:         cmd += [f"--set-tag=DATE={year}"]
            if album_name:   cmd += [f"--set-tag=ALBUM={album_name}"]
            if album_artist: cmd += [f"--set-tag=ALBUMARTIST={album_artist}"]
            if is_compilation: cmd += ["--set-tag=COMPILATION=1"]
            cmd += [real_path]
            result = subprocess.run(cmd, capture_output=True, text=True)
            if result.returncode != 0:
                log.error(f"metaflac extended error for {real_path}: {result.stderr}")
                return False

        elif ext in (".m4a", ".aac", ".mp4"):
            from mutagen.mp4 import MP4
            f = MP4(filepath)
            if genres:       f["\xa9gen"] = ["; ".join(genres)]
            if moods:        f["----:com.apple.iTunes:MOOD"] = [m.encode() for m in moods]
            if year:         f["\xa9day"] = [str(year)]
            if album_name:   f["\xa9alb"] = [album_name]
            if album_artist: f["aART"]    = [album_artist]
            if is_compilation: f["cpil"]  = True
            if clear_mbids:
                for k in list(f.keys()):
                    if "musicbrainz" in k.lower():
                        try: del f[k]
                        except Exception: pass
            f.save()

        elif ext in (".ogg", ".opus"):
            from mutagen import File as MFile
            f = MFile(filepath)
            if f:
                if genres:       f["genre"]       = genres
                if moods:        f["mood"]         = moods
                if year:         f["date"]         = [str(year)]
                if album_name:   f["album"]        = [album_name]
                if album_artist: f["albumartist"]  = [album_artist]
                if is_compilation: f["compilation"] = ["1"]
                if clear_mbids:
                    for k in list(f.keys()):
                        if "musicbrainz" in k.lower():
                            try: del f[k]
                            except Exception: pass
                f.save()

        else:
            from mutagen import File as MFile
            f = MFile(filepath, easy=True)
            if f:
                if genres:     f["genre"] = genres
                if album_name: f["album"] = [album_name]
                if year:
                    try: f["date"] = [str(year)]
                    except Exception: pass
                f.save()

        return True
    except Exception as e:
        log.error(f"write_tags_extended failed for {filepath}: {e}")
        return False


def index_audio_files(root_path: str) -> List[str]:
    """Walk directory and return list of all audio file paths."""
    files = []
    if not os.path.exists(root_path):
        log.warning(f"Scan path does not exist: {root_path}")
        return files
    for root, dirs, fnames in os.walk(root_path):
        dirs[:] = sorted([d for d in dirs if not d.startswith(".")])
        for fname in fnames:
            if os.path.splitext(fname)[1].lower() in AUDIO_EXTS:
                files.append(os.path.join(root, fname))
    return files


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


@router.post("/read-tags")
async def read_tags_from_folders(req: ReadTagsRequest, user: dict = Depends(require_auth)):
    """
    NEW — Folder-based Album Tagger, paginated.
    Groups by FOLDER PATH (not album tag) — one card per folder.
    Returns `page_size` folders at a time. Frontend calls again with
    offset= to load more. Never dumps the whole library at once.

    Also detects likely compilations (3+ unique artists in one folder)
    and returns per-track album tag state for the Override panel.
    """
    base = settings.docker_music_prefix
    page_size = getattr(req, 'page_size', 20)
    offset    = getattr(req, 'offset', 0)

    # Security: all paths must be within music mount
    # H1 FIX: realpath + strict containment
    safe_paths = []
    for p in req.paths:
        p = p.strip()
        if not p.startswith("/"):
            p = os.path.join(base, p)
        p = os.path.normpath(p)
        if not _is_within_music_prefix(p):
            continue
        if os.path.exists(p):
            safe_paths.append(p)

    if not safe_paths:
        raise HTTPException(404, "No valid paths found")

    # ── Step 1: Discover all immediate subfolders that contain audio files ──────
    # We group by the DIRECT parent folder of each audio file.
    # This gives one card per album folder regardless of what tags say.
    folder_files: dict = {}   # folder_path → list of audio file paths
    total_files_found = 0

    for path in safe_paths:
        for root, dirs, fnames in os.walk(path):
            dirs[:] = sorted([d for d in dirs if not d.startswith(".")])
            audio_in_dir = [
                os.path.join(root, fn)
                for fn in sorted(fnames)
                if os.path.splitext(fn)[1].lower() in AUDIO_EXTS
            ]
            if audio_in_dir:
                folder_key = root  # use the full folder path as the unique key
                if folder_key not in folder_files:
                    folder_files[folder_key] = []
                folder_files[folder_key].extend(audio_in_dir)
                total_files_found += len(audio_in_dir)

    # Sort folders alphabetically for consistent pagination
    all_folders = sorted(folder_files.keys())
    total_folders = len(all_folders)

    # Apply pagination — return only the requested slice
    page_folders = all_folders[offset: offset + page_size]

    # ── Step 2: Read tags for this page of folders only ───────────────────────
    result = []
    for folder_path in page_folders:
        filepaths = folder_files[folder_path]
        folder_name = os.path.basename(folder_path)

        genre_counts:  dict = {}
        mood_counts:   dict = {}
        artists_seen:  set  = set()
        years_seen:    list = []
        track_details: list = []   # per-track info for Override panel

        for filepath in filepaths:
            try:
                tags = read_tags_from_file(filepath)
                artist      = tags.get("artist") or tags.get("albumartist") or ""
                album_tag   = tags.get("album") or ""
                title       = tags.get("title") or os.path.splitext(
                                  os.path.basename(filepath))[0]
                year        = tags.get("year")
                genre       = tags.get("genre") or ""
                albumartist = tags.get("albumartist") or ""

                file_genres = [g.strip() for g in
                               genre.replace(";", ",").split(",") if g.strip()][:3]

                # Mood read — MP3 TXXX:MOOD, FLAC MOOD tag
                file_moods: list = []
                try:
                    fext = os.path.splitext(filepath)[1].lower()
                    if fext == ".mp3":
                        from mutagen.id3 import ID3, ID3NoHeaderError
                        try:
                            id3 = ID3(filepath)
                            mf  = id3.get("TXXX:MOOD")
                            if mf:
                                file_moods = [m.strip() for m in
                                              str(mf).split(";") if m.strip()][:3]
                        except Exception:
                            pass
                    elif fext == ".flac":
                        from mutagen.flac import FLAC
                        ff = FLAC(filepath)
                        file_moods = [m.strip() for m in
                                      ff.get("mood", []) if m.strip()][:3]
                except Exception:
                    pass

                for g in file_genres:
                    genre_counts[g] = genre_counts.get(g, 0) + 1
                for m in file_moods:
                    mood_counts[m] = mood_counts.get(m, 0) + 1
                if artist:
                    artists_seen.add(artist.strip())
                if year:
                    years_seen.append(year)

                track_details.append({
                    "file_path":   filepath,
                    "title":       title,
                    "artist":      artist,
                    "album_tag":   album_tag,   # what the ALBUM tag currently says
                    "albumartist": albumartist,
                    "year":        year,
                    "genres":      file_genres,
                    "moods":       file_moods,
                })
            except Exception as e:
                log.debug(f"Tag read error {filepath}: {e}")
                continue

        # Detect compilation: 3+ unique track artists in one folder
        is_compilation = len(artists_seen) >= 3

        # Most common year across tracks
        common_year = None
        if years_seen:
            from collections import Counter
            common_year = Counter(years_seen).most_common(1)[0][0]

        top_genres = [g for g, _ in sorted(genre_counts.items(), key=lambda x: -x[1])[:3]]
        top_moods  = [m for m, _ in sorted(mood_counts.items(),  key=lambda x: -x[1])[:3]]

        result.append({
            "key":            folder_path,          # unique — full path, no collisions
            "folder_name":    folder_name,           # display name (last path component)
            "folder_path":    folder_path,
            "track_count":    len(filepaths),
            "year":           common_year,
            "genres":         top_genres,
            "moods":          top_moods,
            "has_genres":     len(top_genres) > 0,
            "has_moods":      len(top_moods) > 0,
            "is_compilation": is_compilation,        # flag for UI warning on LOOKUP
            "artists":        sorted(list(artists_seen))[:5],
            "tracks":         track_details,         # per-track detail for Override panel
        })

    has_more   = (offset + page_size) < total_folders
    next_offset = offset + page_size if has_more else None

    log.info(
        f"[read-tags] Returning folders {offset}–{offset+len(result)} "
        f"of {total_folders} total | "
        f"paths={[os.path.basename(p) for p in safe_paths]}"
    )

    return {
        "albums":        result,
        "total_files":   total_files_found,
        "total_folders": total_folders,
        "offset":        offset,
        "page_size":     page_size,
        "has_more":      has_more,
        "next_offset":   next_offset,
        # Console message the frontend can display directly
        "console_msg":   (
            f"📂 Found {total_folders} folders containing audio files. "
            f"Showing {offset+1}–{offset+len(result)}."
            + (" Load more when ready." if has_more else " All folders loaded.")
        ),
    }


@router.post("/scan/start")
async def start_scan(req: ScanRequest, background_tasks: BackgroundTasks,
                     user: dict = Depends(require_auth)):
    # Check for existing running job
    async with aiosqlite.connect(settings.database_url) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT job_id FROM scan_jobs WHERE job_type='retriever' AND status='running'"
        )
        if await cursor.fetchone():
            raise HTTPException(409, "A scan is already running. Stop it first.")

    # Determine and validate scan path
    # C3 FIX: every subfolder must resolve inside docker_music_prefix
    if req.scope == "subfolder" and req.subfolders:
        # Multi-folder queue — validate all and join with | separator
        validated = []
        for sf in req.subfolders:
            p = sf.strip()
            if not p.startswith("/"):
                p = os.path.join(settings.docker_music_prefix, p)
            p = os.path.normpath(p)
            if not _is_within_music_prefix(p):
                raise HTTPException(400,
                    f"Subfolder outside music directory: {p}")
            if not os.path.exists(p):
                raise HTTPException(404,
                    f"Subfolder not found: {p}. "
                    f"Use the Docker path starting with {settings.docker_music_prefix}")
            validated.append(p)
        scan_path = "|".join(validated)
    elif req.scope == "subfolder" and req.subfolder:
        scan_path = req.subfolder.strip()
        if not scan_path.startswith("/"):
            scan_path = os.path.join(settings.docker_music_prefix, scan_path)
        scan_path = os.path.normpath(scan_path)
        if not _is_within_music_prefix(scan_path):
            raise HTTPException(400,
                f"Subfolder outside music directory: {scan_path}")
        if not os.path.exists(scan_path):
            raise HTTPException(404,
                f"Subfolder not found: {scan_path}. "
                f"Use the Docker path starting with {settings.docker_music_prefix}")
    else:
        scan_path = settings.docker_music_prefix

    # Create job record
    now = datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(settings.database_url) as db:
        cursor2 = await db.execute(
            "INSERT INTO scan_jobs (job_type, status, started_at, config_json) VALUES (?,?,?,?)",
            ("retriever", "running", now, json.dumps(req.dict()))
        )
        await db.commit()
        job_id = cursor2.lastrowid

    background_tasks.add_task(run_scan_job, job_id, scan_path, req.mode, req.chunk_size)
    return {"ok": True, "job_id": job_id, "scan_path": scan_path, "mode": req.mode}


@router.post("/scan/pause")
async def pause_scan(user: dict = Depends(require_auth)):
    async with aiosqlite.connect(settings.database_url) as db:
        now = datetime.now(timezone.utc).isoformat()
        await db.execute(
            "UPDATE scan_jobs SET status='paused', paused_at=? WHERE job_type='retriever' AND status='running'",
            (now,)
        )
        await db.commit()
    return {"ok": True, "status": "paused", "message": "Scan paused. No files were corrupted."}


@router.post("/scan/resume")
async def resume_scan(background_tasks: BackgroundTasks, user: dict = Depends(require_auth)):
    async with aiosqlite.connect(settings.database_url) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM scan_jobs WHERE job_type='retriever' AND status='paused' ORDER BY job_id DESC LIMIT 1"
        )
        job = await cursor.fetchone()
        if not job:
            raise HTTPException(404, "No paused scan found.")
        await db.execute(
            "UPDATE scan_jobs SET status='running' WHERE job_id=?", (job["job_id"],)
        )
        await db.commit()
        config = json.loads(job["config_json"] or "{}")

    scan_path = config.get("subfolder", settings.docker_music_prefix) \
        if config.get("scope") == "subfolder" else settings.docker_music_prefix
    background_tasks.add_task(run_scan_job, job["job_id"], scan_path,
                               config.get("mode", "preview"), config.get("chunk_size", 20))
    return {"ok": True, "status": "running", "job_id": job["job_id"]}


@router.post("/scan/stop")
async def stop_scan(user: dict = Depends(require_auth)):
    async with aiosqlite.connect(settings.database_url) as db:
        now = datetime.now(timezone.utc).isoformat()
        await db.execute(
            "UPDATE scan_jobs SET status='stopped', completed_at=? "
            "WHERE job_type='retriever' AND status IN ('running','paused')", (now,)
        )
        await db.commit()
    return {"ok": True, "status": "stopped"}


@router.get("/scan/latest")
async def get_latest_job(user: dict = Depends(require_auth)):
    """Returns the most recent scan job — used by Write Selected when scanJobId is null."""
    async with aiosqlite.connect(settings.database_url) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT job_id, status, scan_path FROM scan_jobs "
            "WHERE job_type='retriever' ORDER BY job_id DESC LIMIT 1"
        )
        row = await cursor.fetchone()
    if not row:
        raise HTTPException(404, "No scan jobs found")
    return {"job_id": row["job_id"], "status": row["status"], "scan_path": row["scan_path"]}


@router.get("/scan/job/{job_id}")
async def get_job_status(job_id: int, user: dict = Depends(require_auth)):
    async with aiosqlite.connect(settings.database_url) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM scan_jobs WHERE job_id=?", (job_id,))
        job = await cursor.fetchone()
    if not job:
        raise HTTPException(404, f"Job {job_id} not found")
    return dict(job)


@router.get("/scan/preview/{job_id}")
async def get_preview(job_id: int, offset: int = 0, limit: int = 50,
                      user: dict = Depends(require_auth)):
    """Get preview results — only tracks from the current scan job."""
    async with aiosqlite.connect(settings.database_url) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT started_at FROM scan_jobs WHERE job_id=?", (job_id,)
        )
        job = await cursor.fetchone()
        if not job:
            return []
        started_at = job["started_at"]
        cursor = await db.execute(
            """SELECT t.track_id, t.file_path, t.title, t.year,
                      t.genre_1, t.genre_2, t.genre_3,
                      t.mood_1, t.mood_2, t.mood_3,
                      t.mbid, t.metadata_source, t.art_path,
                      t.tag_artist, t.tag_album, t.confidence
               FROM tracks t
               WHERE t.last_scanned >= ?
               ORDER BY t.track_id ASC
               LIMIT ? OFFSET ?""",
            (started_at, limit, offset)
        )
        rows = await cursor.fetchall()

    # Add current file genre for old vs new comparison
    result = []
    for r in rows:
        d = dict(r)
        try:
            file_tags = read_tags_from_file(r["file_path"])
            d["current_genre"] = file_tags.get("genre", "") or "—"
        except Exception:
            d["current_genre"] = "—"
        result.append(d)
    return result


@router.post("/write")
async def write_tracks(req: ApproveRequest, user: dict = Depends(require_auth)):
    """Write approved tracks directly — not as background task to ensure NAS flush."""
    results = await write_approved_tracks(req.job_id, req.track_ids, req.write_art)
    successes = results.get("success", 0)
    failures = results.get("failed", 0)
    msg = f"{successes} track(s) written successfully"
    if failures:
        msg += f", {failures} failed"
    return {"ok": True, "message": msg, "success": successes, "failed": failures}


@router.delete("/cache")
async def clear_cache(user: dict = Depends(require_auth)):
    async with aiosqlite.connect(settings.database_url) as db:
        await db.execute("DELETE FROM tracks")
        await db.execute("DELETE FROM chart_data")
        await db.execute("DELETE FROM write_log")
        await db.execute("DELETE FROM scan_jobs")
        await db.execute("DELETE FROM artists")
        await db.execute("DELETE FROM albums")
        await db.commit()
    return {"ok": True, "message": "Cache cleared."}


@router.post("/override")
async def manual_override(req: ManualOverrideRequest, user: dict = Depends(require_auth)):
    """
    Manual genre/mood override — bypasses waterfall entirely.
    Accepts track_ids (from preview table) or file_paths (from album tagger).
    Max 3 genres, max 3 moods enforced.
    """
    genres = req.genres[:3]
    moods  = req.moods[:3]
    year   = req.year
    success_count = 0
    fail_count    = 0

    import concurrent.futures
    loop     = asyncio.get_event_loop()
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    now      = datetime.now(timezone.utc).isoformat()

    # Build unified list of (file_path, track_id_or_None, file_year)
    write_targets = []

    for track_id in (req.track_ids or []):
        async with aiosqlite.connect(settings.database_url) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT file_path, year FROM tracks WHERE track_id=?", (track_id,))
            track = await cursor.fetchone()
        # C2 FIX: enforce music-prefix containment on every write target
        if track and os.path.exists(track["file_path"]) and \
                _is_within_music_prefix(track["file_path"]):
            write_targets.append((track["file_path"], track_id, track["year"]))
        else:
            fail_count += 1

    for fp in (req.file_paths or []):
        # C2 FIX: enforce music-prefix containment on every write target
        if os.path.exists(fp) and _is_within_music_prefix(fp):
            write_targets.append((fp, None, None))
        else:
            fail_count += 1

    for (file_path, track_id, file_year) in write_targets:
        use_year = year or file_year
        try:
            success = await loop.run_in_executor(
                executor, write_tags, file_path, genres, moods, use_year, "", 0)
        except Exception as e:
            log.error(f"Override write error {file_path}: {e}")
            success = False

        if success:
            success_count += 1
            async with aiosqlite.connect(settings.database_url) as db:
                if track_id:
                    await db.execute(
                        "UPDATE tracks SET genre_1=?, genre_2=?, genre_3=?, "
                        "mood_1=?, mood_2=?, mood_3=?, last_updated=? WHERE track_id=?",
                        (genres[0] if len(genres) > 0 else None,
                         genres[1] if len(genres) > 1 else None,
                         genres[2] if len(genres) > 2 else None,
                         moods[0]  if len(moods)  > 0 else None,
                         moods[1]  if len(moods)  > 1 else None,
                         moods[2]  if len(moods)  > 2 else None,
                         now, track_id))
                await db.execute(
                    "INSERT INTO write_log (track_id, file_path, field_changed, "
                    "new_value, write_status, written_at) VALUES (?,?,'manual_override',?,?,?)",
                    (track_id, file_path,
                     str({"genres": genres, "moods": moods}), "success", now))
                await db.commit()
        else:
            fail_count += 1

    executor.shutdown(wait=False)
    return {"ok": True, "success": success_count, "failed": fail_count,
            "message": f"{success_count} track(s) updated with manual genre override"}


@router.post("/album-override")
async def album_override(req: AlbumOverrideRequest, user: dict = Depends(require_auth)):
    """
    NEW — Album Tagger enhanced override endpoint.
    Writes Album, Album Artist, Year, Compilation flag, Genre, Mood to every
    selected file in one pass. Optionally clears MusicBrainz IDs.
    Sets manually_verified=1 in DB so Auto-Pilot never overwrites these tracks.
    Returns per-file results with console messages for the UI log.
    """
    genres       = req.genres[:3]
    moods        = req.moods[:3]
    success_list = []
    fail_list    = []
    console_msgs = []

    import concurrent.futures
    loop     = asyncio.get_event_loop()
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    now      = datetime.now(timezone.utc).isoformat()

    console_msgs.append(
        f"✏️  Album Override starting — {len(req.file_paths)} track(s) to write."
    )
    if req.album_name:
        console_msgs.append(f"   Album name → \"{req.album_name}\"")
    if req.album_artist:
        console_msgs.append(f"   Album artist → \"{req.album_artist}\"")
    if req.is_compilation:
        console_msgs.append("   Compilation flag → ON")
    if req.clear_mbids:
        console_msgs.append("   MusicBrainz IDs → will be cleared")
    if genres:
        console_msgs.append(f"   Genres → {', '.join(genres)}")
    if moods:
        console_msgs.append(f"   Moods → {', '.join(moods)}")

    for fp in req.file_paths:
        # C2 FIX: enforce music-prefix containment before any write
        if not os.path.exists(fp) or not _is_within_music_prefix(fp):
            fail_list.append(fp)
            console_msgs.append(f"   ⚠️  File not found or outside /music: {os.path.basename(fp)}")
            continue

        try:
            success = await loop.run_in_executor(
                executor,
                write_tags_extended,
                fp, genres, moods, req.year,
                req.album_name, req.album_artist,
                req.is_compilation, req.clear_mbids
            )
        except Exception as e:
            log.error(f"album-override write error {fp}: {e}")
            success = False

        fname = os.path.basename(fp)
        if success:
            success_list.append(fp)
            console_msgs.append(f"   ✅ Written: {fname}")

            # Mark as manually_verified in DB so Auto-Pilot skips it
            async with aiosqlite.connect(settings.database_url) as db:
                # Try to find existing track record
                cursor = await db.execute(
                    "SELECT track_id FROM tracks WHERE file_path=?", (fp,)
                )
                row = await cursor.fetchone()
                if row:
                    await db.execute(
                        """UPDATE tracks SET
                               genre_1=?, genre_2=?, genre_3=?,
                               mood_1=?, mood_2=?, mood_3=?,
                               manually_verified=1, last_updated=?
                           WHERE track_id=?""",
                        (genres[0] if len(genres) > 0 else None,
                         genres[1] if len(genres) > 1 else None,
                         genres[2] if len(genres) > 2 else None,
                         moods[0]  if len(moods)  > 0 else None,
                         moods[1]  if len(moods)  > 1 else None,
                         moods[2]  if len(moods)  > 2 else None,
                         now, row[0])
                    )
                await db.execute(
                    "INSERT INTO write_log "
                    "(track_id, file_path, field_changed, new_value, write_status, written_at) "
                    "VALUES (?,?,'album_override',?,?,?)",
                    (row[0] if row else None, fp,
                     json.dumps({
                         "album": req.album_name,
                         "album_artist": req.album_artist,
                         "genres": genres,
                         "moods": moods,
                         "year": req.year,
                         "compilation": req.is_compilation,
                         "mbids_cleared": req.clear_mbids,
                     }),
                     "success", now)
                )
                await db.commit()
        else:
            fail_list.append(fp)
            console_msgs.append(f"   ❌ Failed: {fname}")

    executor.shutdown(wait=False)

    summary = (
        f"Album Override complete — "
        f"{len(success_list)} written, {len(fail_list)} failed."
    )
    console_msgs.append(f"✔️  {summary}")
    log.info(f"[album-override] {summary}")

    return {
        "ok":           len(fail_list) == 0,
        "success":      len(success_list),
        "failed":       len(fail_list),
        "message":      summary,
        "console_msgs": console_msgs,
    }


@router.post("/album-lookup")
async def album_lookup(req: AlbumTagRequest, user: dict = Depends(require_auth)):
    """
    Album-level genre lookup via MusicBrainz release-group.
    Returns proposed genres and year for the whole album.
    """
    result = await fetch_musicbrainz_album(req.artist, req.album)
    return {
        "artist":          req.artist,
        "album":           req.album,
        "proposed_genres": result.get("genres", []),
        "year":            result.get("year"),
        "confidence":      result.get("confidence", "low"),
        "album_id":        result.get("album_id"),
    }


@router.get("/genres")
async def get_custom_genres(user: dict = Depends(require_auth)):
    """Returns custom genre and mood tags saved by the user."""
    import json as _json
    async with aiosqlite.connect(settings.database_url) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT key, value FROM app_settings WHERE key IN ('custom_genres','custom_moods')"
        ) as cur:
            rows = await cur.fetchall()
    data = {r["key"]: _json.loads(r["value"] or "[]") for r in rows}
    return {
        "genres": data.get("custom_genres", []),
        "moods":  data.get("custom_moods",  []),
    }


@router.post("/genres")
async def save_custom_genre(req: dict, user: dict = Depends(require_auth)):
    """Add or delete a custom genre or mood tag."""
    import json as _json
    kind   = req.get("kind", "")    # 'genre' | 'mood'
    name   = (req.get("name") or "").strip()
    action = req.get("action", "add")  # 'add' | 'delete'

    if kind not in ("genre", "mood") or not name:
        raise HTTPException(400, "Invalid kind or empty name")

    key = "custom_genres" if kind == "genre" else "custom_moods"
    now = datetime.now(timezone.utc).isoformat()

    async with aiosqlite.connect(settings.database_url) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT value FROM app_settings WHERE key = ?", (key,)
        ) as cur:
            row = await cur.fetchone()
        current = _json.loads(row["value"] or "[]") if row else []

        if action == "add":
            if name not in current:
                current.append(name)
        elif action == "delete":
            current = [v for v in current if v != name]

        await db.execute(
            "INSERT INTO app_settings (key, value, updated_at) VALUES (?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (key, _json.dumps(current), now)
        )
        await db.commit()

    return {"ok": True, "kind": kind, "action": action, "name": name, "current": current}


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
#  BACKGROUND SCAN JOB
# ══════════════════════════════════════════════════════════════════════════════

async def run_scan_job(job_id: int, scan_path: str, mode: str, chunk_size: int):
    # scan_path may be pipe-separated list for multi-folder queue
    scan_paths = [p.strip() for p in scan_path.split('|') if p.strip()]
    log.info(f"[Job {job_id}] Starting scan: {scan_paths} | mode={mode}")
    log.info(f"[Job {job_id}] 🐾 ChartHound Retriever starting up...")
    log.info(f"[Job {job_id}] 📂 Scan path(s): {scan_paths}")
    log.info(f"[Job {job_id}] ⚙️  Mode: {mode.upper()} | Chunk size: {chunk_size}")

    # Capture start time for autopilot post-scan write
    started_at = datetime.now(timezone.utc).isoformat()

    # Get Last.fm key
    lastfm_key = ""
    try:
        async with aiosqlite.connect(settings.database_url) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT token_enc FROM connections WHERE service='lastfm'")
            row = await cursor.fetchone()
            if row and row["token_enc"]:
                from app.security import decrypt_token
                lastfm_key = decrypt_token(row["token_enc"])
    except Exception:
        pass

    # Index files
    # Handle | separated multi-folder paths
    file_list = []
    for sp in scan_paths:
        file_list.extend(index_audio_files(sp))
    file_list = sorted(set(file_list))  # deduplicate, keep sorted
    total = len(file_list)
    log.info(f"[Job {job_id}] 🎵 Found {total} audio files — beginning waterfall scan...")
    log.info(f"[Job {job_id}] 📡 Waterfall order: MusicBrainz → Last.fm → ListenBrainz → Deezer → Discogs → iTunes")

    if total == 0:
        now = datetime.now(timezone.utc).isoformat()
        async with aiosqlite.connect(settings.database_url) as db:
            await db.execute(
                "UPDATE scan_jobs SET status='done', completed_at=?, total_tracks=0 WHERE job_id=?",
                (now, job_id)
            )
            await db.commit()
        log.warning(f"[Job {job_id}] No audio files found in {scan_path}")
        return

    async with aiosqlite.connect(settings.database_url) as db:
        await db.execute("UPDATE scan_jobs SET total_tracks=? WHERE job_id=?", (total, job_id))
        await db.commit()

    processed = 0
    failed = 0
    chunks = [file_list[i:i+chunk_size] for i in range(0, len(file_list), chunk_size)]
    chunk_start_time = started_at

    for chunk in chunks:
        # Check pause/stop
        async with aiosqlite.connect(settings.database_url) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT status FROM scan_jobs WHERE job_id=?", (job_id,))
            job = await cursor.fetchone()
            if job and job["status"] in ("paused", "stopped"):
                log.info(f"[Job {job_id}] Scan {job['status']} by user.")
                return

        chunk_scan_start = datetime.now(timezone.utc).isoformat()
        chunk_num = chunks.index(chunk) + 1
        log.info(
            f"[Job {job_id}] 🔍 Chunk {chunk_num}/{len(chunks)} — "
            f"tracks {processed+1}–{min(processed+len(chunk), total)} of {total}"
        )

        for filepath in chunk:
            try:
                await process_single_file(filepath, job_id, lastfm_key, mode)
                processed += 1
            except Exception as e:
                log.error(f"[Job {job_id}] Error on {filepath}: {e}")
                failed += 1

        # Update progress after each chunk
        async with aiosqlite.connect(settings.database_url) as db:
            await db.execute(
                "UPDATE scan_jobs SET processed=?, failed=? WHERE job_id=?",
                (processed, failed, job_id)
            )
            await db.commit()

        # Autopilot: write this chunk's tracks immediately after scanning
        # Writing per-chunk means at most chunk_size tracks lost if crash occurs
        if mode == "autopilot":
            async with aiosqlite.connect(settings.database_url) as db:
                db.row_factory = aiosqlite.Row
                cursor = await db.execute(
                    "SELECT track_id FROM tracks WHERE last_scanned >= ? ORDER BY track_id ASC",
                    (chunk_scan_start,)
                )
                rows = await cursor.fetchall()
            chunk_track_ids = [r["track_id"] for r in rows]
            if chunk_track_ids:
                results = await write_approved_tracks(job_id, chunk_track_ids, True)
                log.info(f"[Job {job_id}] Chunk written: {results['success']} ok, {results['failed']} failed")

        await asyncio.sleep(0.3)

    now = datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(settings.database_url) as db:
        await db.execute(
            "UPDATE scan_jobs SET status='done', completed_at=?, processed=?, failed=? WHERE job_id=?",
            (now, processed, failed, job_id)
        )
        await db.commit()
    log.info(f"[Job {job_id}] ✅ Scan complete — {processed} processed, {failed} failed")


async def process_single_file(filepath: str, job_id: int, lastfm_key: str, mode: str):
    """
    Process one audio file through the full waterfall.
    FIXED: Reads actual file tags first, falls back to path parsing only if empty.
    """
    ext = os.path.splitext(filepath)[1].lower()
    now = datetime.now(timezone.utc).isoformat()

    # ── Step 1: Read actual file tags (PRIMARY source for artist/title) ────────
    file_tags = read_tags_from_file(filepath)

    artist = file_tags.get("artist") or file_tags.get("albumartist", "")
    album  = file_tags.get("album", "")
    title  = file_tags.get("title", "")

    # ── Step 2: Fall back to path parsing if tags are empty ───────────────────
    if not artist or not title:
        path_data = parse_artist_title_from_path(filepath, settings.docker_music_prefix)
        if not artist: artist = path_data.get("artist", "")
        if not title:  title  = path_data.get("title", "")
        if not album:  album  = path_data.get("album", "")

    # Still nothing — skip this file
    if not artist or not title:
        log.debug(f"Skipping (no artist/title): {filepath}")
        return

    log.debug(f"Processing: {artist} — {title}")

    # ── Step 3: Cache check ────────────────────────────────────────────────────
    file_hash = get_file_hash(filepath)
    async with aiosqlite.connect(settings.database_url) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT track_id, last_updated, manually_verified FROM tracks "
            "WHERE file_path=? OR file_hash=?",
            (filepath, file_hash)
        )
        cached = await cursor.fetchone()

    if cached and cached["last_updated"]:
        # Skip if manually verified by user — Auto-Pilot must never overwrite hand edits
        if cached["manually_verified"]:
            log.info(f"[Auto-Pilot] Skipping manually verified track: {title}")
            return
        log.debug(f"Cache hit: {title}")
        if mode == "autopilot":
            await write_from_cache(cached["track_id"], filepath)
        return

    # ── Step 4: Get Discogs token if available ─────────────────────────────────
    discogs_token = ""
    try:
        async with aiosqlite.connect(settings.database_url) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT token_enc FROM connections WHERE service='discogs'"
            )
            row = await cursor.fetchone()
            if row and row["token_enc"]:
                from app.security import decrypt_token
                discogs_token = decrypt_token(row["token_enc"])
    except Exception:
        pass

    # ── Step 5: True Waterfall API calls ──────────────────────────────────────
    # MusicBrainz first — if it has genres, skip the slow sources
    mb_data  = await fetch_musicbrainz(artist, title, album)
    lfm_data = await fetch_lastfm(artist, title, lastfm_key)  # Always call for moods/charts
    # Fix E: ListenBrainz — free mood supplement, uses MBID from MusicBrainz
    lbz_data = await fetch_listenbrainz(mb_data.get("mbid"), artist, title)

    if mb_data.get("genres"):
        # MusicBrainz has genres — skip Deezer, Discogs, iTunes
        log.debug(f"MB has genres — skipping Deezer/Discogs/iTunes for {title}")
        deezer_data  = {"year": None, "genres": []}
        discogs_data = {"year": None, "genres": [], "styles": []}
        itunes_data  = {"year": None, "genres": [], "art_url": None}
    else:
        # MB has no genres — try Deezer and Discogs
        deezer_data  = await fetch_deezer(artist, title)
        discogs_data = await fetch_discogs(artist, title, discogs_token)

        # Only call iTunes if still no genres found
        if deezer_data.get("genres") or discogs_data.get("genres") or discogs_data.get("styles"):
            log.debug(f"Deezer/Discogs has genres — skipping iTunes for {title}")
            itunes_data = {"year": None, "genres": [], "art_url": None}
        else:
            log.debug(f"No genres found — calling iTunes as last resort for {title}")
            itunes_data = await fetch_itunes(artist, title, album)

    # ── Step 5: Merge metadata ─────────────────────────────────────────────────
    # Year priority: file's originalyear → MusicBrainz → Discogs → file date → Deezer → iTunes
    file_orig_year = file_tags.get("year")
    mb_year = mb_data.get("year")
    discogs_year = discogs_data.get("year")
    deezer_year = deezer_data.get("year")
    itunes_year = itunes_data.get("year")
    raw_year = file_orig_year or mb_year or discogs_year or deezer_year or itunes_year
    year = raw_year if raw_year and 1900 < int(raw_year) < 2030 else None

    # Genres: merge all waterfall sources — Discogs styles count as genres too
    # Broad categories prioritized, MusicBrainz wins ties
    discogs_genres = discogs_data.get("genres", []) + discogs_data.get("styles", [])
    api_genres = merge_genres([
        mb_data.get("genres", []),
        discogs_genres,
        itunes_data.get("genres", []),
        deezer_data.get("genres", []),
    ], top_n=3)

    # SAFETY GATE (Gemini recommendation): Never wipe existing genres unless
    # we have something better to replace them with
    existing_genres = [g.strip() for g in file_tags.get("genre", "").split(";") if g.strip()]
    if api_genres:
        all_genres = api_genres
    elif existing_genres:
        # Keep existing genres — better than nothing
        all_genres = existing_genres[:3]
        log.debug(f"Safety gate: keeping existing genres {all_genres} for {filepath}")
    else:
        all_genres = []

    moods = lfm_data.get("moods", [])
    # Fix E: Merge ListenBrainz moods — add any LBZ moods not already in the list
    for lbz_mood in lbz_data.get("moods", []):
        if lbz_mood not in moods:
            moods.append(lbz_mood)
    # Fallback 1: CCM/Gospel genre-based moods
    if not moods and all_genres:
        moods = get_ccm_moods(all_genres)
    # Fallback 2: GENRE_TO_MOOD — fires when Last.fm + ListenBrainz both return nothing.
    # Uses the genres we already have (Rock, Hard Rock, etc.) to assign a sensible mood.
    # This guarantees mainstream artists always get at least one mood.
    if not moods and all_genres:
        for genre in all_genres:
            mood = GENRE_TO_MOOD.get(genre.lower().strip())
            if mood and mood not in moods:
                moods.append(mood)
            if len(moods) >= 3:
                break
        if moods:
            log.debug(f"Genre-to-mood fallback fired for {title}: {all_genres} → {moods}")
    moods = moods[:3]

    chart_entries = []
    for chart_name in lfm_data.get("charts", []):
        chart_entries.append({
            "chart_name": chart_name,
            "peak_position": lfm_data.get("peak"),
            "weeks_on_chart": lfm_data.get("weeks", 0),
            "star_rating": peak_to_stars(lfm_data.get("peak")),
            "confidence": mb_data.get("confidence", "low"),
            "listener_count": lfm_data.get("listeners", 0),
        })

    comment = format_chart_comment(chart_entries)
    confidence = mb_data.get("confidence", "low")

    # Apply artist fingerprint correction before storing
    if all_genres:
        update_artist_fingerprint(artist, all_genres)
        all_genres = apply_artist_fingerprint(artist, all_genres)
    all_genres = all_genres[:3]
    moods      = moods[:3]

    # ── Step 6: Store in SQLite (available to preview immediately) ─────────────
    async with aiosqlite.connect(settings.database_url) as db:
        cursor = await db.execute(
            """INSERT INTO tracks
               (file_path, file_hash, file_format, title, year,
                genre_1, genre_2, genre_3, mood_1, mood_2, mood_3,
                art_path, mbid, metadata_source, confidence,
                tag_artist, tag_album, last_updated, last_scanned)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(file_path) DO UPDATE SET
                   file_hash=excluded.file_hash, year=excluded.year,
                   genre_1=excluded.genre_1, genre_2=excluded.genre_2, genre_3=excluded.genre_3,
                   mood_1=excluded.mood_1, mood_2=excluded.mood_2, mood_3=excluded.mood_3,
                   art_path=excluded.art_path, mbid=excluded.mbid,
                   metadata_source=excluded.metadata_source, confidence=excluded.confidence,
                   tag_artist=excluded.tag_artist, tag_album=excluded.tag_album,
                   last_updated=excluded.last_updated, last_scanned=excluded.last_scanned""",
            (filepath, file_hash, ext.lstrip("."),
             title, year,
             all_genres[0] if len(all_genres) > 0 else None,
             all_genres[1] if len(all_genres) > 1 else None,
             all_genres[2] if len(all_genres) > 2 else None,
             moods[0] if len(moods) > 0 else None,
             moods[1] if len(moods) > 1 else None,
             moods[2] if len(moods) > 2 else None,
             itunes_data.get("art_url"), mb_data.get("mbid"),
             "waterfall", confidence, artist, album, now, now)
        )
        track_id = cursor.lastrowid
        await db.commit()

        # Store chart entries
        for entry in chart_entries:
            await db.execute(
                """INSERT INTO chart_data
                   (track_id, chart_name, peak_position, weeks_on_chart,
                    star_rating, confidence, listener_count, comment_string)
                   VALUES (?,?,?,?,?,?,?,?)
                   ON CONFLICT(track_id, chart_name) DO UPDATE SET
                       peak_position=excluded.peak_position,
                       weeks_on_chart=excluded.weeks_on_chart,
                       star_rating=excluded.star_rating,
                       confidence=excluded.confidence,
                       listener_count=excluded.listener_count,
                       comment_string=excluded.comment_string""",
                (track_id, entry["chart_name"], entry.get("peak_position"),
                 entry.get("weeks_on_chart", 0), entry.get("star_rating", 0),
                 entry.get("confidence", "low"), entry.get("listener_count", 0), comment)
            )
        await db.commit()

    # Autopilot write is now handled by run_scan_job after all tracks are scanned
    # This uses write_approved_tracks which is more reliable than per-track writes


async def write_from_cache(track_id: int, filepath: str):
    async with aiosqlite.connect(settings.database_url) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM tracks WHERE track_id=?", (track_id,))
        track = await cursor.fetchone()
        cursor2 = await db.execute("SELECT * FROM chart_data WHERE track_id=?", (track_id,))
        charts = await cursor2.fetchall()
    if not track:
        return
    genres = [g for g in [track["genre_1"], track["genre_2"], track["genre_3"]] if g]
    moods  = [m for m in [track["mood_1"], track["mood_2"], track["mood_3"]] if m]
    chart_list = [dict(c) for c in charts]
    comment = format_chart_comment(chart_list)
    star = max((peak_to_stars(c.get("peak_position")) for c in chart_list), default=0)
    import concurrent.futures
    loop = asyncio.get_event_loop()
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    await loop.run_in_executor(executor, write_tags, filepath, genres, moods, track["year"], comment, star)
    executor.shutdown(wait=False)


async def write_approved_tracks(job_id: int, track_ids: List[int], write_art: bool):
    import concurrent.futures
    loop = asyncio.get_event_loop()
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    success_count = 0
    fail_count = 0
    track_data = []

    # Fetch all track data first
    for track_id in track_ids:
        async with aiosqlite.connect(settings.database_url) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM tracks WHERE track_id=?", (track_id,))
            track = await cursor.fetchone()
            cursor2 = await db.execute("SELECT * FROM chart_data WHERE track_id=?", (track_id,))
            charts = await cursor2.fetchall()
        if not track or not os.path.exists(track["file_path"]):
            fail_count += 1
            continue
        genres = [g for g in [track["genre_1"], track["genre_2"], track["genre_3"]] if g]
        moods  = [m for m in [track["mood_1"], track["mood_2"], track["mood_3"]] if m]
        chart_list = [dict(c) for c in charts]
        comment = format_chart_comment(chart_list)
        star = max((peak_to_stars(c.get("peak_position")) for c in chart_list), default=0)
        track_data.append((track_id, track["file_path"], genres, moods,
                          track["year"], comment, star))

    # Write ONE file at a time — serial writes prevent race conditions
    now = datetime.now(timezone.utc).isoformat()
    for (track_id, file_path, genres, moods, year, comment, star) in track_data:
        try:
            success = await loop.run_in_executor(
                executor, write_tags, file_path, genres, moods, year, comment, star
            )
        except Exception as e:
            log.error(f"Write error for {file_path}: {e}")
            success = False

        if success:
            success_count += 1
        else:
            fail_count += 1

        async with aiosqlite.connect(settings.database_url) as db:
            await db.execute(
                "INSERT INTO write_log (track_id, file_path, field_changed, new_value, write_status, written_at) "
                "VALUES (?,?,'all',?,?,?)",
                (track_id, file_path,
                 json.dumps({"genres": genres, "moods": moods, "year": year}),
                 "success" if success else "failed", now)
            )
            await db.commit()

    executor.shutdown(wait=False)
    return {"success": success_count, "failed": fail_count}
