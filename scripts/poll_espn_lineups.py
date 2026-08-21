"""
EPL model: live ESPN lineup poller.

Polls ESPN's summary API for each of today's (or a configured date's)
matches, and writes the predicted starting XI/bench to predicted_lineups.
Meant to run on a schedule close to kickoff (e.g. every 5 min via GitHub
Actions, starting ~90 min before kickoff to be safe) -- confirmed via
real testing that lineup data can appear same-day before kickoff for at
least some competitions, though the exact timing window wasn't precisely
pinned down (data was present at test time, kickoff time unclear exactly
how far out).

Deliberately does NOT use subbedIn/subbedOut -- confirmed those fields
are inconsistently shaped across competitions (plain booleans in some,
{'didSub': bool} objects in others), but this poller doesn't need them
at all: it only cares about the PRE-match predicted starter/bench split,
which the 'starter' boolean gives directly and consistently.

Each run UPSERTs (on conflict do update) rather than accumulates --
running this repeatedly as kickoff approaches naturally keeps the latest
prediction, overwriting an earlier guess if the lineup changes.

SETUP:
    pip install requests sqlalchemy psycopg2-binary --break-system-packages

Set DATABASE_URL the same way as the other migration scripts.
"""

import os
from datetime import date, timedelta

import requests
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
from sqlalchemy import create_engine, text

DATABASE_URL = os.environ["DATABASE_URL"]
engine = create_engine(DATABASE_URL, pool_pre_ping=True, pool_recycle=280)

SEASON = 2627

# Poll today +/- this many days -- narrow window since this is meant to
# run frequently close to kickoff, not scan far ahead.
# Today only -- confirmed via testing that lineups don't post a full day
# ahead of kickoff, so checking tomorrow on every run was pure waste
# (extra ScraperAPI credits for zero chance of finding anything).
DAYS_BACK = 0
DAYS_FORWARD = 0

SCOREBOARD_URL = "https://site.api.espn.com/apis/site/v2/sports/soccer/eng.1/scoreboard"
SUMMARY_URL_TMPL = "https://site.api.espn.com/apis/site/v2/sports/soccer/eng.1/summary?event={event_id}"

# SAFETY: dry run first -- prints what it WOULD write without touching
# the database.
DRY_RUN = True

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


def daterange(start, end):
    d = start
    while d <= end:
        yield d
        d += timedelta(days=1)


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


def has_games_today(engine, season):
    """Same free (no ScraperAPI credits) gameday check as the odds
    scraper -- skip entirely on non-gamedays before spending any
    requests at all."""
    with engine.connect() as conn:
        row = conn.execute(
            text("select 1 from matches where season = :season and match_date = current_date limit 1"),
            {"season": season},
        ).fetchone()
    return row is not None


def get_or_create_player(conn, name, team_id):
    # CRITICAL: lowercase, not just strip -- see scrape_fbref_matches.py
    # for the full explanation. ESPN's athlete.fullName is Title Case;
    # the historical migration used lowercase throughout.
    name = name.strip().lower()

    # Four name sources now: fbref_name, espn_name, and player_aliases
    # (nicknames/shortened forms ESPN itself sometimes switches to
    # mid-season for the SAME player, e.g. "Savio" one week vs the
    # stored espn_name "Savinho" another, or "Toti" vs "Toti Gomes" --
    # this is a real, separate failure mode from an fbref/espn spelling
    # mismatch: ESPN disagreeing with itself over time). fbref_name/
    # espn_name are no longer required to be globally unique (constraint
    # dropped), so team context remains the disambiguator: prefer an
    # existing player with this name (any of the three sources) who has
    # real evidence of being at THIS specific team.
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
        # Confirmed via team context but not matching espn_name/fbref_name
        # exactly -- a newly-observed alias. Record it now so it resolves
        # instantly next time without depending on team-context matching
        # succeeding again. This is exactly what protects minutes-share
        # continuity across an ESPN name switch: as long as the alias is
        # on file, every appearance -- before and after the switch --
        # keeps landing on the same player_id, so involvements_before
        # never resets.
        conn.execute(
            text("insert into player_aliases (player_id, alias, source) values (:pid, :alias, 'espn_poller') on conflict do nothing"),
            {"pid": row[0], "alias": name},
        )
        return row[0]

    # No team-matched candidate. Fall back to a plain name match, but
    # only when it's unambiguous (exactly one existing player with this
    # name across all sources) -- the normal case of a player's
    # first-ever appearance at a new club. A genuine name collision with
    # no team evidence on either side is exactly what this redesign
    # exists to handle safely: create a new row and flag it loudly for
    # manual review rather than guess and risk silently merging two
    # different real people (same process used to merge the Karl Hein /
    # Kosta Tsimikas duplicates earlier this project, just run in
    # reverse).
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

    # Genuinely new player -- store the ESPN-sourced name as BOTH
    # fbref_name and espn_name for now (best information available at
    # creation time). If FBref later reports a different real spelling
    # for this same person, that's a separate reconciliation to catch
    # via the same duplicate-detection approach used earlier this
    # project, not something this function can resolve on its own.
    result = conn.execute(
        text("insert into players (fbref_name, espn_name) values (:name, :name) returning id"),
        {"name": name},
    ).fetchone()
    return result[0]


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

        games.append({"event_id": event_id, "home_team": team_name(home), "away_team": team_name(away)})
    return games


def process_game(conn, teams, matches, game):
    home_code = ESPN_NAME_TO_CODE.get(game["home_team"])
    away_code = ESPN_NAME_TO_CODE.get(game["away_team"])
    if home_code is None or away_code is None:
        print(f"  SKIP: not an EPL match (or unrecognized name): {game['home_team']} / {game['away_team']}", flush=True)
        return

    home_id, away_id = teams.get(home_code), teams.get(away_code)
    match_id = matches.get((home_id, away_id))
    if match_id is None:
        print(f"  SKIP: no matching database row for {game['home_team']} vs {game['away_team']}", flush=True)
        return

    try:
        data = fetch_json(SUMMARY_URL_TMPL.format(event_id=game["event_id"]))
    except Exception as e:
        print(f"  could not fetch summary for {game['home_team']} vs {game['away_team']}: {e}", flush=True)
        return

    rosters = data.get("rosters", [])
    total_written = 0
    for team_roster in rosters:
        home_away = team_roster.get("homeAway")
        team_id = home_id if home_away == "home" else away_id
        is_home = home_away == "home"
        roster = team_roster.get("roster")
        formation = team_roster.get("formation")

        if not roster:
            print(f"  {game['home_team']} vs {game['away_team']}: no lineup posted yet for {home_away} side", flush=True)
            continue

        if formation and not DRY_RUN:
            # Expected formation from ESPN, pre-match. Written to the SAME
            # column FBref's scraper updates the next morning with the
            # real, confirmed formation -- the temporal handoff (this
            # only ever runs same-day, FBref only runs the day after)
            # means FBref's value can never get overwritten by this once
            # it lands; this is purely a same-day placeholder until then.
            conn.execute(
                text("""
                    insert into match_team_stats (match_id, team_id, is_home, formation)
                    values (:match_id, :team_id, :is_home, :formation)
                    on conflict (match_id, team_id) do update set formation = excluded.formation
                """),
                {"match_id": match_id, "team_id": team_id, "is_home": is_home, "formation": formation},
            )
        elif formation and DRY_RUN:
            print(f"  [DRY RUN] would write expected formation: {home_away} = {formation}", flush=True)

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

        # Headcount sanity check -- a genuinely different failure mode
        # from "missing player_ratings" (which is handled separately,
        # see add_player_ratings.py). get_or_create_player() means the
        # count should basically never drop below 11 just because a
        # player is new to our database -- if it does, that points to a
        # malformed/incomplete ESPN response for this specific player,
        # not a data-gap issue.
        if starter_count != 11:
            print(f"  WARNING: {game['home_team'] if is_home else game['away_team']} "
                  f"({home_away}) shows {starter_count} starters, not 11 -- "
                  f"likely a parsing/API issue, not a missing-player issue.", flush=True)

    label = "[DRY RUN] would write" if DRY_RUN else "wrote"
    print(f"  {game['home_team']} vs {game['away_team']}: {label} {total_written} predicted lineup rows", flush=True)


if __name__ == "__main__":
    teams = teams_map(engine)
    matches = match_id_map(engine, SEASON)

    if not has_games_today(engine, SEASON):
        print("No fixtures today -- skipping entirely, no requests made.", flush=True)
        raise SystemExit(0)

    end_date = date.today() + timedelta(days=DAYS_FORWARD)
    start_date = date.today() - timedelta(days=DAYS_BACK)

    for d in daterange(start_date, end_date):
        yyyymmdd = d.strftime("%Y%m%d")
        try:
            sb = fetch_json(SCOREBOARD_URL, params={"dates": yyyymmdd})
        except Exception as e:
            print(f"  could not fetch scoreboard for {d}: {e}", flush=True)
            continue

        games = parse_games_from_scoreboard(sb)
        print(f"{d}: {len(games)} game(s) found", flush=True)
        for g in games:
            with engine.begin() as conn:
                try:
                    process_game(conn, teams, matches, g)
                except Exception as e:
                    print(f"  ERROR on {g['home_team']} vs {g['away_team']}: {e}", flush=True)

    print("Done.")
