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

MISSING DATA IS TREATED AS MISSING, which sounds obvious and was not the case.
Each pillar is a weighted mean over the weight actually available, so a pillar
resting on two inputs is on the same 0-100 scale as one resting on five; it is
then shrunk toward the neutral 50 in proportion to how much of it is known, so
being on the same scale does not mean being trusted equally. `coverage` reports
the share of the composite that rests on evidence rather than on that default.
Read it alongside `gem_score`: 62 on 30% coverage and 62 on 95% coverage are not
the same claim.

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


def _blend(components: list[tuple[pd.Series, float]]) -> tuple[pd.Series, pd.Series]:
    """Weighted mean of ranked components over the weight ACTUALLY AVAILABLE.

    The previous version summed weighted components and stopped there, so a
    missing input did not widen the others' share -- it just removed its own
    weight and left the pillar compressed toward zero. The effect is not subtle
    and it is not neutral. Ranking the 2019 universe on point-in-time data, the
    quality pillar had none of its five inputs except a pledge percentage that
    had been defaulted to zero, and came out a CONSTANT 2.5 for all 69 companies
    -- carrying no information whatever at 20% of the composite weight. Discovery
    kept only turnover and spanned 0.4 to 30.0 where a pillar should span 100,
    so size, 70% of that pillar, was silently absent rather than missing.

    Dividing by the available weight puts every pillar back on one scale, so a
    pillar built from two inputs is comparable with one built from five. How much
    to TRUST it is a separate question, which is what `coverage` answers.

    Returns (score 0-100 on the full scale, coverage 0-1).
    """
    total_weight = sum(weight for _, weight in components)
    weighted = sum(series.fillna(0.0) * weight for series, weight in components)
    available = sum(series.notna().astype(float) * weight for series, weight in components)

    score = weighted / available.replace(0.0, np.nan)
    return score, available / total_weight


def _shrink(score: pd.Series, coverage: pd.Series) -> pd.Series:
    """Pull a thinly-evidenced pillar toward the neutral 50.

    Renormalising alone would let a pillar resting on one weak input swing the
    composite as hard as one resting on all five. Shrinking in proportion to
    coverage says the obvious thing instead: the less of a pillar we can see,
    the less it should move the answer. At full coverage the score is untouched;
    at half coverage it keeps half its distance from neutral; at zero coverage it
    IS neutral, which is what the composite always claimed to do for a missing
    pillar and never actually did for a half-missing one.
    """
    return 50.0 + (score.fillna(50.0) - 50.0) * coverage


def _cagr(series: pd.Series, years: int) -> float | None:
    """Compound growth over `years` annual periods, oldest-to-newest input."""
    if len(series) < years + 1:
        return None
    start, end = float(series.iloc[-years - 1]), float(series.iloc[-1])
    if start <= 0 or end <= 0:
        return None
    return ((end / start) ** (1.0 / years) - 1.0) * 100.0


def _growth_acceleration(series: pd.Series) -> float | None:
    """Change in the growth RATE, measured over windows that do not overlap.

    The previous version subtracted a 3-year CAGR from a 2-year CAGR -- a window
    against a superset of itself, differing by one year -- which is why the
    result correlated +0.44 to +0.76 with the plain 2-year CAGR it was meant to
    add information to. The heaviest input in the growth pillar was largely a
    noisy restatement of the second heaviest.

    Comparing the latest year against the one before it costs some smoothing but
    is at least measuring the thing the pillar claims to measure: a company going
    from 10% to 25% growth, not one already growing 40% and priced for it.
    """
    if len(series) < 3:
        return None
    latest, prior, base = (float(series.iloc[-1]), float(series.iloc[-2]),
                           float(series.iloc[-3]))
    if prior <= 0 or base <= 0:
        return None
    return ((latest / prior) - (prior / base)) * 100.0


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

        rev_2y = _cagr(revenue, 2)
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
            "rev_accel": _growth_acceleration(revenue),
            "operating_leverage": (pat_2y - rev_2y) if (pat_2y is not None and rev_2y is not None) else None,
            "margin_trend": (margin_now - margin_then) if (margin_now is not None and margin_then is not None) else None,
            # Still computed, no longer scored -- see the growth pillar for why.
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


def score(frame: pd.DataFrame, theme_scores: dict[str, float] | None = None,
          theme_exposure: dict[str, float] | None = None) -> pd.DataFrame:
    """Compute the six pillars and the composite.

    `theme_exposure` is the share of each company its themes actually drive.
    Omitted, every theme member is treated as a pure play, which is what the
    engine did before exposure existed.
    """
    if frame.empty:
        return frame

    out = frame.copy()

    # A pledge percentage is only zero if somebody filed a shareholding pattern
    # saying so. Where there is no ownership filing at all, `promoter_pct` is
    # absent too, and defaulting the pledge to zero there asserts "nothing is
    # pledged" on no evidence -- it also kept the quality pillar permanently
    # non-empty, which is how it survived as a constant instead of registering
    # as missing.
    filed = out["promoter_pct"].notna()
    pledge = out["promoter_pledge_pct"].mask(filed & out["promoter_pledge_pct"].isna(), 0.0)

    # G — growth inflection. Acceleration is weighted above the level: a company
    # already growing 40% is priced for it, one going from 10% to 25% is not.
    #
    # CAPEX INTENSITY WAS DROPPED, and the weights of what remains are unchanged
    # -- `_blend` divides by the weight present, so the other four keep their
    # relative sizes and nothing was re-tuned. The thesis for it was reasonable
    # (spending ahead of demand precedes growth) but capex over revenue does not
    # measure that. It measures how capital-hungry an industry is: a cement plant
    # and a company doubling its capacity look identical, and the first is far
    # more common. Reinstating it needs a measure that separates investment from
    # industry -- capex against depreciation, or capex growth against its own
    # history -- not this one pointed in a different direction.
    pillars = {
        "g_score": [
            (_pct_rank(out["rev_accel"]), 0.35),
            (_pct_rank(out["rev_cagr_2y"]), 0.25),
            (_pct_rank(out["operating_leverage"]), 0.20),
            (_pct_rank(out["margin_trend"]), 0.10),
        ],
        # Q — quality. Cash conversion carries the most weight: it is the one
        # input that is hard to manufacture.
        "q_score": [
            (_pct_rank(out["cash_conversion"]), 0.35),
            (_pct_rank(out["roe"]), 0.30),
            (_pct_rank(out["debt_equity"], ascending=False), 0.20),
            (_pct_rank(out["promoter_pct"]), 0.10),
            (_pct_rank(pledge, ascending=False), 0.05),
        ],
        # D — discovery. Smaller scores higher, but liquidity must still permit
        # a position, so illiquidity is penalised rather than rewarded.
        "d_score": [
            (_pct_rank(out["market_cap"], ascending=False), 0.70),
            (_pct_rank(out["turnover"]), 0.30),
        ],
        # V — valuation sanity. A brake, not a value screen: cheapness is
        # rewarded only mildly, because insisting on it is how compounders get
        # missed.
        "v_score": [
            (_pct_rank(out["pe"], ascending=False), 0.60),
            (_pct_rank(out["pb"], ascending=False), 0.40),
        ],
        # M — price trend.
        "m_score": [
            (_pct_rank(out["mom_12m"]), 0.60),
            (_pct_rank(out["mom_3m"]), 0.40),
        ],
    }

    coverages = {}
    for pillar, components in pillars.items():
        raw, coverage = _blend(components)
        out[pillar] = _shrink(raw, coverage)
        out[f"{pillar}_coverage"] = coverage * 100.0
        coverages[pillar] = coverage

    # T — theme tailwind. A company in no tracked theme is not evidence of
    # anything, so it scores neutral and reports zero coverage rather than
    # claiming a 50 it did not earn. Where exposure is known it IS the coverage:
    # a 5% exposure moves the score 5% of the way from neutral, so a bit-part
    # player cannot ride a theme like a pure play.
    theme = out["ticker"].map(theme_scores or {})
    if theme_exposure:
        theme_coverage = out["ticker"].map(theme_exposure).astype(float).clip(0.0, 1.0)
        theme_coverage = theme_coverage.where(theme.notna(), 0.0).fillna(0.0)
    else:
        theme_coverage = theme.notna().astype(float)
    out["t_score"] = _shrink(theme, theme_coverage)
    out["t_score_coverage"] = theme_coverage * 100.0
    coverages["t_score"] = theme_coverage

    # Every pillar is now on the full 0-100 scale and already neutral where it
    # has nothing behind it, so the composite needs no fillna of its own.
    out["gem_score"] = sum(out[p] * w for p, w in GEM_WEIGHTS.items())

    # How much of the score is evidence rather than the neutral default. Quoting
    # a gem_score without this is quoting an average of things partly not known.
    out["coverage"] = sum(coverages[p] * w for p, w in GEM_WEIGHTS.items()) * 100.0
    out["pillars_missing"] = sum((coverages[p] == 0).astype(int) for p in PILLARS)
    return out.sort_values("gem_score", ascending=False)


def blend_theme_exposure(
    contributions: dict[str, list[tuple[float, float]]],
) -> tuple[dict[str, float], dict[str, float]]:
    """Combine the themes a company belongs to into one tailwind and one exposure.

    `contributions` maps ticker to [(theme confluence, exposure to that theme)].
    The score is the exposure-weighted mean, so being mostly a grid company and
    marginally a quantum one is not half of each. The exposure is the sum, capped
    at 1.0: three themes at 0.5 do not make a company 150% exposed to anything.
    """
    scores: dict[str, float] = {}
    exposures: dict[str, float] = {}
    for ticker, pairs in contributions.items():
        total = sum(exposure for _, exposure in pairs)
        if total <= 0:
            continue
        scores[ticker] = sum(value * exposure for value, exposure in pairs) / total
        exposures[ticker] = min(total, 1.0)
    return scores, exposures


def theme_confluence(con, as_of: dt.date) -> tuple[dict[str, float], dict[str, float]]:
    """Trend-confluence felt by each company, and how exposed it is.

    Without this the T pillar is a constant 50 for every company, which means
    20% of the composite weight contributes nothing but still dilutes the
    pillars that do carry signal. The first backtest ran with exactly that
    defect.

    TWO THINGS WERE WRONG WITH THE FIRST FIX. A company in more than one theme
    took the first theme's score via `setdefault`, so CG Power was scored on
    AI & Compute and never on Grid Infrastructure purely because of YAML order.
    And every member of a theme was scored as a pure play, so TCS -- whose own
    config entry calls quantum "a rounding error in each of these companies'
    revenue" -- received the same tailwind as a company that does nothing else.

    So: confluence is averaged across a company's themes weighted by exposure,
    and total exposure is returned separately as the pillar's coverage. A 5%
    exposure moves the score 5% of the way from neutral, which is the whole
    point. Returns (score per ticker, exposure per ticker capped at 1.0).
    """
    from engine.features.trends import build_theme_index, confluence_score, trend_metrics
    from engine.universe.theme_graph import load_theme_graph

    prices = con.execute("""
        SELECT s.ticker, o.date, o.adj_close AS close
          FROM ohlcv o JOIN securities s ON s.security_id = o.security_id
         WHERE o.date <= ?
    """, [as_of]).df()
    if prices.empty:
        return {}, {}
    prices["date"] = pd.to_datetime(prices["date"])

    graph = load_theme_graph()
    contributions: dict[str, list[tuple[float, float]]] = {}
    for theme in graph.themes:
        india = build_theme_index(prices, theme.india_tickers)
        if india.empty:
            continue
        value = confluence_score(trend_metrics(india))
        for ticker, exposure in theme.india_exposures.items():
            contributions.setdefault(ticker, []).append((value, exposure))

    return blend_theme_exposure(contributions)


def score_universe(con, as_of: dt.date | None = None, include_non_pit: bool = True,
                   theme_scores: dict[str, float] | None = None,
                   theme_exposure: dict[str, float] | None = None) -> pd.DataFrame:
    as_of = as_of or dt.date.today()
    features = build_features(con, as_of, include_non_pit=include_non_pit)
    if features.empty:
        return features
    enriched = attach_market_data(con, features, as_of)
    if theme_scores is None:
        theme_scores, derived_exposure = theme_confluence(con, as_of)
        theme_exposure = theme_exposure or derived_exposure
    return score(enriched, theme_scores, theme_exposure)
