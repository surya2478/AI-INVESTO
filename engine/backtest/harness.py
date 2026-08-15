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

   `run(include_non_pit=False)` is the switch that excludes those rows, and it
   is not yet usable: the true point-in-time corpus (NSE XBRL and parsed PDFs)
   is QUARTERLY ONLY, and `build_features` ranks on annual periods, so PIT-only
   scoring currently returns nothing at all. Annual PIT ingestion is the
   prerequisite for a backtest whose result means anything. Until then this
   harness runs on restated data BY DECLARATION, not by oversight, and the mode
   it ran in comes back in the result as `pit_only`.

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

FIXED SINCE THE FIRST RUN, and the reason its numbers are void rather than
merely weak: market cap was read from `securities`, i.e. today's value, at every
historical ranking date. That put the future price into the D and V pillars with
the sign reversed -- 20.5% of the composite weight ranking future winners as big
and expensive. `gem.attach_market_data` now reconstructs the cap from the close
on the ranking date. Any result predating that change should be discarded, not
compared against.
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


def rebalance_dates(con, horizon_months: int, include_non_pit: bool = True) -> list[dt.date]:
    """Dates where enough annual data exists and a full forward window follows.

    The PIT filter has to match the one the scoring will use. Yahoo's annual
    history starts in FY2022 while the XBRL history reaches FY2018, so reading
    the bounds without the filter would hand a PIT-only run a first rebalance
    date four years later than its data actually supports -- discarding most of
    the history and saying nothing about it.
    """
    clause = "" if include_non_pit else " AND coalesce(is_pit, TRUE)"
    bounds = con.execute(f"""
        SELECT min(period_end), max(period_end) FROM fundamentals_pit
         WHERE period_type = 'A'{clause}
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


def run(con, horizon_months: int = 12, min_names: int = 100,
        include_non_pit: bool = True) -> dict:
    """Score the universe at each rebalance date and measure forward returns.

    `include_non_pit` defaults True because PIT-only data cannot currently feed
    the annual pillars -- see bias 1 in the module docstring. It is a parameter
    rather than a hardcoded flag so the compromise is declared at the call site
    and the result records which mode produced it.
    """
    dates = rebalance_dates(con, horizon_months, include_non_pit=include_non_pit)
    if not dates:
        return {"error": "no usable rebalance dates"}

    periods = []
    for as_of in dates:
        scored = gem.score_universe(con, as_of=as_of, include_non_pit=include_non_pit)
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
        if not include_non_pit:
            annual_pit = con.execute("""
                SELECT count(*) FROM fundamentals_pit
                 WHERE coalesce(is_pit, TRUE) AND period_type = 'A'
            """).fetchone()[0]
            if not annual_pit:
                return {"error": "PIT-only run found no annual point-in-time rows: "
                                 "the XBRL and PDF corpus is quarterly, and the "
                                 "pillars rank on annual periods. Ingest annual "
                                 "filings before running with include_non_pit=False.",
                        "pit_only": True}
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
        # Data quality travels with the result so a number cannot be quoted
        # without the caveat that qualifies it.
        "pit_only": not include_non_pit,
    }
