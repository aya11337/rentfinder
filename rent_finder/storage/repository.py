"""
Repository layer for rent-finder SQLite operations.

All functions accept a sqlite3.Connection and return typed Python values.
No connection management here — the caller owns the connection lifecycle.

This module is intentionally isolated: it imports ONLY from storage.database
and stdlib. It never imports from ingestion/, scraper/, filtering/, or
notifications/ to keep the dependency graph acyclic.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from typing import Any

from rent_finder.utils.logging_config import get_logger

log = get_logger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    """Return the current UTC time as an ISO 8601 string."""
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Deduplication
# ---------------------------------------------------------------------------

def get_seen_listing_ids(conn: sqlite3.Connection) -> set[str]:
    """
    Return the set of all listing_ids already in the database.

    Used at pipeline start to skip CSV rows we have already processed.
    O(n) but called once per run; result fits in memory for any realistic
    personal-use dataset.
    """
    rows = conn.execute("SELECT listing_id FROM listings;").fetchall()
    ids = {row["listing_id"] for row in rows}
    log.debug("dedup_cache_loaded", count=len(ids))
    return ids


def is_seen(conn: sqlite3.Connection, listing_id: str) -> bool:
    """Return True if listing_id exists in the database."""
    row = conn.execute(
        "SELECT 1 FROM listings WHERE listing_id = ? LIMIT 1;",
        (listing_id,),
    ).fetchone()
    return row is not None


# ---------------------------------------------------------------------------
# Listing CRUD
# ---------------------------------------------------------------------------

def insert_listing(
    conn: sqlite3.Connection,
    *,
    listing_id: str,
    url: str,
    title: str,
    price_raw: str | None = None,
    price_cents: int | None = None,
    location_raw: str | None = None,
    bedrooms: str | None = None,
    bathrooms: str | None = None,
    image_url: str | None = None,
    scraped_at: str | None = None,
    extra_fields: dict[str, Any] | None = None,
    run_id: str | None = None,
) -> bool:
    """
    Insert a new listing row.

    Uses INSERT OR IGNORE so calling this twice with the same listing_id
    is safe — the second call is a no-op and returns False.

    Returns True if the row was inserted, False if it already existed.
    """
    extra_json = json.dumps(extra_fields) if extra_fields else None
    cursor = conn.execute(
        """
        INSERT OR IGNORE INTO listings (
            listing_id, url, title, price_raw, price_cents,
            location_raw, bedrooms, bathrooms, image_url,
            scraped_at, extra_fields, run_id
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
        """,
        (
            listing_id, url, title, price_raw, price_cents,
            location_raw, bedrooms, bathrooms, image_url,
            scraped_at, extra_json, run_id,
        ),
    )
    conn.commit()
    inserted = cursor.rowcount > 0
    if inserted:
        log.debug("listing_inserted", listing_id=listing_id)
    else:
        log.debug("listing_already_exists", listing_id=listing_id)
    return inserted


def update_description(
    conn: sqlite3.Connection,
    listing_id: str,
    description: str | None,
    description_source: str,
    scrape_attempts: int,
    scrape_error: str | None = None,
) -> None:
    """
    Update the description fields after a Playwright scrape attempt.

    Sets status to 'scraped' regardless of whether description is None
    (description may be None if all selectors failed but the page loaded).
    Use update_status() to set 'scrape_failed' or 'unavailable' on hard failures.
    """
    conn.execute(
        """
        UPDATE listings SET
            description = ?,
            description_source = ?,
            description_scraped_at = ?,
            scrape_attempts = ?,
            scrape_error = ?,
            status = 'scraped'
        WHERE listing_id = ?;
        """,
        (
            description,
            description_source,
            _now_iso(),
            scrape_attempts,
            scrape_error,
            listing_id,
        ),
    )
    conn.commit()
    log.debug(
        "description_updated",
        listing_id=listing_id,
        source=description_source,
        chars=len(description) if description else 0,
    )


def update_status(
    conn: sqlite3.Connection,
    listing_id: str,
    status: str,
    scrape_error: str | None = None,
    scrape_attempts: int | None = None,
) -> None:
    """
    Update only the status (and optionally scrape_error / scrape_attempts).

    Used to mark listings as 'scrape_failed', 'unavailable', 'pre_filter_rejected'.
    """
    if scrape_attempts is not None:
        conn.execute(
            """
            UPDATE listings SET status = ?, scrape_error = ?, scrape_attempts = ?
            WHERE listing_id = ?;
            """,
            (status, scrape_error, scrape_attempts, listing_id),
        )
    else:
        conn.execute(
            "UPDATE listings SET status = ?, scrape_error = ? WHERE listing_id = ?;",
            (status, scrape_error, listing_id),
        )
    conn.commit()
    log.debug("status_updated", listing_id=listing_id, status=status)


def update_filter_result(
    conn: sqlite3.Connection,
    listing_id: str,
    decision: str,
    score: int,
    reasoning: str,
    score_breakdown: dict[str, int],
    new_status: str,
) -> None:
    """
    Persist the AI filter result for a listing.

    new_status should be 'filter_passed' or 'filter_rejected'.
    """
    conn.execute(
        """
        UPDATE listings SET
            filter_decision = ?,
            filter_score = ?,
            filter_reasoning = ?,
            filter_score_breakdown = ?,
            filter_processed_at = ?,
            status = ?
        WHERE listing_id = ?;
        """,
        (
            decision,
            score,
            reasoning,
            json.dumps(score_breakdown),
            _now_iso(),
            new_status,
            listing_id,
        ),
    )
    conn.commit()
    log.debug(
        "filter_result_saved",
        listing_id=listing_id,
        decision=decision,
        score=score,
        status=new_status,
    )


def mark_notified(conn: sqlite3.Connection, listing_id: str) -> None:
    """Mark a listing as successfully notified via Telegram."""
    conn.execute(
        """
        UPDATE listings SET status = 'notified', notified_at = ?
        WHERE listing_id = ?;
        """,
        (_now_iso(), listing_id),
    )
    conn.commit()
    log.debug("listing_notified", listing_id=listing_id)


def mark_notify_failed(conn: sqlite3.Connection, listing_id: str) -> None:
    """Mark a listing where Telegram delivery failed after retries."""
    conn.execute(
        "UPDATE listings SET status = 'notify_failed' WHERE listing_id = ?;",
        (listing_id,),
    )
    conn.commit()
    log.debug("notify_failed_marked", listing_id=listing_id)


def get_unnotified_passes(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """
    Return listings that matched (filter_passed) but were never notified,
    or where Telegram delivery failed on a previous run.

    These are re-notified at the start of each run before processing new CSV rows.
    """
    rows = conn.execute(
        """
        SELECT listing_id, url, title, price_raw, location_raw,
               bedrooms, bathrooms, description, filter_score,
               filter_reasoning, filter_score_breakdown, first_seen_at
        FROM listings
        WHERE status = 'notify_failed'
           OR (status = 'filter_passed' AND notified_at IS NULL)
        ORDER BY first_seen_at ASC;
        """,
    ).fetchall()
    result = [dict(row) for row in rows]
    if result:
        log.info("unnotified_passes_found", count=len(result))
    return result


# ---------------------------------------------------------------------------
# Run log
# ---------------------------------------------------------------------------

def insert_run_log(
    conn: sqlite3.Connection,
    run_id: str,
    csv_path: str,
    dry_run: bool,
) -> None:
    """Insert the initial run_log row at pipeline start."""
    conn.execute(
        """
        INSERT INTO run_log (run_id, started_at, csv_path, dry_run)
        VALUES (?, ?, ?, ?);
        """,
        (run_id, _now_iso(), csv_path, int(dry_run)),
    )
    conn.commit()
    log.debug("run_log_created", run_id=run_id)


def update_run_log(
    conn: sqlite3.Connection,
    run_id: str,
    *,
    finished_at: str | None = None,
    rows_in_csv: int | None = None,
    new_listings: int | None = None,
    pre_filter_rejected: int | None = None,
    scraped_ok: int | None = None,
    scrape_failed: int | None = None,
    filter_passed: int | None = None,
    filter_rejected: int | None = None,
    notified: int | None = None,
    notify_failed: int | None = None,
    exit_status: str | None = None,
    error_summary: list[str] | None = None,
) -> None:
    """
    Update run_log counters.  Only non-None kwargs are written.

    Called multiple times per run — once at start, once at end.
    """
    updates: list[tuple[str, object]] = []
    if finished_at is not None:
        updates.append(("finished_at", finished_at))
    if rows_in_csv is not None:
        updates.append(("rows_in_csv", rows_in_csv))
    if new_listings is not None:
        updates.append(("new_listings", new_listings))
    if pre_filter_rejected is not None:
        updates.append(("pre_filter_rejected", pre_filter_rejected))
    if scraped_ok is not None:
        updates.append(("scraped_ok", scraped_ok))
    if scrape_failed is not None:
        updates.append(("scrape_failed", scrape_failed))
    if filter_passed is not None:
        updates.append(("filter_passed", filter_passed))
    if filter_rejected is not None:
        updates.append(("filter_rejected", filter_rejected))
    if notified is not None:
        updates.append(("notified", notified))
    if notify_failed is not None:
        updates.append(("notify_failed", notify_failed))
    if exit_status is not None:
        updates.append(("exit_status", exit_status))
    if error_summary is not None:
        updates.append(("error_summary", json.dumps(error_summary)))

    if not updates:
        return

    set_clause = ", ".join(f"{col} = ?" for col, _ in updates)
    values = [v for _, v in updates] + [run_id]
    conn.execute(
        f"UPDATE run_log SET {set_clause} WHERE run_id = ?;",  # noqa: S608
        values,
    )
    conn.commit()


def insert_cookie_health(
    conn: sqlite3.Connection,
    is_valid: bool,
    failure_reason: str | None = None,
    run_id: str | None = None,
) -> None:
    """Record a cookie health check result."""
    conn.execute(
        """
        INSERT INTO cookie_health (checked_at, is_valid, failure_reason, run_id)
        VALUES (?, ?, ?, ?);
        """,
        (_now_iso(), int(is_valid), failure_reason, run_id),
    )
    conn.commit()
    log.debug(
        "cookie_health_recorded",
        is_valid=is_valid,
        failure_reason=failure_reason,
    )
