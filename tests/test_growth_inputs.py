"""Growth acceleration must compare windows that do not overlap.

`rev_accel` used to be a 2-year CAGR minus a 3-year CAGR -- a window against a
superset of itself, differing by one year. It correlated +0.44 to +0.76 with the
plain 2-year CAGR it was supposed to add information to, so the heaviest input in
the growth pillar (35%) was largely a noisy restatement of the second heaviest
(25%). The variable was also named `rev_4y` while computing three years.

These fixtures pin the property that matters: a company whose growth rate is
steady has NO acceleration, whatever its level.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from engine.scoring import gem


def revenue(*values: float) -> pd.Series:
    """Oldest to newest, as `build_features` supplies it."""
    return pd.Series(list(values), dtype=float)


# ------------------------------------------------------------- the core property
def test_steady_growth_has_no_acceleration():
    """40% every year is a fast company, not an accelerating one."""
    steady = revenue(100.0, 140.0, 196.0)          # +40%, +40%
    assert gem._growth_acceleration(steady) == pytest.approx(0.0)


def test_a_company_speeding_up_scores_positive():
    """10% then 25% -- the case the pillar's docstring exists to describe."""
    accelerating = revenue(100.0, 110.0, 137.5)    # +10%, +25%
    assert gem._growth_acceleration(accelerating) == pytest.approx(15.0)


def test_a_company_slowing_down_scores_negative():
    decelerating = revenue(100.0, 140.0, 154.0)    # +40%, +10%
    assert gem._growth_acceleration(decelerating) == pytest.approx(-30.0)


def test_level_and_acceleration_are_independent():
    """The old measure could not do this: a fast steady company outranked a
    slow accelerating one purely because the windows overlapped."""
    fast_steady = gem._growth_acceleration(revenue(100.0, 200.0, 400.0))   # +100%, +100%
    slow_speeding = gem._growth_acceleration(revenue(100.0, 101.0, 110.0))  # +1%, +8.9%
    assert fast_steady == pytest.approx(0.0)
    assert slow_speeding > fast_steady


# ------------------------------------------------------------------- guards
def test_too_short_a_history_is_missing_not_zero():
    assert gem._growth_acceleration(revenue(100.0, 140.0)) is None


def test_a_non_positive_base_is_missing_rather_than_infinite():
    """Dividing by a zero or negative revenue base is not a growth rate."""
    assert gem._growth_acceleration(revenue(0.0, 100.0, 140.0)) is None
    assert gem._growth_acceleration(revenue(-50.0, 100.0, 140.0)) is None
    assert gem._growth_acceleration(revenue(100.0, 0.0, 140.0)) is None


# ------------------------------------------------------------ capex intensity
def test_capex_intensity_no_longer_scores_the_growth_pillar():
    """Dropped because capex over revenue measures how capital-hungry an
    industry is, not whether a company is investing ahead of demand."""
    columns = ["rev_accel", "rev_cagr_2y", "operating_leverage", "margin_trend",
               "cash_conversion", "roe", "debt_equity", "promoter_pct",
               "promoter_pledge_pct", "market_cap", "turnover", "pe", "pb",
               "mom_12m", "mom_3m"]
    spread = np.linspace(1.0, 100.0, 20)
    frame = pd.DataFrame({c: spread.copy() for c in columns})
    frame["ticker"] = [f"T{i}.NS" for i in range(20)]

    baseline = gem.score(frame.assign(capex_intensity=spread))["g_score"]
    reversed_capex = gem.score(frame.assign(capex_intensity=spread[::-1]))["g_score"]
    assert list(baseline) == list(reversed_capex)


def test_dropping_capex_did_not_retune_the_remaining_weights():
    """Four inputs present must still read as fully covered."""
    columns = ["rev_accel", "rev_cagr_2y", "operating_leverage", "margin_trend",
               "cash_conversion", "roe", "debt_equity", "promoter_pct",
               "promoter_pledge_pct", "market_cap", "turnover", "pe", "pb",
               "mom_12m", "mom_3m"]
    spread = np.linspace(1.0, 100.0, 20)
    frame = pd.DataFrame({c: spread.copy() for c in columns})
    frame["ticker"] = [f"T{i}.NS" for i in range(20)]
    assert gem.score(frame)["g_score_coverage"].iloc[0] == pytest.approx(100.0)
