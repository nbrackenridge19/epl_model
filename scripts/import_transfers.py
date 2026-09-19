"""
EPL model: one-time import of summer 2026-27 transfer/promotion data.

Reads the finalized `import_data_consolidated.xlsx` (two tabs: `consolidated`
for the promoted teams' full rosters, `staying_teams_transfers` for the other
17 teams' summer arrivals) and writes to Supabase.

Three cases, per the classification worked out in conversation:
  1. Genuinely new to our database (promoted-team players, and staying-team
     signings with no prior PL history) -- insert a new `players` row, then
     a season-2627 `player_ratings` row with rating_l = the SOFIFA rating
     (or 55 if no card was found -- the project's "no real rating history"
     convention).
  2. Already in our database (in-league transfers) -- no new `players` row.
     Just a new season-2627 `player_ratings` row, with rating_l pulled from
     their own most recent `rating_a` (their last real observed rating,
     wherever their previous team's data left off).
  3. rating_p is intentionally left null here -- that's the age/position
     aging-curve projection, computed by the existing separate process, not
     something this script re-derives.

is_new_to_team is always true for every row this script writes -- that's
the entire premise of "promoted team" and "transfer in".

Safe to re-run: every insert is guarded by a check for an existing
season-2627 player_ratings row for that player, so a second run is a no-op
rather than a duplicate.

SETUP:
    pip install openpyxl sqlalchemy psycopg2-binary --break-system-packages

Set DATABASE_URL the same way as the other scripts.
"""

import os
import re
import unicodedata
from collections import defaultdict
from datetime import date

import openpyxl
from sqlalchemy import create_engine, text

DATABASE_URL = os.environ["DATABASE_URL"]
engine = create_engine(DATABASE_URL, pool_pre_ping=True, pool_recycle=280)

SEASON = 2627
DEFAULT_RATING_NO_CARD = 55
INPUT_PATH = "import_data_consolidated.xlsx"
TODAY = date.today().isoformat()

# Real name mismatches that accent-stripping alone won't fix -- confirmed by hand.
# Maps the name as it appears in the spreadsheet -> the real fbref_name in the DB.
MANUAL_NAME_OVERRIDES = {
    "tommy watson": "tom watson",  # same player: Brighton, ex-Sunderland academy, confirmed via research
}


def norm_name(name):
    """Match the project-wide convention: .strip().lower() on every player name,
    to avoid the case-mismatch duplicate-player bug fixed earlier this project."""
    return name.strip().lower()


def strip_accents(s):
    return "".join(c for c in unicodedata.normalize("NFD", s) if unicodedata.category(c) != "Mn")


def accent_free(name):
    return strip_accents(norm_name(name))


def load_promoted_team_rows(wb):
    """consolidated tab -- promoted teams' full rosters, all genuinely new to DB."""
    ws = wb["consolidated"]
    rows = []
    for r in ws.iter_rows(min_row=2, values_only=True):
        name, position, birth_date, team, ea_fc_rating, notes = r[:6]
        if name is None or name == "Legend / notes:" or str(name).startswith("-"):
            continue
        rows.append({
            "name": name, "position": position, "birth_date": birth_date,
            "team": team, "rating": ea_fc_rating, "case": "new",
        })
    return rows


def load_staying_team_rows(wb):
    """staying_teams_transfers tab -- mix of new-to-DB and already-in-DB rows."""
    ws = wb["staying_teams_transfers"]
    rows = []
    for r in ws.iter_rows(min_row=2, values_only=True):
        name, position, team, age, birth_date, rating, status, notes = r[:8]
        if name is None or status is None or name == "Legend / notes:":
            continue
        case = "new" if status.startswith("NEW TO DB") else "existing"
        rows.append({
            "name": name, "position": position, "team": team,
            "age": age, "birth_date": birth_date, "rating": rating, "case": case,
        })
    return rows


def get_team_ids(conn):
    return {row.code: row.id for row in conn.execute(text("select id, code from teams"))}


def load_player_index(conn):
    """Preload every player, indexed by accent-stripped name -- fbref_name in the
    DB can carry diacritics (e.g. 'kosta nedeljković') that Transfermarkt's plain-ASCII
    arrivals table won't, so a live exact-match query silently misses real matches.

    Each name maps to a LIST of candidates, each carrying the set of team_ids
    that player has real evidence of playing for (via player_ratings or
    player_match_appearances). Team evidence is what disambiguates two
    different real people who happen to share a first name -- exactly the
    same approach scrape_fbref_matches.py and poll_espn_lineups.py already
    use in their own get_or_create_player(). This script used to skip that
    step and match on name alone, which is a real, confirmed bug: on
    2026-09-19 an incoming 22yo Man City signing named "Allan Elias" got
    silently merged into an unrelated, already-in-the-DB 31yo "Allan"
    (ex-Everton) purely because both normalize to "allan" -- overwriting the
    real Allan Elias's age and rating with the wrong player's data. Team
    context prevents that regardless of which name happens to collide next."""
    index = defaultdict(list)
    rows = conn.execute(text("""
        select p.id, p.fbref_name,
               coalesce(
                   (select array_agg(distinct pr.team_id) from player_ratings pr where pr.player_id = p.id),
                   '{}'
               ) || coalesce(
                   (select array_agg(distinct pma.team_id) from player_match_appearances pma where pma.player_id = p.id),
                   '{}'
               ) as team_ids
        from players p
    """))
    for row in rows:
        index[accent_free(row.fbref_name)].append({"id": row.id, "team_ids": set(row.team_ids)})
    return index


def get_existing_player(player_index, name, team_id):
    """Prefer a candidate with real evidence at team_id. If no candidate has
    team evidence, fall back to an unambiguous name match (the normal case:
    a player's first-ever appearance in our data). If multiple candidates
    collide on name with no team evidence to break the tie, refuse to guess
    -- return None (which, in both call sites below, means "treat as not
    found") and print a loud warning so it gets a human look rather than a
    silent wrong merge."""
    name_norm = norm_name(name)
    lookup_name = MANUAL_NAME_OVERRIDES.get(name_norm, name_norm)
    candidates = player_index.get(accent_free(lookup_name), [])
    if not candidates:
        return None
    team_matches = [c for c in candidates if team_id in c["team_ids"]]
    if team_matches:
        return team_matches[0]["id"]
    if len(candidates) == 1:
        return candidates[0]["id"]
    print(f"  WARNING: name collision for '{name}' with no team match among {len(candidates)} "
          f"existing players -- treating as not found rather than guessing. Needs manual review.")
    return None


def get_latest_rating_a(conn, player_id):
    row = conn.execute(
        text("""select rating_a from player_ratings
                 where player_id = :pid and rating_a is not null
                 order by effective_date desc limit 1"""),
        {"pid": player_id},
    ).fetchone()
    return float(row.rating_a) if row else None


def already_imported(conn, player_id):
    row = conn.execute(
        text("select 1 from player_ratings where player_id = :pid and season = :season"),
        {"pid": player_id, "season": SEASON},
    ).fetchone()
    return row is not None


def compute_age(birth_date):
    if birth_date is None:
        return None
    if isinstance(birth_date, str):
        from datetime import datetime
        birth_date = datetime.strptime(birth_date, "%Y-%m-%d").date()
    today = date.today()
    return today.year - birth_date.year - ((today.month, today.day) < (birth_date.month, birth_date.day))


def process_row(conn, row, team_ids, player_index, stats):
    name_norm = norm_name(row["name"])
    team_id = team_ids.get(row["team"])
    if team_id is None:
        print(f"  SKIP (unknown team code {row['team']!r}): {row['name']}")
        stats["skipped"] += 1
        return

    if row["case"] == "new":
        player_id = get_existing_player(player_index, row["name"], team_id)
        if player_id is None:
            result = conn.execute(
                text("""insert into players (fbref_name, birth_date)
                         values (:name, :birth_date) returning id"""),
                {"name": name_norm, "birth_date": row.get("birth_date")},
            )
            player_id = result.fetchone().id
            player_index[accent_free(name_norm)].append({"id": player_id, "team_ids": {team_id}})
            stats["players_created"] += 1
        if already_imported(conn, player_id):
            stats["already_done"] += 1
            return
        rating_l = row["rating"] if row["rating"] is not None else DEFAULT_RATING_NO_CARD
        age = row.get("age") or compute_age(row.get("birth_date"))
        conn.execute(
            text("""insert into player_ratings
                     (player_id, season, rating_l, effective_date, is_new_to_team, team_id, position, age)
                     values (:pid, :season, :rating_l, :eff, true, :team_id, :position, :age)"""),
            {"pid": player_id, "season": SEASON, "rating_l": rating_l, "eff": TODAY,
             "team_id": team_id, "position": row["position"], "age": age},
        )
        stats["ratings_created_new"] += 1

    else:  # existing in-league transfer
        player_id = get_existing_player(player_index, row["name"], team_id)
        if player_id is None:
            print(f"  SKIP (marked existing but no DB match): {row['name']}")
            stats["skipped"] += 1
            return
        if already_imported(conn, player_id):
            stats["already_done"] += 1
            return
        rating_l = get_latest_rating_a(conn, player_id)
        if rating_l is None:
            print(f"  WARNING: no prior rating_a found for {row['name']}, defaulting to {DEFAULT_RATING_NO_CARD}")
            rating_l = DEFAULT_RATING_NO_CARD
        age = row.get("age") or compute_age(row.get("birth_date"))
        conn.execute(
            text("""insert into player_ratings
                     (player_id, season, rating_l, effective_date, is_new_to_team, team_id, position, age)
                     values (:pid, :season, :rating_l, :eff, true, :team_id, :position, :age)"""),
            {"pid": player_id, "season": SEASON, "rating_l": rating_l, "eff": TODAY,
             "team_id": team_id, "position": row["position"], "age": age},
        )
        stats["ratings_created_transfer"] += 1


if __name__ == "__main__":
    wb = openpyxl.load_workbook(INPUT_PATH, data_only=True)
    rows = load_promoted_team_rows(wb) + load_staying_team_rows(wb)
    print(f"Loaded {len(rows)} total rows to process.")

    stats = {"players_created": 0, "ratings_created_new": 0, "ratings_created_transfer": 0,
              "already_done": 0, "skipped": 0}

    with engine.begin() as conn:
        team_ids = get_team_ids(conn)
        player_index = load_player_index(conn)
        for row in rows:
            process_row(conn, row, team_ids, player_index, stats)

    print("\nDone.")
    for k, v in stats.items():
        print(f"  {k}: {v}")
