-- AI-Investo analytics schema (DuckDB)
--
-- POINT-IN-TIME CONTRACT
-- ----------------------
-- Every table carrying reported company data has a `filing_date`: the date the
-- figure first became public. Backtests MUST filter `filing_date <= as_of_date`.
-- Rows are append-only and never restated in place -- a company revising a prior
-- period inserts a NEW row with a later filing_date. `latest_fundamentals_asof()`
-- in db.py is the only sanctioned read path for historical scoring.
--
-- Retrofitting this later would invalidate every backtest, which is why it is
-- here in the first migration.

CREATE SEQUENCE IF NOT EXISTS seq_security_id START 1;

-- ---------------------------------------------------------------- securities
CREATE TABLE IF NOT EXISTS securities (
    security_id      BIGINT PRIMARY KEY DEFAULT nextval('seq_security_id'),
    ticker           VARCHAR NOT NULL,          -- provider symbol, e.g. WABAG.NS
    exchange_symbol  VARCHAR,                   -- native code, e.g. WABAG
    isin             VARCHAR,
    name             VARCHAR,
    exchange         VARCHAR NOT NULL,          -- NSE, BSE, NASDAQ, NYSE, TWSE...
    country          VARCHAR NOT NULL,
    currency         VARCHAR,
    sector           VARCHAR,
    industry         VARCHAR,
    market_cap       DOUBLE,                    -- in `currency`, refreshed nightly
    listing_date     DATE,
    delisted_date    DATE,                      -- NOT NULL => dead. Survivorship!
    is_active        BOOLEAN DEFAULT TRUE,
    source           VARCHAR,
    updated_at       TIMESTAMP DEFAULT current_timestamp,
    UNIQUE (ticker, exchange)
);

-- --------------------------------------------------------------------- prices
CREATE TABLE IF NOT EXISTS ohlcv (
    security_id  BIGINT NOT NULL,
    date         DATE   NOT NULL,
    open         DOUBLE,
    high         DOUBLE,
    low          DOUBLE,
    close        DOUBLE,
    adj_close    DOUBLE,
    volume       DOUBLE,
    source       VARCHAR,
    ingested_at  TIMESTAMP DEFAULT current_timestamp,
    PRIMARY KEY (security_id, date)
);

-- Macro / commodity / FX series kept separate: no security identity, no splits.
CREATE TABLE IF NOT EXISTS macro_series (
    series_id    VARCHAR NOT NULL,              -- DXY, BRENT, COPPER, USDINR, IN10Y
    date         DATE    NOT NULL,
    value        DOUBLE,
    source       VARCHAR,
    ingested_at  TIMESTAMP DEFAULT current_timestamp,
    PRIMARY KEY (series_id, date)
);

-- ------------------------------------------------------------ index membership
-- Dated membership is what makes survivorship-free backtests possible: we can
-- reconstruct who was in Nifty Smallcap 250 on any past date.
CREATE TABLE IF NOT EXISTS index_membership (
    index_name   VARCHAR NOT NULL,
    security_id  BIGINT  NOT NULL,
    from_date    DATE    NOT NULL,
    to_date      DATE,                          -- NULL => still a member
    source       VARCHAR,
    PRIMARY KEY (index_name, security_id, from_date)
);

-- ---------------------------------------------------------------- fundamentals
-- EAV rather than wide columns: Indian XBRL field coverage is wildly uneven
-- across companies and years, and a wide table would mean constant migrations.
CREATE TABLE IF NOT EXISTS fundamentals_pit (
    security_id  BIGINT  NOT NULL,
    period_end   DATE    NOT NULL,              -- fiscal period end
    period_type  VARCHAR NOT NULL,              -- Q | A | TTM
    filing_date  DATE    NOT NULL,              -- when it became public (PIT key)
    metric       VARCHAR NOT NULL,              -- revenue, ebitda, cfo, roce...
    value        DOUBLE,
    unit         VARCHAR,
    source       VARCHAR,
    ingested_at  TIMESTAMP DEFAULT current_timestamp,
    PRIMARY KEY (security_id, period_end, period_type, metric, filing_date)
);

-- FALSE means the row has no true filing date -- Yahoo reports the current
-- value of a figure and overwrites restatements in place. Such rows are usable
-- for screening today and must never reach a backtest, so `fundamentals_asof`
-- excludes them unless asked for explicitly.
ALTER TABLE fundamentals_pit ADD COLUMN IF NOT EXISTS is_pit BOOLEAN DEFAULT TRUE;

CREATE TABLE IF NOT EXISTS ownership_pit (
    security_id        BIGINT NOT NULL,
    quarter_end        DATE   NOT NULL,
    filing_date        DATE   NOT NULL,
    promoter_pct       DOUBLE,
    promoter_pledge_pct DOUBLE,
    fii_pct            DOUBLE,
    dii_pct            DOUBLE,
    public_pct         DOUBLE,
    source             VARCHAR,
    PRIMARY KEY (security_id, quarter_end, filing_date)
);

-- FALSE means `filing_date` was INFERRED from the statutory deadline rather than
-- reported by the exchange. Ownership carried no such marker for a long time, so
-- a row dated by the scraper's clock was indistinguishable from one dated by a
-- real broadcast and nothing downstream could tell the difference. Every row in
-- the table at the time this column was added shares ONE filing_date, equal to
-- the ingest date, which is what that failure looks like from the outside.
ALTER TABLE ownership_pit ADD COLUMN IF NOT EXISTS is_pit BOOLEAN DEFAULT TRUE;

-- The newest disclosure per company, for screens that mean "as things stand".
--
-- `ownership_pit` is append-only history, so joining it directly multiplies rows
-- by the number of quarters held. That was invisible while the table carried one
-- quarter per company and became a 3.7x row explosion the moment real history
-- was backfilled -- silently duplicating companies in the screen and in the
-- demo payload. Live screens join THIS; anything as-of must keep filtering
-- `filing_date <= as_of` itself, which this view deliberately does not do.
CREATE OR REPLACE VIEW ownership_latest AS
SELECT security_id, quarter_end, filing_date, promoter_pct, promoter_pledge_pct,
       fii_pct, dii_pct, public_pct, source, is_pit
  FROM (
    SELECT *, row_number() OVER (PARTITION BY security_id
                                 ORDER BY quarter_end DESC, filing_date DESC) AS rn
      FROM ownership_pit
  ) WHERE rn = 1;

-- Corporate red flags with a discovery date, so gates can fire point-in-time.
CREATE TABLE IF NOT EXISTS corporate_events (
    security_id  BIGINT  NOT NULL,
    event_date   DATE    NOT NULL,
    event_type   VARCHAR NOT NULL,              -- AUDITOR_RESIGN, ASM_GSM, PLEDGE...
    detail       VARCHAR,
    severity     VARCHAR,                        -- INFO | WARN | CRITICAL
    source       VARCHAR,
    PRIMARY KEY (security_id, event_date, event_type)
);

-- Official NSE index levels. A SNAPSHOT source: NSE blocks its historical
-- indices API, so this accumulates from the first run forward and cannot
-- backfill. Long-history benchmarks are reconstructed from constituent prices.
CREATE TABLE IF NOT EXISTS index_levels (
    index_name      VARCHAR NOT NULL,
    date            DATE    NOT NULL,
    last            DOUBLE,
    open            DOUBLE,
    high            DOUBLE,
    low             DOUBLE,
    previous_close  DOUBLE,
    pct_change      DOUBLE,
    source          VARCHAR,
    PRIMARY KEY (index_name, date)
);

-- Daily FII/DII cash-market flows (Rs crore) -- regime context for the Indian leg.
CREATE TABLE IF NOT EXISTS flows (
    date        DATE    NOT NULL,
    category    VARCHAR NOT NULL,
    buy_value   DOUBLE,
    sell_value  DOUBLE,
    net_value   DOUBLE,
    source      VARCHAR,
    PRIMARY KEY (date, category)
);

-- BSE identity, added after the fact so existing databases migrate in place.
ALTER TABLE securities ADD COLUMN IF NOT EXISTS bse_scripcode VARCHAR;

-- When a company's results for a period actually became public. This is the
-- source of `fundamentals_pit.filing_date` for anything extracted from PDFs,
-- where the document itself carries no dissemination timestamp.
CREATE TABLE IF NOT EXISTS filing_events (
    security_id  BIGINT  NOT NULL,
    period_end   DATE    NOT NULL,
    filing_date  DATE    NOT NULL,
    filing_ts    TIMESTAMP,
    event_type   VARCHAR,            -- RESULT
    subject      VARCHAR,
    source       VARCHAR,
    PRIMARY KEY (security_id, period_end, filing_date)
);

-- Order wins and disclosed backlog, parsed from announcement text.
-- ORDER_BOOK is a stock (outstanding backlog), ORDER_WIN is a flow (one
-- contract). They are never summed: execution burns the book down, so adding
-- wins to it would double-count work already counted.
CREATE TABLE IF NOT EXISTS order_events (
    security_id  BIGINT  NOT NULL,
    event_date   DATE    NOT NULL,
    kind         VARCHAR NOT NULL,        -- ORDER_BOOK | ORDER_WIN
    value_cr     DOUBLE,                  -- NULL where the text carried no figure
    headline     VARCHAR,
    pdf_url      VARCHAR,
    source       VARCHAR,
    ingested_at  TIMESTAMP DEFAULT current_timestamp,
    PRIMARY KEY (security_id, event_date, kind, headline)
);

-- ---------------------------------------------------------------- theme graph
CREATE TABLE IF NOT EXISTS themes (
    theme_id     VARCHAR PRIMARY KEY,
    name         VARCHAR NOT NULL,
    tier         INTEGER,                        -- 1 = full weight, 3 = watchlist
    status       VARCHAR,                        -- ACTIVE | WATCHLIST
    description  VARCHAR
);

CREATE TABLE IF NOT EXISTS theme_nodes (
    theme_id     VARCHAR NOT NULL,
    node_id      VARCHAR NOT NULL,
    node_name    VARCHAR,
    leg          VARCHAR NOT NULL,               -- GLOBAL | INDIA
    description  VARCHAR,
    PRIMARY KEY (theme_id, node_id)
);

CREATE TABLE IF NOT EXISTS theme_members (
    theme_id     VARCHAR NOT NULL,
    node_id      VARCHAR NOT NULL,
    security_id  BIGINT  NOT NULL,
    weight       DOUBLE DEFAULT 1.0,
    rationale    VARCHAR,
    added_date   DATE,
    removed_date DATE,
    PRIMARY KEY (theme_id, node_id, security_id)
);

-- ------------------------------------------------------------- computed layer
CREATE TABLE IF NOT EXISTS theme_index (
    theme_id           VARCHAR NOT NULL,
    leg                VARCHAR NOT NULL,         -- GLOBAL | INDIA
    date               DATE    NOT NULL,
    level              DOUBLE,                   -- rebased to 100 at inception
    constituent_count  INTEGER,
    PRIMARY KEY (theme_id, leg, date)
);

CREATE TABLE IF NOT EXISTS propagation (
    theme_id      VARCHAR NOT NULL,
    as_of_date    DATE    NOT NULL,
    lag_weeks     INTEGER,                       -- +ve => global leads India
    correlation   DOUBLE,
    stability     DOUBLE,                        -- consistency of lag over time
    window_state  VARCHAR,                       -- OPEN | CLOSING | CLOSED | UNCLEAR
    detail        VARCHAR,
    PRIMARY KEY (theme_id, as_of_date)
);

CREATE TABLE IF NOT EXISTS trend_signals (
    entity_type       VARCHAR NOT NULL,          -- SECURITY | THEME | INDEX
    entity_id         VARCHAR NOT NULL,
    as_of_date        DATE    NOT NULL,
    timeframe         VARCHAR NOT NULL,          -- D | W | M
    metrics           JSON,
    score             DOUBLE,
    PRIMARY KEY (entity_type, entity_id, as_of_date, timeframe)
);

CREATE TABLE IF NOT EXISTS trend_confluence (
    entity_type  VARCHAR NOT NULL,
    entity_id    VARCHAR NOT NULL,
    as_of_date   DATE    NOT NULL,
    score        DOUBLE,                          -- 0..100
    stage        VARCHAR,                         -- BASING|EMERGING|ACCELERATING|CROWDED|FADING
    PRIMARY KEY (entity_type, entity_id, as_of_date)
);

-- ------------------------------------------------------------------- scoring
-- `status` is the authoritative field, not `passed`. A boolean cannot express
-- the distinction the gates exist to make: UNKNOWN (could not be evaluated) is
-- not FAIL and is not PASS. Storing only the boolean collapsed every
-- unevaluated gate into a rejection.
CREATE TABLE IF NOT EXISTS gate_results (
    security_id     BIGINT  NOT NULL,
    as_of_date      DATE    NOT NULL,
    gate_name       VARCHAR NOT NULL,
    status          VARCHAR,                      -- PASS | FAIL | UNKNOWN
    passed          BOOLEAN,                      -- convenience: status = 'PASS'
    observed_value  DOUBLE,
    threshold       DOUBLE,
    detail          VARCHAR,                      -- shown verbatim in the app
    PRIMARY KEY (security_id, as_of_date, gate_name)
);
ALTER TABLE gate_results ADD COLUMN IF NOT EXISTS status VARCHAR;

CREATE TABLE IF NOT EXISTS scores (
    security_id   BIGINT NOT NULL,
    as_of_date    DATE   NOT NULL,
    theme_id      VARCHAR,                        -- primary theme attribution
    t_score       DOUBLE,                         -- Theme tailwind
    g_score       DOUBLE,                         -- Growth inflection
    q_score       DOUBLE,                         -- Business quality
    d_score       DOUBLE,                         -- Discovery / under-ownership
    v_score       DOUBLE,                         -- Valuation sanity
    m_score       DOUBLE,                         -- Price trend confluence
    gem_score     DOUBLE,                         -- weighted composite 0..100
    rank_overall  INTEGER,
    gates_passed  BOOLEAN,
    gates_failed  VARCHAR,                        -- comma-joined gate names
    explain       JSON,                           -- per-pillar inputs for the UI
    PRIMARY KEY (security_id, as_of_date)
);

-- What share of the composite rests on evidence rather than the neutral 50.
-- A gem_score of 62 on 30% coverage and one on 95% coverage are not the same
-- claim, and before this column existed there was no way to tell them apart.
ALTER TABLE scores ADD COLUMN IF NOT EXISTS coverage DOUBLE;

-- Cursors for work that spans many nights. The filings backfill walks BSE in
-- small windows because the exchange throttles sustained pagination, so its
-- progress has to survive across runs.
CREATE TABLE IF NOT EXISTS job_state (
    job          VARCHAR PRIMARY KEY,
    cursor_date  DATE,
    detail       VARCHAR,
    updated_at   TIMESTAMP DEFAULT current_timestamp
);

-- Every PDF extraction attempt, whether or not it was kept. Quarantined rows
-- stay here rather than in fundamentals_pit, so a suspect figure is reviewable
-- without ever reaching the scoring engine.
CREATE TABLE IF NOT EXISTS pdf_extractions (
    security_id  BIGINT  NOT NULL,
    period_end   DATE    NOT NULL,
    model        VARCHAR NOT NULL,
    status       VARCHAR,          -- CLEAN | QUARANTINED | FAILED
    problems     VARCHAR,          -- which guard objected, verbatim
    filing_date  DATE,
    pdf_url      VARCHAR,
    cost_usd     DOUBLE,
    payload      JSON,             -- the full extraction, for later review
    extracted_at TIMESTAMP DEFAULT current_timestamp,
    PRIMARY KEY (security_id, period_end, model)
);

-- ------------------------------------------------------------ run bookkeeping
CREATE TABLE IF NOT EXISTS ingest_log (
    run_id       VARCHAR NOT NULL,
    stage        VARCHAR NOT NULL,
    entity       VARCHAR,
    status       VARCHAR,                         -- OK | EMPTY | ERROR | SKIPPED
    rows         BIGINT,
    detail       VARCHAR,
    started_at   TIMESTAMP,
    finished_at  TIMESTAMP
);

-- Did the night actually run, and did anything fail?
--
-- The nightly report went to reports/nightly.log and nowhere else, so nothing
-- could display it and nobody read it. Two stages then failed on EVERY run for
-- an unknown period -- the Today payload and thesis health, both on the same
-- DuckDB connection rule -- and it surfaced only when the job was run by hand
-- and the traceback read. A pipeline that can fail invisibly is one you have to
-- remember to audit, which is the same as one you do not trust.
--
-- Written per stage as it completes, so a run that dies halfway still leaves a
-- record of how far it got.
CREATE TABLE IF NOT EXISTS pipeline_runs (
    run_id      VARCHAR   NOT NULL,
    started_at  TIMESTAMP NOT NULL,
    stage       VARCHAR   NOT NULL,
    status      VARCHAR   NOT NULL,        -- OK | FAILED | SKIPPED
    seconds     DOUBLE,
    detail      VARCHAR,
    recorded_at TIMESTAMP DEFAULT current_timestamp,
    PRIMARY KEY (run_id, stage)
);

CREATE INDEX IF NOT EXISTS idx_pipeline_started  ON pipeline_runs (started_at);
CREATE INDEX IF NOT EXISTS idx_ohlcv_date        ON ohlcv (date);
CREATE INDEX IF NOT EXISTS idx_fund_sec_filing   ON fundamentals_pit (security_id, filing_date);
CREATE INDEX IF NOT EXISTS idx_scores_date       ON scores (as_of_date);
CREATE INDEX IF NOT EXISTS idx_ingest_run        ON ingest_log (run_id);
