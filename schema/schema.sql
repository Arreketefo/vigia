-- Vigía schema (SQLite). Idempotent: safe to re-apply on an existing DB.
-- Portable to Postgres: TEXT timestamps -> timestamptz, REAL -> numeric.

-- Curated or discovered routes to watch. origin is usually 'ALC'.
CREATE TABLE IF NOT EXISTS routes (
    id            INTEGER PRIMARY KEY,
    origin        TEXT NOT NULL,              -- IATA, e.g. 'ALC'
    destination   TEXT NOT NULL,              -- IATA, e.g. 'BUD'
    enabled       INTEGER NOT NULL DEFAULT 1,
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (origin, destination)
);

-- Raw price observations captured from any FlightSource / HotelSource.
CREATE TABLE IF NOT EXISTS price_observations (
    id                INTEGER PRIMARY KEY,
    route_id          INTEGER NOT NULL REFERENCES routes(id),
    depart_date       TEXT NOT NULL,          -- YYYY-MM-DD
    return_date       TEXT,                   -- YYYY-MM-DD (NULL = one-way)
    nights            INTEGER,                -- derived stay length
    flight_price      REAL,                   -- per person, round trip, EUR
    hotel_price_night REAL,                   -- cheapest nightly, total room, EUR
    source            TEXT NOT NULL,          -- 'aviasales' | 'hotellook' | 'ryanair' | 'duffel'
    is_live           INTEGER NOT NULL DEFAULT 0, -- 1 if confirmed via PriceConfirmer
    deep_link         TEXT,
    captured_at       TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_obs_route_dates
    ON price_observations(route_id, depart_date, return_date);
CREATE INDEX IF NOT EXISTS idx_obs_captured ON price_observations(captured_at);

-- Rolling baseline per route + month bucket (recomputed by DealEngine).
CREATE TABLE IF NOT EXISTS baselines (
    route_id      INTEGER NOT NULL REFERENCES routes(id),
    month_bucket  TEXT NOT NULL,              -- 'YYYY-MM' of depart_date
    median_total  REAL NOT NULL,              -- robust center of total trip cost
    mad_total     REAL NOT NULL,              -- median absolute deviation
    sample_size   INTEGER NOT NULL,
    updated_at    TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (route_id, month_bucket)
);

-- Deals that fired (candidate or confirmed).
CREATE TABLE IF NOT EXISTS deals (
    id            INTEGER PRIMARY KEY,
    route_id      INTEGER NOT NULL REFERENCES routes(id),
    depart_date   TEXT NOT NULL,
    return_date   TEXT,
    total_price   REAL NOT NULL,              -- flight*pax + hotel_night*nights
    baseline      REAL,                       -- median_total at fire time
    drop_pct      REAL,                       -- relative drop vs baseline
    confirmed     INTEGER NOT NULL DEFAULT 0, -- 1 if live-confirmed (Layer 2)
    dedup_key     TEXT NOT NULL,              -- hash(origin,dest,dates,price_bucket)
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_deals_dedup ON deals(dedup_key);
-- should_alert() looks up the last alert by route + exact dates via a JOIN on
-- deals; without this index every deal evaluation scans the whole table.
CREATE INDEX IF NOT EXISTS idx_deals_route_dates
    ON deals(route_id, depart_date, return_date);

-- Alert dedup ledger (per channel).
CREATE TABLE IF NOT EXISTS alerts_sent (
    dedup_key     TEXT NOT NULL,
    channel       TEXT NOT NULL,             -- 'telegram' | 'email' | 'whatsapp'
    total_price   REAL NOT NULL,
    sent_at       TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (dedup_key, channel)
);

-- Last scan ATTEMPT per (route, month), fed to the round-robin scheduler.
-- Deliberately separate from price_observations: a pair that yields no data
-- must still advance in the queue, or empty pairs would starve the batch.
CREATE TABLE IF NOT EXISTS scan_state (
    route_id      INTEGER NOT NULL REFERENCES routes(id),
    month_bucket  TEXT NOT NULL,              -- 'YYYY-MM'
    scanned_at    TEXT NOT NULL,
    PRIMARY KEY (route_id, month_bucket)
);

-- Operational state (e.g. last_tick_at for the container healthcheck).
CREATE TABLE IF NOT EXISTS meta (
    key       TEXT PRIMARY KEY,
    value     TEXT NOT NULL
);

-- Hot config overrides set via the Telegram bot (radar-core botcontrol).
CREATE TABLE IF NOT EXISTS config_overrides (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
