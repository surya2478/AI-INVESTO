"""Does a wide India-versus-global gap predict India catching up?

The Today screen's lead callout fires when India's leg has trailed its global leg
by more than one standard deviation while the global leg is rising. It has been
the largest element on that screen since the app existed and nobody has ever
tested it. `theme_divergence` says as much in its own docstring -- "whether wide
gaps actually mean-revert is a Stage 3 backtest question".

This is that question. It is the one validation left that is not blocked: the
score needed point-in-time fundamentals and the gates need delisted companies,
but this needs only prices, and there are 2.3 million bars going back to 2012.

TWO THINGS THIS DOES DIFFERENTLY FROM THE LIVE SIGNAL
-----------------------------------------------------
1. The z-score uses TRAILING statistics only. The live reading standardises the
   gap against its whole history including the present, which is right for
   "how unusual is today" and is look-ahead in a study: it would judge a 2015
   signal using the distribution of gaps through 2026.
2. Forward returns are compared against the SAME basket on non-signal weeks, so
   the comparison is the theme against itself rather than against a market that
   was rising throughout.

    python scripts/validate_divergence.py
"""

from __future__ import annotations

import sys
import warnings

warnings.filterwarnings("ignore")
sys.path.insert(0, r"C:\AI-Investo")

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from engine.features.trends import build_theme_index  # noqa: E402
from engine.storage import db  # noqa: E402
from engine.universe.theme_graph import load_theme_graph  # noqa: E402

pd.set_option("display.width", 250)

LOOKBACK_WEEKS = 13          # the momentum window the live signal uses
MIN_HISTORY_WEEKS = 104      # before this there is no distribution to speak of
Z_THRESHOLD = -1.0           # matches theme_divergence
HORIZONS = {"1m": 4, "3m": 13, "6m": 26, "12m": 52}


def observations(global_index: pd.Series, india_index: pd.Series) -> pd.DataFrame:
    """Weekly signal state and the India basket's forward returns."""
    gw = global_index.resample("W").last().pct_change(LOOKBACK_WEEKS)
    iw = india_index.resample("W").last().pct_change(LOOKBACK_WEEKS)
    india_weekly = india_index.resample("W").last()

    gap = (iw - gw).dropna()
    if len(gap) < MIN_HISTORY_WEEKS + max(HORIZONS.values()):
        return pd.DataFrame()

    # Trailing standardisation: what the gap looked like relative to what had
    # been seen BY THEN, never after.
    mean = gap.expanding(MIN_HISTORY_WEEKS).mean()
    sd = gap.expanding(MIN_HISTORY_WEEKS).std()
    z = ((gap - mean) / sd.replace(0, np.nan)).dropna()

    frame = pd.DataFrame({"z": z})
    frame["global_up"] = gw.reindex(frame.index) > 0
    frame["signal"] = (frame.z <= Z_THRESHOLD) & frame.global_up

    level = india_weekly.reindex(frame.index)
    for name, weeks in HORIZONS.items():
        frame[name] = (india_weekly.shift(-weeks).reindex(frame.index) / level - 1.0) * 100.0
    return frame


def main() -> None:
    con = db.connect_for_reading()
    prices = con.execute("""
        SELECT s.ticker, o.date, o.adj_close AS close
          FROM ohlcv o JOIN securities s ON s.security_id = o.security_id
    """).df()
    con.close()
    prices["date"] = pd.to_datetime(prices["date"])

    graph = load_theme_graph()
    everything, per_theme = [], []

    for theme in graph.themes:
        gi = build_theme_index(prices, theme.global_tickers)
        ii = build_theme_index(prices, theme.india_tickers)
        if gi.empty or ii.empty:
            continue
        frame = observations(gi, ii)
        if frame.empty:
            print(f"  {theme.theme_id:20} skipped — too little shared history")
            continue

        frame["theme"] = theme.theme_id
        everything.append(frame)

        row = {"theme": theme.theme_id, "weeks": len(frame),
               "signal_weeks": int(frame.signal.sum())}
        for name in HORIZONS:
            on = frame.loc[frame.signal, name].dropna()
            off = frame.loc[~frame.signal, name].dropna()
            row[f"{name}_on"] = on.mean() if len(on) else np.nan
            row[f"{name}_off"] = off.mean() if len(off) else np.nan
            row[f"{name}_gap"] = row[f"{name}_on"] - row[f"{name}_off"]
        per_theme.append(row)

    if not everything:
        print("no theme had enough shared history")
        return

    pooled = pd.concat(everything, ignore_index=True)

    print("=== POOLED: India basket forward return, signal weeks vs the rest ===")
    rows = []
    for name, weeks in HORIZONS.items():
        on = pooled.loc[pooled.signal, name].dropna()
        off = pooled.loc[~pooled.signal, name].dropna()
        if not len(on) or not len(off):
            continue
        # Overlapping windows: consecutive weekly observations share all but one
        # week of a forward window, so the honest sample size is the number of
        # INDEPENDENT windows, not the row count.
        rows.append({
            "horizon": name,
            "signal_obs": len(on), "independent": round(len(on) / weeks, 1),
            "signal_mean": round(on.mean(), 1), "other_mean": round(off.mean(), 1),
            "gap_pp": round(on.mean() - off.mean(), 1),
            "signal_median": round(on.median(), 1), "other_median": round(off.median(), 1),
        })
    print(pd.DataFrame(rows).to_string(index=False))

    print("\n=== PER THEME: 12-month gap (signal minus non-signal, pp) ===")
    per = pd.DataFrame(per_theme).sort_values("12m_gap")
    print(per[["theme", "weeks", "signal_weeks", "3m_gap", "6m_gap", "12m_gap"]]
          .round(1).to_string(index=False))

    positive = int((per["12m_gap"] > 0).sum())
    print(f"\n12-month gap positive in {positive} of {len(per)} themes")
    print(f"signal fired on {int(pooled.signal.sum()):,} of {len(pooled):,} theme-weeks "
          f"({pooled.signal.mean() * 100:.1f}%)")


if __name__ == "__main__":
    main()
