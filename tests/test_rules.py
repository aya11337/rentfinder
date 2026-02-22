"""
Unit tests for rent_finder.filtering.rules

apply_pre_filters() is a pure function — no I/O, no DB, no network.
All tests construct minimal RawListing instances inline.
"""

from __future__ import annotations

import pytest

from rent_finder.filtering.rules import apply_pre_filters
from rent_finder.ingestion.models import RawListing

MAX_RENT = 1600  # CAD — matches user criteria and config default


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _listing(
    listing_id: str = "TEST001",
    title: str = "Apartment for rent",
    price_cents: int | None = 150000,  # $1,500 — within cap
    location_raw: str | None = "Toronto, Ontario",
    extra_fields: dict | None = None,
) -> RawListing:
    return RawListing(
        listing_id=listing_id,
        url=f"https://www.facebook.com/marketplace/item/{listing_id}/",
        title=title,
        price_cents=price_cents,
        location_raw=location_raw,
        extra_fields=extra_fields or {},
    )


def _apply(listing: RawListing, max_rent: int = MAX_RENT) -> tuple[bool, list[str]]:
    return apply_pre_filters(listing, max_rent_cad=max_rent)


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------

class TestPassingListings:
    def test_valid_toronto_listing_passes(self) -> None:
        passes, reasons = _apply(_listing())
        assert passes is True
        assert reasons == []

    def test_north_york_location_passes(self) -> None:
        passes, _ = _apply(_listing(location_raw="North York, Ontario"))
        assert passes is True

    def test_markham_location_passes(self) -> None:
        passes, _ = _apply(_listing(location_raw="Markham, Ontario"))
        assert passes is True

    def test_downtown_toronto_passes(self) -> None:
        passes, _ = _apply(_listing(location_raw="Downtown Toronto, Ontario"))
        assert passes is True

    def test_no_location_passes(self) -> None:
        passes, _ = _apply(_listing(location_raw=None))
        assert passes is True

    def test_unknown_price_passes(self) -> None:
        """Listings with no parseable price should pass through to LLM."""
        passes, _ = _apply(_listing(price_cents=None))
        assert passes is True

    def test_price_exactly_at_cap_passes(self) -> None:
        passes, _ = _apply(_listing(price_cents=MAX_RENT * 100))
        assert passes is True

    def test_generic_toronto_location_passes(self) -> None:
        passes, _ = _apply(_listing(location_raw="Toronto, ON, Canada"))
        assert passes is True


# ---------------------------------------------------------------------------
# Rule 1: Sold / hidden
# ---------------------------------------------------------------------------

class TestSoldHiddenRules:
    def test_sold_listing_rejected(self) -> None:
        passes, reasons = _apply(_listing(extra_fields={"is_sold": True}))
        assert passes is False
        assert "listing_already_sold" in reasons

    def test_hidden_listing_rejected(self) -> None:
        passes, reasons = _apply(_listing(extra_fields={"is_hidden": True}))
        assert passes is False
        assert "listing_hidden_or_removed" in reasons

    def test_both_sold_and_hidden(self) -> None:
        passes, reasons = _apply(_listing(extra_fields={"is_sold": True, "is_hidden": True}))
        assert passes is False
        assert "listing_already_sold" in reasons
        assert "listing_hidden_or_removed" in reasons

    def test_is_live_true_does_not_affect_result(self) -> None:
        passes, _ = _apply(_listing(extra_fields={"is_live": True, "is_sold": False}))
        assert passes is True

    def test_is_sold_false_not_rejected(self) -> None:
        passes, _ = _apply(_listing(extra_fields={"is_sold": False}))
        assert passes is True


# ---------------------------------------------------------------------------
# Rule 2: Price cap
# ---------------------------------------------------------------------------

class TestPriceCap:
    def test_price_below_cap_passes(self) -> None:
        passes, _ = _apply(_listing(price_cents=120000))  # $1,200
        assert passes is True

    def test_price_above_cap_rejected(self) -> None:
        passes, reasons = _apply(_listing(price_cents=170000))  # $1,700
        assert passes is False
        assert any("price_exceeds_cap" in r for r in reasons)

    def test_rejection_reason_contains_amounts(self) -> None:
        passes, reasons = _apply(_listing(price_cents=200000))  # $2,000
        assert passes is False
        reason = next(r for r in reasons if "price_exceeds_cap" in r)
        assert "2000" in reason
        assert "1600" in reason

    def test_price_exactly_at_cap_not_rejected(self) -> None:
        passes, _ = _apply(_listing(price_cents=160000))  # exactly $1,600
        assert passes is True

    def test_none_price_never_rejected(self) -> None:
        passes, reasons = _apply(_listing(price_cents=None))
        assert passes is True
        assert not any("price" in r for r in reasons)

    def test_custom_cap_respected(self) -> None:
        listing = _listing(price_cents=140000)  # $1,400
        passes_1600, _ = _apply(listing, max_rent=1600)  # $1,400 < $1,600 → PASS
        passes_1300, _ = _apply(listing, max_rent=1300)  # $1,400 > $1,300 → FAIL
        assert passes_1600 is True
        assert passes_1300 is False


# ---------------------------------------------------------------------------
# Rule 3: Excluded locations
# ---------------------------------------------------------------------------

class TestExcludedLocations:
    @pytest.mark.parametrize("location", [
        "Brampton, Ontario",
        "Mississauga, Ontario",
        "Hamilton, Ontario",
        "Oshawa, Ontario",
        "Ajax, Ontario",
        "Whitby, Ontario",
        "Pickering, Ontario",
        "Oakville, Ontario",
        "Burlington, Ontario",
        "Vaughan, Ontario",
        "Barrie, Ontario",
    ])
    def test_excluded_cities_rejected(self, location: str) -> None:
        passes, reasons = _apply(_listing(location_raw=location))
        assert passes is False
        assert any("location_excluded" in r for r in reasons)

    def test_brampton_case_insensitive(self) -> None:
        passes, _ = _apply(_listing(location_raw="BRAMPTON, ONTARIO"))
        assert passes is False

    def test_partial_match_excluded(self) -> None:
        """'City of Brampton' should also be caught."""
        passes, _ = _apply(_listing(location_raw="City of Brampton, ON"))
        assert passes is False

    def test_location_reason_includes_city_name(self) -> None:
        passes, reasons = _apply(_listing(location_raw="Mississauga, Ontario"))
        assert passes is False
        assert any("mississauga" in r for r in reasons)

    def test_scarborough_is_not_excluded(self) -> None:
        """Scarborough is borderline — pass to LLM for scoring."""
        passes, _ = _apply(_listing(location_raw="Scarborough, Ontario"))
        assert passes is True

    def test_etobicoke_is_not_excluded(self) -> None:
        passes, _ = _apply(_listing(location_raw="Etobicoke, Ontario"))
        assert passes is True


# ---------------------------------------------------------------------------
# Rule 4: Shared accommodation title
# ---------------------------------------------------------------------------

class TestSharedAccommodationTitle:
    @pytest.mark.parametrize("title", [
        "Private room for rent",
        "private room near downtown",
        "PRIVATE ROOM available",
        "Room for rent in shared house",
        "Roommate needed for 2BR",
        "Roommates wanted downtown",
        "Room for rent - utilities included",
        "Rent a room near Yonge",
        "Accommodation available for couple",
        "Accommodation for 2 male students",
        "Shared accommodation downtown Toronto",
    ])
    def test_shared_titles_rejected(self, title: str) -> None:
        passes, reasons = _apply(_listing(title=title))
        assert passes is False, f"Expected {title!r} to be rejected"
        assert "shared_accommodation_title" in reasons

    @pytest.mark.parametrize("title", [
        "Apartment for rent",
        "1 bedroom basement apartment",
        "Bright 1BR near North York",
        "Studio apartment available April 1",
        "Furnished 1 bedroom condo downtown",
        "Bachelor apartment for rent",
        "Cozy unit near Markham",
        "2 bedroom walkout basement",
    ])
    def test_valid_unit_titles_pass(self, title: str) -> None:
        passes, _ = _apply(_listing(title=title))
        assert passes is True, f"Expected {title!r} to pass"


# ---------------------------------------------------------------------------
# Multiple rules firing simultaneously
# ---------------------------------------------------------------------------

class TestMultipleRulesFiring:
    def test_price_and_location_both_rejected(self) -> None:
        listing = _listing(price_cents=200000, location_raw="Brampton, Ontario")
        passes, reasons = _apply(listing)
        assert passes is False
        assert any("price_exceeds_cap" in r for r in reasons)
        assert any("location_excluded" in r for r in reasons)

    def test_sold_and_shared_both_rejected(self) -> None:
        listing = _listing(
            title="Private room for rent",
            extra_fields={"is_sold": True},
        )
        passes, reasons = _apply(listing)
        assert passes is False
        assert "listing_already_sold" in reasons
        assert "shared_accommodation_title" in reasons

    def test_all_four_rules_fired(self) -> None:
        listing = _listing(
            title="Private room for rent",
            price_cents=250000,
            location_raw="Brampton, Ontario",
            extra_fields={"is_sold": True, "is_hidden": True},
        )
        passes, reasons = _apply(listing)
        assert passes is False
        assert len(reasons) >= 4

    def test_return_type_always_tuple(self) -> None:
        result = _apply(_listing())
        assert isinstance(result, tuple)
        assert isinstance(result[0], bool)
        assert isinstance(result[1], list)
