"""
ChartHound — Chart Reference Loader
Standalone script to populate the chart_reference table.
Run directly: python3 /app/load_charts.py

No async, no FastAPI — pure synchronous Python writing directly to SQLite.
Safe to run multiple times — uses ON CONFLICT DO UPDATE.
"""

import csv
import io
import re
import sqlite3
import time
from datetime import datetime

import httpx

DB_PATH = "/data/charthound.db"

HOT100_CSV_URL = (
    "https://raw.githubusercontent.com/HipsterVizNinja/"
    "random-data/main/Music/hot-100/Hot%20100.csv"
)

BILLBOARD_SLUGS = {
    "ac":       "adult-contemporary",
    "adultpop": "pop-songs",
    "country":  "hot-country-songs",
    "rnb":      "hot-r-and-b-hip-hop-songs",
    "rock":     "mainstream-rock-tracks",
    "dance":    "dance-club-songs",
    "ccm":      "christian-songs",
    "gospel":   "gospel-songs",
    "ccm-ac":   "christian-ac-tips",
    "ccm-rock": "christian-ac-tips",
}

CHART_DISPLAY = {
    "hot100":   "Billboard Hot 100",
    "ac":       "Adult Contemporary",
    "adultpop": "Adult Pop / Top 40",
    "country":  "Hot Country Songs",
    "rnb":      "R&B/Hip-Hop Songs",
    "rock":     "Mainstream Rock",
    "dance":    "Dance/Electronic",
    "ccm":      "Christian Songs (CCM)",
    "gospel":   "Gospel Songs",
    "ccm-ac":   "Christian AC",
    "ccm-rock": "Christian Rock",
}


def norm(text: str) -> str:
    if not text:
        return ""
    t = text.lower().strip()
    t = re.sub(r'^the\s+', '', t)
    t = re.sub(r'^a\s+', '', t)
    t = re.sub(r"[^\w\s]", "", t)
    t = re.sub(r'\s+', ' ', t).strip()
    return t


def load_hot100(conn: sqlite3.Connection) -> int:
    print("  Downloading Hot 100 CSV from GitHub...")
    with httpx.Client(timeout=60.0) as client:
        r = client.get(HOT100_CSV_URL)
    if not r.is_success:
        print(f"  ERROR: HTTP {r.status_code}")
        return 0

    content = r.content.decode("utf-8", errors="replace")
    reader  = csv.DictReader(io.StringIO(content))

    songs = {}
    for row in reader:
        try:
            artist    = (row.get("performer") or "").strip()
            title     = (row.get("song") or "").strip()
            peak_str  = (row.get("peak_position") or "").strip()
            weeks_str = (row.get("time_on_chart") or "").strip()
            date_str  = (row.get("chart_date") or row.get("chart_debut") or "").strip()
            if not artist or not title:
                continue
            peak  = int(peak_str)  if peak_str.isdigit()  else None
            weeks = int(weeks_str) if weeks_str.isdigit() else 1
            year  = int(date_str[:4]) if date_str and date_str[:4].isdigit() else None
            if not peak:
                continue
            key = (norm(artist), norm(title))
            if key not in songs:
                songs[key] = {"artist": artist, "title": title,
                              "peak": peak, "weeks": weeks, "year": year}
            else:
                if peak  < songs[key]["peak"]:  songs[key]["peak"]  = peak
                if weeks > songs[key]["weeks"]: songs[key]["weeks"] = weeks
        except Exception:
            continue

    now = datetime.utcnow().isoformat()
    inserted = 0
    for (an, tn), d in songs.items():
        try:
            conn.execute(
                """INSERT INTO chart_reference
                   (chart_name,artist,title,artist_norm,title_norm,
                    peak_position,weeks_on_chart,chart_year,data_source,loaded_at)
                   VALUES ('hot100',?,?,?,?,?,?,?,'hot100_csv',?)
                   ON CONFLICT(chart_name,artist_norm,title_norm) DO UPDATE SET
                       peak_position =MIN(peak_position,excluded.peak_position),
                       weeks_on_chart=MAX(weeks_on_chart,excluded.weeks_on_chart),
                       loaded_at=excluded.loaded_at""",
                (d["artist"], d["title"], an, tn,
                 d["peak"], d["weeks"], d["year"], now))
            inserted += 1
        except Exception as e:
            pass
    conn.commit()
    print(f"  Hot 100: {inserted} songs inserted/updated")
    return inserted


def load_billboard_chart(conn: sqlite3.Connection, chart_name: str) -> int:
    import billboard  # type: ignore
    slug = BILLBOARD_SLUGS.get(chart_name)
    if not slug:
        print(f"  No slug for {chart_name}, skipping")
        return 0

    songs = {}
    current_year = datetime.now().year

    print(f"  Fetching year-end charts {current_year}→1990...")
    for year in range(current_year, 1990, -1):
        try:
            chart = billboard.ChartData(f"year-end/{year}/{slug}", timeout=20)
            count = 0
            for entry in chart:
                artist = (entry.artist or "").strip()
                title  = (entry.title  or "").strip()
                if not artist or not title:
                    continue
                peak  = entry.peakPos or entry.rank or 100
                weeks = entry.weeks or 1
                key   = (norm(artist), norm(title))
                if key not in songs:
                    songs[key] = {"artist": artist, "title": title,
                                  "peak": peak, "weeks": weeks, "year": year}
                else:
                    if peak  < songs[key]["peak"]:  songs[key]["peak"]  = peak
                    if weeks > songs[key]["weeks"]: songs[key]["weeks"] = weeks
                count += 1
            if count > 0:
                print(f"    {year}: {count} entries")
            time.sleep(0.5)
        except Exception as e:
            print(f"    {year}: skipped ({e})")
            continue

    # Current chart
    try:
        chart = billboard.ChartData(slug, timeout=20)
        for entry in chart:
            artist = (entry.artist or "").strip()
            title  = (entry.title  or "").strip()
            if not artist or not title:
                continue
            key  = (norm(artist), norm(title))
            peak = entry.peakPos or entry.rank or 100
            if key not in songs:
                songs[key] = {"artist": artist, "title": title,
                              "peak": peak, "weeks": entry.weeks or 1,
                              "year": current_year}
            elif peak < songs[key]["peak"]:
                songs[key]["peak"] = peak
        print(f"    current chart: {len(chart)} entries")
    except Exception as e:
        print(f"    current chart failed: {e}")

    now = datetime.utcnow().isoformat()
    inserted = 0
    for (an, tn), d in songs.items():
        try:
            conn.execute(
                """INSERT INTO chart_reference
                   (chart_name,artist,title,artist_norm,title_norm,
                    peak_position,weeks_on_chart,chart_year,data_source,loaded_at)
                   VALUES (?,?,?,?,?,?,?,'billboard_scrape',?)
                   ON CONFLICT(chart_name,artist_norm,title_norm) DO UPDATE SET
                       peak_position =MIN(peak_position,excluded.peak_position),
                       weeks_on_chart=MAX(weeks_on_chart,excluded.weeks_on_chart),
                       loaded_at=excluded.loaded_at""",
                (chart_name, d["artist"], d["title"], an, tn,
                 d["peak"], d["weeks"], d["year"], now))
            inserted += 1
        except Exception as e:
            pass
    conn.commit()
    print(f"  {CHART_DISPLAY[chart_name]}: {inserted} songs inserted/updated")
    return inserted


def update_meta(conn: sqlite3.Connection, chart_name: str):
    now = datetime.utcnow().isoformat()
    row = conn.execute(
        "SELECT COUNT(*), MIN(chart_year), MAX(chart_year) FROM chart_reference WHERE chart_name=?",
        (chart_name,)).fetchone()
    conn.execute(
        "UPDATE chart_reference_meta SET status='loaded', entry_count=?, "
        "first_year=?, last_year=?, last_updated=? WHERE chart_name=?",
        (row[0], row[1], row[2], now, chart_name))
    conn.commit()


def main():
    print("=" * 60)
    print("  ChartHound Chart Reference Loader")
    print(f"  Database: {DB_PATH}")
    print("=" * 60)

    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")

    total = 0

    # 1. Hot 100 — fast CSV download
    print("\n[1/11] Billboard Hot 100 (CSV download)...")
    n = load_hot100(conn)
    total += n
    update_meta(conn, "hot100")

    # 2–11. Other charts via billboard library
    chart_order = ["ac","adultpop","country","rnb","rock","dance",
                   "ccm","gospel","ccm-ac","ccm-rock"]
    for i, chart_name in enumerate(chart_order, start=2):
        print(f"\n[{i}/11] {CHART_DISPLAY[chart_name]}...")
        n = load_billboard_chart(conn, chart_name)
        total += n
        update_meta(conn, chart_name)

    conn.close()

    print("\n" + "=" * 60)
    print(f"  DONE — {total:,} total entries loaded")
    print("=" * 60)


if __name__ == "__main__":
    main()
