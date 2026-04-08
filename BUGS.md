# ChartHound — Known Issues & Bug Tracker

> Last updated: April 2026 · Milestone 3 Testing
> Developed by Colby R. Curtis · Claude.ai code support

---

## 🔴 Active Bugs (To be fixed in Milestone 4)

### BUG-001 — SAVE overwrites encrypted token with empty string
**Tab:** The Kennel  
**Severity:** High  
**Description:** When a connection card is saved with the token/password field left blank (showing ●●●●●●●● stored encrypted), the existing encrypted token is overwritten with an empty value. This silently breaks the saved connection.  
**Expected:** If token field is blank on save, keep the existing encrypted token intact. Only update the token if a new value is entered.  
**Fix planned:** Milestone 4 — kennel.py save endpoint checks if token is empty before overwriting.

---

### BUG-002 — SAVE resets URL field back to default placeholder
**Tab:** The Kennel  
**Severity:** High  
**Description:** After saving a connection, the URL field sometimes resets back to the default placeholder value (e.g. `http://localhost:32400`) instead of keeping the user-entered value.  
**Expected:** URL field should retain whatever value the user entered after saving.  
**Fix planned:** Milestone 4 — frontend save logic preserves field values after successful save.

---

### BUG-003 — Wizard saved connections do not carry over to Kennel
**Tab:** First Run Wizard → The Kennel  
**Severity:** High  
**Description:** Connections saved during the First Run Wizard (Plex, Emby, Last.fm etc.) appear saved in the wizard but show as NOT VERIFIED in The Kennel after the wizard closes. Users must re-enter all credentials manually in The Kennel.  
**Expected:** Connections saved in wizard should be immediately reflected in The Kennel with correct verified status.  
**Fix planned:** Milestone 4 — Wizard simplified to SECRET_KEY + Music Path only. All connections moved to The Kennel exclusively.

---

### BUG-004 — Wizard Finish button does nothing
**Tab:** First Run Wizard  
**Severity:** Medium  
**Description:** On the final Ready screen of the wizard, the Finish button does not close the wizard or navigate into the app.  
**Expected:** Finish button should close the wizard and enter ChartHound.  
**Fix planned:** Milestone 4 — Wizard rebuild.

---

### BUG-005 — Music Path pre-fill not registering on save
**Tab:** The Kennel → Path Translator  
**Severity:** Medium  
**Description:** When the Path Translator fields are pre-filled programmatically from saved database values, clicking SAVE TRANSLATION returns "Enter a server path prefix first" even though the field appears populated. Users must manually retype or paste the path to trigger save.  
**Expected:** Pre-filled values should register correctly and save without requiring manual re-entry.  
**Fix planned:** Milestone 4 — dispatch input event after programmatic field population.

---

### BUG-006 — qBittorrent connection test too lenient
**Tab:** The Kennel → Download Client  
**Severity:** Medium  
**Description:** The qBittorrent connection test passes if the server returns anything other than "Fails." — including error pages from unrelated services on the same port. A false positive connection is reported.  
**Expected:** Test should only pass if qBittorrent returns exactly "Ok." confirming successful authentication.  
**Fix planned:** Milestone 4 — kennel.py qBittorrent test checks for `r.text.strip() == "Ok."`.

---

### BUG-007 — Default URL placeholders use localhost (fails on Linux Docker)
**Tab:** The Kennel — all connection cards  
**Severity:** High  
**Description:** All connection cards default to `http://localhost:PORT`. On Linux Docker, `localhost` inside the container refers to the container itself, not the host machine. All connection tests fail until users manually change to their server's LAN IP address.  
**Expected:** Default placeholder should be `http://YOUR-SERVER-IP:PORT` to make it clear that localhost will not work.  
**Fix planned:** Milestone 4 — update all default URL values and placeholder text.

---

### BUG-008 — No SECRET_KEY warning for new users
**Tab:** App-wide  
**Severity:** High  
**Description:** If a user runs the container with the placeholder SECRET_KEY value, the app refuses to start entirely. The user sees a Docker error with no guidance on how to fix it. They never reach the wizard or any helpful instructions.  
**Expected:** App should start in a limited mode with a prominent banner explaining exactly what SECRET_KEY is, how to generate it for their OS, and how to restart the container after setting it.  
**Fix planned:** Milestone 4 — Option A implementation. App loads with warning banner. Connection saving disabled until real key detected.

---

### BUG-009 — Wizard goal checkboxes opt-in instead of opt-out
**Tab:** First Run Wizard — Step 1  
**Severity:** Low  
**Description:** Only "Tag my music files" is pre-checked by default. Users must actively check everything else they want to set up. This means new users may miss features they would want.  
**Expected:** All goals should be pre-checked by default. Users uncheck what they don't need.  
**Fix planned:** Milestone 4 — Wizard rebuild, all goals selected by default.

---

### BUG-010 — Connection TEST shows immediate failure on slow server restart
**Tab:** The Kennel  
**Severity:** Low  
**Description:** After restarting a service (e.g. Jellyfin after a settings change), hitting TEST immediately returns a 503 error because the service hasn't fully started yet. Users assume the connection is broken when it just needs a few more seconds.  
**Expected:** On 503 response, automatically retry once after 5 seconds and show "Server starting up — retrying..." message.  
**Fix planned:** Milestone 4 — add auto-retry logic to test endpoint handler.

---

## 🟡 Known Limitations (By Design or Deferred)

### LIMIT-001 — Jellyfin requires Docker subnet in LAN Networks
**Description:** ChartHound's Docker container connects from IP `172.28.0.1` which Jellyfin treats as a remote connection. Users must add `172.28.0.0/16` to Jellyfin's LAN Networks setting for connections to work.  
**Workaround:** Jellyfin Dashboard → Networking → LAN Networks → add `172.28.0.0/16`  
**Status:** Documented in docker-compose.example.yml and will be added to Jellyfin tooltip in Milestone 4.

---

### LIMIT-002 — Music volume must not use :ro (read only)
**Description:** The Retriever needs write access to music files to tag them with Mutagen. Using `:ro` on the Docker volume mount will cause all file writes to fail silently.  
**Workaround:** Use `- "/your/music/path:/music"` without the `:ro` suffix.  
**Status:** Fixed in docker-compose.example.yml. Will be documented clearly for new users in Milestone 4.

---

### LIMIT-003 — localhost does not work for service URLs on Linux Docker
**Description:** On Linux, Docker containers cannot reach the host machine via `localhost`. The host's LAN IP address (e.g. `192.168.50.42`) must be used instead. Alternatively `host.docker.internal` works if `extra_hosts` is configured in docker-compose.yml.  
**Workaround:** Use your server's LAN IP address for all service URLs in The Kennel.  
**Status:** Will be documented in all tooltips and default placeholders updated in Milestone 4.

---

## ✅ Fixed Bugs

### FIXED-001 — Two Uvicorn workers cause database lock on startup
**Fixed in:** Milestone 2 hotfix  
**Description:** Running 2 Uvicorn workers caused both to try initializing the SQLite database simultaneously, causing a lock error and preventing startup.  
**Fix:** Reduced to 1 worker in Dockerfile. Fixed `get_user_count()` to use fresh connection.

---

### FIXED-002 — passlib/bcrypt version conflict
**Fixed in:** Milestone 2 hotfix  
**Description:** `passlib[bcrypt]` was incompatible with newer `bcrypt` versions causing password hashing to fail with a ValueError on registration.  
**Fix:** Replaced passlib with direct `bcrypt==4.0.1` usage in security.py.

---

### FIXED-003 — aiosqlite "threads can only be started once" error
**Fixed in:** Milestone 2 hotfix  
**Description:** Database connections were being reused after closing, causing a RuntimeError in auth.py and kennel.py endpoints.  
**Fix:** Replaced all `get_db()` usage with fresh `async with aiosqlite.connect()` blocks in every endpoint.

---

### FIXED-004 — Music volume mounted read-only (:ro)
**Fixed in:** Milestone 3 testing  
**Description:** docker-compose.example.yml had `:ro` on the music volume mount which would prevent Mutagen from writing metadata tags.  
**Fix:** Removed `:ro` from volume mount. Added explanation in comments.
