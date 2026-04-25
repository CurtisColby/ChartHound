# Future Ideas / Jobs

*Refreshed at end of session April 25, 2026.*

The big README rewrite is done. The personal origin story, "The Little Things"
craft section, per-service API walkthroughs, security audit completion note,
softened monetization language, Lookout cut, Scout-backend-complete status —
all landed in this session's README pass.

## Tier 1 — Next session

**The Scout — Cycle 3 (frontend tab).** Final piece of M9. Three-pane layout
mirroring Bloodhound. Backend contracts are stable; build against them.
See Current_State_Summary.md "What's queued for next session" for full
spec including filter chips, result table columns, output buttons,
quota bar, and "How it works" callout. After Cycle 3 lands and is
confirmed working, fast-forward merge `dev` → `main` as the M9 milestone.

## Tier 2 — Quick deliberate decision

- **Tracker Moderate preset cap.** Currently 60/day. Possibly bump to
  100/day if Radarr/Sonarr can handle it without indexer pushback.
  Code change is trivial; the decision is what matters.

## Tier 3 — When energy permits

- Security audit remainder: H7/H8, H3/H4, H9/H10/M11, L5/L11
- `docker-compose.example.yml` drift audit
- YouTube Kennel test cosmetic cleanup (use `videoCategories` instead of `mine=true`)
- Old `/media/colby/NAS1/charthound/` archive folder cleanup

## Tier 4 — When ready to go public

- Complete GitHub Sponsors enrollment, uncomment `github:` line in `.github/FUNDING.yml`
- Update README "Monthly sponsorship" subsection from "coming soon" to live link
- Post launch announcement to r/selfhosted, r/Plex, Awesome-Selfhosted list

## Tier 5 — Possible future features

- **The Lookout** (was cut this session) — local music video manager.
  May return if community demand emerges. Plex-only scope was the original
  blocker; could be reconsidered if there's a clean cross-server abstraction.
- **Scout shape C upgrade** — direct OAuth-based playlist push to user's
  YouTube account, replacing the current deeplink approach. Would require
  Google Cloud Console OAuth client setup, consent screen, `youtube.force-ssl`
  scope verification (multi-week Google review). Only worth pursuing if
  shape B's "deeplink → user clicks Save in YouTube" friction proves to be
  a real complaint.
- **Static DB importers expansion** — current importers (Chart2000, tsort,
  Kworb US, Billboard Christian, UK Official) have proven the pattern.
  Future targets: regional charts (Australian ARIA, German Offizielle,
  Japanese Oricon), niche genre charts, classical/jazz publications.
