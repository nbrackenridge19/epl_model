"""
EPL model: live FBref match-report scraper.

Pulls completed-match data directly from FBref match reports (not the
season-cumulative pages) -- this is the piece that replaces the weekly
hard-paste workflow going forward. Since there's no moving season-total
target to match against, nothing here needs the freeze-and-paste pattern
the old spreadsheet relied on.

Uses the proxy technique from the user's existing, proven FBref scraper
(ScraperAPI) -- plain requests got a flat 403 from FBref's bot protection
even with full browser-realistic headers and a cookie warm-up, confirming
this needs a real bypass, not just header tuning.

WHAT THIS SCRAPES (confirmed still available post the Jan 2026 Opta data
removal -- see conversation): starting XI, bench, minutes played, goals,
cards, final score. xG/xGA are NOT scraped (no longer on FBref at all) --
those stay a manual entry into match_team_stats, same as the plan already
was before this build.

WHAT THIS DOES NOT DO YET: indgd (needs goal-timing data, deliberately
deferred to a follow-up pass) and red_card_minute. Both left null for
now; not blocking on them to get this core piece live.

IMPORTANT: the page-parsing logic (which tables/divs to read) has NOT
been verified against a live page from this environment -- built on
general knowledge of FBref's structure, written defensively (columns
looked up by header name, not position) specifically because of that.
Treat the first real run as a calibration pass, not a finished script.

SETUP:
    pip install requests beautifulsoup4 lxml sqlalchemy psycopg2-binary --break-system-packages

Set DATABASE_URL the same way as the other migration scripts.
"""

import os
import re
import time
import random
from datetime import datetime

import requests
import urllib3
from bs4 import BeautifulSoup, Comment
from sqlalchemy import create_engine, text

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

DATABASE_URL = os.environ["DATABASE_URL"]
engine = create_engine(DATABASE_URL, pool_pre_ping=True, pool_recycle=280)

SEASON = 2627  # 2026-27, matches this database's season-numbering convention
FBREF_SCHEDULE_URL = "https://fbref.com/en/comps/9/schedule/Premier-League-Scores-and-Fixtures"

SLEEP_BETWEEN = (3, 6)
MAX_RETRIES = 3
USE_PROXY = True
# Read from environment, not hardcoded -- this key was previously sitting
# directly in this file, which is fine on your own machine but a real
# exposure risk once this gets committed to a GitHub repo for Actions to
# use. Set it the same way as DATABASE_URL:
#   $env:SCRAPERAPI_PROXY_URL='http://scraperapi:yourkey@proxy-server.scraperapi.com:8001'
#
# ScraperAPI began classifying fbref.com as a "protected domain" as of
# Aug 2026 -- confirmed via a real 500 response from ScraperAPI itself
# (not a database or scheduling problem): "Protected domains may require
# adding premium=true OR ultra_premium=true parameter to your request."
# _add_premium_param() below adds this automatically, per ScraperAPI's
# documented proxy-port syntax (the parameter goes in the username,
# separated by a dot: scraperapi.premium=true) -- no change to the
# underlying secret needed. If "premium" alone still 500s, set
# SCRAPERAPI_PREMIUM_LEVEL=ultra_premium as an additional env var (no
# code change required) -- ultra_premium costs more credits (30x vs 10x
# per request) so premium is the default, cheaper first try.
SCRAPERAPI_PREMIUM_LEVEL = os.environ.get("SCRAPERAPI_PREMIUM_LEVEL", "premium")


def _add_premium_param(proxy_url, level):
    from urllib.parse import urlsplit, urlunsplit
    parts = urlsplit(proxy_url)
    username = parts.username or "scraperapi"
    password = parts.password or ""
    if f"{level}=true" in username:
        return proxy_url  # already set -- don't double-append
    new_username = f"{username}.{level}=true"
    netloc = f"{new_username}:{password}@{parts.hostname}"
    if parts.port:
        netloc += f":{parts.port}"
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


PROXY_URL = _add_premium_param(os.environ["SCRAPERAPI_PROXY_URL"], SCRAPERAPI_PREMIUM_LEVEL)

DRY_RUN = False
MATCH_LIMIT = None  # None = process every completed match found, no cap

TEAMS = [
    {"name": "Arsenal", "code": "ars", "id": "18bb7c10"},
    {"name": "Aston Villa", "code": "avl", "id": "8602292d"},
    {"name": "Bournemouth", "code": "bou", "id": "4ba7cbea"},
    {"name": "Brentford", "code": "bre", "id": "cd051869"},
    {"name": "Brighton", "code": "bri", "id": "d07537b9"},
    {"name": "Burnley", "code": "bur", "id": "943e8050"},
    {"name": "Chelsea", "code": "che", "id": "cff3d9bb"},
    {"name": "Crystal Palace", "code": "cry", "id": "47c64c55"},
    {"name": "Everton", "code": "eve", "id": "d3fd31cc"},
    {"name": "Fulham", "code": "ful", "id": "fd962109"},
    {"name": "Leeds United", "code": "lee", "id": "5bfb9659"},
    {"name": "Liverpool", "code": "liv", "id": "822bd0ba"},
    {"name": "Manchester City", "code": "mci", "id": "b8fd03ef"},
    {"name": "Manchester United", "code": "man", "id": "19538871"},
    {"name": "Newcastle United", "code": "new", "id": "b2b47a98"},
    {"name": "Nottingham Forest", "code": "ntf", "id": "e4a775cb"},
    {"name": "Sunderland", "code": "sun", "id": "8ef52968"},
    {"name": "Tottenham", "code": "tot", "id": "361ca564"},
    {"name": "West Ham", "code": "whm", "id": "7c21e445"},
    {"name": "Wolves", "code": "wol", "id": "8cec06e1"},
]
TEAM_NAME_TO_CODE = {t["name"]: t["code"] for t in TEAMS}
TEAM_NAME_TO_CODE.update({
    "Nott'ham Forest": "ntf", "Manchester Utd": "man", "Newcastle Utd": "new", "Newcastle": "new",
    "Nottingham": "ntf", "Nottingham Forest": "ntf",
    # Newly promoted for 2026-27
    "Coventry City": "cov", "Hull City": "hul", "Ipswich Town": "ips",
})


def get_html(url, render=False, use_premium=True):
    # use_premium=False sends the request through the base proxy with NO
    # premium/ultra_premium param at all. Confirmed via ScraperAPI's own
    # support test (Aug 2026) that a plain standard-tier request against
    # the fbref schedule URL returns a clean 200 -- consistent with the
    # domain-report data showing no-flag requests succeeding ~85% of the
    # time on fbref.com, actually higher than ultra_premium alone (~65%).
    # ultra_premium's sudden 0% success rate the same day remains
    # unexplained (ticket open with ScraperAPI), but dropping it here
    # both sidesteps the current block and is far cheaper (1 credit vs
    # 30) regardless of how that ticket resolves.
    proxy_url = PROXY_URL if use_premium else os.environ["SCRAPERAPI_PROXY_URL"]
    if render:
        # NOTE (confirmed via ScraperAPI's domain-report data, Aug 2026):
        # render=true against fbref.com has a 0% success rate across every
        # combination tried (0 successes / 21 attempts total), while plain
        # ultra_premium alone succeeds ~65% of the time and no-flag
        # requests succeed ~85%. The original "schedule page needs JS
        # rendering" theory (see git history) was an unverified guess and
        # turned out to be wrong -- render actively triggers fbref's bot
        # block rather than bypassing it. Nothing in this codebase should
        # pass render=True for fbref.com; this branch is kept generic for
        # other domains only.
        proxy_url = _add_premium_param(proxy_url, "render")
    proxies = {"http": proxy_url, "https": proxy_url} if USE_PROXY else None
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        )
    }
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            r = requests.get(url, headers=headers, proxies=proxies, timeout=60, verify=False)
            if r.status_code != 200:
                print(f"   DIAGNOSTIC -- status {r.status_code}, body (first 300 chars): {r.text[:300]}", flush=True)
            r.raise_for_status()
            time.sleep(random.uniform(*SLEEP_BETWEEN))
            return r.text
        except Exception as e:
            print(f"   request error: {e}, attempt {attempt}", flush=True)
            time.sleep(random.uniform(*SLEEP_BETWEEN))
    return None


def find_table_in_comments(soup, table_id_substring):
    for comment in soup.find_all(string=lambda t: isinstance(t, Comment)):
        if table_id_substring in comment:
            inner = BeautifulSoup(comment, "lxml")
            table = inner.find("table")
            if table is not None:
                return table
    return soup.find("table", id=lambda x: x and table_id_substring in x)


def teams_map(engine):
    with engine.connect() as conn:
        rows = conn.execute(text("select id, code from teams")).fetchall()
    return {code: tid for tid, code in rows}


def match_id_map(engine, season):
    with engine.connect() as conn:
        rows = conn.execute(
            text("select id, source_lgame, home_team_id, away_team_id, status from matches where season = :season"),
            {"season": season},
        ).fetchall()
    return {lg: (mid, h, a, st) for mid, lg, h, a, st in rows}


def get_or_create_player(conn, name, team_id):
    # CRITICAL: lowercase, not just strip. FBref match reports return
    # Title Case names ("Hugo Ekitike"), but the entire historical
    # migration used lowercase ("hugo ekitike") -- without this, every
    # live scrape silently created a duplicate player row instead of
    # matching the real, existing one (confirmed: this bug affected
    # hundreds of players -- see conversation for the cleanup).
    name = name.strip().lower()

    # Three name sources now: fbref_name, espn_name, and player_aliases
    # (nicknames/shortened forms ESPN sometimes switches to mid-season,
    # e.g. "Savio" for a player whose real name is "Savinho"/"Sávio", or
    # "Toti" vs "Toti Gomes"). fbref_name is no longer required to be
    # globally unique (constraint dropped), so team context remains the
    # disambiguator: prefer an existing player with this name (any of
    # the three sources) who has real evidence of being at THIS specific
    # team.
    row = conn.execute(
        text("""
            select p.id from players p
            where (
                p.fbref_name = :name
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
        # Confirmed via team context, but not matching fbref_name exactly
        # -- this is a newly-observed alias. Record it now so next time
        # (and every downstream feature relying on continuous appearance
        # history, e.g. minutes-share) resolves instantly without relying
        # on team-context matching succeeding again.
        conn.execute(
            text("insert into player_aliases (player_id, alias, source) values (:pid, :alias, 'fbref_scraper') on conflict do nothing"),
            {"pid": row[0], "alias": name},
        )
        return row[0]

    # No team-matched candidate. Fall back to a plain name match, but
    # only when it's unambiguous (exactly one existing player with this
    # name, across all three sources) -- the normal case of a player's
    # first-ever appearance at a new club. A genuine name collision with
    # no team evidence on either side is exactly what this redesign
    # exists to handle safely: rather than guess and risk silently
    # merging two different real people, create a new row and flag it
    # loudly for manual review (same process used to merge the Karl Hein
    # / Kosta Tsimikas duplicates earlier this project, just run in the
    # other direction).
    candidates = conn.execute(
        text("""
            select distinct p.id from players p
            where p.fbref_name = :name
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
        text("insert into players (fbref_name) values (:name) returning id"),
        {"name": name},
    ).fetchone()
    return result[0]


def fetch_schedule_table():
    """Single fetch+parse of the schedule page. Previously, sync_fixtures
    and discover_match_reports each independently fetched this exact
    same URL -- same page, parsed twice, for two different subsets of
    the same rows. Confirmed via real ScraperAPI usage data this is
    genuinely wasteful, not just theoretically: two full ultra_premium
    requests per day for data that only needs fetching once."""
    html = get_html(FBREF_SCHEDULE_URL, use_premium=False)
    if html is None:
        raise RuntimeError("Could not fetch the schedule page after retries -- check proxy connectivity.")
    soup = BeautifulSoup(html, "lxml")
    table = find_table_in_comments(soup, "sched")
    if table is None:
        raise RuntimeError("Could not find the schedule table -- FBref's page structure may have changed.")
    return table


def discover_match_reports(table):
    results = []
    for row in table.find("tbody").find_all("tr"):
        if row.get("class") and "thead" in row.get("class"):
            continue
        report_cell = row.find("td", {"data-stat": "match_report"})
        if report_cell is None:
            continue
        link = report_cell.find("a")
        # CRITICAL: FBref populates this same cell with a Stathead
        # matchup-history link (a different, paywalled product) for
        # UNPLAYED fixtures too -- confirmed via real output (every
        # future 2026-27 fixture had a link here, all pointing at
        # /en/stathead/matchup/... and returning 403). Only a genuine
        # match report URL (starting /en/matches/) means the match was
        # actually played and has a real report.
        if link is None or not link.get("href", "").startswith("/en/matches/"):
            continue
        report_url = "https://fbref.com" + link["href"]

        date_cell = row.find("td", {"data-stat": "date"})
        home_cell = row.find("td", {"data-stat": "home_team"})
        away_cell = row.find("td", {"data-stat": "away_team"})
        if date_cell is None or home_cell is None or away_cell is None:
            continue

        results.append({
            "date": date_cell.get_text(strip=True),
            "home": home_cell.get_text(strip=True),
            "away": away_cell.get_text(strip=True),
            "report_url": report_url,
        })
    return results


def parse_player_stats_table(soup, team_index):
    tables = soup.find_all("table", id=lambda x: x and x.startswith("stats_") and x.endswith("_summary"))
    if len(tables) < 2:
        tables = [find_table_in_comments(soup, "summary")]
    if team_index >= len(tables) or tables[team_index] is None:
        return []

    table = tables[team_index]
    all_rows = table.find_all("tr")
    header_rows_with_stat_names = [r for r in all_rows if r.find("th", attrs={"data-stat": "player"}) and r.find("th").get("scope") == "col"]
    header_row = header_rows_with_stat_names[0] if header_rows_with_stat_names else all_rows[0]
    headers = [th.get("data-stat") for th in header_row.find_all("th")]

    rows = []
    for tr in all_rows:
        if tr is header_row or tr.find("th", {"scope": "col"}):
            continue  # skip header rows
        cells = tr.find_all(["th", "td"])
        row = {headers[i]: cells[i].get_text(strip=True) for i in range(min(len(headers), len(cells)))}
        if row.get("player") and row["player"] not in ("Player",) and "Players" not in row["player"]:
            rows.append(row)
    return rows


def parse_lineup_section(soup, team_index):
    """FBref's 'Lineups' box is a <table>, not a <ul> as originally
    assumed -- confirmed via real HTML. Each row is <td>number</td>
    <td><a>Player Name</a>...</td>; a <th colspan="2"> row marks either
    the team/formation header (e.g. "Liverpool (4-2-3-1)") or the
    literal text "Bench", which is the starter/bench divider. Rows
    before "Bench" = starters, after = bench -- confirmed this is
    correct regardless of substitute_in/out icons present on names in
    either section (those icons describe subsequent match events, not
    initial starter/bench status).
    Returns (starters, bench, formation) -- formation is None if the
    header text doesn't match the expected "(X-X-X)" pattern."""
    lineup_divs = soup.find_all("div", class_="lineup")
    if len(lineup_divs) < 2:
        return None, None, None
    div = lineup_divs[team_index]
    table = div.find("table")
    if table is None:
        return None, None, None

    starters, bench = [], []
    in_bench_section = False
    formation = None
    for tr in table.find_all("tr"):
        th = tr.find("th")
        if th is not None:
            th_text = th.get_text(strip=True)
            if "bench" in th_text.lower():
                in_bench_section = True
            elif formation is None:
                # first non-"Bench" header row is the team/formation line
                m = re.search(r"\(([\d\-]+)\)\s*$", th_text)
                if m:
                    formation = m.group(1)
            continue  # header row -- not a player
        link = tr.find("a")
        if link is None:
            continue
        name = link.get_text(strip=True)
        (bench if in_bench_section else starters).append(name)
    return starters, bench, formation


def discover_all_fixtures(table):
    """Like discover_match_reports, but captures EVERY fixture on the
    schedule page -- played or not. Needed to pre-populate the matches
    table for a new season, since future fixtures have no report link
    (and therefore no FBref match ID) to key off yet."""
    fixtures = []
    for row in table.find_all("tr"):
        if row.get("class") and "thead" in row.get("class"):
            continue
        wk_cell = row.find("td", {"data-stat": "gameweek"}) or row.find("th", {"data-stat": "gameweek"})
        date_cell = row.find("td", {"data-stat": "date"})
        home_cell = row.find("td", {"data-stat": "home_team"})
        away_cell = row.find("td", {"data-stat": "away_team"})
        if date_cell is None or home_cell is None or away_cell is None:
            continue
        date_text = date_cell.get_text(strip=True)
        home_text = home_cell.get_text(strip=True)
        away_text = away_cell.get_text(strip=True)
        if not date_text or not home_text or not away_text:
            continue
        fixtures.append({
            "matchweek": wk_cell.get_text(strip=True) if wk_cell else None,
            "date": date_text,
            "home": home_text,
            "away": away_text,
        })
    return fixtures


def sync_fixtures(conn, teams, season, table):
    """Ensures every fixture for the season has a matches row, even
    before it's played. Also keeps match_date/matchweek current for
    still-unplayed fixtures -- the Premier League only confirms exact
    dates/times on a rolling basis (early season fixtures are initially
    just placeholder dates, e.g. 'Saturday of the matchweek', and get
    officially fixed closer to the time). The WHERE clause on the update
    restricts this to status='scheduled' rows only, so an already-played
    match's real result can never get overwritten by a stale date."""
    fixtures = discover_all_fixtures(table)
    created = 0
    updated = 0
    for f in fixtures:
        home_code = TEAM_NAME_TO_CODE.get(f["home"])
        away_code = TEAM_NAME_TO_CODE.get(f["away"])
        if home_code is None or away_code is None:
            print(f"  SYNC SKIP: unrecognized team name(s): {f['home']} / {f['away']}", flush=True)
            continue
        home_id, away_id = teams.get(home_code), teams.get(away_code)
        if home_id is None or away_id is None:
            print(f"  SYNC SKIP: team code not in database: {home_code} / {away_code}", flush=True)
            continue
        try:
            match_date = datetime.strptime(f["date"], "%Y-%m-%d").date()
        except (ValueError, TypeError):
            continue  # blank/malformed date -- fixture not yet scheduled with a real date

        # Synthetic identifier since future fixtures have no FBref match ID
        # yet (no report link exists until after the match is played).
        synthetic_lgame = f"{season}_{home_code}_{away_code}"
        result = conn.execute(
            text("""
                insert into matches (season, matchweek, match_date, home_team_id, away_team_id, status, source_lgame)
                values (:season, :wk, :date, :home_id, :away_id, 'scheduled', :lgame)
                on conflict (season, home_team_id, away_team_id) do update
                    set match_date = excluded.match_date, matchweek = excluded.matchweek
                    where matches.status = 'scheduled' and matches.match_date is distinct from excluded.match_date
                returning id, (xmax = 0) as was_insert
            """),
            {"season": season, "wk": safe_int(f["matchweek"]), "date": match_date,
             "home_id": home_id, "away_id": away_id, "lgame": synthetic_lgame},
        ).fetchone()
        if result is not None:
            if result[1]:  # was_insert
                created += 1
            else:
                updated += 1
    print(f"  fixture sync: {created} new matches rows created, {updated} dates updated, "
          f"{len(fixtures)} total fixtures found", flush=True)


def safe_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def build_player_records(starters, bench, stats_rows):
    """Combines the lineup list (correct starter/bench status, including
    unused subs) with the stats table (actual minutes for anyone who
    played). starters/bench are name lists; stats_rows is a list of dicts
    keyed by data-stat names (confirmed via real output: 'player' and
    'minutes' are the relevant keys)."""
    stats_by_name = {row["player"]: row for row in stats_rows if row.get("player")}
    records = []
    for name in starters:
        stats = stats_by_name.get(name, {})
        minutes = safe_int(stats.get("minutes")) or 0
        records.append({"player": name, "status": 2, "minutes": minutes})
    for name in bench:
        stats = stats_by_name.get(name)
        minutes = safe_int(stats.get("minutes")) if stats else 0
        minutes = minutes or 0
        records.append({"player": name, "status": 1, "minutes": minutes})
    return records


def scrape_match_report(url):
    html = get_html(url)
    if html is None:
        raise RuntimeError(f"Could not fetch {url} after retries.")
    soup = BeautifulSoup(html, "lxml")

    scorebox = soup.find("div", class_="scorebox")
    if scorebox is None:
        raise RuntimeError(f"No scorebox found at {url} -- page structure may differ from expected.")
    scores = scorebox.find_all("div", class_="score")
    home_goals = safe_int(scores[0].get_text(strip=True)) if len(scores) > 0 else None
    away_goals = safe_int(scores[1].get_text(strip=True)) if len(scores) > 1 else None

    home_stats = parse_player_stats_table(soup, 0)
    away_stats = parse_player_stats_table(soup, 1)

    home_starters, home_bench, home_formation = parse_lineup_section(soup, 0)
    away_starters, away_bench, away_formation = parse_lineup_section(soup, 1)

    if home_starters is None or away_starters is None:
        # lineup section not found -- fall back to the old (imprecise)
        # behavior rather than crash, but this is a real gap worth flagging
        print("  WARNING: lineup section not found -- falling back to stats-table-only "
              "(will misclassify unused subs, or miss them entirely)", flush=True)
        home_records = [{"player": r["player"], "status": 2 if safe_int(r.get("minutes")) else 1,
                          "minutes": safe_int(r.get("minutes")) or 0} for r in home_stats]
        away_records = [{"player": r["player"], "status": 2 if safe_int(r.get("minutes")) else 1,
                          "minutes": safe_int(r.get("minutes")) or 0} for r in away_stats]
    else:
        home_records = build_player_records(home_starters, home_bench, home_stats)
        away_records = build_player_records(away_starters, away_bench, away_stats)

    return {
        "home_goals": home_goals,
        "away_goals": away_goals,
        "home_players": home_records,
        "away_players": away_records,
        "home_formation": home_formation,
        "away_formation": away_formation,
    }


def process_match(conn, teams, matches, report):
    home_code = TEAM_NAME_TO_CODE.get(report["home"])
    away_code = TEAM_NAME_TO_CODE.get(report["away"])
    if home_code is None or away_code is None:
        print(f"  SKIP: unrecognized team name(s): {report['home']} / {report['away']}", flush=True)
        return

    home_id, away_id = teams.get(home_code), teams.get(away_code)
    match_entry = None
    for lg, (mid, h, a, st) in matches.items():
        if h == home_id and a == away_id:
            match_entry = (mid, st)
            break
    if match_entry is None:
        print(f"  SKIP: no matching database row for {report['home']} vs {report['away']}", flush=True)
        return
    match_id, current_status = match_entry

    data = scrape_match_report(report["report_url"])

    print(f"  {'[DRY RUN] ' if DRY_RUN else ''}{report['home']} {data['home_goals']}-{data['away_goals']} {report['away']}", flush=True)
    print(f"    home player rows: {len(data['home_players'])} "
          f"(starters sample: {data['home_players'][:2]}, bench sample: {data['home_players'][-2:]})", flush=True)
    print(f"    away player rows: {len(data['away_players'])} "
          f"(starters sample: {data['away_players'][:2]}, bench sample: {data['away_players'][-2:]})", flush=True)

    if DRY_RUN:
        return

    conn.execute(
        text("""
            insert into match_team_stats (match_id, team_id, is_home, goals_for, goals_against, formation)
            values (:match_id, :team_id, true, :gf, :ga, :formation)
            on conflict (match_id, team_id) do update
                set goals_for = excluded.goals_for, goals_against = excluded.goals_against, formation = excluded.formation
        """),
        {"match_id": match_id, "team_id": home_id, "gf": data["home_goals"], "ga": data["away_goals"],
         "formation": data.get("home_formation")},
    )
    conn.execute(
        text("""
            insert into match_team_stats (match_id, team_id, is_home, goals_for, goals_against, formation)
            values (:match_id, :team_id, false, :gf, :ga, :formation)
            on conflict (match_id, team_id) do update
                set goals_for = excluded.goals_for, goals_against = excluded.goals_against, formation = excluded.formation
        """),
        {"match_id": match_id, "team_id": away_id, "gf": data["away_goals"], "ga": data["home_goals"],
         "formation": data.get("away_formation")},
    )

    conn.execute(text("update matches set home_goals = :hg, away_goals = :ag, status = 'completed' where id = :id"),
                 {"hg": data["home_goals"], "ag": data["away_goals"], "id": match_id})

    for team_id, players in [(home_id, data["home_players"]), (away_id, data["away_players"])]:
        for row in players:
            player_id = get_or_create_player(conn, row["player"], team_id)
            conn.execute(
                text("""
                    insert into player_match_appearances (match_id, player_id, team_id, status, minutes_played)
                    values (:match_id, :player_id, :team_id, :status, :minutes)
                    on conflict (match_id, player_id) do update set status = excluded.status, minutes_played = excluded.minutes_played
                """),
                {"match_id": match_id, "player_id": player_id, "team_id": team_id, "status": row["status"], "minutes": row["minutes"]},
            )

    print(f"  wrote {report['home']} {data['home_goals']}-{data['away_goals']} {report['away']} "
          f"({len(data['home_players'])}+{len(data['away_players'])} player rows)", flush=True)


if __name__ == "__main__":
    teams = teams_map(engine)

    print("Fetching FBref schedule page (used for both fixture sync and match-report discovery)...", flush=True)
    schedule_table = fetch_schedule_table()

    print("Syncing fixture list (creates matches rows for any fixture not yet in the database)...", flush=True)
    with engine.begin() as conn:
        sync_fixtures(conn, teams, SEASON, schedule_table)

    matches = match_id_map(engine, SEASON)

    print("Discovering match reports from FBref schedule page...", flush=True)
    reports = discover_match_reports(schedule_table)
    print(f"Found {len(reports)} completed matches with report links.", flush=True)

    if MATCH_LIMIT is not None:
        # Spread the sample across the whole season instead of just the
        # first N chronologically -- a same-week cluster would likely all
        # be uneventful matches (no red cards, no own goals, standard 5
        # subs used), missing exactly the edge cases worth catching before
        # trusting this on genuinely live matches.
        step = max(1, len(reports) // MATCH_LIMIT)
        reports = reports[::step][:MATCH_LIMIT]
        print(f"Sampling {len(reports)} matches spread across the season.", flush=True)

    for report in reports:
        with engine.begin() as conn:
            try:
                process_match(conn, teams, matches, report)
            except Exception as e:
                print(f"  ERROR on {report['home']} vs {report['away']}: {e}", flush=True)

    print("Done.")
