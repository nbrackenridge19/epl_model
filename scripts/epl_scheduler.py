"""
EPL model: keeps the Cloudflare trigger Worker's per-match timers in sync with the fixture table.

WHAT THIS DOES
--------------------------------------------------------------------------
The Worker (worker/) fires the lineup and odds workflows at T-50 / T-25 / T-5 before each kickoff,
but it does no scraping and has no database access, so something has to tell it each match's kickoff
time. That is this script. Each run:

  1. Refreshes matches.kickoff_time from ESPN's scoreboard (sync_kickoff_times, imported from
     poll_espn_lineups.py). Today's and tomorrow's matches are always re-checked because broadcasters
     move kickoffs close to matchday; dates further out are only filled in when still blank.
  2. Reads the upcoming matches that have a kickoff_time from the database and POSTs each one to the
     Worker's /match/{match_id}/schedule endpoint. The Worker treats a repeat of an unchanged kickoff as
     a no-op, and a changed kickoff as a move (it discards the old timers and sets new ones), so running
     this often is safe and is how rescheduled matches get picked up.

It is started every 30 minutes by the Worker's own cron trigger (repository_dispatch event
"epl-schedule"), with a slower GitHub cron as a backup -- see .github/workflows/epl_scheduler.yml.

SAFETY
--------------------------------------------------------------------------
DRY_RUN defaults to true: nothing is written to the database and nothing is POSTed to the Worker, it
only prints what it would do. The workflow passes DRY_RUN=false for automatic runs.

Exits 1 if any POST to the Worker failed, so the run shows up red in GitHub Actions.

Environment: DATABASE_URL, WORKER_URL, SCHEDULE_SECRET, optional SCRAPERAPI_PROXY_URL,
optional DRY_RUN (default true), optional LOOKAHEAD_DAYS (default 3).
"""

import os
import sys
import time
from datetime import datetime, timedelta, timezone

import requests
from sqlalchemy import text

import poll_espn_lineups as lineups  # same folder; reuses its engine and sync_kickoff_times

DRY_RUN = os.environ.get("DRY_RUN", "true").strip().lower() != "false"
LOOKAHEAD_DAYS = int(os.environ.get("LOOKAHEAD_DAYS", "3"))
WORKER_URL = os.environ.get("WORKER_URL", "").rstrip("/")
SCHEDULE_SECRET = os.environ.get("SCHEDULE_SECRET", "")
REQUEST_TIMEOUT = 20

# A match is still worth (re)scheduling until the last job's expiry: lineup jobs run to kickoff + 10 min.
STILL_ACTIONABLE_MIN = 12


def upcoming_matches(engine, season):
    with engine.connect() as conn:
        rows = conn.execute(
            text("""
                select m.id, ht.code, at.code,
                       (m.match_date + m.kickoff_time) at time zone 'UTC' as kickoff_utc,
                       m.kickoff_time is null as missing
                from matches m
                join teams ht on ht.id = m.home_team_id
                join teams at on at.id = m.away_team_id
                where m.season = :season and m.status != 'completed'
                  and m.match_date between current_date - 1 and current_date + :days
                order by m.match_date, m.kickoff_time
            """),
            {"season": season, "days": LOOKAHEAD_DAYS},
        ).fetchall()
    return rows


def post_schedule(match_id, kickoff_iso):
    """-> (ok, detail). Never raises, so one bad match doesn't stop the rest."""
    url = f"{WORKER_URL}/match/{match_id}/schedule"
    if DRY_RUN:
        print(f"    [dry-run] would POST {url} body={{'kickoff_iso': {kickoff_iso!r}}}")
        return True, "dry-run"
    try:
        r = requests.post(
            url,
            headers={"Authorization": f"Bearer {SCHEDULE_SECRET}", "Content-Type": "application/json"},
            json={"kickoff_iso": kickoff_iso},
            timeout=REQUEST_TIMEOUT,
        )
    except Exception as e:  # noqa: BLE001
        return False, f"request failed: {e}"
    if r.status_code != 200:
        return False, f"HTTP {r.status_code}: {r.text[:200]}"
    try:
        info = r.json()
        return True, "rescheduled" if info.get("rescheduled") else "unchanged"
    except ValueError:
        return True, r.text[:100]


def main():
    now = datetime.now(timezone.utc)
    print(f"EPL scheduler -- DRY_RUN={DRY_RUN} -- now (UTC) {now.isoformat(timespec='seconds')} -- "
          f"lookahead {LOOKAHEAD_DAYS}d", flush=True)

    if not WORKER_URL:
        raise SystemExit("WORKER_URL is not set.")
    if not SCHEDULE_SECRET and not DRY_RUN:
        raise SystemExit("SCHEDULE_SECRET is not set.")

    engine = lineups.engine
    season = lineups.SEASON

    if DRY_RUN:
        print("Step 1: kickoff-time refresh skipped in dry run (it writes to the database).", flush=True)
    else:
        print("Step 1: refreshing kickoff times from ESPN", flush=True)
        try:
            teams = lineups.teams_map(engine)
            matches = lineups.match_id_map(engine, season)
            lineups.sync_kickoff_times(engine, teams, matches, season)
        except Exception as e:  # noqa: BLE001
            # Keep going: the table still holds the last known kickoff times.
            print(f"  kickoff refresh failed ({e}); continuing with the times already in the table.", flush=True)

    print("Step 2: sending kickoff times to the Worker", flush=True)
    rows = upcoming_matches(engine, season)
    horizon = now + timedelta(days=LOOKAHEAD_DAYS)
    sent = failed = skipped = missing = 0
    for match_id, home, away, kickoff_utc, is_missing in rows:
        if is_missing or kickoff_utc is None:
            missing += 1
            print(f"  {home} vs {away}: no kickoff time stored yet -- skipping", flush=True)
            continue
        kickoff_utc = kickoff_utc.astimezone(timezone.utc)
        if kickoff_utc < now - timedelta(minutes=STILL_ACTIONABLE_MIN) or kickoff_utc > horizon:
            skipped += 1
            continue
        kickoff_iso = kickoff_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
        ok, detail = post_schedule(match_id, kickoff_iso)
        print(f"  {home} vs {away} ({match_id}) kickoff {kickoff_iso}: {'OK' if ok else 'FAILED'} -- {detail}",
              flush=True)
        if ok:
            sent += 1
        else:
            failed += 1
        time.sleep(0.2)

    print(f"Done. sent={sent} failed={failed} outside-window={skipped} no-kickoff-yet={missing}", flush=True)
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
