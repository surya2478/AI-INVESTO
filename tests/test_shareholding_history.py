"""Parsing NSE's shareholding-pattern history.

This is the feed that makes ownership usable at a historical date. The pledge
endpoint the engine used before returns one record per company with no history,
which is how `ownership_pit` ended up holding a single quarter stamped with the
scraper's clock -- invisible at every past rebalance and worth 15% of the quality
pillar's weight.

The fixtures below are shaped exactly like the real payload, including the parts
that are easy to get wrong: filing lags that vary by seven months, event-driven
patterns dated off a quarter end, and revisions of an already-published quarter.
"""

from __future__ import annotations

import datetime as dt

import pytest

from engine.providers.nse_provider import NSEProvider


class FakeResponse:
    status_code = 200

    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self, payload):
        self._payload = payload
        self.urls = []

    def get(self, url, **kwargs):
        self.urls.append(url)
        return FakeResponse(self._payload)


def provider_returning(payload) -> NSEProvider:
    provider = NSEProvider()
    provider._session = FakeSession(payload)
    return provider


PAYLOAD = [
    {"date": "30-JUN-2026", "broadcastDate": "20-JUL-2026 23:55:22",
     "pr_and_prgrp": "56.36", "public_val": "43.64", "revisedData": "N",
     "xbrl": "https://nsearchives.nseindia.com/corporate/xbrl/SHP_1.xml"},
    # Filed seven months late -- a 21-day statutory assumption would be badly wrong.
    {"date": "31-DEC-2023", "broadcastDate": "15-JUL-2024 13:17:20",
     "pr_and_prgrp": "58.11", "public_val": "41.89", "revisedData": "N"},
    # Event-driven pattern, not a quarter end, and the more current picture.
    {"date": "04-JUL-2025", "broadcastDate": "07-JUL-2025 21:41:37",
     "pr_and_prgrp": "56.38", "public_val": "43.62", "revisedData": "N"},
    # A revision of an already-published quarter.
    {"date": "30-SEP-2025", "broadcastDate": "16-NOV-2025 10:00:00",
     "pr_and_prgrp": "56.40", "public_val": "43.60", "revisedData": "Y"},
    # No broadcast date; submissionDate is the same event, coarser.
    {"date": "31-MAR-2023", "broadcastDate": None, "submissionDate": "18-APR-2023",
     "pr_and_prgrp": "58.12", "public_val": "41.88", "revisedData": "N"},
]


@pytest.fixture()
def history():
    return provider_returning(PAYLOAD).fetch_shareholding_history("CGPOWER")


def by_quarter(history, day: str) -> dict:
    match = [r for r in history if str(r["quarter_end"]) == day]
    assert len(match) == 1, f"expected one record for {day}, got {len(match)}"
    return match[0]


# ------------------------------------------------------------- the filing date
def test_every_filing_keeps_its_own_broadcast_date(history):
    """The whole point: one date per filing, not one date for the table."""
    assert len({r["filing_date"] for r in history}) == len(history)


def test_a_seven_month_filing_lag_is_preserved_not_normalised(history):
    """CG Power really did file its Dec-2023 pattern in July 2024."""
    record = by_quarter(history, "2023-12-31")
    assert record["filing_date"] == dt.date(2024, 7, 15)
    assert (record["filing_date"] - record["quarter_end"]).days > 180


def test_submission_date_is_used_when_no_broadcast_date(history):
    record = by_quarter(history, "2023-03-31")
    assert record["filing_date"] == dt.date(2023, 4, 18)


def test_a_filing_is_never_dated_before_the_period_it_describes(history):
    for record in history:
        assert record["filing_date"] >= record["quarter_end"], record


# ------------------------------------------------------- non-quarter filings
def test_an_event_driven_pattern_is_kept(history):
    """Filed after a promoter sale; it is the more current holding, not noise."""
    record = by_quarter(history, "2025-07-04")
    assert record["promoter_pct"] == pytest.approx(56.38)


def test_revisions_are_flagged_rather_than_merged(history):
    """A revision arrives as its own row, resolved by filing date on read."""
    assert by_quarter(history, "2025-09-30")["is_revision"] is True
    assert by_quarter(history, "2026-06-30")["is_revision"] is False


# ------------------------------------------------------------------- values
def test_promoter_and_public_percentages_are_parsed(history):
    record = by_quarter(history, "2026-06-30")
    assert record["promoter_pct"] == pytest.approx(56.36)
    assert record["public_pct"] == pytest.approx(43.64)


def test_pledge_is_absent_from_this_feed(history):
    """Worth pinning: quality's pledge input still has no historical source."""
    assert all("promoter_pledge_pct" not in r for r in history)


@pytest.mark.parametrize("bad", ["7905.2", "-3", "abc", "", "-"])
def test_an_impossible_promoter_holding_is_dropped_not_scored(bad):
    """The ITC case: a ratio outside 0-100 is a data error, never a verdict."""
    payload = [{"date": "30-JUN-2026", "broadcastDate": "20-JUL-2026 10:00:00",
                "pr_and_prgrp": bad, "public_val": "43.64"}]
    record = provider_returning(payload).fetch_shareholding_history("X")[0]
    assert record["promoter_pct"] is None


def test_records_without_a_usable_date_are_skipped():
    payload = [{"date": None, "broadcastDate": "20-JUL-2026 10:00:00", "pr_and_prgrp": "50"},
               {"date": "30-JUN-2026", "broadcastDate": None, "pr_and_prgrp": "50"}]
    assert provider_returning(payload).fetch_shareholding_history("X") == []


def test_an_empty_payload_is_not_an_error():
    assert provider_returning([]).fetch_shareholding_history("X") == []
