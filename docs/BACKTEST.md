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

## Biases — every number above still overstates the score's skill

1. **Restatement in place.** Yahoo reports current values, so a 2023 ranking may
   use figures revised in 2025. `harness.run(include_non_pit=False)` is the
   switch that excludes them, and it is **not yet usable**: the true
   point-in-time corpus (NSE XBRL, parsed PDFs) is 39 securities and
   **quarterly only**, while the pillars rank on annual periods, so a PIT-only
   run returns nothing at all. Annual PIT ingestion is the prerequisite for a
   result that means anything. The run mode now travels with the result as
   `pit_only`.
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
