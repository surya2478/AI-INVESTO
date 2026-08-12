"""Compute the numbers the Today screen displays, and emit them as JSON.

Everything written here is derived from real ingested price history. Fields
requiring fundamentals (the G.E.M. score and its pillars) are NOT produced by
this module -- those arrive at Stage 2, and the mockup marks them as placeholder
rather than inventing values that would look authoritative.
"""

from __future__ import annotations

import json

import pandas as pd

from engine.config import settings
from engine.features.trends import (
    build_theme_index,
    classify_stage,
    confluence_score,
    lag_profile,
    theme_divergence,
    trend_metrics,
)
from engine.storage import db
from engine.universe.theme_graph import load_theme_graph


def build() -> dict:
    graph = load_theme_graph()
    con = db.connect(read_only=True)
    try:
        prices = con.execute("""
            SELECT s.ticker, o.date, o.adj_close AS close, o.volume
              FROM ohlcv o JOIN securities s ON s.security_id = o.security_id
        """).df()
    finally:
        con.close()

    prices["date"] = pd.to_datetime(prices["date"])
    as_of = prices["date"].max()

    # ------------------------------------------------------------- benchmarks
    pulse = []
    for label, symbol in graph.benchmarks.items():
        series = (
            prices[prices.ticker == symbol]
            .set_index("date")["close"]
            .sort_index()
        )
        if series.empty:
            continue
        metrics = trend_metrics(series)
        pulse.append({
            "label": label,
            "symbol": symbol,
            "level": metrics.get("level"),
            "d_1m": metrics.get("d_1m"),
            "m_12m": metrics.get("m_12m"),
            "above_ma200": metrics.get("above_ma200"),
        })

    # ----------------------------------------------------------------- themes
    themes = []
    for theme in graph.themes:
        gi = build_theme_index(prices, theme.global_tickers)
        ii = build_theme_index(prices, theme.india_tickers)
        if gi.empty or ii.empty:
            continue

        gm, im = trend_metrics(gi), trend_metrics(ii)
        lag = lag_profile(gi, ii)
        divergence = theme_divergence(gi, ii)

        themes.append({
            "id": theme.theme_id,
            "name": theme.name,
            "tier": theme.tier,
            "status": theme.status,
            "global": gm,
            "india": im,
            "stage": classify_stage(im),
            "global_stage": classify_stage(gm),
            "confluence": round(confluence_score(im), 1),
            "lag": lag,
            "divergence": divergence,
            "india_count": len(theme.india_tickers),
            "global_count": len(theme.global_tickers),
            # Sparkline: 18 months of the India leg, thinned for transport.
            "india_spark": [
                round(v, 2) for v in ii.tail(378).iloc[::7].tolist()
            ],
            "global_spark": [
                round(v, 2) for v in gi.tail(378).iloc[::7].tolist()
            ],
        })

    # Surface India-trailing themes first: those are the ones worth a look.
    themes.sort(
        key=lambda t: (t["divergence"].get("state") != "INDIA_TRAILING",
                       -t["confluence"])
    )

    return {
        "as_of": as_of.strftime("%Y-%m-%d"),
        "bars": int(len(prices)),
        "securities": int(prices.ticker.nunique()),
        "pulse": pulse,
        "themes": themes,
    }


if __name__ == "__main__":
    payload = build()
    settings.REPORT_DIR.mkdir(parents=True, exist_ok=True)
    out = settings.REPORT_DIR / "today.json"
    out.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    print(f"wrote {out}")
    print(f"as of {payload['as_of']} | {payload['securities']} securities "
          f"| {payload['bars']:,} bars | {len(payload['themes'])} themes")
    for t in payload["themes"]:
        lag, div = t["lag"], t["divergence"]
        print(f"  {t['id']:<20} {t['stage']:<13} conf {t['confluence']:>5} "
              f"| {div.get('state','?'):<16} z={div.get('gap_z')} "
              f"| leads={lag.get('leads')} peak={lag.get('best_lag')}w "
              f"r={lag.get('best_corr')} (r@0={lag.get('corr_at_zero')})")
