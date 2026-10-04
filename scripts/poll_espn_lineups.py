"""
EPL model: single-match ESPN lineup poller.

REDESIGNED 2026-10-04 -- now started per match by the Cloudflare trigger Worker.

History: this used to run hourly from a GitHub `schedule:` cron and decide for itself which of
today's matches were in an actionable window. GitHub's scheduler proved unreliable (long gaps, then
no scheduled runs at all), so timing moved to a Cloudflare Worker that sends a `repository_dispatch`
event ("epl-lineup") at T-50 minutes before each kickoff and again at T-25 (a safety run). See
worker/ and .github/workflows/epl_lineup_trigger.yml.

This script now handles exactly ONE match, given by the MATCH_ID environment variable (the
matches.id UUID). It polls ESPN's summary endpoint for that match every POLL_INTERVAL_SEC (5 min)
inside this one job run until BOTH teams' lineups are posted, or until LATE_CUTOFF_MIN minutes after
kickoff. It exits 1 if the lineups were never captured so the failure shows up as a red run in
GitHub Actions instead of passing silently.

The T-25 safety run is queued behind the T-50 run by the workflow's per-match concurrency group. By
the time it starts, the lineups are normally already captured, and lineup_already_captured() makes it
exit immediately. It only does real work if the T-50 run crashed.

kickoff_time is stored as a bare TIME (no timezone in the column type), by convention interpreted as
UTC to match match_date (also effectively a UTC calendar date, consistent with ESPN's own `date`
field -- always UTC, e.g. "2026-09-04T19:00Z"). match_date + kickoff_time together reconstruct the
full UTC kickoff instant. Keeping matches.kickoff_time accurate is the job of sync_kickoff_times()
below, which epl_scheduler.py calls every 30 minutes.

Deliberately does NOT use subbedIn/subbedOut -- confirmed those fields are inconsistently shaped
across competitions (plain booleans in some, {'didSub': bool} objects in others), but this poller
doesn't need them at all: it only cares about the PRE-match predicted starter/bench split, which the
'starter' boolean gives directly and consistently.

Each write UPSERTs (on conflict do update) rather than accumulates -- polling repeatedly as kickoff
approaches naturally keeps the latest prediction, overwriting an earlier guess if the lineup changes.

Environment: DATABASE_URL, MATCH_ID, optional SCRAPERAPI_PROXY_URL, optional DRY_RUN=true.

SETUP:
    pip install requests sqlalchemy psycopg2-binary --break-system-packages
"""

import os
import time
from datetime import datetime, timedelta, timezone

import requests
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
from sqlalchemy import create_engine, text

DATABASE_URL = os.environ["DATABASE_URL"]
engine = create_engine(DATABASE_URL, pool_pre_ping=True, pool_recycle=280)

SEASON = 2627

# Phase A: how many days ahead to keep kickoff_time populated for.
SYNC_DAYS_AHEAD = 5

# In-job lineup polling: keep trying until this long AFTER kickoff, checking every POLL_INTERVAL_SEC.
LATE_CUTOFF_MIN = 10
POLL_INTERVAL_SEC = 300  # 5 min between in-job poll attempts
# Lineup rows written at or after (kickoff - this) count as "already captured" for the safety run. ESPN
# lineups appear roughly an hour out; the first poll is at T-50, so anything written since T-55 is from this flow.
CAPTURED_SINCE_MIN = 55

SCOREBOARD_URL = "https://site.api.espn.com/apis/site/v2/sports/soccer/eng.1/scoreboard"
SUMMARY_URL_TMPL = "https://site.api.espn.com/apis/site/v2/sports/soccer/eng.1/summary?event={event_id}"

# SAFETY: DRY_RUN=true prints what it WOULD write without touching the database. Defaults to a real run,
# because this only ever starts from a real dispatch for a real match (use the workflow's dry_run input to test).
DRY_RUN = os.environ.get("DRY_RUN", "false").strip().lower() == "true"

S = requests.Session()
S.headers.update({"User-Agent": "Mozilla/5.0", "Accept": "application/json"})

# Same reasoning as the odds scraper: ESPN blocks GitHub Actions' runner
# IPs specifically, confirmed via real 403s. Proxy only when available
# (GitHub Actions), unproxied locally (already confirmed working).
_proxy_url = os.environ.get("SCRAPERAPI_PROXY_URL")
PROXIES = {"http": _proxy_url, "https": _proxy_url} if _proxy_url else None

# Same authoritative mapping as the odds scraper (from epl_team_codes.xlsx),
# plus the 2026-27 promoted teams.
ESPN_NAME_TO_CODE = {
    "Arsenal": "ars", "Aston Villa": "avl", "AFC Bournemouth": "bou", "Brentford": "bre",
    "Brighton & Hove Albion": "bri", "Burnley": "bur", "Chelsea": "che",
    "Crystal Palace": "cry", "Everton": "eve", "Fulham": "ful", "Leeds United": "lee",
    "Liverpool": "liv", "Manchester City": "mci", "Manchester United": "man",
    "Newcastle United": "new", "Nottingham Forest": "ntf", "Sunderland": "sun",
    "Tottenham Hotspur": "tot", "West Ham United": "whm", "Wolverhampton Wanderers": "wol",
    "Coventry City": "cov", "Hull City": "hul", "Ipswich Town": "ips",
}


def fetch_json(url, params=None, timeout=20):
    r = S.get(url, params=params, proxies=PROXIES, timeout=timeout, verify=(PROXIES is None))
    r.raise_for_status()
    return r.json()


def teams_map(engine):
    with engine.connect() as conn:
        rows = conn.execute(text("select id, code from teams")).fetchall()
    return {code: tid for tid, code in rows}


def match_id_map(engine, season):
    with engine.connect() as conn:
        rows = conn.execute(
            text("select id, home_team_id, away_team_id from matches where season = :season"),
            {"season": season},
        ).fetchall()
    return {(h, a): mid for mid, h, a in rows}


def parse_games_from_scoreboard(sb):
    games = []
    for ev in sb.get("events", []) or []:
        event_id = ev.get("id")
        comps = ev.get("competitions") or []
        if not event_id or not comps:
            continue
        competitors = (comps[0].get("competitors") or [])
        home = next((c for c in competitors if c.get("homeAway") == "home"), None)
        away = next((c for c in competitors if c.get("homeAway") == "away"), None)

        def team_name(c):
            return (c.get("team") or {}).get("displayName") if c else None

        # ESPN's event-level "date" is always UTC (e.g. "2026-09-04T19:00Z"),
        # matching the convention used for match_date/kickoff_time.
        # fromisoformat (not a fixed strptime pattern) because ESPN's exact
        # format -- with or without seconds -- isn't something confirmed
        # from a live response; fromisoformat handles the 'Z' suffix and
        # either variant on Python 3.11+ (both this script and the
        # GitHub Actions runner are on 3.12).
        kickoff_utc = None
        raw_date = ev.get("date")
        if raw_date:
            try:
                kickoff_utc = datetime.fromisoformat(raw_date)
                if kickoff_utc.tzinfo is None:
                    kickoff_utc = kickoff_utc.replace(tzinfo=timezone.utc)
            except ValueError:
                pass

        games.append({"event_id": event_id, "home_team": team_name(home), "away_team": team_name(away),
                       "kickoff_utc": kickoff_utc})
    return games


def sync_kickoff_times(engine, teams, matches, season):
    """Phase A. Backfills matches.kickoff_time for upcoming matches.

    IMPORTANT: today's and tomorrow's dates are ALWAYS re-checked, even if
    every match on them already has a kickoff_time -- fixtures get moved
    by broadcasters right up until close to matchday (confirmed: this bit
    us for real, a match synced days out at 15:30 UTC actually kicked off
    at 16:30 UTC, and the original "only fill NULLs" design never
    refreshed it since the column wasn't null anymore). Dates further out
    than tomorrow still only get fetched once, since a schedule change
    further out has more chances to be caught by a later run before it's
    ever actionable -- keeps this from re-fetching the full
    SYNC_DAYS_AHEAD window (and its ScraperAPI cost) every single run."""
    with engine.connect() as conn:
        stale_or_missing_dates = conn.execute(
            text("""
                select distinct match_date from matches
                where season = :season
                  and match_date between current_date and current_date + (:days || ' days')::interval
                  and (kickoff_time is null or match_date <= current_date + interval '1 day')
                order by match_date
            """),
            {"season": season, "days": SYNC_DAYS_AHEAD},
        ).fetchall()

    if not stale_or_missing_dates:
        print("Phase A: kickoff_time already synced for the upcoming window, nothing to do.", flush=True)
        return

    for (d,) in stale_or_missing_dates:
        yyyymmdd = d.strftime("%Y%m%d")
        try:
            sb = fetch_json(SCOREBOARD_URL, params={"dates": yyyymmdd})
        except Exception as e:
            print(f"  Phase A: could not fetch scoreboard for {d}: {e}", flush=True)
            continue

        games = parse_games_from_scoreboard(sb)
        synced = 0
        with engine.begin() as conn:
            for g in games:
                if g["kickoff_utc"] is None:
                    continue
                home_code = ESPN_NAME_TO_CODE.get(g["home_team"])
                away_code = ESPN_NAME_TO_CODE.get(g["away_team"])
                if home_code is None or away_code is None:
                    continue
                home_id, away_id = teams.get(home_code), teams.get(away_code)
                match_id = matches.get((home_id, away_id))
                if match_id is None:
                    continue
                result = conn.execute(
                    text("""
                        update matches set kickoff_time = :kt
                        where id = :mid and (kickoff_time is null or kickoff_time != :kt)
                    """),
                    {"kt": g["kickoff_utc"].time(), "mid": match_id},
                )
                synced += result.rowcount
        print(f"  Phase A: {d} -- {len(games)} game(s) on ESPN's scoreboard, {synced} kickoff_time(s) set/corrected", flush=True)


def get_or_create_player(conn, name, team_id):
    # CRITICAL: lowercase, not just strip -- see scrape_fbref_matches.py
    # for the full explanation. ESPN's athlete.fullName is Title Case;
    # the historical migration used lowercase throughout.
    name = name.strip().lower()

    row = conn.execute(
        text("""
            select p.id from players p
            where (
                p.espn_name = :name or p.fbref_name = :name
                or exists (select 1 from player_aliases pa where pa.player_id = p.id and pa.alias = :name)
              )
              and (
                exists (select 1 from player_match_appearances pma where pma.player_id = p.id and pma.team_id = :team_id)
                or exists (select 1 from player_ratings pr where pr.player_id = p.id and pr.team_id = :team_id)
              )
            limit 1
        """),
        {"name": name, "team_id": team_id},
    ).fetchone()
    if row is not None:
        conn.execute(
            text("insert into player_aliases (player_id, alias, source) values (:pid, :alias, 'espn_poller') on conflict do nothing"),
            {"pid": row[0], "alias": name},
        )
        return row[0]

    candidates = conn.execute(
        text("""
            select distinct p.id from players p
            where p.espn_name = :name or p.fbref_name = :name
               or exists (select 1 from player_aliases pa where pa.player_id = p.id and pa.alias = :name)
        """),
        {"name": name},
    ).fetchall()
    if len(candidates) == 1:
        return candidates[0][0]
    if len(candidates) > 1:
        print(f"  WARNING: name collision for '{name}' with no team match among {len(candidates)} "
              f"existing players -- creating a new row rather than guessing. Needs manual review.", flush=True)

    result = conn.execute(
        text("insert into players (fbref_name, espn_name) values (:name, :name) returning id"),
        {"name": name},
    ).fetchone()
    return result[0]


def find_event_id(match):
    """The scoreboard call gives us event_id by date; the match day's scoreboard
    fetch is cheap (one request) and lets us map our match_id -> ESPN's
    event_id for the summary call that actually has roster data."""
    match_day = match["kickoff_utc"].date()
    sb = fetch_json(SCOREBOARD_URL, params={"dates": match_day.strftime("%Y%m%d")})
    for ev in sb.get("events", []) or []:
        comps = ev.get("competitions") or []
        if not comps:
            continue
        competitors = comps[0].get("competitors") or []
        home = next((c for c in competitors if c.get("homeAway") == "home"), None)
        away = next((c for c in competitors if c.get("homeAway") == "away"), None)
        home_code = ESPN_NAME_TO_CODE.get((home.get("team") or {}).get("displayName") if home else None)
        away_code = ESPN_NAME_TO_CODE.get((away.get("team") or {}).get("displayName") if away else None)
        if home_code == match["home_code"] and away_code == match["away_code"]:
            return ev.get("id")
    return None


def poll_match_for_lineup(engine, match_id, event_id, home_id, away_id, home_name, away_name):
    """One attempt. Returns True if BOTH sides' lineups were found and
    written (done, stop polling this match), False if at least one side
    still has nothing posted (keep trying)."""
    try:
        data = fetch_json(SUMMARY_URL_TMPL.format(event_id=event_id))
    except Exception as e:
        print(f"  could not fetch summary for {home_name} vs {away_name}: {e}", flush=True)
        return False

    rosters = data.get("rosters", [])
    total_written = 0
    sides_found = 0
    with engine.begin() as conn:
        for team_roster in rosters:
            home_away = team_roster.get("homeAway")
            team_id = home_id if home_away == "home" else away_id
            is_home = home_away == "home"
            roster = team_roster.get("roster")
            formation = team_roster.get("formation")

            if not roster:
                print(f"  {home_name} vs {away_name}: no lineup posted yet for {home_away} side", flush=True)
                continue
            sides_found += 1

            if formation and not DRY_RUN:
                conn.execute(
                    text("""
                        insert into match_team_stats (match_id, team_id, is_home, formation)
                        values (:match_id, :team_id, :is_home, :formation)
                        on conflict (match_id, team_id) do update set formation = excluded.formation
                    """),
                    {"match_id": match_id, "team_id": team_id, "is_home": is_home, "formation": formation},
                )

            starter_count = 0
            for p in roster:
                name = (p.get("athlete") or {}).get("fullName")
                if not name:
                    continue
                predicted_status = 2 if p.get("starter") else 1
                if predicted_status == 2:
                    starter_count += 1

                if DRY_RUN:
                    continue
                player_id = get_or_create_player(conn, name, team_id)
                conn.execute(
                    text("""
                        insert into predicted_lineups (match_id, player_id, team_id, predicted_status, scraped_at)
                        values (:match_id, :player_id, :team_id, :status, now())
                        on conflict (match_id, player_id) do update
                            set predicted_status = excluded.predicted_status, scraped_at = excluded.scraped_at
                    """),
                    {"match_id": match_id, "player_id": player_id, "team_id": team_id, "status": predicted_status},
                )
                total_written += 1

            if starter_count != 11:
                print(f"  WARNING: {home_name if is_home else away_name} "
                      f"({home_away}) shows {starter_count} starters, not 11 -- "
                      f"likely a parsing/API issue, not a missing-player issue.", flush=True)

    label = "[DRY RUN] would write" if DRY_RUN else "wrote"
    print(f"  {home_name} vs {away_name}: {label} {total_written} predicted lineup rows", flush=True)
    return sides_found == 2


def get_match(engine, match_id):
    with engine.connect() as conn:
        row = conn.execute(
            text("""
                select m.id, ht.code, at.code, ht.id, at.id, m.match_date, m.kickoff_time, m.status
                from matches m
                join teams ht on ht.id = m.home_team_id
                join teams at on at.id = m.away_team_id
                where m.id = :mid
            """),
            {"mid": match_id},
        ).fetchone()
    if row is None:
        return None
    mid, home_code, away_code, home_id, away_id, match_date, kickoff_time, status = row
    kickoff_utc = datetime.combine(match_date, kickoff_time, tzinfo=timezone.utc) if kickoff_time else None
    return {"match_id": mid, "home_code": home_code, "away_code": away_code, "home_id": home_id,
            "away_id": away_id, "kickoff_utc": kickoff_utc, "status": status}


def lineup_already_captured(engine, match_id, kickoff_utc):
    """True if BOTH teams already have 11+ predicted starters written at or after (kickoff - CAPTURED_SINCE_MIN).
    This is what lets the T-25 safety run exit at once when the T-50 run did its job."""
    since = kickoff_utc - timedelta(minutes=CAPTURED_SINCE_MIN)
    with engine.connect() as conn:
        rows = conn.execute(
            text("""
                select team_id, count(*) from predicted_lineups
                where match_id = :mid and predicted_status = 2 and scraped_at >= :since
                group by team_id
            """),
            {"mid": match_id, "since": since},
        ).fetchall()
    return len(rows) == 2 and all(n >= 11 for _, n in rows)


if __name__ == "__main__":
    match_id = os.environ.get("MATCH_ID", "").strip()
    if not match_id:
        raise SystemExit("MATCH_ID is not set -- this script handles one match per run.")

    m = get_match(engine, match_id)
    if m is None:
        raise SystemExit(f"No match found with id {match_id}.")
    if m["kickoff_utc"] is None:
        raise SystemExit(f"Match {match_id} has no kickoff_time stored -- nothing to time against.")
    if m["status"] == "completed":
        print("Match already completed -- nothing to do.", flush=True)
        raise SystemExit(0)

    home_name, away_name = m["home_code"], m["away_code"]
    print(f"{home_name} vs {away_name} -- kickoff {m['kickoff_utc'].isoformat()} -- DRY_RUN={DRY_RUN}", flush=True)

    if lineup_already_captured(engine, m["match_id"], m["kickoff_utc"]):
        print("Both lineups already captured for this match -- exiting.", flush=True)
        raise SystemExit(0)

    event_id = find_event_id(m)
    if event_id is None:
        raise SystemExit(f"Could not find {home_name} vs {away_name} on ESPN's scoreboard for "
                         f"{m['kickoff_utc'].date()}.")

    deadline = m["kickoff_utc"] + timedelta(minutes=LATE_CUTOFF_MIN)
    captured = False
    while True:
        captured = poll_match_for_lineup(engine, m["match_id"], event_id, m["home_id"], m["away_id"],
                                         home_name, away_name)
        if captured:
            break
        remaining = (deadline - datetime.now(timezone.utc)).total_seconds()
        if remaining <= 0:
            print(f"  reached cutoff ({LATE_CUTOFF_MIN} min after kickoff) without a full lineup -- giving up.",
                  flush=True)
            break
        time.sleep(min(POLL_INTERVAL_SEC, remaining))

    print("\nDone.", flush=True)
    raise SystemExit(0 if captured else 1)
