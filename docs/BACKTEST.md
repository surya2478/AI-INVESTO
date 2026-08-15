# G.E.M. backtest results

Re-run 15 Aug 2026, after fixing a look-ahead leak that voided the first run.

**The composite now ranks, but only because it is a small-cap-and-momentum tilt
measured across a small-cap melt-up. The fundamental pillars still contribute
nothing. Scores are still used to group, not to rank.**

## What was wrong with the 14 Aug run

`attach_market_data` read `securities.market_cap` — today's value — at every
historical ranking date. That put the future price into two pillars with the sign
reversed: a company that went on to triple carried a large cap *now*, so it ranked
as big (Discovery rewards small) and as expensive (Valuation divided a price
nobody had yet paid by earnings already reported). Discovery and Valuation carry
15% and 10% of the weight, and market cap is 70% of Discovery and all of
Valuation — **20.5% of the composite was an inverted copy of the answer.**

Market cap is now reconstructed as the close on the ranking date × the share
count published by then. On current data the reconstruction matches the stored
figure to a median 0.11%. At the July-2023 ranking date, the old code was using
caps a median of **1.76×** too large, p90 4.66×, max 119× — and the inflation was
largest for exactly the names that went up most.

The 14 Aug numbers should be discarded, not compared against.

## Result

3 annual rebalances (2023–2025), 12-month forward returns, 676–760 names each.

| Decile | 14 Aug (leaked) | 15 Aug (corrected) |
|---|---|---|
| 1 (lowest score) | 49.7% | 11.7% |
| 5 | 31.9% | 31.4% |
| 10 (highest score) | 42.0% | 55.0% |
| **Universe mean** | **31.3%** | **31.3%** |
| Top − bottom spread | −7.7 pts | **+43.3 pts** |
| Mean rank IC | −0.078 | **+0.167** |

The corrected deciles are monotonic: 11.7, 21.9, 21.9, 28.0, 31.4, 32.6, 34.7,
45.2, 45.7, 55.0.

## Per-pillar rank IC

| Pillar | Weight | 2023 | 2024 | 2025 | Mean (was) |
|---|---|---|---|---|---|
| Discovery | 15% | +0.33 | +0.15 | +0.02 | **+0.167** (−0.242) |
| Valuation | 10% | +0.32 | +0.07 | +0.03 | **+0.137** (−0.235) |
| Momentum | 10% | +0.30 | +0.01 | +0.05 | +0.119 (+0.119) |
| Theme | 20% | +0.12 | +0.02 | −0.00 | +0.047 (+0.047) |
| Growth | 25% | −0.08 | +0.03 | +0.09 | +0.016 (+0.016) |
| Quality | 20% | −0.12 | +0.02 | −0.05 | −0.047 (−0.047) |
| **Composite** | | **+0.33** | **+0.12** | **+0.06** | **+0.167** |

Only the two market-cap pillars moved. The other four are unchanged to three
decimal places, which is the check that the fix did one thing and nothing else.

## Read this before quoting the improvement

**The composite's IC equals Discovery's IC exactly (+0.167).** Adding five more
pillars to a size ranking bought nothing. Valuation (+0.137) is mostly the same
bet by another route — small companies are cheap on P/B. Momentum adds a little.

**Growth (25% weight) and Quality (20% weight) contribute nothing and slightly
negative.** Those two pillars are the entire intellectual claim of the score:
that growth inflection and cash-backed quality identify future multibaggers. On
this sample they do not. 45% of the weight is currently inert.

**The signal decays hard: 0.33 → 0.12 → 0.06.** The result is concentrated in the
2023–24 Indian small-cap run and has largely faded by 2025. A universe mean of
31.3% per year says what regime this was.

So the honest reading is not "the score works." It is: *a small-cap tilt beat a
market in which small caps ran, and the parts of the score that are supposed to
be doing the work were not doing it.*

## The PIT-only run — 15 Aug 2026, after annual XBRL ingestion

Annual point-in-time fundamentals now exist: 51,026 facts for 101 companies,
FY2018–FY2024, every one carrying the filing date NSE recorded. So
`harness.run(include_non_pit=False)` runs for the first time. Seven rebalance
dates, 2019–2025, 69–97 names.

| | Restated (Yahoo) | Point-in-time |
|---|---|---|
| Rebalance dates | 3 | 7 |
| Names per date | 676–760 | 69–97 |
| Mean rank IC | +0.167 | **+0.037** |
| Yearly IC | +0.33, +0.12, +0.06 | +0.09, +0.01, **−0.22**, +0.00, **+0.42**, +0.07, **−0.12** |
| Top − bottom | +43.3 pts | +11.0 pts |
| Deciles monotonic | yes | **no** |

**On point-in-time data the score shows no skill.** Mean IC +0.037 is
indistinguishable from zero, the sign flips in three of seven years, and the
whole mean rests on 2023 (+0.42) — the smallcap melt-up year. The decile ladder
is not monotonic: the bottom decile (60.3%) beats deciles 3 through 8.

Two things make even that flattering. The universe is 97 theme names rather than
760, so it is narrow, pre-selected and thematically correlated, with a mean
return of 54.4% a year. And the deciles hold about ten names each.

## What the PIT run actually exposed: the score cannot consume partial data

This matters more than the IC. Pillars do not go missing when their inputs go
missing — they silently collapse to whichever component still has data, at a
fraction of their intended scale, and enter the composite as though nothing
happened. `gem.score` sums weighted components with `min_count=1` and never
renormalises to available weight.

At 2019-07-01, across 69 companies:

| Pillar | Inputs present | What it actually was | Range |
|---|---|---|---|
| Quality | 0 of 5 | pledge% defaulted to zero for everyone | **constant 2.5** |
| Discovery | turnover only | turnover rank at 30% of scale | 0.4–30.0 |
| Valuation | none | correctly NaN | — |

A real pillar spans ~100. Quality was a **constant**, carrying no information at
20% of the composite weight, and Discovery carried turnover at 30% strength with
size — 70% of it — silently absent. The 2019–2022 dates are therefore not
measuring the score described in the spec; they are measuring growth, momentum
and theme with two stubs attached.

Even 2023 is not clean: `cash_conversion` — the heaviest input in Quality, and
the one the module calls hardest to manufacture — is absent for all 95 names,
because it needs three years of annual cash flow and point-in-time CFO only
begins at FY2022.

**So the blocker is no longer data.** It is that the scoring code has no concept
of coverage. Available-weight renormalisation, shrinkage toward 50 in proportion
to missingness, and a visible coverage score have to land before another
backtest number is worth reading.

## After renormalisation — the measurement got honest, the score did not get better

Each pillar is now a weighted mean over the weight actually available, shrunk
toward 50 in proportion to coverage, and `scores.coverage` records what share of
the composite rests on evidence rather than on that default.

What that does to the same universe:

| Date | Composite coverage | Pillars at zero coverage |
|---|---|---|
| 2019-07-01 | **34%** | Growth, Quality, Valuation |
| 2023-07-01 | **86%** | none (Quality at 49%, cash conversion still absent) |

The 2019 ranking was always 34% evidence and 66% neutral default. It now says so,
and the three empty pillars sit flat at 50 instead of voting as a constant and a
stub.

**It did not improve the result.**

| | Before renormalisation | After |
|---|---|---|
| PIT-only mean IC | +0.037 | **+0.044** |
| Restated mean IC | +0.167 (3 dates) | **+0.139** (4 dates) |

PIT-only per-year: +0.12, +0.01, **−0.22**, +0.08, **+0.40**, +0.05, **−0.14**.
Still no skill, still sign-unstable, still resting entirely on 2023. The restated
run now reaches back to 2022 because annual PIT ingestion moved the earliest
usable rebalance date, so its mean is over four dates rather than three; on the
three shared dates it went +0.167 to +0.158.

This is the point of the fix and it is worth stating plainly: renormalisation was
necessary to stop the backtest measuring something other than the score, and it
bought no performance. The composite still does not rank on point-in-time data.

## After theme exposure — the first change that improved anything

Theme membership was a bit, not a degree. `theme_confluence` gave a company the
FIRST theme it appeared in via `setdefault`, so CG Power was scored on AI &
Compute and never on Grid Infrastructure purely because of YAML ordering. And
every member scored as a pure play, so TCS — whose config entry calls quantum
"a rounding error in each of these companies' revenue" — got the same tailwind as
a company that does nothing else.

Confluence is now averaged across a company's themes weighted by exposure, and
total exposure is the pillar's coverage. At 2025-07-01:

| Company | Blended confluence | Exposure | T pillar |
|---|---|---|---|
| CG Power | 75.3 | 100% | 75.3 |
| MTAR | 79.4 | 100% | 79.4 |
| TCS | 70.2 | 15% | **53.0** |
| Infosys | 72.8 | 10% | **52.3** |
| L&T | 53.2 | 30% | 51.0 |

| | Before exposure | After |
|---|---|---|
| Theme pillar mean IC | +0.107 | **+0.164** |
| Composite mean IC | +0.044 | **+0.061** |
| Top − bottom | 11.1 pts | **23.5 pts** |

The theme pillar is positive in five of seven years and is now the strongest
pillar in the score.

**Read the caveat before believing the number.** The exposure values are
hand-authored judgements, written from what config/themes.yaml already said in
prose — "adjacency only", "optionality within larger businesses, not pure-play
exposure", "a small revenue slice today" — and set BEFORE any IC was measured.
They are not segment revenue. Tuning them until the backtest improves is exactly
the overfitting this document exists to refuse; if they are to be revised it
should be against reported segment disclosures, not against this table.

**And the composite is still worse than its own best pillar.** Theme alone scores
+0.164; the composite scores +0.061. Quality at −0.160 and Growth at −0.033 are
subtracting, at 20% and 25% of the weight. Five of the six pillars are not
earning their place.

## Why growth fails — measured, not guessed

| Input | Weight | Mean IC |
|---|---|---|
| `rev_accel` | 35% | −0.018 |
| `rev_cagr_2y` | 25% | −0.073 |
| `operating_leverage` | 20% | **+0.122** |
| `margin_trend` | 10% | −0.040 |
| `capex_intensity` | 10% | −0.098 |

Sixty per cent of the pillar's weight sat on the two inputs earning nothing, and
the only component that worked carried twenty.

**Revenue growth does not predict forward returns in this sample, in any
measurement.** Four variants tested against the same universe: the shipped
overlapping acceleration −0.018, a clean non-overlapping acceleration −0.036,
year-on-year growth −0.090, two-year CAGR −0.105. All flat to negative, sign
unstable. This is not an implementation defect hiding a good signal.

The likely mechanism is valuation, not growth: `rev_cagr_2y` correlates +0.14 to
+0.27 with P/E, while `operating_leverage` — the one input that survives — has no
such loading at −0.04 to −0.05. High-growth names were the expensive ones, and
paying up is what cost. That is a statement about 2021-25, not a law.

Note also what `operating_leverage` actually is: PAT CAGR minus revenue CAGR,
i.e. margin expansion. The growth pillar's only working component is not a growth
signal.

### Three hygiene fixes, and what they did

Chosen on correctness grounds, not by looking at returns; re-measured afterwards.

1. `rev_4y` was named for four years and computed three.
2. Acceleration compared a 2-year CAGR against a 3-year CAGR — a window against a
   superset of itself. It now compares the latest year against the prior year.
3. `capex_intensity` was dropped. The thesis was fine (spending ahead of demand
   precedes growth) but capex over revenue measures how capital-hungry an
   industry is; a cement plant and a company doubling capacity look identical.
   Remaining weights were left untouched.

| | Before | After |
|---|---|---|
| `rev_accel` vs `rev_cagr_2y` correlation | **+0.44 to +0.76** | **−0.22 to +0.08** |
| Growth coverage (2021 / 2024) | 35% / 88% | **79% / 94%** |
| Growth pillar mean IC | −0.033 | −0.012 |
| Composite mean IC | +0.061 | +0.072 |

The structural aims were met: acceleration is now independent of the level rather
than a restatement of it, and more companies are scorable. The return improvement
is small and well inside noise for seven observations — it should not be read as
the fix working. Growth still earns nothing, which is what the four-variant test
above already said it would.

## Why quality fails — and the feed that was hiding half the question

`ownership_pit` held 515 rows sharing ONE filing_date: the day the scraper ran.
Since the scoring read filters `filing_date <= as_of`, promoter holding and
pledge were invisible at every rebalance date. **15% of the quality pillar's
weight had never been populated in any backtest that had ever run.**

The cause was the endpoint, not the exchange. NSE's pledge feed returns one
record per company with no history. A different endpoint,
`corporate-share-holdings-master`, returns ~23 quarters each carrying the
broadcast timestamp NSE recorded. Backfilled: **2,048 rows, 101 companies,
425 distinct filing dates**, some reaching back to 2015. Filing lags are real
and vary from 3 to 236 days — CG Power filed its Dec-2023 pattern seven months
late, against a 20-day norm, and lateness is itself what the gates look for.

| | Before | After |
|---|---|---|
| Promoter % coverage, 2022–25 | **0%** | **100%** |
| Quality coverage at 2023 / 2025 | 49% / 97% | **64% / 97%** |
| Dates where quality is scorable | 3 | **4** |
| Quality mean IC | −0.160 | **−0.071** |
| Composite mean IC | +0.072 | +0.073 |

So the pillar became measurable and stayed negative. What drives it is not the
missing ownership data:

| Input | Weight | Coverage | Contribution |
|---|---|---|---|
| `cash_conversion` | 35% | 0% at 2023, 91% after | ~0 |
| `roe` | 30% | 99–100% from 2023 | **−0.156** |
| `debt_equity` | 20% | 99–100% from 2023 | **−0.127** (ranked descending) |
| `promoter_pct` | 10% | now 100% from 2022 | +0.036 |
| `pledge` | 5% | **still 0%** | none |

**The mechanism is size, not valuation.** Unlike growth, ROE does not load on
price — it correlates −0.16 to −0.28 with P/E, i.e. high-ROE names were *cheaper*.
What it loads on is size: corr(ROE, market cap) +0.36 to +0.40, and
corr(debt/equity, market cap) −0.22 to −0.25. High-ROE, low-debt companies are
the large ones, and in a universe returning 54% a year on a small-cap melt-up,
quality was a short-small-caps bet in disguise.

**Which puts it in direct opposition to Discovery.** Correlation between the two
pillars runs −0.33, −0.49, −0.52 and worsens each year. Discovery puts 70% of its
weight on *small*; quality's two live inputs both proxy *large*. They are 20% and
15% of the composite cancelling each other by construction — a large part of why
the composite (+0.073) scores below its best single pillar (+0.164).

Do NOT flip `debt_equity`. "Leverage outperformed across three years of a
cyclical upswing" is the most regime-dependent claim in this document, and
inverting a solvency preference on it would be the worst trade in the file.

### Size-neutral ranking — the confound is gone, the pillar is not fixed

Quality's five inputs are now ranked inside size quintiles rather than across the
whole universe, so the question is whether a company is well run FOR ITS SIZE.
Discovery and Valuation are deliberately left alone: ranking on size is
Discovery's entire purpose, and the small-company discount is Valuation's signal
rather than a confound.

| | Before | After |
|---|---|---|
| corr(quality, market cap) | +0.36 / +0.38 / +0.40 | **+0.035 / +0.020 / +0.027** |
| corr(quality, discovery) | −0.33 / −0.49 / −0.52 | **−0.020 / −0.061 / −0.089** |
| Quality mean IC | −0.071 | −0.042 |
| Composite mean IC | +0.073 | +0.078 |

The size loading is gone and the two pillars have stopped cancelling — 35% of the
composite is no longer spent on a contradiction. That was the aim and it is
comprehensively met.

**Quality is still negative.** Which settles something: the residual is NOT the
size confound, because the confound is now absent and the sign did not change.
It is either a genuine property of this regime, or noise on four observations —
and four observations cannot tell those apart. The composite improvement of
+0.005 is well inside noise and should not be read as the fix working.

Pledge remains without a historical source: it is not in the shareholding feed,
and the pledge endpoint currently returns empty payloads for every symbol.

## The expanded universe — 662 companies, and the answer changes shape

Every result above ran on the 102 theme names, so deciles held about ten
companies each. Annual XBRL is now backfilled across the whole investable
universe: **662 companies with annual point-in-time data**, 82,573 facts, and
`share_count` reaching FY2018 — so historical market cap is computable and
Discovery and Valuation stop defaulting to neutral in the early years.

Eight rebalance dates, 305 to 662 names, deciles of 30 to 66.

| | Theme universe (97) | Full universe (662) |
|---|---|---|
| Rebalance dates | 7 | **8** (from 2018) |
| Names per date | 69–97 | **305–662** |
| Mean rank IC | +0.078 | **+0.083** |
| Top − bottom | 21.3 pts | 20.5 pts |

Per year: −0.02, +0.02, +0.12, −0.05, +0.15, +0.30, +0.10, +0.05. Positive in
six of eight.

### Which pillars survive a real universe

| Pillar | Weight | Mean IC (97 names) | **Mean IC (662)** | Coverage |
|---|---|---|---|---|
| Valuation | 10% | +0.070 | **+0.149** | 46–48%, 2023 on |
| Discovery | 15% | −0.012 | **+0.133** | 89–100% |
| Theme | 20% | +0.164 | **+0.066** | **11–15%** |
| Momentum | 10% | +0.068 | +0.060 | 95–100% |
| Growth | 25% | −0.012 | −0.008 | 0–85% |
| Quality | 20% | −0.042 | −0.027 | 0–75% |
| **Composite** | | +0.078 | **+0.083** | 26–68% |

Three things moved, and each says something different.

**Discovery went from −0.012 to +0.133** — not because the pillar changed but
because it finally has data. Market cap was missing before FY2023, so the pillar
was 30% covered and mostly neutral; it is now 89–100% covered. This is the size
effect, measurable at last.

**Theme collapsed from +0.164 to +0.066** — and that is honest rather than
disappointing. The theme graph tags 102 of 662 names, so coverage is 11–15% and
the pillar correctly abstains on the rest. Its earlier strength was measured on a
universe made entirely of theme members. It has not got worse; it is being asked
a fair question for the first time.

**Growth and Quality still earn nothing** at 45% of the combined weight, now
across eight dates and up to 662 names rather than three dates and 95.

### The composite is still worse than its best pillar

Valuation alone scores +0.149 and Discovery +0.133; the composite scores +0.083.
Weighting six pillars together, four of which are flat or negative, produces
something worse than either of the two that work. That has now been true in every
configuration tested.

And note what the two that work are: cheapness and smallness, across 2018–2025 in
Indian equities. That is the value-and-size premium in a small-cap bull market —
the most heavily documented pair of factors in the literature, and not what a
theme-driven multibagger screener claims to be doing.

### Read the coverage column before the IC column

Composite coverage runs 26% at 2018 to 68% at 2025. The early dates rank on
roughly a quarter evidence and three-quarters neutral default, because cash flow
starts at FY2022 and the balance sheet at FY2023. The 2018 and 2019 ICs are
nearly meaningless as tests of the score as designed.

## Biases — every number above still overstates the score's skill

1. **Restatement in place** (restated run only). Yahoo reports current values, so
   a 2023 ranking may use figures revised in 2025. The PIT-only run above
   excludes those rows; the run mode travels with the result as `pit_only`.
   The point-in-time corpus has its own ceiling, measured rather than assumed:
   annual XBRL exists from about FY2018 and carries the P&L throughout, but the
   balance sheet and cash-flow statement only from FY2023. Seven years of
   growth history; two of quality. Testing the quality pillar still needs a paid
   vendor.
2. **Survivorship.** The universe is today's NIFTY TOTAL MARKET; companies that
   failed and delisted are absent. Their absence lifts the bottom decile most,
   so the true spread is probably wider and the true bottom-decile return worse.
3. **Assumed filing lag** of 90 days, not observed.
4. **No costs.** No brokerage, taxes, slippage, impact, or participation limits.
   Small caps are where those bite hardest, and the result is a small-cap tilt.
5. **n = 3.** Three observations, overlapping regime, no held-out period.

## Why the weights are still not tuned

Nothing here justifies re-weighting toward Discovery. Doing so would fit a
smallcap-beta bet to three years of a smallcap bull market and call it a stock
picker. The correct response to "45% of the weight is inert" is to find out
whether Growth and Quality fail because the thesis is wrong or because the
inputs are too sparse and too restated to test — and that question is answered by
ingesting annual point-in-time fundamentals, not by moving weights.

## What would change the conclusion

Annual point-in-time fundamentals over ten or more years, a universe including
delisted names, and costs. That is the paid-vendor decision, and this is the
evidence for it.
