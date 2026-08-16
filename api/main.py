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
    con = db.connect_for_reading()
    try:
        frame = con.execute(sql, params or []).df()
    finally:
        con.close()
    return json.loads(frame.to_json(orient="records", date_format="iso"))


@app.get("/api/health")
def health() -> dict:
    con = db.connect_for_reading()
    try:
        bars, last = con.execute("SELECT count(*), max(date) FROM ohlcv").fetchone()
        # Companies in the newest run, not rows in the table. `scores` keeps
        # history, so counting all of it reported 1,520 "scored" for a universe
        # of 760 the first time the job ran twice.
        scored = con.execute("""
            SELECT count(*) FROM scores
             WHERE as_of_date = (SELECT max(as_of_date) FROM scores)
        """).fetchone()[0]
        run = con.execute("""
            SELECT run_id, min(started_at) AS started_at, count(*) AS stages,
                   sum(CASE WHEN status = 'FAILED' THEN 1 ELSE 0 END) AS failed,
                   string_agg(CASE WHEN status = 'FAILED' THEN stage END, ', ') AS failed_stages
              FROM pipeline_runs
             WHERE run_id = (SELECT run_id FROM pipeline_runs
                              ORDER BY started_at DESC LIMIT 1)
             GROUP BY run_id
        """).df()
    finally:
        con.close()

    # A pipeline that can fail invisibly is one you have to remember to audit.
    # Two stages failed on every run for an unknown period before this existed.
    pipeline = {"ran": False}
    if not run.empty:
        row = run.iloc[0]
        started = pd.Timestamp(row["started_at"])
        pipeline = {
            "ran": True,
            "run_id": str(row["run_id"]),
            "started_at": started.isoformat(timespec="seconds"),
            "age_hours": round((pd.Timestamp.now() - started).total_seconds() / 3600, 1),
            "stages": int(row["stages"]),
            "failed": int(row["failed"] or 0),
            "failed_stages": row["failed_stages"] or "",
        }

    stale_days = None
    if last is not None:
        stale_days = (dt.date.today() - pd.Timestamp(last).date()).days

    return {"status": "ok", "bars": int(bars), "latest_bar": str(last),
            "data_age_days": stale_days, "scored": int(scored),
            "pipeline": pipeline,
            "served_at": dt.datetime.now().isoformat(timespec="seconds")}


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
          LEFT JOIN ownership_latest o ON o.security_id = per.security_id
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

    from engine.features import security_trend
    from engine.orders import company_orders

    con = db.connect_for_reading()
    try:
        trend = security_trend.for_company(con, symbol)
        orders = company_orders(con, symbol)
    finally:
        con.close()

    return {"profile": profile[0], "gates": gates,
            "pillars": pillars[0] if pillars else None,
            "trend": trend, "orders": orders,
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

        # Latest review PER POSITION, and its date. A global max returns only
        # rows from the newest review run, so a position last looked at months
        # ago vanishes from the join and renders with no health at all -- the
        # same defect the deployment gate had, where it read as permission.
        health = con.execute("""
            SELECT position_id, health, reasons, as_of_date AS health_as_of
              FROM (
                SELECT position_id, health, reasons, as_of_date,
                       row_number() OVER (PARTITION BY position_id
                                          ORDER BY as_of_date DESC) AS rn
                  FROM thesis_health
              ) WHERE rn = 1
        """).df()
        merged = held.merge(health, on="position_id", how="left")

        # How old the verdict is, so the screen can say so. A thesis reviewed
        # six months ago is not a thesis that passed today.
        age = (pd.Timestamp(dt.date.today()) - pd.to_datetime(merged["health_as_of"])).dt.days
        merged["health_age_days"] = age
        merged["health_stale"] = ~age.between(0, book.HEALTH_MAX_AGE_DAYS)

        # Dates as ISO strings, not pandas' epoch default -- the screen prints
        # these, and "1786752000" is not a date anyone can read.
        for column in ("health_as_of", "opened_on"):
            merged[column] = (pd.to_datetime(merged[column])
                              .dt.strftime("%Y-%m-%d").where(merged[column].notna()))

        positions = json.loads(
            merged[["position_id", "ticker", "tier", "theme", "cost", "value",
                    "pnl_pct", "weight_pct", "next_stage", "health", "reasons",
                    "thesis", "opened_on", "health_as_of", "health_age_days",
                    "health_stale"]]
            .to_json(orient="records")
        )

        # Claims travel WITH the position, holding ones included. A thesis that
        # is still true is the evidence for staying in, and it was being
        # measured nightly and shown nowhere.
        claims = book.claim_status(con)
        by_position: dict[int, list] = {}
        if not claims.empty:
            claims["observed"] = claims["observed"].astype(object).where(
                claims["observed"].notna(), None)
            for record in json.loads(claims.to_json(orient="records",
                                                    date_format="iso")):
                by_position.setdefault(record["position_id"], []).append(record)
        for position in positions:
            position["claims"] = by_position.get(position["position_id"], [])
            # An empty list is not the same as claims that all hold, and the
            # screen has to be able to tell them apart.
            position["falsifiable"] = bool(position["claims"])

        return {"positions": positions, "xray": book.xray(con),
                "health_max_age_days": book.HEALTH_MAX_AGE_DAYS,
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
