"""
Additional unit tests for rent_finder.main helper functions and
pipeline edge cases not covered by test_integration.py.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from rent_finder.filtering.openai_client import FilterResult
from rent_finder.ingestion.models import EnrichedListing
from rent_finder.main import _format_duration, _rebuild_enriched, _rebuild_filter_result, run_pipeline
from rent_finder.storage.database import get_connection, init_db
from rent_finder.storage import repository as repo


# ---------------------------------------------------------------------------
# _format_duration
# ---------------------------------------------------------------------------

class TestFormatDuration:
    def test_seconds_only(self) -> None:
        assert _format_duration(45) == "45s"

    def test_minutes_and_seconds(self) -> None:
        assert _format_duration(75) == "1m 15s"

    def test_hours_minutes_seconds(self) -> None:
        assert _format_duration(3661) == "1h 1m 1s"

    def test_exactly_one_minute(self) -> None:
        assert _format_duration(60) == "1m 0s"

    def test_exactly_one_hour(self) -> None:
        assert _format_duration(3600) == "1h 0m 0s"

    def test_zero_seconds(self) -> None:
        assert _format_duration(0) == "0s"

    def test_two_hours(self) -> None:
        result = _format_duration(7384)  # 2h 3m 4s
        assert "h" in result
        assert "2h" in result


# ---------------------------------------------------------------------------
# _rebuild_enriched
# ---------------------------------------------------------------------------

class TestRebuildEnriched:
    def test_minimal_row(self) -> None:
        row = {
            "listing_id": "ABC123",
            "url": "https://www.facebook.com/marketplace/item/ABC123/",
            "title": "Nice Apartment",
        }
        enriched = _rebuild_enriched(row)
        assert enriched.listing_id == "ABC123"
        assert enriched.title == "Nice Apartment"
        assert enriched.description_source == "db"

    def test_none_title_becomes_empty_string(self) -> None:
        row = {
            "listing_id": "X",
            "url": "https://www.facebook.com/marketplace/item/X/",
            "title": None,
        }
        enriched = _rebuild_enriched(row)
        assert enriched.title == ""

    def test_optional_fields_carried_through(self) -> None:
        row = {
            "listing_id": "X",
            "url": "https://www.facebook.com/marketplace/item/X/",
            "title": "Test",
            "price_raw": "CA$1,200",
            "location_raw": "North York",
            "bedrooms": "1",
            "bathrooms": "1",
            "description": "Nice place.",
        }
        enriched = _rebuild_enriched(row)
        assert enriched.price_raw == "CA$1,200"
        assert enriched.location_raw == "North York"
        assert enriched.bedrooms == "1"
        assert enriched.description == "Nice place."


# ---------------------------------------------------------------------------
# _rebuild_filter_result
# ---------------------------------------------------------------------------

_BREAKDOWN = {
    "neighbourhood": 2, "laundry": 2, "transit": 2, "natural_light": 2,
    "condition": 2, "parking": 2, "furnished": 2, "move_in_timing": 2,
}


class TestRebuildFilterResult:
    def test_builds_pass_result(self) -> None:
        row = {
            "filter_score": 16,
            "filter_reasoning": "Good listing.",
            "filter_score_breakdown": json.dumps(_BREAKDOWN),
        }
        result = _rebuild_filter_result(row)
        assert result.decision == "PASS"
        assert result.total_score == 16

    def test_missing_breakdown_uses_empty_dict(self) -> None:
        row = {"filter_score": 0, "filter_reasoning": "None.", "filter_score_breakdown": None}
        result = _rebuild_filter_result(row)
        assert result.score_breakdown == {}

    def test_invalid_breakdown_json_uses_empty_dict(self) -> None:
        row = {
            "filter_score": 0,
            "filter_reasoning": "Fallback.",
            "filter_score_breakdown": "not json {{",
        }
        result = _rebuild_filter_result(row)
        assert result.score_breakdown == {}

    def test_none_score_defaults_to_zero(self) -> None:
        row = {"filter_score": None, "filter_reasoning": "ok", "filter_score_breakdown": None}
        result = _rebuild_filter_result(row)
        assert result.total_score == 0

    def test_none_reasoning_uses_fallback(self) -> None:
        row = {"filter_score": 10, "filter_reasoning": None, "filter_score_breakdown": None}
        result = _rebuild_filter_result(row)
        assert "Previously" in result.reasoning


# ---------------------------------------------------------------------------
# run_pipeline edge cases
# ---------------------------------------------------------------------------

@pytest.fixture
def tmp_settings_edge(tmp_path: Path, sample_json_path: Path) -> MagicMock:
    s = MagicMock()
    s.database_path = str(tmp_path / "edge.db")
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


class TestPipelineEdgeCases:
    def test_json_file_not_found_returns_1(
        self, tmp_settings_edge: MagicMock
    ) -> None:
        code = run_pipeline(
            settings=tmp_settings_edge,
            json_path="/nonexistent/path/listings.json",
            dry_run=False, headed=False, run_id="EDGE01",
        )
        assert code == 1

    def test_db_init_failure_returns_1(
        self, tmp_settings_edge: MagicMock, sample_json_path: Path
    ) -> None:
        with patch(
            "rent_finder.main.init_db",
            side_effect=Exception("disk full"),
        ):
            code = run_pipeline(
                settings=tmp_settings_edge,
                json_path=str(sample_json_path),
                dry_run=False, headed=False, run_id="EDGE02",
            )
        assert code == 1

    def test_scraper_max_listings_per_run_cap(
        self, tmp_settings_edge: MagicMock, sample_json_path: Path
    ) -> None:
        """When scraper_max_listings_per_run=1, only 1 listing is scraped."""
        tmp_settings_edge.scraper_max_listings_per_run = 1

        scraped_ids: list[str] = []

        async def mock_scrape(listings, **kwargs):
            scraped_ids.extend(l.listing_id for l in listings)
            return [
                EnrichedListing(
                    listing_id=listings[0].listing_id,
                    url=listings[0].url, title="T",
                    price_raw=None, price_cents=None, location_raw=None,
                    bedrooms=None, bathrooms=None, image_url=None,
                    scraped_at=None, extra_fields={},
                    description="test", description_source="primary",
                )
            ]

        with patch("rent_finder.main.scrape_all", new=mock_scrape):
            with patch("rent_finder.main.filter_listing") as mock_filter:
                mock_filter.return_value = FilterResult.model_construct(
                    decision="REJECT", rejection_reasons=["test"],
                    scam_flag=False, total_score=0,
                    score_breakdown={k: 0 for k in [
                        "neighbourhood","laundry","transit","natural_light",
                        "condition","parking","furnished","move_in_timing"
                    ]},
                    reasoning="Test.",
                )
                with patch("rent_finder.main.send_summary"):
                    run_pipeline(
                        settings=tmp_settings_edge,
                        json_path=str(sample_json_path),
                        dry_run=False, headed=False, run_id="EDGE03",
                    )

        assert len(scraped_ids) == 1

    def test_scrape_generic_error_continues(
        self, tmp_settings_edge: MagicMock, sample_json_path: Path
    ) -> None:
        """A non-CookieExpiredError scrape exception logs an error and returns 0."""
        with patch(
            "rent_finder.main.scrape_all",
            new=AsyncMock(side_effect=RuntimeError("network blip")),
        ):
            with patch("rent_finder.main.send_summary"):
                code = run_pipeline(
                    settings=tmp_settings_edge,
                    json_path=str(sample_json_path),
                    dry_run=False, headed=False, run_id="EDGE04",
                )
        assert code == 0

    def test_telegram_not_configured_skips_notify(
        self, tmp_settings_edge: MagicMock, sample_json_path: Path
    ) -> None:
        """When telegram_configured() is False, send_listing is never called."""
        tmp_settings_edge.telegram_configured.return_value = False
        _ID = "999888777666555"
        enriched = EnrichedListing(
            listing_id=_ID,
            url=f"https://www.facebook.com/marketplace/item/{_ID}/",
            title="Test", price_raw="CA$1,200", price_cents=120000,
            location_raw="Toronto, Ontario", bedrooms=None, bathrooms=None,
            image_url=None, scraped_at=None, extra_fields={},
            description="Nice place.", description_source="primary",
        )
        _pass = FilterResult.model_construct(
            decision="PASS", rejection_reasons=[], scam_flag=False,
            total_score=16, score_breakdown={k: 2 for k in [
                "neighbourhood","laundry","transit","natural_light",
                "condition","parking","furnished","move_in_timing"
            ]},
            reasoning="Good.",
        )
        with patch("rent_finder.main.scrape_all", new=AsyncMock(return_value=[enriched])):
            with patch("rent_finder.main.filter_listing", return_value=_pass):
                with patch("rent_finder.main.send_listing") as mock_notify:
                    with patch("rent_finder.main.send_summary"):
                        run_pipeline(
                            settings=tmp_settings_edge,
                            json_path=str(sample_json_path),
                            dry_run=False, headed=False, run_id="EDGE05",
                        )
        mock_notify.assert_not_called()

    def test_unavailable_listing_skipped_in_filter(
        self, tmp_settings_edge: MagicMock, sample_json_path: Path
    ) -> None:
        """Listings with description_source='unavailable' skip the AI filter."""
        _ID = "999888777666555"
        unavailable = EnrichedListing(
            listing_id=_ID,
            url=f"https://www.facebook.com/marketplace/item/{_ID}/",
            title="Removed", price_raw=None, price_cents=None,
            location_raw=None, bedrooms=None, bathrooms=None,
            image_url=None, scraped_at=None, extra_fields={},
            description=None, description_source="unavailable",
        )
        with patch("rent_finder.main.scrape_all", new=AsyncMock(return_value=[unavailable])):
            with patch("rent_finder.main.filter_listing") as mock_filter:
                with patch("rent_finder.main.send_summary"):
                    run_pipeline(
                        settings=tmp_settings_edge,
                        json_path=str(sample_json_path),
                        dry_run=False, headed=False, run_id="EDGE06",
                    )
        mock_filter.assert_not_called()

    def test_re_notification_of_notify_failed(
        self, tmp_path: Path, sample_json_path: Path
    ) -> None:
        """notify_failed listings from previous run are re-notified on next run."""
        db_path = tmp_path / "renotify.db"
        settings = MagicMock()
        settings.database_path = str(db_path)
        settings.json_input_path = str(sample_json_path)
        settings.scraper_min_delay_seconds = 0.01
        settings.scraper_max_delay_seconds = 0.02
        settings.scraper_max_listings_per_run = 0
        settings.playwright_headless = True
        settings.playwright_page_timeout_ms = 5000
        settings.openai_api_key = "sk-test-fake"
        settings.openai_model = "gpt-4o-mini"
        settings.openai_max_tokens = 600
        settings.criteria_max_rent_cad = 1600
        settings.criteria_min_score = 12
        settings.telegram_bot_token = "123:FAKE"
        settings.telegram_chat_id = "999"
        settings.telegram_send_summary = True
        settings.telegram_request_timeout_seconds = 5
        settings.telegram_configured.return_value = True

        # Pre-seed the DB with a notify_failed listing
        conn = get_connection(str(db_path))
        init_db(conn)
        _ID = "RENOTIFY_TEST_001"
        repo.insert_listing(
            conn,
            listing_id=_ID,
            url=f"https://www.facebook.com/marketplace/item/{_ID}/",
            title="Previously Failed",
        )
        repo.update_filter_result(
            conn, _ID, "PASS", 16, "Good.",
            {k: 2 for k in [
                "neighbourhood","laundry","transit","natural_light",
                "condition","parking","furnished","move_in_timing",
            ]},
            "notify_failed",
        )
        conn.close()

        # Run with all new listings already seen (only re-notification matters)
        with patch("rent_finder.main.scrape_all", new=AsyncMock(return_value=[])):
            with patch("rent_finder.main.send_listing", return_value=True) as mock_notify:
                with patch("rent_finder.main.send_summary"):
                    run_pipeline(
                        settings=settings,
                        json_path=str(sample_json_path),
                        dry_run=False, headed=False, run_id="RENOTIFY01",
                    )

        mock_notify.assert_called_once()

        # Verify DB status updated to "notified"
        conn2 = get_connection(str(db_path))
        row = conn2.execute(
            "SELECT status FROM listings WHERE listing_id = ?", (_ID,)
        ).fetchone()
        conn2.close()
        assert row["status"] == "notified"
