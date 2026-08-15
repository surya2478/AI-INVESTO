"""Pillars must degrade honestly when their inputs are missing.

The old `score` summed weighted components and stopped, so a missing input
removed its own weight without widening anyone else's. A pillar did not go
missing -- it shrank toward zero and kept voting. Measured on the 2019
point-in-time universe, quality had none of its five real inputs and came out a
CONSTANT 2.5 across all 69 companies, carrying no information at 20% of the
composite weight, while discovery kept turnover alone and spanned 0.4 to 30.0
where a pillar should span 100.

Two rules fix it and both are tested here: renormalise to available weight so
every pillar shares one scale, then shrink toward the neutral 50 in proportion
to coverage so a thinly-evidenced pillar cannot swing the answer as hard as a
fully-evidenced one.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from engine.config import GEM_WEIGHTS
from engine.scoring import gem

N = 40

# Every column `score` reads. Tests blank out what they mean to test.
INPUTS = [
    "rev_accel", "rev_cagr_2y", "operating_leverage", "margin_trend",
    "capex_intensity", "cash_conversion", "roe", "debt_equity",
    "promoter_pct", "promoter_pledge_pct", "market_cap", "turnover",
    "pe", "pb", "mom_12m", "mom_3m",
]


def universe(blank: list[str] | None = None, **overrides) -> pd.DataFrame:
    """A spread-out universe, optionally with some inputs unavailable."""
    spread = np.linspace(1.0, 100.0, N)
    frame = pd.DataFrame({name: spread.copy() for name in INPUTS})
    frame["ticker"] = [f"T{i}.NS" for i in range(N)]
    for name in blank or []:
        frame[name] = np.nan
    for name, value in overrides.items():
        frame[name] = value
    return frame


def scored(blank: list[str] | None = None, themes=None, **overrides) -> pd.DataFrame:
    return gem.score(universe(blank, **overrides), themes)


# --------------------------------------------------------------- the old bug
def test_a_pillar_with_no_inputs_is_neutral_not_a_constant_near_zero():
    """The 2019 quality case: every real input gone."""
    frame = scored(blank=["cash_conversion", "roe", "debt_equity",
                          "promoter_pct", "promoter_pledge_pct"])
    assert (frame["q_score"] == 50.0).all()
    assert (frame["q_score_coverage"] == 0.0).all()


def test_a_half_missing_pillar_still_spans_the_full_scale():
    """The 2019 discovery case: turnover alone spanned 0.4 to 30.0 of 100."""
    frame = scored(blank=["market_cap"])
    span = frame["d_score"].max() - frame["d_score"].min()
    assert span > 25.0, "renormalised to available weight, then shrunk by coverage"


def test_partial_data_is_not_scored_worse_than_no_data():
    """The perverse incentive: losing one input used to cost more than losing all.

    A company with four of five growth inputs must not rank below one with none.
    """
    partial = scored(blank=["rev_accel"])["g_score"]
    none_at_all = scored(blank=["rev_accel", "rev_cagr_2y", "operating_leverage",
                                "margin_trend", "capex_intensity"])["g_score"]
    assert partial.max() > none_at_all.max()


# ------------------------------------------------------------ renormalisation
def test_losing_an_input_does_not_move_the_middle_of_the_distribution():
    """Renormalisation keeps the centre where it was.

    Shrinkage pulls the extremes toward 50 -- deliberately -- so the invariant
    worth asserting is that it pivots ABOUT 50 rather than sliding the whole
    pillar down, which is what the unnormalised sum used to do.
    """
    full = scored().sort_index()["m_score"]
    partial = scored(blank=["mom_3m"]).sort_index()["m_score"]
    assert float(full.mean()) == pytest.approx(float(partial.mean()), abs=0.5)


def test_shrinkage_does_not_reorder_companies_within_a_pillar():
    """It is monotone in the score, so it changes confidence, never the ranking."""
    full = scored().sort_index()["m_score"]
    partial = scored(blank=["mom_3m"]).sort_index()["m_score"]
    assert list(full.rank()) == list(partial.rank())


def test_coverage_reports_the_weight_that_was_available():
    """Momentum is 60/40; losing the 40 leaves 60% coverage."""
    frame = scored(blank=["mom_3m"])
    assert frame["m_score_coverage"].iloc[0] == pytest.approx(60.0)


# ----------------------------------------------------------------- shrinkage
def test_thin_evidence_is_pulled_toward_neutral():
    """Half the weight means half the distance from 50, not half the scale."""
    full = scored()
    thin = scored(blank=["mom_3m"])
    full_gap = (full["m_score"] - 50.0).abs().max()
    thin_gap = (thin["m_score"] - 50.0).abs().max()
    assert thin_gap == pytest.approx(full_gap * 0.60, rel=0.05)


def test_a_fully_covered_pillar_is_not_shrunk():
    frame = scored()
    assert frame["m_score_coverage"].iloc[0] == pytest.approx(100.0)
    assert frame["m_score"].max() > 95.0


# ------------------------------------------------------------------- pledge
def test_pledge_is_zero_only_where_a_filing_says_so():
    """No shareholding filing at all is not evidence that nothing is pledged.

    Defaulting it to zero is what kept quality permanently non-empty, so it
    registered as a constant rather than as missing.
    """
    unfiled = scored(blank=["cash_conversion", "roe", "debt_equity",
                            "promoter_pct", "promoter_pledge_pct"])
    assert (unfiled["q_score_coverage"] == 0.0).all()

    # A filing exists (promoter_pct present) but reports no pledge: that IS zero.
    filed = scored(blank=["cash_conversion", "roe", "debt_equity",
                          "promoter_pledge_pct"])
    assert (filed["q_score_coverage"] > 0).all()


# ------------------------------------------------------------------ composite
def test_composite_coverage_is_the_weighted_average_of_the_pillars():
    frame = scored(blank=["mom_3m"])
    expected = sum(frame[f"{p}_coverage"].iloc[0] * w for p, w in GEM_WEIGHTS.items())
    assert frame["coverage"].iloc[0] == pytest.approx(expected)


def test_a_company_with_nothing_known_scores_exactly_neutral():
    frame = scored(blank=INPUTS)
    assert (frame["gem_score"] == 50.0).all()
    assert (frame["coverage"] == 0.0).all()
    assert (frame["pillars_missing"] == len(gem.PILLARS)).all()


def test_an_untracked_theme_is_neutral_and_reports_no_coverage():
    """Belonging to no theme is not evidence; it used to claim a 50 it had not earned."""
    frame = gem.score(universe(), theme_scores={})
    assert (frame["t_score"] == 50.0).all()
    assert (frame["t_score_coverage"] == 0.0).all()


def test_a_tracked_theme_carries_full_coverage():
    themes = {f"T{i}.NS": 80.0 for i in range(N)}
    frame = gem.score(universe(), theme_scores=themes)
    assert (frame["t_score"] == 80.0).all()
    assert (frame["t_score_coverage"] == 100.0).all()
