"""Quality is judged against size peers, not against the whole universe.

Ranked universe-wide, the pillar was measuring size. ROE correlates +0.36 to
+0.40 with market cap and debt-to-equity -0.22 to -0.25, so high-ROE low-debt
companies are simply the large ones -- which made quality a short-small-caps bet
and set it directly against Discovery, whose weight is 70% on smallness. The two
pillars ran at -0.33 to -0.52 correlation and cancelled: 35% of the composite
spent on a contradiction.

Ranking inside a size cohort asks what the pillar meant to ask -- is this well
run FOR ITS SIZE -- and is deliberately not applied to Discovery, whose entire
purpose is to rank on size.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from engine.scoring import gem

OTHER_INPUTS = ["rev_accel", "rev_cagr_2y", "operating_leverage", "margin_trend",
                "turnover", "pe", "pb", "mom_12m", "mom_3m",
                "promoter_pct", "promoter_pledge_pct", "cash_conversion"]


def universe(n: int, roe, market_cap, debt_equity=None) -> pd.DataFrame:
    frame = pd.DataFrame({c: np.linspace(1.0, 100.0, n) for c in OTHER_INPUTS})
    frame["ticker"] = [f"T{i}.NS" for i in range(n)]
    frame["roe"] = roe
    frame["market_cap"] = market_cap
    frame["debt_equity"] = np.linspace(0.1, 2.0, n) if debt_equity is None else debt_equity
    return frame


# ------------------------------------------------------------------- cohorts
def test_companies_are_split_into_size_cohorts():
    caps = pd.Series(np.linspace(1e9, 1e12, 100))
    cohorts = gem._size_buckets(caps)
    assert cohorts.nunique() == gem.SIZE_BUCKETS


def test_a_universe_too_small_to_bucket_falls_back_to_one_cohort():
    """Ranking inside a bucket of four is an artefact of the bucket boundary."""
    caps = pd.Series(np.linspace(1e9, 1e12, 10))
    assert gem._size_buckets(caps).nunique() == 1


def test_companies_of_unknown_size_get_their_own_cohort():
    """Not knowing a company's size is a different claim from it being small."""
    caps = pd.Series(list(np.linspace(1e9, 1e12, 60)) + [np.nan] * 20)
    cohorts = gem._size_buckets(caps)
    unknown = cohorts[caps.isna()]
    assert unknown.nunique() == 1
    assert set(unknown) & set(cohorts[caps.notna()]) == set()


# ------------------------------------------------------- the size loading goes
def test_quality_no_longer_tracks_size():
    """The defect: ROE rising with size made q_score a proxy for bigness."""
    n = 100
    caps = np.linspace(1e9, 1e12, n)
    roe_tracks_size = np.linspace(5.0, 40.0, n)          # big companies earn more
    frame = universe(n, roe=roe_tracks_size, market_cap=caps)

    scored = gem.score(frame)
    loading = scored["q_score"].corr(scored["market_cap"], method="spearman")
    assert abs(loading) < 0.35, f"quality still loads on size at {loading:+.2f}"


def test_a_small_company_can_out_score_a_large_one_on_quality():
    """Best-in-cohort beats mid-of-cohort regardless of absolute size."""
    n = 100
    caps = np.linspace(1e9, 1e12, n)
    roe = np.linspace(5.0, 40.0, n)
    roe[0] = 39.0          # smallest company, near-top ROE for the whole market
    frame = universe(n, roe=roe, market_cap=caps).set_index("ticker")
    scored = gem.score(frame.reset_index()).set_index("ticker")
    assert scored.loc["T0.NS", "q_score"] > scored.loc["T50.NS", "q_score"]


def test_quality_and_discovery_stop_cancelling():
    """They ran at -0.33 to -0.52 by construction, 35% of the composite."""
    n = 100
    caps = np.linspace(1e9, 1e12, n)
    frame = universe(n, roe=np.linspace(5.0, 40.0, n), market_cap=caps)
    scored = gem.score(frame)
    opposition = scored["q_score"].corr(scored["d_score"], method="spearman")
    assert opposition > -0.35, f"q and d still oppose at {opposition:+.2f}"


# --------------------------------------------------------- what is NOT changed
def test_discovery_still_ranks_on_size():
    """Neutralising the pillar whose purpose is size would empty it out."""
    n = 100
    caps = np.linspace(1e9, 1e12, n)
    frame = universe(n, roe=np.linspace(5.0, 40.0, n), market_cap=caps)
    scored = gem.score(frame)
    loading = scored["d_score"].corr(scored["market_cap"], method="spearman")
    assert loading < -0.8, "small must still score high on discovery"


def test_ranking_within_a_cohort_still_discriminates():
    """Smaller comparison groups must not flatten the pillar into a constant.

    Two things about this fixture shape the expectation. Its inputs oppose each
    other by construction -- ROE rises with the index while debt-to-equity is
    ranked descending -- so the achievable span is well under 100. And every
    input is monotone in the index, so each cohort is a contiguous slice and all
    five produce the SAME within-cohort ranks; full separation is therefore one
    distinct score per cohort member, not per company.
    """
    n = 100
    frame = universe(n, roe=np.linspace(5.0, 40.0, n),
                     market_cap=np.linspace(1e9, 1e12, n))
    scored = gem.score(frame)
    assert scored["q_score"].nunique() >= n // gem.SIZE_BUCKETS
    assert scored["q_score"].max() - scored["q_score"].min() > 40.0


def test_missing_inputs_still_report_coverage_honestly():
    """Size neutrality must not quietly resurrect the partial-missingness bug."""
    n = 100
    frame = universe(n, roe=np.linspace(5.0, 40.0, n),
                     market_cap=np.linspace(1e9, 1e12, n))
    frame["cash_conversion"] = np.nan
    scored = gem.score(frame)
    assert scored["q_score_coverage"].iloc[0] == pytest.approx(65.0)
