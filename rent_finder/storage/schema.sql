-- schema.sql
-- Applied by database.init_db() at startup using CREATE TABLE IF NOT EXISTS.
-- Safe to run on every startup (idempotent).
--
-- Connection-time PRAGMAs set in database.py (not here):
--   PRAGMA journal_mode = WAL;
--   PRAGMA foreign_keys = ON;
--   PRAGMA busy_timeout = 5000;
--   PRAGMA synchronous = NORMAL;

-- ============================================================
-- TABLE: listings
-- Canonical record for every listing ever seen in any CSV.
-- One row per listing_id; status tracks the pipeline state machine.
-- ============================================================
CREATE TABLE IF NOT EXISTS listings (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    listing_id              TEXT    NOT NULL UNIQUE,
        -- Numeric ID from Facebook URL e.g. "123456789012345"; dedup key
    url                     TEXT    NOT NULL,
    title                   TEXT    NOT NULL,
    price_raw               TEXT,
        -- Original string e.g. "$1,800 / month"
    price_cents             INTEGER,
        -- Parsed price in cents e.g. 180000 for $1,800. NULL if unparseable.
    location_raw            TEXT,
    bedrooms                TEXT,
        -- Raw string from CSV if present e.g. "2" or "1+den"
    bathrooms               TEXT,
    image_url               TEXT,
    scraped_at              TEXT,
        -- ISO 8601 datetime string from the CSV "scraped_at" column
    extra_fields            TEXT,
        -- JSON blob of any CSV columns not explicitly mapped above

    -- Record metadata
    first_seen_at           TEXT    NOT NULL
        DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
        -- When this pipeline first inserted this listing_id
    run_id                  TEXT,
        -- UUID of the pipeline run that first saw this listing

    -- Playwright scrape results
    description             TEXT,
        -- Full text extracted by Playwright. NULL until successfully scraped.
    description_source      TEXT,
        -- Selector level used: "primary"|"secondary"|"tertiary"|
        --                       "og_meta"|"full_text"|"none"|"unavailable"
    description_scraped_at  TEXT,
        -- ISO 8601 timestamp of the successful Playwright scrape
    scrape_attempts         INTEGER NOT NULL DEFAULT 0,
    scrape_error            TEXT,
        -- Last error message if scrape_status indicates failure

    -- Pipeline state machine
    status                  TEXT    NOT NULL DEFAULT 'pending'
        CHECK (status IN (
            'pending',              -- Seen in CSV, not yet scraped
            'scraped',              -- Playwright ran (description may still be NULL)
            'scrape_failed',        -- All Playwright retries exhausted
            'unavailable',          -- Listing removed from Facebook
            'pre_filter_rejected',  -- Rejected by rules.py before LLM call
            'filter_passed',        -- GPT said PASS and score >= min_score
            'filter_rejected',      -- GPT said REJECT or score < min_score
            'notified',             -- Telegram message sent successfully
            'notify_failed'         -- Telegram failed after retries; retry next run
        )),

    -- AI filter results
    filter_decision         TEXT
        CHECK (filter_decision IN ('PASS', 'REJECT', NULL)),
    filter_score            INTEGER,
        -- Total score 0-24 from GPT score_breakdown sum
    filter_reasoning        TEXT,
        -- GPT 1-4 sentence explanation
    filter_score_breakdown  TEXT,
        -- JSON object e.g. {"neighbourhood":3,"laundry":2,...}
    filter_processed_at     TEXT,
        -- ISO 8601 when AI filter ran

    -- Notification tracking
    notified_at             TEXT
        -- ISO 8601 when Telegram message was successfully sent
);

-- ============================================================
-- TABLE: run_log
-- One row per pipeline execution for audit and statistics.
-- ============================================================
CREATE TABLE IF NOT EXISTS run_log (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id              TEXT    NOT NULL UNIQUE,
        -- UUID4 generated at pipeline start
    started_at          TEXT    NOT NULL,
    finished_at         TEXT,
        -- NULL if the run crashed before a clean exit
    csv_path            TEXT    NOT NULL,
    dry_run             INTEGER NOT NULL DEFAULT 0,
        -- 1 if --dry-run flag was active; 0 otherwise
    rows_in_csv         INTEGER DEFAULT 0,
    new_listings        INTEGER DEFAULT 0,
        -- Count after deduplication; excludes already-seen listing_ids
    pre_filter_rejected INTEGER DEFAULT 0,
    scraped_ok          INTEGER DEFAULT 0,
    scrape_failed       INTEGER DEFAULT 0,
    filter_passed       INTEGER DEFAULT 0,
    filter_rejected     INTEGER DEFAULT 0,
    notified            INTEGER DEFAULT 0,
    notify_failed       INTEGER DEFAULT 0,
    exit_status         TEXT
        CHECK (exit_status IN ('success', 'partial', 'cookie_expired', 'crash', NULL)),
    error_summary       TEXT
        -- JSON array of top-level error message strings
);

-- ============================================================
-- TABLE: cookie_health
-- Tracks Facebook cookie validity on each pipeline check.
-- ============================================================
CREATE TABLE IF NOT EXISTS cookie_health (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    checked_at      TEXT    NOT NULL,
    is_valid        INTEGER NOT NULL CHECK (is_valid IN (0, 1)),
    failure_reason  TEXT,
    run_id          TEXT
);

-- ============================================================
-- INDEXES
-- ============================================================

-- Primary dedup lookup (UNIQUE constraint already creates an index,
-- but naming it explicitly aids debugging and future schema inspection)
CREATE UNIQUE INDEX IF NOT EXISTS idx_listings_listing_id
    ON listings (listing_id);

-- Re-notification recovery: find listings where Telegram send failed
CREATE INDEX IF NOT EXISTS idx_listings_notify_failed
    ON listings (status)
    WHERE status = 'notify_failed';

-- Time-based queries for future dashboard
CREATE INDEX IF NOT EXISTS idx_listings_first_seen
    ON listings (first_seen_at DESC);

-- Price range queries for future dashboard / analysis
CREATE INDEX IF NOT EXISTS idx_listings_price
    ON listings (price_cents)
    WHERE price_cents IS NOT NULL;

-- Run log time ordering
CREATE INDEX IF NOT EXISTS idx_run_log_started
    ON run_log (started_at DESC);
