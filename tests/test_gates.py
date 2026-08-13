"""Gate logic tested against hand-built fixtures.

Gates decide whether a company is investable at all, so their thresholds are
tested directly rather than inferred from whatever the live data happens to
contain. Each test states the real-world case it stands for.
"""

from __future__ import annotations

import datetime as dt

import pandas as pd
import pytest

from engine.scoring import gates
from engine.scoring.gates import FAIL, PASS, UNKNOWN, GateContext

AS_OF = dt.date(2026, 8, 13)


def quarters(**metrics) -> pd.DataFrame:
    """Build a quarterly frame; each metric is a list oldest-first."""
    length = max(len(v) for v in metrics.values())
    index = pd.date_range(end="2026-06-30", periods=length, freq="QE").date
    return pd.DataFrame(metrics, index=index)


def context(quarterly=None, prices=None, events=None, market_cap=5_000e7) -> GateContext:
    return GateContext(
        security_id=1, ticker="TEST.NS", as_of=AS_OF,
        quarterly=quarterly if quarterly is not None else pd.DataFrame(),
        prices=prices if prices is not None else pd.DataFrame(),
        events=events if events is not None else pd.DataFrame(),
        market_cap=market_cap,
    )


def price_frame(close: float, volume: float, days: int = 90) -> pd.DataFrame:
    return pd.DataFrame({
        "date": pd.date_range(end="2026-08-13", periods=days).date,
        "close": [close] * days,
        "volume": [volume] * days,
    })


# ------------------------------------------------------------- surveillance
def test_surveillance_fails_when_flagged():
    events = pd.DataFrame([{
        "event_date": dt.date(2026, 7, 1), "event_type": "ASM_SURVEILLANCE",
        "detail": "LONGTERM Stage I", "severity": "CRITICAL",
    }])
    assert gates.gate_surveillance(context(events=events)).status == FAIL


def test_surveillance_passes_with_unrelated_events():
    events = pd.DataFrame([{
        "event_date": dt.date(2026, 7, 1), "event_type": "DIVIDEND",
        "detail": "interim", "severity": "INFO",
    }])
    assert gates.gate_surveillance(context(events=events)).status == PASS


# ----------------------------------------------------------------- liquidity
def test_liquidity_fails_below_floor():
    # Rs 100 x 1,000 shares = Rs 1 lakh/day, far below the Rs 0.5 cr floor.
    result = gates.gate_liquidity(context(prices=price_frame(100, 1_000)))
    assert result.status == FAIL


def test_liquidity_passes_for_a_tradeable_name():
    # Rs 500 x 200,000 shares = Rs 10 cr/day.
    result = gates.gate_liquidity(context(prices=price_frame(500, 200_000)))
    assert result.status == PASS
    assert result.observed == pytest.approx(10.0, rel=0.01)


def test_liquidity_unknown_on_thin_history():
    assert gates.gate_liquidity(context(prices=price_frame(500, 200_000, days=10))).status == UNKNOWN


# ------------------------------------------------------------------ dilution
def test_serial_dilution_fails_on_heavy_issuance():
    q = quarters(equity_capital=[100, 100, 100, 100, 120, 130, 140, 150])
    assert gates.gate_serial_dilution(context(q)).status == FAIL


def test_serial_dilution_passes_on_stable_capital():
    q = quarters(equity_capital=[100] * 8)
    assert gates.gate_serial_dilution(context(q)).status == PASS


def test_serial_dilution_unknown_without_enough_history():
    q = quarters(equity_capital=[100] * 5)
    assert gates.gate_serial_dilution(context(q)).status == UNKNOWN


# --------------------------------------------------------- interest coverage
def test_interest_coverage_fails_when_debt_eats_profit():
    # EBIT 120 against interest 100 -> 1.2x, below the 1.5x floor.
    q = quarters(pbt=[5] * 4, finance_cost=[25] * 4)
    assert gates.gate_interest_coverage(context(q)).status == FAIL


def test_interest_coverage_passes_for_a_debt_free_company():
    q = quarters(pbt=[100] * 4, finance_cost=[0] * 4)
    assert gates.gate_interest_coverage(context(q)).status == PASS


# -------------------------------------------------------------------- losses
def test_sustained_losses_fails_on_three_of_four():
    q = quarters(pat=[-10, -5, 2, -8])
    assert gates.gate_sustained_losses(context(q)).status == FAIL


def test_single_loss_quarter_is_tolerated():
    """A pre-inflection company may post one bad quarter; that is not a reject."""
    q = quarters(pat=[10, 12, -3, 15])
    assert gates.gate_sustained_losses(context(q)).status == PASS


# ------------------------------------------------------------------- revenue
def test_revenue_collapse_fails_on_sharp_decline():
    q = quarters(revenue=[100] * 4 + [60, 60, 60, 60])
    assert gates.gate_revenue_collapse(context(q)).status == FAIL


def test_revenue_growth_passes():
    q = quarters(revenue=[100] * 4 + [130, 130, 130, 130])
    result = gates.gate_revenue_collapse(context(q))
    assert result.status == PASS
    assert result.observed == pytest.approx(30.0, rel=0.01)


# ---------------------------------------------------------------- market cap
def test_market_cap_units_are_rupees_not_crores():
    """Guards the bug where BSE crores were stored unconverted.

    BSE serves Mktcap in crores; securities.market_cap is rupees. If the
    conversion is ever dropped, a Rs 5,000 cr company arrives as 5,000 rupees
    and every name fails the floor.
    """
    ok = gates.gate_market_cap_band(context(market_cap=5_000e7))
    assert ok.status == PASS
    assert ok.observed == pytest.approx(5_000, rel=0.001)

    unconverted = gates.gate_market_cap_band(context(market_cap=5_000))
    assert unconverted.status == FAIL


def test_market_cap_rejects_megacaps():
    """A company at Rs 5 lakh crore cannot realistically multiply from here."""
    assert gates.gate_market_cap_band(context(market_cap=500_000e7)).status == FAIL


def test_market_cap_unknown_when_missing():
    assert gates.gate_market_cap_band(context(market_cap=None)).status == UNKNOWN


# ----------------------------------------------------------------- verdicts
def _rows(**by_gate) -> pd.DataFrame:
    return pd.DataFrame([
        {"ticker": "TEST.NS", "gate_name": name, "status": status, "detail": f"{name} {status}"}
        for name, status in by_gate.items()
    ])


def test_critical_failure_rejects():
    frame = _rows(surveillance=FAIL, liquidity=PASS)
    assert gates.verdicts(frame).verdict.iloc[0] == "REJECTED"


def test_non_critical_failure_only_flags():
    frame = _rows(liquidity=FAIL, surveillance=PASS, cash_conversion=PASS,
                  serial_dilution=PASS, sustained_losses=PASS)
    assert gates.verdicts(frame).verdict.iloc[0] == "FLAGGED"


def test_unknown_critical_gate_is_not_cleared():
    """The central rule: absence of evidence is not evidence of quality."""
    frame = _rows(surveillance=PASS, cash_conversion=UNKNOWN,
                  serial_dilution=PASS, sustained_losses=PASS)
    assert gates.verdicts(frame).verdict.iloc[0] == "UNVETTED"


def test_all_critical_gates_passing_clears():
    frame = _rows(surveillance=PASS, cash_conversion=PASS,
                  serial_dilution=PASS, sustained_losses=PASS, liquidity=PASS)
    assert gates.verdicts(frame).verdict.iloc[0] == "CLEARED"
