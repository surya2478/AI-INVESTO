# AI-Investo — Product Specification & Build Plan

> **Status:** For your review. Nothing built yet. `C:\AI-Investo` is empty.
> **Not investment advice.** This tool computes signals from public data. Every buy/sell decision is yours. I am not a licensed advisor and this spec contains no recommendation to buy any security. Company names appear only as examples of how the theme graph maps a value chain.

---

## 1. Context — what we're building and why

You want to find early-stage multibaggers in India, in deep-tech and problem-solving sectors, using **global trends as the leading indicator** and Indian listed companies as the vehicle. You want to accumulate them in stages over years, and track the whole thing from your phone.

No existing product does this. They each do one slice:

| Platform | Cost/yr | What it's great at | Why it fails *your* thesis |
|---|---|---|---|
| **Screener.in** | ₹4,999 | Deepest 10-yr Indian financials, custom query language | India-only, zero global context, backward-looking, no theme model, no trend engine |
| **Trendlyne** | ₹5,900 | DVM scores, alerts, huge screener | Generic scores tuned for broad market; momentum-biased; no global→India linkage; no early-stage bias |
| **Tickertape** | ₹2,399 | Cleanest UX, good for beginners | Shallow analytics, no custom quant, largecap-tilted |
| **Tijori Finance** | ₹3,500 | Segment-level & alt data (capacity, order books) — closest to your need | No scoring engine, no ranking, no global trend layer, no portfolio logic |
| **MarketsMojo** | ~₹5,000 | Score-based verdicts | Black box — you cannot see or tune the formula, and it's never backtested for you |
| **Koyfin / TIKR / Fiscal.ai** | $300–600 | Global breadth, 90+ countries | Weak/stale Indian smallcap & microcap coverage — exactly where your gems live |
| **Simply Wall St** | ~$120 | Beautiful DCF visualisations | DCF is meaningless for pre-inflection companies; thin on illiquid India |
| **Seeking Alpha Quant** | ~$240 | Genuinely good factor scoring | US-only |
| **smallcase** | ~₹2,000 | Thematic baskets, one-click invest | Pre-built, largecap-heavy, someone else's thesis, no discovery |
| **StockEdge** | ~₹3,000 | Technical scans, delivery data | Trading tool, not a compounding tool |

**The gap:** nobody connects a *global* theme to its *Indian* derivative and tells you when the India leg hasn't moved yet. That lag is your entire edge, and it is measurable.

You currently could pay ~₹8,500–15,000/yr and still not get it. This is built for **₹0/month recurring**.

### The three things that make this beat paid platforms

1. **Theme Propagation Engine** — measures the historical lead-lag between a global theme basket and its Indian value-chain basket, then flags *"global leg fired 7 weeks ago, India leg still at base."* That's the early entry window. No product does this.
2. **A score you can see, tune, and — critically — backtest.** Every paid platform ships a black-box score with no published edge. Ours reports its own decile performance since 2015 before you trust it.
3. **Filings & concall NLP.** Annual reports and earnings-call transcripts parsed for order book, capacity expansion, capex timelines and guidance — then scored on *whether management actually delivered what they promised last time*. This is Tijori's moat, automated, plus something Tijori doesn't do.

---

## 2. Assumptions I've locked (you skipped these — override any of them)

| Decision | Chosen | Why |
|---|---|---|
| **Data** | Free sources now, behind a `MarketDataProvider` interface | yfinance + NSE/BSE XBRL filings + AMFI + FRED covers a multi-year thesis fully. A paid key (EODHD/FMP) drops in later with no rewrite. |
| **App** | Installable PWA now → Capacitor native shell later | Add-to-Home-Screen on your iPhone/Android feels native, ships in days, costs nothing, avoids the ₹8,000/yr Apple fee until the engine is proven. Same codebase wraps into store builds later. |
| **Hosting** | Docker on this PC first, identical compose file lifts to cloud | Free. EOD data means a missed night is harmless — the pipeline backfills on next run. Move to Fly.io/Oracle free tier (~₹0–800/mo) when you want alerts to fire reliably at 2 AM. |

---

## 3. Theme universe

### Your five, mapped to where the money actually is in India

The critical insight: for most global themes, **the Indian listed play is not the obvious one.**

| Theme | Global leading indicator | Indian value-chain nodes the engine tracks |
|---|---|---|
| **Solar** | Polysilicon & module prices, global installs, US/EU IPP capex | Module & cell mfg (margin-squeezed by China), **inverters, cables & conductors, mounting structures, EPC, IPPs/yieldcos, grid interconnect** — the ancillaries usually beat the panel makers |
| **AI** | Hyperscaler capex guidance, NVDA/TSMC/AVGO/Vertiv/Eaton, HBM & foundry utilisation | India has almost no AI chip play. Real exposure = **power T&D, transformers, HVDC, switchgear, data-centre cooling/HVAC, diesel gensets, fibre & cable, DC real estate, IT services AI revenue mix, ESDM/OSAT** |
| **Quantum** | IONQ/RGTI/QBTS, IBM/Google roadmaps, national programmes, patent flow | No listed pure-play in India. Exposure via **cryogenics, photonics & optics, specialty materials, precision instrumentation, defence R&D, IT services quantum practices**, plus National Quantum Mission award flow. **Watchlist tier — long-dated, low weight.** |
| **Water treatment** | Global water utility indices, membrane makers, desal capex | Genuinely rich in India: **water EPC, membranes & media, ZLD/effluent treatment, pumps & valves, pipes, smart metering, sludge & waste-to-value.** Driven by AMRUT 2.0, Namami Gange, ZLD mandates — policy-visible order books |
| **Innovative health** | Global CDMO cycle, GLP-1 volumes, medtech, diagnostics | **CDMO/CRO, API for GLP-1 & peptides, diagnostics chains, medtech & devices, hospital chains, genomics, biosimilars, health-tech** |

### Additional themes matching your criteria (problems being solved over 5–10 years)

**Tier 1 — highest conviction, direct adjacencies to what you already hold a view on:**

1. **Grid & Power Transmission Infrastructure** — the literal physical bottleneck for *both* AI and solar. Transformers, HVDC, conductors, switchgear, T&D EPC, substation automation. If you believe in AI *and* solar, you already believe in this, and India's grid capex cycle is a decade long.
2. **Energy Storage / BESS + battery materials** — solar without storage is half a thesis. India's BESS tender pipeline is inflecting hard, and cell chemistry, BMS, containerised systems and recycling are all separate nodes.
3. **Semiconductors & ESDM/OSAT** — India Semiconductor Mission + PLI. The physical substrate under AI. Assembly/test/packaging is where India realistically wins first.
4. **Data-centre ecosystem** — cooling, UPS/backup, fibre, land and DC REITs. The most direct, most under-priced Indian AI derivative.

**Tier 2 — strong, longer-dated:**

5. **Defence & space tech** — indigenisation mandate, private launch, 10-year order-book visibility. Best revenue predictability of any theme here.
6. **Specialty chemicals & advanced materials** — the enabling layer beneath solar, batteries, semis *and* water membranes. Cross-theme compounding exposure.
7. **Recycling & circular economy** — e-waste, battery recycling, EPR mandates creating a regulated demand floor.
8. **Precision medicine & genomics** — the sharp end of your health interest.

**Tier 3 — watchlist only, high failure rate:**

9. **Green hydrogen & electrolysers** — economics still unproven; track, don't size.
10. **Nuclear / SMR** — India opening nuclear to private capital; the AI-power endgame. Very few listed vehicles today.

**My recommendation:** run Tier 1 at full weight from day one. Grid/T&D in particular is the highest-conviction idea in this document — it's the shared bottleneck of two of your five themes, and it's an infrastructure buildout, not a technology bet, so it fails less often.

---

## 4. The analytics engine

### 4a. Multi-timeframe trend layer (your daily / weekly / monthly ask)

**Markets tracked:** US (S&P 500, Nasdaq), India (Nifty 500 + Smallcap 250 + Microcap 250), China/HK, Japan, Taiwan, Korea, Germany/Europe. Plus the macro plumbing that actually drives theme rotation: DXY, US 10Y, Brent, copper, silver, lithium, polysilicon, uranium, India 10Y, USDINR, FII/DII flows.

For every theme, build two rebased equal-weight indices: a **Global Theme Index** and an **India Theme Index**.

| Timeframe | Signals computed | Question it answers |
|---|---|---|
| **Daily** | 20DMA slope, RSI(14), ADX, volume thrust, distance from 52w high | Is it moving *now*? |
| **Weekly** | 13/26-week EMA state, relative strength vs local index and vs world | Is the trend real or noise? |
| **Monthly** | 12-month momentum, distance from 36-month base, drawdown recovery | Is this a genuine multi-year regime? |

**Trend Confluence Score (0–100)** combines the three, and classifies each theme into a stage:

- **BASING** — monthly flat/bottoming, no weekly confirmation → too early, watch
- **EMERGING** — monthly turning up off a base + weekly confirming + daily leading → **this is your entry window**
- **ACCELERATING** — all three aligned up, RS strong → add on strength
- **CROWDED** — extended vs 36m base, valuation percentile top decile → hold, stop adding
- **FADING** — weekly breaking, monthly rolling → thesis review trigger

### 4b. Theme Propagation Engine — the differentiator

For each theme, compute rolling cross-correlation between the Global Theme Index and the India Theme Index across lags of 0–26 weeks. Store the **dominant lag** and its stability.

Output, in plain language, on the Themes screen:

> **AI / Data-centre power:** global leg has led India by ~8 weeks historically (corr 0.71, stable). Global index broke out 6 weeks ago. India basket still 14% below its 36-month resistance. **Propagation window: OPEN.**

That single line is the product. It tells you *what* to look at and, more importantly, *when it's still early*.

Also computed: **policy catalysts** (PLI/tender/budget announcements) as discrete events layered onto the India leg, since Indian themes often front-run global ones when policy-driven — the engine detects both directions of lead-lag, not just global→India.

### 4c. The G.E.M. Score (Growth · Early · Momentum) — 0 to 100

Six pillars, each 0–100, weighted. **You can see and tune every weight.**

| Pillar | Weight | What it measures |
|---|---|---|
| **T — Theme Tailwind** | 20% | Trend confluence of the parent theme + propagation-window state + policy catalyst density |
| **G — Growth Inflection** | 25% | Sales-growth *acceleration* (2y CAGR > 4y CAGR), operating leverage (PAT growth > EBITDA > GP > sales), margin expansion trend, capex/gross-block ramp, order-book-to-sales |
| **Q — Business Quality** | 20% | ΔRoCE trend (improving matters more than high), CFO/EBITDA conversion, debt/equity trend, interest coverage, promoter holding trend, working-capital discipline |
| **D — Discovery / Under-ownership** | 15% | Market cap band, institutional holding % (low = undiscovered, *rising* = accumulation), analyst coverage count, liquidity |
| **V — Valuation Sanity** | 10% | PEG, EV/EBITDA vs own 5-yr band and vs sub-industry percentile — a sanity brake, not a value screen |
| **M — Price Trend Confluence** | 10% | The stock's own daily/weekly/monthly alignment + RS vs its theme basket |

**Why these weights:** Growth inflection is the single strongest predictor of a multibagger, so it leads. Discovery is weighted meaningfully because a great company already at ₹1L cr cannot 10x. Valuation is deliberately light — insisting on cheapness is how people miss every genuine compounder — but non-zero so you don't buy at the top of a mania.

### 4d. Quality Gates — the graveyard filter (hard reject, any score)

Most retail multibagger hunting dies here, not at stock selection. Any of these flips a name to **REJECTED** with the reason shown:

- Promoter pledge > 20%, or rising pledge in any quarter
- Cumulative CFO negative over 3 years while PAT positive *(the single best accounting-fraud filter)*
- Auditor resignation, qualified opinion, or CFO/auditor churn
- Receivable days > 1.5× 3-year average
- Related-party transactions > 10% of revenue
- Share count up > 25% in 2 years without matching asset growth (serial dilution)
- Contingent liabilities > 50% of net worth
- NSE/BSE surveillance (ASM/GSM) listing
- Median daily traded value below your configurable floor
- Repeated promoter selling

Gates are **explainable** — the app shows exactly which gate fired and the underlying number, so you can override with a documented reason if you disagree.

### 4e. Filings & concall NLP layer

Nightly: pull BSE/NSE announcements, quarterly XBRL results, annual report PDFs and concall transcripts. Extract structured fields via the Claude API:

- Order book value and execution timeline
- Capacity, utilisation, and announced expansion with dates
- Management guidance (revenue, margin, capex)
- **Guidance-delivery score** — did they hit what they promised 4 quarters ago? Tracked as a running record per management team.
- Tone/emphasis shift vs the prior quarter's call

The guidance-delivery score is the closest thing to a management-integrity metric you can compute from public data, and no platform on the comparison table publishes it.

### 4f. Backtest harness — this is non-negotiable

A score you haven't validated is astrology. Before you trust G.E.M.:

- Rebuild the score monthly from 2015 using **point-in-time data only** (fundamentals stamped with actual filing date, never restated — this is where most retail backtests silently cheat)
- Form decile portfolios, measure forward 1/3/5-year returns, hit-rate of >3x outcomes, max drawdown, and survivorship-bias-corrected results (delisted names included)
- Walk-forward validation: tune weights on 2015–2020, verify on 2021–2026
- **Report the honest number.** If the top decile doesn't beat Nifty Smallcap 250 meaningfully, we change the model rather than ship a pretty dashboard.

---

## 5. Portfolio construction — staged accumulation

You said "continue to invest to build" — so the tool is built around accumulation, not one-shot buying.

**Conviction tiers:** Core (target 3–5%), Satellite (1.5–2.5%), Watchlist (0%, tracked only).

**Staged entry ladder** — no single-shot entries:
- Tranche 1 (40%) on signal — score crosses threshold with propagation window open
- Tranche 2 (30%) on *thesis confirmation* — the next quarterly result validates the growth inflection
- Tranche 3 (30%) on breakout confirmation, or on a drawdown add if thesis intact

**Monthly deployment planner:** you enter your monthly investable amount; it allocates across the ladder by score rank and tier, respecting a liquidity cap per name.

**Thesis-health monitor (Green/Amber/Red)** — this is what turns a screener into a portfolio tool. Exits are triggered by *thesis break*, not price:
- RoCE declining 2 consecutive quarters → Amber
- Order book shrinking, or guidance missed → Amber
- Any quality gate newly firing → Red
- Theme stage flips to FADING → Amber
- CFO turning negative → Red

**Portfolio X-ray:** theme concentration, market-cap ladder, correlation clustering (are your "10 ideas" really 3 bets?), and overlap with any mutual funds you already hold.

**Investment journal:** you record *why* you bought, in your own words. Each quarter the tool re-tests those specific claims against new data and tells you which parts of your original thesis have broken. Nothing on the comparison table does this, and it's the highest-value habit in long-horizon investing.

---

## 6. The app (PWA — iPhone + Android)

| Screen | Contents |
|---|---|
| **Today** | Global + India market pulse, theme heatmap across D/W/M, top 5 actions, alerts |
| **Themes** | Per-theme card: global vs India index chart, propagation-lag gauge, stage badge, catalysts, constituents |
| **Gems** | Ranked G.E.M. list, filterable by theme/market cap/stage. Radar chart of the six pillars per name |
| **Company** | Thesis card, inflection charts, ownership trend, valuation band, gates passed/failed, concall extracts, "why it scored this" breakdown |
| **Portfolio** | Holdings, staged-entry tracker with next-tranche prompt, thesis health, X-ray |
| **Journal** | Your thesis notes + automated quarterly re-test results |
| **Settings** | Score weights, gate thresholds, universe, deployment budget, alert rules |

Dark-first, thumb-reachable, offline-capable, installable to home screen. Push alerts via Web Push, with a Telegram bot as the reliable iOS fallback.

---

## 7. Technical architecture

```
C:\AI-Investo\
├── engine/                    # Python 3.12
│   ├── providers/             # MarketDataProvider interface
│   │   ├── yfinance_provider.py    # global + India OHLCV
│   │   ├── nse_provider.py         # indices, corp actions, FII/DII, ASM/GSM
│   │   ├── bse_xbrl_provider.py    # authoritative quarterly financials
│   │   ├── macro_provider.py       # FRED, commodities, RBI
│   │   └── llm_extractor.py        # Claude API: filings & concall parsing
│   ├── universe/              # index constituents, theme graph, value-chain map
│   ├── features/              # trend indicators, fundamental features, ownership
│   ├── scoring/               # gates.py, gem_score.py, propagation.py
│   ├── backtest/              # point-in-time engine, walk-forward validation
│   ├── portfolio/             # sizing, ladder, thesis health
│   └── pipeline.py            # typer CLI: ingest → features → score → publish
├── api/                       # FastAPI — serves scores, themes, portfolio
├── app/                       # React + Vite + TS + Tailwind + shadcn, PWA
├── data/                      # DuckDB (analytics) + Parquet (raw) + SQLite (state)
└── docker-compose.yml         # runs local; same file deploys to cloud
```

**Choices worth noting:** DuckDB over Postgres — columnar, embedded, zero-admin, and enormously faster for the time-series scans this engine does constantly. Provider interface from day one so a paid data key is a config change, never a rewrite. Point-in-time storage discipline baked into the schema rather than bolted on, because retrofitting it later invalidates every backtest.

---

## 8. Build stages

Each stage produces something you can actually look at. Nothing is a black box until the end.

| Stage | Delivers | You can review |
|---|---|---|
| **0. Foundation** | Repo scaffold, DuckDB schema, providers, universe build (~1,200 Indian + ~400 global names), nightly ingest | Data actually landing, coverage report |
| **1. Trend engine** | D/W/M signals, theme indices, **propagation lag**, stage classification | First real output — a theme heatmap with lag readings |
| **2. Fundamentals & scoring** | XBRL ingest, feature store, quality gates, G.E.M. score | A ranked gem list you can sanity-check against names you know |
| **3. Backtest** | Point-in-time harness, decile performance, walk-forward, **weight calibration** | The honest edge number. Weights get tuned here, on evidence |
| **4. API + app core** | FastAPI, PWA shell, Today / Themes / Gems / Company screens | Installed on your phone, working |
| **5. Portfolio & journal** | Staged ladder, deployment planner, thesis health, X-ray, journal | End-to-end usable |
| **6. Alerts & automation** | Web Push + Telegram, scheduler, Docker | Runs itself |
| **7. Optional** | Capacitor native shells, cloud deploy, paid data provider | Store builds if you want them |

Stages 0–3 are where the intellectual value is — I'd suggest reviewing the backtest results at Stage 3 before we invest effort in the app polish, because the backtest may tell us to change the model.

## 9. Verification

- **Data integrity:** coverage report per stage — % of universe with complete 5-yr financials, stale-data detection, cross-check a sample of names against Screener.in values
- **Scoring:** unit tests on gate logic with hand-built fixtures; manual review of the top 20 and bottom 20 ranked names for face validity
- **Backtest honesty:** explicit lookahead test — corrupt the point-in-time layer deliberately and confirm returns jump (proving the guard is real); delisted names included for survivorship
- **App:** run on your actual phone over Tailscale/LAN, install to home screen, verify offline mode and push delivery
- **End-to-end:** `docker compose up`, one CLI command runs the full nightly pipeline, phone reflects new scores next morning

---

## 10. Honest limitations

- **Free Indian smallcap fundamentals are patchy.** NSE/BSE XBRL filings are the authoritative fix and we use them, but expect a real coverage-cleanup effort in Stage 0. This is the least glamorous and most important part of the build.
- **Quantum has no Indian pure-play.** It stays watchlist-tier with low weight; pretending otherwise would be dishonest.
- **Propagation lag is a statistical relationship, not a law.** It breaks in regime shifts. The engine reports correlation stability alongside the lag so you can see when to distrust it.
- **Microcaps are illiquid.** Position sizing is liquidity-capped, and the backtest must model realistic impact costs or it will lie to you.
- **The score ranks; it does not decide.** Final judgment stays with you — that's both the design intent and the correct legal posture for a personal research tool.
