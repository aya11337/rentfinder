"""
Unit tests for rent_finder.notifications.formatter

No network calls. Tests cover MarkdownV2 escaping, message structure,
field fallbacks, and the 4096-character length limit.
"""

from __future__ import annotations

import pytest

from rent_finder.filtering.openai_client import FilterResult
from rent_finder.ingestion.models import EnrichedListing
from rent_finder.notifications.formatter import (
    _TELEGRAM_MAX_CHARS,
    escape_md,
    format_listing_message,
    format_summary_message,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_BREAKDOWN = {
    "neighbourhood": 3,
    "laundry": 2,
    "transit": 2,
    "natural_light": 3,
    "condition": 2,
    "parking": 3,
    "furnished": 2,
    "move_in_timing": 3,
}


def _result(
    decision: str = "PASS",
    score: int | None = None,
    breakdown: dict | None = None,
    reasoning: str = "Great listing near North York.",
    scam_flag: bool = False,
) -> FilterResult:
    bd = breakdown or _BREAKDOWN
    return FilterResult(
        decision=decision,
        rejection_reasons=[],
        scam_flag=scam_flag,
        total_score=score if score is not None else sum(bd.values()),
        score_breakdown=bd,
        reasoning=reasoning,
    )


def _listing(
    listing_id: str = "TEST001",
    title: str = "Bright 1BR North York",
    price_raw: str | None = "CA$1,400",
    location_raw: str | None = "North York, Ontario",
    bedrooms: str | None = "1",
    bathrooms: str | None = "1",
    description: str | None = "Walkout basement, parking included.",
    url: str | None = None,
) -> EnrichedListing:
    return EnrichedListing(
        listing_id=listing_id,
        url=url or f"https://www.facebook.com/marketplace/item/{listing_id}/",
        title=title,
        price_raw=price_raw,
        price_cents=140000,
        location_raw=location_raw,
        bedrooms=bedrooms,
        bathrooms=bathrooms,
        image_url=None,
        scraped_at=None,
        extra_fields={},
        description=description,
        description_source="primary",
    )


# ---------------------------------------------------------------------------
# escape_md
# ---------------------------------------------------------------------------

class TestEscapeMd:
    @pytest.mark.parametrize("char", list("_*[]()~`>#+\\-=|{}.!"))
    def test_each_special_char_escaped(self, char: str) -> None:
        result = escape_md(char)
        assert result == f"\\{char}"

    def test_plain_text_unchanged(self) -> None:
        assert escape_md("hello world") == "hello world"

    def test_none_returns_empty_string(self) -> None:
        assert escape_md(None) == ""

    def test_price_string_unchanged(self) -> None:
        # $ is NOT a MarkdownV2 special character — passes through unchanged.
        result = escape_md("$1,400 / month")
        assert result == "$1,400 / month"

    def test_mixed_content(self) -> None:
        result = escape_md("$1,800 (pet-friendly!)")
        assert "\\(" in result
        assert "\\)" in result
        assert "\\!" in result
        assert "\\-" in result
        assert "$1,800" in result  # $ is not special, preserved as-is

    def test_url_not_passed_through_escape(self) -> None:
        """URLs should NOT be passed through escape_md."""
        url = "https://www.facebook.com/marketplace/item/123/"
        escaped = escape_md(url)
        # Colons and slashes are NOT special — but dots and hyphens are
        assert "\\." in escaped  # dots in URL get escaped when misused


# ---------------------------------------------------------------------------
# format_listing_message
# ---------------------------------------------------------------------------

class TestFormatListingMessage:
    def test_score_in_message(self) -> None:
        msg = format_listing_message(_listing(), _result())
        assert "20/24" in msg

    def test_title_in_message(self) -> None:
        msg = format_listing_message(_listing(title="Nice Apartment"), _result())
        assert "Nice Apartment" in msg

    def test_price_in_message(self) -> None:
        msg = format_listing_message(_listing(), _result())
        assert "CA" in msg  # dollar sign is escaped but CA is not

    def test_location_in_message(self) -> None:
        msg = format_listing_message(_listing(), _result())
        assert "North York" in msg

    def test_all_eight_breakdown_categories_present(self) -> None:
        msg = format_listing_message(_listing(), _result())
        for category in ["Neighbourhood", "Laundry", "Transit", "Natural Light",
                         "Condition", "Parking", "Furnished", "Move"]:
            assert category in msg, f"{category} missing from message"

    def test_url_in_message(self) -> None:
        url = "https://www.facebook.com/marketplace/item/TEST001/"
        msg = format_listing_message(_listing(), _result())
        assert url in msg

    def test_bedrooms_shown_when_present(self) -> None:
        msg = format_listing_message(_listing(bedrooms="2"), _result())
        assert "Bedrooms" in msg
        assert "2" in msg

    def test_bathrooms_shown_when_present(self) -> None:
        msg = format_listing_message(_listing(bathrooms="1"), _result())
        assert "Bathrooms" in msg

    def test_no_bedrooms_line_when_none(self) -> None:
        msg = format_listing_message(_listing(bedrooms=None), _result())
        assert "Bedrooms" not in msg

    def test_scam_flag_shown(self) -> None:
        msg = format_listing_message(_listing(), _result(scam_flag=True))
        assert "SCAM FLAG" in msg

    def test_no_description_warning_shown(self) -> None:
        msg = format_listing_message(_listing(description=None), _result())
        assert "unavailable" in msg.lower()

    def test_description_present_no_warning(self) -> None:
        msg = format_listing_message(_listing(description="Nice unit."), _result())
        assert "unavailable" not in msg.lower()

    def test_missing_title_shows_fallback(self) -> None:
        msg = format_listing_message(_listing(title=None), _result())
        assert "Untitled Listing" in msg

    def test_missing_price_shows_fallback(self) -> None:
        msg = format_listing_message(_listing(price_raw=None), _result())
        assert "Price not specified" in msg

    def test_missing_location_shows_fallback(self) -> None:
        msg = format_listing_message(_listing(location_raw=None), _result())
        assert "Location not specified" in msg

    def test_message_within_telegram_limit(self) -> None:
        msg = format_listing_message(_listing(), _result())
        assert len(msg) <= _TELEGRAM_MAX_CHARS

    def test_very_long_reasoning_truncated(self) -> None:
        # Use model_construct to bypass the 800-char reasoning limit for this test.
        long_reasoning = "X" * 5000
        result = FilterResult.model_construct(
            decision="PASS",
            rejection_reasons=[],
            scam_flag=False,
            total_score=20,
            score_breakdown=_BREAKDOWN,
            reasoning=long_reasoning,
        )
        msg = format_listing_message(_listing(), result)
        assert len(msg) <= _TELEGRAM_MAX_CHARS

    def test_reasoning_in_italic(self) -> None:
        msg = format_listing_message(_listing(), _result(reasoning="Good location."))
        # reasoning is wrapped in _ _ for italic
        assert "_" in msg


# ---------------------------------------------------------------------------
# format_summary_message
# ---------------------------------------------------------------------------

class TestFormatSummaryMessage:
    def _summary(self, dry_run: bool = False, notify_failed: int = 0) -> str:
        return format_summary_message(
            total_rows=50,
            new_listings=15,
            scraped_ok=14,
            scrape_failed=1,
            filter_passed=3,
            filter_rejected=11,
            notified=3,
            notify_failed=notify_failed,
            errors=1,
            duration_str="2m 30s",
            dry_run=dry_run,
        )

    def test_contains_new_listings_count(self) -> None:
        assert "15" in self._summary()

    def test_contains_filter_passed_count(self) -> None:
        assert "3" in self._summary()

    def test_contains_duration(self) -> None:
        assert "2m 30s" in self._summary()

    def test_dry_run_badge_shown(self) -> None:
        msg = self._summary(dry_run=True)
        assert "DRY RUN" in msg

    def test_no_dry_run_badge_when_false(self) -> None:
        msg = self._summary(dry_run=False)
        assert "DRY RUN" not in msg

    def test_notify_failed_line_shown(self) -> None:
        msg = self._summary(notify_failed=2)
        assert "Retry pending" in msg or "retry" in msg.lower()

    def test_no_notify_failed_line_when_zero(self) -> None:
        msg = self._summary(notify_failed=0)
        assert "Retry pending" not in msg

    def test_message_within_telegram_limit(self) -> None:
        assert len(self._summary()) <= _TELEGRAM_MAX_CHARS
