"""Cash-conversion and pledge gates, tested on the cases they exist to catch."""

from __future__ import annotations

import datetime as dt

import pandas as pd
import pytest

from engine.scoring import gates
from engine.scoring.gates import FAIL, PASS, UNKNOWN, GateContext

AS_OF = dt.date(2026, 8, 14)
CR = 1e7


def annual(**metrics) -> pd.DataFrame:
    length = max(len(v) for v in metrics.values())
    index = pd.date_range(end="2026-03-31", periods=length, freq="YE").date
    return pd.DataFrame(metrics, index=index)


def ctx(annual_frame=None, ownership=None) -> GateContext:
    return GateContext(
        security_id=1, ticker="TEST.NS", as_of=AS_OF,
        quarterly=pd.DataFrame(),
        annual_frame=annual_frame if annual_frame is not None else pd.DataFrame(),
        prices=pd.DataFrame(), events=pd.DataFrame(),
        market_cap=5_000 * CR,
        ownership=ownership if ownership is not None else pd.DataFrame(),
    )


# ------------------------------------------------------------ cash conversion
def test_profit_without_cash_fails():
    """The Kaynes shape: four profitable years, cumulative cash flow negative."""
    frame = annual(
        cfo=[-42 * CR, 88 * CR, -82 * CR, -600 * CR],
        pat=[126 * CR, 232 * CR, 372 * CR, 504 * CR],
    )
    result = gates.gate_cash_conversion(ctx(frame))
    assert result.status == FAIL
    assert "no cash" in result.detail


def test_healthy_conversion_passes():
    frame = annual(
        cfo=[947 * CR, 1028 * CR, 916 * CR, 702 * CR],
        pat=[1002 * CR, 1158 * CR, 1348 * CR, 1628 * CR],
    )
    assert gates.gate_cash_conversion(ctx(frame)).status == PASS


def test_weak_conversion_fails():
    """Positive cash, but under 40% of reported profit reaches it."""
    frame = annual(cfo=[30 * CR] * 4, pat=[100 * CR] * 4)
    assert gates.gate_cash_conversion(ctx(frame)).status == FAIL


def test_loss_making_company_is_not_judged_on_conversion():
    """Divergence needs profit to diverge from; a loss-maker has none."""
    frame = annual(cfo=[-50 * CR] * 4, pat=[-100 * CR] * 4)
    assert gates.gate_cash_conversion(ctx(frame)).status == PASS


def test_short_history_is_unknown_not_pass():
    frame = annual(cfo=[100 * CR, 120 * CR], pat=[90 * CR, 100 * CR])
    assert gates.gate_cash_conversion(ctx(frame)).status == UNKNOWN


# -------------------------------------------------------------------- pledge
def ownership(pledge_pct, promoter_pct=45.0) -> pd.DataFrame:
    return pd.DataFrame([{
        "quarter_end": dt.date(2026, 6, 30), "filing_date": dt.date(2026, 7, 21),
        "promoter_pct": promoter_pct, "promoter_pledge_pct": pledge_pct,
        "public_pct": 100 - promoter_pct,
    }])


def test_heavy_pledge_fails():
    """WABAG's real reading: 47.8% of the promoter stake encumbered."""
    result = gates.gate_promoter_pledge(ctx(ownership=ownership(47.8, 19.08)))
    assert result.status == FAIL
    assert result.observed == pytest.approx(47.8)


def test_light_pledge_passes():
    assert gates.gate_promoter_pledge(ctx(ownership=ownership(7.4, 11.7))).status == PASS


def test_pledge_uses_promoter_denominator_not_equity():
    """Guards the denominator bug.

    NSE reports pledged-over-total-equity (9.12% for WABAG). The gate must see
    pledged-over-promoter-holding (47.8%). If the wrong field is ever wired in,
    a heavily encumbered promoter passes a 20% threshold.
    """
    assert gates.gate_promoter_pledge(ctx(ownership=ownership(9.12))).status == PASS
    assert gates.gate_promoter_pledge(ctx(ownership=ownership(47.8))).status == FAIL


def test_no_disclosure_is_unknown():
    assert gates.gate_promoter_pledge(ctx()).status == UNKNOWN
