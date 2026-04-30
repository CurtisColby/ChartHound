# 🐕 ChartHound — The New World

**A self-hosted music *and media* library management engine built for power users.**

Tag your music with real Billboard chart data, discover missing chart hits, hunt albums by any artist — *and* automatically find missing movies and TV episodes from your Radarr/Sonarr libraries. All from a single Dockerized dashboard that never exposes your API keys.

**Developed by Colby R. Curtis** · [Buy Me a Coffee](https://buymeacoffee.com/colbycurtis)

> Built with Python (FastAPI), SQLite, and vanilla JavaScript.
> Code support by Claude.ai (Anthropic).

---

## A Note From the Developer

ChartHound started life as a janky little HTML file on my laptop.

I was tired of my Plex music library being a graveyard of mistagged albums and missing chart hits, so I wrote a 200-line Python script to make custom playlists for my server. Then I needed a way to know which songs in my library were actually #1 hits, so I bolted on a chart database. Then I needed it to push playlists to Plex. Then to Emby. Then to Jellyfin.

At some point I looked up and realized "just a playlist creator" wasn't going to cut it anymore. So I spent many evenings and weekends developing a full-featured music and media management tool — adding album hunting through MusicBrainz, missing-media tracking through Radarr and Sonarr, chart hit discovery, file-first metadata tagging, and a properly secured connection vault for all the API keys it now needed. My OCD had absolutely taken the wheel, and the "little tool just for me" had grown teeth.

I'm a solo developer. ChartHound exists because I wanted it to exist. I write the code with Claude (Anthropic's AI) as my code support partner — Claude helps me think through architecture, catches my bugs in code review, and writes the boilerplate I'd otherwise have to type by hand. Every architectural decision, every feature, and every line of business logic is mine. I actively review every change, question every approach, and push back when something isn't right. The result is code I actually understand, written faster than I could write alone.

Months of work later, I did a full security audit and hardening pass on April 24, 2026 — auditing the auth flow, encrypting credentials, locking down the registration endpoint, reviewing every endpoint for token exposure — before deciding to share ChartHound with the community. If you're a Plex, Emby, or Jellyfin user who cares about your music library the way I care about mine, I think you'll find a lot to like here.

If ChartHound saves you time, [a coffee](https://buymeacoffee.com/colbycurtis) is appreciated but never expected. The tool is yours — go make your music library beautiful.

— **Colby**

---

## Table of Contents

- [Why ChartHound?](#why-charthound)
- [Security Model](#security-model)
- [Requirements](#requirements)
- [Quick Start](#quick-start)
- [Docker Compose Reference](#docker-compose-reference)
- [First Boot](#first-boot)
- [Getting API Keys](#getting-api-keys)
- [The Tabs](#the-tabs)
- [The Little Things](#the-little-things)
- [Reverse Proxy Setup](#reverse-proxy-setup)
- [Updating ChartHound](#updating-charthound)
- [Troubleshooting](#troubleshooting)
- [Support ChartHound](#support-charthound)
- [License](#license)

---

## Why ChartHound?

Most music tools focus on one thing — tagging, or searching, or playlist building. Most media tools focus only on movies and TV. ChartHound combines all of them into a single self-hosted application that understands your entire media ecosystem — music, movies, and TV all in one place.

ChartHound knows which songs in your library were Billboard #1 hits, which albums you're missing from an artist's discography, and which movies and TV episodes in your Radarr/Sonarr watchlists still haven't been found. It writes real chart performance data directly into your music file metadata so your media server can display it. It hunts down missing media in the background while you sleep. And it does all of this without ever sending your API keys or tokens outside your local network.

---

## Security Model

ChartHound was designed from the ground up with security as a hard requirement — not an afterthought. A full security audit and hardening pass was completed on April 24, 2026, before the project was shared publicly.

### Encrypted Vault (The Kennel)

Every API key, token, and password you enter into ChartHound is encrypted using **Fernet symmetric encryption** (AES-128-CBC with HMAC-SHA256) before being stored in the SQLite database. The encryption key is your `SECRET_KEY` environment variable, which never leaves your Docker container. There is no "show password" button. There is no API endpoint that returns decrypted credentials. Keys are decrypted in-memory only at the moment they are needed to make an API call, and only on the backend — the browser never sees them.

### Zero Key Transmission

When ChartHound connects to your Plex, Radarr, Sonarr, Prowlarr, or any other service, those API calls happen **server-side inside the Docker container**. Your browser sends a request to ChartHound's backend ("search for this artist"), and the backend handles the actual API call using the decrypted credentials. Your API keys are never included in any HTTP response sent to the browser. They never appear in network traffic between your browser and ChartHound.

### Auto-Lockdown Registration

On first boot, ChartHound allows one user registration to create the admin account. After that, the registration endpoint is automatically disabled and the "Sign Up" link is hidden from the UI. No configuration needed — it locks itself. If you need to add another user later, you temporarily set `CH_OPEN_REGISTRATION=true` in your compose file, add the user, and set it back to `false`.

### Session Authentication

Every API endpoint (except login/register and the health check) requires a valid JWT session token. Unauthenticated requests are rejected with a 401. There are no backdoor endpoints, no debug routes that bypass auth, no settings pages accessible without a session. Sessions last 7 days and gracefully redirect to the login screen with a clear banner when expired.

### Reverse Proxy Requirement

ChartHound should always be accessed through a reverse proxy with a valid SSL/TLS certificate when exposed beyond your local network. The application itself runs on HTTP inside the container (port 8000). Your reverse proxy (Caddy, Nginx, Traefik) handles HTTPS termination. This ensures all traffic between your browser and ChartHound is encrypted in transit. See the [Reverse Proxy Setup](#reverse-proxy-setup) section below.

---

## Requirements

ChartHound connects to your existing media stack. You don't need all of these — only the ones relevant to the features you want to use.

**Required:**
- Docker and Docker Compose
- A music library on a local or network drive

**For metadata tagging (Retriever & Groomer):**
- Plex, Emby, or Jellyfin (to scan your library)
- **Last.fm API key** — *strongly recommended.* Crucial for popularity lookups on tracks not in the static Billboard chart database. ChartHound's metadata waterfall works without it, but you'll see significantly weaker chart estimation for non-static-DB tracks (independent releases, deep cuts, international hits, anything outside major US charts). Free, takes 30 seconds.

**For music discovery (Sniffer & Bloodhound):**
- Prowlarr (indexer manager)
- qBittorrent (download client)

**For movie/TV hunting (Tracker):**
- Radarr (movie management)
- Sonarr (TV management)
- Prowlarr connected to Radarr/Sonarr (ChartHound tells Radarr/Sonarr to search — they handle Prowlarr internally)

**Optional but recommended:**
- Discogs personal access token (excellent genre and style metadata, especially for classic rock, jazz, soul, and any artist where MusicBrainz tags are sparse)

---

## Quick Start

### 1. Clone the Repository

```bash
git clone https://github.com/CurtisColby/ChartHound.git
cd ChartHound
```

### 2. Generate Your Secret Key

Pick one command for your platform:

```bash
# Linux / Mac / Unraid / TrueNAS
python3 -c "import secrets; print(secrets.token_hex(32))"

# Windows PowerShell
python -c "import secrets; print(secrets.token_hex(32))"

# If Python is not installed
docker run --rm python:3.12-slim python3 -c "import secrets; print(secrets.token_hex(32))"
```

Copy the output. You'll paste it into your compose file in the next step.

### 3. Configure Docker Compose

```bash
cp docker-compose_example.yml docker-compose.yml
```

Open `docker-compose.yml` in a text editor and replace every `CHANGE_ME` value. The file is heavily commented — read each section carefully. The critical values are:

- **`SECRET_KEY`** — paste the key you generated above
- **Music library path** — the folder where your music lives
- **`MEDIA_SERVER_MUSIC_PREFIX`** — the path your media server uses for your music (check any track's file info in Plex/Emby/Jellyfin)

### 4. Build and Start

```bash
docker compose up --build -d
```

### 5. Open ChartHound

Navigate to `http://YOUR-SERVER-IP:8585` in your browser. Create your admin account, then head to **The Kennel** to connect your services. A full **User Guide** is available inside the app — look for the **OPEN USER GUIDE** button in The Kennel's left panel.

---

## Docker Compose Reference

| Environment Variable | Required | Description |
|---|---|---|
| `SECRET_KEY` | Yes | Fernet encryption key for all stored credentials. Generate with `secrets.token_hex(32)`. Treat like a master password. |
| `DATABASE_URL` | No | Path to dynamic database inside container. Default: `/data/charthound.db` |
| `STATIC_DB_URL` | No | Path to static chart database. Default: `/data/charthound_static.db` |
| `CH_OPEN_REGISTRATION` | No | Set to `true` to allow new user registration. Default: `false` (auto-lockdown after first user). |
| `MEDIA_SERVER_MUSIC_PREFIX` | Yes | The file path prefix your media server uses for music. Used for path translation. |
| `DOCKER_MUSIC_PREFIX` | No | The mount point inside Docker. Default: `/music`. Do not change unless you modified the volume mapping. |

| Volume Mount | Purpose |
|---|---|
| `YOUR_MUSIC_PATH:/music` | Your music library. ChartHound writes metadata tags to files here. Never moves, renames, or deletes files. |
| `YOUR_MUSIC_PATH:YOUR_MUSIC_PATH` | Second mount at the same path. Required for FLAC tag writing on NAS/network drives due to Docker filesystem behavior. |
| `YOUR_STATIC_DB_PATH:/data/charthound_static.db:ro` | Pre-populated Billboard chart database. Read-only. Ships with the repository. |
| `charthound_data:/data` | Named volume for the dynamic database. Persists across container rebuilds. |

---

## First Boot

When you first open ChartHound, you'll see a login screen. Since no users exist yet, click **"Create Account"** to register your admin account. After registration, the signup option is permanently hidden (auto-lockdown — see [Security Model](#security-model) for details).

Once logged in, head straight to **The Kennel** to connect your services. Each service card has a URL field, a token/API key field, a **Save** button, and a **Test** button. Always test after saving to verify the connection works. The [Getting API Keys](#getting-api-keys) section below walks you through how to obtain each one.

> 📖 **A full User Guide is built into ChartHound.** Once logged in, find the **OPEN USER GUIDE** button in The Kennel's left panel under "HELP & DOCS". It covers every tab — what it does, what needs to be connected, and best practices — including step-by-step first-time setup instructions.

---

## Getting API Keys

Most of ChartHound's features rely on free third-party APIs. None of them require credit cards. Here's how to get the ones that matter most.

### Last.fm API Key (strongly recommended)

Last.fm is **crucial for popularity lookups on tracks that aren't in the static chart database.** ChartHound ships with over 108,000 real Billboard chart entries pre-loaded, but for any track outside that set — independent releases, deep cuts, international hits, anything not on a major US chart — Last.fm provides the popularity signal that lets the Sniffer, Groomer, and Retriever do their best work. **The waterfall metadata system works without it, but you'll see significantly weaker chart estimation for non-static-DB tracks.**

It's free and takes 30 seconds.

**Step 1 — Create a Last.fm API account**

Go to [last.fm/api/account/create](https://www.last.fm/api/account/create). Fill in the application name (e.g., `ChartHound`), description, and contact email. Application Homepage and Callback URL can be left blank or filled with anything — ChartHound doesn't use OAuth.

**Step 2 — Copy the API Key**

Last.fm gives you both an API Key and a Shared Secret. ChartHound only needs the **API Key** (the longer string). Copy it.

**Step 3 — Paste it into The Kennel**

In ChartHound, open **The Kennel**, find the Last.fm card, paste the key, **Save**, then **Test**.

### Discogs Personal Access Token (recommended for genre data)

Discogs provides excellent genre and style metadata, especially for classic rock, jazz, soul, and any artist where MusicBrainz tags are sparse. It's optional but noticeably improves the metadata waterfall.

**Step 1 — Create a Discogs account**

Sign up at [discogs.com](https://www.discogs.com/) if you don't already have one. (Discogs is also a fantastic site for collectors — worth using even outside ChartHound.)

**Step 2 — Generate a personal access token**

Go to [discogs.com/settings/developers](https://www.discogs.com/settings/developers) and click **Generate new token**. Copy the token immediately — Discogs only shows it once.

**Step 3 — Paste it into The Kennel**

In ChartHound, open **The Kennel**, find the Discogs card, paste the token, **Save**, then **Test**.

> Discogs rate-limits at 60 requests per minute. ChartHound respects this automatically.

### Plex / Emby / Jellyfin Tokens

Each media server hands out tokens differently:

- **Plex:** Sign in at [plex.tv](https://www.plex.tv/), then visit any item in your web app. View the page source and search for `X-Plex-Token` — your token is the value. Or follow [this official Plex guide](https://support.plex.tv/articles/204059436-finding-an-authentication-token-x-plex-token/).
- **Emby:** Dashboard → Advanced → API Keys → **+ New API Key**. Name it `ChartHound`.
- **Jellyfin:** Dashboard → Advanced → API Keys → **+ Add Key**. Name it `ChartHound`.

For all three, paste the token (and the server URL — usually something like `http://192.168.1.x:32400` for Plex or `http://192.168.1.x:8096` for Emby/Jellyfin) into the corresponding card in The Kennel and test.

> **Jellyfin gotcha:** if the test fails despite a correct token, add Docker's bridge subnet (`172.28.0.0/16`) to Jellyfin's LAN Networks list (Dashboard → Networking → LAN Networks). Jellyfin blocks unrecognized internal IPs by default.

### Radarr / Sonarr / Prowlarr

For all three, the API key is in **Settings → General → API Key**. Copy it from the Arr app's UI and paste it into the matching ChartHound Kennel card along with the server URL.

### qBittorrent

qBittorrent uses username/password authentication, not an API key. Open the qBittorrent Web UI settings and confirm Web UI is enabled, then put the URL (`http://your-server:8080`), username (default `admin`), and password into ChartHound's qBittorrent card. ChartHound logs in once per session and uses the cookie.

### A note on Spotify

ChartHound has no Spotify integration — and that's deliberate. Spotify changed their developer terms to require a **Premium account** to use their API, which defeats the entire purpose of running your own self-hosted music library. ChartHound gets the popularity, charting, and metadata signals it needs from MusicBrainz, Last.fm, ListenBrainz, Deezer, Discogs, iTunes, and the static Billboard chart database — all free, all working without strings attached.

---

## The Tabs

### The Kennel — Connection Vault
🔑 *Connections & Encrypted API Vault*

This is where you connect ChartHound to your media stack. Each service gets a card with fields for the server URL and API key/token. When you click Save, the credentials are encrypted with your SECRET_KEY and stored in the database. When you click Test, ChartHound decrypts the key server-side, makes a test API call, and reports back whether the connection succeeded.

**Supported services:** Plex, Emby, Jellyfin, Last.fm, Prowlarr, Radarr, Sonarr, qBittorrent, Deluge, Transmission, Discogs

**Path Translator:** At the bottom of The Kennel, the Path Translator helps ChartHound convert between your media server's file paths and Docker's `/music` mount point. Enter your server's music library prefix (the path you see in Plex/Emby/JF file info) and ChartHound handles the rest.

### The Retriever — Metadata Tagger
🏷 *Write Metadata to Physical Files*

The Retriever scans your music library through your media server (Plex, Emby, or Jellyfin) and writes genre, mood, and year tags directly to your physical audio files using Mutagen. It uses a multi-source waterfall to find the best metadata: MusicBrainz → Last.fm → ListenBrainz → Deezer → Discogs → iTunes.

Tags are written to the actual file on disk before refreshing your media server — this is the "File-First" principle. Your metadata survives even if you switch media servers.

### The Sniffer — Chart Hit Finder
📡 *Find Missing Chart Hits & Grab Them*

The Sniffer cross-references your music library against a master list of chart hits and popular tracks, showing you what you own and what you're missing. It uses your connected media server (Plex first, then Emby, then Jellyfin) for the library comparison — meaning it sees your entire library, not just folders you've scanned in The Retriever. Two modes:

- **Chart Gap Fill** — Select genres, decades, and a notability tier (Essential/Notable/Deep Cuts), and see every charting song you don't own. Narrowing to a single genre gives you up to 1,000 of that genre's biggest hits. Add a decade filter to go even deeper.
- **Trending** — Browse top tracks by genre using Last.fm data.

For any missing track, click to search Prowlarr for album torrents. Results show seeders, size, and indexer. One-click grab sends the torrent to qBittorrent with a `charthound-music` category tag.

### The Groomer — Playlist Builder
✂️ *Build Playlists from What You Own*

The Groomer scans your library, looks up each track against the chart reference database, and writes chart performance data into the COMMENT tag of your music files. A track that peaked at #4 on the Hot 100 for 12 weeks gets a comment like: `Hot 100: #4 (12 wks) | Adult Pop: #1 (18 wks)`.

It also generates star ratings (1–5) based on chart performance and can build smart playlists that you push directly to Plex, Emby, or Jellyfin.

Features a skip cache system so re-scans skip tracks that have already been checked, making 33,000+ track libraries manageable. The Groomer is also where Last.fm shows its true value — for tracks not in the static Billboard database, the Last.fm popularity score becomes the primary signal for chart estimation. Without a Last.fm key, those tracks fall back to weaker heuristics.

### The Bloodhound — Album Hunter
🔍 *Hunt Every Album by Any Artist*

Four search modes powered by MusicBrainz:

- **Artist Search** — Find an artist, then browse their complete discography filtered by release type (Albums, Compilations, Singles, All). Results are sortable by Artist, Title, Year, or Owned/Missing status.
- **Album Search** — Search for any album by name across all of MusicBrainz.
- **Compilation Search** — 31 preset compilation series (Now That's What I Call Music, WOW Hits, Grammy Nominees, etc.) plus custom search.
- **Genre Browse** — Browse by primary genre (Rock, Pop, Country, R&B, Hip-Hop, Electronic, Jazz, Blues, Metal, Alternative, Folk, Classical, CCM/Gospel). Selecting a genre automatically pulls in all its sub-genres behind the scenes.

Every result shows whether you already own it (cross-referenced against your library). Missing releases can be searched on Prowlarr and grabbed to qBittorrent directly from the results table.

### The Tracker — Missing Media Hunter
🎯 *Radarr / Sonarr Automatic Search*

The Tracker monitors your Radarr and Sonarr libraries for missing movies and TV episodes, then automatically tells them to search for downloads. ChartHound never touches Prowlarr directly — it fires search commands through Radarr/Sonarr's own API, so they handle indexer selection, category tagging, and download client handoff exactly as they normally would.

**Key features:**

- **Default OFF** — must be explicitly enabled from the Tracker page
- **Smart TV ordering** — searches for the earliest missing season first; won't look for season 3 if season 2 is still missing
- **Season search** — when an entire season is missing, fires a single SeasonSearch instead of individual episode searches
- **Cooldown system** — won't re-search the same unfindable item until the cooldown expires (default 7 days)
- **Daily cap** — limits total searches per day to prevent overloading (Moderate preset = 60/day)
- **Manual override** — skip a stuck season to allow later seasons to be searched, or manually trigger a search for any specific item
- **Activity log** — tracks every search, sync, and error with timestamps
- **Runs in background** — continues hunting even when the browser is closed, survives container restarts
- **Request jitter** — small randomized delay between searches so your indexers see human-like patterns instead of bot bursts

### The Veterinarian — Database Admin
🩺 *Database Health & Admin Tools*

The Veterinarian is where you check on ChartHound's internal database — track counts, artist counts, album counts, skip cache statistics, dynamic vs. static DB sizes, and maintenance tools (VACUUM, integrity checks). Includes an in-tab debug console (off by default, 1,000-line cap) for troubleshooting scans without polluting Docker logs.

The Veterinarian also hosts the **Clear Full Database** button — and it's in the danger zone for a reason. ChartHound's database accumulates valuable scan knowledge over time (see [The Little Things](#the-little-things) for why). Clearing it sends you back to first-scan speeds and discards weeks of learned data. Only use it if the database is corrupted or you want a genuinely fresh start. For routine maintenance, use VACUUM and integrity check instead — they tidy up without wiping anything.

---

## The Little Things

Software like this lives or dies on the details. ChartHound has spent a lot of time on the things you'd never notice — until they save you from a problem you didn't know you had.

### The Sniffer's deduplication logic

When the Sniffer builds a playlist of missing chart hits, it doesn't just throw every match into the result list. It dedupes by track identity (artist + title, normalized) and — when you have multiple copies of the same song in your library — picks the highest-quality version automatically. A 320kbps MP3 wins over a 128kbps one. A FLAC wins over both. You don't see the lower-quality dupes; the playlist gets the best of what you own.

### The Tracker's request jitter

When the Tracker fires search commands at Radarr or Sonarr, it doesn't blast them out at predictable intervals. Each search has a small randomized delay (jitter) added before it goes out, so the request pattern looks more like a human clicking buttons than a bot pounding an API. This keeps your indexers happier and reduces the chance of getting rate-limited mid-batch.

### The Groomer's skip cache

Re-scanning a 33,000-track library is expensive. The Groomer remembers which tracks have already been processed and skips them on subsequent runs unless something changed. The first scan takes a while; every scan after that is fast. The cache is automatically invalidated when a track's path or metadata changes.

### Your library gets faster the longer you use it

ChartHound's database isn't just storage — it's a knowledge cache. Every scan teaches it something. Track fingerprints, chart matches, MusicBrainz IDs, Discogs lookups, Last.fm popularity scores, album-folder hashes, the skip cache, the path index, the genre-resolution waterfall results — all of it accumulates and gets reused on subsequent runs. The first scan of a 33,000-track library is the slowest one you'll ever do. Every scan after that gets faster as the database fills out and fewer tracks need fresh lookups.

This is why the Veterinarian's "Clear Full Database" button is in the danger zone — and why you should treat it that way. Clearing the database wipes all that accumulated knowledge and sends you back to first-scan speeds. Only use it if the database has actually become corrupted, or if you want to genuinely start over. Day-to-day, a database that's been growing for weeks is a feature, not clutter.

### File-first metadata writes

Every metadata update goes to the *physical file* via Mutagen *before* ChartHound asks Plex/Emby/Jellyfin to refresh. This means your tags survive a media server migration. If you switch from Plex to Jellyfin tomorrow, your chart data goes with you because it lives in the files, not in some database you can't export.

### Bounded debug consoles

The Veterinarian's debug console (and other in-tab consoles) are capped at 1,000 lines and turned off by default. Debug output can pile up fast — a long-running scan can generate thousands of log lines per minute. The cap and opt-in behavior keep the UI snappy and the noise out of your way unless you explicitly ask for it.

### Album batching for chart lookups

When the Groomer scans your library, it groups tracks by album folder and makes one API call per album instead of one per track. For a 33,000-track library, that's the difference between ~33,000 API calls and ~3,000. Same data, an order of magnitude less work.

### Aggressive caching everywhere

Static chart data ships in a read-only SQLite database with the project. Lookups against your library check local SQLite first, external APIs second. The whole architecture is designed around the assumption that your time and your API quotas matter.

### Auto-lockdown registration

The first user to register becomes the admin. The moment that user exists, the registration endpoint auto-disables and the "Sign Up" link is hidden from the UI. There's no checkbox to forget, no manual step to remember. ChartHound locks itself.

### Encrypted credentials, never visible

Every API key, token, and password is Fernet-encrypted before it touches the database. There is no "show password" button. There is no API endpoint that returns decrypted credentials. Keys are decrypted in memory only at the moment they're used, on the backend, never sent to the browser. If someone steals your `charthound.db` file, they get nothing usable without your `SECRET_KEY`.

### iTunes rate-limit respect

When ChartHound talks to iTunes for metadata, it strictly enforces a 20-requests-per-minute fixed window — Apple's documented limit. We never burst-fire requests, even during big batch operations. It takes longer; it doesn't get our IPs throttled.

---

## Reverse Proxy Setup

ChartHound should be placed behind a reverse proxy with SSL/TLS when accessed outside your local network. Here are examples for common reverse proxies.

### Caddy (Recommended — automatic HTTPS)

```
charthound.yourdomain.com {
    reverse_proxy localhost:8585
}
```

That's it. Caddy automatically provisions and renews a Let's Encrypt certificate.

### Nginx

```nginx
server {
    listen 443 ssl;
    server_name charthound.yourdomain.com;

    ssl_certificate     /etc/letsencrypt/live/charthound.yourdomain.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/charthound.yourdomain.com/privkey.pem;

    location / {
        proxy_pass http://localhost:8585;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

### Traefik (Docker labels)

```yaml
labels:
  - "traefik.enable=true"
  - "traefik.http.routers.charthound.rule=Host(`charthound.yourdomain.com`)"
  - "traefik.http.routers.charthound.tls.certresolver=letsencrypt"
  - "traefik.http.services.charthound.loadbalancer.server.port=8000"
```

---

## Updating ChartHound

```bash
cd ~/ChartHound
git pull
docker compose up --build -d
```

Your dynamic database (`charthound.db`) is stored in a Docker named volume and survives rebuilds. Your connections, scan history, and user account are preserved.

---

## Troubleshooting

**Container won't start after an update:**
```bash
docker stop charthound
docker compose up --build -d
```

**Forgot your password:**
```bash
docker exec charthound python3 -c "
import bcrypt, asyncio, aiosqlite
async def reset():
    hashed = bcrypt.hashpw(b'NewPassword1!', bcrypt.gensalt()).decode()
    async with aiosqlite.connect('/data/charthound.db') as db:
        await db.execute('UPDATE users SET password_hash=? WHERE username=?', (hashed, 'YourUsername'))
        await db.commit()
asyncio.run(reset())"
```

**Jellyfin connection fails:**
Add Docker's subnet to Jellyfin's LAN Networks list: `172.28.0.0/16` (Jellyfin Dashboard → Networking → LAN Networks).

**SECRET_KEY warning on startup:**
You haven't replaced the placeholder key in your `docker-compose.yml`. Generate one with `python3 -c "import secrets; print(secrets.token_hex(32))"` and restart the container.

**qBittorrent on a different machine:**
ChartHound and qBittorrent don't need to be on the same server. Enter the qBittorrent machine's LAN IP and port in The Kennel (e.g., `http://192.168.1.100:8080`).

**Session expired banner appears unexpectedly:**
ChartHound sessions last 7 days. After that you'll see a clear "session expired" banner on the login screen — log back in and you're good. Your data, settings, and connections are all preserved.

---

## Support ChartHound

ChartHound is free for personal use today, and that isn't changing anytime soon. The current license is non-commercial — it's the tool I built for myself, shared with anyone who can use it. If it saves you time or makes your library better, here are some ways to chip in.

### Buy Me a Coffee

The easiest way. One-time tips of any amount.

[☕ buymeacoffee.com/colbycurtis](https://buymeacoffee.com/colbycurtis)

### Monthly sponsorship *(coming soon)*

GitHub Sponsors enrollment is in progress. Once active, it'll let you set up recurring contributions starting around $2/month with a custom-amount option. Watch this space — link will go live as soon as enrollment completes.

### Cryptocurrency

For users who prefer crypto, donations are welcome:

- **Bitcoin (BTC · Native SegWit):**  
  `bc1qdr9j5al9qq29cskrjxvm4myzq4se4c3kk6p8hv`
- **Ethereum (ETH · ERC-20 OK):**  
  `0x60D4519eA1CcBAB149403e232C54468572f783C7`

These addresses match the ones in ChartHound's in-app donation panel, so you can verify them by checking your own UI.

### Other ways to help (free)

If money isn't on the table, there's plenty else that helps:

- **⭐ Star the repo on GitHub** — visibility helps more than people think
- **🐛 Report bugs** — open an issue with reproduction steps and I'll get to it
- **💡 Share use cases** — tell me how you're using ChartHound, what's working, what's not. Real user feedback shapes the roadmap.
- **📣 Spread the word** — tell other Plex/Emby/Jellyfin users. r/selfhosted, r/Plex, the Awesome-Selfhosted list, your homelab Discord.

---

## License

**Copyright © 2026 Colby R. Curtis. All Rights Reserved.**

This software is licensed for **personal, non-commercial use only**. You may view the source code and run the software for personal use. Modification, redistribution, forking (beyond local personal use), and commercial use are strictly prohibited without prior written consent. See [LICENSE.md](LICENSE.md) for full terms.

**ChartHound** and all tab names (The Kennel, The Retriever, The Sniffer, The Groomer, The Bloodhound, The Tracker, The Veterinarian) are protected identifiers of this project.

---

*ChartHound — Developed by Colby R. Curtis with Claude.ai code support.*

[Buy Me a Coffee](https://buymeacoffee.com/colbycurtis)
