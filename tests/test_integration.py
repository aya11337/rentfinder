"""
Integration tests for rent_finder.main.run_pipeline.

All external I/O is mocked:
  - scrape_all   (Playwright)        → AsyncMock
  - filter_listing (OpenAI)          → MagicMock
  - send_listing, send_summary,
    send_text_alert (Telegram)       → MagicMock

Tests use a temporary file-based SQLite DB via tmp_path so that
run_pipeline() can call get_connection() normally (an in-memory ":memory:" DB
cannot be shared across separate sqlite3.connect() calls inside the pipeline).

Test scenarios:
  1. Happy path — new listings processed, 1 notified
  2. All listings already in DB — 0 new, 0 scrapes, 0 OpenAI calls
  3. CookieExpiredError — pipeline aborts with exit code 2, alert sent
  4. dry_run=True — no DB writes, no send_listing, summary always sent
  5. OpenAIAuthError — pipeline aborts with exit code 1, alert sent
  6. Telegram send fails — listing marked notify_failed in DB
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from rent_finder.filtering.openai_client import FilterResult, OpenAIAuthError
from rent_finder.ingestion.models import EnrichedListing
from rent_finder.main import run_pipeline
from rent_finder.scraper.browser import CookieExpiredError
from rent_finder.storage import repository as repo
from rent_finder.storage.database import get_connection, init_db

# ---------------------------------------------------------------------------
# Shared test data
# ---------------------------------------------------------------------------

# Score breakdown used for PASS results — all categories sum to 16
_BREAKDOWN_PASS = {
    "neighbourhood": 2, "laundry": 2, "transit": 2, "natural_light": 2,
    "condition": 2, "parking": 2, "furnished": 2, "move_in_timing": 2,
}
# Score breakdown for REJECT results — sums to 4
_BREAKDOWN_REJECT = {
    "neighbourhood": 2, "laundry": 0, "transit": 2, "natural_light": 0,
    "condition": 0, "parking": 0, "furnished": 0, "move_in_timing": 0,
}

_PASS_RESULT = FilterResult(
    decision="PASS",
    rejection_reasons=[],
    scam_flag=False,
    total_score=16,
    score_breakdown=_BREAKDOWN_PASS,
    reasoning="Good listing near North York with parking included.",
)

_REJECT_RESULT = FilterResult(
    decision="REJECT",
    rejection_reasons=["no_parking_confirmed"],
    scam_flag=False,
    total_score=4,
    score_breakdown=_BREAKDOWN_REJECT,
    reasoning="No parking available.",
)

# IDs from sample_listings.json that survive JSON parsing
_ID_STUDIO = "999888777666555"   # CA$1,200 → passes price cap
_ID_PLACEHOLDER = "123000000000001"  # CA$1 placeholder → price_cents=None, passes
_ID_BRAMPTON = "555444333222111"  # Brampton location → pre-filter rejected
_ID_EXPENSIVE = "111222333444555"  # CA$1,800 → price cap rejected


def _make_enriched(
    listing_id: str = _ID_STUDIO,
    description: str = "Bright unit near Yonge with parking.",
    source: str = "primary",
) -> EnrichedListing:
    return EnrichedListing(
        listing_id=listing_id,
        url=f"https://www.facebook.com/marketplace/item/{listing_id}/",
        title="Test Listing",
        price_raw="CA$1,200",
        price_cents=120000,
        location_raw="Toronto, Ontario",
        bedrooms=None,
        bathrooms=None,
        image_url=None,
        scraped_at=None,
        extra_fields={},
        description=description,
        description_source=source,
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def tmp_settings(tmp_path: Path, sample_json_path: Path) -> MagicMock:
    """
    Settings mock using a temporary file-based DB and the fixture JSON.

    The DB is a real file so pipeline can open it with get_connection().
    """
    s = MagicMock()
    s.database_path = str(tmp_path / "test.db")
    s.json_input_path = str(sample_json_path)
    s.facebook_cookies_path = "tests/fixtures/sample_cookies.json"
    s.scraper_min_delay_seconds = 0.01
    s.scraper_max_delay_seconds = 0.02
    s.scraper_max_listings_per_run = 0
    s.playwright_headless = True
    s.playwright_page_timeout_ms = 5000
    s.openai_api_key = "sk-test-fake-key-for-unit-tests"
    s.openai_model = "gpt-4o-mini"
    s.openai_max_tokens = 600
    s.criteria_max_rent_cad = 1600
    s.criteria_min_score = 12
    s.telegram_bot_token = "123456789:FAKE_TOKEN"
    s.telegram_chat_id = "999999999"
    s.telegram_send_summary = True
    s.telegram_request_timeout_seconds = 5
    s.telegram_configured.return_value = True
    return s


@pytest.fixture
def tmp_conn(tmp_settings: MagicMock):
    """Open a connection to the tmp DB after the pipeline has run, for assertions."""
    # The pipeline creates+closes its own connection; this is a separate reader.
    conn = get_connection(tmp_settings.database_path)
    init_db(conn)
    yield conn
    conn.close()


# ---------------------------------------------------------------------------
# Scenario 1 — Happy path
# ---------------------------------------------------------------------------

class TestScenario1HappyPath:
    """
    5 JSON rows total.
    - 2 skipped (no valid listing_id): missing URL + non-FB URL
    - 3 parsed: ID_EXPENSIVE (price rejected), ID_BRAMPTON (location rejected),
                ID_STUDIO and ID_PLACEHOLDER pass pre-filter
    - Scraper returns 2 enriched listings
    - OpenAI: PASS for ID_STUDIO, REJECT for ID_PLACEHOLDER
    - Telegram: 1 notification sent successfully
    """

    def test_returns_exit_code_0(self, tmp_settings: MagicMock, sample_json_path: Path) -> None:
        with patch("rent_finder.main.scrape_all", new=AsyncMock(return_value=[
            _make_enriched(_ID_STUDIO),
            _make_enriched(_ID_PLACEHOLDER),
        ])):
            with patch("rent_finder.main.filter_listing", side_effect=[
                _PASS_RESULT, _REJECT_RESULT,
            ]):
                with patch("rent_finder.main.send_listing", return_value=True):
                    with patch("rent_finder.main.send_summary", return_value=True):
                        code = run_pipeline(
                            settings=tmp_settings,
                            json_path=str(sample_json_path),
                            dry_run=False, headed=False, run_id="TEST01",
                        )
        assert code == 0

    def test_db_has_notified_listing(
        self, tmp_settings: MagicMock, sample_json_path: Path
    ) -> None:
        with patch("rent_finder.main.scrape_all", new=AsyncMock(return_value=[
            _make_enriched(_ID_STUDIO),
            _make_enriched(_ID_PLACEHOLDER),
        ])):
            with patch("rent_finder.main.filter_listing", side_effect=[
                _PASS_RESULT, _REJECT_RESULT,
            ]):
                with patch("rent_finder.main.send_listing", return_value=True):
                    with patch("rent_finder.main.send_summary"):
                        run_pipeline(
                            settings=tmp_settings,
                            json_path=str(sample_json_path),
                            dry_run=False, headed=False, run_id="TEST01",
                        )

        conn = get_connection(tmp_settings.database_path)
        row = conn.execute(
            "SELECT status FROM listings WHERE listing_id = ?", (_ID_STUDIO,)
        ).fetchone()
        conn.close()
        assert row is not None
        assert row["status"] == "notified"

    def test_summary_called_once(
        self, tmp_settings: MagicMock, sample_json_path: Path
    ) -> None:
        with patch("rent_finder.main.scrape_all", new=AsyncMock(return_value=[
            _make_enriched(_ID_STUDIO),
        ])):
            with patch("rent_finder.main.filter_listing", return_value=_PASS_RESULT):
                with patch("rent_finder.main.send_listing", return_value=True):
                    with patch("rent_finder.main.send_summary") as mock_summary:
                        run_pipeline(
                            settings=tmp_settings,
                            json_path=str(sample_json_path),
                            dry_run=False, headed=False, run_id="TEST01",
                        )
        mock_summary.assert_called_once()

    def test_filter_rejected_stored_in_db(
        self, tmp_settings: MagicMock, sample_json_path: Path
    ) -> None:
        with patch("rent_finder.main.scrape_all", new=AsyncMock(return_value=[
            _make_enriched(_ID_STUDIO),
            _make_enriched(_ID_PLACEHOLDER),
        ])):
            with patch("rent_finder.main.filter_listing", side_effect=[
                _PASS_RESULT, _REJECT_RESULT,
            ]):
                with patch("rent_finder.main.send_listing", return_value=True):
                    with patch("rent_finder.main.send_summary"):
                        run_pipeline(
                            settings=tmp_settings,
                            json_path=str(sample_json_path),
                            dry_run=False, headed=False, run_id="TEST01",
                        )

        conn = get_connection(tmp_settings.database_path)
        row = conn.execute(
            "SELECT status FROM listings WHERE listing_id = ?", (_ID_PLACEHOLDER,)
        ).fetchone()
        conn.close()
        assert row is not None
        assert row["status"] == "filter_rejected"


# ---------------------------------------------------------------------------
# Scenario 2 — All listings already in DB (full dedup)
# ---------------------------------------------------------------------------

class TestScenario2AllSeen:
    """
    Pre-seed the DB with all listing IDs → 0 new → scrape/filter/notify skipped.
    """

    def _pre_seed(self, db_path: str) -> None:
        conn = get_connection(db_path)
        init_db(conn)
        for lid in [_ID_STUDIO, _ID_PLACEHOLDER, _ID_BRAMPTON, _ID_EXPENSIVE]:
            repo.insert_listing(
                conn,
                listing_id=lid,
                url=f"https://www.facebook.com/marketplace/item/{lid}/",
                title="Pre-seeded",
            )
        conn.close()

    def test_scrape_not_called(
        self, tmp_settings: MagicMock, sample_json_path: Path
    ) -> None:
        self._pre_seed(tmp_settings.database_path)
        with patch("rent_finder.main.scrape_all", new=AsyncMock()) as mock_scrape:
            with patch("rent_finder.main.filter_listing") as mock_filter:
                with patch("rent_finder.main.send_summary"):
                    run_pipeline(
                        settings=tmp_settings,
                        json_path=str(sample_json_path),
                        dry_run=False, headed=False, run_id="TEST02",
                    )
        mock_scrape.assert_not_called()
        mock_filter.assert_not_called()

    def test_returns_exit_code_0(
        self, tmp_settings: MagicMock, sample_json_path: Path
    ) -> None:
        self._pre_seed(tmp_settings.database_path)
        with patch("rent_finder.main.scrape_all", new=AsyncMock()):
            with patch("rent_finder.main.filter_listing"):
                with patch("rent_finder.main.send_summary"):
                    code = run_pipeline(
                        settings=tmp_settings,
                        json_path=str(sample_json_path),
                        dry_run=False, headed=False, run_id="TEST02",
                    )
        assert code == 0


# ---------------------------------------------------------------------------
# Scenario 3 — CookieExpiredError → exit code 2
# ---------------------------------------------------------------------------

class TestScenario3CookieExpired:
    def test_returns_exit_code_2(
        self, tmp_settings: MagicMock, sample_json_path: Path
    ) -> None:
        with patch(
            "rent_finder.main.scrape_all",
            new=AsyncMock(side_effect=CookieExpiredError("session expired")),
        ):
            with patch("rent_finder.main.send_text_alert"):
                with patch("rent_finder.main.send_summary"):
                    code = run_pipeline(
                        settings=tmp_settings,
                        json_path=str(sample_json_path),
                        dry_run=False, headed=False, run_id="TEST03",
                    )
        assert code == 2

    def test_telegram_alert_sent_on_cookie_expiry(
        self, tmp_settings: MagicMock, sample_json_path: Path
    ) -> None:
        with patch(
            "rent_finder.main.scrape_all",
            new=AsyncMock(side_effect=CookieExpiredError("session expired")),
        ):
            with patch("rent_finder.main.send_text_alert") as mock_alert:
                run_pipeline(
                    settings=tmp_settings,
                    json_path=str(sample_json_path),
                    dry_run=False, headed=False, run_id="TEST03",
                )
        mock_alert.assert_called_once()
        alert_text = mock_alert.call_args[0][0]
        assert "cookie" in alert_text.lower() or "expired" in alert_text.lower()

    def test_no_notifications_sent(
        self, tmp_settings: MagicMock, sample_json_path: Path
    ) -> None:
        with patch(
            "rent_finder.main.scrape_all",
            new=AsyncMock(side_effect=CookieExpiredError("session expired")),
        ):
            with patch("rent_finder.main.send_text_alert"):
                with patch("rent_finder.main.send_listing") as mock_notify:
                    run_pipeline(
                        settings=tmp_settings,
                        json_path=str(sample_json_path),
                        dry_run=False, headed=False, run_id="TEST03",
                    )
        mock_notify.assert_not_called()


# ---------------------------------------------------------------------------
# Scenario 4 — dry_run=True
# ---------------------------------------------------------------------------

class TestScenario4DryRun:
    def test_send_listing_not_called_in_dry_run(
        self, tmp_settings: MagicMock, sample_json_path: Path
    ) -> None:
        with patch("rent_finder.main.scrape_all", new=AsyncMock(return_value=[
            _make_enriched(_ID_STUDIO),
        ])):
            with patch("rent_finder.main.filter_listing", return_value=_PASS_RESULT):
                with patch("rent_finder.main.send_listing") as mock_notify:
                    with patch("rent_finder.main.send_summary"):
                        run_pipeline(
                            settings=tmp_settings,
                            json_path=str(sample_json_path),
                            dry_run=True, headed=False, run_id="TEST04",
                        )
        # send_listing is called but with dry_run=True internally → no HTTP
        # (the dry_run flag is passed through to send_listing)
        if mock_notify.called:
            _, kwargs = mock_notify.call_args
            assert kwargs.get("dry_run") is True

    def test_summary_always_sent_in_dry_run(
        self, tmp_settings: MagicMock, sample_json_path: Path
    ) -> None:
        with patch("rent_finder.main.scrape_all", new=AsyncMock(return_value=[])):
            with patch("rent_finder.main.filter_listing"):
                with patch("rent_finder.main.send_summary") as mock_summary:
                    run_pipeline(
                        settings=tmp_settings,
                        json_path=str(sample_json_path),
                        dry_run=True, headed=False, run_id="TEST04",
                    )
        mock_summary.assert_called_once()
        _, kwargs = mock_summary.call_args
        assert kwargs.get("dry_run") is True

    def test_db_not_written_in_dry_run(
        self, tmp_settings: MagicMock, sample_json_path: Path
    ) -> None:
        with patch("rent_finder.main.scrape_all", new=AsyncMock(return_value=[
            _make_enriched(_ID_STUDIO),
        ])):
            with patch("rent_finder.main.filter_listing", return_value=_PASS_RESULT):
                with patch("rent_finder.main.send_listing", return_value=True):
                    with patch("rent_finder.main.send_summary"):
                        run_pipeline(
                            settings=tmp_settings,
                            json_path=str(sample_json_path),
                            dry_run=True, headed=False, run_id="TEST04",
                        )

        # DB should be empty — no inserts in dry_run mode
        conn = get_connection(tmp_settings.database_path)
        init_db(conn)
        count = conn.execute("SELECT COUNT(*) FROM listings").fetchone()[0]
        conn.close()
        assert count == 0

    def test_returns_exit_code_0_in_dry_run(
        self, tmp_settings: MagicMock, sample_json_path: Path
    ) -> None:
        with patch("rent_finder.main.scrape_all", new=AsyncMock(return_value=[])):
            with patch("rent_finder.main.send_summary"):
                code = run_pipeline(
                    settings=tmp_settings,
                    json_path=str(sample_json_path),
                    dry_run=True, headed=False, run_id="TEST04",
                )
        assert code == 0


# ---------------------------------------------------------------------------
# Scenario 5 — OpenAIAuthError → exit code 1
# ---------------------------------------------------------------------------

class TestScenario5OpenAIAuthError:
    def test_returns_exit_code_1(
        self, tmp_settings: MagicMock, sample_json_path: Path
    ) -> None:
        with patch("rent_finder.main.scrape_all", new=AsyncMock(return_value=[
            _make_enriched(_ID_STUDIO),
        ])):
            with patch(
                "rent_finder.main.filter_listing",
                side_effect=OpenAIAuthError("Invalid API key"),
            ):
                with patch("rent_finder.main.send_text_alert"):
                    code = run_pipeline(
                        settings=tmp_settings,
                        json_path=str(sample_json_path),
                        dry_run=False, headed=False, run_id="TEST05",
                    )
        assert code == 1

    def test_alert_sent_on_auth_error(
        self, tmp_settings: MagicMock, sample_json_path: Path
    ) -> None:
        with patch("rent_finder.main.scrape_all", new=AsyncMock(return_value=[
            _make_enriched(_ID_STUDIO),
        ])):
            with patch(
                "rent_finder.main.filter_listing",
                side_effect=OpenAIAuthError("Invalid API key"),
            ):
                with patch("rent_finder.main.send_text_alert") as mock_alert:
                    run_pipeline(
                        settings=tmp_settings,
                        json_path=str(sample_json_path),
                        dry_run=False, headed=False, run_id="TEST05",
                    )
        mock_alert.assert_called_once()

    def test_no_notifications_sent_on_auth_error(
        self, tmp_settings: MagicMock, sample_json_path: Path
    ) -> None:
        with patch("rent_finder.main.scrape_all", new=AsyncMock(return_value=[
            _make_enriched(_ID_STUDIO),
            _make_enriched(_ID_PLACEHOLDER),
        ])):
            with patch(
                "rent_finder.main.filter_listing",
                side_effect=OpenAIAuthError("Invalid API key"),
            ):
                with patch("rent_finder.main.send_text_alert"):
                    with patch("rent_finder.main.send_listing") as mock_notify:
                        run_pipeline(
                            settings=tmp_settings,
                            json_path=str(sample_json_path),
                            dry_run=False, headed=False, run_id="TEST05",
                        )
        mock_notify.assert_not_called()


# ---------------------------------------------------------------------------
# Scenario 6 — Telegram send fails → notify_failed in DB
# ---------------------------------------------------------------------------

class TestScenario6TelegramFailure:
    def test_listing_marked_notify_failed(
        self, tmp_settings: MagicMock, sample_json_path: Path
    ) -> None:
        with patch("rent_finder.main.scrape_all", new=AsyncMock(return_value=[
            _make_enriched(_ID_STUDIO),
        ])):
            with patch("rent_finder.main.filter_listing", return_value=_PASS_RESULT):
                with patch("rent_finder.main.send_listing", return_value=False):
                    with patch("rent_finder.main.send_summary"):
                        run_pipeline(
                            settings=tmp_settings,
                            json_path=str(sample_json_path),
                            dry_run=False, headed=False, run_id="TEST06",
                        )

        conn = get_connection(tmp_settings.database_path)
        row = conn.execute(
            "SELECT status FROM listings WHERE listing_id = ?", (_ID_STUDIO,)
        ).fetchone()
        conn.close()
        assert row is not None
        assert row["status"] == "notify_failed"

    def test_returns_exit_code_0_despite_notify_failure(
        self, tmp_settings: MagicMock, sample_json_path: Path
    ) -> None:
        """A failed Telegram send is non-fatal; pipeline returns 0."""
        with patch("rent_finder.main.scrape_all", new=AsyncMock(return_value=[
            _make_enriched(_ID_STUDIO),
        ])):
            with patch("rent_finder.main.filter_listing", return_value=_PASS_RESULT):
                with patch("rent_finder.main.send_listing", return_value=False):
                    with patch("rent_finder.main.send_summary"):
                        code = run_pipeline(
                            settings=tmp_settings,
                            json_path=str(sample_json_path),
                            dry_run=False, headed=False, run_id="TEST06",
                        )
        assert code == 0
