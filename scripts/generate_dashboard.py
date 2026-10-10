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

Current bankroll is computed dynamically: the season's starting bankroll
(STARTING_BANKROLL, $2,500 -- reset at the start of each season; 2025-26
also started from $2,500 and ended at $2,176.93) plus the sum of any
already-settled bets' profit for the season. No hardcoded running total to
maintain -- it just adds up correctly every time this runs.

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

DASHBOARD LAYOUT (restructured 2026-10-09 to mirror nhl_generate_dashboard.py)
--------------------------------------------------------------------------
Betting logic above is unchanged. Only the page was rebuilt. Sections, top to bottom:

- Header + "Needs attention" callout: the three former warning banners (missing xG,
  missing ratings, starter count != 11) merged into one box. Turns red if any
  team-match has a bad starter count (the ESPN-parsing class of problem), amber otherwise.
- Next matchweek: replaces "Today's matches". Same two-rows-per-game layout as the NHL
  page (dashed rule between the two teams, thick rule closing out the game), home row first.
  Columns: Date, Matchup (Away @ Home), Team, Bet?, $ Amount, Model %, Market %, Delta,
  Lineup check. Bet? shows BET or the reason there is no bet (pass, too early, outside
  betting band, awaiting lineup odds, ...).
- <season> at a glance: one summary row for the detail season (same columns as Past seasons,
  incl. Wallet Return).
- <season> wallet & LogLoss over time: inline SVG (wallet $ + cumulative average LL delta on
  one panel, daily $ result bars below), x axis spans the whole season's fixture list.
- Last matchweek: replaces "Yesterday's games". One collapsible block, same as a row in
  "by matchweek".
- <season> by matchweek / by team: collapsible blocks. Each block's summary row shows
  Bets, Record, Wagered, Profit, Return and LogLoss (model / market / delta) twice -- over the
  bets placed and over all games -- and opens to a "Bets placed" and an "All games" table.
  Game tables are one row per game, away side first, with the score (A-H) and wager & result.
- Past seasons: walk-forward backtest rows from season_backtests, chained from a $2,500 start in
  2020-21 (Start Bank, End Bank, Wallet Return columns).

Matchweek definitions (see get_matchweek_context). Matchweek NUMBERS are used, not dates,
because postponed matches keep their original matchweek number but get a new date (2025-26
matchweek 31 spans Feb-May):
  - last completed matchweek = highest matchweek number with a completed match;
  - next matchweek = lowest matchweek number >= that with a match not yet completed.
    If that is the same matchweek (a round in progress), it IS the "next (or current)"
    matchweek and "last matchweek" falls back to the previous round.
Any match with a posted lineup, or kicking off today, is also listed in the next-matchweek
table (the candidate set the betting loop has always used), so nothing the loop evaluates
is hidden.

Detail season: the season shown in the at-a-glance row, chart and drilldowns is whichever
season has the most recent completed match, so it flips to the new season by itself when
that season's first match finishes. Override with the DETAIL_SEASON env var (e.g. 2526).

Bank: in-year. Every season starts at STARTING_BANKROLL ($2,500) and compounds only with that
season's own settled bets. Wallet Return = (ending bank - starting bank) / starting bank.
Past seasons: $2,500 invested at the start of 2020-21 (CHAIN_START_BANKROLL) with each season's
ending bank carried into the next. season_backtests holds each season on a flat reset basis
($2,176.93 per backtest_seasons.py); because that script sizes every stake off the running bank
(stake = Kelly x current bank, bank updated after each bet), a season's result scales exactly
with its starting bank, so the chain is computed here by rescaling the stored dollars -- no
re-run needed. Start Bank / End Bank / Wagered / Profit are the chained figures; Return
(profit / wagered), record and LogLoss are unaffected. The Total row's Wallet Return is the
cumulative return on the original $2,500. This is separate from 2026-27, whose bank is $2,500
for the actual betting decisions.

Picks correct: each matchweek and team block also shows how many games the model and the
market picked right (the side with the higher probability is the pick; ties go to home; a draw
counts against whichever side was picked) -- same definition as the old matchweek summaries.
Games where either side lacks a probability are left out of that block's denominator.

All figures for the detail season come from the real `bets` table (stake, outcome, profit);
Model % / Market % / LogLoss are recomputed live, as before.
"""

import os
import re
import math
import json
from datetime import datetime, time as dtime, timedelta, timezone

import patsy
from sqlalchemy import create_engine, text

DATABASE_URL = os.environ["DATABASE_URL"]
engine = create_engine(DATABASE_URL, pool_pre_ping=True, pool_recycle=280)

SEASON = 2627
KELLY_FRACTION = 0.15
MIN_GAMES_PLAYED = 5
STARTING_BANKROLL = 2500.00  # each season's bank resets to this at the start of the season (set 2026-10-09)
# Past seasons: a hypothetical $2,500 invested at the start of the first backtested season (2020-21), with each
# season's ending bank carried into the next so the table shows the effect of compounding.
CHAIN_START_BANKROLL = 2500.00
# season_backtests dollar figures were computed from a flat per-season reset (backtest_seasons.py's
# STARTING_BANKROLL, recorded in each row's notes as "resets to $X"). Used only as a fallback if a row's notes
# can't be parsed. See chain_backtest_seasons for why rescaling those figures is exact.
BACKTEST_BASE_BANKROLL = 2176.93
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


def season_start_bankroll(season):
    """Every season starts from STARTING_BANKROLL. Kept as a function so a one-off per-season
    override would be a one-line change."""
    return STARTING_BANKROLL


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
    """In-year bank: the season's starting bankroll plus only this season's own settled bets."""
    with engine.connect() as conn:
        settled_profit = conn.execute(
            text("""
                select coalesce(sum(b.profit), 0)
                from bets b join matches m on m.id = b.match_id
                where m.season = :season and b.outcome is not null
            """),
            {"season": SEASON},
        ).fetchone()[0]
    return season_start_bankroll(SEASON) + float(settled_profit)


def get_matchweek_context(engine, season):
    """(next_mw, last_mw) for a season, by matchweek NUMBER (postponed matches keep their original
    number but move date, so dates would mislead -- see module docstring).
      last_done = highest matchweek with a completed match
      next_mw   = lowest matchweek >= last_done with a match not yet completed (None if none left)
      last_mw   = last_done, unless the next matchweek is that same round still in progress, in which
                  case the previous completed round."""
    with engine.connect() as conn:
        last_done = conn.execute(
            text("select max(matchweek) from matches where season = :season and status = 'completed'"),
            {"season": season},
        ).fetchone()[0]
        if last_done is None:
            next_mw = conn.execute(
                text("select min(matchweek) from matches where season = :season and status != 'completed'"),
                {"season": season},
            ).fetchone()[0]
        else:
            next_mw = conn.execute(
                text("""select min(matchweek) from matches
                        where season = :season and status != 'completed' and matchweek >= :last_done"""),
                {"season": season, "last_done": last_done},
            ).fetchone()[0]
        last_mw = last_done
        if last_done is not None and next_mw is not None and next_mw <= last_done:
            last_mw = conn.execute(
                text("""select max(matchweek) from matches
                        where season = :season and status = 'completed' and matchweek < :next_mw"""),
                {"season": season, "next_mw": next_mw},
            ).fetchone()[0]
    return next_mw, last_mw


def get_candidate_matches(engine, next_mw):
    """Every not-yet-completed match the betting loop should evaluate and the page should list: the whole
    next matchweek, plus (as before) anything kicking off today or with a posted lineup."""
    with engine.connect() as conn:
        rows = conn.execute(
            text("""
                select distinct m.id, m.match_date, m.kickoff_time, m.matchweek,
                       ht.code as home_code, at.code as away_code,
                       ht.id as home_id, at.id as away_id
                from matches m
                join teams ht on ht.id = m.home_team_id
                join teams at on at.id = m.away_team_id
                where m.season = :season and m.status != 'completed'
                  and (m.match_date = current_date
                       or m.matchweek = :next_mw
                       or exists (select 1 from predicted_lineups pl where pl.match_id = m.id))
                order by m.match_date, m.kickoff_time, m.id
            """),
            {"season": SEASON, "next_mw": next_mw},
        ).fetchall()
    return rows


def get_detail_season(engine):
    """Season shown in the at-a-glance row, chart and drilldowns: the one with the most recent completed
    match. DETAIL_SEASON env var overrides (e.g. DETAIL_SEASON=2526)."""
    override = os.environ.get("DETAIL_SEASON", "").strip()
    if override:
        return int(override)
    with engine.connect() as conn:
        row = conn.execute(text("select max(season) from matches where status = 'completed'")).fetchone()
    return row[0] if row and row[0] else SEASON


def get_season_bounds(engine, season):
    """First and last fixture dates of a season, for the chart's full-season x axis."""
    with engine.connect() as conn:
        row = conn.execute(
            text("select min(match_date), max(match_date) from matches where season = :season"),
            {"season": season},
        ).fetchone()
    return row[0], row[1]


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


def get_bet_odds(engine, match_id):
    """The odds row bets are evaluated against. Prefers the lineup_release snapshot (captured at T-50 by the
    Cloudflare trigger Worker, around lineup release), so a later closing snapshot (T-5, kept for the data model)
    never changes a bet. If a match has no lineup_release row (e.g. that capture failed), falls back to the
    newest row, which is the previous behaviour: a closing row is still bettable, an opening row is not."""
    with engine.connect() as conn:
        row = conn.execute(
            text("""
                select home_odds, away_odds, line_type from odds
                where match_id = :match_id and source = 'espn_draftkings'
                order by (line_type = 'lineup_release') desc, captured_at desc limit 1
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

    if line_type not in ("lineup_release", "closing"):
        # Opening line (or a legacy/untyped row) -- illustrative only. The
        # line can still move a lot before kickoff, so never compute kf/stake
        # off it; wait for the lineup_release snapshot (captured at T-50).
        # ("closing" stays bettable as a fallback for a match whose
        # lineup_release capture failed -- see get_bet_odds.)
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


def _logloss(p, y):
    """Per-instance log loss of probability p against outcome y (1 = the team won, 0 = drew or lost,
    same convention as the previous matchweek summaries). None if p is missing or degenerate."""
    if p is None:
        return None
    p = float(p)
    if not (0 < p < 1):
        return None
    return -(y * math.log(p) + (1 - y) * math.log(1 - p))


def get_completed_match_results(engine, coefs, spline_config, season):
    """Every completed match of `season`, both team perspectives, as one instance dict per team-match.
    Model % and Market % are ALWAYS recomputed live -- current model coefficients
    against v_match_model_features_predicted, and the latest odds row --
    exactly like the next-matchweek loop does for upcoming games, rather
    than read from any persisted bets row. That means it reflects the
    CURRENT model's read on every match regardless of whether it was ever
    actually bet on, evaluated, or even reached by the pipeline live
    (deliberate choice, not a bug: there's no live-prediction snapshot to
    fall back to for a match the pipeline skipped). Wager amount and bet
    result are the one piece pulled from `bets`, since that's real
    financial history that can't be recomputed after the fact.

    Field names follow the NHL dashboard's instances so the shared rendering code lines up:
    win = the team won the match (a draw counts as not winning, as in moneyline settlement);
    bet_outcome = 'win' / 'loss' from bets.outcome, None while a placed bet is unsettled;
    profit_dollar = bets.profit (None while unsettled)."""
    with engine.connect() as conn:
        matches = conn.execute(
            text("""
                select m.id, m.matchweek, m.match_date, m.kickoff_time, ht.code as home_code, at.code as away_code,
                       ht.id as home_id, at.id as away_id, m.home_goals, m.away_goals
                from matches m
                join teams ht on ht.id = m.home_team_id
                join teams at on at.id = m.away_team_id
                where m.season = :season and m.status = 'completed'
                order by m.matchweek desc, m.match_date desc
            """),
            {"season": season},
        ).fetchall()
        bet_rows = conn.execute(
            text("""
                select b.match_id, b.team_id, b.stake, b.outcome, b.profit
                from bets b join matches m on m.id = b.match_id
                where m.season = :season
            """),
            {"season": season},
        ).fetchall()
    bets_by_match_team = {(mid, tid): (float(stake or 0), outcome, float(profit) if profit is not None else None)
                           for mid, tid, stake, outcome, profit in bet_rows}

    instances = []
    for (match_id, matchweek, match_date, kickoff, home_code, away_code, home_id, away_id,
         home_goals, away_goals) in matches:
        if home_goals is None or away_goals is None:
            continue  # marked completed but scores not in yet -- shouldn't normally happen
        odds = get_latest_odds(engine, match_id)
        home_ml, away_ml, line_type = odds if odds else (None, None, None)

        for team_id, team_code, opp_code, ml, is_home in [
            (home_id, home_code, away_code, home_ml, True), (away_id, away_code, home_code, away_ml, False)
        ]:
            features = get_match_features(engine, match_id, team_id)
            model_prob = compute_probability(coefs, team_code, features, spline_config) if features else None
            market_prob = moneyline_to_implied_prob(ml)
            model_prob = float(model_prob) if model_prob is not None else None
            market_prob = float(market_prob) if market_prob is not None else None
            team_goals = home_goals if is_home else away_goals
            opp_goals = away_goals if is_home else home_goals
            y = 1.0 if team_goals > opp_goals else 0.0
            stake, bet_outcome, profit = bets_by_match_team.get((match_id, team_id), (0.0, None, None))
            instances.append({
                "game_id": match_id, "matchweek": matchweek, "season": season, "date": match_date,
                "kickoff": kickoff, "team": team_code, "opp": opp_code, "home": is_home,
                "win": team_goals > opp_goals,
                "model_pct": model_prob, "mlpct": market_prob,
                "logloss": _logloss(model_prob, y), "vlogloss": _logloss(market_prob, y),
                "bets_fire": stake > 0, "stake_dollar": stake, "bet_outcome": bet_outcome,
                "profit_dollar": profit,
                "home_goals": home_goals, "away_goals": away_goals,
            })
    return instances


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
    season completes, to keep this current. Returned dollar figures are chained (see
    chain_backtest_seasons) from CHAIN_START_BANKROLL."""
    with engine.connect() as conn:
        rows = conn.execute(
            text("""
                select season, bets, wins, losses, wagered, profit, return_pct, logloss, market_logloss, notes
                from season_backtests order by season
            """)
        ).fetchall()

    summaries = []
    for season, bets, wins, losses, wagered, profit, return_pct, logloss, market_logloss, notes in rows:
        m = re.search(r"resets to \$([\d,]+(?:\.\d+)?)", notes or "")
        base = float(m.group(1).replace(",", "")) if m else BACKTEST_BASE_BANKROLL
        summaries.append({
            "season": season, "bets_placed": bets, "wins": wins, "losses": losses,
            "wagered": float(wagered), "profit": float(profit),
            "return_pct": float(return_pct) if return_pct is not None else None,
            "model_logloss": float(logloss), "market_logloss": float(market_logloss),
            "base_bankroll": base,
        })
    return chain_backtest_seasons(summaries)


def chain_backtest_seasons(summaries, start_bank=CHAIN_START_BANKROLL):
    """Carries each season's ending bank into the next, starting from start_bank at the first season.

    Exact, not an approximation: backtest_seasons.py sizes each stake as kelly_fraction x CURRENT bank and
    adds the result to the bank after every bet, so the bank is multiplied by (1 + stake/bank x payoff) at
    each bet -- independent of the starting bank. Starting a season with B instead of its stored base bank
    therefore multiplies every stake and profit by B/base. Wagered, profit and the bank figures are rescaled
    accordingly; bet counts, record, return_pct and LogLoss do not depend on the bank and are left alone.
    Adds start_bank / end_bank to each row."""
    bank = start_bank
    out = []
    for s in sorted(summaries, key=lambda x: x["season"]):
        k = bank / s["base_bankroll"]
        profit = s["profit"] * k
        out.append({**s, "wagered": s["wagered"] * k, "profit": profit,
                    "start_bank": bank, "end_bank": bank + profit})
        bank += profit
    return out


# --------------------------- instance grouping + aggregation ---------------------------

def _pair_key(p):
    return (p["date"], p.get("kickoff") or dtime.min, str(p["game_id"]))


def pair_by_game(instances):
    """Groups the per-team-perspective instances back into one row per GAME, away side first, home side
    second, matching how a viewer reads a schedule (consistent away-then-home ordering, actual score
    shown). Returns a list sorted by date/kickoff, each item holding both sides' instance dicts under
    'away'/'home'."""
    by_game = {}
    for x in instances:
        by_game.setdefault(x["game_id"], {})["home" if x["home"] else "away"] = x
    pairs = []
    for game_id, sides in by_game.items():
        away, home = sides.get("away"), sides.get("home")
        if away is None or home is None:
            continue  # shouldn't happen -- both sides are always built for a completed match
        pairs.append({"game_id": game_id, "date": away["date"], "kickoff": away.get("kickoff"),
                      "away": away, "home": home})
    return sorted(pairs, key=_pair_key)


def group_by(instances, key_fn):
    groups = {}
    for x in instances:
        groups.setdefault(key_fn(x), []).append(x)
    return groups


def build_bank_days(instances, start_bank):
    """[(date, in-year bank at end of day, that day's settled $ result)] for every date with a completed match,
    starting from the season's starting bankroll. Days with no settled bet carry a result of 0."""
    by_date = {}
    for x in instances:
        by_date.setdefault(x["date"], 0.0)
        if x["bets_fire"] and x["bet_outcome"] in ("win", "loss"):
            by_date[x["date"]] += x["profit_dollar"] or 0.0
    bank, days = start_bank, []
    for d in sorted(by_date):
        bank += by_date[d]
        days.append((d, bank, by_date[d]))
    return days


def cumulative_avg_ll_delta_series(instances):
    """(date, running average of logloss-vlogloss over every instance up to and including that date)
    -- a cumulative average, not a per-day one, so it doesn't whipsaw on days with only a couple of games."""
    by_date = {}
    for x in sorted(instances, key=lambda x: x["date"]):
        if x["logloss"] is not None and x["vlogloss"] is not None:
            by_date.setdefault(x["date"], []).append(x["logloss"] - x["vlogloss"])
    out = []
    total, n = 0.0, 0
    for dt in sorted(by_date):
        for v in by_date[dt]:
            total += v
            n += 1
        out.append((dt, total / n))
    return out


def aggregate(instances):
    """bets/wagered/profit/return are about the fired bets. Wagered, profit and the W-L record only count
    SETTLED bets (bets.outcome set), so a placed-but-unsettled bet can't distort the return; 'bets' counts
    every placed bet. LogLoss is reported two ways -- over just the fired-bet instances ('bet_*') and over
    every instance ('all_*')."""
    fired = [x for x in instances if x["bets_fire"]]
    settled = [x for x in fired if x["bet_outcome"] in ("win", "loss")]
    wins = sum(1 for x in settled if x["bet_outcome"] == "win")
    losses = len(settled) - wins
    wagered = sum(x["stake_dollar"] for x in settled)
    profit = sum(x["profit_dollar"] or 0.0 for x in settled)

    def ll_pair(pool):
        ll = [x["logloss"] for x in pool if x["logloss"] is not None]
        vll = [x["vlogloss"] for x in pool if x["vlogloss"] is not None]
        return (sum(ll) / len(ll)) if ll else None, (sum(vll) / len(vll)) if vll else None

    bet_ll, bet_vll = ll_pair(fired)
    all_ll, all_vll = ll_pair(instances)
    return {
        "n_instances": len(instances), "bets_placed": len(fired), "wins": wins, "losses": losses,
        "wagered": wagered, "profit": profit,
        "return_pct": (profit / wagered) if wagered else None,
        "bet_model_logloss": bet_ll, "bet_market_logloss": bet_vll,
        "all_model_logloss": all_ll, "all_market_logloss": all_vll,
        "model_logloss": all_ll, "market_logloss": all_vll,
    }


def pick_accuracy(pairs):
    """Picks correct, Model vs Market, over completed games. A 'pick' is the side with the higher win
    probability (ties go to the home side); it is correct only if that side won, so a draw counts as
    incorrect for both. Returns (model_correct, model_n, market_correct, market_n); a game only counts
    toward a source if both sides have a probability from it."""
    mc = me = kc = ke = 0
    for p in pairs:
        a, h = p["away"], p["home"]
        if a["model_pct"] is not None and h["model_pct"] is not None:
            me += 1
            picked = h if h["model_pct"] >= a["model_pct"] else a
            if picked["win"]:
                mc += 1
        if a["mlpct"] is not None and h["mlpct"] is not None:
            ke += 1
            picked = h if h["mlpct"] >= a["mlpct"] else a
            if picked["win"]:
                kc += 1
    return mc, me, kc, ke


# --------------------------- rendering ---------------------------

STYLE = (
    "body { font-family: -apple-system, sans-serif; max-width: 1200px; margin: 0 auto; "
    "padding: 16px; background: #fafafa; color: #4a4a4a; } "
    "h1 { font-size: 20px; } h2 { font-size: 16px; margin-top: 28px; } "
    ".meta { color: #666; font-size: 13px; margin-bottom: 16px; } "
    "table { width: 100%; border-collapse: collapse; background: white; border-radius: 8px; "
    "overflow: hidden; margin-bottom: 8px; } "
    "th, td { padding: 10px 8px; text-align: left; font-size: 14px; border-bottom: 1px solid #eee; } "
    "th { background: #f0f0f0; font-size: 12px; text-transform: uppercase; } "
    ".tag { color: #999; font-size: 11px; } "
    ".scroll-wrap { overflow-x: auto; margin-bottom: 8px; } "
    ".row-grid { display: grid; gap: 4px 10px; align-items: center; padding: 8px 12px; "
    "font-size: 12px; min-width: 920px; } "
    ".row-grid > div { white-space: nowrap; } "
    ".group-head, .col-head { background: #f0f0f0; font-weight: 600; text-transform: uppercase; "
    "font-size: 10px; color: #555; } "
    ".group-head { padding-bottom: 0; } "
    ".col-head { padding-top: 2px; border-radius: 8px 8px 0 0; } "
    ".group-label { text-align: center; border-bottom: 1px solid #ddd; padding-bottom: 2px; } "
    ".block { background: white; border-radius: 0 0 8px 8px; margin-bottom: 8px; overflow: hidden; "
    "border-top: 1px solid #eee; } "
    ".block:first-of-type { border-top: none; } "
    ".block > summary { cursor: pointer; list-style: none; } "
    ".block > summary::-webkit-details-marker { display: none; } "
    ".block > summary::before { content: '\\25B8'; margin-right: 4px; color: #999; } "
    ".block[open] > summary::before { content: '\\25BE'; } "
    ".block-title { font-weight: 700; } "
    ".nested { margin: 0 12px 10px; border: 1px solid #eee; border-radius: 6px; overflow: hidden; } "
    ".nested summary { padding: 8px 10px; cursor: pointer; list-style: none; font-size: 12px; "
    "font-weight: 600; color: #444; background: #fbfbfb; } "
    ".nested summary::-webkit-details-marker { display: none; } "
    ".nested summary::before { content: '\\25B8'; margin-right: 4px; color: #999; } "
    ".nested[open] summary::before { content: '\\25BE'; } "
    "table.upcoming tr.team-top td { border-bottom: 1px dashed #aaa; } "
    "table.upcoming tr.game-end td { border-bottom: 3px solid #8a8f98; } "
    "table.detail { border-radius: 0; margin: 0; box-shadow: none; } "
    "table.detail th, table.detail td { padding: 8px; white-space: nowrap; } "
    "@media (max-width: 480px) { th, td { font-size: 12px; padding: 8px 4px; } }"
)


def logloss_delta_bg(delta, scale=0.04):
    """delta = model_logloss - market_logloss. Negative means the model beat the market (lower loss is
    better) -> green; positive means the market beat the model -> red. Intensity scales with magnitude,
    capped at `scale` -- deltas beyond that saturate rather than clip abruptly."""
    if delta is None:
        return ""
    intensity = min(abs(delta) / scale, 1.0)
    alpha = 0.10 + intensity * 0.55
    rgb = "26,127,55" if delta < 0 else "192,57,43"
    return f"background:rgba({rgb},{alpha:.2f});"


def money(v, decimals=2):
    """Signed dollar figure. Rounds first so a tiny negative never renders as '-$0.00'."""
    r = round(v, decimals)
    return f"{'+' if r >= 0 else '-'}${abs(r):,.{decimals}f}"


def summary_row_html(label, s, wallet_return_pct=None, bold=False, ll_title=None, banks=None):
    win_pct = f"{s['wins'] / s['bets_placed']:.0%}" if s["bets_placed"] else "-"
    profit_color = "#1a7f37" if s["profit"] >= 0 else "#c0392b"
    return_str = f"{s['return_pct']:+.1%}" if s["return_pct"] is not None else "-"
    model_ll = f"{s['model_logloss']:.3f}" if s["model_logloss"] is not None else "-"
    market_ll = f"{s['market_logloss']:.3f}" if s["market_logloss"] is not None else "-"
    delta = (s["model_logloss"] - s["market_logloss"]) if (
        s["model_logloss"] is not None and s["market_logloss"] is not None) else None
    delta_str = f"{delta:+.3f}" if delta is not None else "-"
    if wallet_return_pct is not None:
        wr_color = "#1a7f37" if wallet_return_pct >= 0 else "#c0392b"
        wallet_cell = f"<td><span style=\"color:{wr_color}; font-weight:600;\">{wallet_return_pct:+.1%}</span></td>"
    else:
        wallet_cell = "<td>-</td>"
    bank_cells = f"<td>${banks[0]:,.2f}</td><td>${banks[1]:,.2f}</td>" if banks else ""
    style = "font-weight:700; border-top:2px solid #ccc;" if bold else ""
    title = f" title=\"{ll_title}\"" if ll_title else ""
    return (
        f"<tr style=\"{style}\">"
        f"<td>{label}</td><td>{s['bets_placed']}</td>"
        f"<td>{s['wins']}-{s['losses']} ({win_pct})</td>"
        f"<td>${s['wagered']:,.2f}</td>"
        f"<td><span style=\"color:{profit_color}; font-weight:600;\">{money(s['profit'])}</span></td>"
        f"<td>{return_str}</td>"
        f"{bank_cells}"
        f"{wallet_cell}"
        f"<td{title}>{model_ll}</td><td{title}>{market_ll}</td>"
        f"<td style=\"{logloss_delta_bg(delta)}\">{delta_str}</td>"
        "</tr>"
    )


def summary_table_html(header_label, rows_html, bank_cols=False):
    bank_heads = "<th>Start Bank</th><th>End Bank</th>" if bank_cols else ""
    return (
        f"<div class=\"scroll-wrap\"><table><tr><th>{header_label}</th><th>Bets</th><th>Record</th>"
        "<th>Wagered</th><th>Profit</th><th>Return</th>"
        f"{bank_heads}"
        "<th>Wallet Return</th>"
        "<th>LogLoss</th><th>Mkt LogLoss</th><th>LL &Delta;</th></tr>"
        f"{rows_html}</table></div>"
    )


def _nice_axis(lo, hi, min_span, n_ticks=5):
    """Expand [lo, hi] to at least min_span, then snap to round tick values (1/2/5 x 10^k steps).
    Returns (axis_lo, axis_hi, ticks, step)."""
    if hi - lo < min_span:
        mid = (hi + lo) / 2
        lo, hi = mid - min_span / 2, mid + min_span / 2
    raw = (hi - lo) / (n_ticks - 1)
    mag = 10 ** math.floor(math.log10(raw))
    step = next(m * mag for m in (1, 2, 5, 10) if m * mag >= raw)
    a_lo = math.floor(lo / step + 1e-9) * step
    a_hi = math.ceil(hi / step - 1e-9) * step
    ticks, t = [], a_lo
    while t <= a_hi + step * 1e-6:
        ticks.append(round(t, 10))
        t += step
    return a_lo, a_hi, ticks, step


def render_wallet_chart_svg(bank_days, ll_delta_series, start_bank, season_start, season_end, width=760):
    """Self-contained inline SVG (no chart library / CDN, so the page stays static on GitHub Pages).

    Two stacked panels sharing one x axis that spans the FULL season (first to last fixture); lines and
    bars stop at the last completed match day.
      Top panel    -- in-year wallet $ (left axis, green; starts at the season's starting bankroll) and
                      cumulative average LogLoss delta (right axis, purple). The dashed purple line is the
                      LL-delta zero line: below it the model has beaten the market, above it the market has
                      beaten the model.
      Bottom panel -- each day's total settled $ result (green up / red down).
    bank_days: [(date, bank_at_end_of_day, result_dollars)]; ll_delta_series: [(date, cumulative avg delta)]."""
    if not bank_days or season_start is None:
        return "<p class=\"meta\">No completed matches yet this season.</p>"

    left, right = 64, 64
    plot_w = width - left - right
    p1_top, p1_h = 34, 210
    p2_top, p2_h = p1_top + p1_h + 34, 84
    x_label_y = p2_top + p2_h + 20
    height = x_label_y + 10
    dmin = season_start
    dmax = max(season_end or bank_days[-1][0], bank_days[-1][0])
    dspan = max(1, (dmax - dmin).days)

    def x_of(dt):
        return left + (dt - dmin).days / dspan * plot_w

    def y_mapper(top, h, lo, hi):
        return lambda v: top + (1 - (v - lo) / (hi - lo)) * h

    GRID, AXIS = "#ececec", "#b5b5b5"
    svg = [f'<svg viewBox="0 0 {width} {height}" xmlns="http://www.w3.org/2000/svg" '
           f'style="width:100%; height:auto; background:white; border-radius:8px;" '
           f'font-family="-apple-system, sans-serif">']

    # ---- x ticks: weekly from the first fixture, shared by both panels; a full EPL season is ~40 weeks, so
    # label every few weeks to keep the labels from running together ----
    n_weeks = dspan // 7
    label_every = max(1, math.ceil(n_weeks / 13))
    week_dates = [dmin + timedelta(days=7 * k) for k in range(n_weeks + 1)]
    for top, h in ((p1_top, p1_h), (p2_top, p2_h)):
        for dt in week_dates:
            svg.append(f'<line x1="{x_of(dt):.1f}" x2="{x_of(dt):.1f}" y1="{top}" y2="{top + h}" '
                       f'stroke="{GRID}" stroke-width="1" />')
        svg.append(f'<line x1="{left}" x2="{left + plot_w}" y1="{top + h}" y2="{top + h}" stroke="{AXIS}" />')
        for dt in week_dates:
            svg.append(f'<line x1="{x_of(dt):.1f}" x2="{x_of(dt):.1f}" y1="{top + h}" y2="{top + h + 4}" '
                       f'stroke="{AXIS}" />')
    for k, dt in enumerate(week_dates):
        if k % label_every == 0:
            svg.append(f'<text x="{x_of(dt):.1f}" y="{x_label_y}" font-size="9" fill="#888" '
                       f'text-anchor="middle">{dt.month}/{dt.day}</text>')

    # ---- top panel: wallet (left) ----
    wallet_vals = [start_bank] + [b for _, b, _ in bank_days]
    w_lo, w_hi, w_ticks, _ = _nice_axis(min(wallet_vals), max(wallet_vals), min_span=start_bank * 0.10)
    y_w = y_mapper(p1_top, p1_h, w_lo, w_hi)
    svg.append(f'<line x1="{left}" x2="{left}" y1="{p1_top}" y2="{p1_top + p1_h}" stroke="{AXIS}" />')
    for v in w_ticks:
        y = y_w(v)
        svg.append(f'<line x1="{left}" x2="{left + plot_w}" y1="{y:.1f}" y2="{y:.1f}" stroke="{GRID}" />')
        svg.append(f'<line x1="{left - 4}" x2="{left}" y1="{y:.1f}" y2="{y:.1f}" stroke="#1a7f37" />')
        svg.append(f'<text x="{left - 8}" y="{y:.1f}" font-size="10" fill="#1a7f37" text-anchor="end" '
                   f'dominant-baseline="middle">${v:,.0f}</text>')

    # ---- top panel: LL delta (right) ----
    delta_vals = [v for _, v in ll_delta_series] + [0.0]
    d_lo, d_hi, d_ticks, d_step = _nice_axis(min(delta_vals), max(delta_vals), min_span=0.02)
    y_d = y_mapper(p1_top, p1_h, d_lo, d_hi)
    decimals = 3 if d_step >= 0.001 else 4
    svg.append(f'<line x1="{left + plot_w}" x2="{left + plot_w}" y1="{p1_top}" y2="{p1_top + p1_h}" '
               f'stroke="{AXIS}" />')
    for v in d_ticks:
        y = y_d(v)
        is_zero = abs(v) < d_step * 1e-6
        svg.append(f'<line x1="{left + plot_w}" x2="{left + plot_w + 4}" y1="{y:.1f}" y2="{y:.1f}" '
                   f'stroke="#6a3fb5" />')
        label = f"{0:.{decimals}f}" if is_zero else f"{v:+.{decimals}f}"
        weight = ' font-weight="700"' if is_zero else ""
        svg.append(f'<text x="{left + plot_w + 8}" y="{y:.1f}" font-size="10" fill="#6a3fb5"{weight} '
                   f'text-anchor="start" dominant-baseline="middle">{label}</text>')
    svg.append(f'<line x1="{left}" x2="{left + plot_w}" y1="{y_d(0):.1f}" y2="{y_d(0):.1f}" '
               f'stroke="#9d86d4" stroke-width="1.2" stroke-dasharray="5,4" />')

    # lines stop at the last completed match day
    wallet_pts = [(season_start, start_bank)] + [(d, b) for d, b, _ in bank_days]
    svg.append('<polyline points="' + " ".join(f"{x_of(d):.1f},{y_w(v):.1f}" for d, v in wallet_pts) +
               '" fill="none" stroke="#1a7f37" stroke-width="2" />')
    if ll_delta_series:
        svg.append('<polyline points="' + " ".join(f"{x_of(d):.1f},{y_d(v):.1f}" for d, v in ll_delta_series) +
                   '" fill="none" stroke="#6a3fb5" stroke-width="2" />')

    # ---- bottom panel: daily result bars ----
    results = [r for _, _, r in bank_days]
    b_lo, b_hi, b_ticks, _ = _nice_axis(min(results + [0.0]), max(results + [0.0]),
                                        min_span=max(start_bank * 0.02, 1.0), n_ticks=4)
    y_b = y_mapper(p2_top, p2_h, b_lo, b_hi)
    svg.append(f'<line x1="{left}" x2="{left}" y1="{p2_top}" y2="{p2_top + p2_h}" stroke="{AXIS}" />')
    for v in b_ticks:
        y = y_b(v)
        svg.append(f'<line x1="{left}" x2="{left + plot_w}" y1="{y:.1f}" y2="{y:.1f}" stroke="{GRID}" />')
        svg.append(f'<line x1="{left - 4}" x2="{left}" y1="{y:.1f}" y2="{y:.1f}" stroke="#555" />')
        svg.append(f'<text x="{left - 8}" y="{y:.1f}" font-size="10" fill="#555" text-anchor="end" '
                   f'dominant-baseline="middle">{"$0" if abs(v) < 1e-9 else money(v, 0)}</text>')
    svg.append(f'<line x1="{left}" x2="{left + plot_w}" y1="{y_b(0):.1f}" y2="{y_b(0):.1f}" stroke="#888" />')
    bar_w = max(3.0, min(14.0, plot_w / dspan * 0.7))
    for d, _, r in bank_days:
        if abs(r) < 0.5:
            continue
        y0, y1 = y_b(0), y_b(r)
        svg.append(f'<rect x="{x_of(d) - bar_w / 2:.1f}" y="{min(y0, y1):.1f}" width="{bar_w:.1f}" '
                   f'height="{max(abs(y1 - y0), 1):.1f}" fill="{"#1a7f37" if r >= 0 else "#c0392b"}" />')
    svg.append(f'<text x="{left + 6}" y="{p2_top - 8}" font-size="11" fill="#444">Daily result ($)</text>')

    # ---- legend ----
    svg.append(
        '<g font-size="11">'
        f'<rect x="{left}" y="10" width="10" height="10" fill="#1a7f37" />'
        f'<text x="{left + 14}" y="19" fill="#444">Wallet ($, left)</text>'
        f'<rect x="{left + 130}" y="10" width="10" height="10" fill="#6a3fb5" />'
        f'<text x="{left + 144}" y="19" fill="#444">Cumulative avg LL &#916; (right)</text>'
        f'<line x1="{left + 330}" x2="{left + 354}" y1="15" y2="15" stroke="#9d86d4" stroke-width="1.5" '
        f'stroke-dasharray="5,4" />'
        f'<text x="{left + 360}" y="19" fill="#444">LL &#916; = 0 (below = model beats market)</text>'
        '</g>'
    )
    svg.append("</svg>")
    return "".join(svg)


BET_BADGE = {"bet": "BET", "pass": "pass", "too_early": "too early",
             "awaiting_closing": "awaiting lineup odds",
             "outside_prob_band": "outside betting band",
             "no_odds": "no odds yet"}
BET_BADGE_COLOR = {"bet": "#1a7f37", "pass": "#666", "too_early": "#999",
                   "awaiting_closing": "#b8860b",
                   "outside_prob_band": "#999",
                   "no_odds": "#999"}


def lineup_check_cell(msg):
    """Empty message means 11 starters posted and every starter has a rating; otherwise show what's wrong."""
    if not msg:
        return "<td style=\"color:#1a7f37;\">11/11</td>"
    if msg == "No lineup posted yet":
        return "<td class=\"tag\">no lineup yet</td>"
    return f"<td style=\"color:#c0392b; font-weight:600;\">{msg}</td>"


def render_next_matchweek(upcoming):
    """upcoming: [{match_date, home_code, away_code, home: evaluation, away: evaluation}], in kickoff order.
    Two rows per game, home row first. A dashed rule separates the two teams in a game (first row =
    team-top); a thick rule closes out the game (second row = game-end)."""
    if not upcoming:
        return "<p class=\"meta\">No upcoming matches.</p>"
    html = ""
    for g in upcoming:
        matchup = f"{g['away_code'].upper()} @ {g['home_code'].upper()}"
        for is_home in (True, False):
            ev = g["home"] if is_home else g["away"]
            team = g["home_code"] if is_home else g["away_code"]
            row_cls = "team-top" if is_home else "game-end"
            head = (f"<tr class=\"{row_cls}\"><td>{g['match_date']}</td><td>{matchup}</td>"
                    f"<td>{team.upper()} {'(H)' if is_home else '(A)'}</td>")
            lineup_td = lineup_check_cell(ev.get("lineup_check"))
            status = ev["status"]
            if status == "no_prediction":
                html += (head + "<td colspan=\"5\" class=\"tag\">no lineup yet -- no signal</td>"
                         + lineup_td + "</tr>")
                continue
            mpct = float(ev["model_prob"])
            ipct = float(ev["implied_prob"]) if ev.get("implied_prob") is not None else None
            if ipct is not None:
                market_str = f"{ipct:.1%}"
                if ev.get("line_type"):
                    market_str += f" <span class=\"tag\">({ev['line_type']})</span>"
                delta = mpct - ipct
                delta_color = "#1a7f37" if delta > 0 else ("#c0392b" if delta < 0 else "#666")
                delta_cell = f"<td style=\"color:{delta_color}; font-weight:600;\">{delta:+.1%}</td>"
            else:
                market_str = "-"
                delta_cell = "<td>-</td>"
            stake_str = f"${ev['stake']:,.2f}" if status == "bet" else "-"
            html += (
                head
                + f"<td><span style=\"color:{BET_BADGE_COLOR[status]}; font-weight:600;\">{BET_BADGE[status]}</span></td>"
                + f"<td>{stake_str}</td>"
                + f"<td>{mpct:.1%}</td>"
                + f"<td>{market_str}</td>"
                + delta_cell
                + lineup_td
                + "</tr>"
            )
    return (
        "<div class=\"scroll-wrap\"><table class=\"upcoming\"><tr><th>Date</th><th>Matchup (Away @ Home)</th>"
        "<th>Team</th><th>Bet?</th><th>$ Amount</th><th>Model %</th><th>Market %</th><th>Delta</th>"
        "<th>Lineup check</th></tr>"
        + html + "</table></div>"
    )


def team_label(code, model_pct, mktpct, fired):
    """Bold if the model's own win% for this side beats the market's implied win% for this side
    (model_pct > mktpct) -- NOT a >50% threshold. Bold+blue if a bet actually fired on this side. Since
    each side's comparison is independent (mktpct for both sides need not sum to 1, thanks to vig), it's
    possible for both sides, one, or neither to be bold."""
    if fired:
        style = "font-weight:700; color:#1450c9;"
    elif model_pct is not None and mktpct is not None and model_pct > mktpct:
        style = "font-weight:700;"
    else:
        style = ""
    return f"<span style=\"{style}\">{code.upper()}</span>"


def game_row_html(pair):
    away, home = pair["away"], pair["home"]
    away_label = team_label(away["team"], away["model_pct"], away["mlpct"], away["bets_fire"])
    home_label = team_label(home["team"], home["model_pct"], home["mlpct"], home["bets_fire"])
    a_mpct = f"{away['model_pct']:.1%}" if away["model_pct"] is not None else "-"
    h_mpct = f"{home['model_pct']:.1%}" if home["model_pct"] is not None else "-"
    a_mktpct = f"{away['mlpct']:.1%}" if away["mlpct"] is not None else "-"
    h_mktpct = f"{home['mlpct']:.1%}" if home["mlpct"] is not None else "-"
    if away["away_goals"] is not None and away["home_goals"] is not None:
        score = f"{away['away_goals']}-{away['home_goals']}"
    else:
        score = "-"
    wagers = []
    for side in (away, home):
        if side["bets_fire"]:
            if side["bet_outcome"] == "win":
                result_str, result_color = "WON", "#1a7f37"
            elif side["bet_outcome"] == "loss":
                result_str, result_color = "lost", "#c0392b"
            else:
                result_str, result_color = "pending", "#b8860b"  # placed but not yet settled
            wagers.append(f"{side['team'].upper()} ${side['stake_dollar']:,.2f} &mdash; "
                          f"<span style=\"color:{result_color}; font-weight:600;\">{result_str}</span>")
    wager_html = "; ".join(wagers) if wagers else "<span class=\"tag\">-</span>"
    return (
        "<tr>"
        f"<td>{pair['date']}</td>"
        f"<td>{away_label} @ {home_label}</td>"
        f"<td>{a_mpct} / {h_mpct}</td>"
        f"<td>{a_mktpct} / {h_mktpct}</td>"
        f"<td>{score}</td>"
        f"<td>{wager_html}</td>"
        "</tr>"
    )


def games_table_html(pairs):
    if not pairs:
        return "<p class=\"meta\">None.</p>"
    rows = "".join(game_row_html(p) for p in pairs)
    return (
        "<div class=\"scroll-wrap\">"
        "<table class=\"detail\"><tr><th>Date</th><th>Matchup (Away @ Home)</th>"
        "<th>Model % (A/H)</th><th>Market % (A/H)</th><th>Score (A-H)</th><th>Wager &amp; Result</th></tr>"
        f"{rows}</table></div>"
    )


ROW_GRID_COLS = "1.3fr 0.6fr 0.9fr 0.9fr 0.8fr 0.7fr 0.6fr 0.6fr 0.6fr 0.6fr 0.6fr 0.6fr 0.6fr 0.6fr"


def group_header_row():
    """The shared, non-collapsible two-tier header sitting above a matchweek/team block list -- printed once,
    with each block's own summary row (see render_group_block) using the same grid so it lines up."""
    return (
        f"<div class=\"row-grid group-head\" style=\"grid-template-columns:{ROW_GRID_COLS};\">"
        "<div></div><div></div><div></div><div></div><div></div><div></div>"
        "<div class=\"group-label\" style=\"grid-column: span 2;\">Picks Correct</div>"
        "<div class=\"group-label\" style=\"grid-column: span 3;\">LogLoss &mdash; Bets Placed</div>"
        "<div class=\"group-label\" style=\"grid-column: span 3;\">LogLoss &mdash; All Games</div>"
        "</div>"
        f"<div class=\"row-grid col-head\" style=\"grid-template-columns:{ROW_GRID_COLS};\">"
        "<div></div><div>Bets</div><div>Record</div><div>Wagered</div><div>Profit</div><div>Return</div>"
        "<div>Model</div><div>Market</div>"
        "<div>Model</div><div>Market</div><div>&Delta;</div>"
        "<div>Model</div><div>Market</div><div>&Delta;</div>"
        "</div>"
    )


def render_group_block(label, instances, pairs):
    s = aggregate(instances)
    record = f"{s['wins']}-{s['losses']}"
    profit_color = "#1a7f37" if s["profit"] >= 0 else "#c0392b"
    return_str = f"{s['return_pct']:+.1%}" if s["return_pct"] is not None else "-"

    def ll_cells(ll, vll):
        delta = (ll - vll) if (ll is not None and vll is not None) else None
        ll_str = f"{ll:.3f}" if ll is not None else "-"
        vll_str = f"{vll:.3f}" if vll is not None else "-"
        delta_str = f"{delta:+.3f}" if delta is not None else "-"
        return (
            f"<div>{ll_str}</div>"
            f"<div>{vll_str}</div>"
            f"<div style=\"{logloss_delta_bg(delta)}\">{delta_str}</div>"
        )

    mc, me, kc, ke = pick_accuracy(pairs)
    model_picks = f"{mc}/{me}" if me else "-"
    market_picks = f"{kc}/{ke}" if ke else "-"

    summary_row = (
        f"<div class=\"row-grid\" style=\"grid-template-columns:{ROW_GRID_COLS};\">"
        f"<div class=\"block-title\">{label}</div>"
        f"<div>{s['bets_placed']}</div><div>{record}</div>"
        f"<div>${s['wagered']:,.2f}</div>"
        f"<div style=\"color:{profit_color}; font-weight:600;\">{money(s['profit'])}</div>"
        f"<div>{return_str}</div>"
        f"<div>{model_picks}</div><div>{market_picks}</div>"
        f"{ll_cells(s['bet_model_logloss'], s['bet_market_logloss'])}"
        f"{ll_cells(s['all_model_logloss'], s['all_market_logloss'])}"
        "</div>"
    )

    bet_pairs = [p for p in pairs if p["away"]["bets_fire"] or p["home"]["bets_fire"]]
    return (
        "<details class=\"block\"><summary>" + summary_row + "</summary>"
        f"<details class=\"nested\"><summary>Bets placed ({len(bet_pairs)})</summary>"
        f"{games_table_html(bet_pairs)}</details>"
        f"<details class=\"nested\"><summary>All games ({len(pairs)})</summary>"
        f"{games_table_html(pairs)}</details>"
        "</details>"
    )


def render_nested_drilldown(groups_sorted, label_fn, all_pairs_by_game_id):
    """groups_sorted: list of (key, instances) already in display order. all_pairs_by_game_id maps
    game_id -> pair (see pair_by_game), used to pull each group's games without re-pairing repeatedly."""
    if not groups_sorted:
        return ""
    blocks = ""
    for key, instances in groups_sorted:
        game_ids_in_group = {x["game_id"] for x in instances}
        pairs = sorted((all_pairs_by_game_id[gid] for gid in game_ids_in_group if gid in all_pairs_by_game_id),
                       key=_pair_key)
        blocks += render_group_block(label_fn(key), instances, pairs)
    return f"<div class=\"scroll-wrap\">{group_header_row()}{blocks}</div>"


def render_attention_callout(missing_xg_count, missing_ratings_count, headcount_issues_count):
    """One box for everything that needs a manual fix. Red if any team-match has a bad starter count (an
    ESPN API/parsing problem, a different class from the 'known, needs manual entry' items); amber otherwise."""
    items = []
    if missing_xg_count > 0:
        items.append(f"{missing_xg_count} completed match{'es' if missing_xg_count != 1 else ''} "
                     f"missing xG &mdash; run <code>enter_xg.py</code>")
    if missing_ratings_count > 0:
        items.append(f"{missing_ratings_count} player{'s' if missing_ratings_count != 1 else ''} "
                     f"missing a {SEASON} rating &mdash; run <code>add_player_ratings.py</code>")
    if headcount_issues_count > 0:
        items.append(f"{headcount_issues_count} team-match{'es' if headcount_issues_count != 1 else ''} "
                     f"showing a starter count &ne; 11 &mdash; likely an ESPN parsing issue, "
                     f"check <code>poll_espn_lineups.py</code> logs")
    if not items:
        return ""
    border, bg = ("#dc3545", "#f8d7da") if headcount_issues_count > 0 else ("#b7791f", "#fff8e6")
    return (f"<div style=\"border:1px solid {border}; background:{bg}; padding:8px 12px; margin:10px 0; "
            f"border-radius:4px;\"><b>Needs attention:</b><br>" + "<br>".join(items) + "</div>")


def season_display(season):
    s = str(season)
    return f"{s[:2]}-{s[2:]}"


def render_html(bankroll, model_version, upcoming, next_mw, detail_season, detail_summary, detail_wallet_pct,
                chart_svg, last_mw, last_instances, week_groups, team_groups, pairs_by_game_id,
                season_summaries, callout_html):
    disp = season_display(detail_season)
    next_label = f" (Matchweek {next_mw})" if next_mw is not None else ""
    last_label = f" (Matchweek {last_mw})" if last_mw is not None else ""

    none_yet = "<p class=\"meta\">No completed matches yet this season.</p>"
    week_html = render_nested_drilldown(week_groups, lambda k: f"Matchweek {k}", pairs_by_game_id) or none_yet
    team_html = render_nested_drilldown(team_groups, lambda k: k.upper(), pairs_by_game_id) or none_yet
    if last_mw is not None and last_instances:
        last_html = render_nested_drilldown([(last_mw, last_instances)], lambda k: f"Matchweek {k}",
                                            pairs_by_game_id)
    else:
        last_html = "<p class=\"meta\">No completed matchweeks yet.</p>"

    detail_row_html = summary_row_html(disp, detail_summary, wallet_return_pct=detail_wallet_pct)

    # Past seasons: chained season_backtests rows (see chain_backtest_seasons). Wallet Return per season =
    # profit / start bank; the Total's Wallet Return is the cumulative return on the original start bank.
    season_rows_html = ""
    tot = {"bets_placed": 0, "wins": 0, "losses": 0, "wagered": 0.0, "profit": 0.0}
    ll_sum = ll_n = vll_sum = vll_n = 0
    for s in season_summaries:
        season_rows_html += summary_row_html(season_display(s["season"]), s,
                                             wallet_return_pct=s["profit"] / s["start_bank"],
                                             banks=(s["start_bank"], s["end_bank"]))
        for k in tot:
            tot[k] += s[k]
        if s["model_logloss"] is not None:
            ll_sum += s["model_logloss"]; ll_n += 1
        if s["market_logloss"] is not None:
            vll_sum += s["market_logloss"]; vll_n += 1
    if season_summaries:
        # LogLoss/Mkt LogLoss totals are an unweighted mean across seasons (season_backtests doesn't store
        # a per-season evaluated-instance count to weight by) -- reasonable here since every EPL season
        # has ~760 evaluated instances, but flagged via the title tooltip.
        tot["model_logloss"] = ll_sum / ll_n if ll_n else None
        tot["market_logloss"] = vll_sum / vll_n if vll_n else None
        tot["return_pct"] = (tot["profit"] / tot["wagered"]) if tot["wagered"] else None
        first_bank, last_bank = season_summaries[0]["start_bank"], season_summaries[-1]["end_bank"]
        total_wr = (last_bank - first_bank) / first_bank
        season_rows_html += summary_row_html("Total", tot, wallet_return_pct=total_wr, bold=True,
                                             ll_title="Unweighted mean across seasons",
                                             banks=(first_bank, last_bank))

    html = (
        "<!DOCTYPE html><html><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
        "<title>EPL Model Dashboard</title>"
        f"<style>{STYLE}</style></head><body>"
        "<h1>EPL Model Dashboard</h1>"
        f"<div class=\"meta\">Bankroll: ${bankroll:,.2f} &middot; "
        f"Model version fit {model_version[1].strftime('%Y-%m-%d')} &middot; "
        f"Updated {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}</div>"
        f"{callout_html}"
        f"<h2>Next matchweek{next_label}</h2>"
        f"{render_next_matchweek(upcoming)}"
        f"<h2>{disp} at a glance</h2>"
        f"{summary_table_html('Season', detail_row_html)}"
        f"<h2>{disp} &mdash; wallet &amp; LogLoss over time</h2>"
        f"{chart_svg}"
        f"<h2>Last matchweek{last_label}</h2>"
        f"{last_html}"
        f"<h2>{disp} &mdash; by matchweek</h2>"
        f"{week_html}"
        f"<h2>{disp} &mdash; by team</h2>"
        f"{team_html}"
        "<h2>Past seasons</h2>"
        "<div class=\"meta\">Walk-forward backtest. "
        f"${CHAIN_START_BANKROLL:,.2f} invested at the start of the first season, each season's ending "
        "bank carried into the next (compounding). Wagered and Profit are on that growing bank.</div>"
        f"{summary_table_html('Season', season_rows_html, bank_cols=True)}"
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

    next_mw, _ = get_matchweek_context(engine, SEASON)
    print(f"Next matchweek: {next_mw}")

    matches = get_candidate_matches(engine, next_mw)
    print(f"Found {len(matches)} candidate matches (next matchweek, today, or a posted lineup).")

    starter_counts = get_starter_counts(engine, SEASON)
    starters_missing_ratings = get_starters_missing_ratings(engine, SEASON)

    upcoming = []
    with engine.begin() as conn:
        for m in matches:
            match_id, match_date, kickoff, matchweek, home_code, away_code, home_id, away_id = m
            odds = get_bet_odds(engine, match_id)
            home_ml, away_ml, line_type = odds if odds else (None, None, None)
            game = {"match_id": match_id, "match_date": match_date, "matchweek": matchweek,
                    "home_code": home_code, "away_code": away_code}

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
                game["home" if is_home else "away"] = evaluation

                if evaluation["status"] in ("bet", "pass", "outside_prob_band"):
                    record_bet(conn, match_id, team_id, evaluation)
            upcoming.append(game)

    # ---- detail season: at-a-glance row, chart, last matchweek, matchweek + team drilldowns ----
    detail_season = get_detail_season(engine)
    print(f"Detail season: {detail_season}")
    detail_instances = get_completed_match_results(engine, coefs, spline_config, detail_season)
    print(f"Found {len(detail_instances)} completed match/team instances for {detail_season}.")

    pairs_by_game_id = {p["game_id"]: p for p in pair_by_game(detail_instances)}
    week_groups = sorted(group_by(detail_instances, lambda x: x["matchweek"] or 0).items(),
                         key=lambda kv: kv[0], reverse=True)
    team_groups = sorted(group_by(detail_instances, lambda x: x["team"]).items(), key=lambda kv: kv[0])
    detail_summary = aggregate(detail_instances)

    _, last_mw = get_matchweek_context(engine, detail_season)
    last_instances = [x for x in detail_instances if x["matchweek"] == last_mw] if last_mw is not None else []

    start_bank = season_start_bankroll(detail_season)
    bank_days = build_bank_days(detail_instances, start_bank)
    detail_wallet_pct = ((bank_days[-1][1] - start_bank) / start_bank) if bank_days else 0.0
    season_first, season_last = get_season_bounds(engine, detail_season)
    chart_svg = render_wallet_chart_svg(bank_days, cumulative_avg_ll_delta_series(detail_instances),
                                        start_bank, season_first, season_last)

    season_summaries = get_season_summaries(engine)
    print(f"Found {len(season_summaries)} backtested seasons.")

    missing_xg_count = get_missing_xg_count(engine)
    print(f"Missing xG: {missing_xg_count} completed matches.")

    missing_ratings_count = get_missing_ratings_count(engine)
    print(f"Missing player ratings: {missing_ratings_count} players.")

    headcount_issues_count = get_headcount_issues_count(engine)
    print(f"Headcount issues: {headcount_issues_count} team-matches with starters != 11.")

    callout_html = render_attention_callout(missing_xg_count, missing_ratings_count, headcount_issues_count)

    os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)
    with open(OUTPUT_PATH, "w") as f:
        f.write(render_html(bankroll, version, upcoming, next_mw, detail_season, detail_summary,
                            detail_wallet_pct, chart_svg, last_mw, last_instances, week_groups, team_groups,
                            pairs_by_game_id, season_summaries, callout_html))
    print(f"Dashboard written to {OUTPUT_PATH}")
