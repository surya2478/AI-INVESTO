"""The G.E.M. score — Growth, Early, Momentum.

Six pillars, each 0-100, combined with the weights in config.GEM_WEIGHTS. Every
pillar is scored as a PERCENTILE within the universe being ranked rather than
against absolute thresholds. Absolute cut-offs encode a view about what "good"
looks like in a particular market and quietly stop working when conditions
change; a percentile only claims that one company looks better than another on
the same day, which is all a ranking needs to claim.

Inputs come from `fundamentals_pit`. For live scoring that includes Yahoo rows
(`is_pit = FALSE`); for the backtest it must not, and `score_universe` takes
`include_non_pit` so the caller decides rather than the module assuming.

WHAT IS NOT MODELLED, and would change the ranking if it were:
  * order book and capacity — the strongest forward signal for capital goods,
    available only in filings prose. Nothing here sees it.
  * analyst coverage count, which the spec wanted for Discovery.
  * segment mix, so a company deriving 5% of revenue from a theme scores the
    same as a pure play.
"""

from __future__ import annotations

import datetime as dt
import logging

import numpy as np
import pandas as pd

from engine.config import GEM_WEIGHTS, settings
from engine.storage import db

log = logging.getLogger(__name__)

PILLARS = ["t_score", "g_score", "q_score", "d_score", "v_score", "m_score"]


def _pct_rank(series: pd.Series, ascending: bool = True) -> pd.Series:
    """Percentile rank 0-100, NaN-safe, ties averaged."""
    ranked = series.rank(pct=True, ascending=ascending, na_option="keep")
    return ranked * 100.0


def _cagr(series: pd.Series, years: int) -> float | None:
    """Compound growth over `years` annual periods, oldest-to-newest input."""
    if len(series) < years + 1:
        return None
    start, end = float(series.iloc[-years - 1]), float(series.iloc[-1])
    if start <= 0 or end <= 0:
        return None
    return ((end / start) ** (1.0 / years) - 1.0) * 100.0


def build_features(con, as_of: dt.date, include_non_pit: bool = True) -> pd.DataFrame:
    """Per-company inputs for the pillars, as of a date."""
    facts = db.fundamentals_asof(con, as_of, periods=24, include_non_pit=include_non_pit)
    if facts.empty:
        return pd.DataFrame()

    annual = facts[facts.period_type == "A"]
    quarterly = facts[facts.period_type == "Q"]

    rows = []
    for security_id, group in annual.groupby("security_id"):
        wide = (group.pivot_table(index="period_end", columns="metric",
                                  values="value", aggfunc="last").sort_index())
        if wide.empty:
            continue

        revenue = wide["revenue"].dropna() if "revenue" in wide else pd.Series(dtype=float)
        pat = wide["pat"].dropna() if "pat" in wide else pd.Series(dtype=float)
        ebitda = wide["ebitda"].dropna() if "ebitda" in wide else pd.Series(dtype=float)
        cfo = wide["cfo"].dropna() if "cfo" in wide else pd.Series(dtype=float)
        capex = wide["capex"].dropna() if "capex" in wide else pd.Series(dtype=float)
        net_worth = wide["net_worth"].dropna() if "net_worth" in wide else pd.Series(dtype=float)
        debt = wide["total_debt"].dropna() if "total_debt" in wide else pd.Series(dtype=float)

        rev_2y, rev_4y = _cagr(revenue, 2), _cagr(revenue, 3)
        pat_2y = _cagr(pat, 2)

        # Margin trend: latest EBITDA margin minus the margin two years back.
        margin_now = margin_then = None
        if len(ebitda) >= 1 and len(revenue) >= 1 and float(revenue.iloc[-1]):
            margin_now = float(ebitda.iloc[-1]) / float(revenue.iloc[-1]) * 100
        if len(ebitda) >= 3 and len(revenue) >= 3 and float(revenue.iloc[-3]):
            margin_then = float(ebitda.iloc[-3]) / float(revenue.iloc[-3]) * 100

        latest_rev = float(revenue.iloc[-1]) if len(revenue) else None

        rows.append({
            "security_id": security_id,
            # Growth
            "rev_cagr_2y": rev_2y,
            "rev_accel": (rev_2y - rev_4y) if (rev_2y is not None and rev_4y is not None) else None,
            "operating_leverage": (pat_2y - rev_2y) if (pat_2y is not None and rev_2y is not None) else None,
            "margin_trend": (margin_now - margin_then) if (margin_now is not None and margin_then is not None) else None,
            "capex_intensity": (abs(float(capex.iloc[-1])) / latest_rev * 100)
                               if len(capex) and latest_rev else None,
            # Quality
            "roe": (float(pat.iloc[-1]) / float(net_worth.iloc[-1]) * 100)
                   if len(pat) and len(net_worth) and float(net_worth.iloc[-1]) > 0 else None,
            "cash_conversion": (float(cfo.iloc[-3:].sum()) / float(pat.iloc[-3:].sum()))
                               if len(cfo) >= 3 and len(pat) >= 3 and float(pat.iloc[-3:].sum()) > 0 else None,
            "debt_equity": (float(debt.iloc[-1]) / float(net_worth.iloc[-1]))
                           if len(debt) and len(net_worth) and float(net_worth.iloc[-1]) > 0 else None,
            "net_worth": float(net_worth.iloc[-1]) if len(net_worth) else None,
            "pat_ttm": None,   # filled from quarterly below
            "revenue_latest": latest_rev,
        })

    features = pd.DataFrame(rows)
    if features.empty:
        return features

    # Trailing twelve months from quarterly rows, where available.
    ttm = []
    for security_id, group in quarterly.groupby("security_id"):
        wide = (group.pivot_table(index="period_end", columns="metric",
                                  values="value", aggfunc="last").sort_index())
        if "pat" in wide and wide["pat"].dropna().shape[0] >= 4:
            ttm.append({"security_id": security_id,
                        "pat_ttm_q": float(wide["pat"].dropna().iloc[-4:].sum())})
    if ttm:
        features = features.merge(pd.DataFrame(ttm), on="security_id", how="left")
        features["pat_ttm"] = features["pat_ttm_q"]
        features = features.drop(columns="pat_ttm_q")

    return features


def shares_outstanding_asof(con, as_of: dt.date) -> pd.DataFrame:
    """Share count visible on `as_of`, newest published period wins.

    Same discipline as `fundamentals_asof`: `filing_date <= as_of` excludes
    counts not yet published, and the newest filing wins per period so a later
    revision supersedes without leaking backwards.
    """
    return con.execute("""
        WITH visible AS (
            SELECT security_id, value,
                   row_number() OVER (
                       PARTITION BY security_id
                       ORDER BY period_end DESC, filing_date DESC
                   ) AS rn
              FROM fundamentals_pit
             WHERE metric = 'share_count' AND value > 0 AND filing_date <= ?
        )
        SELECT security_id, value AS share_count FROM visible WHERE rn = 1
    """, [as_of]).df()


def attach_market_data(con, features: pd.DataFrame, as_of: dt.date) -> pd.DataFrame:
    """Market cap, liquidity, ownership and price trend.

    MARKET CAP IS RECONSTRUCTED, NOT READ. `securities.market_cap` holds today's
    value, and using it at a historical date does not merely add noise -- it
    feeds tomorrow's price into yesterday's ranking, with the sign reversed.
    A company that went on to triple carries a large cap now, so it would rank
    as big (low D, which rewards small) and as expensive (low V, since the
    numerator is a price the market had not yet paid). Between them D and V
    carry 20.5% of the composite, so a fifth of the score would be an inverted
    copy of the answer. The first backtest ran with exactly that defect and its
    negative result says nothing about the score.

    So: last close on or before `as_of`, times the share count published by then.
    Both halves are point-in-time. Reconstructed caps match the stored figure to
    within a few tenths of a percent on current data.
    """
    if features.empty:
        return features

    profile = con.execute("""
        SELECT security_id, ticker, coalesce(industry,'') AS industry
          FROM securities
    """).df()

    liquidity = con.execute("""
        SELECT security_id,
               median(adj_close * volume) AS turnover,
               count(*) AS bars
          FROM ohlcv WHERE date <= ? AND date >= ? - INTERVAL 90 DAY
         GROUP BY security_id
    """, [as_of, as_of]).df()

    momentum = con.execute("""
        WITH w AS (
            SELECT security_id, date, adj_close,
                   row_number() OVER (PARTITION BY security_id ORDER BY date DESC) AS rn
              FROM ohlcv WHERE date <= ?
        )
        SELECT security_id,
               max(CASE WHEN rn = 1   THEN adj_close END) AS px_now,
               max(CASE WHEN rn = 65  THEN adj_close END) AS px_3m,
               max(CASE WHEN rn = 252 THEN adj_close END) AS px_12m
          FROM w WHERE rn IN (1, 65, 252) GROUP BY security_id
    """, [as_of]).df()

    ownership = con.execute("""
        SELECT security_id, promoter_pct, promoter_pledge_pct
          FROM ownership_pit WHERE filing_date <= ?
    """, [as_of]).df().drop_duplicates("security_id", keep="last")

    out = (features
           .merge(profile, on="security_id", how="left")
           .merge(liquidity, on="security_id", how="left")
           .merge(momentum, on="security_id", how="left")
           .merge(ownership, on="security_id", how="left")
           .merge(shares_outstanding_asof(con, as_of), on="security_id", how="left"))

    # px_now is already the last close on or before as_of, so the cap is dated
    # the same day as the ranking. No fallback to the stored figure: a name
    # without a published share count scores NaN on the pillars that need a cap,
    # which is the honest answer. Silently substituting today's cap is the bug.
    out["market_cap"] = out["px_now"] * out["share_count"]
    missing = int(out["market_cap"].isna().sum())
    if missing:
        log.info("%s: no point-in-time market cap for %d of %d names",
                 as_of, missing, len(out))

    out["mom_3m"] = (out["px_now"] / out["px_3m"] - 1) * 100
    out["mom_12m"] = (out["px_now"] / out["px_12m"] - 1) * 100
    out["pe"] = np.where(out["pat_ttm"] > 0, out["market_cap"] / out["pat_ttm"], np.nan)
    out["pb"] = np.where(out["net_worth"] > 0, out["market_cap"] / out["net_worth"], np.nan)
    return out


def score(frame: pd.DataFrame, theme_scores: dict[str, float] | None = None) -> pd.DataFrame:
    """Compute the six pillars and the composite."""
    if frame.empty:
        return frame

    out = frame.copy()

    # G — growth inflection. Acceleration is weighted above the level: a company
    # already growing 40% is priced for it, one going from 10% to 25% is not.
    g = pd.concat([
        _pct_rank(out["rev_accel"]) * 0.35,
        _pct_rank(out["rev_cagr_2y"]) * 0.25,
        _pct_rank(out["operating_leverage"]) * 0.20,
        _pct_rank(out["margin_trend"]) * 0.10,
        _pct_rank(out["capex_intensity"]) * 0.10,
    ], axis=1).sum(axis=1, min_count=1)

    # Q — quality. Cash conversion carries the most weight: it is the one input
    # that is hard to manufacture.
    q = pd.concat([
        _pct_rank(out["cash_conversion"]) * 0.35,
        _pct_rank(out["roe"]) * 0.30,
        _pct_rank(out["debt_equity"], ascending=False) * 0.20,
        _pct_rank(out["promoter_pct"]) * 0.10,
        _pct_rank(out["promoter_pledge_pct"].fillna(0), ascending=False) * 0.05,
    ], axis=1).sum(axis=1, min_count=1)

    # D — discovery. Smaller scores higher, but liquidity must still permit a
    # position, so illiquidity is penalised rather than rewarded.
    d = pd.concat([
        _pct_rank(out["market_cap"], ascending=False) * 0.70,
        _pct_rank(out["turnover"]) * 0.30,
    ], axis=1).sum(axis=1, min_count=1)

    # V — valuation sanity. A brake, not a value screen: cheapness is rewarded
    # only mildly, because insisting on it is how compounders get missed.
    v = pd.concat([
        _pct_rank(out["pe"], ascending=False) * 0.60,
        _pct_rank(out["pb"], ascending=False) * 0.40,
    ], axis=1).sum(axis=1, min_count=1)

    # M — price trend.
    m = pd.concat([
        _pct_rank(out["mom_12m"]) * 0.60,
        _pct_rank(out["mom_3m"]) * 0.40,
    ], axis=1).sum(axis=1, min_count=1)

    out["g_score"] = g
    out["q_score"] = q
    out["d_score"] = d
    out["v_score"] = v
    out["m_score"] = m
    # T — theme tailwind. Neutral 50 where a company belongs to no tracked theme,
    # so the absence of a theme neither helps nor hurts.
    out["t_score"] = out["ticker"].map(theme_scores or {}).fillna(50.0)

    # Pillars are filled at the neutral midpoint when missing, so a company is
    # not rewarded for having no data on a dimension.
    composite = sum(out[p].fillna(50.0) * w for p, w in GEM_WEIGHTS.items())
    out["gem_score"] = composite
    out["pillars_missing"] = out[PILLARS].isna().sum(axis=1)
    return out.sort_values("gem_score", ascending=False)


def theme_confluence(con, as_of: dt.date) -> dict[str, float]:
    """Trend-confluence score of each company's parent theme.

    Without this the T pillar is a constant 50 for every company, which means
    20% of the composite weight contributes nothing but still dilutes the
    pillars that do carry signal. The first backtest ran with exactly that
    defect.
    """
    from engine.features.trends import build_theme_index, confluence_score, trend_metrics
    from engine.universe.theme_graph import load_theme_graph

    prices = con.execute("""
        SELECT s.ticker, o.date, o.adj_close AS close
          FROM ohlcv o JOIN securities s ON s.security_id = o.security_id
         WHERE o.date <= ?
    """, [as_of]).df()
    if prices.empty:
        return {}
    prices["date"] = pd.to_datetime(prices["date"])

    graph = load_theme_graph()
    out: dict[str, float] = {}
    for theme in graph.themes:
        india = build_theme_index(prices, theme.india_tickers)
        if india.empty:
            continue
        value = confluence_score(trend_metrics(india))
        for ticker in theme.india_tickers:
            out.setdefault(ticker, value)
    return out


def score_universe(con, as_of: dt.date | None = None, include_non_pit: bool = True,
                   theme_scores: dict[str, float] | None = None) -> pd.DataFrame:
    as_of = as_of or dt.date.today()
    features = build_features(con, as_of, include_non_pit=include_non_pit)
    if features.empty:
        return features
    enriched = attach_market_data(con, features, as_of)
    if theme_scores is None:
        theme_scores = theme_confluence(con, as_of)
    return score(enriched, theme_scores)
