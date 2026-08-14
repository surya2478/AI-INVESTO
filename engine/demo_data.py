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
        "gates": gate_payload(),
    }


def gate_payload() -> dict:
    """Latest gate verdicts, if they have been computed."""
    con = db.connect(read_only=True)
    try:
        as_of = con.execute("SELECT max(as_of_date) FROM gate_results").fetchone()[0]
        if as_of is None:
            return {"available": False}

        # Read `status`, never `passed` -- the boolean cannot separate a gate
        # that failed from one that could not be evaluated.
        per_gate = con.execute("""
            SELECT g.gate_name,
                   sum(CASE WHEN g.status = 'PASS'    THEN 1 ELSE 0 END) AS passed,
                   sum(CASE WHEN g.status = 'FAIL'    THEN 1 ELSE 0 END) AS failed,
                   sum(CASE WHEN g.status = 'UNKNOWN' THEN 1 ELSE 0 END) AS unknown,
                   count(*)                                             AS total
              FROM gate_results g
             WHERE g.as_of_date = ?
             GROUP BY g.gate_name ORDER BY failed DESC, unknown DESC
        """, [as_of]).df()

        # Only genuine FAILs on critical gates are rejections.
        rejected = con.execute("""
            SELECT s.ticker, g.gate_name, g.detail
              FROM gate_results g JOIN securities s ON s.security_id = g.security_id
             WHERE g.as_of_date = ? AND g.status = 'FAIL'
               AND g.gate_name IN ('surveillance','serial_dilution','sustained_losses')
             ORDER BY s.ticker
        """, [as_of]).df()

        # Verdict per company, mirroring gates.verdicts(): a FAIL on a critical
        # gate rejects; UNKNOWN on a critical gate leaves it unvetted, never
        # cleared.
        verdicts = con.execute("""
            WITH per_company AS (
                SELECT security_id,
                       max(CASE WHEN status = 'FAIL' AND gate_name IN
                            ('surveillance','cash_conversion','serial_dilution','sustained_losses')
                            THEN 1 ELSE 0 END) AS critical_fail,
                       max(CASE WHEN status = 'FAIL' THEN 1 ELSE 0 END) AS any_fail,
                       max(CASE WHEN status = 'UNKNOWN' AND gate_name IN
                            ('surveillance','cash_conversion','serial_dilution','sustained_losses')
                            THEN 1 ELSE 0 END) AS critical_unknown
                  FROM gate_results WHERE as_of_date = ?
                 GROUP BY security_id
            )
            SELECT CASE WHEN critical_fail = 1 THEN 'REJECTED'
                        WHEN any_fail = 1 THEN 'FLAGGED'
                        WHEN critical_unknown = 1 THEN 'UNVETTED'
                        ELSE 'CLEARED' END AS verdict,
                   count(*) AS n
              FROM per_company GROUP BY 1
        """, [as_of]).df()

        # The actual output: theme companies that survived every gate, smallest
        # first, since that is where a multibagger can still start.
        from engine.universe.theme_graph import load_theme_graph

        theme_of: dict[str, str] = {}
        for theme in load_theme_graph().themes:
            for ticker in theme.india_tickers:
                theme_of.setdefault(ticker, theme.name)

        cleared = con.execute("""
            WITH per AS (
                SELECT security_id,
                    max(CASE WHEN status='FAIL' AND gate_name IN
                        ('surveillance','cash_conversion','serial_dilution','sustained_losses')
                        THEN 1 ELSE 0 END) AS crit_fail,
                    max(CASE WHEN status='FAIL' THEN 1 ELSE 0 END) AS any_fail,
                    max(CASE WHEN status='UNKNOWN' AND gate_name IN
                        ('surveillance','cash_conversion','serial_dilution','sustained_losses')
                        THEN 1 ELSE 0 END) AS crit_unknown
                  FROM gate_results WHERE as_of_date = ? GROUP BY security_id)
            SELECT s.ticker, s.name, round(s.market_cap/1e7, 0) AS mcap_cr,
                   round(o.promoter_pct, 1) AS promoter_pct,
                   round(o.promoter_pledge_pct, 1) AS pledge_pct
              FROM per JOIN securities s ON s.security_id = per.security_id
              LEFT JOIN ownership_pit o ON o.security_id = per.security_id
             WHERE crit_fail = 0 AND any_fail = 0 AND crit_unknown = 0
             ORDER BY s.market_cap
        """, [as_of]).df()

        cleared["theme"] = cleared["ticker"].map(theme_of)
        in_theme = cleared[cleared["theme"].notna()].copy()

        return {
            "available": True,
            "as_of": str(as_of)[:10],
            "verdicts": {r.verdict: int(r.n) for r in verdicts.itertuples()},
            "cleared_total": int(len(cleared)),
            "cleared_theme": [
                {
                    "ticker": r.ticker.replace(".NS", ""),
                    "theme": r.theme,
                    "mcap_cr": None if pd.isna(r.mcap_cr) else float(r.mcap_cr),
                    "promoter_pct": None if pd.isna(r.promoter_pct) else float(r.promoter_pct),
                    "pledge_pct": None if pd.isna(r.pledge_pct) else float(r.pledge_pct),
                }
                for r in in_theme.head(24).itertuples()
            ],
            "companies": int(con.execute(
                "SELECT count(DISTINCT security_id) FROM gate_results WHERE as_of_date = ?",
                [as_of]).fetchone()[0]),
            "per_gate": per_gate.to_dict("records"),
            "rejected": rejected.head(40).to_dict("records"),
            "rejected_total": int(rejected.ticker.nunique()),
        }
    finally:
        con.close()


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
