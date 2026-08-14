"""FastAPI backend for the AI-Investo app.

Serves the analytics DuckDB read-only over JSON, and hosts the installable PWA
from the same origin so the phone needs one address and no CORS dance.

Read-only by design: the pipeline writes, the API never does. A phone app that
can mutate the analytics store is a way to lose a night's ingest to a stray tap.

Run:
    uvicorn api.main:app --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pandas as pd
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from engine.config import settings
from engine.storage import db

app = FastAPI(title="AI-Investo", version="0.1.0",
              description="Personal research engine. Not investment advice.")

PWA_DIR = Path(__file__).resolve().parent.parent / "app" / "pwa"


def _read(sql: str, params: list | None = None) -> list[dict]:
    """Run a query on a read-only connection and return plain records."""
    con = db.connect(read_only=True)
    try:
        frame = con.execute(sql, params or []).df()
    finally:
        con.close()
    return json.loads(frame.to_json(orient="records", date_format="iso"))


@app.get("/api/health")
def health() -> dict:
    con = db.connect(read_only=True)
    try:
        bars, last = con.execute("SELECT count(*), max(date) FROM ohlcv").fetchone()
        scored = con.execute("SELECT count(*) FROM scores").fetchone()[0]
    finally:
        con.close()
    return {"status": "ok", "bars": int(bars), "latest_bar": str(last),
            "scored": int(scored), "served_at": dt.datetime.now().isoformat(timespec="seconds")}


@app.get("/api/today")
def today() -> dict:
    """The Today payload: pulse, themes, screen and bands.

    Served from the file the pipeline writes rather than recomputed per request —
    theme indices take about a minute to build and do not change intraday.
    """
    path = settings.REPORT_DIR / "today.json"
    if not path.exists():
        raise HTTPException(503, "Not built yet. Run: python -m engine.demo_data")
    return json.loads(path.read_text(encoding="utf-8"))


@app.get("/api/screen")
def screen(verdict: str | None = None, theme: str | None = None,
           limit: int = 200) -> list[dict]:
    """Gate verdicts per company, newest run."""
    rows = _read("""
        WITH per AS (
            SELECT security_id,
                max(CASE WHEN status='FAIL' AND gate_name IN
                    ('surveillance','cash_conversion','serial_dilution','sustained_losses')
                    THEN 1 ELSE 0 END) AS crit_fail,
                max(CASE WHEN status='FAIL' THEN 1 ELSE 0 END) AS any_fail,
                max(CASE WHEN status='UNKNOWN' AND gate_name IN
                    ('surveillance','cash_conversion','serial_dilution','sustained_losses')
                    THEN 1 ELSE 0 END) AS crit_unknown
              FROM gate_results
             WHERE as_of_date = (SELECT max(as_of_date) FROM gate_results)
             GROUP BY security_id)
        SELECT s.ticker, s.name, coalesce(s.industry,'') AS industry,
               round(s.market_cap/1e7, 0) AS mcap_cr,
               CASE WHEN crit_fail=1 THEN 'REJECTED' WHEN any_fail=1 THEN 'FLAGGED'
                    WHEN crit_unknown=1 THEN 'UNVETTED' ELSE 'CLEARED' END AS verdict,
               sc.gem_score, json_extract_string(sc.explain,'$.band') AS band,
               sc.g_score, sc.q_score, sc.m_score, sc.t_score,
               round(o.promoter_pct,1) AS promoter_pct,
               round(o.promoter_pledge_pct,1) AS pledge_pct
          FROM per
          JOIN securities s ON s.security_id = per.security_id
          LEFT JOIN scores sc ON sc.security_id = per.security_id
               AND sc.as_of_date = (SELECT max(as_of_date) FROM scores)
          LEFT JOIN ownership_pit o ON o.security_id = per.security_id
         ORDER BY s.market_cap
    """)

    if verdict:
        rows = [r for r in rows if r["verdict"] == verdict.upper()]
    if theme:
        from engine.universe.theme_graph import load_theme_graph
        graph = load_theme_graph()
        wanted = {t for th in graph.themes if th.theme_id == theme for t in th.india_tickers}
        rows = [r for r in rows if r["ticker"] in wanted]
    return rows[:limit]


@app.get("/api/company/{ticker}")
def company(ticker: str) -> dict:
    """Everything the app shows on a company page, with gate reasons verbatim."""
    symbol = ticker.upper()
    if not symbol.endswith(".NS"):
        symbol += ".NS"

    profile = _read("""
        SELECT ticker, name, coalesce(industry,'') AS industry,
               round(market_cap/1e7,0) AS mcap_cr, isin
          FROM securities WHERE ticker = ?
    """, [symbol])
    if not profile:
        raise HTTPException(404, f"{symbol} not found")

    gates = _read("""
        SELECT g.gate_name, g.status, g.detail, g.observed_value, g.threshold
          FROM gate_results g JOIN securities s ON s.security_id = g.security_id
         WHERE s.ticker = ?
           AND g.as_of_date = (SELECT max(as_of_date) FROM gate_results)
         ORDER BY CASE g.status WHEN 'FAIL' THEN 0 WHEN 'UNKNOWN' THEN 1 ELSE 2 END
    """, [symbol])

    pillars = _read("""
        SELECT sc.gem_score, json_extract_string(sc.explain,'$.band') AS band,
               sc.t_score, sc.g_score, sc.q_score, sc.d_score, sc.v_score, sc.m_score
          FROM scores sc JOIN securities s ON s.security_id = sc.security_id
         WHERE s.ticker = ? AND sc.as_of_date = (SELECT max(as_of_date) FROM scores)
    """, [symbol])

    financials = _read("""
        SELECT f.period_end, f.period_type, f.metric, f.value, f.source, f.is_pit
          FROM fundamentals_pit f JOIN securities s ON s.security_id = f.security_id
         WHERE s.ticker = ? AND f.metric IN
               ('revenue','pat','ebitda','cfo','capex','net_worth','total_debt')
         ORDER BY f.period_end DESC LIMIT 120
    """, [symbol])

    prices = _read("""
        SELECT o.date, o.adj_close AS close
          FROM ohlcv o JOIN securities s ON s.security_id = o.security_id
         WHERE s.ticker = ? AND o.date >= current_date - INTERVAL 730 DAY
         ORDER BY o.date
    """, [symbol])

    return {"profile": profile[0], "gates": gates,
            "pillars": pillars[0] if pillars else None,
            "financials": financials, "prices": prices,
            "disclaimer": "Research output. Not investment advice."}


@app.get("/api/themes")
def themes() -> list[dict]:
    path = settings.REPORT_DIR / "today.json"
    if not path.exists():
        raise HTTPException(503, "Not built yet")
    return json.loads(path.read_text(encoding="utf-8")).get("themes", [])


@app.get("/api/folio")
def folio() -> dict:
    """Holdings, thesis health, concentration and what is due.

    Read-only like the rest: positions are opened and tranches recorded from the
    CLI, deliberately. Recording a purchase is a decision, and a decision made by
    mistyping on a phone is a decision you did not mean to make.
    """
    from engine.portfolio import book

    con = book.connect(read_only=book.portfolio_path().exists())
    try:
        held = book.holdings(con)
        if held.empty:
            return {"positions": [], "xray": {"positions": 0}, "plan": [],
                    "note": "No positions yet. Open one with `investo folio open`."}

        health = con.execute("""
            SELECT position_id, health, reasons FROM thesis_health
             WHERE as_of_date = (SELECT max(as_of_date) FROM thesis_health)
        """).df()
        merged = held.merge(health, on="position_id", how="left")

        positions = json.loads(
            merged[["ticker", "tier", "theme", "cost", "value", "pnl_pct",
                    "weight_pct", "next_stage", "health", "reasons", "thesis"]]
            .to_json(orient="records")
        )
        return {"positions": positions, "xray": book.xray(con),
                "disclaimer": "Your record, not advice."}
    finally:
        con.close()


@app.get("/api/folio/plan")
def folio_plan(budget: float = 100000.0) -> list[dict]:
    """How a monthly budget would split across tranches that are due.

    Reads stored thesis health rather than recomputing it. A GET that writes is
    wrong in principle, and here it was wrong in practice too: the app loads
    this alongside /api/folio, and two concurrent connections to the same DuckDB
    file — one of them for writing — collide, so the panel silently rendered
    empty. Health is refreshed by `folio status` and by the nightly job.
    """
    from engine.portfolio import book

    con = book.connect(read_only=True)
    try:
        plan = book.deployment_plan(con, budget)
        return [] if plan.empty else json.loads(plan.to_json(orient="records"))
    finally:
        con.close()


# The PWA is served last so /api/* wins any path collision.
if PWA_DIR.exists():
    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(PWA_DIR / "index.html")

    app.mount("/", StaticFiles(directory=PWA_DIR, html=True), name="pwa")
