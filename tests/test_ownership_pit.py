"""Ownership disclosures must be dated by when they were published.

`ownership_pit` held 515 rows sharing ONE filing_date, equal to the day the
scraper ran. Because `attach_market_data` filters `filing_date <= as_of`, that
made promoter holding and pledge invisible at every historical date -- 15% of the
quality pillar's weight had never once been populated in any backtest.

Two defects behind it, and only one is the exchange's fault:

* the fallback dated an undated disclosure to the QUARTER END itself, asserting
  the pattern was public on the last day of the quarter it describes, about three
  weeks before it could exist;
* there was no marker distinguishing a real publication date from an assumed one,
  so nothing downstream could tell them apart.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import duckdb
import pandas as pd
import pytest

from engine.scoring import gem
from engine.universe.builder import OWNERSHIP_FILING_LAG_DAYS, ownership_filing_date

SCHEMA = Path(__file__).resolve().parent.parent / "engine" / "storage" / "schema.sql"

QUARTER = dt.date(2025, 3, 31)
DEADLINE = QUARTER + dt.timedelta(days=OWNERSHIP_FILING_LAG_DAYS)


# ------------------------------------------------------------- the filing date
def test_a_reported_publication_date_is_used_as_given():
    filed = dt.date(2025, 4, 12)
    assert ownership_filing_date(QUARTER, filed) == (filed, True)


def test_an_undated_disclosure_falls_back_to_the_statutory_deadline():
    """Not to the quarter end, which is three weeks before it could be filed."""
    filing_date, is_pit = ownership_filing_date(QUARTER, None)
    assert filing_date == DEADLINE
    assert filing_date > QUARTER
    assert is_pit is False


def test_an_inferred_date_is_marked_so_a_backtest_can_exclude_it():
    assert ownership_filing_date(QUARTER, None)[1] is False
    assert ownership_filing_date(QUARTER, dt.date(2025, 4, 12))[1] is True


def test_a_date_before_the_quarter_ended_is_rejected():
    """A company cannot disclose a quarter's shareholding before it closes."""
    filing_date, is_pit = ownership_filing_date(QUARTER, dt.date(2025, 3, 1))
    assert filing_date == DEADLINE
    assert is_pit is False


def test_the_inferred_date_errs_late_rather_than_early():
    """Being late delays acting on information; being early manufactures an edge."""
    real_filing = dt.date(2025, 4, 10)
    inferred, _ = ownership_filing_date(QUARTER, None)
    assert inferred > real_filing


# --------------------------------------------------------------- the read path
@pytest.fixture()
def con():
    connection = duckdb.connect(":memory:")
    connection.execute(SCHEMA.read_text(encoding="utf-8"))
    connection.execute("""
        INSERT INTO securities (security_id, ticker, exchange, country)
        VALUES (1, 'TEST.NS', 'NSE', 'IN')
    """)
    connection.executemany("""
        INSERT INTO ownership_pit
            (security_id, quarter_end, filing_date, is_pit, promoter_pct,
             promoter_pledge_pct, source)
        VALUES (?, ?, ?, ?, ?, ?, 'test')
    """, [
        (1, dt.date(2024, 3, 31), dt.date(2024, 4, 15), True, 60.0, 5.0),
        (1, dt.date(2024, 9, 30), dt.date(2024, 10, 15), True, 55.0, 40.0),
        # Newest quarter, but not yet public on the as-of date below.
        (1, dt.date(2025, 3, 31), dt.date(2025, 4, 15), True, 50.0, 90.0),
    ])
    yield connection
    connection.close()


def ownership_on(con, as_of: dt.date, include_non_pit: bool = True) -> pd.DataFrame:
    features = pd.DataFrame({"security_id": [1], "pat_ttm": [1.0], "net_worth": [1.0]})
    return gem.attach_market_data(con, features, as_of,
                                  include_non_pit=include_non_pit).set_index("security_id")


def test_the_newest_visible_disclosure_wins_not_an_arbitrary_one(con):
    """This was drop_duplicates over an unordered result: whichever row the
    database happened to return last decided the answer."""
    frame = ownership_on(con, dt.date(2024, 12, 31))
    assert frame.loc[1, "promoter_pledge_pct"] == pytest.approx(40.0)


def test_a_disclosure_filed_after_the_as_of_date_is_not_visible(con):
    frame = ownership_on(con, dt.date(2024, 12, 31))
    assert frame.loc[1, "promoter_pct"] != 50.0


def test_the_latest_quarter_wins_once_it_has_been_filed(con):
    frame = ownership_on(con, dt.date(2025, 6, 30))
    assert frame.loc[1, "promoter_pledge_pct"] == pytest.approx(90.0)


def test_a_backtest_can_exclude_rows_whose_date_was_inferred(con):
    con.execute("""
        INSERT INTO ownership_pit
            (security_id, quarter_end, filing_date, is_pit, promoter_pct,
             promoter_pledge_pct, source)
        VALUES (1, '2025-06-30', '2025-07-21', FALSE, 10.0, 99.0, 'test')
    """)
    as_of = dt.date(2025, 8, 31)
    assert ownership_on(con, as_of).loc[1, "promoter_pledge_pct"] == pytest.approx(99.0)
    strict = ownership_on(con, as_of, include_non_pit=False)
    assert strict.loc[1, "promoter_pledge_pct"] == pytest.approx(90.0)
