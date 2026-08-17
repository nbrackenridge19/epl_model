"""
EPL model: manual xG entry.

FBref no longer serves xG easily (the Opta data removal, Jan 2026 --
see conversation). Until an automated replacement source exists, this
is the manual entry point: finds completed matches still missing xG for
either team, and prompts for quick entry one match at a time.

Run periodically (e.g. right after the FBref scraper's morning run) to
keep xgfpgD/xgapgD -- two of the model's actual features -- current.
Leave either field blank and press Enter to skip a match for now; it'll
show up again next run.

SETUP:
    pip install sqlalchemy psycopg2-binary --break-system-packages

Set DATABASE_URL the same way as the other scripts.
"""

import os

from sqlalchemy import create_engine, text

DATABASE_URL = os.environ["DATABASE_URL"]
engine = create_engine(DATABASE_URL, pool_pre_ping=True, pool_recycle=280)


def get_missing_xg(engine):
    with engine.connect() as conn:
        rows = conn.execute(
            text("""
                select m.id, m.match_date, m.home_team_id, m.away_team_id,
                       ht.code as home_code, at.code as away_code,
                       m.home_goals, m.away_goals
                from matches m
                join teams ht on ht.id = m.home_team_id
                join teams at on at.id = m.away_team_id
                left join match_team_stats mts_h on mts_h.match_id = m.id and mts_h.team_id = m.home_team_id
                left join match_team_stats mts_a on mts_a.match_id = m.id and mts_a.team_id = m.away_team_id
                where m.status = 'completed'
                  and (mts_h.xg is null or mts_a.xg is null or mts_h.id is null or mts_a.id is null)
                order by m.match_date
            """)
        ).fetchall()
    return rows


def prompt_float(label):
    raw = input(f"    {label} (blank to skip): ").strip()
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        print("    Not a number, skipping this value.")
        return None


def update_xg(conn, match_id, team_id, is_home, xg, xga):
    conn.execute(
        text("""
            insert into match_team_stats (match_id, team_id, is_home, xg, xga)
            values (:match_id, :team_id, :is_home, :xg, :xga)
            on conflict (match_id, team_id) do update
                set xg = coalesce(excluded.xg, match_team_stats.xg),
                    xga = coalesce(excluded.xga, match_team_stats.xga)
        """),
        {"match_id": match_id, "team_id": team_id, "is_home": is_home, "xg": xg, "xga": xga},
    )


if __name__ == "__main__":
    missing = get_missing_xg(engine)
    print(f"Found {len(missing)} completed matches still missing xG.\n")

    if not missing:
        print("Nothing to do.")
        raise SystemExit(0)

    with engine.begin() as conn:
        for row in missing:
            match_id, match_date, home_id, away_id, home_code, away_code, home_goals, away_goals = row
            print(f"{match_date}: {home_code.upper()} {home_goals}-{away_goals} {away_code.upper()}")

            home_xg = prompt_float(f"{home_code.upper()} xG")
            away_xg = prompt_float(f"{away_code.upper()} xG")

            if home_xg is not None or away_xg is not None:
                # xga for each team is simply the opponent's xg
                update_xg(conn, match_id, home_id, True, home_xg, away_xg)
                update_xg(conn, match_id, away_id, False, away_xg, home_xg)
                print("    saved.\n")
            else:
                print("    skipped.\n")

    print("Done.")
