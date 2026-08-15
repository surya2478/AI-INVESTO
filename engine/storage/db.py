"""DuckDB access layer.

The important thing in this module is `fundamentals_asof`. Reported financials
must only ever be read through a filter on `filing_date`, otherwise a backtest
sees numbers before they were published and reports an edge that does not exist.
Every historical read goes through here so that rule lives in exactly one place.
"""

from __future__ import annotations

import datetime as dt
import logging
import os
import shutil
from pathlib import Path

import duckdb
import pandas as pd

from engine.config import settings

log = logging.getLogger(__name__)

SCHEMA_PATH = Path(__file__).with_name("schema.sql")


def connect(read_only: bool = False) -> duckdb.DuckDBPyConnection:
    """Open the analytics database, creating and migrating it if needed."""
    settings.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    fresh = not settings.DB_PATH.exists()
    con = duckdb.connect(str(settings.DB_PATH), read_only=read_only)
    if not read_only:
        con.execute(SCHEMA_PATH.read_text(encoding="utf-8"))
    elif fresh:
        raise FileNotFoundError(
            f"No database at {settings.DB_PATH}. Run `investo init` first."
        )
    return con


def publish_snapshot() -> Path | None:
    """Copy the analytics database to the file readers serve from.

    DuckDB gives a writer an EXCLUSIVE lock on the file, so while the nightly
    job runs, every read-only connection fails outright -- the API returns 500
    on every data endpoint for the duration, which is twenty minutes to an hour.
    That is not a race to be retried around; it is the storage engine working as
    designed.

    So writers own `investo.duckdb` and readers own a copy of it. The copy is
    replaced atomically at the end of a run, which is the only moment the file
    is consistent anyway: a reader either sees the whole previous snapshot or
    the whole new one, never a half-written night.

    Returns the snapshot path, or None if the replace could not complete because
    a reader still had it open -- in which case the previous snapshot stays in
    place and is served instead. Stale is recoverable; a locked database is not.
    """
    source = settings.DB_PATH
    if not source.exists():
        return None

    target = settings.SERVE_DB_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    staging = target.with_suffix(".tmp")

    shutil.copy2(source, staging)
    try:
        os.replace(staging, target)
    except OSError as exc:
        # Windows refuses to replace a file another process holds open. Keep the
        # snapshot that is already there rather than leaving readers with none.
        log.warning("snapshot in use, keeping the previous one: %s", exc)
        staging.unlink(missing_ok=True)
        return target if target.exists() else None
    return target


def connect_for_reading() -> duckdb.DuckDBPyConnection:
    """Open the serving snapshot, falling back to the live database.

    Every read path that a person waits on should use this. The fallback keeps a
    fresh install working before any snapshot exists, at the cost of being
    blocked while a write runs -- which is exactly the behaviour the snapshot
    exists to remove, so it is a starting condition, not a steady state.
    """
    snapshot = settings.SERVE_DB_PATH
    if snapshot.exists():
        return duckdb.connect(str(snapshot), read_only=True)
    return connect(read_only=True)


def init_db() -> Path:
    """Create the database and apply the schema. Idempotent."""
    con = connect()
    try:
        con.execute(SCHEMA_PATH.read_text(encoding="utf-8"))
    finally:
        con.close()
    return settings.DB_PATH


# --------------------------------------------------------------- security ids
def upsert_securities(con: duckdb.DuckDBPyConnection, frame: pd.DataFrame) -> int:
    """Insert new securities, refresh mutable fields on existing ones.

    Matches on (ticker, exchange). Returns the number of rows written.
    """
    if frame.empty:
        return 0

    cols = [
        "ticker", "exchange_symbol", "isin", "name", "exchange", "country",
        "currency", "sector", "industry", "market_cap", "listing_date",
        "delisted_date", "is_active", "source",
    ]
    staged = frame.reindex(columns=cols)
    con.register("staged_securities", staged)

    con.execute("""
        INSERT INTO securities
            (ticker, exchange_symbol, isin, name, exchange, country, currency,
             sector, industry, market_cap, listing_date, delisted_date,
             is_active, source)
        SELECT s.ticker, s.exchange_symbol, s.isin, s.name, s.exchange, s.country,
               s.currency, s.sector, s.industry, s.market_cap, s.listing_date,
               s.delisted_date, coalesce(s.is_active, TRUE), s.source
        FROM staged_securities s
        LEFT JOIN securities e
               ON e.ticker = s.ticker AND e.exchange = s.exchange
        WHERE e.security_id IS NULL
    """)

    # Refresh fields that legitimately change; never overwrite identity columns.
    con.execute("""
        UPDATE securities e
           SET name          = coalesce(s.name, e.name),
               sector        = coalesce(s.sector, e.sector),
               industry      = coalesce(s.industry, e.industry),
               market_cap    = coalesce(s.market_cap, e.market_cap),
               isin          = coalesce(s.isin, e.isin),
               delisted_date = coalesce(s.delisted_date, e.delisted_date),
               is_active     = coalesce(s.is_active, e.is_active),
               updated_at    = current_timestamp
          FROM staged_securities s
         WHERE e.ticker = s.ticker AND e.exchange = s.exchange
    """)

    con.unregister("staged_securities")
    return len(staged)


def security_map(con: duckdb.DuckDBPyConnection) -> dict[str, int]:
    """Map ticker -> security_id for every known security."""
    rows = con.execute("SELECT ticker, security_id FROM securities").fetchall()
    return {ticker: sid for ticker, sid in rows}


def resolve_ticker(con: duckdb.DuckDBPyConnection, ticker: str) -> int | None:
    row = con.execute(
        "SELECT security_id FROM securities WHERE ticker = ?", [ticker]
    ).fetchone()
    return row[0] if row else None


# ------------------------------------------------------------------- price io
def upsert_ohlcv(con: duckdb.DuckDBPyConnection, frame: pd.DataFrame) -> int:
    """Write daily bars, replacing any existing row for the same (security, date).

    Replace-on-conflict rather than ignore, because split/bonus adjustments
    legitimately restate historical closes for Indian names.
    """
    if frame.empty:
        return 0
    con.register("staged_ohlcv", frame)
    con.execute("""
        DELETE FROM ohlcv
        WHERE (security_id, date) IN (
            SELECT security_id, date FROM staged_ohlcv
        )
    """)
    con.execute("""
        INSERT INTO ohlcv (security_id, date, open, high, low, close,
                           adj_close, volume, source)
        SELECT security_id, date, open, high, low, close,
               adj_close, volume, source
        FROM staged_ohlcv
    """)
    con.unregister("staged_ohlcv")
    return len(frame)


def price_history(
    con: duckdb.DuckDBPyConnection,
    security_ids: list[int] | None = None,
    start: dt.date | None = None,
    end: dt.date | None = None,
) -> pd.DataFrame:
    """Daily adjusted closes as a long frame (security_id, date, close, volume)."""
    clauses, params = ["adj_close IS NOT NULL"], []
    if security_ids:
        placeholders = ",".join("?" * len(security_ids))
        clauses.append(f"security_id IN ({placeholders})")
        params.extend(security_ids)
    if start:
        clauses.append("date >= ?")
        params.append(start)
    if end:
        clauses.append("date <= ?")
        params.append(end)

    return con.execute(
        f"""SELECT security_id, date, adj_close AS close, volume
              FROM ohlcv
             WHERE {' AND '.join(clauses)}
             ORDER BY security_id, date""",
        params,
    ).df()


# ------------------------------------------------------- point-in-time reads
def fundamentals_asof(
    con: duckdb.DuckDBPyConnection,
    as_of: dt.date,
    metrics: list[str] | None = None,
    security_ids: list[int] | None = None,
    periods: int = 20,
    include_non_pit: bool = False,
) -> pd.DataFrame:
    """Fundamentals visible on `as_of`, newest filing wins per period.

    This is the ONLY sanctioned way to read `fundamentals_pit` for scoring or
    backtesting. Three guards are applied:

      1. `filing_date <= as_of` -- excludes figures not yet published.
      2. newest `filing_date` per (security, period, metric) -- so a later
         restatement supersedes the original *without* leaking backwards, since
         restatements filed after `as_of` are already excluded by guard 1.
      3. `is_pit` -- sources without a real filing date (Yahoo, which reports
         the current value and overwrites restatements in place) are excluded by
         default. `include_non_pit=True` is for screening TODAY, where "what is
         true now" is the right question. A backtest must never pass it.
    """
    clauses, params = ["f.filing_date <= ?"], [as_of]
    if not include_non_pit:
        clauses.append("coalesce(f.is_pit, TRUE)")
    if metrics:
        clauses.append(f"f.metric IN ({','.join('?' * len(metrics))})")
        params.extend(metrics)
    if security_ids:
        clauses.append(f"f.security_id IN ({','.join('?' * len(security_ids))})")
        params.extend(security_ids)

    params.append(periods)
    return con.execute(
        f"""
        WITH visible AS (
            SELECT f.*,
                   row_number() OVER (
                       PARTITION BY f.security_id, f.period_end, f.period_type, f.metric
                       ORDER BY f.filing_date DESC
                   ) AS revision_rank
              FROM fundamentals_pit f
             WHERE {' AND '.join(clauses)}
        ),
        current_revision AS (
            SELECT * FROM visible WHERE revision_rank = 1
        ),
        ranked AS (
            SELECT *,
                   dense_rank() OVER (
                       PARTITION BY security_id, period_type, metric
                       ORDER BY period_end DESC
                   ) AS period_rank
              FROM current_revision
        )
        SELECT security_id, period_end, period_type, filing_date,
               metric, value, unit, source
          FROM ranked
         WHERE period_rank <= ?
         ORDER BY security_id, metric, period_end DESC
        """,
        params,
    ).df()


def index_members_asof(
    con: duckdb.DuckDBPyConnection, index_name: str, as_of: dt.date
) -> list[int]:
    """Constituents of an index on a past date -- the survivorship-bias guard."""
    rows = con.execute(
        """SELECT security_id
             FROM index_membership
            WHERE index_name = ?
              AND from_date <= ?
              AND (to_date IS NULL OR to_date > ?)""",
        [index_name, as_of, as_of],
    ).fetchall()
    return [r[0] for r in rows]


# ---------------------------------------------------------------- bookkeeping
def log_ingest(
    con: duckdb.DuckDBPyConnection,
    run_id: str,
    stage: str,
    entity: str | None,
    status: str,
    rows: int = 0,
    detail: str | None = None,
    started_at: dt.datetime | None = None,
) -> None:
    con.execute(
        """INSERT INTO ingest_log
               (run_id, stage, entity, status, rows, detail, started_at, finished_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        [run_id, stage, entity, status, rows, detail,
         started_at or dt.datetime.now(), dt.datetime.now()],
    )
