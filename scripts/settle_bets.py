"""
EPL model: settle completed bets.

Finds every bets row (including stake=0 "pass" decisions, kept for a
complete record) where the match has since completed but the outcome
hasn't been recorded yet. Computes the real win/loss and profit using
the actual moneyline stored at bet time, and updates the running
bankroll chain.

Not scheduled yet -- run manually for now (e.g. the morning after a
matchday, same cadence as the FBref scraper, which is what marks
matches 'completed' in the first place). Safe to re-run: only touches
bets where outcome is still null, so it naturally picks up wherever it
left off.

SETUP:
    pip install sqlalchemy psycopg2-binary --break-system-packages

Set DATABASE_URL the same way as the other scripts.
"""

import os

from sqlalchemy import create_engine, text

DATABASE_URL = os.environ["DATABASE_URL"]
engine = create_engine(DATABASE_URL, pool_pre_ping=True, pool_recycle=280)

SEASON = 2627
STARTING_BANKROLL = 2176.93  # 2025-26's real ending bankroll -- same anchor as generate_dashboard.py


def moneyline_profit(stake, ml, won):
    if not won:
        return -stake
    return stake * (ml / 100) if ml > 0 else stake * (100 / -ml)


def get_unsettled_bets(engine):
    with engine.connect() as conn:
        rows = conn.execute(
            text("""
                select b.id, b.match_id, b.team_id, b.stake, b.odds_used,
                       m.match_date, m.home_team_id, m.home_goals, m.away_goals
                from bets b
                join matches m on m.id = b.match_id
                where b.outcome is null and m.status = 'completed' and m.season = :season
                order by m.match_date
            """),
            {"season": SEASON},
        ).fetchall()
    return rows


def get_running_bankroll(engine, season):
    with engine.connect() as conn:
        row = conn.execute(
            text("""
                select coalesce(sum(b.profit), 0)
                from bets b join matches m on m.id = b.match_id
                where b.outcome is not null and m.season = :season
            """),
            {"season": season},
        ).fetchone()
    return STARTING_BANKROLL + float(row[0])


if __name__ == "__main__":
    bets = get_unsettled_bets(engine)
    print(f"Found {len(bets)} unsettled bets with completed matches.")

    running_bankroll = get_running_bankroll(engine, SEASON)
    print(f"Starting from bankroll: ${running_bankroll:,.2f}")

    with engine.begin() as conn:
        for bet in bets:
            bet_id, match_id, team_id, stake, odds_used, match_date, home_team_id, home_goals, away_goals = bet

            if home_goals is None or away_goals is None:
                print(f"  SKIP bet {bet_id}: match marked completed but missing scores.")
                continue

            won = (team_id == home_team_id and home_goals > away_goals) or \
                  (team_id != home_team_id and away_goals > home_goals)
            stake = stake or 0

            if stake == 0:
                profit = 0.0
            elif odds_used is None:
                print(f"  SKIP bet {bet_id}: stake > 0 but no odds_used recorded, cannot compute profit.")
                continue
            else:
                profit = moneyline_profit(stake, odds_used, won)

            running_bankroll += profit
            conn.execute(
                text("""
                    update bets set outcome = :outcome, profit = :profit, bankroll_after = :bankroll_after
                    where id = :id
                """),
                {"outcome": "win" if won else "loss", "profit": profit, "bankroll_after": running_bankroll, "id": bet_id},
            )
            print(f"  settled bet {bet_id} ({match_date}): {'WON' if won else 'lost'}, "
                  f"stake=${stake:.2f}, profit=${profit:.2f}, bankroll=${running_bankroll:,.2f}")

    print("Done.")
