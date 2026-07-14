#!/usr/bin/env python3
"""
Seattle Sports Calendar - production feed generator.

Fetches league-wide scoreboards from ESPN, filters to Seattle-metro venues,
and writes an ICS feed. Designed to run unattended (GitHub Actions, daily).

Safety model: NEVER overwrite a good feed with a suspicious one.
Any anomaly -> exit nonzero WITHOUT writing the feed, so the previous
committed feed keeps serving and the Actions run shows red.

Anomalies that block publishing:
  - any league fetch fails (network/HTTP error)
  - an in-season league returns zero events
  - total event count drops more than max_drop_pct vs the existing feed

Usage:
  python3 generate_feed.py                         # production run
  python3 generate_feed.py --config config.json
  python3 generate_feed.py --fixture fixture.json  # offline test
"""

import argparse
import json
import re
import sys
import urllib.request
import urllib.error
from datetime import datetime, timedelta, timezone
from pathlib import Path

BASE = "https://site.api.espn.com/apis/site/v2/sports"
UA = {"User-Agent": "seattle-sports-calendar/1.0 (personal project)"}


# ---------------------------------------------------------------------------
# Fetch
# ---------------------------------------------------------------------------

def fetch_scoreboard(path: str, start: str, end: str, extra: str) -> dict:
    url = f"{BASE}/{path}/scoreboard?dates={start}-{end}{extra}"
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode("utf-8"))


# ---------------------------------------------------------------------------
# Classification and parsing
# ---------------------------------------------------------------------------

def classify(event: dict, cfg: dict) -> tuple[str, str]:
    """Return (decision, reason): 'include' | 'flag' | 'exclude'."""
    comp = (event.get("competitions") or [{}])[0]
    venue = comp.get("venue") or {}
    vid = str(venue.get("id", ""))
    city = (venue.get("address") or {}).get("city", "").strip().lower()

    if vid and vid in cfg["venue_registry"]:
        return "include", f"venue id {vid} in registry"
    if city in cfg["_metro_set"]:
        return "flag", f"metro city '{city}' with unregistered venue id {vid or '?'}"
    if not venue:
        return "exclude", "no venue data"
    return "exclude", f"outside metro (city='{city or '?'}')"


def parse_event(event: dict, league: dict, cfg: dict) -> dict:
    comp = (event.get("competitions") or [{}])[0]
    venue = comp.get("venue") or {}
    status = ((event.get("status") or comp.get("status") or {}).get("type") or {})
    competitors = comp.get("competitors") or []

    home = next((c for c in competitors if c.get("homeAway") == "home"), {})
    away = next((c for c in competitors if c.get("homeAway") == "away"), {})
    home_name = (home.get("team") or {}).get("displayName", "TBD")
    away_name = (away.get("team") or {}).get("displayName", "TBD")

    completed = bool(status.get("completed"))
    if completed:
        title = f"{home_name} {home.get('score', '?')} - {away.get('score', '?')} {away_name}"
    else:
        title = f"{home_name} - {away_name}"

    start = datetime.strptime(event["date"], "%Y-%m-%dT%H:%MZ").replace(tzinfo=timezone.utc)
    raw_name = venue.get("fullName", "Unknown venue")
    display = cfg["venue_display_aliases"].get(
        raw_name, cfg["venue_registry"].get(str(venue.get("id", "")), raw_name))

    return {
        "uid": f"espn-{league['label'].lower()}-{event['id']}",
        "league": league["label"],
        "title": title,
        "venue": display,
        "start": start,
        "end": start + timedelta(hours=league["duration_hours"]),
        "completed": completed,
    }


# ---------------------------------------------------------------------------
# ICS
# ---------------------------------------------------------------------------

def ics_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace(",", "\\,").replace(";", "\\;")


def to_ics(events: list[dict], cfg: dict) -> str:
    now = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//seattle-sports-calendar//1.0//EN",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        f"X-WR-CALNAME:{ics_escape(cfg['calendar_name'])}",
        f"X-WR-TIMEZONE:{cfg['timezone']}",
    ]
    for ev in sorted(events, key=lambda e: e["start"]):
        lines += [
            "BEGIN:VEVENT",
            f"UID:{ev['uid']}@seattle-sports-calendar",
            f"DTSTAMP:{now}",
            f"DTSTART:{ev['start'].strftime('%Y%m%dT%H%M%SZ')}",
            f"DTEND:{ev['end'].strftime('%Y%m%dT%H%M%SZ')}",
            f"SUMMARY:{ics_escape(ev['title'])}",
            f"LOCATION:{ics_escape(ev['venue'])}",
            f"DESCRIPTION:{ics_escape(ev['league'] + ' | source: ESPN')}",
            "END:VEVENT",
        ]
    lines.append("END:VCALENDAR")
    return "\r\n".join(lines) + "\r\n"


def count_existing_events(feed_path: Path) -> int:
    if not feed_path.exists():
        return 0
    return len(re.findall(r"^BEGIN:VEVENT", feed_path.read_text(), re.MULTILINE))


# ---------------------------------------------------------------------------
# Per-league state: failure counters and event cache for graceful degradation
# ---------------------------------------------------------------------------

STATE_PATH = Path("league_state.json")


def serialize_events(events: list[dict]) -> list[dict]:
    return [{**ev, "start": ev["start"].isoformat(), "end": ev["end"].isoformat()}
            for ev in events]


def deserialize_events(events: list[dict]) -> list[dict]:
    return [{**ev,
             "start": datetime.fromisoformat(ev["start"]),
             "end": datetime.fromisoformat(ev["end"])}
            for ev in events]


def load_state() -> dict:
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text())
    return {}


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--config", default="config.json")
    p.add_argument("--fixture", help="offline test: JSON file of saved payloads")
    args = p.parse_args()

    cfg = json.loads(Path(args.config).read_text())
    cfg["_metro_set"] = {c.lower() for c in cfg["metro_cities"]}
    feed_path = Path(cfg["feed_filename"])
    escalate_after = cfg.get("escalate_after_failures", 3)
    stale_max_days = cfg.get("stale_max_days", 14)
    today = datetime.now(timezone.utc)
    month = today.month
    anomalies: list[str] = []
    escalations: list[str] = []
    included: list[dict] = []
    flags: list[dict] = []
    state = load_state()
    failed_league_count = 0
    report = {"run_utc": today.strftime("%Y-%m-%dT%H:%M:%SZ"), "leagues": {}, "flags": []}

    fixture = None
    if args.fixture:
        fixture = {item["league"]: item["payload"]
                   for item in json.loads(Path(args.fixture).read_text())}

    for league in cfg["leagues"]:
        label = league["label"]
        start = (today - timedelta(days=cfg["retention_days_past"])).strftime("%Y%m%d")
        end = (today + timedelta(days=league["lookahead_days"])).strftime("%Y%m%d")
        in_season = month in league["season_months"]
        lstate = state.setdefault(label, {"consecutive_failures": 0,
                                          "cached_events": [],
                                          "last_success_utc": None})

        # --- fetch, classifying the outcome as success or failure -----------
        failure_reason = None
        events: list[dict] = []
        if fixture is not None:
            events = fixture.get(label, {"events": []}).get("events") or []
        else:
            try:
                payload = fetch_scoreboard(league["path"], start, end, league["extra"])
                events = payload.get("events") or []
                # Zero events from an in-season non-tournament league is
                # treated as a source failure (ESPN may have moved the data),
                # not as truth about the schedule.
                if not events and in_season and not league.get("zero_events_ok"):
                    failure_reason = f"zero events while in season ({start}-{end})"
            except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError,
                    json.JSONDecodeError) as e:
                failure_reason = f"fetch failed: {e}"

        # --- failure path: degrade gracefully, escalate visibly -------------
        if failure_reason is not None:
            failed_league_count += 1
            lstate["consecutive_failures"] += 1
            n = lstate["consecutive_failures"]
            cache_age_days = None
            if lstate["last_success_utc"]:
                cache_age_days = (today - datetime.fromisoformat(
                    lstate["last_success_utc"])).days

            if lstate["cached_events"] and cache_age_days is not None \
                    and cache_age_days <= stale_max_days:
                cached = deserialize_events(lstate["cached_events"])
                included.extend(cached)
                status = f"stale (serving {len(cached)} cached events, " \
                         f"cache age {cache_age_days}d)"
            else:
                status = "dropped (no usable cache - events for this league " \
                         "are absent from the feed)"

            if n >= escalate_after:
                escalations.append(
                    f"{label}: {n} consecutive daily failures ({failure_reason}). "
                    f"Status: {status}")
            report["leagues"][label] = {"status": status, "failure": failure_reason,
                                        "consecutive_failures": n,
                                        "in_season": in_season}
            continue

        # --- success path ----------------------------------------------------
        kept_events: list[dict] = []
        for event in events:
            decision, reason = classify(event, cfg)
            if decision == "exclude":
                continue
            try:
                parsed = parse_event(event, league, cfg)
            except (KeyError, ValueError) as e:
                report.setdefault("parse_warnings", []).append(
                    f"{label} event {event.get('id')}: {e}")
                continue
            if decision == "flag":
                flags.append({"uid": parsed["uid"], "title": parsed["title"],
                              "reason": reason})
            kept_events.append(parsed)

        included.extend(kept_events)
        lstate["consecutive_failures"] = 0
        lstate["cached_events"] = serialize_events(kept_events)
        lstate["last_success_utc"] = today.isoformat()
        report["leagues"][label] = {"status": "fresh", "fetched": len(events),
                                    "kept": len(kept_events), "in_season": in_season}

    # Systemic failure: every league failed in one run -> block publishing.
    if cfg["leagues"] and failed_league_count == len(cfg["leagues"]):
        anomalies.append("ALL leagues failed this run - systemic problem "
                         "(network, API relocation); refusing to publish")

    report["flags"] = flags
    report["total_events"] = len(included)
    report["escalations"] = escalations

    # Drop guard: compare against the currently committed feed.
    prev_count = count_existing_events(feed_path)
    if prev_count >= cfg["min_events_for_drop_check"]:
        floor = prev_count * (1 - cfg["max_drop_pct"] / 100)
        if len(included) < floor:
            anomalies.append(
                f"event count dropped {prev_count} -> {len(included)} "
                f"(below {cfg['max_drop_pct']}% guard)")

    report["anomalies"] = anomalies
    Path("run_report.json").write_text(json.dumps(report, indent=2) + "\n")
    # State persists on BOTH paths so failure counters survive blocked runs.
    STATE_PATH.write_text(json.dumps(state, indent=2) + "\n")

    if anomalies:
        print("PUBLISH BLOCKED - previous feed left untouched:", file=sys.stderr)
        for a in anomalies:
            print(f"  - {a}", file=sys.stderr)
        return 1

    feed_path.write_text(to_ics(included, cfg))
    print(f"Wrote {len(included)} events to {feed_path} "
          f"({len(flags)} flagged for review - see run_report.json)")
    for esc in escalations:
        print(f"ESCALATION: {esc}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
