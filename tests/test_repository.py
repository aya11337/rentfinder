"""
Unit tests for rent_finder.storage.repository

All tests use the tmp_db_conn fixture (in-memory SQLite with schema applied).
No file I/O, no network calls.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from rent_finder.storage import repository as repo

# ---------------------------------------------------------------------------
# Schema sanity
# ---------------------------------------------------------------------------

class TestSchemaInit:
    def test_tables_exist(self, tmp_db_conn: sqlite3.Connection) -> None:
        tables = {
            row[0]
            for row in tmp_db_conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table';"
            ).fetchall()
        }
        assert {"listings", "run_log", "cookie_health"}.issubset(tables)

    def test_wal_mode_enabled_on_file_db(self, tmp_path) -> None:
        # WAL mode does not apply to :memory: DBs (they report "memory").
        # This test verifies WAL is set when using a real file.
        from rent_finder.storage.database import get_connection, init_db
        db_file = str(tmp_path / "test.db")
        conn = get_connection(db_file)
        init_db(conn)
        mode = conn.execute("PRAGMA journal_mode;").fetchone()[0]
        conn.close()
        assert mode == "wal"

    def test_unique_index_on_listing_id(self, tmp_db_conn: sqlite3.Connection) -> None:
        indexes = {
            row[0]
            for row in tmp_db_conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index';"
            ).fetchall()
        }
        assert "idx_listings_listing_id" in indexes


# ---------------------------------------------------------------------------
# Deduplication
# ---------------------------------------------------------------------------

class TestDeduplication:
    def test_is_seen_false_on_empty_db(self, tmp_db_conn: sqlite3.Connection) -> None:
        assert repo.is_seen(tmp_db_conn, "999111222333444") is False

    def test_is_seen_true_after_insert(self, tmp_db_conn: sqlite3.Connection) -> None:
        repo.insert_listing(
            tmp_db_conn,
            listing_id="111222333444555",
            url="https://www.facebook.com/marketplace/item/111222333444555/",
            title="Test Listing",
        )
        assert repo.is_seen(tmp_db_conn, "111222333444555") is True

    def test_get_seen_ids_returns_all(self, tmp_db_conn: sqlite3.Connection) -> None:
        for lid in ["AAA111", "BBB222", "CCC333"]:
            repo.insert_listing(
                tmp_db_conn,
                listing_id=lid,
                url=f"https://www.facebook.com/marketplace/item/{lid}/",
                title=f"Listing {lid}",
            )
        seen = repo.get_seen_listing_ids(tmp_db_conn)
        assert seen == {"AAA111", "BBB222", "CCC333"}

    def test_get_seen_ids_empty_db(self, tmp_db_conn: sqlite3.Connection) -> None:
        assert repo.get_seen_listing_ids(tmp_db_conn) == set()


# ---------------------------------------------------------------------------
# Insert listing
# ---------------------------------------------------------------------------

class TestInsertListing:
    def test_insert_returns_true(self, tmp_db_conn: sqlite3.Connection) -> None:
        inserted = repo.insert_listing(
            tmp_db_conn,
            listing_id="123456789",
            url="https://www.facebook.com/marketplace/item/123456789/",
            title="Test",
        )
        assert inserted is True

    def test_duplicate_insert_returns_false(self, tmp_db_conn: sqlite3.Connection) -> None:
        kwargs = dict(
            listing_id="123456789",
            url="https://www.facebook.com/marketplace/item/123456789/",
            title="Test",
        )
        repo.insert_listing(tmp_db_conn, **kwargs)
        second = repo.insert_listing(tmp_db_conn, **kwargs)
        assert second is False

    def test_duplicate_insert_does_not_raise(self, tmp_db_conn: sqlite3.Connection) -> None:
        kwargs = dict(
            listing_id="DUPE001",
            url="https://www.facebook.com/marketplace/item/DUPE001/",
            title="Dupe",
        )
        repo.insert_listing(tmp_db_conn, **kwargs)
        # Should not raise any exception
        repo.insert_listing(tmp_db_conn, **kwargs)

    def test_row_count_stays_one_on_duplicate(self, tmp_db_conn: sqlite3.Connection) -> None:
        kwargs = dict(
            listing_id="DUPE002",
            url="https://www.facebook.com/marketplace/item/DUPE002/",
            title="Dupe",
        )
        repo.insert_listing(tmp_db_conn, **kwargs)
        repo.insert_listing(tmp_db_conn, **kwargs)
        count = tmp_db_conn.execute(
            "SELECT COUNT(*) FROM listings WHERE listing_id = 'DUPE002';"
        ).fetchone()[0]
        assert count == 1

    def test_all_optional_fields_stored(self, tmp_db_conn: sqlite3.Connection) -> None:
        repo.insert_listing(
            tmp_db_conn,
            listing_id="FULL001",
            url="https://www.facebook.com/marketplace/item/FULL001/",
            title="Full Listing",
            price_raw="$1,800 / month",
            price_cents=180000,
            location_raw="Leslieville Toronto",
            bedrooms="1",
            bathrooms="1",
            image_url="https://example.com/img.jpg",
            scraped_at="2026-02-22T10:00:00Z",
            extra_fields={"custom_col": "value"},
            run_id="run-uuid-001",
        )
        row = tmp_db_conn.execute(
            "SELECT * FROM listings WHERE listing_id = 'FULL001';"
        ).fetchone()
        assert row["price_cents"] == 180000
        assert row["location_raw"] == "Leslieville Toronto"
        assert json.loads(row["extra_fields"]) == {"custom_col": "value"}
        assert row["run_id"] == "run-uuid-001"
        assert row["status"] == "pending"

    def test_default_status_is_pending(self, tmp_db_conn: sqlite3.Connection) -> None:
        repo.insert_listing(
            tmp_db_conn,
            listing_id="STATUS001",
            url="https://www.facebook.com/marketplace/item/STATUS001/",
            title="Status Test",
        )
        row = tmp_db_conn.execute(
            "SELECT status FROM listings WHERE listing_id = 'STATUS001';"
        ).fetchone()
        assert row["status"] == "pending"


# ---------------------------------------------------------------------------
# Update description
# ---------------------------------------------------------------------------

class TestUpdateDescription:
    def _insert(self, conn: sqlite3.Connection, listing_id: str = "DESC001") -> None:
        repo.insert_listing(
            conn,
            listing_id=listing_id,
            url=f"https://www.facebook.com/marketplace/item/{listing_id}/",
            title="Test",
        )

    def test_description_stored(self, tmp_db_conn: sqlite3.Connection) -> None:
        self._insert(tmp_db_conn)
        repo.update_description(
            tmp_db_conn, "DESC001",
            description="Bright 1BR near Leslieville.",
            description_source="primary",
            scrape_attempts=1,
        )
        row = tmp_db_conn.execute(
            "SELECT description, description_source, status FROM listings "
            "WHERE listing_id = 'DESC001';"
        ).fetchone()
        assert row["description"] == "Bright 1BR near Leslieville."
        assert row["description_source"] == "primary"
        assert row["status"] == "scraped"

    def test_none_description_allowed(self, tmp_db_conn: sqlite3.Connection) -> None:
        self._insert(tmp_db_conn, "NODESC001")
        repo.update_description(
            tmp_db_conn, "NODESC001",
            description=None,
            description_source="none",
            scrape_attempts=2,
            scrape_error="All selectors failed",
        )
        row = tmp_db_conn.execute(
            "SELECT description, scrape_error FROM listings "
            "WHERE listing_id = 'NODESC001';"
        ).fetchone()
        assert row["description"] is None
        assert row["scrape_error"] == "All selectors failed"


# ---------------------------------------------------------------------------
# Update status
# ---------------------------------------------------------------------------

class TestUpdateStatus:
    def test_status_updated(self, tmp_db_conn: sqlite3.Connection) -> None:
        repo.insert_listing(
            tmp_db_conn,
            listing_id="STAT001",
            url="https://www.facebook.com/marketplace/item/STAT001/",
            title="Test",
        )
        repo.update_status(tmp_db_conn, "STAT001", "scrape_failed", scrape_error="Timeout")
        row = tmp_db_conn.execute(
            "SELECT status, scrape_error FROM listings WHERE listing_id = 'STAT001';"
        ).fetchone()
        assert row["status"] == "scrape_failed"
        assert row["scrape_error"] == "Timeout"

    def test_invalid_status_rejected_by_db(self, tmp_db_conn: sqlite3.Connection) -> None:
        repo.insert_listing(
            tmp_db_conn,
            listing_id="BADSTAT001",
            url="https://www.facebook.com/marketplace/item/BADSTAT001/",
            title="Test",
        )
        with pytest.raises(sqlite3.IntegrityError):
            repo.update_status(tmp_db_conn, "BADSTAT001", "invalid_status_value")


# ---------------------------------------------------------------------------
# Filter result
# ---------------------------------------------------------------------------

class TestUpdateFilterResult:
    def _insert(self, conn: sqlite3.Connection) -> None:
        repo.insert_listing(
            conn,
            listing_id="FILT001",
            url="https://www.facebook.com/marketplace/item/FILT001/",
            title="Filter Test",
        )

    def test_pass_result_stored(self, tmp_db_conn: sqlite3.Connection) -> None:
        self._insert(tmp_db_conn)
        breakdown = {
            "neighbourhood": 3, "laundry": 3, "transit": 2, "natural_light": 3,
            "condition": 2, "internet": 1, "office_suitability": 3, "move_in_timing": 2,
        }
        repo.update_filter_result(
            tmp_db_conn, "FILT001",
            decision="PASS", score=19,
            reasoning="Great listing near Lessieville.",
            score_breakdown=breakdown,
            new_status="filter_passed",
        )
        row = tmp_db_conn.execute(
            "SELECT filter_decision, filter_score, status FROM listings "
            "WHERE listing_id = 'FILT001';"
        ).fetchone()
        assert row["filter_decision"] == "PASS"
        assert row["filter_score"] == 19
        assert row["status"] == "filter_passed"

    def test_reject_result_stored(self, tmp_db_conn: sqlite3.Connection) -> None:
        self._insert(tmp_db_conn)
        repo.update_filter_result(
            tmp_db_conn, "FILT001",
            decision="REJECT", score=5,
            reasoning="Price too high.",
            score_breakdown={
                "neighbourhood": 1, "laundry": 0, "transit": 1, "natural_light": 1,
                "condition": 1, "internet": 0, "office_suitability": 1, "move_in_timing": 0,
            },
            new_status="filter_rejected",
        )
        row = tmp_db_conn.execute(
            "SELECT filter_decision, status FROM listings WHERE listing_id = 'FILT001';"
        ).fetchone()
        assert row["filter_decision"] == "REJECT"
        assert row["status"] == "filter_rejected"


# ---------------------------------------------------------------------------
# Notification tracking
# ---------------------------------------------------------------------------

class TestNotification:
    def _insert_pass(self, conn: sqlite3.Connection, lid: str = "NOTIF001") -> None:
        repo.insert_listing(
            conn,
            listing_id=lid,
            url=f"https://www.facebook.com/marketplace/item/{lid}/",
            title="Notif Test",
        )
        repo.update_filter_result(
            conn, lid,
            decision="PASS", score=18,
            reasoning="Good listing.",
            score_breakdown={k: 2 for k in [
                "neighbourhood","laundry","transit","natural_light",
                "condition","internet","office_suitability","move_in_timing",
            ]},
            new_status="filter_passed",
        )

    def test_mark_notified(self, tmp_db_conn: sqlite3.Connection) -> None:
        self._insert_pass(tmp_db_conn)
        repo.mark_notified(tmp_db_conn, "NOTIF001")
        row = tmp_db_conn.execute(
            "SELECT status, notified_at FROM listings WHERE listing_id = 'NOTIF001';"
        ).fetchone()
        assert row["status"] == "notified"
        assert row["notified_at"] is not None

    def test_mark_notify_failed(self, tmp_db_conn: sqlite3.Connection) -> None:
        self._insert_pass(tmp_db_conn, "NOTIF002")
        repo.mark_notify_failed(tmp_db_conn, "NOTIF002")
        row = tmp_db_conn.execute(
            "SELECT status FROM listings WHERE listing_id = 'NOTIF002';"
        ).fetchone()
        assert row["status"] == "notify_failed"

    def test_get_unnotified_passes_returns_failed_and_unnotified(
        self, tmp_db_conn: sqlite3.Connection
    ) -> None:
        # Insert one notify_failed and one filter_passed (not yet notified)
        self._insert_pass(tmp_db_conn, "RETRY001")
        repo.mark_notify_failed(tmp_db_conn, "RETRY001")

        self._insert_pass(tmp_db_conn, "UNNOTIF001")
        # UNNOTIF001 stays at filter_passed with no notified_at

        unnotified = repo.get_unnotified_passes(tmp_db_conn)
        ids = {r["listing_id"] for r in unnotified}
        assert "RETRY001" in ids
        assert "UNNOTIF001" in ids

    def test_get_unnotified_passes_excludes_notified(
        self, tmp_db_conn: sqlite3.Connection
    ) -> None:
        self._insert_pass(tmp_db_conn, "DONE001")
        repo.mark_notified(tmp_db_conn, "DONE001")
        unnotified = repo.get_unnotified_passes(tmp_db_conn)
        ids = {r["listing_id"] for r in unnotified}
        assert "DONE001" not in ids


# ---------------------------------------------------------------------------
# Run log
# ---------------------------------------------------------------------------

class TestRunLog:
    def test_insert_run_log(self, tmp_db_conn: sqlite3.Connection) -> None:
        repo.insert_run_log(tmp_db_conn, "run-001", "input/listings.csv", dry_run=False)
        row = tmp_db_conn.execute(
            "SELECT * FROM run_log WHERE run_id = 'run-001';"
        ).fetchone()
        assert row is not None
        assert row["csv_path"] == "input/listings.csv"
        assert row["dry_run"] == 0

    def test_update_run_log_partial(self, tmp_db_conn: sqlite3.Connection) -> None:
        repo.insert_run_log(tmp_db_conn, "run-002", "input/listings.csv", dry_run=True)
        repo.update_run_log(
            tmp_db_conn, "run-002",
            rows_in_csv=50,
            new_listings=10,
            exit_status="success",
        )
        row = tmp_db_conn.execute(
            "SELECT * FROM run_log WHERE run_id = 'run-002';"
        ).fetchone()
        assert row["rows_in_csv"] == 50
        assert row["new_listings"] == 10
        assert row["exit_status"] == "success"
        assert row["dry_run"] == 1

    def test_update_run_log_noop_on_no_kwargs(self, tmp_db_conn: sqlite3.Connection) -> None:
        repo.insert_run_log(tmp_db_conn, "run-003", "input/listings.csv", dry_run=False)
        # Should not raise
        repo.update_run_log(tmp_db_conn, "run-003")


# ---------------------------------------------------------------------------
# Cookie health
# ---------------------------------------------------------------------------

class TestCookieHealth:
    def test_insert_valid(self, tmp_db_conn: sqlite3.Connection) -> None:
        repo.insert_cookie_health(tmp_db_conn, is_valid=True, run_id="run-001")
        row = tmp_db_conn.execute(
            "SELECT is_valid, failure_reason FROM cookie_health LIMIT 1;"
        ).fetchone()
        assert row["is_valid"] == 1
        assert row["failure_reason"] is None

    def test_insert_invalid_with_reason(self, tmp_db_conn: sqlite3.Connection) -> None:
        repo.insert_cookie_health(
            tmp_db_conn, is_valid=False,
            failure_reason="login_redirect", run_id="run-002"
        )
        row = tmp_db_conn.execute(
            "SELECT is_valid, failure_reason FROM cookie_health "
            "WHERE run_id = 'run-002';"
        ).fetchone()
        assert row["is_valid"] == 0
        assert row["failure_reason"] == "login_redirect"
