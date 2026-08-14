"""Daily / weekly / monthly trend per company.

The theme board has carried stage and confluence since Stage 1; individual
companies never did, so the app could tell you a theme was ACCELERATING while
saying nothing about whether the specific name you were looking at had already
run or was still basing.

Same classifier as themes, deliberately. A stock and a theme index are both
price series, and using one rule for both means a company's stage can be read
against its theme's stage without translating between two scales.

Relative strength is added because it is the question a theme framework raises:
if the theme is working and this name is not, that is worth seeing.
"""

from __future__ import annotations

import datetime as dt
import logging

import pandas as pd

from engine.features.trends import classify_stage, confluence_score, trend_metrics
from engine.storage import db

log = logging.getLogger(__name__)


def compute(con, as_of: dt.date | None = None, min_bars: int = 260) -> pd.DataFrame:
    """Stage, confluence and relative strength for every priced Indian security."""
    as_of = as_of or dt.date.today()

    prices = con.execute("""
        SELECT s.security_id, s.ticker, o.date, o.adj_close AS close
          FROM ohlcv o JOIN securities s ON s.security_id = o.security_id
         WHERE s.country = 'IN' AND o.date <= ?
         ORDER BY s.security_id, o.date
    """, [as_of]).df()
    if prices.empty:
        return prices
    prices["date"] = pd.to_datetime(prices["date"])

    # Benchmark: the equal-weight Indian universe itself, so relative strength
    # measures against the opportunity set actually being screened rather than
    # against a cap-weighted index dominated by names not in it.
    wide = prices.pivot_table(index="date", columns="security_id", values="close")
    benchmark = (1 + wide.pct_change().mean(axis=1)).cumprod()

    rows = []
    for security_id, group in prices.groupby("security_id"):
        series = group.set_index("date")["close"].sort_index()
        if len(series) < min_bars:
            continue

        metrics = trend_metrics(series)
        if not metrics:
            continue

        rs_12m = None
        if len(series) > 252 and len(benchmark) > 252:
            stock = series.iloc[-1] / series.iloc[-253] - 1
            bench = benchmark.iloc[-1] / benchmark.iloc[-253] - 1
            rs_12m = float((stock - bench) * 100)

        rows.append({
            "security_id": security_id,
            "ticker": group["ticker"].iloc[0],
            "as_of_date": as_of,
            "stage": classify_stage(metrics),
            "confluence": round(confluence_score(metrics), 1),
            "mom_1m": metrics.get("d_1m"),
            "mom_3m": metrics.get("w_3m"),
            "mom_12m": metrics.get("m_12m"),
            "from_high": metrics.get("from_36m_high"),
            "above_ma200": metrics.get("above_ma200"),
            "rs_12m": rs_12m,
        })

    return pd.DataFrame(rows)


def store(con, frame: pd.DataFrame) -> int:
    """Persist to trend_confluence and trend_signals."""
    if frame.empty:
        return 0

    confluence = pd.DataFrame({
        "entity_type": "SECURITY",
        "entity_id": frame["ticker"],
        "as_of_date": frame["as_of_date"],
        "score": frame["confluence"],
        "stage": frame["stage"],
    })
    con.register("staged_conf", confluence)
    con.execute("""
        DELETE FROM trend_confluence
         WHERE entity_type = 'SECURITY'
           AND (entity_id, as_of_date) IN (SELECT entity_id, as_of_date FROM staged_conf)
    """)
    con.execute("""
        INSERT INTO trend_confluence (entity_type, entity_id, as_of_date, score, stage)
        SELECT entity_type, entity_id, as_of_date, score, stage FROM staged_conf
    """)
    con.unregister("staged_conf")

    import json as _json

    signals = pd.DataFrame({
        "entity_type": "SECURITY",
        "entity_id": frame["ticker"],
        "as_of_date": frame["as_of_date"],
        "timeframe": "ALL",
        "metrics": frame.apply(lambda r: _json.dumps({
            "mom_1m": None if pd.isna(r.mom_1m) else round(float(r.mom_1m), 1),
            "mom_3m": None if pd.isna(r.mom_3m) else round(float(r.mom_3m), 1),
            "mom_12m": None if pd.isna(r.mom_12m) else round(float(r.mom_12m), 1),
            "from_high": None if pd.isna(r.from_high) else round(float(r.from_high), 1),
            "rs_12m": None if pd.isna(r.rs_12m) else round(float(r.rs_12m), 1),
            "above_ma200": bool(r.above_ma200) if pd.notna(r.above_ma200) else None,
        }), axis=1),
        "score": frame["confluence"],
    })
    con.register("staged_sig", signals)
    con.execute("""
        DELETE FROM trend_signals
         WHERE entity_type = 'SECURITY' AND timeframe = 'ALL'
           AND (entity_id, as_of_date) IN (SELECT entity_id, as_of_date FROM staged_sig)
    """)
    con.execute("""
        INSERT INTO trend_signals (entity_type, entity_id, as_of_date, timeframe, metrics, score)
        SELECT entity_type, entity_id, as_of_date, timeframe, metrics, score FROM staged_sig
    """)
    con.unregister("staged_sig")
    return len(frame)


def for_company(con, ticker: str) -> dict | None:
    """Latest trend reading for one company, for the app."""
    row = con.execute("""
        SELECT c.stage, c.score, s.metrics, c.as_of_date
          FROM trend_confluence c
          LEFT JOIN trend_signals s ON s.entity_id = c.entity_id
               AND s.as_of_date = c.as_of_date AND s.entity_type = 'SECURITY'
         WHERE c.entity_type = 'SECURITY' AND c.entity_id = ?
         ORDER BY c.as_of_date DESC LIMIT 1
    """, [ticker]).fetchone()
    if not row:
        return None

    import json as _json

    metrics = {}
    if row[2]:
        try:
            metrics = _json.loads(row[2]) if isinstance(row[2], str) else row[2]
        except Exception:  # noqa: BLE001
            metrics = {}
    return {"stage": row[0], "confluence": row[1], "as_of": str(row[3])[:10], **metrics}
