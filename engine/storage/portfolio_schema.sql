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
