-- Portfolio state: YOUR data, not derived data.
--
-- Deliberately a SEPARATE database file from the analytics store. Everything in
-- investo.duckdb can be rebuilt from providers in an afternoon; nothing in here
-- can be recovered at all. Keeping them together means one `rm` or one schema
-- migration gone wrong destroys the record of what you bought and why.
--
-- Attached as `folio` when needed, so analytics can be dropped and rebuilt
-- without touching this file.

CREATE SEQUENCE IF NOT EXISTS seq_position_id START 1;
CREATE SEQUENCE IF NOT EXISTS seq_tranche_id START 1;
CREATE SEQUENCE IF NOT EXISTS seq_entry_id START 1;

-- A position is an intention, opened before any money moves. Watchlist entries
-- carry a thesis and are tracked exactly like held positions, because the point
-- is to find out whether your reasoning was right, not only whether the trade was.
CREATE TABLE IF NOT EXISTS positions (
    position_id       BIGINT PRIMARY KEY DEFAULT nextval('seq_position_id'),
    ticker            VARCHAR NOT NULL,
    tier              VARCHAR NOT NULL,   -- CORE | SATELLITE | WATCHLIST
    target_weight_pct DOUBLE,             -- of the whole portfolio
    thesis            VARCHAR NOT NULL,   -- why, in your words
    theme             VARCHAR,
    opened_on         DATE NOT NULL,
    closed_on         DATE,
    close_reason      VARCHAR,
    status            VARCHAR DEFAULT 'OPEN',  -- OPEN | CLOSED
    UNIQUE (ticker, opened_on)
);

-- The staged ladder. Rows exist as PLANNED before they are executed, so the app
-- can show what is due rather than only what happened.
CREATE TABLE IF NOT EXISTS tranches (
    tranche_id   BIGINT PRIMARY KEY DEFAULT nextval('seq_tranche_id'),
    position_id  BIGINT NOT NULL,
    stage        INTEGER NOT NULL,        -- 1, 2, 3
    planned_pct  DOUBLE NOT NULL,         -- share of the position's target
    trigger      VARCHAR,                 -- what should happen before buying
    status       VARCHAR DEFAULT 'PLANNED',  -- PLANNED | EXECUTED | SKIPPED
    executed_on  DATE,
    shares       DOUBLE,
    price        DOUBLE,
    amount       DOUBLE,
    note         VARCHAR,
    UNIQUE (position_id, stage)
);

-- Why you bought, and what you expected. `claims` holds the checkable parts so
-- a later review can test them rather than re-reading prose.
CREATE TABLE IF NOT EXISTS journal (
    entry_id     BIGINT PRIMARY KEY DEFAULT nextval('seq_entry_id'),
    position_id  BIGINT,
    ticker       VARCHAR,
    entry_date   DATE NOT NULL,
    kind         VARCHAR NOT NULL,        -- THESIS | REVIEW | EXIT | NOTE
    body         VARCHAR NOT NULL,
    claims       JSON,                    -- e.g. {"revenue_growth_min": 20}
    created_at   TIMESTAMP DEFAULT current_timestamp
);

-- Computed each run, kept as history so a slide from GREEN to RED is visible
-- as a trend rather than only as today's state.
CREATE TABLE IF NOT EXISTS thesis_health (
    position_id  BIGINT NOT NULL,
    as_of_date   DATE NOT NULL,
    health       VARCHAR,                 -- GREEN | AMBER | RED
    reasons      VARCHAR,
    gem_band     VARCHAR,
    verdict      VARCHAR,
    PRIMARY KEY (position_id, as_of_date)
);

CREATE TABLE IF NOT EXISTS folio_settings (
    key    VARCHAR PRIMARY KEY,
    value  VARCHAR
);

-- WHAT WOULD MAKE THIS WRONG.
--
-- A thesis written as prose cannot be checked, so nothing ever checked one.
-- `review_thesis` tested gates and the score band -- generic quality signals,
-- none of them the reason anybody actually bought. WABAG's thesis rests on a
-- "policy-visible order book"; the engine has tracked order books all along and
-- the two were never introduced. A reason can evaporate in silence.
--
-- A claim is one falsifiable assertion: a metric, a direction and a threshold.
-- Recorded once, checked every night, and kept when retired rather than deleted,
-- because a claim you stopped believing is part of the record of your thinking.
CREATE SEQUENCE IF NOT EXISTS seq_claim_id START 1;

CREATE TABLE IF NOT EXISTS thesis_claims (
    claim_id     BIGINT PRIMARY KEY DEFAULT nextval('seq_claim_id'),
    position_id  BIGINT NOT NULL,
    metric       VARCHAR NOT NULL,        -- see claims.MEASURES
    comparator   VARCHAR NOT NULL,        -- '>=' | '<='
    threshold    DOUBLE  NOT NULL,
    note         VARCHAR,                 -- why this number, in your words
    created_on   DATE NOT NULL,
    retired_on   DATE,                    -- NULL => still part of the thesis
    UNIQUE (position_id, metric, created_on)
);

-- History, so "when did this stop being true" is answerable rather than only
-- "is it true now".
CREATE TABLE IF NOT EXISTS thesis_claim_checks (
    claim_id     BIGINT NOT NULL,
    position_id  BIGINT NOT NULL,
    as_of_date   DATE NOT NULL,
    status       VARCHAR NOT NULL,        -- HOLDS | BROKEN | UNCHECKABLE
    observed     DOUBLE,
    detail       VARCHAR,
    PRIMARY KEY (claim_id, as_of_date)
);
