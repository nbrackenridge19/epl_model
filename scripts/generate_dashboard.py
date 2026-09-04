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
  - Only bet if the model's predicted probability falls in [0.3, 0.7]
    (PROB_BAND_LOW/HIGH below). Historical validation showed this
    roughly triples profit and is profitable in 6/6 seasons, but a
    formal significance test didn't clear the conventional bar (p=0.07)
    -- shipping this as a deliberate decision, not a proven edge.

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
    pip install sqlalchemy psycopg2-binary patsy --break-system-packages
    (patsy added 2026-09-03 -- needed to reconstruct the strtD spline
    basis at prediction time; see compute_probability/spline_basis)

Set DATABASE_URL the same way as the other scripts.
"""

import os
import math
import json
from datetime import date

import patsy
from sqlalchemy import create_engine, text

DATABASE_URL = os.environ["DATABASE_URL"]
engine = create_engine(DATABASE_URL, pool_pre_ping=True, pool_recycle=280)

SEASON = 2627
KELLY_FRACTION = 0.15
MIN_GAMES_PLAYED = 5
STARTING_BANKROLL = 2176.93  # 2025-26's real ending bankroll -- see conversation for how this was derived
OUTPUT_PATH = "docs/index.html"  # GitHub Pages serves from /docs by default

# Betting eligibility restriction (added 2026-09-03): only bet when the
# model's own predicted probability falls in this band. Historical
# validation (bootstrap/permutation/leave-out) roughly tripled profit and
# was profitable in 6/6 seasons, but did NOT clear a conventional
# significance bar (p=0.07) -- profit is concentrated in a handful of
# underdog wins. Shipping anyway per explicit decision, not because the
# edge is proven durable. Revisit if live results diverge badly from the
# historical pattern.
PROB_BAND_LOW = 0.3
PROB_BAND_HIGH = 0.7


def get_latest_model(engine):
    with engine.connect() as conn:
        version = conn.execute(
            text("""select id, fit_date, min_edge_threshold, spline_config, notes
                     from model_versions order by fit_date desc limit 1""")
        ).fetchone()
        if version is None:
            raise RuntimeError("No model_versions found -- run fit_model.py first.")
        coefs = conn.execute(
            text("select feature_name, coefficient from model_coefficients where model_version_id = :vid"),
            {"vid": version[0]},
        ).fetchall()
    return version, {name: coef for name, coef in coefs}


def get_missing_xg_count(engine):
    """Same query as enter_xg.py's get_missing_xg, just a count for the
    dashboard's warning banner -- enter_xg.py itself has to stay a local,
    interactive script (it prompts for input, which can't run inside a
    scheduled cloud job), so this is just the visible reminder that
    something needs your attention there."""
    with engine.connect() as conn:
        row = conn.execute(
            text("""
                select count(*)
                from matches m
                left join match_team_stats mts_h on mts_h.match_id = m.id and mts_h.team_id = m.home_team_id
                left join match_team_stats mts_a on mts_a.match_id = m.id and mts_a.team_id = m.away_team_id
                where m.status = 'completed'
                  and (mts_h.xg is null or mts_a.xg is null or mts_h.id is null or mts_a.id is null)
            """)
        ).fetchone()
    return row[0]


def get_missing_ratings_count(engine):
    """Same query as add_player_ratings.py's get_missing_ratings, just a
    count for the warning banner -- same reasoning as the xG one, this
    has to stay an interactive local script, not something a scheduled
    job can run on its own."""
    with engine.connect() as conn:
        row = conn.execute(
            text("""
                select count(*) from (
                    select distinct player_id, team_id from (
                        select player_id, team_id from player_match_appearances pma
                        join matches m on m.id = pma.match_id where m.season = :season
                        union
                        select player_id, team_id from predicted_lineups pl
                        join matches m on m.id = pl.match_id where m.season = :season
                    ) seen
                    where not exists (
                        select 1 from player_ratings pr
                        where pr.player_id = seen.player_id and pr.team_id = seen.team_id and pr.season = :season
                    )
                ) x
            """),
            {"season": SEASON},
        ).fetchone()
    return row[0]


def get_headcount_issues_count(engine):
    """A genuinely different check from missing ratings -- this catches
    a malformed/incomplete ESPN API response for a specific player (the
    parsing loop silently skips an entry with no name field), not a
    'we don't have this player's data yet' situation. Since
    get_or_create_player() always creates a row for any name it DOES
    see, the starter count should basically never legitimately drop
    below 11 for a posted lineup."""
    with engine.connect() as conn:
        row = conn.execute(
            text("""
                select count(*) from (
                    select pl.match_id, pl.team_id, count(*) filter (where pl.predicted_status = 2) as starters
                    from predicted_lineups pl
                    join matches m on m.id = pl.match_id
                    where m.season = :season
                    group by pl.match_id, pl.team_id
                    having count(*) filter (where pl.predicted_status = 2) != 11
                ) x
            """),
            {"season": SEASON},
        ).fetchone()
    return row[0]


def get_starter_counts(engine, season):
    """Maps every (match_id, team_id) with ANY predicted_lineups rows this
    season to its posted starter count -- not filtered down to just the
    mismatched ones (that's get_headcount_issues_count's job for the
    summary banner). Used to show a per-row 'Lineup Check' note right next
    to each match/team's own betting recommendation, so a bad lineup pull
    is visible exactly where it matters, not just as an aggregate count
    somewhere else on the page."""
    with engine.connect() as conn:
        rows = conn.execute(
            text("""
                select pl.match_id, pl.team_id, count(*) filter (where pl.predicted_status = 2) as starters
                from predicted_lineups pl
                join matches m on m.id = pl.match_id
                where m.season = :season
                group by pl.match_id, pl.team_id
            """),
            {"season": season},
        ).fetchall()
    return {(r[0], r[1]): r[2] for r in rows}


def get_starters_missing_ratings(engine, season):
    """A genuinely different problem from a bad headcount: a team can show
    exactly 11 posted starters (headcount check passes clean) while some
    of those specific players still have no player_ratings row for this
    team/season -- they silently drop out of the starters_rating average
    (avg() just skips nulls) rather than causing any visible error. This
    surfaced for real: three summer-import duplicate-name players had a
    posted lineup land on the wrong player_id before being merged --
    exactly the kind of thing this check exists to catch before it
    affects a live prediction."""
    with engine.connect() as conn:
        rows = conn.execute(
            text("""
                select pl.match_id, pl.team_id, count(*) as missing_count
                from predicted_lineups pl
                where pl.predicted_status = 2
                  and not exists (
                      select 1 from player_ratings pr
                      where pr.player_id = pl.player_id and pr.team_id = pl.team_id and pr.season = :season
                  )
                group by pl.match_id, pl.team_id
            """),
            {"season": season},
        ).fetchall()
    return {(r[0], r[1]): r[2] for r in rows}


def lineup_check_message(starter_counts, missing_ratings, match_id, team_id):
    count = starter_counts.get((match_id, team_id))
    if count is None:
        return "No lineup posted yet"
    messages = []
    if count != 11:
        messages.append(f"{count} starters posted (expected 11)")
    missing = missing_ratings.get((match_id, team_id), 0)
    if missing > 0:
        messages.append(f"{missing} starter{'s' if missing != 1 else ''} missing a {SEASON} rating")
    return "; ".join(messages)


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
                select home_odds, away_odds, line_type from odds
                where match_id = :match_id and source = 'espn_draftkings'
                order by captured_at desc limit 1
            """),
            {"match_id": match_id},
        ).fetchone()
    return row


def spline_basis(value, knots, degree=3, lower_bound=None, upper_bound=None):
    """Reconstructs the exact same B-spline basis patsy produced at fit
    time for a single raw value, using the knot locations (and explicit
    boundary knots) stored in model_versions.spline_config. Must stay in
    lockstep with fit_model.py's build_formula -- same knots, same
    bounds, same degree, include_intercept=False -- or the basis won't
    match the stored coefficients.

    lower_bound/upper_bound MUST be passed and must be the training
    data's actual min/max: patsy's bs() infers boundary knots from
    whatever data it's given, and a single live value has no "range" of
    its own to infer from -- without an explicit bound this raises a
    ValueError the instant a live value falls outside the guessed range.
    A live match's strtD more extreme than anything in training data is
    clipped into range rather than crashing the whole dashboard run."""
    if lower_bound is not None and upper_bound is not None:
        value = min(max(value, lower_bound), upper_bound)
    formula = (f"bs(x, knots={knots!r}, degree={degree}, "
               f"lower_bound={lower_bound!r}, upper_bound={upper_bound!r}, include_intercept=False) - 1")
    design = patsy.dmatrix(formula, {"x": [value]})
    return list(design[0])


def compute_probability(coefs, team_code, features, spline_config=None):
    # Cast every value to float explicitly. Postgres NUMERIC columns
    # come back as Decimal via SQLAlchemy, and the 9 named features
    # below stay Decimal-consistent throughout (Decimal coefficient *
    # Decimal feature value), so that part silently worked. But a team
    # with no fixed-effect coefficient (the promoted teams -- they've
    # never appeared in the model's training data) hits the .get(...,
    # 0.0) fallback, a literal Python float -- Decimal += float raises
    # TypeError. Casting everything to float up front avoids this
    # regardless of what type any individual value happens to be.
    logit = float(coefs.get("Intercept", 0.0))
    spline_config = spline_config or {}
    names = ["avgatt", "xgfpgd", "xgapgd", "gkpgd", "possd", "strtd", "bertd", "stmsd", "formcd"]
    for name, value in zip(names, features):
        if value is None:
            return None
        if name == "possd":
            continue  # tracked descriptively but not part of the regression formula
        if name in spline_config:
            cfg = spline_config[name]
            basis_values = spline_basis(float(value), cfg["knots"], cfg.get("degree", 3),
                                         cfg.get("lower_bound"), cfg.get("upper_bound"))
            for i, bv in enumerate(basis_values):
                logit += float(coefs.get(f"{name}_bs{i}", 0.0)) * bv
            continue
        logit += float(coefs.get(name, 0.0)) * float(value)
    logit += float(coefs.get(f"C(team_code)[T.{team_code}]", 0.0))
    return 1 / (1 + math.exp(-logit))


def moneyline_to_implied_prob(ml):
    if ml is None:
        return None
    return 100 / (ml + 100) if ml > 0 else -ml / (-ml + 100)


def moneyline_to_net_odds(ml):
    return ml / 100 if ml > 0 else -100 / ml


def evaluate_side(model_prob, ml, line_type, bankroll, edge_threshold, games_played):
    if model_prob is None:
        return {"status": "no_prediction"}

    implied_prob = moneyline_to_implied_prob(ml)

    if games_played < MIN_GAMES_PLAYED:
        return {"status": "too_early", "model_prob": model_prob, "implied_prob": implied_prob,
                "line_type": line_type, "games_played": games_played}
    if ml is None:
        return {"status": "no_odds", "model_prob": model_prob}

    if line_type != "closing":
        # Opening line (or a legacy/untyped row) -- illustrative only. The
        # line can still move before kickoff, so never compute kf/stake
        # off it; wait for the actual closing snapshot.
        return {"status": "awaiting_closing", "model_prob": model_prob, "implied_prob": implied_prob,
                "line_type": line_type, "moneyline": ml}

    if not (PROB_BAND_LOW <= model_prob <= PROB_BAND_HIGH):
        # Outside the validated betting band -- see PROB_BAND_LOW/HIGH
        # comment at top of file. Model still considered this and passed,
        # same as a below-edge-threshold "pass".
        return {"status": "outside_prob_band", "model_prob": model_prob, "implied_prob": implied_prob,
                "line_type": line_type, "moneyline": ml}

    b = moneyline_to_net_odds(ml)
    kf = model_prob - (1 - model_prob) / b

    if edge_threshold is None or kf < edge_threshold * 2:
        return {"status": "pass", "model_prob": model_prob, "implied_prob": implied_prob,
                "line_type": line_type, "moneyline": ml, "kf": kf}

    stake = kf * bankroll * KELLY_FRACTION
    return {"status": "bet", "model_prob": model_prob, "implied_prob": implied_prob,
            "line_type": line_type, "moneyline": ml, "kf": kf, "stake": stake}


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


def get_season_summaries(engine):
    """Every completed season's walk-forward backtest result -- NOT the
    real historical bets table. That table holds whatever model was
    actually live when each bet was historically placed (largely
    Excel-migrated results from years of different, evolving formulas),
    which is a real record of what happened but stops being a fair
    comparison point once the model changes -- it doesn't tell you
    anything about how the CURRENT modeling approach would have done.

    season_backtests is a precomputed table (see backtest_seasons.py,
    run manually, not scheduled -- same pattern as fit_model.py) holding
    a genuine walk-forward backtest: each season refit using only data
    available before it, respecting the historical team-dummy
    introduction rule (no C(team_code) before the 1920-2223 training
    window, matching the original spreadsheet's own formula evolution),
    with today's possD-drop/strtD-spline structure and [0.3,0.7]
    probability band applied at every step. Re-run backtest_seasons.py
    whenever the model or betting-eligibility logic changes, or a new
    season completes, to keep this current."""
    with engine.connect() as conn:
        rows = conn.execute(
            text("""
                select season, bets, wins, losses, wagered, profit, return_pct, logloss, market_logloss
                from season_backtests order by season
            """)
        ).fetchall()

    summaries = []
    for season, bets, wins, losses, wagered, profit, return_pct, logloss, market_logloss in rows:
        summaries.append({
            "season": season, "bets_placed": bets, "wins": wins, "losses": losses,
            "total_profit": float(profit), "total_wagered": float(wagered),
            "return_pct": float(return_pct) if return_pct is not None else None,
            "model_logloss": float(logloss), "market_logloss": float(market_logloss),
        })
    return summaries


def render_html(bankroll, model_version, results, settled_bets, season_summaries, missing_xg_count, missing_ratings_count, headcount_issues_count):
    rows_html = ""
    for r in results:
        badge = {"bet": "BET", "pass": "pass", "too_early": "too early",
                 "awaiting_closing": "awaiting closing line",
                 "outside_prob_band": "outside betting band",
                 "no_odds": "no odds yet", "no_prediction": "no lineup yet"}[r["status"]]
        badge_color = {"bet": "#1a7f37", "pass": "#666", "too_early": "#999",
                       "awaiting_closing": "#b8860b",
                       "outside_prob_band": "#999",
                       "no_odds": "#999", "no_prediction": "#999"}[r["status"]]
        model_pct = f"{r['model_prob']:.1%}" if r.get("model_prob") is not None else "-"
        if r.get("implied_prob") is not None:
            market_pct = f"{r['implied_prob']:.1%}"
            if r.get("line_type"):
                market_pct += f" <span class=\"line-tag\">({r['line_type']})</span>"
        else:
            market_pct = "-"
        stake_str = f"${r['stake']:.2f}" if r["status"] == "bet" else "-"
        lineup_check = r.get("lineup_check") or ""
        lineup_check_html = (
            f"<span style=\"color:#c0392b; font-weight:600;\">{lineup_check}</span>" if lineup_check else ""
        )
        rows_html += (
            "<tr>"
            f"<td>{r['match_date']}</td>"
            f"<td>{r['team_code'].upper()} {'(H)' if r['is_home'] else '(A)'}</td>"
            f"<td>{model_pct}</td>"
            f"<td>{market_pct}</td>"
            f"<td>{stake_str}</td>"
            f"<td><span style=\"color:{badge_color}; font-weight:600;\">{badge}</span></td>"
            f"<td>{lineup_check_html}</td>"
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
        ".line-tag { color: #999; font-size: 11px; } "
        "@media (max-width: 480px) { th, td { font-size: 12px; padding: 8px 4px; } }"
    )

    season_rows_html = ""
    for s in season_summaries:
        win_pct = f"{s['wins'] / s['bets_placed']:.0%}" if s['bets_placed'] else "-"
        profit_str = f"{'+' if s['total_profit'] >= 0 else ''}${s['total_profit']:,.2f}"
        profit_color = "#1a7f37" if s['total_profit'] >= 0 else "#c0392b"
        return_str = f"{s['return_pct']:+.1%}" if s['return_pct'] is not None else "-"
        model_ll = f"{s['model_logloss']:.3f}" if s['model_logloss'] is not None else "-"
        market_ll = f"{s['market_logloss']:.3f}" if s['market_logloss'] is not None else "-"
        season_display = f"{str(s['season'])[:2]}-{str(s['season'])[2:]}"
        season_rows_html += (
            "<tr>"
            f"<td>{season_display}</td>"
            f"<td>{s['bets_placed']}</td>"
            f"<td>{s['wins']}-{s['losses']} ({win_pct})</td>"
            f"<td>${s['total_wagered']:,.2f}</td>"
            f"<td><span style=\"color:{profit_color}; font-weight:600;\">{profit_str}</span></td>"
            f"<td>{return_str}</td>"
            f"<td>{model_ll}</td>"
            f"<td>{market_ll}</td>"
            "</tr>"
        )

    xg_warning_html = ""
    if missing_xg_count > 0:
        xg_warning_html = (
            f"<div style=\"background:#fff3cd; border:1px solid #ffc107; border-radius:6px; "
            f"padding:10px 12px; margin-bottom:16px; font-size:14px;\">"
            f"&#9888; {missing_xg_count} completed match{'es' if missing_xg_count != 1 else ''} "
            f"missing xG &mdash; run enter_xg.py</div>"
        )

    ratings_warning_html = ""
    if missing_ratings_count > 0:
        ratings_warning_html = (
            f"<div style=\"background:#fff3cd; border:1px solid #ffc107; border-radius:6px; "
            f"padding:10px 12px; margin-bottom:16px; font-size:14px;\">"
            f"&#9888; {missing_ratings_count} player{'s' if missing_ratings_count != 1 else ''} "
            f"missing a {SEASON} rating &mdash; run add_player_ratings.py</div>"
        )

    headcount_warning_html = ""
    if headcount_issues_count > 0:
        # Red, not yellow -- this is a different class of problem (an
        # ESPN API/parsing issue) from the yellow "known, expected,
        # needs manual entry" warnings above.
        headcount_warning_html = (
            f"<div style=\"background:#f8d7da; border:1px solid #dc3545; border-radius:6px; "
            f"padding:10px 12px; margin-bottom:16px; font-size:14px;\">"
            f"&#9888; {headcount_issues_count} team-match{'es' if headcount_issues_count != 1 else ''} "
            f"showing a starter count &ne; 11 &mdash; likely an ESPN parsing issue, check poll_espn_lineups.py logs</div>"
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
        f"{xg_warning_html}"
        f"{ratings_warning_html}"
        f"{headcount_warning_html}"
        "<table><tr><th>Date</th><th>Team</th><th>Model %</th><th>Market %</th>"
        f"<th>Stake</th><th>Decision</th><th>Lineup Check</th></tr>{rows_html}</table>"
        "<h2>Results</h2>"
        "<table><tr><th>Date</th><th>Team</th><th>Stake</th><th>Result</th><th>Profit</th></tr>"
        f"{history_rows_html}</table>"
        "<h2>Past Seasons</h2>"
        "<table><tr><th>Season</th><th>Bets</th><th>Record</th><th>Wagered</th>"
        "<th>Profit</th><th>Return</th><th>LogLoss</th><th>Mkt LogLoss</th></tr>"
        f"{season_rows_html}</table>"
        "</body></html>"
    )
    return html


if __name__ == "__main__":
    version, coefs = get_latest_model(engine)
    edge_threshold = version[2]
    spline_config = version[3] or {}
    if isinstance(spline_config, str):  # psycopg2 usually auto-parses jsonb, but be defensive
        spline_config = json.loads(spline_config)
    bankroll = get_current_bankroll(engine)
    print(f"Current bankroll: ${bankroll:,.2f}")
    print(f"Using model fit {version[1]}, edge threshold {edge_threshold}")

    matches = get_candidate_matches(engine)
    print(f"Found {len(matches)} candidate matches with a posted lineup.")

    starter_counts = get_starter_counts(engine, SEASON)
    starters_missing_ratings = get_starters_missing_ratings(engine, SEASON)

    results = []
    with engine.begin() as conn:
        for m in matches:
            match_id, match_date, home_code, away_code, home_id, away_id = m
            odds = get_latest_odds(engine, match_id)
            home_ml, away_ml, line_type = odds if odds else (None, None, None)

            for team_id, team_code, ml, is_home in [
                (home_id, home_code, home_ml, True), (away_id, away_code, away_ml, False)
            ]:
                features = get_match_features(engine, match_id, team_id)
                model_prob = compute_probability(coefs, team_code, features, spline_config) if features else None
                games_played = get_games_played(engine, team_id, match_date)
                evaluation = evaluate_side(model_prob, ml, line_type, bankroll, edge_threshold, games_played)
                evaluation.update({
                    "match_date": match_date, "team_code": team_code, "is_home": is_home,
                    "lineup_check": lineup_check_message(starter_counts, starters_missing_ratings, match_id, team_id),
                })
                results.append(evaluation)

                if evaluation["status"] in ("bet", "pass", "outside_prob_band"):
                    record_bet(conn, match_id, team_id, evaluation)

    settled_bets = get_settled_bets(engine)
    print(f"Found {len(settled_bets)} settled bets to show in history.")

    season_summaries = get_season_summaries(engine)
    print(f"Found {len(season_summaries)} seasons with settled bet history.")

    missing_xg_count = get_missing_xg_count(engine)
    print(f"Missing xG: {missing_xg_count} completed matches.")

    missing_ratings_count = get_missing_ratings_count(engine)
    print(f"Missing player ratings: {missing_ratings_count} players.")

    headcount_issues_count = get_headcount_issues_count(engine)
    print(f"Headcount issues: {headcount_issues_count} team-matches with starters != 11.")

    os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)
    with open(OUTPUT_PATH, "w") as f:
        f.write(render_html(bankroll, version, results, settled_bets, season_summaries,
                             missing_xg_count, missing_ratings_count, headcount_issues_count))
    print(f"Dashboard written to {OUTPUT_PATH}")
