# rent-finder Implementation Plan

> **Status:** Pending approval
> **Delivery:** Save approved plan as `PLAN.md` in project root (`d:/GSClaude/PLAN.md`) as Milestone 1 step.

---

## 0. Operating Rules (Implementation Contract)

These rules govern all implementation sessions. They override any default behaviour.

### Workflow Rules
- Always read `PLAN.md` before writing any code in a new session.
- Never deviate from this plan's architecture without flagging it first.
- Build **one milestone at a time** and stop for confirmation after each.
- Never start the next milestone until the user explicitly types **NEXT**.
- Never make silent architectural decisions — surface every ambiguity immediately.

### Git Rules
- Commit after every milestone using conventional commit format.
- `cookies.json` must **NEVER** be committed under any circumstance.
- `.env` must **NEVER** be committed under any circumstance.
- Commit message format: `type(scope): description`
  - Examples: `feat(scraper): add playwright cookie injection`
  - Examples: `fix(filter): handle missing listing description`
  - Examples: `chore(deps): pin openai to 1.30.0`

### External Service Rules
- `--dry-run` flag must work at every milestone involving external calls.
- All API calls wrapped in `try/except` with meaningful error messages.
- Failed listings must log and continue — **never crash the pipeline**.

### Milestone Output Format

After completing each milestone, respond in exactly this structure:

```
✅ MILESTONE [N] COMPLETE: [name]

FILES CREATED:
- [filename] — [description]

FILES MODIFIED:
- [filename] — [what changed and why]

TO VERIFY:
[exact command to run]

EXPECTED OUTPUT:
[what success looks like]

ASSUMPTIONS MADE:
[anything not covered in PLAN.md]

COMMIT READY:
[exact commit message]

Waiting for: NEXT
```

### Blocker Format

If any blocker is encountered, stop immediately and report:

```
🚨 BLOCKER AT MILESTONE [N]: [name]

WHAT HAPPENED:
ROOT CAUSE:
OPTIONS:
- Option A — [approach, tradeoff]
- Option B — [approach, tradeoff]
RECOMMENDATION:
Waiting for your decision.
```

### Current Status Checklist

```
[ ] PLAN.md generated and approved
[ ] Milestone 1  — Project scaffold
[ ] Milestone 2  — Configuration and logging
[ ] Milestone 3  — SQLite module
[ ] Milestone 4  — CSV reader and models
[ ] Milestone 5  — Pre-filter rules engine
[ ] Milestone 6  — Playwright scraper
[ ] Milestone 7  — OpenAI filter
[ ] Milestone 8  — Telegram notifier
[ ] Milestone 9  — Pipeline orchestrator and CLI
[ ] Milestone 10 — End-to-end test suite
[ ] Milestone 11 — Final cleanup and documentation
```

---

## 1. Project Overview

rent-finder is a personal, on-demand automation pipeline that eliminates the daily grind of manually browsing Facebook Marketplace for Toronto rentals. The system ingests a pre-scraped CSV of listings, deduplicates against a local SQLite database so nothing is evaluated twice, launches a Playwright browser session authenticated with injected Facebook session cookies to retrieve the full dynamically-rendered listing description, feeds each enriched listing to OpenAI GPT-4o-mini for intelligent criteria-based evaluation, and delivers only matching listings as formatted Telegram notifications. Designed for a single user, the tool runs on demand via CLI or on a cron schedule via daemon mode.

**Data flow for a single listing:**

```
[CSV Row]
    │ url, title, price, location, scraped_at
    ▼
[csv_reader.py]  ── extract listing_id from URL via regex
    │ RawListing dataclass
    ▼
[repository.py]  ── check listing_id in SQLite listings table
    │ already seen? ──► SKIP (log DEBUG "duplicate")
    │ new listing
    ▼
[rules.py]  ── price_cents > cap? ──► REJECT (log INFO, save to DB)
    │ passes pre-filter
    ▼
[facebook.py / Playwright]  ── inject cookies → navigate URL → extract description
    │ login wall detected? ──► ABORT RUN (alert Telegram, exit 2)
    │ EnrichedListing (+ description, description_source)
    ▼
[openai_client.py / GPT-4o-mini]  ── structured JSON filter decision
    │ verdict: "REJECT" ──► save to DB, continue
    │ verdict: "PASS" (score ≥ min_score)
    ▼
[telegram.py]  ── MarkdownV2 formatted notification sent
    │
    ▼
[repository.py]  ── mark listing notified=True in DB
```

---

## 2. Assumptions Log

Every ambiguity is resolved here. These are not questions — they are architectural decisions with explicit justifications.

1. **CSV column names:** The pre-scraped CSV contains these columns (header row required, case-insensitive): `url`, `title`, `price`, `location`, `scraped_at`. Optional columns `image_url`, `bedrooms`, `bathrooms` are stored if present. Any additional columns are stored in a JSON blob (`extra_fields`). Rows missing `url` are skipped with a WARNING.

2. **Listing ID extraction:** Facebook Marketplace URLs follow `https://www.facebook.com/marketplace/item/{NUMERIC_ID}/`. The `listing_id` is extracted via regex `r'/item/(\d+)/'` applied to the `url` column. This is the canonical deduplication key. Rows where the regex finds no match are skipped.

3. **CSV encoding:** UTF-8 with optional BOM. The CSV reader uses `encoding='utf-8-sig'` to handle both.

4. **Cookie file format:** JSON array of cookie objects in Playwright-native format or Cookie-Editor extension export format. Both use the same keys (`name`, `value`, `domain`, `path`, `httpOnly`, `secure`, `sameSite`) with the only difference being that Cookie-Editor uses `expirationDate` where Playwright uses `expires`. The loader normalises both formats. File stored at path defined by `FACEBOOK_COOKIES_PATH` env var.

5. **Cookie health check:** Two required cookies confirm a valid Facebook session: `c_user` (user ID, indicates logged-in state) and `xs` (session token). Their absence at startup triggers a `CookieExpiredError` before any scraping begins.

6. **Toronto rental criteria (defaults, all configurable via .env):**
   - Max rent: $2,400/month CAD
   - Property type: whole unit only (apartment, condo, basement with windows, garden suite, laneway). Shared rooms, rooming houses, homestays → always REJECT.
   - Bedrooms: studio/bachelor acceptable if price < $1,500; 1-bedroom or 1+den preferred; 2-bedroom maximum.
   - Location preference: Leslieville, Riverside, Corktown, Distillery District, St. Lawrence, Regent Park (east end bias). Suburbs without TTC access → lower score.
   - Pets: Must not say "no pets" (user has one small cat).
   - Lease: Minimum 12 months. Short-term listings (<6 months) → REJECT.

7. **Python version:** 3.11+. Uses `X | None` union syntax, `tomllib` stdlib, and `asyncio.TaskGroup`. Minimum tested version is 3.11.

8. **Sync/async design:** The pipeline orchestrator is synchronous. Playwright scraping is the single async island, invoked via `asyncio.run()`. OpenAI calls are synchronous (no parallelism needed for a personal tool). This keeps the code readable for mid-level developers.

9. **OpenAI model:** `gpt-4o-mini` at temperature `0.0` for deterministic decisions. JSON mode (`response_format={"type": "json_object"}`) enforced to minimise parse failures.

10. **Telegram delivery:** Raw `httpx` HTTP client calls to the Telegram Bot API. No `python-telegram-bot` library — simpler dependency footprint for this use case. MarkdownV2 parse mode.

11. **Database location:** `data/rent_finder.db` relative to project root (configurable via `DATABASE_PATH` env var).

12. **Log format:** `structlog` with JSON output to rotating log files and pretty console output. Log directory: `logs/` relative to project root.

13. **Dry-run mode:** When `--dry-run` is passed, Telegram sends and DB writes are both suppressed. All other steps (CSV read, scrape, OpenAI filter) execute normally. The end-of-run summary IS sent to Telegram even in dry-run (so the user knows the run happened and sees counts).

14. **Rate limiting:** 4–8 second random uniform delay between Playwright page visits (configurable). Never set below 2 seconds.

15. **Error tolerance per listing:** The pipeline is fault-tolerant at the listing level. A failure on one listing (scrape timeout, OpenAI error) is logged and the pipeline continues. The run only aborts on systemic failures: cookie expiry, OpenAI auth error, DB unavailable.

16. **No live Facebook scraping:** This tool does NOT scrape Facebook Marketplace for listings. It consumes a pre-scraped CSV. Live scraping is a future extension via Apify integration (see Section 15).

17. **Re-notification recovery:** On startup, `repository.get_unnotified_passes()` retrieves any PASS listings from previous runs where Telegram delivery failed. These are re-notified before processing the new CSV.

18. **Scheduling:** APScheduler for daemon mode (cron trigger). `--once` flag for on-demand runs. Schedule is not managed internally by default.

19. **Score threshold:** Only PASS listings meeting `CRITERIA_MIN_SCORE` (default: 12/24) trigger a Telegram notification. This suppresses low-quality passes.

20. **Windows compatibility:** All paths use `pathlib.Path`. `asyncio.run()` uses `ProactorEventLoop` on Windows (compatible with Playwright). SIGTERM handler is Unix-only; Windows Task Scheduler is the daemon alternative.

---

## 3. Technical Architecture

### 3.1 File and Folder Structure

```
rent-finder/
├── .env                              # Active secrets (never committed)
├── .env.example                      # Documented template for all variables
├── .gitignore                        # Excludes .env, *.db, cookies.json, logs/, input CSVs
├── .pre-commit-config.yaml           # Pre-commit hooks: block cookies.json, detect-secrets
├── pyproject.toml                    # Project metadata, ruff config, mypy config, pytest config
├── requirements.txt                  # Pinned dependencies with comments
├── README.md                         # Setup, cookie export procedure, usage guide
├── PLAN.md                           # This implementation plan (created at Milestone 1)
│
├── rent_finder/                      # Main application package
│   ├── __init__.py                   # Package marker, exposes __version__ = "1.0.0"
│   ├── main.py                       # CLI entry point (click); orchestrates full pipeline
│   ├── config.py                     # pydantic-settings BaseSettings; loads/validates .env
│   ├── scheduler.py                  # APScheduler setup for --daemon cron mode
│   │
│   ├── ingestion/
│   │   ├── __init__.py
│   │   ├── models.py                 # RawListing, EnrichedListing Pydantic models
│   │   └── csv_reader.py             # CSV parsing, column normalisation, listing_id extraction
│   │
│   ├── storage/
│   │   ├── __init__.py
│   │   ├── database.py               # SQLite connection factory, WAL mode, schema init
│   │   ├── schema.sql                # Canonical DDL; applied once at startup
│   │   └── repository.py            # CRUD: dedup check, save listing, update status, run log
│   │
│   ├── scraper/
│   │   ├── __init__.py
│   │   ├── browser.py                # Playwright async context factory; cookie injection; health check
│   │   ├── facebook.py               # Page navigation, 6-level selector fallback, description extractor
│   │   └── rate_limiter.py           # Token-bucket async rate limiter (configurable delay range)
│   │
│   ├── filtering/
│   │   ├── __init__.py
│   │   ├── rules.py                  # Deterministic pre-filter (price cap) before LLM call
│   │   ├── prompt.py                 # SYSTEM_PROMPT constant + build_user_message()
│   │   └── openai_client.py          # GPT-4o-mini API call, JSON parse, FilterResult model
│   │
│   ├── notifications/
│   │   ├── __init__.py
│   │   ├── formatter.py              # MarkdownV2 escape + message template builder
│   │   └── telegram.py               # httpx-based Telegram Bot API sender; retry logic
│   │
│   └── utils/
│       ├── __init__.py
│       ├── logging_config.py         # structlog setup: JSON file + pretty console
│       └── retry.py                  # tenacity-based exponential backoff decorator
│
├── data/
│   ├── .gitkeep
│   ├── rent_finder.db                # SQLite database (gitignored)
│   └── cookies.json                  # Facebook session cookies (gitignored — treat as password)
│
├── input/
│   ├── .gitkeep
│   └── marketplace_export.csv        # Drop pre-scraped CSVs here (gitignored)
│
├── logs/
│   ├── .gitkeep
│   └── rent_finder_YYYY-MM-DD.jsonl  # Daily rotating structured log (gitignored)
│
└── tests/
    ├── __init__.py
    ├── conftest.py                   # Shared fixtures: tmp_db, mock_settings, sample CSV
    ├── test_csv_reader.py            # CSV parsing and listing_id extraction unit tests
    ├── test_repository.py            # SQLite CRUD and deduplication unit tests
    ├── test_rules.py                 # Pre-filter price cap logic unit tests
    ├── test_scraper.py               # Playwright selector chain and cookie injection unit tests
    ├── test_openai_filter.py         # GPT filter, retry, and parse failure unit tests
    ├── test_formatter.py             # MarkdownV2 escaping and message layout unit tests
    ├── test_telegram.py              # Telegram send, retry, and truncation unit tests
    ├── test_integration.py           # Full pipeline integration tests (all I/O mocked)
    └── fixtures/
        ├── sample_listings.csv       # Valid CSV for testing (non-real URLs, fake data)
        ├── sample_page.html          # Captured Facebook listing HTML for selector tests
        └── sample_cookies.json       # Non-functional fake cookies (safe to commit)
```

### 3.2 Module Dependency Map

```
main.py
  ├── config.py                          (no internal imports)
  ├── scheduler.py                       → config.py
  │
  └── pipeline() orchestrator
        ├── ingestion/csv_reader.py      → ingestion/models.py, config.py
        ├── storage/database.py          → storage/schema.sql (read at runtime)
        ├── storage/repository.py        → storage/database.py, ingestion/models.py
        ├── filtering/rules.py           → ingestion/models.py, config.py
        ├── scraper/browser.py           → config.py, utils/logging_config.py
        ├── scraper/facebook.py          → scraper/browser.py, scraper/rate_limiter.py,
        │                                  ingestion/models.py
        ├── filtering/openai_client.py   → filtering/prompt.py, ingestion/models.py,
        │                                  config.py, utils/retry.py
        ├── notifications/formatter.py  → ingestion/models.py
        └── notifications/telegram.py   → notifications/formatter.py, config.py,
                                          utils/retry.py

All modules import: utils/logging_config.py (structlog logger)
External deps: scraper/ → playwright; filtering/ → openai; notifications/ → httpx
Internal dep isolation: storage/ never imports from any other internal package
```

### 3.3 Data Flow with Types at Each Boundary

```
csv_reader.parse(path) → list[RawListing]
repository.get_seen_ids(conn) → set[str]
  → dedup filter → list[RawListing]  (new only)
rules.apply(listing, settings) → tuple[bool, list[str]]
  → pre-filter → list[RawListing]  (passes pre-filter)
asyncio.run(scraper.scrape_all(listings, settings))
  → list[tuple[RawListing, str|None, str]]  (listing, description, source)
  → EnrichedListing constructed
openai_client.filter(listing, settings) → FilterResult
  → verdict: "PASS"|"REJECT", score: int, reasoning: str, breakdown: dict
telegram.send(listing, result, dry_run) → bool
repository.mark_notified(conn, listing_id) → None
```

### 3.4 Third-Party Services

| Service | Why Chosen | Alternative Rejected |
|---|---|---|
| OpenAI GPT-4o-mini | Lowest cost LLM with structured JSON mode; ~$0.005/run; excellent at criteria-based classification | Claude Haiku (slightly pricier, less widely documented for JSON mode); Ollama (requires local GPU, complex setup) |
| Playwright | First-class async Python API; cookie injection is trivial; handles dynamic React SPAs; actively maintained | Selenium (verbose, no modern async API); requests-html (unreliable for heavy SPAs) |
| httpx (Telegram) | Zero-dependency sync HTTP; no abstraction layer needed for 2 API endpoints | python-telegram-bot (adds async complexity and 15+ transitive deps for simple sends) |
| structlog | One-line setup; JSON to file, pretty to console; zero boilerplate vs stdlib logging | loguru (similar simplicity but structlog's JSON output is more parseable for future dashboard) |
| tenacity | Decorator-based retry with backoff, jitter, and exception filtering; clean API | Custom retry loops (error-prone to implement correctly) |
| APScheduler | Proven cron-style scheduling with asyncio executor; no daemon process required | Celery (overkill for personal tool); cron (external, not self-documenting) |
| pydantic-settings | Type-validated .env loading with coercion; `ValidationError` at startup prevents runtime surprises | python-dotenv alone (no type validation); dynaconf (over-featured for this use case) |
| SQLite (stdlib) | Zero server setup; single-file; perfect ACID guarantees for personal tools; already in Python stdlib | PostgreSQL (overkill); JSON file (no indexing, no ACID) |

---

## 4. SQLite Schema Design

### 4.1 Full Schema DDL

```sql
-- storage/schema.sql
-- Applied by database.init_db() on first run if tables do not exist.
-- Connection-time PRAGMAs (not in DDL): journal_mode=WAL, foreign_keys=ON,
-- busy_timeout=5000, synchronous=NORMAL

-- ============================================================
-- TABLE: listings
-- Canonical record for every listing ever seen in any CSV.
-- ============================================================
CREATE TABLE IF NOT EXISTS listings (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    listing_id              TEXT    NOT NULL UNIQUE,
        -- Numeric ID extracted from Facebook URL path; dedup key
    url                     TEXT    NOT NULL,
    title                   TEXT    NOT NULL,
    price_raw               TEXT,
        -- Original string e.g. "$1,800 / month"
    price_cents             INTEGER,
        -- Parsed: 180000 for $1,800. NULL if unparseable.
    location_raw            TEXT,
    bedrooms                TEXT,
        -- Raw string from CSV if present e.g. "2" or "1+den"
    bathrooms               TEXT,
    image_url               TEXT,
    scraped_at              TEXT,
        -- ISO 8601 datetime from CSV column
    extra_fields            TEXT,
        -- JSON blob of any CSV columns not explicitly mapped
    first_seen_at           TEXT    NOT NULL
        DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
        -- When this pipeline first encountered this listing_id
    description             TEXT,
        -- Full text extracted by Playwright. NULL until scraped.
    description_source      TEXT,
        -- Which selector level succeeded: "primary"|"secondary"|...
        -- "og_meta"|"full_text"|"none"
    description_scraped_at  TEXT,
        -- ISO 8601 timestamp of successful Playwright scrape
    scrape_attempts         INTEGER NOT NULL DEFAULT 0,
    scrape_error            TEXT,
        -- Last error message if scrape failed
    status                  TEXT    NOT NULL DEFAULT 'pending'
        CHECK (status IN (
            'pending',        -- In CSV, not yet scraped
            'scraped',        -- Description retrieved (may be NULL if selector failed)
            'scrape_failed',  -- All Playwright retries exhausted
            'unavailable',    -- Listing removed from Facebook
            'pre_filter_rejected', -- Rejected by rules.py before LLM
            'filter_passed',  -- GPT said PASS; score >= min_score
            'filter_rejected',-- GPT said REJECT or score < min_score
            'notified',       -- Telegram message sent successfully
            'notify_failed'   -- Telegram send failed after retries; retry next run
        )),
    filter_decision         TEXT
        CHECK (filter_decision IN ('PASS', 'REJECT', NULL)),
    filter_score            INTEGER,
        -- Total score 0-24 from GPT score_breakdown sum
    filter_reasoning        TEXT,
        -- GPT's 2-4 sentence explanation
    filter_score_breakdown  TEXT,
        -- JSON object: {"neighbourhood":3,"laundry":2,...}
    filter_processed_at     TEXT,
    notified_at             TEXT,
        -- ISO 8601 when Telegram message successfully sent
    run_id                  TEXT
        -- UUID of the pipeline run that first processed this listing
);

-- ============================================================
-- TABLE: run_log
-- One row per pipeline execution for observability.
-- ============================================================
CREATE TABLE IF NOT EXISTS run_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id          TEXT    NOT NULL UNIQUE,
        -- UUID4, generated at pipeline start
    started_at      TEXT    NOT NULL,
    finished_at     TEXT,
        -- NULL if crashed before clean exit
    csv_path        TEXT    NOT NULL,
    dry_run         INTEGER NOT NULL DEFAULT 0,
        -- 1 if --dry-run flag was set
    rows_in_csv     INTEGER DEFAULT 0,
    new_listings    INTEGER DEFAULT 0,
        -- After deduplication against DB
    pre_filter_rejected INTEGER DEFAULT 0,
    scraped_ok      INTEGER DEFAULT 0,
    scrape_failed   INTEGER DEFAULT 0,
    filter_passed   INTEGER DEFAULT 0,
    filter_rejected INTEGER DEFAULT 0,
    notified        INTEGER DEFAULT 0,
    notify_failed   INTEGER DEFAULT 0,
    exit_status     TEXT
        CHECK (exit_status IN ('success','partial','cookie_expired','crash',NULL)),
    error_summary   TEXT
        -- JSON array of top-level error messages for the run
);

-- ============================================================
-- TABLE: cookie_health
-- Tracks Facebook cookie validity per pipeline run.
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
-- Primary dedup check (also enforced by UNIQUE constraint)
CREATE UNIQUE INDEX IF NOT EXISTS idx_listings_listing_id
    ON listings (listing_id);

-- Efficiently find listings needing re-notification on next run
CREATE INDEX IF NOT EXISTS idx_listings_notify_failed
    ON listings (status)
    WHERE status = 'notify_failed';

-- Time-based queries for future dashboard
CREATE INDEX IF NOT EXISTS idx_listings_first_seen
    ON listings (first_seen_at DESC);

-- Price range queries for future dashboard/analysis
CREATE INDEX IF NOT EXISTS idx_listings_price
    ON listings (price_cents)
    WHERE price_cents IS NOT NULL;

-- Run log time ordering
CREATE INDEX IF NOT EXISTS idx_run_log_started
    ON run_log (started_at DESC);
```

### 4.2 Schema Reasoning

- **`listing_id` as TEXT UNIQUE:** Facebook numeric IDs can exceed SQLite's safe INTEGER range; TEXT avoids truncation. The UNIQUE constraint provides O(log n) dedup.
- **`price_cents` INTEGER:** Avoids floating point. Enables future WHERE clauses like `price_cents BETWEEN 120000 AND 240000`.
- **`extra_fields` JSON TEXT:** Future-proofs against unknown CSV columns without schema migrations.
- **`status` enum with CHECK:** Enforces state machine integrity at DB level. Prevents invalid states from corrupt writes.
- **`filter_score_breakdown` JSON TEXT:** Stores the full GPT score object without requiring 8 extra columns now.
- **BOOLEAN as INTEGER:** SQLite has no native BOOLEAN. The `dry_run` and `is_valid` columns use 0/1.
- **ISO 8601 TEXT for all timestamps:** SQLite's lexicographic comparison works correctly on ISO 8601, enabling ORDER BY and range queries without conversion.

### 4.3 Migration Strategy

For this personal tool, migrations are handled pragmatically:
1. `database.init_db()` runs `schema.sql` with `CREATE TABLE IF NOT EXISTS` — safe to run repeatedly.
2. When a column must be added: use `ALTER TABLE listings ADD COLUMN new_col TYPE DEFAULT x;` — SQLite supports this.
3. For structural changes (rename column, change type): `database.migrate()` backs up the DB to `rent_finder.db.bak` then runs a migration SQL file from a future `storage/migrations/` folder.
4. A `schema_version` table will be added only when the first migration is needed (YAGNI principle).

---

## 5. Environment Configuration

### 5.1 Complete `.env.example`

```dotenv
# ── Database ───────────────────────────────────────────────────────────────────
# Path to SQLite database. Created on first run.
DATABASE_PATH=data/rent_finder.db

# ── Input ─────────────────────────────────────────────────────────────────────
# Default path to pre-scraped Facebook Marketplace CSV.
# Can be overridden per-run with the --csv CLI flag.
CSV_INPUT_PATH=input/marketplace_export.csv

# ── Facebook / Playwright ──────────────────────────────────────────────────────
# Path to Facebook session cookies JSON file.
# Export using "Cookie-Editor" browser extension while logged into Facebook.
# Required cookies: c_user (user ID), xs (session token), datr, fr.
# NEVER commit this file. Treat it as your Facebook account password.
FACEBOOK_COOKIES_PATH=data/cookies.json

# Min seconds of random delay between Playwright page loads. Never set below 2.
SCRAPER_MIN_DELAY_SECONDS=4

# Max seconds of random delay between Playwright page loads.
SCRAPER_MAX_DELAY_SECONDS=8

# Maximum listings to scrape per run (0 = unlimited). Use low number for testing.
SCRAPER_MAX_LISTINGS_PER_RUN=0

# Run Playwright in headless mode. Set to false for debugging selector issues.
PLAYWRIGHT_HEADLESS=true

# Page load timeout per listing URL, in milliseconds.
PLAYWRIGHT_PAGE_TIMEOUT_MS=30000

# ── OpenAI ─────────────────────────────────────────────────────────────────────
# Required. OpenAI API key. Get from: https://platform.openai.com/api-keys
OPENAI_API_KEY=sk-...

# OpenAI model for filtering. gpt-4o-mini is the cost-efficient default.
# Switch to gpt-4o for higher accuracy at ~20x the cost.
OPENAI_MODEL=gpt-4o-mini

# Max completion tokens for OpenAI response. 600 is ample for the JSON response.
OPENAI_MAX_TOKENS=600

# ── Rental Criteria ────────────────────────────────────────────────────────────
# Maximum monthly rent in CAD (integer). Listings above this are hard-rejected
# by rules.py before any OpenAI call is made.
CRITERIA_MAX_RENT_CAD=2400

# Set to true to hard-reject listings that explicitly say "no pets".
CRITERIA_REQUIRE_PET_FRIENDLY=true

# Minimum total score (out of 24) for a PASS listing to trigger a notification.
# Listings that PASS hard criteria but score below this are saved but not notified.
CRITERIA_MIN_SCORE=12

# ── Telegram ───────────────────────────────────────────────────────────────────
# Required. Telegram Bot token from @BotFather.
TELEGRAM_BOT_TOKEN=123456789:AABBccDD...

# Required. Your personal Telegram chat ID. Find via @userinfobot on Telegram.
TELEGRAM_CHAT_ID=123456789

# Send an end-of-run summary message to Telegram after each pipeline run.
TELEGRAM_SEND_SUMMARY=true

# HTTP timeout in seconds for Telegram API requests.
TELEGRAM_REQUEST_TIMEOUT_SECONDS=15

# ── Scheduling (daemon mode only) ──────────────────────────────────────────────
# Cron expression for scheduled runs. APScheduler cron trigger format.
# Default: 8:00 AM and 6:00 PM Toronto time every day.
SCHEDULE_CRON=0 8,18 * * *

# IANA timezone name for cron schedule.
SCHEDULE_TIMEZONE=America/Toronto

# ── Logging ────────────────────────────────────────────────────────────────────
# Directory for log files. Created if it does not exist.
LOG_DIR=logs

# Log level for file output (JSON). Options: DEBUG INFO WARNING ERROR CRITICAL
LOG_LEVEL_FILE=DEBUG

# Log level for console output (pretty-printed).
LOG_LEVEL_CONSOLE=INFO
```

### 5.2 `.gitignore` Contents

```gitignore
# === SECURITY — NEVER COMMIT ===================================================
.env
# Facebook session cookie — equivalent to account password. Exposure = compromise.
data/cookies.json
# Catch-all for any cookie export file in the project root
cookies*.json
# Allow the fake test cookies
!tests/fixtures/sample_cookies.json

# === DATA — POTENTIALLY SENSITIVE ==============================================
input/*.csv
data/*.db
data/*.db-shm
data/*.db-wal
data/*.bak

# === LOGS ======================================================================
logs/
!logs/.gitkeep

# === PYTHON ====================================================================
__pycache__/
*.py[cod]
*$py.class
*.so
.Python
.venv/
venv/
env/
.eggs/
*.egg-info/
dist/
build/

# === TOOLS =====================================================================
.pytest_cache/
.mypy_cache/
.ruff_cache/
.coverage
htmlcov/
.DS_Store
Thumbs.db
```

### 5.3 Security Notes

**`cookies.json` — treat as an account password:**
- This file grants complete access to a Facebook account. Any commit, even briefly, should be treated as a full account compromise.
- Two layers of protection: (1) `.gitignore` entry, (2) pre-commit hook that blocks any staged file matching `*cookie*.json` outside `tests/fixtures/`.
- If exposure occurs: immediately log out all Facebook sessions (Settings → Security → Active Sessions → Log Out All), change password, re-export fresh cookies.
- Do not paste cookie values into chat, tickets, or logs. The `logging_config.py` must never log the full cookies file contents.

**API Keys (`OPENAI_API_KEY`, `TELEGRAM_BOT_TOKEN`):**
- Stored in `.env` only. Never hardcoded, never logged.
- For server deployment: use OS-level secrets management (Windows Credential Manager, macOS Keychain, or environment variables set by the OS, not a .env file).
- The `config.py` Settings model logs a sanitised version of config at startup (keys masked to first 8 chars).

---

## 6. OpenAI Integration Design

### 6.1 Full System Prompt

Stored as `SYSTEM_PROMPT` constant in `rent_finder/filtering/prompt.py`.

```
You are a precise rental listing evaluator for a person searching for an apartment
in Toronto, Canada. Your task is to evaluate a single rental listing and decide
whether it meets the searcher's criteria. Respond ONLY with a valid JSON object —
no markdown, no prose outside the JSON structure.

## Searcher Profile
Single adult with one small cat, works remotely full-time, needs a home office
space, wants a long-term rental (minimum 12-month lease) in Toronto.

## Hard Requirements — ANY failure means "REJECT"

1. PRICE: Monthly rent must not exceed $2,400 CAD inclusive of mandatory fees.
   - If price is absent, REJECT.
   - If price says "starting from X" where X > $2,400, REJECT.
   - If price range spans $2,400 (e.g., $2,200–$2,600), REJECT conservatively.

2. UNIT TYPE: Must be an entire self-contained unit for exclusive use:
   - ACCEPTABLE: apartment, condo, basement apartment (with confirmed windows),
     garden suite, laneway house, 1+den.
   - REJECT: shared room, private room in shared house, rooming house, homestay,
     any listing where bathroom is shared with strangers.
   - If ambiguous and description says "private room", REJECT.

3. PETS: If the listing explicitly says "no pets", "no animals", "no cats",
   or "no dogs", REJECT. If silent on pets, do NOT reject — mark as noted.

4. LEASE LENGTH: Minimum 12-month lease required.
   - REJECT if listing says "short-term", "month-to-month only", or available
     duration is explicitly stated as less than 6 months.
   - If lease duration is not mentioned, do NOT reject — mark as noted.

5. LEGITIMACY: REJECT with scam_flag=true if:
   - Price is suspiciously below market (< $900/month for a 1BR in Toronto).
   - Listing requests e-transfer deposit before viewing.
   - Listing says to contact only via WhatsApp with no other contact method.
   - Multiple grammatical errors combined with urgency language ("must rent ASAP").

## Soft Preference Scoring — score each 0 to 3

Sum all 8 categories for a total_score out of 24.

NEIGHBOURHOOD (0-3):
  3 = Leslieville, Riverside, Corktown, Distillery District, St. Lawrence,
      Regent Park, Moss Park, Dundas East, Queen East.
  2 = Danforth, Greektown, Broadview North, Downtown East, Rosedale, Cabbagetown.
  1 = Downtown Core, Annex, Kensington Market, Little Italy, Chinatown.
  0 = Etobicoke, Scarborough (far), North York, Mississauga, Brampton,
      or location not mentioned.
  NOTE: If location says only "Toronto" with no neighbourhood, score 1.

LAUNDRY (0-3):
  3 = In-suite washer/dryer.
  2 = Ensuite laundry in building (coin or card laundry room).
  1 = Shared laundry in building or nearby.
  0 = No mention of laundry, or laundromat only.

TRANSIT (0-3):
  3 = Walking distance to subway station mentioned explicitly.
  2 = Streetcar or bus line mentioned, or "TTC accessible".
  1 = General transit mention with no specifics.
  0 = No transit mention, or car required.

NATURAL LIGHT (0-3):
  3 = South/west/east-facing, large windows, or "bright" mentioned.
  2 = Windows mentioned or confirmed above-ground unit.
  1 = Above-ground implied (not basement), no window mention.
  0 = Basement with no light confirmation, or north-facing only.

CONDITION (0-3):
  3 = Recently renovated, new appliances, modern finishes.
  2 = Well-maintained, updated kitchen/bath.
  1 = As-is, older building, no renovations mentioned.
  0 = Mentions of damage, mold, major maintenance issues, or poor condition.

INTERNET (0-3):
  3 = Internet explicitly included in rent.
  2 = "All utilities included" (likely includes internet).
  1 = Some utilities included (e.g., heat and water only).
  0 = All utilities separate, or no mention.

OFFICE SUITABILITY (0-3):
  3 = Den, separate office room, or explicitly quiet/professional building.
  2 = Mentions desk space, quiet neighbourhood, or work-from-home friendly.
  1 = Open-concept (workable for home office with furniture).
  0 = Noisy environment, nightlife proximity, or shared common spaces.

MOVE-IN TIMING (0-3):
  3 = Available immediately or within 30 days.
  2 = Available 31-60 days from now.
  1 = Available 61-90 days from now.
  0 = Available 90+ days from now, or date not mentioned.

## Response Format

Respond ONLY with this exact JSON structure. No other text.

{
  "decision": "PASS",
  "rejection_reasons": [],
  "scam_flag": false,
  "total_score": 0,
  "score_breakdown": {
    "neighbourhood": 0,
    "laundry": 0,
    "transit": 0,
    "natural_light": 0,
    "condition": 0,
    "internet": 0,
    "office_suitability": 0,
    "move_in_timing": 0
  },
  "reasoning": "2-3 sentence plain-English explanation of the decision."
}

RULES:
- decision must be exactly "PASS" or "REJECT" (uppercase, no other values).
- rejection_reasons is a list of short strings naming each hard requirement failed.
- scam_flag is a boolean (true/false, not a string).
- total_score is the integer sum of score_breakdown values.
- reasoning must be 1-4 sentences. No markdown formatting inside reasoning.
- Never refuse to respond. Always return valid JSON even for very short descriptions.
- If description is empty or under 20 words, still evaluate on title + price + location.
```

### 6.2 Input Structure Sent to API

```python
# filtering/openai_client.py

def build_messages(listing: EnrichedListing) -> list[dict]:
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {
            "role": "user",
            "content": (
                f"LISTING ID: {listing.listing_id}\n"
                f"TITLE: {listing.title or 'Not specified'}\n"
                f"PRICE: {listing.price_raw or 'Not specified'}\n"
                f"LOCATION: {listing.location_raw or 'Not specified'}\n"
                f"BEDROOMS: {listing.bedrooms or 'Not specified'}\n"
                f"BATHROOMS: {listing.bathrooms or 'Not specified'}\n"
                f"URL: {listing.url}\n\n"
                f"FULL DESCRIPTION:\n"
                f"{listing.description or '[No description available — evaluate on title and price only]'}"
            ),
        },
    ]

# API call parameters:
response = client.chat.completions.create(
    model=settings.openai_model,             # "gpt-4o-mini"
    messages=build_messages(listing),
    temperature=0.0,                          # Deterministic
    max_tokens=settings.openai_max_tokens,    # 600
    response_format={"type": "json_object"},  # Enforce JSON mode
)
```

### 6.3 Output Format and Validation

Expected response parsed from `response.choices[0].message.content`:

```json
{
  "decision": "PASS",
  "rejection_reasons": [],
  "scam_flag": false,
  "total_score": 19,
  "score_breakdown": {
    "neighbourhood": 3,
    "laundry": 3,
    "transit": 2,
    "natural_light": 3,
    "condition": 2,
    "internet": 1,
    "office_suitability": 3,
    "move_in_timing": 2
  },
  "reasoning": "South-facing 1-bedroom in Leslieville with in-suite laundry and confirmed pet-friendly policy. Owner mentions home office space and nearby streetcar. Price at $1,950 is well within the $2,400 cap."
}
```

**Validation via Pydantic model in `openai_client.py`:**

```python
class FilterResult(BaseModel):
    decision: Literal["PASS", "REJECT"]
    rejection_reasons: list[str] = []
    scam_flag: bool = False
    total_score: int = Field(ge=0, le=24)
    score_breakdown: dict[str, int]  # validated: all values 0-3, 8 keys required
    reasoning: str = Field(min_length=1, max_length=600)

    @field_validator("score_breakdown")
    def validate_breakdown(cls, v):
        required = {"neighbourhood","laundry","transit","natural_light",
                    "condition","internet","office_suitability","move_in_timing"}
        if not required.issubset(v.keys()):
            raise ValueError("Missing score_breakdown keys")
        if any(not (0 <= score <= 3) for score in v.values()):
            raise ValueError("Score values must be 0-3")
        return v
```

On `ValidationError`: log WARNING with raw response, treat as REJECT with `rejection_reasons=["llm_response_invalid"]`, increment error counter, do NOT raise.

### 6.4 Token Estimation and Monthly Cost

| Component | Tokens (estimated) |
|---|---|
| System prompt | ~650 tokens |
| User message template | ~80 tokens |
| Average listing description (150 words) | ~200 tokens |
| **Total input per listing** | **~930 tokens** |
| Output per listing | ~200 tokens |

GPT-4o-mini pricing (Feb 2026): $0.00015/1K input, $0.00060/1K output.

Cost per listing: (930 × 0.00015/1000) + (200 × 0.00060/1000) = **$0.000260/listing**

At 30 new listings/day, 30 days: 900 listings × $0.000260 = **~$0.23/month** — effectively free.

### 6.5 Fallback Behavior

| Failure | Retry | Action After Exhaustion |
|---|---|---|
| `openai.APIConnectionError` | 1 retry after 5s | Mark `filter_decision=NULL`, continue to next listing |
| `openai.RateLimitError` | 3 retries: 2s, 4s, 8s (honour `retry-after` header) | Pause 60s then continue |
| `openai.APIStatusError` (5xx) | 1 retry after 5s | Mark `filter_decision=NULL`, continue |
| JSON parse failure | 1 retry with explicit JSON instruction appended | Treat as REJECT with `rejection_reasons=["llm_parse_error"]` |
| `ValidationError` | No retry | Treat as REJECT with `rejection_reasons=["llm_response_invalid"]` |
| `openai.AuthenticationError` | No retry | Raise `OpenAIAuthError` → abort entire pipeline, send Telegram alert |

---

## 7. Playwright Strategy

### 7.1 Browser Choice

**Chromium** (Playwright's default). Justification: Facebook is developed and tested against Chromium-based browsers; session cookies are most reliably accepted; Playwright's Chromium implementation has the largest test surface. Firefox is available as a fallback command-line override but is not the default.

### 7.2 Cookie Injection Method (Step-by-Step)

Implemented in `rent_finder/scraper/browser.py`:

```python
# Step 1: Load and normalise cookies from JSON file
def load_cookies(path: Path) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        raw = json.load(f)
    normalised = []
    for c in raw:
        cookie = {
            "name": c["name"],
            "value": c["value"],
            "domain": c.get("domain", ".facebook.com"),
            "path": c.get("path", "/"),
            "httpOnly": c.get("httpOnly", False),
            "secure": c.get("secure", True),
        }
        # Cookie-Editor uses "expirationDate"; Playwright uses "expires"
        expires = c.get("expires") or c.get("expirationDate")
        if expires and float(expires) > 0:
            cookie["expires"] = float(expires)
        same_site = c.get("sameSite", "Lax")
        cookie["sameSite"] = same_site.capitalize() if same_site else "Lax"
        normalised.append(cookie)
    return normalised

# Step 2: Validate required session cookies exist
def validate_cookies(cookies: list[dict]) -> None:
    names = {c["name"] for c in cookies}
    if "c_user" not in names:
        raise CookieExpiredError("Required cookie 'c_user' not found")
    if "xs" not in names:
        raise CookieExpiredError("Required cookie 'xs' not found")

# Step 3: Warn if any cookie expires within 7 days
def check_expiry_warning(cookies: list[dict]) -> list[str]:
    soon = time.time() + (7 * 86400)
    return [c["name"] for c in cookies
            if c.get("expires") and c["expires"] < soon]

# Step 4: Create async Playwright context (called once per run)
async def create_context(pw: Playwright, settings: Settings) -> tuple[Browser, BrowserContext]:
    browser = await pw.chromium.launch(
        headless=settings.playwright_headless,
        args=[
            "--disable-blink-features=AutomationControlled",
            "--no-sandbox",
            "--disable-dev-shm-usage",
        ],
    )
    context = await browser.new_context(
        viewport={"width": 1280, "height": 800},
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/122.0.0.0 Safari/537.36",
        locale="en-CA",
        timezone_id="America/Toronto",
    )
    cookies = load_cookies(Path(settings.facebook_cookies_path))
    validate_cookies(cookies)
    await context.add_cookies(cookies)  # Injected at context level (all pages share cookies)

    # Step 5: Cookie health check navigation
    page = await context.new_page()
    await page.goto("https://www.facebook.com/marketplace/",
                    wait_until="domcontentloaded", timeout=20000)
    if "/login" in page.url or await page.query_selector('input[name="email"]'):
        await browser.close()
        raise CookieExpiredError("Cookies rejected by Facebook — session expired")
    await page.close()

    return browser, context
```

### 7.3 Page Load and Dynamic Content Strategy

Facebook Marketplace listing pages are React SPAs with asynchronous content loading. Six-level selector fallback chain (tried in order; first match wins):

```
Level 1 — PRIMARY (structured test ID)
  Selector: div[data-testid="marketplace-listing-item-description"]
  Wait: wait_for_selector(timeout=8000)

Level 2 — SECONDARY (aria-label region)
  Selector: div[aria-label="Listing details"] >> p
  Wait: wait_for_selector(timeout=5000)

Level 3 — TERTIARY (React component heuristic)
  Method: page.query_selector_all('div[role="main"] span')
  Post-process: return the span with the most text (> 80 chars)

Level 4 — QUATERNARY (Open Graph meta tag — often truncated)
  Method: page.evaluate(
    "() => document.querySelector('meta[property=\"og:description\"]')?.content"
  )
  Note: Typically truncated to 200 chars; record description_source="og_meta"

Level 5 — QUINARY (full visible text extraction)
  Method: page.inner_text('div[role="main"]')
  Post-process: split by '\n', take longest block > 80 chars

Level 6 — FAILURE
  description = None
  description_source = "none"
  status = "scraped" (page loaded, description absent)
  Log WARNING: selector_chain_exhausted=True
```

**Navigation sequence per listing:**
```
1. page.goto(url, wait_until="networkidle", timeout=30000)
   → On TimeoutError: retry with wait_until="domcontentloaded"

2. Check for cookie expiry: "/login" in page.url → raise CookieExpiredError

3. Check for "listing no longer available":
   await page.query_selector('span:text("no longer available")')
   → If found: return (None, "unavailable"); do NOT attempt selector chain

4. Dismiss modal/popup:
   try: await page.click('div[aria-label="Close"]', timeout=2000)
   except: pass

5. Scroll to trigger lazy load:
   await page.evaluate("window.scrollTo(0, document.body.scrollHeight / 2)")
   await asyncio.sleep(1.5)

6. Execute selector fallback chain (levels 1-5)

7. Return (description_text, level_name)
```

### 7.4 Timeout Values, Retry Logic, and Backoff

```
Page goto timeout:         30,000ms (PLAYWRIGHT_PAGE_TIMEOUT_MS env var)
Selector wait (level 1):    8,000ms (hardcoded)
Selector wait (level 2):    5,000ms (hardcoded)
Modal dismiss timeout:      2,000ms (hardcoded, silent fail)
Max retries per listing:    2 (retry on TimeoutError and net::ERR_ errors only)
Retry wait:                 attempt 1 → 5s; attempt 2 → 10s
```

Do NOT retry on: login wall detection (abort run), "listing no longer available" (mark unavailable and continue).

### 7.5 Rate Limiting and Delay Strategy

```python
# scraper/rate_limiter.py
import asyncio, random

class RateLimiter:
    async def acquire(self, min_s: float, max_s: float) -> None:
        delay = random.uniform(min_s, max_s)
        log.debug("rate_limit_delay", seconds=round(delay, 2))
        await asyncio.sleep(delay)
```

Called after every page load attempt (success or failure). Default 4–8 seconds. If `--headed` mode: automatically increases to 6–10 seconds to allow visual inspection.

### 7.6 Cookie Expiry Detection and Graceful Failure

Detection signals checked after every `page.goto()`:
1. `"/login" in page.url`
2. `"/checkpoint" in page.url`
3. `await page.query_selector('input[name="email"]')` returns non-None

On detection:
1. Log `ERROR` with `cookie_expired=True`
2. Call `repository.insert_cookie_health(conn, is_valid=0, reason="login_redirect")`
3. Call `telegram.send_text("⚠️ rent-finder: Facebook cookies have expired. Please export fresh cookies from a logged-in browser session and replace data/cookies.json.")` — always sends even if dry_run (operational alert)
4. Close browser context cleanly: `await context.close(); await browser.close()`
5. Update run_log: `exit_status="cookie_expired"`
6. `sys.exit(2)` — exit code 2 distinguishes cookie expiry from normal (0) and error (1)

### 7.7 Headless vs Headed Mode

Default: headless (`PLAYWRIGHT_HEADLESS=true`). Pass `--headed` CLI flag (or set `PLAYWRIGHT_HEADLESS=false`) to launch a visible browser window for debugging selector issues.

Headed mode automatically:
- Increases delay range to 6–10 seconds
- Logs selector level used at INFO (not DEBUG) so it's visible in console
- Does not change any other behaviour

The same Playwright context is used for both modes; only the `launch(headless=...)` parameter changes.

---

## 8. Error Handling Matrix

| # | Failure Scenario | Detection Method | Retry Strategy | Fallback Behavior | Log Level | User Impact |
|---|---|---|---|---|---|---|
| 1 | CSV file not found | `FileNotFoundError` in csv_reader | No retry | Abort pipeline; exit code 1 | CRITICAL | No run; fix CSV path |
| 2 | CSV missing required columns | `KeyError` at header validation | No retry | Abort with descriptive message listing missing columns | CRITICAL | No run; fix CSV export |
| 3 | CSV row missing URL | Empty string check per row | Skip row | Increment `rows_skipped`; log row index | WARNING | One listing dropped |
| 4 | URL has no parseable listing_id | Regex returns None | Skip row | Log WARNING with URL value | WARNING | One listing dropped |
| 5 | Cookies file missing | `FileNotFoundError` at startup | No retry | Abort; direct user to export instructions | CRITICAL | No run starts |
| 6 | Cookies file invalid JSON | `json.JSONDecodeError` | No retry | Abort with file path and error position | ERROR | No run starts |
| 7 | Required cookies absent (`c_user`, `xs`) | Set membership check | No retry | Raise `CookieExpiredError`; abort run | ERROR | No scraping this run |
| 8 | Facebook session expired (mid-run) | Login URL or email input detected post-navigation | No retry (all cookies expired) | Send Telegram alert; mark in-progress as failed; clean exit; exit code 2 | ERROR | Partial run; future runs blocked until cookies refreshed |
| 9 | Playwright page load timeout | `TimeoutError` on `page.goto()` | Retry once (5s wait) with `domcontentloaded` | Mark `status="scrape_failed"`; continue to next listing | WARNING | One listing has no description; AI filters on title/price only |
| 10 | Selector chain exhausted | All 6 levels return None | N/A (selectors not retried) | `description=None`; `description_source="none"`; AI still runs | WARNING | Listing filtered on title/price only; higher reject rate |
| 11 | Listing removed from Facebook | "no longer available" text detected | No retry | Mark `status="unavailable"`; skip AI call | INFO | Expected; one listing skipped |
| 12 | OpenAI `AuthenticationError` | Exception type check | No retry | Abort entire filtering stage; Telegram alert | CRITICAL | No listings notified this run |
| 13 | OpenAI `RateLimitError` | Exception type check | 3 retries: 2s, 4s, 8s (honour `retry-after` header) | Pause 60s; if still failing: mark listing filter pending | WARNING | Brief delay; listing processed on next run |
| 14 | OpenAI 5xx server error | `APIStatusError` status ≥ 500 | 1 retry after 5s | Mark `filter_decision=NULL`; continue | ERROR | Listing unfiltered this run |
| 15 | OpenAI JSON parse failure | `json.JSONDecodeError` | 1 retry with explicit JSON instruction | Treat as REJECT; `rejection_reasons=["llm_parse_error"]` | WARNING | Potential false negative |
| 16 | OpenAI `ValidationError` | Pydantic model validation | No retry | Treat as REJECT; `rejection_reasons=["llm_response_invalid"]` | WARNING | Potential false negative |
| 17 | Telegram send failure (network) | `httpx.HTTPError` | 2 retries: 3s, 6s | Mark `status="notify_failed"`; re-attempt on next run | WARNING | Listing not delivered; auto-recovered next run |
| 18 | Telegram 400 message too long | HTTP 400 response | No retry on same content | Truncate reasoning/description; resend truncated version | WARNING | User receives partial description |
| 19 | Telegram 429 rate limit | HTTP 429 response + `retry_after` field | Honour `retry_after` delay; 1 retry | If still failing: `status="notify_failed"` | WARNING | Brief delay in delivery |
| 20 | SQLite locked | `sqlite3.OperationalError: database is locked` | `busy_timeout=5000` auto-waits; then 2 manual retries at 1s | Log ERROR; listing state may be inconsistent | ERROR | State corrected on next run |
| 21 | SQLite file permission denied | `OperationalError: unable to open` | No retry | Abort at startup | CRITICAL | No run starts |
| 22 | Network unavailable | `httpx.ConnectError` or socket error | 1 retry after 10s | Abort run; log CRITICAL | CRITICAL | No run; check connectivity |

---

## 9. Logging Strategy

### 9.1 Log Levels and Triggers

| Level | Triggers |
|---|---|
| `DEBUG` | Per-listing state transitions; rate limiter delays; selector level used; token estimates; dedup cache hits |
| `INFO` | Run started/finished; CSV parse summary; new listings count; each successful scrape; each matched listing; notifications sent; run statistics |
| `WARNING` | Skipped CSV rows; scrape failures; all selector fallbacks; AI validation failures; Telegram retries |
| `ERROR` | OpenAI API failures; Telegram failures after retries; SQLite errors; unrecoverable per-listing errors |
| `CRITICAL` | Cookie expiry; DB unavailable; OpenAI auth failure; network unavailable; any unhandled exception |

### 9.2 Log Format and Example Lines

structlog configuration: JSON to file, pretty `ConsoleRenderer` to stdout.

```
# Console output (pretty):
2026-02-22 14:31:00 [info     ] run_started            csv=input/marketplace_export.csv dry_run=False run_id=a3f2b1c9
2026-02-22 14:31:00 [info     ] csv_parsed             rows=87 valid=84 skipped=3
2026-02-22 14:31:01 [info     ] new_listings           count=23 seen=61
2026-02-22 14:31:01 [debug    ] dedup_hit              listing_id=111222333444 title="Studio near Bloor"
2026-02-22 14:31:01 [info     ] pre_filter_rejected    listing_id=555666777888 price_cents=275000 cap=240000
2026-02-22 14:31:06 [info     ] scrape_success         listing_id=987654321098 selector=primary chars=412
2026-02-22 14:31:06 [debug    ] rate_limiter           seconds=5.3
2026-02-22 14:31:09 [warning  ] scrape_retry           listing_id=111333555777 attempt=1 error=TimeoutError
2026-02-22 14:31:20 [warning  ] scrape_failed          listing_id=111333555777 attempt=2 status=scrape_failed
2026-02-22 14:31:21 [info     ] filter_result          listing_id=987654321098 decision=PASS score=19
2026-02-22 14:31:22 [info     ] notification_sent      listing_id=987654321098 title="Bright 1BR Leslieville"
2026-02-22 14:32:18 [info     ] run_complete           new=23 scraped=20 matched=4 notified=4 errors=2
2026-02-22 14:32:18 [error    ] cookie_expired         run_id=a3f2b1c9
```

```json
// File output (JSON, one object per line):
{"timestamp":"2026-02-22T14:31:00.123Z","level":"info","event":"run_started","csv":"input/marketplace_export.csv","dry_run":false,"run_id":"a3f2b1c9"}
{"timestamp":"2026-02-22T14:31:06.450Z","level":"info","event":"scrape_success","listing_id":"987654321098","selector":"primary","chars":412}
```

### 9.3 Log File Configuration

```python
# utils/logging_config.py

import logging, structlog
from pathlib import Path

def configure_logging(log_dir: str, file_level: str, console_level: str) -> None:
    log_path = Path(log_dir)
    log_path.mkdir(parents=True, exist_ok=True)

    # Rotating file handler: new file per day, keep 14 days
    from logging.handlers import TimedRotatingFileHandler
    file_handler = TimedRotatingFileHandler(
        filename=log_path / "rent_finder.jsonl",
        when="midnight",
        backupCount=14,
        encoding="utf-8",
    )
    file_handler.setLevel(file_level)

    logging.basicConfig(handlers=[file_handler], level=logging.DEBUG)

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.DEBUG),
        logger_factory=structlog.stdlib.LoggerFactory(),
    )
```

Log retention: 14 daily files kept; older files deleted automatically by `TimedRotatingFileHandler`. Log files are `.jsonl` (one JSON object per line) for easy parsing by future tooling.

### 9.4 Clean Successful Run (Console)

```
INFO  run_started          csv=input/marketplace_export.csv dry_run=False
INFO  csv_parsed           rows=50 valid=50 skipped=0
INFO  new_listings         count=15 seen=35
INFO  scrape_success ×15
INFO  filter_result        decision=PASS  score=19  listing_id=AAA
INFO  filter_result        decision=PASS  score=16  listing_id=BBB
INFO  filter_result        decision=PASS  score=14  listing_id=CCC
INFO  filter_result        decision=REJECT ×12
INFO  notification_sent ×3
INFO  summary_sent
INFO  run_complete         new=15 scraped=15 matched=3 notified=3 errors=0
```

### 9.5 Partial Failure Run (Console)

```
INFO  run_started
INFO  csv_parsed           rows=50 valid=48 skipped=2
WARNING  csv_row_skipped   row=12 reason=missing_url
WARNING  csv_row_skipped   row=31 reason=no_listing_id url=https://fb.com/...
INFO  new_listings         count=10
WARNING  scrape_retry      listing_id=BBB attempt=1 error=TimeoutError
WARNING  scrape_failed     listing_id=BBB attempt=2
WARNING  no_description    listing_id=BBB filter_will_use=title_price_only
ERROR   openai_api_error   listing_id=CCC error=APIConnectionError retrying...
ERROR   openai_api_error   listing_id=CCC retry_failed mark=filter_pending
INFO  filter_result        decision=PASS score=18 listing_id=DDD
WARNING  telegram_retry    listing_id=DDD attempt=1 error=ConnectTimeout
INFO  notification_sent    listing_id=DDD attempt=2
INFO  run_complete         new=10 scraped=9 matched=1 notified=1 errors=3
```

---

## 10. Telegram Notification Design

### 10.1 Exact Message Format (MarkdownV2)

```
🏠 *New Rental Match* \- Score: {score}/24

*{TITLE_ESCAPED}*

💰 *Price:* {PRICE_ESCAPED}
📍 *Location:* {LOCATION_ESCAPED}

━━━━━━━━━━━━━━━━
📊 *Score Breakdown*
━━━━━━━━━━━━━━━━
• Neighbourhood: {nb}/3
• Laundry: {lw}/3
• Transit: {tr}/3
• Natural Light: {nl}/3
• Condition: {cn}/3
• Internet: {in}/3
• Office: {of}/3
• Move\-in: {mi}/3

📝 *Why it matched:*
_{REASONING_ESCAPED}_

🔗 [View on Facebook]({URL})

⏱ _Seen: {FIRST_SEEN_AT}_
```

**Rendered example:**

```
🏠 *New Rental Match* \- Score: 19/24

*Bright 1BR near Leslieville \- Pet Friendly\!*

💰 *Price:* \$1,950 / month
📍 *Location:* Leslieville, Toronto

━━━━━━━━━━━━━━━━
📊 *Score Breakdown*
━━━━━━━━━━━━━━━━
• Neighbourhood: 3/3
• Laundry: 3/3
• Transit: 2/3
• Natural Light: 3/3
• Condition: 2/3
• Internet: 1/3
• Office: 3/3
• Move\-in: 2/3

📝 *Why it matched:*
_South\-facing 1BR in Leslieville with in\-suite laundry and confirmed pet\-friendly policy\. Streetcar nearby\. Price well within budget\._

🔗 [View on Facebook](https://www\.facebook\.com/marketplace/item/987654321098/)

⏱ _Seen: 2026\-02\-22 14:31 EST_
```

**MarkdownV2 escaping rule:** All user-supplied text (title, price, location, reasoning) is passed through `escape_md()` which escapes all 18 special characters: `_ * [ ] ( ) ~ ` > # + - = | { } . !`. URLs in `[]()` link syntax are NOT escaped — the URL itself must be a raw valid URL.

### 10.2 Handling Missing Fields

| Field | Absent Behavior |
|---|---|
| `title` | Renders as `Untitled Listing` |
| `price_raw` | Renders as `Price not specified` |
| `location_raw` | Renders as `Location not specified` |
| `bedrooms` | Omits bedrooms line entirely |
| `bathrooms` | Omits bathrooms line entirely |
| `description` | Adds line: `⚠️ _Description unavailable — filtered on title and price only_` |
| `reasoning` | Renders as `No reasoning provided` |
| `score_breakdown` field | Renders as `-/3` for that category |

### 10.3 End-of-Run Summary Message Format

```
📊 *rent\-finder Run Summary*{DRY_RUN_BADGE}

📥 CSV rows: {rows_in_csv}
🆕 New listings: {new_listings}
🔍 Scraped OK: {scraped_ok} \({scrape_failed} failed\)
🤖 Filter: {filter_passed} passed / {filter_rejected} rejected
📨 Notified: {notified}{notify_failed_line}
❌ Errors: {errors}

⏱ _Duration: {duration_str}_
```

`{DRY_RUN_BADGE}` = `\n🧪 _DRY RUN — no notifications sent_` when `--dry-run` active.
`{notify_failed_line}` = `\n📭 _Retry pending: {notify_failed}_` when > 0 failed sends.

### 10.4 Dry-Run Mode Behavior

- `telegram.send_listing()` logs `[DRY RUN] Would notify: "{title}" score={score}` at INFO and returns `True` without making any HTTP request.
- `telegram.send_summary()` **does** send the summary even in dry-run mode (operational visibility). The summary includes the `🧪 DRY RUN` badge.
- No database writes occur (repository calls are no-ops in dry-run).
- All other pipeline steps (CSV read, scrape, OpenAI filter) execute normally.

---

## 11. Testing Strategy

### 11.1 Unit Tests

**`test_csv_reader.py`:**
- Valid CSV with all fields → correct `RawListing` list, `listing_id` extracted from URL
- CSV with `url = https://www.facebook.com/marketplace/item/123456789/` → `listing_id = "123456789"`
- CSV missing `url` column → `ValueError` with message listing the missing column
- CSV rows where URL has no numeric ID → those rows skipped; others returned
- CSV with UTF-8 BOM → parses cleanly
- Empty CSV (header only) → returns empty list, no error
- CSV with extra columns (e.g., `notes`) → stored in `extra_fields` JSON

**`test_repository.py`** (uses in-memory SQLite `:memory:`):
- `init_db()` on fresh path → all tables and indexes created
- `is_seen("X")` → False on fresh DB
- `save_listing()` then `is_seen("X")` → True
- `save_listing()` twice with same `listing_id` → second call no-ops (INSERT OR IGNORE)
- `get_unnotified_passes()` → returns only `status="notify_failed"` rows
- `update_listing_status()` → status changes in DB

**`test_rules.py`:**
- Price $2,600 with cap $2,400 → REJECT, `rejection_reasons=["price_exceeds_cap"]`
- Price $1,900 with cap $2,400 → PASS
- `price_cents=None` with any cap → PASS (always)
- `price_cents=0` → PASS (free listing; let AI evaluate)

**`test_scraper.py`** (mocked `playwright.async_api.Page`):
- `load_cookies()` with Playwright format JSON → correctly normalised
- `load_cookies()` with Cookie-Editor format (uses `expirationDate`) → correctly normalised to `expires`
- `validate_cookies()` with `c_user` missing → `CookieExpiredError`
- `detect_login_wall()` with URL containing `/login` → `CookieExpiredError`
- Selector level 1 succeeds → returns (description_text, "primary")
- Selector level 1 times out → falls to level 2
- All 6 levels fail → returns (None, "none")
- "no longer available" text detected → returns (None, "unavailable"), no selector chain

**`test_openai_filter.py`** (mocked `openai.OpenAI` client):
- Valid PASS JSON response → `FilterResult(decision="PASS", total_score=19, ...)`
- Valid REJECT JSON response → `FilterResult(decision="REJECT", rejection_reasons=[...])`
- `decision` field missing → ValidationError caught; returns REJECT with `"llm_response_invalid"`
- `total_score > 24` → ValidationError caught; returns REJECT
- `APIConnectionError` → retries once; second call succeeds → FilterResult returned
- `APIConnectionError` twice → returns fallback REJECT after retries exhausted

**`test_formatter.py`:**
- `escape_md("$1,800/month (pet-friendly!)")` → `\$1,800/month \(pet\-friendly\!\)`
- URL inside `[text](url)` → URL not escaped
- `format_listing_message()` with score 19 → contains "Score: 19/24"
- `format_listing_message()` with missing price → contains "Price not specified"
- Message with max-length fields → stays under 4096 characters

**`test_telegram.py`** (mocked `httpx.Client`):
- `send_listing()` in dry_run=True → HTTP client never called
- `send_listing()` with 429 response + `retry_after=3` → sleeps 3s, retries
- `send_listing()` with HTTP 400 "message too long" → truncates and retries
- `send_summary()` in dry_run=True → HTTP client IS called (summary always sent)

### 11.2 Integration Tests (`test_integration.py`)

All external I/O mocked (Playwright, OpenAI, Telegram, file reads). Uses temp SQLite DB.

- **Scenario 1:** 5 CSV rows, 2 already in DB → 3 processed; 2 pre-filter pass; mocked scraper returns description; mocked OpenAI returns 1 PASS, 1 REJECT; 1 Telegram send → DB has 3 new rows, 1 `status="notified"`.
- **Scenario 2:** All CSV rows already in DB → 0 scrapes, 0 OpenAI calls, summary shows 0 new.
- **Scenario 3:** Playwright raises `CookieExpiredError` on first listing → pipeline aborts cleanly, Telegram alert sent, exit code 2.
- **Scenario 4:** `--dry-run` → `repository.save_listing()` never called; `telegram.send_listing()` never calls httpx; `telegram.send_summary()` DOES call httpx.
- **Scenario 5:** OpenAI `AuthenticationError` → filtering aborted, Telegram alert sent, `filter_decision=NULL` for all scraped listings.
- **Scenario 6:** 3 listings where Telegram fails → all marked `status="notify_failed"`; on second run with same CSV (all already seen), `get_unnotified_passes()` returns 3 → re-notified.

### 11.3 Testing Playwright Without Hitting Facebook

Use `unittest.mock.patch` to mock the entire `scraper.facebook.scrape_listing` function in integration tests. In unit tests for `facebook.py` itself, mock `playwright.async_api.Page` using `pytest-mock`'s `MagicMock` with async support. Store a captured `tests/fixtures/sample_page.html` for selector testing — load it via `page.set_content(html)` in tests that need real selector evaluation.

### 11.4 Testing OpenAI Without API Credits

Use `unittest.mock.patch("openai.OpenAI")` and return `MagicMock` objects that mimic `openai.types.chat.ChatCompletion` structure. Define canned responses in `conftest.py` as fixtures. Zero live API calls in test suite.

### 11.5 Testing Telegram Without Sending Messages

Use `unittest.mock.patch("httpx.Client.post")` and return mock `httpx.Response` objects with configurable status codes. `test_telegram.py` asserts the `post()` call arguments match the expected Telegram API payload.

### 11.6 Definition of Done Per Module

| Module | Done When |
|---|---|
| `storage/` | All `test_repository.py` pass; UNIQUE constraint verified; WAL mode confirmed |
| `ingestion/` | All `test_csv_reader.py` pass; all CSV edge cases covered |
| `filtering/rules.py` | All `test_rules.py` pass; pure function (no I/O) verified |
| `scraper/` | All `test_scraper.py` pass; cookie normalisation verified against both formats |
| `filtering/openai_client.py` | All `test_openai_filter.py` pass; retry logic verified via mock call count |
| `notifications/` | All `test_formatter.py` and `test_telegram.py` pass; MarkdownV2 escaping verified |
| `main.py` (orchestrator) | All integration tests pass; `--dry-run` verified end-to-end |
| `main.py` (CLI) | `python -m rent_finder.main --help` shows all flags; `--dry-run` runs without error |

---

## 12. Build Order & Milestones

### Milestone 1 — Project Scaffold

**What gets built:**
- All directories (`rent_finder/`, `tests/`, `data/`, `input/`, `logs/`, all `__init__.py`, `tests/fixtures/`)
- `pyproject.toml` with ruff, mypy, and pytest config
- `.gitignore`, `.env.example`, `.pre-commit-config.yaml`
- `requirements.txt` (see Section 14)
- `PLAN.md` copied to project root

**Acceptance criteria:**
- `pip install -r requirements.txt && playwright install chromium` succeeds
- `ruff check rent_finder/` returns zero errors on empty packages
- `.gitignore` blocks `.env` and `data/cookies.json`

**Commit:** `feat: scaffold project structure and development environment`

---

### Milestone 2 — Configuration and Logging

**What gets built:** `config.py` (pydantic-settings), `utils/logging_config.py`, `utils/retry.py`

**Acceptance criteria:**
- `Settings()` raises `ValidationError` with helpful message when `OPENAI_API_KEY` is missing
- `Settings()` successfully loads all fields from a `.env` file
- `configure_logging()` creates a log file in the configured directory
- `retry_with_backoff` decorator retries the configured number of times with correct delays (verified via mock)

**Commit:** `feat: typed configuration loader, structured logging, and retry decorator`

---

### Milestone 3 — SQLite Data Layer

**What gets built:** `storage/schema.sql`, `storage/database.py`, `storage/repository.py`

**Acceptance criteria:**
- All `test_repository.py` tests pass
- `init_db()` is idempotent (safe to call repeatedly)
- UNIQUE constraint prevents duplicate `listing_id` inserts
- WAL mode confirmed via `PRAGMA journal_mode`

**Commit:** `feat: SQLite schema, connection factory, and repository CRUD layer`

---

### Milestone 4 — CSV Ingestion and Models

**What gets built:** `ingestion/models.py` (RawListing, EnrichedListing), `ingestion/csv_reader.py`

**Acceptance criteria:**
- All `test_csv_reader.py` tests pass
- `listing_id` correctly extracted from Facebook URL via regex
- Price `"$1,800 / month"` → `price_cents=180000`
- Price `"Contact for price"` → `price_cents=None` without exception

**Commit:** `feat: Pydantic data models, CSV reader with listing_id extraction and price parsing`

---

### Milestone 5 — Pre-Filter Rules Engine

**What gets built:** `filtering/rules.py`

**Acceptance criteria:**
- All `test_rules.py` tests pass
- `apply_pre_filters()` is pure (no I/O, no imports from scraper/storage/notifications)
- `price_cents=None` always passes through to LLM stage

**Commit:** `feat: deterministic pre-filter rules engine with price cap check`

---

### Milestone 6 — Playwright Scraper

**What gets built:** `scraper/rate_limiter.py`, `scraper/browser.py`, `scraper/facebook.py`

**Acceptance criteria:**
- All `test_scraper.py` tests pass
- Cookie normalisation handles both Playwright and Cookie-Editor formats
- `CookieExpiredError` raised correctly on login wall detection
- Selector fallback chain proceeds to next level on `TimeoutError`
- Rate limiter sleep called between every listing (verified via mock)
- Manual smoke test: one real Facebook listing description retrieved with real cookies (result not committed)

**Commit:** `feat: async Playwright scraper with cookie injection and 6-level selector fallback`

---

### Milestone 7 — OpenAI Filtering

**What gets built:** `filtering/prompt.py`, `filtering/openai_client.py`

**Acceptance criteria:**
- All `test_openai_filter.py` tests pass
- System prompt is complete and matches Section 6.1 exactly
- `FilterResult` Pydantic model validates all edge cases
- `RateLimitError` triggers retry with correct delays (verified via mock sleep assertions)
- `AuthenticationError` raises `OpenAIAuthError` without retry
- Manual smoke test: one real listing evaluated correctly with real API key

**Commit:** `feat: OpenAI GPT-4o-mini filter with structured JSON output, scoring, and retry logic`

---

### Milestone 8 — Telegram Notifier

**What gets built:** `notifications/formatter.py`, `notifications/telegram.py`

**Acceptance criteria:**
- All `test_formatter.py` and `test_telegram.py` tests pass
- All 18 MarkdownV2 special characters are correctly escaped
- Message length never exceeds 4096 characters for any input
- `send_listing()` is a no-op (returns True, no HTTP) in dry-run mode
- `send_summary()` always sends even in dry-run
- Manual smoke test: one real Telegram message delivered to personal chat

**Commit:** `feat: Telegram notifier with MarkdownV2 formatting, truncation, and retry logic`

---

### Milestone 9 — Pipeline Orchestrator and CLI

**What gets built:** `main.py` (orchestrator + click CLI), `scheduler.py`

**Acceptance criteria:**
- All `test_integration.py` Scenarios 1–6 pass
- `python -m rent_finder.main --help` shows all flags with descriptions
- `python -m rent_finder.main --dry-run --csv tests/fixtures/sample_listings.csv` runs to completion
- `--daemon` mode starts APScheduler and fires at configured cron time
- `--headed` flag launches visible browser window
- `sys.exit(2)` occurs on `CookieExpiredError`

**Commit:** `feat: pipeline orchestrator, click CLI, and APScheduler daemon mode`

---

### Milestone 10 — End-to-End Test Suite

**What gets built:** Complete `test_integration.py` with all 6 scenarios; finalize all unit test edge cases

**Acceptance criteria:**
- `pytest tests/ -v` fully green
- `pytest tests/ --cov=rent_finder --cov-report=term-missing` shows ≥ 80% coverage
- No test makes live HTTP requests (verified by disabling network in CI)
- `mypy rent_finder/` passes with zero errors

**Commit:** `test: complete integration test suite with full pipeline and error scenario coverage`

---

### Milestone 11 — Final Cleanup and Documentation

**What gets built:**
- Complete `README.md` (setup steps, cookie export procedure with screenshots, first-run checklist, cron setup, Windows Task Scheduler alternative, DB query guide)
- `--health-check` CLI command (validates DB, cookies, OpenAI key, Telegram token)
- Cookie expiry warning at pipeline start (Telegram alert if any cookie expires within 7 days)
- Graceful SIGTERM handler in daemon mode
- `git tag v1.0.0`

**Acceptance criteria:**
- A fresh clone can run the pipeline following README instructions alone (no prior knowledge needed)
- `pre-commit install && pre-commit run --all-files` passes with no blocking issues
- `python -m rent_finder.main --health-check` exits 0 with valid config, exits 1 with specific failure message for each misconfiguration
- Cookie expiry warning fires for cookies expiring within 7 days

**Commit:** `docs: complete README, health-check command, cookie expiry warning, and pre-commit hooks`
**Tag:** `v1.0.0`

---

## 13. Git Strategy

### 13.1 Branch Strategy

Solo project: **trunk-based development**. All commits go to `main`. Short-lived feature branches are created per milestone and merged via fast-forward (`git merge --ff-only`):

```
main (always stable and runnable)
  └── feat/milestone-3-sqlite    → fast-forward merge → main
  └── feat/milestone-6-scraper   → fast-forward merge → main
```

### 13.2 Commit Message Convention

Format: `<type>(<optional scope>): <short description>`

| Type | Use |
|---|---|
| `feat` | New module or functionality |
| `fix` | Bug fix |
| `test` | Adding or updating tests |
| `docs` | Documentation only |
| `refactor` | Code restructuring, no behaviour change |
| `chore` | Dependency updates, config changes |

Examples:
```
feat: scaffold project structure and development environment
feat(scraper): add login wall detection with CookieExpiredError
feat(openai): add score_breakdown validation to FilterResult model
fix(formatter): escape backtick character in MarkdownV2 escaper
test(repository): add test for concurrent write with busy_timeout
docs: add cookie export procedure with step-by-step instructions
chore: pin playwright to 1.44.0 in requirements.txt
```

### 13.3 What Gets Committed and What Never Does

**Always committed:** `rent_finder/`, `tests/`, `main.py`, `requirements.txt`, `pyproject.toml`, `.env.example`, `.gitignore`, `.pre-commit-config.yaml`, `README.md`, `PLAN.md`, `data/.gitkeep`, `input/.gitkeep`, `logs/.gitkeep`, `tests/fixtures/`

**Never committed under any circumstance:**
- `.env` — contains live API keys and bot tokens
- `data/cookies.json` — Facebook session cookie equivalent to account password
- `data/*.db`, `data/*.db-shm`, `data/*.db-wal` — personal rental history
- `input/*.csv` — may contain scraped personal data
- `logs/*.jsonl` — may contain listing URLs and titles

### 13.4 Pre-Commit Cookie Safety Hook

`.pre-commit-config.yaml`:

```yaml
repos:
  - repo: local
    hooks:
      - id: block-cookies
        name: Block accidental cookie file commit
        language: system
        entry: >-
          bash -c '
            staged=$(git diff --cached --name-only);
            if echo "$staged" | grep -qE "cookies.*\.json$" && ! echo "$staged" | grep -q "tests/fixtures/"; then
              echo "ERROR: Refusing to commit a cookies file. This is equivalent to committing your Facebook password.";
              exit 1;
            fi
          '
        always_run: true

      - id: block-env
        name: Block .env commit
        language: system
        entry: >-
          bash -c '
            if git diff --cached --name-only | grep -qE "^\.env$"; then
              echo "ERROR: Refusing to commit .env file. Use .env.example for documentation.";
              exit 1;
            fi
          '
        always_run: true

  - repo: https://github.com/Yelp/detect-secrets
    rev: v1.4.0
    hooks:
      - id: detect-secrets
        args: ['--baseline', '.secrets.baseline']
```

> **CRITICAL REMINDER:** `cookies.json` is a live Facebook session token. If it is ever accidentally committed and pushed — even to a private repository, even briefly — treat the account as fully compromised. Immediately: (1) go to Facebook Settings → Security → Active Sessions → Log out all, (2) change your Facebook password, (3) re-export fresh cookies, (4) run `git filter-repo --path cookies.json --invert-paths` to purge the file from history, (5) force-push the purged history.

---

## 14. Dependency Manifest

```
# requirements.txt
# Python 3.11+ required
# Install: pip install -r requirements.txt
# After install: playwright install chromium

# ── Data validation and configuration ────────────────────────────────────────
pydantic==2.7.1
    # Core data model validation for RawListing, EnrichedListing, FilterResult
pydantic-settings==2.3.4
    # .env file loading with type coercion; wraps pydantic BaseSettings

# ── Async browser automation ──────────────────────────────────────────────────
playwright==1.44.0
    # Chromium browser control for Facebook page scraping with cookie injection
    # Post-install required: playwright install chromium

# ── OpenAI API ────────────────────────────────────────────────────────────────
openai==1.30.5
    # Official OpenAI Python client; used for GPT-4o-mini filtering with JSON mode

# ── HTTP client (Telegram Bot API) ───────────────────────────────────────────
httpx==0.27.0
    # Sync HTTP client for Telegram Bot API; simpler than python-telegram-bot for 2 endpoints

# ── Structured logging ────────────────────────────────────────────────────────
structlog==24.2.0
    # Structured log output: JSON to file, pretty-printed to console

# ── CLI ───────────────────────────────────────────────────────────────────────
click==8.1.7
    # CLI argument parsing: --csv, --dry-run, --headed, --health-check, --daemon, --once

# ── Task scheduling (daemon mode) ────────────────────────────────────────────
APScheduler==3.10.4
    # Cron-style scheduling for --daemon mode; asyncio-compatible

# ── Retry logic ───────────────────────────────────────────────────────────────
tenacity==8.3.0
    # Exponential backoff retry decorator for OpenAI and Telegram calls

# ── Environment file loading ──────────────────────────────────────────────────
python-dotenv==1.0.1
    # Required by pydantic-settings internally for .env file parsing

# ── Testing ───────────────────────────────────────────────────────────────────
pytest==8.2.1
    # Test runner for all unit and integration tests
pytest-asyncio==0.23.7
    # Async test support; required for Playwright async tests
pytest-mock==3.14.0
    # Clean mock fixtures; used in every test file
pytest-cov==5.0.0
    # Coverage reporting; target ≥ 80% for all modules

# ── Code quality ──────────────────────────────────────────────────────────────
ruff==0.4.5
    # Linter and formatter: replaces flake8, isort, black in one tool
mypy==1.10.0
    # Static type checking; run with strict=false for practical personal project

# ── Security (pre-commit) ─────────────────────────────────────────────────────
detect-secrets==1.4.0
    # Scans staged files for accidentally committed secrets (API keys, tokens)
```

**Standard library modules used (no entry in requirements.txt):**
`sqlite3`, `csv`, `json`, `re`, `pathlib`, `asyncio`, `argparse` (replaced by click), `time`, `random`, `uuid`, `datetime`, `sys`, `logging`, `signal`

---

## 15. Future Extensibility Notes

### 15.1 Swapping CSV for Live Apify API

The `ingestion/csv_reader.py` module exposes a single function `read_csv(path: str) -> list[RawListing]`. To add Apify support, create `ingestion/apify_reader.py` with function `fetch_dataset(dataset_id: str, api_token: str) -> list[RawListing]`. Both functions return the same `list[RawListing]` — the orchestrator in `main.py` only needs a one-line change to call the Apify reader based on a new `--source apify` CLI flag or `SOURCE=apify` env var. No other module changes required.

### 15.2 Adding More Listing Sources (Kijiji, Craigslist)

The `listing_id` field in `RawListing` must be globally unique across sources. Prefix it with a source identifier: `"fb:123456789"` for Facebook, `"kijiji:98765432"` for Kijiji. The SQLite UNIQUE constraint on `listing_id` then prevents cross-source duplicates automatically. The `listings` table gains a `source TEXT NOT NULL DEFAULT 'facebook'` column via a one-line `ALTER TABLE` migration. Add a `ingestion/kijiji_reader.py` that produces `RawListing` with the correct prefix. The Playwright scraper in `facebook.py` is Facebook-specific; a separate `scraper/kijiji.py` would handle Kijiji's different DOM.

### 15.3 Web Dashboard for Browsing Matched Listings

The SQLite database is the single source of truth and is intentionally readable by any tool. A future FastAPI + Jinja2 dashboard (`dashboard/` directory, not part of this build) would connect directly to `data/rent_finder.db` as a read-only consumer. The schema was designed with dashboard queries in mind: the `status`, `filter_score`, `filter_reasoning`, `score_breakdown`, `first_seen_at`, and all listing fields are persisted. A `GET /listings?status=filter_passed&min_score=15&location=leslieville` endpoint requires only standard SQL queries against the existing schema — no data model changes needed.

---

## Critical Files Reference for Implementation

| File | Priority | Reason |
|---|---|---|
| `rent_finder/config.py` | P0 | All modules import Settings; get validation right once |
| `rent_finder/ingestion/models.py` | P0 | Shared data contract; `listing_id` extraction is error-prone |
| `rent_finder/storage/schema.sql` | P0 | The `status` enum and indexes determine pipeline recovery |
| `rent_finder/scraper/facebook.py` | P1 | Highest-risk module; Facebook DOM changes break this first |
| `rent_finder/filtering/prompt.py` | P1 | System prompt quality directly determines notification signal-to-noise |
| `rent_finder/main.py` | P1 | Orchestrator; first file any developer reads to understand the system |
| `rent_finder/notifications/formatter.py` | P2 | MarkdownV2 escaping is subtle; incorrect escaping causes silent send failures |

---

*Plan covers: 11 modules, 3 third-party API integrations (OpenAI, Telegram, Playwright/Facebook), 22 identified failure scenarios, 11 build milestones.*
