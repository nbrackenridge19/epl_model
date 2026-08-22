"""
EPL model: add ratings for players missing this season's data.

Finds players who've actually appeared (in a real match, or a posted
predicted lineup) for their CURRENT 2026-27 team, but have no
player_ratings row for (player, team, season) -- meaning they're
silently excluded from starters_rating/bench_rating averages (SQL's
avg() skips nulls without any visible sign anything's wrong). Covers
both genuinely new players (transfers, signings, academy graduates --
no rating history ever existed) and known players who transferred clubs
(prior history exists, just not for their new team this season).

Prompts for rating_l, rating_a, age, and position -- NOT rating_p.
rating_p is the age/position aging-curve projection, computed by the
existing separate process from rating_l -- typing a number in for it
here would bypass that formula entirely, which is exactly the bug this
version fixes (the old version prompted for rating_p directly).

For anyone with a known PRIOR rating (any team, any season), suggests
their most recent rating_a, age, and position as starting points --
press Enter to carry any of them forward as-is, or type a new value to
override. Genuinely new players get no suggestion, since there's
nothing to carry forward.

Run periodically -- there's no automated trigger for this (same as
enter_xg.py, this is inherently a judgment call only you can make), but
generate_dashboard.py surfaces a warning when there's something waiting
here.

SETUP:
    pip install sqlalchemy psycopg2-binary --break-system-packages

Set DATABASE_URL the same way as the other scripts.
"""

import os
from datetime import date

from sqlalchemy import create_engine, text

DATABASE_URL = os.environ["DATABASE_URL"]
engine = create_engine(DATABASE_URL, pool_pre_ping=True, pool_recycle=280)

SEASON = 2627
DEFAULT_RATING_NO_CARD = 55  # project convention: genuinely no rating history anywhere


def get_missing_ratings(engine):
    with engine.connect() as conn:
        rows = conn.execute(
            text("""
                select distinct p.id as player_id, p.fbref_name, t.id as team_id, t.code as team_code
                from (
                    select player_id, team_id from player_match_appearances pma
                    join matches m on m.id = pma.match_id where m.season = :season
                    union
                    select player_id, team_id from predicted_lineups pl
                    join matches m on m.id = pl.match_id where m.season = :season
                ) seen
                join players p on p.id = seen.player_id
                join teams t on t.id = seen.team_id
                where not exists (
                    select 1 from player_ratings pr
                    where pr.player_id = seen.player_id and pr.team_id = seen.team_id and pr.season = :season
                )
                order by t.code, p.fbref_name
            """),
            {"season": SEASON},
        ).fetchall()
    return rows


def get_most_recent_rating(engine, player_id):
    """Pulls the player's own most recent REAL observed rating (rating_a),
    not rating_p -- rating_a is their actual last-known performance level,
    which is exactly what rating_l is defined as for the new season. Also
    returns age/position/team as suggestion material, same idea."""
    with engine.connect() as conn:
        row = conn.execute(
            text("""
                select rating_a, season, team_id, age, position
                from player_ratings
                where player_id = :player_id and rating_a is not null
                order by season desc, effective_date desc
                limit 1
            """),
            {"player_id": player_id},
        ).fetchone()
    return row


def search_players(conn, query):
    rows = conn.execute(
        text("select id, fbref_name from players where fbref_name ilike :q order by fbref_name limit 8"),
        {"q": f"%{query}%"},
    ).fetchall()
    return rows


def merge_into(conn, from_player_id, into_player_id, alias_text):
    """Same merge shape used throughout this project: reassign real
    dependent rows, record the alias so this can't recur, delete the
    now-empty duplicate. Handles the case where the flagged player
    already has predicted_lineups/appearances (created by a live
    scraper under the mismatched name) -- those move to the real,
    established identity instead of being lost."""
    conn.execute(text("update predicted_lineups set player_id=:into where player_id=:frm"),
                 {"into": into_player_id, "frm": from_player_id})
    conn.execute(text("update player_match_appearances set player_id=:into where player_id=:frm"),
                 {"into": into_player_id, "frm": from_player_id})
    conn.execute(text("delete from player_ratings where player_id=:frm"), {"frm": from_player_id})
    conn.execute(
        text("insert into player_aliases (player_id, alias, source) values (:pid, :alias, 'manual') on conflict do nothing"),
        {"pid": into_player_id, "alias": alias_text},
    )
    conn.execute(text("delete from players where id=:frm"), {"frm": from_player_id})


def prompt_value(label, suggested, cast=float):
    suffix = f" [{suggested}]" if suggested is not None else " (no prior value on file)"
    raw = input(f"    {label}{suffix}: ").strip()
    if not raw:
        return suggested
    try:
        return cast(raw)
    except ValueError:
        print("    Not a valid value, keeping suggestion.")
        return suggested


def insert_rating(conn, player_id, team_id, rating_l, rating_a, age, position):
    conn.execute(
        text("""
            insert into player_ratings
                (player_id, team_id, season, rating_l, rating_a, age, position, effective_date, is_new_to_team)
            values
                (:player_id, :team_id, :season, :rating_l, :rating_a, :age, :position, :effective_date, true)
            on conflict (player_id, team_id, season) do update
                set rating_l = excluded.rating_l, rating_a = excluded.rating_a,
                    age = excluded.age, position = excluded.position
        """),
        {"player_id": player_id, "team_id": team_id, "season": SEASON,
         "rating_l": rating_l, "rating_a": rating_a, "age": age, "position": position,
         "effective_date": date.today()},
    )


if __name__ == "__main__":
    missing = get_missing_ratings(engine)
    print(f"Found {len(missing)} player(s) who've appeared this season with no {SEASON} rating on file.\n")

    if not missing:
        print("Nothing to do.")
        raise SystemExit(0)

    with engine.begin() as conn:
        for player_id, name, team_id, team_code in missing:
            prior = get_most_recent_rating(engine, player_id)
            if prior:
                prior_rating_a, prior_season, prior_team_id, prior_age, prior_position = prior
                note = " (new club)" if prior_team_id != team_id else ""
                print(f"{name} ({team_code.upper()}) -- last rated {prior_rating_a:.1f} in season {prior_season}{note}")
                suggested_l = prior_rating_a  # rating_l = last known rating_a, per project convention
            else:
                print(f"{name} ({team_code.upper()}) -- no prior rating found, likely genuinely new to the model")
                search = input("    Is this actually a known player under a different name? "
                                "(type a name to search, blank if genuinely new): ").strip()
                if search:
                    candidates = [c for c in search_players(conn, search) if c[0] != player_id]
                    if not candidates:
                        print("    No matches found -- treating as genuinely new.\n")
                        suggested_l = DEFAULT_RATING_NO_CARD
                        prior_age = None
                        prior_position = None
                    else:
                        print("    Matches:")
                        for i, (cid, cname) in enumerate(candidates, 1):
                            print(f"      {i}. {cname}")
                        choice = input(f"    Pick a number to merge into (blank to skip merging): ").strip()
                        if choice.isdigit() and 1 <= int(choice) <= len(candidates):
                            into_id, into_name = candidates[int(choice) - 1]
                            merge_into(conn, player_id, into_id, name)
                            print(f"    Merged '{name}' into existing player '{into_name}' -- alias recorded.")
                            player_id = into_id  # continue processing under the real identity
                            prior = get_most_recent_rating(engine, player_id)
                            if prior:
                                prior_rating_a, prior_season, prior_team_id, prior_age, prior_position = prior
                                suggested_l = prior_rating_a
                            else:
                                suggested_l = DEFAULT_RATING_NO_CARD
                                prior_age = None
                                prior_position = None
                        else:
                            print("    Skipped merging -- treating as genuinely new.\n")
                            suggested_l = DEFAULT_RATING_NO_CARD
                            prior_age = None
                            prior_position = None
                else:
                    suggested_l = DEFAULT_RATING_NO_CARD
                    prior_age = None
                    prior_position = None

            rating_l = prompt_value("rating_l", suggested_l, cast=float)
            if rating_l is None:
                print("    skipped.\n")
                continue
            rating_a = prompt_value("rating_a", rating_l, cast=float)
            age = prompt_value("age", prior_age, cast=int)
            position = prompt_value("position (g/d/m/f)", prior_position, cast=str)

            insert_rating(conn, player_id, team_id, rating_l, rating_a, age, position)
            print("    saved.\n")

    print("Done.")
