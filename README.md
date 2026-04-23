# 🐕 ChartHound — The New World

**A self-hosted music library management engine built for power users.**

Tag your music with real Billboard chart data, discover missing chart hits, hunt albums by any artist, and automatically find missing movies and TV episodes — all from a single Dockerized dashboard that never exposes your API keys.

**Developed by Colby R. Curtis** · [Buy Me a Coffee](https://buymeacoffee.com/colbycurtis)

> Built with Python (FastAPI), SQLite, and vanilla JavaScript.
> Code support by Claude.ai (Anthropic).

---

## Table of Contents

- [Why ChartHound?](#why-charthound)
- [Security Model](#security-model)
- [Requirements](#requirements)
- [Quick Start](#quick-start)
- [Docker Compose Reference](#docker-compose-reference)
- [First Boot & Setup Wizard](#first-boot--setup-wizard)
- [The Tabs](#the-tabs)
  - [The Kennel — Connection Vault](#the-kennel--connection-vault)
  - [The Retriever — Metadata Tagger](#the-retriever--metadata-tagger)
  - [The Sniffer — Chart Hit Finder](#the-sniffer--chart-hit-finder)
  - [The Groomer — Playlist Builder](#the-groomer--playlist-builder)
  - [The Bloodhound — Album Hunter](#the-bloodhound--album-hunter)
  - [The Tracker — Missing Media Hunter](#the-tracker--missing-media-hunter)
  - [The Veterinarian — Database Admin](#the-veterinarian--database-admin)
- [Reverse Proxy Setup](#reverse-proxy-setup)
- [Updating ChartHound](#updating-charthound)
- [Troubleshooting](#troubleshooting)
- [License](#license)

---

## Why ChartHound?

Most music management tools focus on one thing — tagging, or searching, or playlist building. ChartHound combines all of them into a single self-hosted application that understands your entire media ecosystem.

ChartHound knows which songs in your library were Billboard #1 hits, which albums you're missing from an artist's discography, and which movies in your Radarr watchlist still haven't been found. It writes real chart performance data directly into your music file metadata so your media server can display it. And it does all of this without ever sending your API keys or tokens outside your local network.

---

## Security Model

ChartHound was designed from the ground up with security as a hard requirement — not an afterthought. Every architectural decision prioritizes keeping your credentials safe.

### Encrypted Vault (The Kennel)

Every API key, token, and password you enter into ChartHound is encrypted using **Fernet symmetric encryption** (AES-128-CBC with HMAC-SHA256) before being stored in the SQLite database. The encryption key is your `SECRET_KEY` environment variable, which never leaves your Docker container. There is no "show password" button. There is no API endpoint that returns decrypted credentials. Keys are decrypted in-memory only at the moment they are needed to make an API call, and only on the backend — the browser never sees them.

### Zero Key Transmission

When ChartHound connects to your Plex, Radarr, Sonarr, Prowlarr, or any other service, those API calls happen **server-side inside the Docker container**. Your browser sends a request to ChartHound's backend ("search for this artist"), and the backend handles the actual API call using the decrypted credentials. Your API keys are never included in any HTTP response sent to the browser. They never appear in network traffic between your browser and ChartHound.

### Auto-Lockdown Registration

On first boot, ChartHound allows one user registration to create the admin account. After that, the registration endpoint is automatically disabled and the "Sign Up" link is hidden from the UI. No configuration needed — it locks itself. If you need to add another user later, you temporarily set `CH_OPEN_REGISTRATION=true` in your compose file, add the user, and set it back to `false`.

### Session Authentication

Every API endpoint (except login/register and the health check) requires a valid JWT session token. Unauthenticated requests are rejected with a 401. There are no backdoor endpoints, no debug routes that bypass auth, no settings pages accessible without a session.

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
- Last.fm API key (free — for chart estimation and trending data)

**For music discovery (Sniffer & Bloodhound):**
- Prowlarr (indexer manager)
- qBittorrent (download client)

**For movie/TV hunting (Tracker):**
- Radarr (movie management)
- Sonarr (TV management)
- Prowlarr connected to Radarr/Sonarr (ChartHound tells Radarr/Sonarr to search — they handle Prowlarr internally)

**Optional:**
- Discogs personal access token (additional metadata source)
- YouTube API key (future Scout tab)

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

Navigate to `http://YOUR-SERVER-IP:8585` in your browser. The setup wizard will guide you through creating your admin account and connecting your first services.

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

## First Boot & Setup Wizard

When you first open ChartHound, you'll see a login screen. Since no users exist yet, click **"Create Account"** to register your admin account. After registration, the signup option is permanently hidden (auto-lockdown).

The setup wizard walks you through three steps:

1. **Welcome** — overview of ChartHound
2. **Generate a SECRET_KEY** — shows the command if you haven't done it yet
3. **Connect services** — directs you to The Kennel to add your API keys

After the wizard, head to **The Kennel** to connect your services. Each service card has a URL field, a token/API key field, a **Save** button, and a **Test** button. Always test after saving to verify the connection works.

---

## The Tabs

### The Kennel — Connection Vault
🔑 *Connections & Encrypted API Vault*

This is where you connect ChartHound to your media stack. Each service gets a card with fields for the server URL and API key/token. When you click Save, the credentials are encrypted with your SECRET_KEY and stored in the database. When you click Test, ChartHound decrypts the key server-side, makes a test API call, and reports back whether the connection succeeded.

**Supported services:** Plex, Emby, Jellyfin, Last.fm, Prowlarr, Radarr, Sonarr, qBittorrent, Deluge, Transmission, YouTube, Discogs

**Path Translator:** At the bottom of The Kennel, the Path Translator helps ChartHound convert between your media server's file paths and Docker's `/music` mount point. Enter your server's music library prefix (the path you see in Plex/Emby/JF file info) and ChartHound handles the rest.

### The Retriever — Metadata Tagger
🏷 *Write Metadata to Physical Files*

The Retriever scans your music library through your media server (Plex, Emby, or Jellyfin) and writes genre, mood, and year tags directly to your physical audio files using Mutagen. It uses a multi-source waterfall to find the best metadata: MusicBrainz → Last.fm → ListenBrainz → Deezer → Discogs → iTunes.

Tags are written to the actual file on disk before refreshing your media server — this is the "File-First" principle. Your metadata survives even if you switch media servers.

### The Sniffer — Chart Hit Finder
📡 *Find Missing Chart Hits & Grab Them*

The Sniffer cross-references your music library against a database of over 108,000 real Billboard chart entries. It shows you which chart hits you own and which ones you're missing. Two modes:

- **Chart Gap Fill** — Select which Billboard charts to check (Hot 100, Country, R&B, Rock, etc.), set a year range and peak position filter, and see every charting song you don't own.
- **Trending** — Browse top tracks by genre using Last.fm data.

For any missing track, click to search Prowlarr for album torrents. Results show seeders, size, and indexer. One-click grab sends the torrent to qBittorrent with a `charthound-music` category tag.

### The Groomer — Playlist Builder
✂️ *Build Playlists from What You Own*

The Groomer scans your library, looks up each track against the chart reference database, and writes chart performance data into the COMMENT tag of your music files. A track that peaked at #4 on the Hot 100 for 12 weeks gets a comment like: `Hot 100: #4 (12 wks) | Adult Pop: #1 (18 wks)`.

It also generates star ratings (1–5) based on chart performance and can build smart playlists that you push directly to Plex, Emby, or Jellyfin.

Features a skip cache system so re-scans skip tracks that have already been checked, making 33,000+ track libraries manageable.

### The Bloodhound — Album Hunter
🔍 *Hunt Every Album by Any Artist*

Three search modes powered by MusicBrainz:

- **Artist Search** — Find an artist, then browse their complete discography filtered by release type (Albums, Compilations, Singles, All).
- **Album Search** — Search for any album by name across all of MusicBrainz.
- **Compilation Search** — 31 preset compilation series (Now That's What I Call Music, WOW Hits, Grammy Nominees, etc.) plus custom search.

Every result shows whether you already own it (cross-referenced against your library). Missing releases can be searched on Prowlarr and grabbed to qBittorrent directly from the results table.

### The Tracker — Missing Media Hunter
🎯 *Radarr / Sonarr Automatic Search*

The Tracker monitors your Radarr and Sonarr libraries for missing movies and TV episodes, then automatically tells them to search for downloads. ChartHound never touches Prowlarr directly — it fires search commands through Radarr/Sonarr's own API, so they handle indexer selection, category tagging, and download client handoff exactly as they normally would.

**Key features:**

- **Default OFF** — must be explicitly enabled from the Tracker page
- **Smart TV ordering** — searches for the earliest missing season first; won't look for season 3 if season 2 is still missing
- **Season search** — when an entire season is missing, fires a single SeasonSearch instead of individual episode searches
- **Cooldown system** — won't re-search the same unfindable item until the cooldown expires (default 7 days)
- **Daily cap** — limits total searches per day to prevent overloading (default 100)
- **Manual override** — skip a stuck season to allow later seasons to be searched, or manually trigger a search for any specific item
- **Activity log** — tracks every search, sync, and error with timestamps
- **Runs in background** — continues hunting even when the browser is closed, survives container restarts

### The Veterinarian — Database Admin
🩺 *Database Health & Admin Tools*

Database health monitoring, skip cache statistics, maintenance tools (VACUUM, integrity checks), and a danger zone for clearing the database. Includes a debug console (off by default, 1000-line cap) for troubleshooting.

### Future Tabs

- **The Scout** (Milestone 9) — YouTube playlist creator
- **The Lookout** (Milestone 10) — Local music video manager

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

---

## License

**Copyright © 2026 Colby R. Curtis. All Rights Reserved.**

This software is licensed for **personal, non-commercial use only**. You may view the source code and run the software for personal use. Modification, redistribution, forking (beyond local personal use), and commercial use are strictly prohibited without prior written consent. See [LICENSE.md](LICENSE.md) for full terms.

**ChartHound** and all tab names (The Kennel, The Retriever, The Sniffer, The Groomer, The Bloodhound, The Tracker, The Scout, The Lookout) are protected identifiers of this project.

---

*ChartHound — Developed by Colby R. Curtis with Claude.ai code support.*

[Buy Me a Coffee](https://buymeacoffee.com/colbycurtis)
