# ChartHound — Known Bugs & Issues
**Last Updated:** April 19, 2026 — Post Milestone 6

---

## ACTIVE BUGS

### Kennel
- No Known bugs at this time.
### Retriever
- No Known Bugs at this time.

### Sniffer / Bloodhound (Shared)
- No Known Bugs at this time.
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
