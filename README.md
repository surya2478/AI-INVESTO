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

## LLM extraction

Post-2024 quarterly financials exist only as PDFs, so they are extracted with a
model routed through OpenRouter. Put the key in `.env` (gitignored):

```
OPENROUTER_API_KEY=sk-or-v1-...
INVESTO_LLM_MODEL=anthropic/claude-sonnet-5
INVESTO_PDF_ENGINE=native
```

The model is config, not code, because accuracy and cost trade off sharply —
152 OpenRouter models accept PDFs and enforce JSON schemas, spanning roughly
20x in price. Measure before choosing:

```bash
python scripts/validate_pdf_extraction.py --companies 5 --model anthropic/claude-sonnet-5
```

That extracts quarters we already hold XBRL for, compares every metric against
the known-correct figure at 0.5% tolerance, prints real spend per statement and
a projected backfill cost, and exits non-zero below 95%. Nothing extracted feeds
a score until it passes.

Two settings are deliberate. `INVESTO_PDF_ENGINE=native` overrides OpenRouter's
`mistral-ocr` default, which bills $2/1,000 pages to OCR documents that are
already digital text and flattens the table structure the figures live in. And
`provider.require_parameters` makes a request fail rather than route to an
endpoint that ignores the schema — an unvalidated blob that looks like an answer
is worse than a clear error.

## Automation

A Windows scheduled task, `AI-Investo Nightly`, runs `scripts/run_nightly.cmd`
at 06:30 daily and appends to `reports/nightly.log`. It is set to
StartWhenAvailable, so a machine that was asleep catches up rather than losing
the night.

Seven stages run, each isolated — a failing stage is recorded and the rest still
run. A nightly job that aborts halfway hides the failure until you go looking
for data that never arrived.

The filings backfill is deliberately incremental. BSE stops responding under
sustained pagination rather than returning a 429, so the job walks three
18-day windows per night and stores its cursor in `job_state`. A throttled
night costs a night, not the backfill. It reaches the Oct-2024 floor in about
13 nights.

That floor is not the start of history on purpose: NSE's XBRL already carries
true filing dates through Dec-2024, so BSE announcements are only needed to
timestamp PDF-extracted quarters from 2025 onward. The overlap into the XBRL
era lets the two sources' filing dates be cross-checked.

```bash
schtasks /query /tn "AI-Investo Nightly"     # check it
schtasks /run   /tn "AI-Investo Nightly"     # run it now
schtasks /delete /tn "AI-Investo Nightly" /f # remove it
```

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
      (2.3M bars, 867 symbols, 761 priced Indian names)
- [x] Stage 1 — trend engine, theme indices, divergence
- [~] Stage 2 — fundamentals and quality gates
  - [x] XBRL ingest with true filing dates (through Dec-2024)
  - [x] Quality gates, tri-state, 21 tests
  - [ ] PDF extraction for 2025+ quarters — built, needs `OPENROUTER_API_KEY`
        and an accuracy run before anything it produces is trusted
  - [ ] Promoter shareholding ingest (pledge gate)
- [ ] Stage 3 — backtest and weight calibration
- [ ] Stage 4 — API + PWA
- [ ] Stage 5 — portfolio, staged accumulation, journal
- [x] Stage 6 — nightly automation (see above)

### Known limitations

- **Survivorship.** NSE publishes no historical index membership and no
  delisted archive, so dated membership is only accurate from the first run
  forward. A 2015–2026 backtest on today's constituents excludes every company
  that failed, which flatters returns. Stage 3 must correct for this or state
  it plainly in its output.
- **No structured fundamentals after Dec-2024.** Both exchanges moved to
  PDF-only filing under SEBI Integrated Filing. Until the extraction layer runs,
  scoring would be on 18-month-old financials.
- **Four gates cannot be evaluated yet** (cash conversion, receivable days,
  related party, contingent liabilities) because quarterly results carry no
  cash-flow or balance-sheet detail. They return UNKNOWN, which is not a pass —
  so most names correctly show as UNVETTED rather than CLEARED.
- **Divergence is unvalidated.** Whether a wide India-vs-global gap actually
  mean-reverts is a Stage 3 question. The UI states it as an observation.
