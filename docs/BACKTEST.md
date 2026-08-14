# G.E.M. backtest results

Run 14 Aug 2026. **The composite does not rank. Scores are used to group.**

## Result

3 annual rebalances (2023–2025), 12-month forward returns, 676–760 names each.

| Decile | Avg forward return |
|---|---|
| 1 (lowest score) | 49.7% |
| 5 | 31.9% |
| 10 (highest score) | 42.0% |
| **Universe mean** | **31.3%** |

Top decile beats the universe by **+10.7 points**. The bottom decile beats the
top. The relationship is **U-shaped, not monotonic** — which is why mean rank
IC is **−0.078** despite a good top decile.

## Per-pillar rank IC

| Pillar | Weight | 2023 | 2024 | 2025 | Mean |
|---|---|---|---|---|---|
| Momentum | 10% | +0.30 | +0.01 | +0.05 | **+0.119** |
| Theme | 20% | +0.12 | +0.02 | −0.00 | +0.047 |
| Growth | 25% | −0.08 | +0.03 | +0.09 | +0.016 |
| Quality | 20% | −0.12 | +0.02 | −0.05 | −0.047 |
| Valuation | 10% | −0.09 | −0.29 | −0.33 | **−0.235** |
| Discovery | 15% | −0.10 | −0.25 | −0.38 | **−0.242** |

## Why the weights were not tuned

Reversing Discovery and Valuation would produce a good-looking backtest
immediately. It was not done.

Three years of a momentum-led Indian bull market is the wrong sample on which to
conclude that small companies cannot multiply or that overpaying does not cost.
Those pillars encode a 5–10 year thesis; this window measures a regime. Fitting
weights to three observations produces a confident-looking score with no basis,
which is the exact failure the backtest requirement existed to prevent.

Also relevant: fixing one bug (the theme pillar was a constant 50) moved the top
decile from 24.2% to 42.0%. A composite that swings 18 points on one pillar,
measured over three observations, is not stable enough to rank anything.

## Biases — every number above overstates the score's skill

1. **Restatement in place.** Yahoo reports current values, so a 2023 ranking may
   use figures revised in 2025. Real point-in-time data would not.
2. **Survivorship.** The universe is today's NIFTY TOTAL MARKET; companies that
   failed and delisted are absent.
3. **Assumed filing lag** of 90 days, not observed. Conservative, but assumed.

## What would change the conclusion

Ten or more years of point-in-time fundamentals, which no free source provides.
That is the paid-vendor decision, and this is the evidence for it.
