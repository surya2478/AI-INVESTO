"""Market cap must be dated the same day as the ranking that uses it.

This is the defect that voided the first backtest. `attach_market_data` read
`securities.market_cap` -- today's value -- at every historical as-of date, which
fed the future price into the D and V pillars with the sign reversed: a company
that went on to triple ranked as big (D rewards small) and as expensive (V
divides a price nobody had paid yet by earnings already reported). Together
those pillars carry 20.5% of the composite.

The read path is tested directly in test_point_in_time.py; these fixtures test
that the SCORING path honours the same rule for the one input that never went
through it.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import duckdb
import pandas as pd
import pytest

from engine.scoring import gem

SCHEMA = Path(__file__).resolve().parent.parent / "engine" / "storage" / "schema.sql"

AS_OF = dt.date(2024, 7, 1)

RISER, LATE, NOSHARES = 1, 2, 3

PRICE_THEN = 100.0        # close on the ranking date
PRICE_LATER = 300.0       # what it went on to do, which must not be visible
SHARES = 10_000_000.0

# Deliberately absurd, and stored on every row: if any of it reaches a score,
# the test has caught the leak rather than merely failing.
STORED_CAP = 9.99e14


@pytest.fixture()
def con():
    connection = duckdb.connect(":memory:")
    connection.execute(SCHEMA.read_text(encoding="utf-8"))
    connection.executemany("""
        INSERT INTO securities (security_id, ticker, exchange, country, industry, market_cap)
        VALUES (?, ?, 'NSE', 'IN', 'Capital Goods', ?)
    """, [(RISER, "RISER.NS", STORED_CAP),
          (LATE, "LATE.NS", STORED_CAP),
          (NOSHARES, "NOSHARES.NS", STORED_CAP)])

    # Flat into the ranking date, then a tripling nobody could have known about.
    bars = []
    for security_id in (RISER, LATE, NOSHARES):
        for offset in range(120, 0, -1):
            bars.append((security_id, AS_OF - dt.timedelta(days=offset), PRICE_THEN, 5_000.0))
        bars.append((security_id, AS_OF, PRICE_THEN, 5_000.0))
        for offset in (1, 40, 200):
            bars.append((security_id, AS_OF + dt.timedelta(days=offset), PRICE_LATER, 5_000.0))
    connection.executemany("""
        INSERT INTO ohlcv (security_id, date, adj_close, volume) VALUES (?, ?, ?, ?)
    """, bars)

    connection.executemany("""
        INSERT INTO fundamentals_pit
            (security_id, period_end, period_type, filing_date, metric, value, unit, source)
        VALUES (?, ?, 'A', ?, 'share_count', ?, 'shares', 'test')
    """, [
        # Published before the ranking date: usable.
        (RISER, dt.date(2024, 3, 31), dt.date(2024, 5, 20), SHARES),
        # A later period, filed after the ranking date: must not supersede.
        (RISER, dt.date(2025, 3, 31), dt.date(2025, 5, 20), SHARES * 2),
        # LATE has published nothing by the ranking date at all.
        (LATE, dt.date(2025, 3, 31), dt.date(2025, 5, 20), SHARES),
    ])
    yield connection
    connection.close()


def scored(con) -> pd.DataFrame:
    features = pd.DataFrame({
        "security_id": [RISER, LATE, NOSHARES],
        "pat_ttm": [50_000_000.0] * 3,
        "net_worth": [400_000_000.0] * 3,
    })
    return gem.attach_market_data(con, features, AS_OF).set_index("security_id")


def test_cap_uses_the_price_on_the_ranking_date(con):
    """The whole point: yesterday's cap, not the one today's price implies."""
    frame = scored(con)
    assert frame.loc[RISER, "market_cap"] == pytest.approx(PRICE_THEN * SHARES)


def test_the_later_tripling_is_invisible(con):
    """If the post-date rally leaked in, the cap would be three times as large."""
    frame = scored(con)
    assert frame.loc[RISER, "market_cap"] < PRICE_LATER * SHARES


def test_stored_market_cap_is_never_used(con):
    """`securities.market_cap` is today's figure and has no place in a ranking."""
    frame = scored(con)
    assert (frame["market_cap"].dropna() < STORED_CAP).all()
    assert "market_cap" not in gem.shares_outstanding_asof(con, AS_OF).columns


def test_share_count_filed_later_does_not_leak_backwards(con):
    """The revision rule from fundamentals_asof, applied to the share count."""
    shares = gem.shares_outstanding_asof(con, AS_OF).set_index("security_id")
    assert shares.loc[RISER, "share_count"] == SHARES


def test_no_published_share_count_scores_nan_not_a_substitute(con):
    """A missing input is missing. Falling back to today's cap is the bug."""
    frame = scored(con)
    assert pd.isna(frame.loc[LATE, "market_cap"])
    assert pd.isna(frame.loc[NOSHARES, "market_cap"])


def test_valuation_pillars_inherit_the_point_in_time_cap(con):
    """P/E and P/B are the other half of the leak, and share the numerator."""
    frame = scored(con)
    assert frame.loc[RISER, "pe"] == pytest.approx(PRICE_THEN * SHARES / 50_000_000.0)
    assert frame.loc[RISER, "pb"] == pytest.approx(PRICE_THEN * SHARES / 400_000_000.0)
    assert pd.isna(frame.loc[LATE, "pe"])
