"""The health gate on `deployment_plan`, tested on the states that release money.

This is the only function in the project that decides where capital goes, so its
failure mode matters more than its behaviour. It must fail CLOSED: silence about
a position is not a clearance for it. The case that motivated these tests is a
position opened after the last nightly review -- it has no health row at all,
and the previous code read that absence as GREEN and funded it.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import duckdb
import pandas as pd
import pytest

from engine.portfolio import book

SCHEMA = Path(__file__).resolve().parent.parent / "engine" / "storage" / "portfolio_schema.sql"

TODAY = dt.date(2026, 8, 15)
FRESH = TODAY - dt.timedelta(days=1)
STALE = TODAY - dt.timedelta(days=book.HEALTH_MAX_AGE_DAYS + 1)

BUDGET = 100_000.0


@pytest.fixture()
def con(monkeypatch):
    connection = duckdb.connect(":memory:")
    connection.execute(SCHEMA.read_text(encoding="utf-8"))
    # holdings() reads last close from the analytics store; irrelevant here, and
    # hitting the real database would make these tests depend on tonight's job.
    monkeypatch.setattr(book, "_latest_prices", lambda tickers, analytics_db=None:
                        {t: 100.0 for t in tickers})
    yield connection
    connection.close()


def add_position(con, position_id: int, ticker: str) -> None:
    """An open position with stage 1 executed and stage 2 planned, i.e. due."""
    con.execute("""
        INSERT INTO positions (position_id, ticker, tier, target_weight_pct,
                               thesis, opened_on, status)
        VALUES (?, ?, 'CORE', 4.0, 'test thesis', ?, 'OPEN')
    """, [position_id, ticker, TODAY - dt.timedelta(days=200)])
    con.execute("""
        INSERT INTO tranches (position_id, stage, planned_pct, trigger, status,
                              executed_on, shares, price, amount)
        VALUES (?, 1, 40.0, 'opening', 'EXECUTED', ?, 100, 90.0, 9000.0)
    """, [position_id, TODAY - dt.timedelta(days=200)])
    con.execute("""
        INSERT INTO tranches (position_id, stage, planned_pct, trigger, status)
        VALUES (?, 2, 30.0, 'result confirms', 'PLANNED')
    """, [position_id])


def set_health(con, position_id: int, health: str, as_of: dt.date) -> None:
    con.execute("""
        INSERT INTO thesis_health (position_id, as_of_date, health, reasons, verdict)
        VALUES (?, ?, ?, 'test', 'CLEARED')
    """, [position_id, as_of, health])


def funded(con) -> set[str]:
    plan = book.deployment_plan(con, BUDGET, as_of=TODAY)
    return set() if plan.empty else set(plan["ticker"])


# ------------------------------------------------------------------ the states
def test_fresh_green_is_funded(con):
    """The one state that should release money."""
    add_position(con, 1, "GREENCO.NS")
    set_health(con, 1, "GREEN", FRESH)
    assert funded(con) == {"GREENCO.NS"}


def test_never_reviewed_is_not_funded(con):
    """No row at all. Previously this defaulted to GREEN and drew a tranche."""
    add_position(con, 1, "UNVETTED.NS")
    assert funded(con) == set()


def test_stale_green_is_not_funded(con):
    """A verdict older than the review cycle is not evidence about today."""
    add_position(con, 1, "FORGOTTEN.NS")
    set_health(con, 1, "GREEN", STALE)
    assert funded(con) == set()


def test_green_on_the_age_boundary_is_still_funded(con):
    """The cutoff is inclusive, so a same-cycle review is not thrown away."""
    add_position(con, 1, "EDGE.NS")
    set_health(con, 1, "GREEN", TODAY - dt.timedelta(days=book.HEALTH_MAX_AGE_DAYS))
    assert funded(con) == {"EDGE.NS"}


@pytest.mark.parametrize("health", ["AMBER", "RED"])
def test_flagged_theses_are_not_funded(con, health):
    add_position(con, 1, "BROKEN.NS")
    set_health(con, 1, health, FRESH)
    assert funded(con) == set()


def test_one_positions_review_says_nothing_about_another(con):
    """The per-position rule, and the subtlest form of the original bug.

    Selecting health at `max(as_of_date)` over the whole table returns only rows
    from the newest review run. A position last looked at months ago is simply
    absent from that result, reads as missing, and -- under a GREEN default --
    gets funded on the strength of a review that was about a different company.
    """
    add_position(con, 1, "REVIEWED.NS")
    add_position(con, 2, "FORGOTTEN.NS")
    set_health(con, 1, "RED", FRESH)
    set_health(con, 2, "GREEN", STALE)
    assert funded(con) == set()


def test_the_plan_reports_how_old_its_evidence_is(con):
    """A verdict without a date cannot be judged, so the date travels with it."""
    add_position(con, 1, "GREENCO.NS")
    set_health(con, 1, "GREEN", FRESH)
    plan = book.deployment_plan(con, BUDGET, as_of=TODAY)
    assert pd.Timestamp(plan["health_as_of"].iloc[0]).date() == FRESH
