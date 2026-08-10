# AI-Investo

A personal research engine for finding early-stage compounders in India using
global sector trends as the leading indicator.

**Not investment advice.** This tool ranks and explains; it does not decide.
Every buy and sell decision is the user's own.

Full product specification: `docs/SPEC.md`

## Why this exists

Paid platforms each cover one slice. Screener.in has deep Indian financials but
no global context; Koyfin has global breadth but weak Indian smallcap coverage;
MarketsMojo and Trendlyne ship black-box scores that are never backtested for
you. None of them connect a *global* theme to its *Indian* derivative and tell
you the India leg has not moved yet.

That lag is measurable, and measuring it is the point of this project.

## Setup

```bash
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
```

## Usage

```bash
python -m engine.pipeline init            # create the database
python -m engine.pipeline validate        # probe every theme ticker
python -m engine.pipeline ingest prices   # pull daily bars
python -m engine.pipeline coverage        # what landed, what is missing
```

Run `validate` after every edit to `config/themes.yaml`. A mistyped symbol
silently shrinks a theme basket and biases its index, so the validator treats
an unresolved ticker as a config bug rather than dropping it quietly.

## Layout

```
engine/providers/   data sources behind one interface (swap free -> paid)
engine/universe/    theme graph: global leg vs India value-chain leg
engine/features/    trend indicators, fundamental features
engine/scoring/     quality gates + G.E.M. score
engine/backtest/    point-in-time harness
config/themes.yaml  the theme graph -- the main thing worth reviewing
```

## The point-in-time contract

Reported financials carry a `filing_date`: the date the number became public.
Historical reads go through `db.fundamentals_asof()`, which filters
`filing_date <= as_of_date`. Yahoo is deliberately *not* used for fundamentals
because it exposes no filing date, and substituting the period end would make
figures look public ~45 days before they were, inflating every backtest.

## Status

- [x] Stage 0 — foundation: schema, providers, theme graph, price ingest
- [ ] Stage 1 — trend engine + theme propagation
- [ ] Stage 2 — fundamentals, quality gates, G.E.M. score
- [ ] Stage 3 — backtest and weight calibration
- [ ] Stage 4 — API + PWA
- [ ] Stage 5 — portfolio, staged accumulation, journal
- [ ] Stage 6 — alerts and automation
