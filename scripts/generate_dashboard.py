"""
EPL model: Kelly betting decisions + visual dashboard.

Replicates the actual spreadsheet formula (verified against the real
epl2526.xlsx file, not assumed):
  - kf (raw Kelly fraction) = model_prob - (1-model_prob)/b, where b is
    the American moneyline converted to net decimal odds.
  - Only bet if kf >= 2 x model_versions.min_edge_threshold (the
    dynamically-computed edge threshold from fit_model.py -- an
    empirical proxy for "meaningfully above the typical edge").
  - Only bet if the team has played at least 5 matches this season
    (confirmed via the real data: the spreadsheet's '#' column is the
    MATCHWEEK number, not a row index -- '#'<=5 means "skip the first
    5 gameweeks," giving rolling features real time to stabilize).
  - stake = kf * current_bankroll * 0.15 (the spreadsheet's own
    conservative fraction -- 15% of full Kelly, not half or quarter).

Current bankroll is computed dynamically: 2025-26's actual ending
bankroll ($2,176.93, computed directly from the real historical data,
starting from $2,500) plus the sum of any already-settled 2026-27 bets'
profit. No hardcoded running total to maintain -- it just adds up
correctly every time this runs.

Writes a `bets` row for every match with a computed decision (stake=0
rows included, so "the model considered this and passed" is itself a
permanent record -- separate concern from recording actual outcomes,
which happens later once a match completes).

Generates a simple, mobile-readable HTML dashboard. Meant to be run
on the same schedule as the odds/lineup scrapers, regenerating whenever
fresh data might be available.

SETUP:
    pip install sqlalchemy psycopg2-binary --break-system-packages

Set DATABASE_URL the same way as the other scripts.
"""

import os
import math
from datetime import date

from sqlalchemy import create_engine, text

DATABASE_URL = os.environ["DATABASE_URL"]
engine = create_engine(DATABASE_URL, pool_pre_ping=True, pool_recycle=280)

SEASON = 2627
KELLY_FRACTION = 0.15
MIN_GAMES_PLAYED = 5
STARTING_BANKROLL = 2176.93  # 2025-26's real ending bankroll -- see conversation for how this was derived
OUTPUT_PATH = "docs/index.html"  # GitHub Pages serves from /docs by default


def get_latest_model(engine):
    with engine.connect() as conn:
        version = conn.execute(
            text("""select id, fit_date, min_edge_threshold, notes
                     from model_versions order by fit_date desc limit 1""")
        ).fetchone()
        if version is None:
            raise RuntimeError("No model_versions found -- run fit_model.py first.")
        coefs = conn.execute(
            text("select feature_name, coefficient from model_coefficients where model_version_id = :vid"),
            {"vid": version[0]},
        ).fetchall()
    return version, {name: coef for name, coef in coefs}


def get_current_bankroll(engine):
    with engine.connect() as conn:
        settled_profit = conn.execute(
            text("""
                select coalesce(sum(b.profit), 0)
                from bets b join matches m on m.id = b.match_id
                where m.season = :season and b.outcome is not null
            """),
            {"season": SEASON},
        ).fetchone()[0]
    return STARTING_BANKROLL + float(settled_profit)


def get_candidate_matches(engine):
    with engine.connect() as conn:
        rows = conn.execute(
            text("""
                select distinct m.id, m.match_date, ht.code as home_code, at.code as away_code,
                       ht.id as home_id, at.id as away_id
                from matches m
                join teams ht on ht.id = m.home_team_id
                join teams at on at.id = m.away_team_id
                where m.season = :season and m.status != 'completed'
                  and exists (select 1 from predicted_lineups pl where pl.match_id = m.id)
                order by m.match_date
            """),
            {"season": SEASON},
        ).fetchall()
    return rows


def get_match_features(engine, match_id, team_id):
    with engine.connect() as conn:
        row = conn.execute(
            text("""
                select avgatt, xgfpgd, xgapgd, gkpgd, possd, strtd, bertd, stmsd, formcd
                from v_match_model_features_predicted
                where match_id = :match_id and team_id = :team_id
            """),
            {"match_id": match_id, "team_id": team_id},
        ).fetchone()
    return row


def get_games_played(engine, team_id, before_date):
    with engine.connect() as conn:
        row = conn.execute(
            text("""
                select count(*) from matches
                where season = :season and status = 'completed' and match_date < :before_date
                  and (home_team_id = :team_id or away_team_id = :team_id)
            """),
            {"season": SEASON, "before_date": before_date, "team_id": team_id},
        ).fetchone()
    return row[0]


def get_latest_odds(engine, match_id):
    with engine.connect() as conn:
        row = conn.execute(
            text("""
                select home_odds, away_odds from odds
                where match_id = :match_id and source = 'espn_draftkings'
                order by captured_at desc limit 1
            """),
            {"match_id": match_id},
        ).fetchone()
    return row


def compute_probability(coefs, team_code, features):
    logit = coefs.get("Intercept", 0.0)
    names = ["avgatt", "xgfpgd", "xgapgd", "gkpgd", "possd", "strtd", "bertd", "stmsd", "formcd"]
    for name, value in zip(names, features):
        if value is None:
            return None
        logit += coefs.get(name, 0.0) * value
    logit += coefs.get(f"C(team_code)[T.{team_code}]", 0.0)
    return 1 / (1 + math.exp(-logit))


def moneyline_to_implied_prob(ml):
    if ml is None:
        return None
    return 100 / (ml + 100) if ml > 0 else -ml / (-ml + 100)


def moneyline_to_net_odds(ml):
    return ml / 100 if ml > 0 else -100 / ml


def evaluate_side(model_prob, ml, bankroll, edge_threshold, games_played):
    if model_prob is None:
        return {"status": "no_prediction"}
    if games_played < MIN_GAMES_PLAYED:
        return {"status": "too_early", "model_prob": model_prob, "games_played": games_played}
    if ml is None:
        return {"status": "no_odds", "model_prob": model_prob}

    implied_prob = moneyline_to_implied_prob(ml)
    b = moneyline_to_net_odds(ml)
    kf = model_prob - (1 - model_prob) / b

    if edge_threshold is None or kf < edge_threshold * 2:
        return {"status": "pass", "model_prob": model_prob, "implied_prob": implied_prob, "moneyline": ml, "kf": kf}

    stake = kf * bankroll * KELLY_FRACTION
    return {"status": "bet", "model_prob": model_prob, "implied_prob": implied_prob, "moneyline": ml, "kf": kf, "stake": stake}


def record_bet(conn, match_id, team_id, evaluation):
    stake = evaluation.get("stake", 0) or 0
    conn.execute(
        text("""
            insert into bets (match_id, team_id, predicted_prob, odds_used, stake, placed_at)
            values (:match_id, :team_id, :predicted_prob, :odds_used, :stake, now())
            on conflict (match_id, team_id) do update
                set predicted_prob = excluded.predicted_prob, odds_used = excluded.odds_used,
                    stake = excluded.stake, placed_at = excluded.placed_at
        """),
        # odds_used stores the actual moneyline (e.g. -150), NOT the implied
        # probability -- needed for exact payout math when settling later.
        # implied_prob is still used for the dashboard's own "Market %"
        # display, just not what gets persisted here.
        {"match_id": match_id, "team_id": team_id, "predicted_prob": evaluation.get("model_prob"),
         "odds_used": evaluation.get("moneyline"), "stake": stake},
    )


def get_settled_bets(engine):
    with engine.connect() as conn:
        rows = conn.execute(
            text("""
                select m.match_date, t.code as team_code, (m.home_team_id = b.team_id) as is_home,
                       b.predicted_prob, b.odds_used, b.stake, b.outcome, b.profit, b.bankroll_after
                from bets b
                join matches m on m.id = b.match_id
                join teams t on t.id = b.team_id
                where m.season = :season and b.outcome is not null
                order by m.match_date desc, b.stake desc
                limit 100
            """),
            {"season": SEASON},
        ).fetchall()
    return rows


def render_html(bankroll, model_version, results, settled_bets):
    rows_html = ""
    for r in results:
        badge = {"bet": "BET", "pass": "pass", "too_early": "too early",
                 "no_odds": "no odds yet", "no_prediction": "no lineup yet"}[r["status"]]
        badge_color = {"bet": "#1a7f37", "pass": "#666", "too_early": "#999",
                       "no_odds": "#999", "no_prediction": "#999"}[r["status"]]
        model_pct = f"{r['model_prob']:.1%}" if r.get("model_prob") is not None else "-"
        market_pct = f"{r['implied_prob']:.1%}" if r.get("implied_prob") is not None else "-"
        stake_str = f"${r['stake']:.2f}" if r["status"] == "bet" else "-"
        rows_html += (
            "<tr>"
            f"<td>{r['match_date']}</td>"
            f"<td>{r['team_code'].upper()} {'(H)' if r['is_home'] else '(A)'}</td>"
            f"<td>{model_pct}</td>"
            f"<td>{market_pct}</td>"
            f"<td>{stake_str}</td>"
            f"<td><span style=\"color:{badge_color}; font-weight:600;\">{badge}</span></td>"
            "</tr>"
        )

    history_rows_html = ""
    for h in settled_bets:
        match_date, team_code, is_home, predicted_prob, odds_used, stake, outcome, profit, bankroll_after = h
        if stake and stake > 0:
            result_str = "WON" if outcome == "win" else "lost"
            result_color = "#1a7f37" if outcome == "win" else "#c0392b"
            stake_str = f"${stake:.2f}"
            profit_str = f"{'+' if profit >= 0 else ''}${profit:.2f}"
        else:
            result_str, result_color, stake_str, profit_str = "passed", "#999", "-", "-"
        history_rows_html += (
            "<tr>"
            f"<td>{match_date}</td>"
            f"<td>{team_code.upper()} {'(H)' if is_home else '(A)'}</td>"
            f"<td>{stake_str}</td>"
            f"<td><span style=\"color:{result_color}; font-weight:600;\">{result_str}</span></td>"
            f"<td>{profit_str}</td>"
            "</tr>"
        )

    style = (
        "body { font-family: -apple-system, sans-serif; max-width: 700px; margin: 0 auto; "
        "padding: 16px; background: #fafafa; } "
        "h1 { font-size: 20px; } h2 { font-size: 16px; margin-top: 28px; } "
        ".meta { color: #666; font-size: 13px; margin-bottom: 16px; } "
        "table { width: 100%; border-collapse: collapse; background: white; border-radius: 8px; "
        "overflow: hidden; } "
        "th, td { padding: 10px 8px; text-align: left; font-size: 14px; border-bottom: 1px solid #eee; } "
        "th { background: #f0f0f0; font-size: 12px; text-transform: uppercase; } "
        "@media (max-width: 480px) { th, td { font-size: 12px; padding: 8px 4px; } }"
    )

    html = (
        "<!DOCTYPE html><html><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
        "<title>EPL Model Dashboard</title>"
        f"<style>{style}</style></head><body>"
        "<h1>EPL Model Dashboard</h1>"
        f"<div class=\"meta\">Bankroll: ${bankroll:,.2f} &middot; "
        f"Model version fit {model_version[1].strftime('%Y-%m-%d')} &middot; "
        f"Updated {date.today()}</div>"
        "<table><tr><th>Date</th><th>Team</th><th>Model %</th><th>Market %</th>"
        f"<th>Stake</th><th>Decision</th></tr>{rows_html}</table>"
        "<h2>Results</h2>"
        "<table><tr><th>Date</th><th>Team</th><th>Stake</th><th>Result</th><th>Profit</th></tr>"
        f"{history_rows_html}</table>"
        "</body></html>"
    )
    return html


if __name__ == "__main__":
    version, coefs = get_latest_model(engine)
    edge_threshold = version[2]
    bankroll = get_current_bankroll(engine)
    print(f"Current bankroll: ${bankroll:,.2f}")
    print(f"Using model fit {version[1]}, edge threshold {edge_threshold}")

    matches = get_candidate_matches(engine)
    print(f"Found {len(matches)} candidate matches with a posted lineup.")

    results = []
    with engine.begin() as conn:
        for m in matches:
            match_id, match_date, home_code, away_code, home_id, away_id = m
            odds = get_latest_odds(engine, match_id)
            home_ml, away_ml = odds if odds else (None, None)

            for team_id, team_code, ml, is_home in [
                (home_id, home_code, home_ml, True), (away_id, away_code, away_ml, False)
            ]:
                features = get_match_features(engine, match_id, team_id)
                model_prob = compute_probability(coefs, team_code, features) if features else None
                games_played = get_games_played(engine, team_id, match_date)
                evaluation = evaluate_side(model_prob, ml, bankroll, edge_threshold, games_played)
                evaluation.update({"match_date": match_date, "team_code": team_code, "is_home": is_home})
                results.append(evaluation)

                if evaluation["status"] in ("bet", "pass"):
                    record_bet(conn, match_id, team_id, evaluation)

    settled_bets = get_settled_bets(engine)
    print(f"Found {len(settled_bets)} settled bets to show in history.")

    os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)
    with open(OUTPUT_PATH, "w") as f:
        f.write(render_html(bankroll, version, results, settled_bets))
    print(f"Dashboard written to {OUTPUT_PATH}")
