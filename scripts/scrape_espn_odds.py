"""
EPL model: live ESPN odds scraper.

Adapted from the user's existing, proven ESPN odds script -- same core
API calls and logic, redirected to write into the odds table instead of
Excel. Replaces the manual moneyline paste into the old `data` tab.

Unlike the FBref scraper, this hits ESPN's public JSON API directly
(no HTML scraping, no bot-detection dance needed based on the reference
script -- no proxy was required there).

Writes one row per game per run into `odds` (source='espn_draftkings'),
NOT an upsert -- the odds table intentionally keeps every capture over
time so you can see line movement, matching how it was designed earlier
in this project. Re-running this on a schedule (e.g. daily, or several
times as kickoff approaches) is expected and fine.

CAUTION: team name mapping (ESPN's names -> our team codes) now comes
directly from the user's own epl_team_codes.xlsx -- no longer a guess.
Still worth watching for SKIP messages on first run in case ESPN's
scoreboard API uses a slightly different name than that reference file
in some edge case, but this should be solid.

SETUP:
    pip install requests sqlalchemy psycopg2-binary --break-system-packages

Set DATABASE_URL the same way as the other migration scripts.
"""

import os
import time
from datetime import date, datetime, timedelta, timezone

import requests
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
from sqlalchemy import create_engine, text

DATABASE_URL = os.environ["DATABASE_URL"]
engine = create_engine(DATABASE_URL, pool_pre_ping=True, pool_recycle=280)

SEASON = 2627  # matches this database's season-numbering convention

# Rolling window: today +/- this many days. For a live/ongoing run this
# naturally re-captures odds for upcoming fixtures as kickoff approaches
# (line movement) and catches anything just finished. For a first TEST,
# set DAYS_BACK/DAYS_FORWARD to bracket a date you know has real fixtures
# (e.g. opening weekend) rather than relying on "today" alone.
# Overridable via environment variable so the SAME script can run in two
# modes from two different GitHub Actions schedules: a frequent, narrow
# scan (today only, for closing-line capture) and a much less frequent,
# wide scan (the full week ahead, for discovering new matches and their
# opening snapshot). Running the wide window frequently was pure waste --
# matches 5-7 days out can never be in the closing window yet anyway, so
# scanning them every 15 minutes bought nothing.
DAYS_BACK = int(os.environ.get("ODDS_DAYS_BACK", "0"))
DAYS_FORWARD = int(os.environ.get("ODDS_DAYS_FORWARD", "0"))

# How close to kickoff counts as "closing line" territory -- a second
# capture only happens once a match is within this many minutes of
# kickoff, capturing the most informative snapshot for a betting model.
CLOSING_WINDOW_MINUTES = 45

SCOREBOARD_URL = "https://site.api.espn.com/apis/site/v2/sports/soccer/eng.1/scoreboard"
ODDS_URL_TMPL = "https://sports.core.api.espn.com/v2/sports/soccer/leagues/eng.1/events/{event_id}/competitions/{comp_id}/odds"

REQUEST_SLEEP_SECONDS = 0.5  # ESPN's public API is more permissive than FBref, but still be polite

S = requests.Session()
S.headers.update({"User-Agent": "Mozilla/5.0", "Accept": "application/json"})

# ESPN's API blocks GitHub Actions' runner IPs (confirmed via real 403s
# in production, even though the exact same requests work fine from a
# regular machine) -- route through the same ScraperAPI proxy already
# used for FBref, but only when that credential is actually available
# (i.e. on GitHub Actions). Local runs continue unproxied, matching
# already-confirmed working behavior.
_proxy_url = os.environ.get("SCRAPERAPI_PROXY_URL")
PROXIES = {"http": _proxy_url, "https": _proxy_url} if _proxy_url else None

# Authoritative ESPN displayName -> team code, from the user's own
# epl_team_codes.xlsx (not guessed).
ESPN_NAME_TO_CODE = {
    "Arsenal": "ars", "Aston Villa": "avl", "AFC Bournemouth": "bou", "Brentford": "bre",
    "Brighton & Hove Albion": "bri", "Burnley": "bur", "Chelsea": "che",
    "Crystal Palace": "cry", "Everton": "eve", "Fulham": "ful", "Leeds United": "lee",
    "Liverpool": "liv", "Manchester City": "mci", "Manchester United": "man",
    "Newcastle United": "new", "Nottingham Forest": "ntf", "Sunderland": "sun",
    "Tottenham Hotspur": "tot", "West Ham United": "whm", "Wolverhampton Wanderers": "wol",
    # Newly promoted for 2026-27 -- not in the original epl_team_codes.xlsx
    # (which predates this season's promotions/relegations).
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


def normalize_team_name(s):
    if not s:
        return ""
    s = s.strip().lower()
    for ch in [".", ",", "'", '"']:
        s = s.replace(ch, "")
    return " ".join(s.split())


NORMALIZED_NAME_TO_CODE = {normalize_team_name(k): v for k, v in ESPN_NAME_TO_CODE.items()}


def parse_games_from_scoreboard(sb):
    games = []
    for ev in sb.get("events", []) or []:
        event_id = ev.get("id")
        comps = ev.get("competitions") or []
        if not event_id or not comps:
            continue
        comp = comps[0]
        comp_id = comp.get("id")
        game_time = ev.get("date")
        competitors = comp.get("competitors") or []
        home = next((c for c in competitors if c.get("homeAway") == "home"), None)
        away = next((c for c in competitors if c.get("homeAway") == "away"), None)

        def team_name(c):
            if not c:
                return None
            return (c.get("team") or {}).get("displayName")

        games.append({
            "event_id": event_id, "competition_id": comp_id, "game_time": game_time,
            "home_team": team_name(home), "away_team": team_name(away),
        })
    return games


def extract_draftkings_moneylines(odds_json):
    items = odds_json.get("items") or []
    for it in items:
        provider_name = ((it.get("provider") or {}).get("name") or "").strip().lower().replace(" ", "")
        if provider_name == "draftkings":
            home_ml = (it.get("homeTeamOdds") or {}).get("moneyLine")
            away_ml = (it.get("awayTeamOdds") or {}).get("moneyLine")
            draw_ml = (it.get("drawOdds") or {}).get("moneyLine")
            return home_ml, away_ml, draw_ml
    return None, None, None


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
    """Cheap, FREE check (no ScraperAPI credits, just a database query) --
    the fixture list is already fully synced in `matches`, so we can know
    whether today is a gameday before spending any ESPN/proxy requests at
    all. Used to skip the frequent job entirely on non-gamedays."""
    with engine.connect() as conn:
        row = conn.execute(
            text("select 1 from matches where season = :season and match_date = current_date limit 1"),
            {"season": season},
        ).fetchone()
    return row is not None


def minutes_until_kickoff(game_time_iso):
    """game_time_iso is ESPN's ISO timestamp (e.g. '2026-08-21T19:00Z').
    Returns minutes until kickoff (negative if already started/passed)."""
    try:
        kickoff = datetime.strptime(game_time_iso, "%Y-%m-%dT%H:%MZ").replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None
    return (kickoff - datetime.now(timezone.utc)).total_seconds() / 60


def process_game(conn, teams, matches, game):
    home_code = NORMALIZED_NAME_TO_CODE.get(normalize_team_name(game["home_team"]))
    away_code = NORMALIZED_NAME_TO_CODE.get(normalize_team_name(game["away_team"]))
    if home_code is None or away_code is None:
        print(f"  SKIP: unrecognized team name(s): {game['home_team']} / {game['away_team']}", flush=True)
        return

    home_id, away_id = teams.get(home_code), teams.get(away_code)
    match_id = matches.get((home_id, away_id))
    if match_id is None:
        print(f"  SKIP: no matching database row for {game['home_team']} vs {game['away_team']}", flush=True)
        return

    # Time-gating: GitHub Actions cron can't dynamically schedule "N
    # minutes before THIS match's kickoff" (schedules are static, kickoff
    # times vary week to week) -- so this runs frequently instead, and
    # decides per-match whether to actually capture right now. Always
    # capture the FIRST snapshot we ever see for a match (an early
    # reference point), then only capture again once within
    # CLOSING_WINDOW_MINUTES of kickoff (the closing line -- the most
    # informative single snapshot for a betting model). This avoids
    # writing a near-duplicate row every single run for a match that's
    # still hours away.
    mins_to_kickoff = minutes_until_kickoff(game.get("game_time"))
    already_captured = conn.execute(
        text("select 1 from odds where match_id = :match_id and source = 'espn_draftkings' limit 1"),
        {"match_id": match_id},
    ).fetchone() is not None

    is_closing_window = mins_to_kickoff is not None and 0 <= mins_to_kickoff <= CLOSING_WINDOW_MINUTES
    if already_captured and not is_closing_window:
        print(f"  SKIP (not yet in closing window): {game['home_team']} vs {game['away_team']} "
              f"({mins_to_kickoff:.0f} min to kickoff)" if mins_to_kickoff is not None else
              f"  SKIP (already captured, no kickoff time available): {game['home_team']} vs {game['away_team']}", flush=True)
        return

    try:
        odds_json = fetch_json(ODDS_URL_TMPL.format(event_id=game["event_id"], comp_id=game["competition_id"]))
        home_ml, away_ml, draw_ml = extract_draftkings_moneylines(odds_json)
    except Exception as e:
        print(f"  no odds available for {game['home_team']} vs {game['away_team']}: {e}", flush=True)
        home_ml, away_ml, draw_ml = None, None, None

    if home_ml is None and away_ml is None and draw_ml is None:
        print(f"  {game['home_team']} vs {game['away_team']}: no DraftKings odds posted yet", flush=True)
        return

    conn.execute(
        text("""
            insert into odds (match_id, source, market, home_odds, away_odds, draw_odds, captured_at)
            values (:match_id, 'espn_draftkings', 'moneyline', :home_odds, :away_odds, :draw_odds, now())
        """),
        {"match_id": match_id, "home_odds": home_ml, "away_odds": away_ml, "draw_odds": draw_ml},
    )
    tag = "closing" if is_closing_window else "opening"
    print(f"  wrote {tag} odds: {game['home_team']} ({home_ml}) vs {game['away_team']} ({away_ml}), draw ({draw_ml})", flush=True)


if __name__ == "__main__":
    teams = teams_map(engine)
    matches = match_id_map(engine, SEASON)

    # For the narrow/frequent job specifically (DAYS_FORWARD=0, i.e. today
    # only): skip entirely, before spending any ScraperAPI credits, if
    # today isn't even a gameday. The daily-wide job (DAYS_FORWARD>0)
    # always runs regardless, since its job is discovering upcoming
    # fixtures over the next week, not reacting to today specifically.
    if DAYS_FORWARD == 0 and DAYS_BACK == 0 and not has_games_today(engine, SEASON):
        print("No fixtures today -- skipping entirely, no requests made.", flush=True)
        raise SystemExit(0)

    end_date = date.today() + timedelta(days=DAYS_FORWARD)
    start_date = date.today() - timedelta(days=DAYS_BACK)

    total_games = 0
    for d in daterange(start_date, end_date):
        yyyymmdd = d.strftime("%Y%m%d")
        try:
            sb = fetch_json(SCOREBOARD_URL, params={"dates": yyyymmdd})
        except Exception as e:
            print(f"  could not fetch scoreboard for {d}: {e}", flush=True)
            continue

        games = parse_games_from_scoreboard(sb)
        if games:
            print(f"{d}: {len(games)} game(s) found", flush=True)
        for g in games:
            total_games += 1
            with engine.begin() as conn:
                try:
                    process_game(conn, teams, matches, g)
                except Exception as e:
                    print(f"  ERROR on {g['home_team']} vs {g['away_team']}: {e}", flush=True)
            time.sleep(REQUEST_SLEEP_SECONDS)

    print(f"Done. {total_games} games checked across {start_date} to {end_date}.")
