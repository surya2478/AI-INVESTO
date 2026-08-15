"""Portfolio book: positions, the staged ladder, thesis health and the X-ray.

Built around accumulation rather than trading, because the stated horizon is
five to ten years and the hard part over that span is not picking — it is
deciding what to do next month, and noticing when a reason you bought has
stopped being true.

Three ideas do the work:

* A position is opened as an INTENTION, before money moves, and a watchlist
  entry is tracked exactly like a held one. That way you find out whether your
  reasoning was right, not merely whether the trade was.
* Buying is staged. The ladder exists as PLANNED rows so the app can show what
  is DUE, and each tranche records the condition that should hold before it is
  taken.
* Exits are triggered by THESIS BREAK, not by price. A position is reviewed
  against the gates and the score band it was bought under; a fall in price is
  not itself a reason, and a gate newly failing is.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
from pathlib import Path

import duckdb
import pandas as pd

from engine.config import settings

log = logging.getLogger(__name__)

SCHEMA = Path(__file__).resolve().parent.parent / "storage" / "portfolio_schema.sql"

TIERS = {"CORE": 4.0, "SATELLITE": 2.0, "WATCHLIST": 0.0}

# How old a thesis review may be and still gate a purchase. `review_thesis` runs
# nightly, so anything past a fortnight means the pipeline has been failing and
# nobody noticed -- exactly the state in which the last known health is least
# trustworthy. Money does not move on a stale verdict.
HEALTH_MAX_AGE_DAYS = 14

# The ladder from the spec: buy on signal, add when the next result confirms the
# thesis, add again on confirmation or into weakness while the thesis holds.
LADDER = [
    (1, 40.0, "Opening position — gates clear and thesis written"),
    (2, 30.0, "Next quarterly result confirms the growth thesis"),
    (3, 30.0, "Breakout confirmation, or add into a drawdown with thesis intact"),
]


def portfolio_path() -> Path:
    return settings.DATA_DIR / "db" / "portfolio.duckdb"


def connect(read_only: bool = False) -> duckdb.DuckDBPyConnection:
    """Open the portfolio store, applying its schema."""
    path = portfolio_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(path), read_only=read_only)
    if not read_only:
        con.execute(SCHEMA.read_text(encoding="utf-8"))
    return con


# ------------------------------------------------------------------ positions
def open_position(con, ticker: str, tier: str, thesis: str,
                  theme: str | None = None, target_weight: float | None = None,
                  opened_on: dt.date | None = None) -> int:
    """Record an intention and lay out its ladder."""
    tier = tier.upper()
    if tier not in TIERS:
        raise ValueError(f"tier must be one of {sorted(TIERS)}")
    if not thesis.strip():
        raise ValueError("a position needs a thesis — that is the point of it")

    ticker = ticker.upper()
    if not ticker.endswith(".NS"):
        ticker += ".NS"
    opened_on = opened_on or dt.date.today()
    weight = target_weight if target_weight is not None else TIERS[tier]

    con.execute("""
        INSERT INTO positions (ticker, tier, target_weight_pct, thesis, theme, opened_on)
        VALUES (?, ?, ?, ?, ?, ?)
    """, [ticker, tier, weight, thesis.strip(), theme, opened_on])

    position_id = con.execute(
        "SELECT position_id FROM positions WHERE ticker = ? AND opened_on = ?",
        [ticker, opened_on]).fetchone()[0]

    for stage, pct, trigger in LADDER:
        con.execute("""
            INSERT INTO tranches (position_id, stage, planned_pct, trigger)
            VALUES (?, ?, ?, ?)
        """, [position_id, stage, pct, trigger])

    con.execute("""
        INSERT INTO journal (position_id, ticker, entry_date, kind, body)
        VALUES (?, ?, ?, 'THESIS', ?)
    """, [position_id, ticker, opened_on, thesis.strip()])

    return int(position_id)


def record_buy(con, ticker: str, stage: int, shares: float, price: float,
               executed_on: dt.date | None = None, note: str | None = None) -> None:
    """Mark a tranche executed."""
    ticker = ticker.upper()
    if not ticker.endswith(".NS"):
        ticker += ".NS"
    row = con.execute("""
        SELECT position_id FROM positions
         WHERE ticker = ? AND status = 'OPEN' ORDER BY opened_on DESC LIMIT 1
    """, [ticker]).fetchone()
    if not row:
        raise ValueError(f"no open position for {ticker}")

    con.execute("""
        UPDATE tranches
           SET status = 'EXECUTED', executed_on = ?, shares = ?, price = ?,
               amount = ?, note = ?
         WHERE position_id = ? AND stage = ?
    """, [executed_on or dt.date.today(), shares, price, shares * price,
          note, row[0], stage])


def holdings(con, analytics_db: Path | None = None) -> pd.DataFrame:
    """Positions with cost, current value and the next tranche due."""
    positions = con.execute("""
        SELECT p.position_id, p.ticker, p.tier, p.target_weight_pct, p.theme,
               p.thesis, p.opened_on, p.status,
               coalesce(sum(CASE WHEN t.status='EXECUTED' THEN t.amount END), 0) AS cost,
               coalesce(sum(CASE WHEN t.status='EXECUTED' THEN t.shares END), 0) AS shares,
               min(CASE WHEN t.status='PLANNED' THEN t.stage END) AS next_stage
          FROM positions p LEFT JOIN tranches t ON t.position_id = p.position_id
         WHERE p.status = 'OPEN'
         GROUP BY ALL ORDER BY p.ticker
    """).df()
    if positions.empty:
        return positions

    prices = _latest_prices(positions["ticker"].tolist(), analytics_db)
    positions["price"] = positions["ticker"].map(prices)
    positions["value"] = positions["shares"] * positions["price"]
    positions["pnl_pct"] = ((positions["value"] / positions["cost"] - 1) * 100).where(
        positions["cost"] > 0)
    total = positions["value"].sum()
    positions["weight_pct"] = (positions["value"] / total * 100) if total else 0.0
    return positions


def _latest_prices(tickers: list[str], analytics_db: Path | None = None) -> dict:
    """Last close per ticker, read from the analytics store."""
    from engine.storage import db as analytics

    if not tickers:
        return {}
    # The serving snapshot: this runs inside API requests, and opening the live
    # database here means the folio screen dies whenever the pipeline writes.
    con = analytics.connect_for_reading()
    try:
        placeholders = ",".join("?" * len(tickers))
        frame = con.execute(f"""
            SELECT s.ticker, o.adj_close AS price
              FROM ohlcv o JOIN securities s ON s.security_id = o.security_id
             WHERE s.ticker IN ({placeholders})
               AND o.date = (SELECT max(date) FROM ohlcv o2
                              WHERE o2.security_id = o.security_id)
        """, tickers).df()
    finally:
        con.close()
    return dict(zip(frame["ticker"], frame["price"]))


# -------------------------------------------------------------- thesis health
def review_thesis(con, as_of: dt.date | None = None, analytics_con=None) -> pd.DataFrame:
    """Score each open position GREEN / AMBER / RED against current evidence.

    Exits are thesis-driven, so nothing here looks at price. A holding that has
    halved with every gate still passing is not a sell signal; one that is up
    with cash conversion newly failing is.

    `analytics_con` lets a caller lend its own analytics connection. DuckDB will
    not open a second connection to a file with a different configuration inside
    one process, so opening a read-only one here while the nightly job holds a
    write connection raises -- which is why this stage had never once succeeded
    in the night, leaving thesis health to whatever last ran it by hand.
    """
    from engine.storage import db as analytics

    as_of = as_of or dt.date.today()
    positions = con.execute(
        "SELECT position_id, ticker, tier, opened_on FROM positions WHERE status='OPEN'"
    ).df()
    if positions.empty:
        return positions

    owns_analytics = analytics_con is None
    acon = analytics_con if analytics_con is not None else analytics.connect(read_only=True)
    try:
        gates = acon.execute("""
            SELECT s.ticker, g.gate_name, g.status, g.detail
              FROM gate_results g JOIN securities s ON s.security_id = g.security_id
             WHERE g.as_of_date = (SELECT max(as_of_date) FROM gate_results)
        """).df()
        bands = acon.execute("""
            SELECT s.ticker, json_extract_string(sc.explain,'$.band') AS band
              FROM scores sc JOIN securities s ON s.security_id = sc.security_id
             WHERE sc.as_of_date = (SELECT max(as_of_date) FROM scores)
        """).df()
    finally:
        if owns_analytics:
            acon.close()

    band_of = dict(zip(bands.get("ticker", []), bands.get("band", [])))
    rows = []
    CRITICAL = {"surveillance", "cash_conversion", "serial_dilution", "sustained_losses"}

    for record in positions.itertuples():
        mine = gates[gates.ticker == record.ticker]
        failures = mine[mine.status == "FAIL"]
        critical = failures[failures.gate_name.isin(CRITICAL)]
        band = band_of.get(record.ticker)

        reasons, health = [], "GREEN"
        if not critical.empty:
            health = "RED"
            reasons += list(critical.detail.head(2))
        elif not failures.empty:
            health = "AMBER"
            reasons += list(failures.detail.head(2))

        if band == "LOWER" and health == "GREEN":
            health = "AMBER"
            reasons.append("Score band has fallen to the lower third of the universe")
        if mine.empty:
            health = "AMBER"
            reasons.append("No current screen for this company — cannot verify the thesis")

        rows.append({
            "position_id": record.position_id, "ticker": record.ticker,
            "as_of_date": as_of, "health": health,
            "reasons": "; ".join(reasons)[:500], "gem_band": band,
            "verdict": "REJECTED" if not critical.empty else
                       ("FLAGGED" if not failures.empty else "CLEARED"),
        })

    frame = pd.DataFrame(rows)
    con.register("staged_health", frame)
    con.execute("""
        DELETE FROM thesis_health WHERE (position_id, as_of_date) IN
            (SELECT position_id, as_of_date FROM staged_health)
    """)
    con.execute("""
        INSERT INTO thesis_health (position_id, as_of_date, health, reasons, gem_band, verdict)
        SELECT position_id, as_of_date, health, reasons, gem_band, verdict FROM staged_health
    """)
    con.unregister("staged_health")
    return frame


# ------------------------------------------------------------------- planner
def deployment_plan(con, budget: float, analytics_db: Path | None = None,
                    as_of: dt.date | None = None) -> pd.DataFrame:
    """Split this month's money across the tranches that are actually due.

    Only positions whose thesis is currently GREEN receive money. Adding to a
    holding whose thesis has broken is the single most expensive habit this tool
    exists to interrupt.

    The health check FAILS CLOSED. A position is funded only on a positive,
    recent verdict; three states are all treated as "no":

      * health is RED or AMBER      -- the thesis is in question,
      * no health row at all        -- opened since the last review, never vetted,
      * health older than HEALTH_MAX_AGE_DAYS -- the verdict predates the
        evidence it was supposed to be checked against.

    Absence of a warning is not a clearance. Defaulting the missing case to GREEN
    would let a position that has never been reviewed draw money on its first
    month, which is the one case this function exists to prevent.
    """
    as_of = as_of or dt.date.today()
    book = holdings(con, analytics_db)
    if book.empty:
        return book

    # Latest review PER POSITION, not the newest review in the table: a global
    # max would silently treat one position's fresh verdict as evidence about
    # another that has not been looked at in months.
    health = con.execute("""
        SELECT position_id, health, as_of_date AS health_as_of
          FROM (
            SELECT position_id, health, as_of_date,
                   row_number() OVER (PARTITION BY position_id
                                      ORDER BY as_of_date DESC) AS rn
              FROM thesis_health
          ) WHERE rn = 1
    """).df()
    book = book.merge(health, on="position_id", how="left")

    age_days = (pd.Timestamp(as_of) - pd.to_datetime(book.get("health_as_of"))).dt.days
    book["health_age_days"] = age_days
    fresh = age_days.between(0, HEALTH_MAX_AGE_DAYS)     # NaN -> False, fails closed
    eligible = book["health"].eq("GREEN") & fresh        # NaN -> False, fails closed

    held_back = book[book["next_stage"].notna() & ~eligible]
    for row in held_back.itertuples():
        if pd.isna(row.health):
            why = "never reviewed"
        elif pd.isna(row.health_age_days):
            why = "review has no date"
        elif not (0 <= row.health_age_days <= HEALTH_MAX_AGE_DAYS):
            why = f"review is {int(row.health_age_days)} days old"
        else:
            why = f"thesis health is {row.health}"
        log.warning("holding back %s: %s", row.ticker, why)

    due = book[book["next_stage"].notna() & eligible].copy()
    if due.empty:
        return due

    tranche = con.execute("""
        SELECT position_id, stage, planned_pct, trigger FROM tranches WHERE status='PLANNED'
    """).df()
    due = due.merge(tranche, left_on=["position_id", "next_stage"],
                    right_on=["position_id", "stage"], how="left")

    # Weight by tier and by how much of the target weight is still unfilled.
    total_value = max(book["value"].sum(), 1.0)
    due["target_value"] = due["target_weight_pct"] / 100.0 * total_value
    due["gap"] = (due["target_value"] - due["value"]).clip(lower=0)
    weights = due["gap"] * due["planned_pct"]
    if weights.sum() <= 0:
        weights = pd.Series(1.0, index=due.index)
    due["allocate"] = budget * weights / weights.sum()

    return due[["ticker", "tier", "stage", "trigger", "value", "target_value",
                "allocate", "health", "health_as_of"]].sort_values("allocate", ascending=False)


def xray(con, analytics_db: Path | None = None) -> dict:
    """Concentration checks: are ten ideas really three bets?"""
    book = holdings(con, analytics_db)
    if book.empty:
        return {"positions": 0}

    total = book["value"].sum()
    by_theme = (book.groupby(book["theme"].fillna("unassigned"))["value"]
                .sum().sort_values(ascending=False) / max(total, 1) * 100)
    by_tier = book.groupby("tier")["value"].sum() / max(total, 1) * 100

    return {
        "positions": int(len(book)),
        "invested": float(book["cost"].sum()),
        "value": float(total),
        "pnl_pct": float((total / book["cost"].sum() - 1) * 100) if book["cost"].sum() else None,
        "theme_concentration": by_theme.round(1).to_dict(),
        "largest_theme_pct": float(by_theme.iloc[0]) if len(by_theme) else 0.0,
        "tier_split": by_tier.round(1).to_dict(),
        "ladders_incomplete": int(book["next_stage"].notna().sum()),
    }


def add_journal(con, ticker: str, body: str, kind: str = "NOTE",
                claims: dict | None = None) -> None:
    ticker = ticker.upper()
    if not ticker.endswith(".NS"):
        ticker += ".NS"
    row = con.execute(
        "SELECT position_id FROM positions WHERE ticker = ? ORDER BY opened_on DESC LIMIT 1",
        [ticker]).fetchone()
    con.execute("""
        INSERT INTO journal (position_id, ticker, entry_date, kind, body, claims)
        VALUES (?, ?, ?, ?, ?, ?)
    """, [row[0] if row else None, ticker, dt.date.today(), kind.upper(),
          body.strip(), json.dumps(claims) if claims else None])
