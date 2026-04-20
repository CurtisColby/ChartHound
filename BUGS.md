# ChartHound — Known Bugs & Issues
**Last Updated:** April 19, 2026 — Post Milestone 6

---

## ACTIVE BUGS

### Kennel
- **Emby/Jellyfin library selector "Poll error: Failed to fetch"** — Likely `ParentId` filter format issue when selecting specific libraries. Connection works, but library dropdown polling fails.

### Retriever
- **Local Folder scan freezes the container** — Blocking I/O on NAS mount locks the event loop. Needs `run_in_executor` wrapper.
- **Auto-Pilot not writing tags** — Scan completes but tags are not written to files.
- **Comment tag writing wrong data** — Comment field populated with incorrect content.
- **Preview mode lacks Keep/Dismiss option** — No way for user to selectively keep or dismiss individual rows before writing.

### Sniffer / Bloodhound (Shared)
- **Indexer name shows as "Indexer #60"** — Torznab results only have the indexer ID, not the human-readable name. Need to resolve ID → name from Prowlarr's `/api/v1/indexer` response.

---

## KNOWN QUIRKS (Not Bugs — By Design or Third-Party)

- **qBit occasional manual checkmark needed** — Background checkmark task sets priority 1 but qBittorrent occasionally doesn't flip the checkbox on certain torrents. User may need to manually checkmark in qBit's file pane. Low frequency. Add tooltip/banner.
- **qBit torrent names show as hashes initially** — Normal behavior when added via Prowlarr proxy URL. Resolves once metadata fetches from peers. Dead/fake torrents never resolve — user deletes manually.
- **MusicBrainz 1 req/sec rate limit** — Enforced in code. Large artist discographies or compilation searches may feel slow. This is MusicBrainz policy, not a bug.

---

## RESOLVED (This Session — April 19, 2026)

- ✅ **Sniffer single-card overwrite** — Selecting a second track replaced the first card. Now uses multi-card system with independent results.
- ✅ **Background checkmark multi-grab collision** — Previously queried "most recently added charthound-music torrent," causing one task to fix the wrong torrent. Now captures specific hash per grab.
- ✅ **Torrent metadata never resolving (chicken-and-egg)** — Files at priority 0 meant no peer connection, so metadata never arrived. Background task now force-resumes first to trigger metadata fetch, then sets priorities once files appear.
