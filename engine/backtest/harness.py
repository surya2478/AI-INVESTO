"""Decile backtest for the G.E.M. score.

READ THE BIASES BEFORE THE RESULT
---------------------------------
This harness is honest about being weaker than the spec demanded, because the
data cannot support what the spec assumed. Three limits apply to every number it
produces, and none is a bug to be fixed later by more code:

1. RESTATEMENT IN PLACE. Yahoo reports the current value of each figure. If a
   company revised FY2023 in FY2025, the backtest sees the revised number when
   ranking in 2023. Real point-in-time data would not. This inflates the score's
   apparent skill and cannot be corrected from this source.

2. SURVIVORSHIP. The universe is today's NIFTY TOTAL MARKET. Companies that
   failed and delisted are simply absent, so the sample is drawn from survivors.
   This inflates returns for every decile, though it biases the top decile less
   than the bottom.

3. FILING LAG IS ASSUMED, NOT KNOWN. Indian rules require annual results within
   60 days of the year end; `AVAILABILITY_LAG_DAYS` uses 90 to stay on the safe
   side. Being late is conservative -- it delays acting on information -- while
   being early would manufacture an edge.

And the binding constraint: Yahoo carries 4-5 annual periods, so there are only
about three usable rebalance dates. Three observations cannot establish that a
ranking works. What follows is a smoke test of the machinery, not evidence.
"""

from __future__ import annotations

import datetime as dt
import logging

import numpy as np
import pandas as pd

from engine.scoring import gem
from engine.storage import db

log = logging.getLogger(__name__)

# Conservative: Indian annual results are due within 60 days of the year end.
AVAILABILITY_LAG_DAYS = 90
DECILES = 10


def forward_return(con, security_ids: list[int], start: dt.date, months: int) -> pd.DataFrame:
    """Total return from `start` to `start + months`, per security."""
    end = start + dt.timedelta(days=int(months * 30.44))
    if not security_ids:
        return pd.DataFrame()

    placeholders = ",".join("?" * len(security_ids))
    frame = con.execute(f"""
        WITH bounds AS (
            SELECT security_id,
                   min(CASE WHEN date >= ? THEN date END) AS d0,
                   max(CASE WHEN date <= ? THEN date END) AS d1
              FROM ohlcv
             WHERE security_id IN ({placeholders})
             GROUP BY security_id
        )
        SELECT b.security_id,
               p0.adj_close AS px0, p1.adj_close AS px1
          FROM bounds b
          JOIN ohlcv p0 ON p0.security_id = b.security_id AND p0.date = b.d0
          JOIN ohlcv p1 ON p1.security_id = b.security_id AND p1.date = b.d1
    """, [start, end, *security_ids]).df()

    if frame.empty:
        return frame
    frame["fwd_return"] = (frame["px1"] / frame["px0"] - 1.0) * 100.0
    return frame[["security_id", "fwd_return"]]


def rebalance_dates(con, horizon_months: int) -> list[dt.date]:
    """Dates where enough annual data exists and a full forward window follows."""
    bounds = con.execute("""
        SELECT min(period_end), max(period_end) FROM fundamentals_pit
         WHERE period_type = 'A'
    """).fetchone()
    if not bounds or not bounds[0]:
        return []

    last_price = con.execute("SELECT max(date) FROM ohlcv").fetchone()[0]
    earliest = pd.Timestamp(bounds[0]).date() + dt.timedelta(days=AVAILABILITY_LAG_DAYS)
    latest = pd.Timestamp(last_price).date() - dt.timedelta(days=int(horizon_months * 30.44))

    dates, cursor = [], dt.date(earliest.year, 7, 1)
    while cursor <= latest:
        if cursor >= earliest:
            dates.append(cursor)
        cursor = dt.date(cursor.year + 1, 7, 1)
    return dates


def run(con, horizon_months: int = 12, min_names: int = 100) -> dict:
    """Score the universe at each rebalance date and measure forward returns."""
    dates = rebalance_dates(con, horizon_months)
    if not dates:
        return {"error": "no usable rebalance dates"}

    periods = []
    for as_of in dates:
        scored = gem.score_universe(con, as_of=as_of, include_non_pit=True)
        if scored.empty or len(scored) < min_names:
            log.info("skipping %s: only %d names", as_of, len(scored))
            continue

        # Rank only names with a real score and a tradeable price history.
        scored = scored.dropna(subset=["gem_score"]).copy()
        returns = forward_return(con, scored["security_id"].tolist(), as_of, horizon_months)
        merged = scored.merge(returns, on="security_id", how="inner")
        if len(merged) < min_names:
            continue

        merged["decile"] = pd.qcut(merged["gem_score"].rank(method="first"),
                                   DECILES, labels=False) + 1
        periods.append({"as_of": as_of, "frame": merged})

    if not periods:
        return {"error": "no period had enough names to rank"}

    by_decile = []
    for period in periods:
        grouped = (period["frame"].groupby("decile")["fwd_return"]
                   .agg(["mean", "median", "count"]).reset_index())
        grouped["as_of"] = period["as_of"]
        by_decile.append(grouped)

    deciles = pd.concat(by_decile, ignore_index=True)
    summary = (deciles.groupby("decile")[["mean", "median"]].mean().reset_index()
               .rename(columns={"mean": "avg_return", "median": "median_return"}))

    universe_mean = float(pd.concat(p["frame"] for p in periods)["fwd_return"].mean())
    top = float(summary.loc[summary.decile == DECILES, "avg_return"].iloc[0])
    bottom = float(summary.loc[summary.decile == 1, "avg_return"].iloc[0])

    # Spearman correlation between score and forward return, per period. A
    # ranking that works should show a positive rank correlation consistently,
    # not merely a good top decile in one year.
    rank_ic = [
        float(p["frame"]["gem_score"].corr(p["frame"]["fwd_return"], method="spearman"))
        for p in periods
    ]

    return {
        "periods": [p["as_of"] for p in periods],
        "names_per_period": [len(p["frame"]) for p in periods],
        "summary": summary,
        "universe_mean": universe_mean,
        "top_decile": top,
        "bottom_decile": bottom,
        "spread": top - bottom,
        "excess_vs_universe": top - universe_mean,
        "rank_ic": rank_ic,
        "mean_ic": float(np.mean(rank_ic)) if rank_ic else None,
        "horizon_months": horizon_months,
    }
