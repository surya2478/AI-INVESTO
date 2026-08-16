"""A thesis you cannot check is a thesis nothing checks.

Positions carried their reasons as prose and `review_thesis` tested gates and
the score band -- generic quality signals, none of them the reason anybody
bought. WABAG's thesis rests on a "policy-visible order book"; the engine has
extracted order books from BSE announcements the whole time and the two were
never connected, so a reason could stop being true in silence.

The rule these fixtures pin is the one that makes the feature worth having:
UNCHECKABLE is not HOLDS. A claim whose input has no data has not passed.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import duckdb
import pandas as pd
import pytest

from engine.portfolio import book
from engine.portfolio import claims as claim_engine

SCHEMA = Path(__file__).resolve().parent.parent / "engine" / "storage" / "portfolio_schema.sql"


def claim(metric="order_book_to_sales", comparator=">=", threshold=2.0, claim_id=1):
    return claim_engine.Claim(metric=metric, comparator=comparator,
                              threshold=threshold, claim_id=claim_id)


# ------------------------------------------------------------ the three states
def test_a_claim_that_holds_is_reported_as_holding():
    checks = claim_engine.check([claim(threshold=2.0)], {"order_book_to_sales": 2.4})
    assert checks[0].status == claim_engine.HOLDS
    assert checks[0].observed == 2.4


def test_a_claim_that_fails_is_reported_as_broken():
    checks = claim_engine.check([claim(threshold=2.0)], {"order_book_to_sales": 1.1})
    assert checks[0].status == claim_engine.BROKEN
    assert "no longer" in checks[0].detail


def test_an_unmeasurable_claim_is_not_a_passing_claim():
    """The rule this module exists for: absence of evidence is not support."""
    checks = claim_engine.check([claim()], {"order_book_to_sales": None})
    assert checks[0].status == claim_engine.UNCHECKABLE
    assert checks[0].status != claim_engine.HOLDS


def test_a_missing_metric_entirely_is_uncheckable_not_broken():
    """An input the engine never measured says nothing either way."""
    checks = claim_engine.check([claim()], {})
    assert checks[0].status == claim_engine.UNCHECKABLE


# ------------------------------------------------------------------ direction
def test_a_less_than_claim_reads_the_other_way():
    holds = claim_engine.check([claim("promoter_pledge_pct", "<=", 10.0)],
                               {"promoter_pledge_pct": 4.0})
    broken = claim_engine.check([claim("promoter_pledge_pct", "<=", 10.0)],
                                {"promoter_pledge_pct": 47.8})
    assert holds[0].status == claim_engine.HOLDS
    assert broken[0].status == claim_engine.BROKEN


def test_the_boundary_counts_as_holding():
    checks = claim_engine.check([claim(threshold=2.0)], {"order_book_to_sales": 2.0})
    assert checks[0].status == claim_engine.HOLDS


# --------------------------------------------------------- health from claims
def test_no_checkable_claims_says_nothing_at_all():
    """Silence, not a verdict -- so an unmeasurable thesis cannot read as clean."""
    health, reasons = claim_engine.health_from_checks(
        claim_engine.check([claim()], {"order_book_to_sales": None}))
    assert health is None and reasons == []


def test_one_broken_claim_of_three_is_amber():
    checks = claim_engine.check(
        [claim("roe", ">=", 15, 1), claim("revenue_growth_yoy", ">=", 20, 2),
         claim("debt_equity", "<=", 1.0, 3)],
        {"roe": 18.0, "revenue_growth_yoy": 25.0, "debt_equity": 2.5})
    health, reasons = claim_engine.health_from_checks(checks)
    assert health == "AMBER"
    assert len(reasons) == 1


def test_a_majority_broken_is_red():
    """At that point the reason for holding has mostly gone."""
    checks = claim_engine.check(
        [claim("roe", ">=", 15, 1), claim("revenue_growth_yoy", ">=", 20, 2)],
        {"roe": 4.0, "revenue_growth_yoy": 1.0})
    health, _ = claim_engine.health_from_checks(checks)
    assert health == "RED"


def test_uncheckable_claims_do_not_dilute_the_majority():
    """Two of two checkable broken is RED even with unmeasurable claims alongside."""
    checks = claim_engine.check(
        [claim("roe", ">=", 15, 1), claim("revenue_growth_yoy", ">=", 20, 2),
         claim("order_book_to_sales", ">=", 2.0, 3)],
        {"roe": 4.0, "revenue_growth_yoy": 1.0, "order_book_to_sales": None})
    health, _ = claim_engine.health_from_checks(checks)
    assert health == "RED"


# ------------------------------------------------------------------- storage
@pytest.fixture()
def folio():
    con = duckdb.connect(":memory:")
    con.execute(SCHEMA.read_text(encoding="utf-8"))
    con.execute("""
        INSERT INTO positions (position_id, ticker, tier, target_weight_pct,
                               thesis, opened_on, status)
        VALUES (1, 'WABAG.NS', 'SATELLITE', 2.0, 'Water EPC, order book', ?, 'OPEN')
    """, [dt.date(2026, 8, 14)])
    yield con
    con.close()


def test_a_claim_round_trips(folio):
    book.add_claim(folio, "WABAG", "order_book_to_sales", ">=", 2.0, note="AMRUT 2.0")
    stored = book.claims_for(folio, 1)
    assert len(stored) == 1
    assert stored[0].metric == "order_book_to_sales"
    assert stored[0].threshold == 2.0
    assert stored[0].note == "AMRUT 2.0"


def test_an_unmeasurable_metric_is_refused_at_entry(folio):
    """Better to reject the claim than to record one that silently never checks."""
    with pytest.raises(ValueError, match="unknown metric"):
        book.add_claim(folio, "WABAG", "vibes", ">=", 1.0)


def test_a_bad_comparator_is_refused(folio):
    with pytest.raises(ValueError, match="comparator"):
        book.add_claim(folio, "WABAG", "roe", ">", 15.0)


def test_a_claim_needs_an_open_position(folio):
    with pytest.raises(ValueError, match="no open position"):
        book.add_claim(folio, "NOTHELD", "roe", ">=", 15.0)


def test_revising_a_claim_replaces_it_rather_than_failing(folio):
    """Changing your mind about a threshold is normal, not a constraint error.

    The uniqueness key includes created_on, so a same-day revision collided
    outright until this was handled.
    """
    book.add_claim(folio, "WABAG", "order_book_to_sales", ">=", 2.0)
    book.add_claim(folio, "WABAG", "order_book_to_sales", ">=", 3.0, note="raised the bar")
    live = book.claims_for(folio, 1)
    assert len(live) == 1
    assert live[0].threshold == 3.0
    assert live[0].note == "raised the bar"


def test_a_retired_claim_can_be_recorded_again(folio):
    """Retiring then reinstating on the same day must not hit the unique key."""
    book.add_claim(folio, "WABAG", "roe", ">=", 12.0)
    book.retire_claim(folio, book.claims_for(folio, 1)[0].claim_id)
    assert book.claims_for(folio, 1) == []
    book.add_claim(folio, "WABAG", "roe", ">=", 15.0)
    live = book.claims_for(folio, 1)
    assert len(live) == 1 and live[0].threshold == 15.0


def test_claims_on_different_metrics_coexist(folio):
    book.add_claim(folio, "WABAG", "roe", ">=", 12.0)
    book.add_claim(folio, "WABAG", "debt_equity", "<=", 1.0)
    assert len(book.claims_for(folio, 1)) == 2


def test_retiring_a_claim_keeps_it_but_stops_testing_it(folio):
    book.add_claim(folio, "WABAG", "roe", ">=", 15.0)
    stored = book.claims_for(folio, 1)
    book.retire_claim(folio, stored[0].claim_id)
    assert book.claims_for(folio, 1) == []
    kept = folio.execute("SELECT count(*) FROM thesis_claims").fetchone()[0]
    assert kept == 1, "a belief you abandoned is part of the record"
