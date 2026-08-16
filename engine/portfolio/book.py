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
import shutil
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


# Tables whose row counts are compared against the copy. A backup that exists
# but cannot be opened is worse than none, because it stops you looking for one.
BACKED_UP_TABLES = ("positions", "tranches", "journal", "thesis_claims",
                    "thesis_claim_checks", "thesis_health")
BACKUP_SETTING = "backup_dir"


def backup_destination(con=None) -> Path:
    """Where backups go: the remembered folder, else one beside the data dir.

    The default is honest but weak -- a second copy on the same disk survives a
    mistake and not a disk failure. `--to` records a real destination in
    folio_settings so it is chosen once rather than remembered every time.
    """
    if con is not None:
        row = con.execute("SELECT value FROM folio_settings WHERE key = ?",
                          [BACKUP_SETTING]).fetchone()
        if row and row[0]:
            return Path(row[0])
    return settings.DATA_DIR / "backups"


def backup(destination: Path | None = None, keep: int = 10) -> dict:
    """Copy the portfolio to a timestamped file, and prove the copy is readable.

    TAKES NO CONNECTION, deliberately. DuckDB holds the file open and Windows
    refuses to read a file a writer has open -- which is precisely how the
    analytics snapshot failed on its first unattended run. Callers must have
    closed their write connection first.

    Everything in the analytics store rebuilds from providers in an afternoon.
    Nothing in here rebuilds at all: it is the positions, the theses, the claims
    and the history of what was true when. The schema has said "back it up" since
    it was written and nothing did.
    """
    source = portfolio_path()
    if not source.exists():
        return {"ok": False, "reason": "no portfolio to back up"}

    if destination is None:
        con = connect(read_only=True)
        try:
            destination = backup_destination(con)
        finally:
            con.close()
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)

    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    target = destination / f"portfolio-{stamp}.duckdb"
    shutil.copy2(source, target)

    # Verify by OPENING the copy and comparing row counts, not by checking the
    # file exists. A truncated copy has a size and a timestamp too.
    #
    # Opening it can raise outright -- DuckDB rejects a file that is not a
    # database at all -- and that has to come back as a failed backup rather
    # than a traceback, or the nightly stage dies and the bad file survives
    # looking like a good one.
    expected = _table_counts(source)
    try:
        actual = _table_counts(target)
    except Exception as exc:  # noqa: BLE001 - any unreadable copy is a failed backup
        target.unlink(missing_ok=True)
        return {"ok": False, "reason": f"copy would not open: {exc}"}
    if expected != actual:
        target.unlink(missing_ok=True)
        return {"ok": False, "reason": f"copy did not verify: {expected} vs {actual}"}

    pruned = _prune_backups(destination, keep)
    return {"ok": True, "path": target, "rows": sum(expected.values()),
            "tables": expected, "pruned": pruned,
            "bytes": target.stat().st_size}


def _table_counts(path: Path) -> dict[str, int]:
    con = duckdb.connect(str(path), read_only=True)
    try:
        return {
            table: int(con.execute(f"SELECT count(*) FROM {table}").fetchone()[0])
            for table in BACKED_UP_TABLES
        }
    finally:
        con.close()


def _prune_backups(destination: Path, keep: int) -> int:
    """Keep the newest `keep`, delete the rest. Zero means keep everything."""
    if keep <= 0:
        return 0
    existing = sorted(destination.glob("portfolio-*.duckdb"),
                      key=lambda p: p.name, reverse=True)
    for old in existing[keep:]:
        old.unlink(missing_ok=True)
    return max(0, len(existing) - keep)


def set_backup_destination(con, path: str) -> None:
    """Remember where backups go, so it is chosen once."""
    con.execute("""
        INSERT INTO folio_settings (key, value) VALUES (?, ?)
        ON CONFLICT (key) DO UPDATE SET value = excluded.value
    """, [BACKUP_SETTING, str(Path(path).expanduser())])


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


# ------------------------------------------------------------- thesis claims
def add_claim(con, ticker: str, metric: str, comparator: str, threshold: float,
              note: str = "") -> int:
    """Record one falsifiable assertion against the newest open position."""
    from engine.portfolio import claims as claim_engine

    if metric not in claim_engine.MEASURES:
        raise ValueError(
            f"unknown metric {metric!r}; the engine can measure: "
            + ", ".join(sorted(claim_engine.MEASURES))
        )
    if comparator not in claim_engine.COMPARATORS:
        raise ValueError(f"comparator must be one of {claim_engine.COMPARATORS}")

    ticker = ticker.upper()
    if not ticker.endswith(".NS"):
        ticker += ".NS"
    row = con.execute("""
        SELECT position_id FROM positions
         WHERE ticker = ? AND status = 'OPEN' ORDER BY opened_on DESC LIMIT 1
    """, [ticker]).fetchone()
    if not row:
        raise ValueError(f"no open position for {ticker}")

    # Recording a claim on a metric that already has one is a REVISION, not an
    # error: you changed your mind about the threshold, which is the normal way
    # a thesis evolves. The live claim is retired so the change is on the
    # record, and any same-day entry is replaced outright -- the uniqueness key
    # includes created_on, so without this a revision (or re-adding after
    # retiring) fails with a constraint violation instead of doing the obvious
    # thing.
    today = dt.date.today()
    position_id = int(row[0])
    con.execute("""
        UPDATE thesis_claims SET retired_on = ?
         WHERE position_id = ? AND metric = ? AND retired_on IS NULL
    """, [today, position_id, metric])
    con.execute("""
        DELETE FROM thesis_claims
         WHERE position_id = ? AND metric = ? AND created_on = ?
    """, [position_id, metric, today])
    con.execute("""
        INSERT INTO thesis_claims (position_id, metric, comparator, threshold,
                                   note, created_on)
        VALUES (?, ?, ?, ?, ?, ?)
    """, [position_id, metric, comparator, float(threshold), note or None, today])
    return position_id


def claims_for(con, position_id: int) -> list:
    """Live claims on a position, newest first. Retired ones are kept, not shown."""
    from engine.portfolio import claims as claim_engine

    frame = con.execute("""
        SELECT claim_id, metric, comparator, threshold, coalesce(note, '') AS note
          FROM thesis_claims
         WHERE position_id = ? AND retired_on IS NULL
         ORDER BY created_on DESC, claim_id DESC
    """, [position_id]).df()
    return [
        claim_engine.Claim(metric=r.metric, comparator=r.comparator,
                           threshold=float(r.threshold), note=r.note,
                           claim_id=int(r.claim_id))
        for r in frame.itertuples()
    ]


def claim_status(con) -> pd.DataFrame:
    """Every live claim with its most recent check.

    Claims used to surface only when they BROKE, folded into the reasons string.
    A thesis holding is evidence too -- WABAG's order book sitting at 5x revenue
    is the reason the position exists, measured nightly and shown nowhere.
    """
    return con.execute("""
        SELECT c.position_id, c.claim_id, c.metric, c.comparator, c.threshold,
               coalesce(c.note, '') AS note,
               k.status, k.observed, k.detail, k.as_of_date AS checked_on
          FROM thesis_claims c
          LEFT JOIN (
            SELECT claim_id, status, observed, detail, as_of_date FROM (
                SELECT *, row_number() OVER (PARTITION BY claim_id
                                             ORDER BY as_of_date DESC) AS rn
                  FROM thesis_claim_checks
            ) WHERE rn = 1
          ) k ON k.claim_id = c.claim_id
         WHERE c.retired_on IS NULL
         ORDER BY c.position_id, c.metric
    """).df()


def retire_claim(con, claim_id: int) -> None:
    """Stop testing a claim without deleting it: a belief you abandoned is
    part of the record of your thinking, and the reason it was abandoned is
    usually more interesting than the claim."""
    con.execute("UPDATE thesis_claims SET retired_on = ? WHERE claim_id = ?",
                [dt.date.today(), claim_id])


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
    from engine.portfolio import claims as claim_engine
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
        rows, claim_rows = _review_positions(con, acon, positions, as_of, claim_engine)
    finally:
        if owns_analytics:
            acon.close()

    frame = pd.DataFrame(rows)
    _store_health(con, frame, claim_rows)
    return frame


def _review_positions(con, acon, positions, as_of, claim_engine):
    """The review itself, with the analytics connection held open throughout.

    Claims are measured per position, so the connection has to survive the loop
    -- it used to be closed as soon as the gates and bands were read.
    """
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
    acon_measure = acon

    band_of = dict(zip(bands.get("ticker", []), bands.get("band", [])))
    rows, claim_rows = [], []
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

        # YOUR reasons, not the generic ones. Everything above tests whether the
        # company still looks acceptable; this tests whether the specific thing
        # you believed is still happening, which is the failure that otherwise
        # occurs in silence.
        position_claims = claims_for(con, record.position_id)
        checks = []
        if position_claims:
            measured = claim_engine.measure(acon_measure, record.ticker, as_of)
            checks = claim_engine.check(position_claims, measured)
            claim_health, claim_reasons = claim_engine.health_from_checks(checks)
            reasons += claim_reasons
            if claim_health == "RED":
                health = "RED"
            elif claim_health == "AMBER" and health == "GREEN":
                health = "AMBER"

            unmeasurable = [c for c in checks if c.status == claim_engine.UNCHECKABLE]
            if unmeasurable and health == "GREEN":
                reasons.append(
                    f"{len(unmeasurable)} of {len(checks)} thesis claims cannot be "
                    "checked from current data"
                )
        claim_rows.extend(
            {"claim_id": c.claim.claim_id, "position_id": record.position_id,
             "as_of_date": as_of, "status": c.status, "observed": c.observed,
             "detail": c.detail[:500]}
            for c in checks if c.claim.claim_id is not None
        )

        rows.append({
            "position_id": record.position_id, "ticker": record.ticker,
            "as_of_date": as_of, "health": health,
            "reasons": "; ".join(reasons)[:500], "gem_band": band,
            "verdict": "REJECTED" if not critical.empty else
                       ("FLAGGED" if not failures.empty else "CLEARED"),
        })

    return rows, claim_rows


def _store_health(con, frame: pd.DataFrame, claim_rows: list[dict]) -> None:
    """Persist the review and the per-claim checks behind it."""
    if frame.empty:
        return

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

    if not claim_rows:
        return
    checks = pd.DataFrame(claim_rows)
    con.register("staged_checks", checks)
    con.execute("""
        DELETE FROM thesis_claim_checks WHERE (claim_id, as_of_date) IN
            (SELECT claim_id, as_of_date FROM staged_checks)
    """)
    con.execute("""
        INSERT INTO thesis_claim_checks
            (claim_id, position_id, as_of_date, status, observed, detail)
        SELECT claim_id, position_id, as_of_date, status, observed, detail
          FROM staged_checks
    """)
    con.unregister("staged_checks")


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
