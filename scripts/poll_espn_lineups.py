"""
EPL model: fixture-aware ESPN lineup poller.

REDESIGNED 2026-09-05 -- see below for why.

Runs HOURLY (not every 5 min). Two phases each run:

  Phase A -- sync kickoff times. Queries ESPN's scoreboard for any of the
  next SYNC_DAYS_AHEAD days that still have a match missing kickoff_time,
  and backfills it. This is what lets Phase B work BEFORE match day
  arrives -- previously kickoff_time was never populated at all (column
  existed, nothing wrote to it), so there was no way to know in advance
  when to expect a kickoff.

  Phase B -- if any of TODAY's matches has its kickoff inside
  [now - LATE_CUTOFF_MIN, now + EARLY_MARGIN_MIN] (i.e. we're in the
  actionable pre-kickoff-through-just-after-kickoff window), poll ESPN's
  summary endpoint for that match repeatedly INSIDE this one job run
  (POLL_INTERVAL_SEC apart) until either a lineup is found or the window
  closes. This is the key change: the fine-grained ~4-minute cadence
  needed near kickoff no longer depends on GitHub's scheduler firing
  reliably at that cadence -- it happens via time.sleep() inside a
  single job execution, well under GitHub's 6-hour job limit.

WHY THIS EXISTS: the previous design (schedule: "*/5 ...", relying on
GitHub firing a fresh job every 5 minutes) was empirically NOT
happening -- real run history showed multi-hour gaps between
consecutive "Scheduled" runs even after cutting the interval, e.g. a
4-hour gap between two consecutive runs at a nominal 5-minute cadence.
That's consistent with GitHub's scheduler dropping the large majority
of ticks for a high-frequency schedule, not just running a few minutes
late (GitHub's own docs only promise occasional delays during high
load, not near-total drops). The fix here is to ask GitHub's scheduler
for something much less frequent (hourly -- a much lower-volume ask of
the same mechanism, hypothesized to be more reliable, though this is a
bet rather than something proven for this specific account) and do the
actual precision timing in-process instead. If hourly checks turn out
to ALSO get dropped at a similar rate, that's evidence GitHub's
scheduler can't be trusted at any frequency for this account, and the
next step would be triggering externally (e.g. a Cloudflare Worker cron
hitting the workflow_dispatch API) instead of via `schedule:` at all --
not attempted here per an explicit preference to avoid new
infrastructure unless GitHub-only options are exhausted.

An hourly cadence checking an 80-minute-wide actionable window
mathematically guarantees at least one tick lands inside that window
for every match, AS LONG AS hourly ticks themselves don't get dropped --
window width (80 min) exceeds tick spacing (60 min), so by the
pigeonhole principle no kickoff can have its whole window fall between
two consecutive ticks.

kickoff_time is stored as a bare TIME (no timezone in the column type),
by convention interpreted as UTC to match match_date (which is also
effectively a UTC calendar date, consistent with how ESPN's own `date`
field -- always UTC, e.g. "2026-09-04T19:00Z" -- lines up with what's
already stored there). match_date + kickoff_time together reconstruct
the full UTC kickoff instant.

Deliberately does NOT use subbedIn/subbedOut -- confirmed those fields
are inconsistently shaped across competitions (plain booleans in some,
{'didSub': bool} objects in others), but this poller doesn't need them
at all: it only cares about the PRE-match predicted starter/bench split,
which the 'starter' boolean gives directly and consistently.

Each write UPSERTs (on conflict do update) rather than accumulates --
polling repeatedly as kickoff approaches naturally keeps the latest
prediction, overwriting an earlier guess if the lineup changes.

SETUP:
    pip install requests sqlalchemy psycopg2-binary --break-system-packages

Set DATABASE_URL the same way as the other migration scripts.
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

# Phase B: actionable window around kickoff, and in-job poll cadence.
LATE_CUTOFF_MIN = 10     # keep trying up to this long AFTER kickoff
EARLY_MARGIN_MIN = 70    # start being willing to try this long BEFORE kickoff
POLL_INTERVAL_SEC = 240  # ~4 min between in-job poll attempts

SCOREBOARD_URL = "https://site.api.espn.com/apis/site/v2/sports/soccer/eng.1/scoreboard"
SUMMARY_URL_TMPL = "https://site.api.espn.com/apis/site/v2/sports/soccer/eng.1/summary?event={event_id}"

# SAFETY: dry run first -- prints what it WOULD write without touching
# the database.
DRY_RUN = False

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
    """Phase A. Backfills matches.kickoff_time for any of the next
    SYNC_DAYS_AHEAD days that still have a match without one. Skips a
    day entirely once every match on it already has a kickoff_time --
    keeps this cheap on days that already synced fine."""
    with engine.connect() as conn:
        missing_dates = conn.execute(
            text("""
                select distinct match_date from matches
                where season = :season and kickoff_time is null
                  and match_date between current_date and current_date + (:days || ' days')::interval
                order by match_date
            """),
            {"season": season, "days": SYNC_DAYS_AHEAD},
        ).fetchall()

    if not missing_dates:
        print("Phase A: kickoff_time already synced for the upcoming window, nothing to do.", flush=True)
        return

    for (d,) in missing_dates:
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
                    text("update matches set kickoff_time = :kt where id = :mid and kickoff_time is null"),
                    {"kt": g["kickoff_utc"].time(), "mid": match_id},
                )
                synced += result.rowcount
        print(f"  Phase A: {d} -- {len(games)} game(s) on ESPN's scoreboard, {synced} kickoff_time(s) newly set", flush=True)


def get_actionable_matches(engine, season):
    """Phase B gate. Any of TODAY's matches whose kickoff (match_date +
    kickoff_time, both UTC by convention) falls inside
    [now - LATE_CUTOFF_MIN, now + EARLY_MARGIN_MIN]. Returns dicts with
    everything process_game needs -- no separate lookup required later."""
    with engine.connect() as conn:
        rows = conn.execute(
            text("""
                select m.id, ht.code, at.code,
                       (m.match_date + m.kickoff_time) at time zone 'UTC' as kickoff_utc
                from matches m
                join teams ht on ht.id = m.home_team_id
                join teams at on at.id = m.away_team_id
                where m.season = :season and m.match_date = current_date and m.kickoff_time is not null
            """),
            {"season": season},
        ).fetchall()

    now = datetime.now(timezone.utc)
    actionable = []
    for mid, home_code, away_code, kickoff_utc in rows:
        # psycopg2 returns a tz-aware datetime for a timestamptz column.
        # Convert (not blindly relabel) to UTC -- safe regardless of
        # whatever timezone the DB session itself reports in.
        if kickoff_utc.tzinfo is not None:
            kickoff_utc = kickoff_utc.astimezone(timezone.utc)
        else:
            kickoff_utc = kickoff_utc.replace(tzinfo=timezone.utc)
        delta_min = (kickoff_utc - now).total_seconds() / 60
        if -LATE_CUTOFF_MIN <= delta_min <= EARLY_MARGIN_MIN:
            actionable.append({"match_id": mid, "home_code": home_code, "away_code": away_code,
                                "kickoff_utc": kickoff_utc})
    return actionable


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
    """The scoreboard call gives us event_id by date; today's scoreboard
    fetch is cheap (one request) and lets us map our match_id -> ESPN's
    event_id for the summary call that actually has roster data."""
    today = datetime.now(timezone.utc).date()
    sb = fetch_json(SCOREBOARD_URL, params={"dates": today.strftime("%Y%m%d")})
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


if __name__ == "__main__":
    teams = teams_map(engine)
    matches = match_id_map(engine, SEASON)
    code_to_team_id = teams  # already keyed by code

    print("=== Phase A: sync kickoff times ===", flush=True)
    sync_kickoff_times(engine, teams, matches, SEASON)

    print("\n=== Phase B: check for actionable matches ===", flush=True)
    actionable = get_actionable_matches(engine, SEASON)
    if not actionable:
        print("No match within the actionable window right now -- exiting, no lineup requests made.", flush=True)
        raise SystemExit(0)

    for m in actionable:
        home_id = code_to_team_id.get(m["home_code"])
        away_id = code_to_team_id.get(m["away_code"])
        home_name = m["home_code"]
        away_name = m["away_code"]
        print(f"\nActionable: {home_name} vs {away_name} (kickoff {m['kickoff_utc'].isoformat()})", flush=True)

        event_id = find_event_id(m)
        if event_id is None:
            print(f"  could not find this match on ESPN's scoreboard for today -- skipping.", flush=True)
            continue

        deadline = m["kickoff_utc"] + timedelta(minutes=LATE_CUTOFF_MIN)
        while True:
            done = poll_match_for_lineup(engine, m["match_id"], event_id, home_id, away_id, home_name, away_name)
            if done:
                break
            now = datetime.now(timezone.utc)
            if now >= deadline:
                print(f"  reached cutoff ({LATE_CUTOFF_MIN} min after kickoff) without a full lineup -- giving up for this match.", flush=True)
                break
            time.sleep(POLL_INTERVAL_SEC)

    print("\nDone.", flush=True)
