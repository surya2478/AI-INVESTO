"""Annual XBRL parsing, on the three traps that make these documents hostile.

Annual point-in-time fundamentals are the input the whole engine was missing:
the score ranks on annual periods, and until the provider learned to ask NSE for
`period=Annual` there were zero annual rows with a real filing date. Getting the
request right is the easy half. The document itself carries three hazards:

1. The full-year and the fourth-quarter columns BOTH end on 31-Mar, and the
   `<context>` element stamps the quarter's start date on both. Only the
   declared `DateOfStartOfReportingPeriod` fact separates a year from a quarter.
2. The balance sheet is filed as INSTANT facts with no span at all, and the
   basis (standalone/consolidated) is not stamped on them.
3. Segment breakdowns carry explicit dimensions and would double-count revenue.

The fixture below reproduces all three exactly as CG Power's FY2024 filing does.
"""

from __future__ import annotations

import datetime as dt

import pandas as pd
import pytest

from engine.fundamentals import derive_metrics
from engine.providers.xbrl_provider import (
    NSEFilingsProvider,
    classify_period,
    select_filing_facts,
)

YEAR_END = dt.date(2024, 3, 31)
FILED = dt.date(2024, 5, 6)

FULL_YEAR_REVENUE = 80_459_800_000.0
Q4_REVENUE = 21_917_200_000.0
SEGMENT_REVENUE = 44_000_000_000.0

EQUITY_OWNERS = 30_174_400_000.0
EQUITY_TOTAL = 30_187_700_000.0
SHARE_CAPITAL = 3_054_700_000.0
FACE_VALUE = 2.0
BORROWINGS_CURRENT = 1_342_300_000.0
CFO = 3_969_900_000.0


DOCUMENT = f"""<?xml version="1.0" encoding="UTF-8"?>
<xbrl xmlns="http://www.xbrl.org/2003/instance"
      xmlns:in-bse-fin="http://www.bseindia.com/xbrl/fin/2020-03-31/in-bse-fin">
  <!-- Both duration contexts declare the QUARTER's dates. They are lying:
       the declared reporting-period facts below are authoritative. -->
  <context id="OneD">
    <period><startDate>2024-01-01</startDate><endDate>2024-03-31</endDate></period>
  </context>
  <context id="FourD">
    <period><startDate>2024-01-01</startDate><endDate>2024-03-31</endDate></period>
  </context>
  <context id="OneI">
    <period><instant>2024-03-31</instant></period>
  </context>
  <context id="SegD">
    <entity><segment><explicitMember dimension="d:Segment">Industrial</explicitMember></segment></entity>
    <period><startDate>2023-04-01</startDate><endDate>2024-03-31</endDate></period>
  </context>

  <in-bse-fin:DateOfStartOfReportingPeriod contextRef="OneD">2024-01-01</in-bse-fin:DateOfStartOfReportingPeriod>
  <in-bse-fin:DateOfEndOfReportingPeriod contextRef="OneD">2024-03-31</in-bse-fin:DateOfEndOfReportingPeriod>
  <in-bse-fin:NatureOfReportStandaloneConsolidated contextRef="OneD">Consolidated</in-bse-fin:NatureOfReportStandaloneConsolidated>

  <in-bse-fin:DateOfStartOfReportingPeriod contextRef="FourD">2023-04-01</in-bse-fin:DateOfStartOfReportingPeriod>
  <in-bse-fin:DateOfEndOfReportingPeriod contextRef="FourD">2024-03-31</in-bse-fin:DateOfEndOfReportingPeriod>
  <in-bse-fin:NatureOfReportStandaloneConsolidated contextRef="FourD">Consolidated</in-bse-fin:NatureOfReportStandaloneConsolidated>

  <in-bse-fin:RevenueFromOperations contextRef="OneD">{Q4_REVENUE}</in-bse-fin:RevenueFromOperations>
  <in-bse-fin:RevenueFromOperations contextRef="FourD">{FULL_YEAR_REVENUE}</in-bse-fin:RevenueFromOperations>
  <in-bse-fin:SegmentRevenue contextRef="SegD">{SEGMENT_REVENUE}</in-bse-fin:SegmentRevenue>
  <in-bse-fin:RevenueFromOperations contextRef="SegD">{SEGMENT_REVENUE}</in-bse-fin:RevenueFromOperations>

  <in-bse-fin:ProfitBeforeTax contextRef="FourD">19000000000.00</in-bse-fin:ProfitBeforeTax>
  <in-bse-fin:FinanceCosts contextRef="FourD">200000000.00</in-bse-fin:FinanceCosts>
  <in-bse-fin:DepreciationDepletionAndAmortisationExpense contextRef="FourD">1000000000.00</in-bse-fin:DepreciationDepletionAndAmortisationExpense>
  <in-bse-fin:OtherIncome contextRef="FourD">8700000000.00</in-bse-fin:OtherIncome>
  <in-bse-fin:ProfitLossForPeriod contextRef="FourD">14276100000.00</in-bse-fin:ProfitLossForPeriod>
  <in-bse-fin:FaceValueOfEquityShareCapital contextRef="FourD">{FACE_VALUE}</in-bse-fin:FaceValueOfEquityShareCapital>

  <in-bse-fin:CashFlowsFromUsedInOperatingActivities contextRef="FourD">{CFO}</in-bse-fin:CashFlowsFromUsedInOperatingActivities>
  <in-bse-fin:PurchaseOfPropertyPlantAndEquipmentClassifiedAsInvestingActivities contextRef="FourD">2060200000.00</in-bse-fin:PurchaseOfPropertyPlantAndEquipmentClassifiedAsInvestingActivities>

  <in-bse-fin:EquityAttributableToOwnersOfParent contextRef="OneI">{EQUITY_OWNERS}</in-bse-fin:EquityAttributableToOwnersOfParent>
  <in-bse-fin:Equity contextRef="OneI">{EQUITY_TOTAL}</in-bse-fin:Equity>
  <in-bse-fin:EquityShareCapital contextRef="OneI">{SHARE_CAPITAL}</in-bse-fin:EquityShareCapital>
  <in-bse-fin:OtherEquity contextRef="OneI">27119700000.00</in-bse-fin:OtherEquity>
  <in-bse-fin:BorrowingsCurrent contextRef="OneI">{BORROWINGS_CURRENT}</in-bse-fin:BorrowingsCurrent>
  <in-bse-fin:BorrowingsNoncurrent contextRef="OneI">0.00</in-bse-fin:BorrowingsNoncurrent>
</xbrl>
"""


@pytest.fixture()
def facts() -> pd.DataFrame:
    parsed, _ = NSEFilingsProvider.parse_xbrl(DOCUMENT)
    return parsed


@pytest.fixture()
def annual(facts) -> pd.DataFrame:
    """What a FY2024 annual filing contributes, after selection."""
    selected = select_filing_facts(facts, YEAR_END, "A", "Consolidated")
    selected = selected.assign(symbol="CGPOWER", period_type="A", filing_date=FILED)
    return derive_metrics(selected)


def value_of(frame: pd.DataFrame, metric: str) -> float:
    rows = frame[frame["metric"] == metric]
    assert len(rows) == 1, f"expected exactly one {metric}, got {len(rows)}"
    return float(rows["value"].iloc[0])


# ------------------------------------------------- the year / quarter conflict
def test_declared_period_beats_the_context_element(facts):
    """Both contexts claim a January start. One of them is the full year."""
    annual_rows = facts[[classify_period(s, e) == "A"
                         for s, e in zip(facts.period_start, facts.period_end)]]
    revenue = annual_rows[annual_rows.metric == "revenue"]
    assert len(revenue) == 1
    assert float(revenue["value"].iloc[0]) == FULL_YEAR_REVENUE


def test_the_fourth_quarter_is_not_mistaken_for_the_year(annual):
    """Taking the quarter as the year would understate revenue fourfold."""
    assert value_of(annual, "revenue") == FULL_YEAR_REVENUE


def test_an_annual_filing_yields_no_quarterly_rows(annual):
    assert (annual["period_type"] == "A").all()


# ------------------------------------------------------------ the balance sheet
def test_instant_facts_survive_selection(annual):
    """The bug this whole change exists to fix: no span, so previously dropped."""
    assert value_of(annual, "net_worth") == EQUITY_OWNERS


def test_net_worth_prefers_the_owners_measure(annual):
    """ROE pairs equity with profit attributable to owners, so exclude minorities."""
    assert value_of(annual, "net_worth") != EQUITY_TOTAL


def test_borrowings_sum_across_both_legs(annual):
    assert value_of(annual, "total_debt") == BORROWINGS_CURRENT


def test_share_count_comes_from_capital_over_face_value(annual):
    """Never the rupee figure alone: a split moves it without issuing a share."""
    assert value_of(annual, "share_count") == SHARE_CAPITAL / FACE_VALUE


def test_share_count_falls_back_to_the_quarterly_capital_line(facts):
    """The balance sheet starts in FY2023; `equity_capital` goes back to FY2018.

    Point-in-time market cap needs a share count at every ranking date, so the
    quarterly line is the one that makes the historical cap computable.
    """
    quarterly = pd.DataFrame({
        "metric": ["equity_capital", "face_value", "revenue"],
        "value": [SHARE_CAPITAL, FACE_VALUE, Q4_REVENUE],
        "symbol": "CGPOWER", "period_type": "Q",
        "period_end": YEAR_END, "filing_date": FILED,
    })
    derived = derive_metrics(quarterly)
    assert value_of(derived, "share_count") == SHARE_CAPITAL / FACE_VALUE


def test_share_count_tracks_face_value_rather_than_assuming_it(facts):
    """A split halves the face value and doubles the count on unchanged capital.

    The count really does double here -- that is what a split does. The point is
    that the derivation reads face value each period instead of holding it
    fixed, because holding it fixed is what made the rupee measure claim Blue
    Star had diluted 113% when its share count had risen 6.7%.
    """
    before = derive_metrics(pd.DataFrame({
        "metric": ["equity_capital", "face_value"], "value": [SHARE_CAPITAL, 10.0],
        "symbol": "X", "period_type": "A", "period_end": YEAR_END, "filing_date": FILED,
    }))
    after = derive_metrics(pd.DataFrame({
        "metric": ["equity_capital", "face_value"], "value": [SHARE_CAPITAL, 5.0],
        "symbol": "X", "period_type": "A", "period_end": YEAR_END, "filing_date": FILED,
    }))
    assert value_of(after, "share_count") == 2 * value_of(before, "share_count")


def test_cash_flow_is_captured(annual):
    """Cash conversion is the heaviest input in the quality pillar."""
    assert value_of(annual, "cfo") == CFO


def test_the_pillars_inputs_are_all_present(annual):
    """The exact set `gem.build_features` reads. A gap here is a dead pillar."""
    needed = {"revenue", "pat", "ebitda", "cfo", "capex", "net_worth",
              "total_debt", "share_count"}
    assert needed <= set(annual["metric"])


# ------------------------------------------------------------------- segments
def test_segment_rows_are_dropped(annual):
    """Keeping a dimensional row would double-count revenue against the total."""
    assert SEGMENT_REVENUE not in set(annual["value"])


# ------------------------------------------------------- basis, on instants
def test_unlabelled_instants_are_not_filtered_out_by_basis(facts):
    """NSE stamps no basis on instant contexts; dropping them loses the balance sheet."""
    selected = select_filing_facts(facts, YEAR_END, "A", "Consolidated")
    assert "net_worth" not in set(selected["metric"])       # not derived yet
    assert "equity_owners" in set(selected["metric"])       # but present to derive from


def test_standalone_request_still_keeps_the_instants(facts):
    """A standalone filing has its own instant context, equally unlabelled."""
    selected = select_filing_facts(facts, YEAR_END, "A", "Non-Consolidated")
    assert "equity_owners" in set(selected["metric"])
