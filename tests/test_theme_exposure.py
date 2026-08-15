"""Theme membership is a matter of degree, and the engine used to treat it as a bit.

Two defects, both visible in config/themes.yaml's own prose before any code was
read. `theme_confluence` assigned a company the FIRST theme it appeared in via
`setdefault`, so CG Power was scored on AI & Compute and never on Grid
Infrastructure purely because of the order themes are written in the file. And
every member of a theme scored as a pure play, so TCS -- whose config entry says
quantum is "a rounding error in each of these companies' revenue" -- received the
same tailwind as a company that does nothing else.

Exposure fixes both: confluence is averaged across a company's themes weighted by
exposure, and total exposure becomes the pillar's coverage, so a 5% exposure moves
the score 5% of the way from neutral.
"""

from __future__ import annotations

import textwrap

import numpy as np
import pandas as pd
import pytest

from engine.scoring import gem
from engine.universe.theme_graph import load_theme_graph

N = 6


def universe(tickers: list[str]) -> pd.DataFrame:
    """A frame with every input `score` reads, so only the theme pillar varies."""
    spread = np.linspace(1.0, 100.0, len(tickers))
    columns = ["rev_accel", "rev_cagr_2y", "operating_leverage", "margin_trend",
               "capex_intensity", "cash_conversion", "roe", "debt_equity",
               "promoter_pct", "promoter_pledge_pct", "market_cap", "turnover",
               "pe", "pb", "mom_12m", "mom_3m"]
    frame = pd.DataFrame({c: spread.copy() for c in columns})
    frame["ticker"] = tickers
    return frame


def t_score_of(scores, exposure, ticker="A.NS") -> float:
    tickers = ["A.NS", "B.NS", "C.NS"]
    frame = gem.score(universe(tickers), scores, exposure).set_index("ticker")
    return float(frame.loc[ticker, "t_score"])


# ------------------------------------------------------------------ exposure
def test_a_bit_part_player_does_not_ride_a_theme_like_a_pure_play():
    """The headline requirement: 5% of a company is not the whole company."""
    hot = {"A.NS": 90.0, "B.NS": 90.0, "C.NS": 90.0}
    pure = t_score_of(hot, {"A.NS": 1.0, "B.NS": 1.0, "C.NS": 1.0})
    sliver = t_score_of(hot, {"A.NS": 0.05, "B.NS": 1.0, "C.NS": 1.0})
    assert pure == pytest.approx(90.0)
    assert sliver == pytest.approx(52.0)          # 50 + (90-50) * 0.05


def test_exposure_scales_the_distance_from_neutral_not_the_score():
    """A cold theme at low exposure must move UP toward 50, not down."""
    cold = {"A.NS": 10.0}
    assert t_score_of(cold, {"A.NS": 1.0}) == pytest.approx(10.0)
    assert t_score_of(cold, {"A.NS": 0.25}) == pytest.approx(40.0)


def test_exposure_becomes_the_pillars_coverage():
    frame = gem.score(universe(["A.NS", "B.NS", "C.NS"]),
                      {"A.NS": 90.0}, {"A.NS": 0.25}).set_index("ticker")
    assert float(frame.loc["A.NS", "t_score_coverage"]) == pytest.approx(25.0)
    assert float(frame.loc["B.NS", "t_score_coverage"]) == 0.0


def test_no_exposure_map_means_every_member_is_a_pure_play():
    """Backwards compatible: this is what the engine did before exposure existed."""
    assert t_score_of({"A.NS": 90.0}, None) == pytest.approx(90.0)


# --------------------------------------------------------- the setdefault bug
def test_a_company_in_two_themes_blends_them_rather_than_taking_the_first():
    """`setdefault` made YAML ordering decide which theme a company was scored on."""
    scores, exposures = gem.blend_theme_exposure(
        {"A.NS": [(90.0, 1.0), (30.0, 1.0)]}            # hot theme, cold theme
    )
    assert scores["A.NS"] == pytest.approx(60.0)        # neither 90 nor 30
    assert exposures["A.NS"] == pytest.approx(1.0)


def test_the_blend_is_weighted_by_exposure_not_a_plain_average():
    """Being mostly a grid company and marginally a quantum one is not half each."""
    scores, _ = gem.blend_theme_exposure(
        {"A.NS": [(90.0, 1.0), (30.0, 0.05)]}           # grid pure play, quantum sliver
    )
    assert scores["A.NS"] == pytest.approx(87.1, abs=0.1)
    assert scores["A.NS"] > 60.0                        # a plain mean would say 60


def test_total_exposure_is_capped_at_one_whole_company():
    scores, exposures = gem.blend_theme_exposure(
        {"A.NS": [(80.0, 0.5), (80.0, 0.5), (80.0, 0.5)]}
    )
    assert exposures["A.NS"] == pytest.approx(1.0)
    assert scores["A.NS"] == pytest.approx(80.0)


def test_a_company_with_no_theme_exposure_is_dropped_rather_than_divided_by_zero():
    scores, exposures = gem.blend_theme_exposure({"A.NS": []})
    assert "A.NS" not in scores and "A.NS" not in exposures


# --------------------------------------------------------------- yaml parsing
def parse(text: str):
    import yaml
    from engine.universe.theme_graph import _parse_node_tickers
    return _parse_node_tickers(yaml.safe_load(textwrap.dedent(text)), "INDIA")


def test_a_bare_ticker_list_still_means_pure_play():
    tickers, exposures = parse("""
        id: n
        tickers: [CGPOWER, SIEMENS]
    """)
    assert tickers == ["CGPOWER.NS", "SIEMENS.NS"]
    assert set(exposures.values()) == {1.0}


def test_a_node_level_exposure_applies_to_every_member():
    _, exposures = parse("""
        id: n
        exposure: 0.1
        tickers: [TCS, INFY]
    """)
    assert exposures == {"TCS.NS": 0.1, "INFY.NS": 0.1}


def test_a_ticker_can_override_its_nodes_default():
    _, exposures = parse("""
        id: n
        exposure: 0.1
        tickers:
          - TCS
          - {ticker: LTTS, exposure: 0.4}
    """)
    assert exposures == {"TCS.NS": 0.1, "LTTS.NS": 0.4}


@pytest.mark.parametrize("bad", [0.0, -0.2, 1.5])
def test_an_exposure_outside_zero_to_one_is_rejected(bad):
    """It is a share of a company; 1.5 of one is not a thing."""
    with pytest.raises(ValueError, match="share of the company"):
        parse(f"""
            id: n
            exposure: {bad}
            tickers: [TCS]
        """)


# ------------------------------------------------------- within-theme nodes
def test_two_nodes_of_one_theme_do_not_double_the_exposure():
    """A transformer maker in both the DC power chain and interconnect is still
    one company exposed to one theme."""
    graph = load_theme_graph()
    ai = graph.theme("ai_compute")
    for ticker, exposure in ai.india_exposures.items():
        assert 0.0 < exposure <= 1.0, ticker


# ------------------------------------------------------- the shipped config
def test_the_shipped_config_marks_its_adjacencies():
    """themes.yaml says quantum is a rounding error; the graph must agree."""
    graph = load_theme_graph()
    assert graph.theme("quantum").india_exposures["TCS.NS"] == pytest.approx(0.05)
    assert graph.theme("nuclear").india_exposures["LT.NS"] == pytest.approx(0.15)
    # And a genuine pure play is untouched.
    assert graph.theme("grid_infra").india_exposures["CGPOWER.NS"] == pytest.approx(1.0)
