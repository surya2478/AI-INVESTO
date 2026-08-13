"""The point-in-time read path, tested with controlled fixtures.

`fundamentals_asof` is the single guard between the backtest and lookahead bias,
and its restatement rule is subtle: a later filing must supersede an earlier one
for the same period, but only once that later filing was itself public. The live
selftest can only confirm nothing leaked from whatever data happens to be
loaded; these fixtures prove the rule directly.

WABAG is the real case being modelled -- it filed Oct-Dec 2024 on 07-Feb-2025
and filed the same period again on 13-Mar-2025.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import duckdb
import pytest

from engine.storage.db import fundamentals_asof

SCHEMA = Path(__file__).resolve().parent.parent / "engine" / "storage" / "schema.sql"

PERIOD = dt.date(2024, 12, 31)
ORIGINAL_FILED = dt.date(2025, 2, 7)
REVISED_FILED = dt.date(2025, 3, 13)

ORIGINAL_REVENUE = 8_110_000_000.0
REVISED_REVENUE = 8_250_000_000.0


@pytest.fixture()
def con():
    connection = duckdb.connect(":memory:")
    connection.execute(SCHEMA.read_text(encoding="utf-8"))
    connection.execute("""
        INSERT INTO securities (security_id, ticker, exchange, country)
        VALUES (1, 'WABAG.NS', 'NSE', 'IN')
    """)
    rows = [
        (1, PERIOD, "Q", ORIGINAL_FILED, "revenue", ORIGINAL_REVENUE, "INR", "test"),
        (1, PERIOD, "Q", REVISED_FILED, "revenue", REVISED_REVENUE, "INR", "test"),
    ]
    connection.executemany("""
        INSERT INTO fundamentals_pit
            (security_id, period_end, period_type, filing_date, metric, value, unit, source)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, rows)
    yield connection
    connection.close()


def revenue_on(con, as_of: dt.date) -> float | None:
    frame = fundamentals_asof(con, as_of, metrics=["revenue"], security_ids=[1])
    if frame.empty:
        return None
    assert len(frame) == 1, "exactly one revision must survive per period"
    return float(frame["value"].iloc[0])


def test_nothing_visible_before_the_first_filing(con):
    """The figure did not exist publicly yet, so the engine must not see it."""
    assert revenue_on(con, dt.date(2025, 1, 15)) is None


def test_original_visible_between_the_two_filings(con):
    """A backtest on 20-Feb-2025 must see what the market saw that day."""
    assert revenue_on(con, dt.date(2025, 2, 20)) == ORIGINAL_REVENUE


def test_original_visible_on_its_own_filing_date(con):
    """filing_date <= as_of is inclusive: it was public that day."""
    assert revenue_on(con, ORIGINAL_FILED) == ORIGINAL_REVENUE


def test_revision_supersedes_once_published(con):
    assert revenue_on(con, dt.date(2025, 4, 1)) == REVISED_REVENUE


def test_revision_does_not_leak_backwards(con):
    """The core guard.

    The revised figure was filed 13-Mar. Asked for 12-Mar, the engine must
    still return the original -- otherwise a backtest silently trades on a
    number that did not exist yet.
    """
    assert revenue_on(con, dt.date(2025, 3, 12)) == ORIGINAL_REVENUE


def test_period_limit_counts_periods_not_rows(con):
    """`periods` bounds distinct periods, so revisions cannot crowd them out."""
    con.executemany("""
        INSERT INTO fundamentals_pit
            (security_id, period_end, period_type, filing_date, metric, value, unit, source)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, [
        (1, dt.date(2024, 9, 30), "Q", dt.date(2024, 11, 7), "revenue", 7_003_000_000.0, "INR", "test"),
        (1, dt.date(2024, 6, 30), "Q", dt.date(2024, 8, 8), "revenue", 6_265_000_000.0, "INR", "test"),
    ])
    frame = fundamentals_asof(con, dt.date(2026, 1, 1), metrics=["revenue"],
                              security_ids=[1], periods=2)
    assert sorted(frame["period_end"].dt.date.unique(), reverse=True) == [
        dt.date(2024, 12, 31), dt.date(2024, 9, 30),
    ]


def test_annual_and_quarterly_do_not_collide(con):
    """A Q and an A ending on the same date are distinct facts."""
    con.execute("""
        INSERT INTO fundamentals_pit
            (security_id, period_end, period_type, filing_date, metric, value, unit, source)
        VALUES (1, ?, 'A', ?, 'revenue', ?, 'INR', 'test')
    """, [PERIOD, ORIGINAL_FILED, 30_000_000_000.0])

    frame = fundamentals_asof(con, dt.date(2026, 1, 1), metrics=["revenue"], security_ids=[1])
    by_type = dict(zip(frame["period_type"], frame["value"]))
    assert by_type["Q"] == REVISED_REVENUE
    assert by_type["A"] == 30_000_000_000.0
