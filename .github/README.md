# Seattle Sports Calendar

Auto-updating ICS feed of major sports events at greater Seattle venues.
Fetches league schedules from ESPN's public JSON API daily, filters by
venue location (not by team), and publishes via GitHub Pages. Finished
games get their final score written into the event title.

## Repo contents

| File | Purpose |
|---|---|
| `generate_feed.py` | Fetch, filter, anomaly-check, write `seattle.ics` |
| `config.json` | All Seattle-specific data: metro cities, venue registry, leagues, lookaheads |
| `.github/workflows/update-feed.yml` | Daily run + commit-on-change |
| `index.html` | Landing page with the subscription link |
| `seattle.ics` | The feed (generated on first run - do not hand-edit) |
| `run_report.json` | Per-run audit: counts, flags, anomalies (generated on first run) |
| `league_state.json` | Per-league failure counters + event cache for graceful degradation (generated on first run, committed by the workflow) |

## One-time setup

1. **Fill in `config.json`:**
   - Paste your confirmed venue IDs into `venue_registry`
     (from the validator's `discover` mode).
   - Replace each league's `lookahead_days` with the ceilings you measured.
2. **Create a GitHub repo** (public, so Pages is free) and push these files.
3. **Enable Pages:** repo Settings -> Pages -> Source: "Deploy from a branch"
   -> Branch: `main`, folder `/ (root)`.
4. **First run:** Actions tab -> "Update calendar feed" -> "Run workflow".
   Confirm it goes green and commits `seattle.ics`.
5. **Subscribe:** open `https://<username>.github.io/<repo>/` - the landing
   page shows the feed URL and per-app instructions.
6. *(Optional)* Custom domain: add a CNAME record pointing
   `calendar.yourdomain.com` at `<username>.github.io`, then set the custom
   domain in the Pages settings. This is independent of any Netlify site.

## Operations: the failure model

Failures are handled per severity - the feed degrades before it blocks,
and every failure path has a detection mechanism:

| Situation | Behavior | How you find out |
|---|---|---|
| One league fails (fetch error, or in-season zero events) | Feed still publishes; that league's previously fetched events are served from cache, marked `stale` in `run_report.json` | Report status; failure counter in `league_state.json` |
| Same league fails `escalate_after_failures` days in a row (default 3) | Still publishing (cached) | A GitHub Issue is auto-filed in this repo; further days add comments to the same issue |
| League still failing past `stale_max_days` (default 14) | That league's events drop from the feed (a postponed game we cannot re-fetch must not be shown at a stale time) | Issue keeps updating; report shows `dropped` |
| League recovers | Counter resets to 0, fresh data resumes, cache re-primed | Report shows `fresh` |
| ALL leagues fail in one run | Publish blocked entirely (systemic: network/API relocation) | Red run + failure email + Issue |
| Event count drops > `max_drop_pct` vs current feed | Publish blocked | Red run + failure email + Issue |
| **The workflow stops running at all** | Nothing inside the workflow can catch this (GitHub auto-disables schedules after ~60 days of repo inactivity; Actions outages; misconfig) | **Dead-man's switch**: the last workflow step pings healthchecks.io on success; if pings stop, healthchecks emails you. Setup: create a free check at healthchecks.io with a 2-day grace period, store its ping URL as the `HEALTHCHECK_URL` repo secret. Optional but strongly recommended - it is the only guard against silent death. |

Notes:
- `league_state.json` (failure counters + per-league event cache) is
  committed on every run. This also keeps the repo "active", so the 60-day
  auto-disable should never trigger while the pipeline is healthy.
- **Flags are homework, not errors.** `run_report.json` lists events that
  entered the feed via the metro-city fallback with an unregistered venue
  ID. Verify each, then add the ID to `venue_registry` in `config.json`.
- **Score timing:** the job runs at 12:00 UTC (4-5am Pacific). Google
  Calendar refreshes subscribed feeds on its own 12-24h+ cycle, so scores
  typically appear in Google within a day of the game. Apple Calendar can
  be set to refresh faster.
- **Off-season silence is normal.** A league outside its `season_months`
  returning zero events is expected: no failure, no counter.
- **Tournaments are different.** Competitions with irregular or non-annual
  calendars (World Cup and similar) carry `"zero_events_ok": true` in
  `config.json`: zero events is never treated as failure for them, because
  their schedules cannot be predicted by month. Fetch errors still count.
  Use this flag for any tournament-type league you add.

## Adding a league

1. Confirm the slug with the audit script (`audit_leagues.py`, kept locally).
2. Add a row to `leagues` in `config.json` (college leagues need
   `&groups=NN&limit=500` in `extra`).
3. Run `discover` locally if a Seattle team plays in it; otherwise let the
   first runs flag venue IDs and promote them into the registry.
4. Measure and set `lookahead_days`; set `season_months`.

## V2 (other cities)

Everything city-specific lives in `config.json`. A new city = a new config
file plus a second workflow step invoking
`python3 generate_feed.py --config <city>.json`. The venue-based filter
needs no code changes.
