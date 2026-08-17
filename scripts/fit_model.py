"""
EPL model: fit the win-probability logistic regression.

Manually run -- NOT scheduled. Confirmed with the user this should only
be refit at season boundaries (post-season, occasionally mid-season by
their own judgment), never automatically: the training set is always
"every FULLY COMPLETED season," so refitting mid-season without that
boundary changing would just reproduce identical coefficients -- there's
no new completed-season data for it to learn from. Run this yourself,
whenever you've decided it's time.

Replicates the R script's actual approach (glm(family="binomial") only --
the lm() linear probability model was fit in R but never actually used,
so it's not ported here) with two changes:
  - Team fixed effects are now a proper categorical variable (C(team_code)
    in the formula) instead of a hardcoded list of dummy columns that
    needed manual editing every season as teams got promoted/relegated.
  - Every fit's coefficients are STORED (model_versions / model_coefficients
    tables), not just printed -- this is what makes "track model
    performance over time" (stated as core to this project) possible.

SETUP:
    pip install pandas statsmodels sqlalchemy psycopg2-binary --break-system-packages

Set DATABASE_URL the same way as the other scripts.
"""

import os

import pandas as pd
import statsmodels.api as sm
import statsmodels.formula.api as smf
from sqlalchemy import create_engine, text

DATABASE_URL = os.environ["DATABASE_URL"]
engine = create_engine(DATABASE_URL, pool_pre_ping=True, pool_recycle=280)

FORMULA = (
    "win ~ avgatt + xgfpgd + xgapgd + gkpgd + possd + strtd + bertd + stmsd + formcd "
    "+ C(team_code)"
)

NOTES = input("Optional note for this model version (e.g. 'post 2025-26 season refit'), or press Enter to skip: ").strip() or None


def get_completed_seasons(engine):
    with engine.connect() as conn:
        rows = conn.execute(
            text("select season from matches group by season having bool_and(status = 'completed') order by season")
        ).fetchall()
    return [r[0] for r in rows]


def load_training_data(engine):
    """Queries one season at a time rather than all seasons in a single
    query -- v_match_model_features does real per-row computation (live
    is_new_to_team lookups, rolling windows), not a read of pre-computed
    values, so pulling ~5,300 rows across 7 seasons in one shot was
    hitting Supabase's statement timeout. Each season individually is a
    much lighter query."""
    seasons = get_completed_seasons(engine)
    print(f"Fully completed seasons found: {seasons}")

    query = text("""
        select
            t.code as team_code,
            m.season,
            case when v.is_home then (m.home_goals > m.away_goals)
                 else (m.away_goals > m.home_goals) end as win,
            v.avgatt, v.xgfpgd, v.xgapgd, v.gkpgd, v.possd, v.strtd, v.bertd, v.stmsd, v.formcd
        from v_match_model_features v
        join matches m on m.id = v.match_id
        join teams t on t.id = v.team_id
        where m.season = :season
    """)

    frames = []
    with engine.connect() as conn:
        for season in seasons:
            print(f"  loading season {season}...", flush=True)
            df_season = pd.read_sql(query, conn, params={"season": season})
            print(f"    {len(df_season)} rows", flush=True)
            frames.append(df_season)
    return pd.concat(frames, ignore_index=True)


def store_model(engine, seasons_included, n_obs, result, notes):
    with engine.begin() as conn:
        version_id = conn.execute(
            text("""
                insert into model_versions (seasons_included, n_observations, notes)
                values (:seasons, :n_obs, :notes)
                returning id
            """),
            {"seasons": seasons_included, "n_obs": n_obs, "notes": notes},
        ).fetchone()[0]

        rows = []
        for feature_name in result.params.index:
            rows.append({
                "model_version_id": version_id,
                "feature_name": feature_name,
                "coefficient": float(result.params[feature_name]),
                "std_error": float(result.bse[feature_name]),
                "p_value": float(result.pvalues[feature_name]),
            })
        conn.execute(
            text("""
                insert into model_coefficients (model_version_id, feature_name, coefficient, std_error, p_value)
                values (:model_version_id, :feature_name, :coefficient, :std_error, :p_value)
            """),
            rows,
        )
    return version_id


if __name__ == "__main__":
    print("Loading training data (every fully completed season)...")
    df = load_training_data(engine)
    seasons_included = sorted(df["season"].unique().tolist())
    print(f"Seasons included: {seasons_included}")
    print(f"Raw rows: {len(df)}")

    before = len(df)
    df = df.dropna()
    dropped = before - len(df)
    if dropped:
        print(f"Dropped {dropped} rows with missing feature values ({dropped/before:.1%}).")
    print(f"Final training rows: {len(df)}")

    df["win"] = df["win"].astype(int)

    print("\nFitting logistic regression (statsmodels GLM, binomial family)...")
    result = smf.glm(formula=FORMULA, data=df, family=sm.families.Binomial()).fit()
    print(result.summary())

    version_id = store_model(engine, seasons_included, len(df), result, NOTES)
    print(f"\nStored as model_versions.id = {version_id}")
    print("This is now the latest model version -- predict_matches.py (next step) will use it.")
