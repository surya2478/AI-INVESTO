"""Theme index construction, multi-timeframe trend, and propagation lag.

This is the Stage 1 core in prototype form: it produces the numbers the Today
and Themes screens display. The classification thresholds here are reasoned
defaults, not calibrated ones -- Stage 3's backtest is what turns them from
hypothesis into evidence.
"""

from __future__ import annotations

import datetime as dt

import numpy as np
import pandas as pd

TRADING_DAYS = {"1m": 21, "3m": 63, "6m": 126, "12m": 252, "36m": 756}


def build_theme_index(prices: pd.DataFrame, tickers: list[str]) -> pd.Series:
    """Equal-weight, daily-rebalanced index for a basket, rebased to 100.

    Equal weight rather than cap weight on purpose: the question is "is this
    theme working", and a cap-weighted basket would just track its largest
    member. Daily rebalancing also lets constituents with different listing
    dates join mid-series without distorting history -- each day averages
    whatever is actually trading.
    """
    wide = (
        prices[prices.ticker.isin(tickers)]
        .pivot_table(index="date", columns="ticker", values="close")
        .sort_index()
    )
    if wide.empty:
        return pd.Series(dtype="float64")

    returns = wide.pct_change()
    # Require 2+ live names before the index starts, so one early listing
    # cannot define the whole basket's history.
    live = returns.notna().sum(axis=1)
    basket = returns.mean(axis=1).where(live >= 2)

    first = basket.first_valid_index()
    if first is None:
        return pd.Series(dtype="float64")

    basket = basket.loc[first:].fillna(0.0)
    return (1.0 + basket).cumprod() * 100.0


def _pct_change_over(series: pd.Series, days: int) -> float | None:
    if len(series) <= days:
        return None
    now, then = series.iloc[-1], series.iloc[-1 - days]
    if not then or np.isnan(then):
        return None
    return float((now / then - 1.0) * 100.0)


def trend_metrics(index: pd.Series) -> dict:
    """Daily / weekly / monthly readings for one series."""
    if index.empty or len(index) < 30:
        return {}

    level = float(index.iloc[-1])
    ma50 = index.rolling(50).mean().iloc[-1]
    ma200 = index.rolling(200).mean().iloc[-1] if len(index) >= 200 else np.nan
    window36 = index.tail(TRADING_DAYS["36m"])
    high36 = float(window36.max())

    return {
        "level": level,
        "d_1m": _pct_change_over(index, TRADING_DAYS["1m"]),
        "w_3m": _pct_change_over(index, TRADING_DAYS["3m"]),
        "m_12m": _pct_change_over(index, TRADING_DAYS["12m"]),
        "m_36m": _pct_change_over(index, TRADING_DAYS["36m"]),
        "above_ma50": bool(level > ma50) if not np.isnan(ma50) else None,
        "above_ma200": bool(level > ma200) if not np.isnan(ma200) else None,
        "from_36m_high": float((level / high36 - 1.0) * 100.0) if high36 else None,
        "vol_ann": float(index.pct_change().tail(252).std() * np.sqrt(252) * 100),
    }


def classify_stage(m: dict) -> str:
    """Map trend readings onto the lifecycle stage the app displays.

    The ordering matters: CROWDED is tested before ACCELERATING so a theme that
    has already run hard is never labelled as a fresh entry.
    """
    if not m:
        return "UNKNOWN"

    d, w = m.get("d_1m") or 0.0, m.get("w_3m") or 0.0
    y, from_high = m.get("m_12m") or 0.0, m.get("from_36m_high") or 0.0

    if y > 60 and from_high > -6:
        return "CROWDED"
    if w < -6 and y < 8:
        return "FADING"
    if y > 12 and w > 2 and d > -3:
        return "ACCELERATING"
    if w > 0 and from_high < -12:
        return "EMERGING"
    return "BASING"


def confluence_score(m: dict) -> float:
    """0-100 alignment of the three timeframes.

    Each timeframe contributes a squashed momentum reading; a trend confirmed on
    all three scores far higher than one strong on dailies alone.
    """
    if not m:
        return 0.0

    def squash(value: float | None, scale: float) -> float:
        if value is None:
            return 50.0
        return float(100.0 / (1.0 + np.exp(-value / scale)))

    daily = squash(m.get("d_1m"), 6.0)
    weekly = squash(m.get("w_3m"), 12.0)
    monthly = squash(m.get("m_12m"), 25.0)

    score = 0.25 * daily + 0.35 * weekly + 0.40 * monthly
    if m.get("above_ma200"):
        score += 5.0
    return float(np.clip(score, 0, 100))


def propagation_lag(
    global_index: pd.Series,
    india_index: pd.Series,
    max_lag_weeks: int = 26,
) -> dict:
    """Lead-lag between a theme's global leg and its India leg.

    Correlates weekly returns at lags 0..max_lag_weeks, where a positive lag
    means the global leg moves first. Stability is the share of independent
    sub-periods whose best lag lands near the full-sample estimate -- a lag with
    low stability is noise and the UI must say so rather than imply a rule.
    """
    gw = global_index.resample("W").last().pct_change().dropna()
    iw = india_index.resample("W").last().pct_change().dropna()

    joined = pd.concat([gw, iw], axis=1, keys=["g", "i"]).dropna()
    if len(joined) < 60:
        return {"lag_weeks": None, "correlation": None, "stability": None,
                "state": "INSUFFICIENT_DATA"}

    def best_lag(frame: pd.DataFrame) -> tuple[int, float]:
        scores = []
        for lag in range(max_lag_weeks + 1):
            shifted = frame["g"].shift(lag)
            pair = pd.concat([shifted, frame["i"]], axis=1).dropna()
            if len(pair) < 30:
                continue
            scores.append((lag, float(pair.corr().iloc[0, 1])))
        if not scores:
            return 0, 0.0
        return max(scores, key=lambda pair: pair[1])

    lag, corr = best_lag(joined)

    # Stability: split into 3 sub-periods, see whether the lag holds up.
    # Slice positionally -- np.array_split would drop the column labels that
    # best_lag needs.
    bounds = np.linspace(0, len(joined), 4, dtype=int)
    agreements = testable = 0
    for lo, hi in zip(bounds[:-1], bounds[1:]):
        chunk = joined.iloc[lo:hi]
        if len(chunk) < 30:
            continue
        testable += 1
        sub_lag, _ = best_lag(chunk)
        if abs(sub_lag - lag) <= 4:
            agreements += 1
    stability = agreements / testable if testable else 0.0

    return {
        "lag_weeks": int(lag),
        "correlation": round(corr, 3),
        "stability": round(stability, 2),
        "state": "OK",
    }


def lag_profile(
    global_index: pd.Series, india_index: pd.Series, max_lag_weeks: int = 26
) -> dict:
    """Correlation of 13-week momentum at each lag, plus whether a lead exists.

    MEASURED RESULT (Aug 2026, 2012-2026 history, all 14 themes): correlation
    decays monotonically from lag 0 for every theme with a meaningful basket.
    ai_compute runs 0.48 -> 0.39 -> 0.27 -> 0.12 across 0/4/8/12 weeks;
    grid_infra 0.35 -> 0.32 -> 0.25 -> 0.10. India's legs move WITH their global
    counterparts, not weeks behind them.

    `leads` therefore reports False for essentially every theme, and that is the
    honest answer rather than a defect. Apparent peaks at the 25-26 week boundary
    carry low correlation and are edge artefacts of the search window.

    Caveat: overlapping momentum windows are autocorrelated, so these
    correlations are inflated in absolute terms. They are used only to compare
    lags against each other, never as significance tests.
    """
    gw = global_index.resample("W").last().pct_change(13).dropna()
    iw = india_index.resample("W").last().pct_change(13).dropna()

    profile: list[tuple[int, float]] = []
    for lag in range(max_lag_weeks + 1):
        pair = pd.concat([gw.shift(lag), iw], axis=1).dropna()
        if len(pair) < 40:
            continue
        profile.append((lag, float(pair.corr().iloc[0, 1])))

    if not profile:
        return {"profile": [], "best_lag": None, "best_corr": None,
                "corr_at_zero": None, "leads": False}

    best = max(profile, key=lambda p: p[1])
    at_zero = profile[0][1]

    # A real lead needs the peak away from zero AND materially better than zero.
    leads = bool(best[0] >= 2 and best[1] > at_zero + 0.05 and best[0] < max_lag_weeks - 1)

    return {
        "profile": [(lag, round(c, 3)) for lag, c in profile],
        "best_lag": best[0],
        "best_corr": round(best[1], 3),
        "corr_at_zero": round(at_zero, 3),
        "leads": leads,
    }


def theme_divergence(global_index: pd.Series, india_index: pd.Series) -> dict:
    """How far India's leg has diverged from its global leg, versus normal.

    This replaces the lead-lag framing the spec originally assumed. Since the
    legs move together but only loosely (peak r roughly 0.2-0.5), the gap between
    them varies a lot and is the measurable thing. A gap far below its own
    history says India has not kept up with a theme that is working globally.

    It is an observation about relative momentum, not a prediction that the gap
    closes. Whether wide gaps actually mean-revert is a Stage 3 backtest question.
    """
    gw = global_index.resample("W").last().pct_change(13)
    iw = india_index.resample("W").last().pct_change(13)

    gap = (iw - gw).dropna()          # negative => India trailing
    if len(gap) < 60:
        return {"state": "INSUFFICIENT_DATA", "text": "Not enough shared history."}

    now = float(gap.iloc[-1])
    mean, sd = float(gap.mean()), float(gap.std())
    z = (now - mean) / sd if sd else 0.0
    pct = float((gap < now).mean() * 100)

    g3 = float(gw.iloc[-1] * 100) if pd.notna(gw.iloc[-1]) else 0.0
    i3 = float(iw.iloc[-1] * 100) if pd.notna(iw.iloc[-1]) else 0.0

    if z <= -1.0 and g3 > 0:
        state = "INDIA_TRAILING"
        text = (
            f"Global leg up {g3:.0f}% over 13 weeks while India is "
            f"{'up' if i3 >= 0 else 'down'} {abs(i3):.0f}%. That gap sits in the "
            f"{pct:.0f}th percentile of its own history ({z:+.1f} SD) — India has "
            "rarely trailed this theme by this much."
        )
    elif z >= 1.0:
        state = "INDIA_AHEAD"
        text = (
            f"India up {i3:.0f}% versus global {g3:.0f}% over 13 weeks, a gap "
            f"wider than {pct:.0f}% of history ({z:+.1f} SD). India is leading, "
            "not catching up."
        )
    else:
        state = "IN_LINE"
        text = (
            f"India {i3:+.0f}% versus global {g3:+.0f}% over 13 weeks — within "
            f"the normal range for this pair ({z:+.1f} SD)."
        )

    return {
        "state": state, "text": text, "gap_now": round(now * 100, 1),
        "gap_z": round(z, 2), "gap_pct": round(pct, 0),
        "global_13w": round(g3, 1), "india_13w": round(i3, 1),
    }
