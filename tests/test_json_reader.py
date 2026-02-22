"""
Unit tests for rent_finder.ingestion.json_reader

All tests use the sample_listings.json fixture or minimal inline JSON.
No network calls, no file I/O beyond the fixture.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from rent_finder.ingestion import json_reader as reader
from rent_finder.ingestion.models import EnrichedListing, RawListing

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_json(tmp_path: Path, data: list[dict]) -> Path:
    """Write data as a JSON file and return its path."""
    p = tmp_path / "listings.json"
    p.write_text(json.dumps(data), encoding="utf-8")
    return p


SAMPLE_FIXTURE = Path(__file__).parent / "fixtures" / "sample_listings.json"


# ---------------------------------------------------------------------------
# File-level validation
# ---------------------------------------------------------------------------

class TestFileValidation:
    def test_file_not_found_raises(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            reader.parse_listings(tmp_path / "does_not_exist.json")

    def test_invalid_json_raises(self, tmp_path: Path) -> None:
        p = tmp_path / "bad.json"
        p.write_text("not json at all", encoding="utf-8")
        with pytest.raises(ValueError, match="Invalid JSON"):
            reader.parse_listings(p)

    def test_json_object_not_array_raises(self, tmp_path: Path) -> None:
        p = _write_json(tmp_path, {})  # type: ignore[arg-type]
        p.write_text('{"key": "value"}', encoding="utf-8")
        with pytest.raises(ValueError, match="JSON array"):
            reader.parse_listings(p)

    def test_empty_array_returns_empty_list(self, tmp_path: Path) -> None:
        p = _write_json(tmp_path, [])
        result = reader.parse_listings(p)
        assert result == []


# ---------------------------------------------------------------------------
# Listing ID extraction
# ---------------------------------------------------------------------------

class TestListingIdExtraction:
    def test_standard_facebook_url(self, tmp_path: Path) -> None:
        p = _write_json(tmp_path, [{
            "URl": "https://www.facebook.com/marketplace/item/123456789012345/?ref=search",
            "marketplace_listing_title": "Test",
            "amount": "1500.00",
            "formatted_amount": "CA$1,500",
        }])
        result = reader.parse_listings(p)
        assert len(result) == 1
        assert result[0].listing_id == "123456789012345"

    def test_url_without_item_id_is_skipped(self, tmp_path: Path) -> None:
        p = _write_json(tmp_path, [{
            "URl": "https://www.kijiji.ca/v-apartments/no-item-id",
            "marketplace_listing_title": "Test",
            "amount": "1500.00",
        }])
        result = reader.parse_listings(p)
        assert result == []

    def test_empty_url_is_skipped(self, tmp_path: Path) -> None:
        p = _write_json(tmp_path, [{
            "URl": "",
            "marketplace_listing_title": "Test",
            "amount": "1500.00",
        }])
        result = reader.parse_listings(p)
        assert result == []

    def test_missing_url_key_is_skipped(self, tmp_path: Path) -> None:
        p = _write_json(tmp_path, [{
            "marketplace_listing_title": "Test",
            "amount": "1500.00",
        }])
        result = reader.parse_listings(p)
        assert result == []


# ---------------------------------------------------------------------------
# Price parsing
# ---------------------------------------------------------------------------

class TestPriceParsing:
    def _single(self, tmp_path: Path, amount: str | None, formatted: str | None = None) -> RawListing:  # noqa: E501
        p = _write_json(tmp_path, [{
            "URl": "https://www.facebook.com/marketplace/item/111000000000001/?ref=search",
            "marketplace_listing_title": "Test",
            "amount": amount,
            "formatted_amount": formatted,
        }])
        result = reader.parse_listings(p)
        assert len(result) == 1
        return result[0]

    def test_standard_price_parsed(self, tmp_path: Path) -> None:
        listing = self._single(tmp_path, "1800.00", "CA$1,800")
        assert listing.price_cents == 180000
        assert listing.price_raw == "CA$1,800"

    def test_decimal_price_rounded(self, tmp_path: Path) -> None:
        listing = self._single(tmp_path, "1350.50", "CA$1,350")
        assert listing.price_cents == 135050

    def test_placeholder_price_one_dollar_is_none(self, tmp_path: Path) -> None:
        listing = self._single(tmp_path, "1.00", "CA$1")
        assert listing.price_cents is None

    def test_zero_price_is_none(self, tmp_path: Path) -> None:
        listing = self._single(tmp_path, "0.00", "CA$0")
        assert listing.price_cents is None

    def test_none_amount_gives_none_cents(self, tmp_path: Path) -> None:
        listing = self._single(tmp_path, None, None)
        assert listing.price_cents is None

    def test_formatted_amount_preserved_as_price_raw(self, tmp_path: Path) -> None:
        listing = self._single(tmp_path, "2400.00", "CA$2,400")
        assert listing.price_raw == "CA$2,400"


# ---------------------------------------------------------------------------
# Bed / bath parsing
# ---------------------------------------------------------------------------

class TestBedBathParsing:
    def _listing(self, tmp_path: Path, custom_title: str | None) -> RawListing:
        p = _write_json(tmp_path, [{
            "URl": "https://www.facebook.com/marketplace/item/222000000000001/?ref=search",
            "marketplace_listing_title": "Test",
            "amount": "1500.00",
            "custom_title": custom_title,
        }])
        result = reader.parse_listings(p)
        assert len(result) == 1
        return result[0]

    def test_one_bed_one_bath(self, tmp_path: Path) -> None:
        listing = self._listing(tmp_path, "1 bed \u00b7 1 bath")
        assert listing.bedrooms == "1"
        assert listing.bathrooms == "1"

    def test_plural_beds_and_baths(self, tmp_path: Path) -> None:
        listing = self._listing(tmp_path, "5 beds \u00b7 2 baths")
        assert listing.bedrooms == "5"
        assert listing.bathrooms == "2"

    def test_private_room_no_numbers(self, tmp_path: Path) -> None:
        listing = self._listing(tmp_path, "Private Room")
        assert listing.bedrooms is None
        assert listing.bathrooms is None

    def test_none_custom_title(self, tmp_path: Path) -> None:
        listing = self._listing(tmp_path, None)
        assert listing.bedrooms is None
        assert listing.bathrooms is None

    def test_beds_only_no_bath(self, tmp_path: Path) -> None:
        listing = self._listing(tmp_path, "2 beds")
        assert listing.bedrooms == "2"
        assert listing.bathrooms is None


# ---------------------------------------------------------------------------
# Field mapping and extra_fields
# ---------------------------------------------------------------------------

class TestFieldMapping:
    def test_title_mapped_correctly(self, tmp_path: Path) -> None:
        p = _write_json(tmp_path, [{
            "URl": "https://www.facebook.com/marketplace/item/333000000000001/?ref=search",
            "marketplace_listing_title": "Bright 1BR in Leslieville",
            "amount": "1800.00",
        }])
        result = reader.parse_listings(p)
        assert result[0].title == "Bright 1BR in Leslieville"

    def test_location_mapped_from_location_display_name(self, tmp_path: Path) -> None:
        p = _write_json(tmp_path, [{
            "URl": "https://www.facebook.com/marketplace/item/444000000000001/?ref=search",
            "marketplace_listing_title": "Test",
            "amount": "1500.00",
            "location_display_name": "Toronto, Ontario",
        }])
        result = reader.parse_listings(p)
        assert result[0].location_raw == "Toronto, Ontario"

    def test_image_mapped_from_image_field(self, tmp_path: Path) -> None:
        p = _write_json(tmp_path, [{
            "URl": "https://www.facebook.com/marketplace/item/555000000000001/?ref=search",
            "marketplace_listing_title": "Test",
            "amount": "1500.00",
            "image": "https://example.com/img.jpg",
        }])
        result = reader.parse_listings(p)
        assert result[0].image_url == "https://example.com/img.jpg"

    def test_scraped_at_is_always_none(self, tmp_path: Path) -> None:
        p = _write_json(tmp_path, [{
            "URl": "https://www.facebook.com/marketplace/item/666000000000001/?ref=search",
            "marketplace_listing_title": "Test",
            "amount": "1500.00",
        }])
        result = reader.parse_listings(p)
        assert result[0].scraped_at is None

    def test_unmapped_fields_go_to_extra_fields(self, tmp_path: Path) -> None:
        p = _write_json(tmp_path, [{
            "URl": "https://www.facebook.com/marketplace/item/777000000000001/?ref=search",
            "marketplace_listing_title": "Test",
            "amount": "1500.00",
            "is_live": True,
            "is_sold": False,
            "state": "ON",
            "delivery_types": ["IN_PERSON"],
        }])
        result = reader.parse_listings(p)
        ef = result[0].extra_fields
        assert ef["is_live"] is True
        assert ef["is_sold"] is False
        assert ef["state"] == "ON"
        assert ef["delivery_types"] == ["IN_PERSON"]

    def test_mapped_keys_not_in_extra_fields(self, tmp_path: Path) -> None:
        p = _write_json(tmp_path, [{
            "URl": "https://www.facebook.com/marketplace/item/888000000000001/?ref=search",
            "marketplace_listing_title": "Test",
            "formatted_amount": "CA$1,500",
            "amount": "1500.00",
            "location_display_name": "Toronto",
            "image": "https://example.com/img.jpg",
            "custom_title": "1 bed \u00b7 1 bath",
        }])
        result = reader.parse_listings(p)
        ef = result[0].extra_fields
        assert "URl" not in ef
        assert "marketplace_listing_title" not in ef
        assert "formatted_amount" not in ef
        assert "amount" not in ef
        assert "location_display_name" not in ef
        assert "image" not in ef
        assert "custom_title" not in ef


# ---------------------------------------------------------------------------
# Skipped records
# ---------------------------------------------------------------------------

class TestSkippedRecords:
    def test_missing_title_is_skipped(self, tmp_path: Path) -> None:
        p = _write_json(tmp_path, [{
            "URl": "https://www.facebook.com/marketplace/item/100000000000001/?ref=search",
            "marketplace_listing_title": "",
            "amount": "1500.00",
        }])
        result = reader.parse_listings(p)
        assert result == []

    def test_valid_records_returned_despite_skipped(self, tmp_path: Path) -> None:
        p = _write_json(tmp_path, [
            {
                "URl": "https://www.facebook.com/marketplace/item/100000000000002/?ref=search",
                "marketplace_listing_title": "Valid listing",
                "amount": "1500.00",
            },
            {"URl": "", "marketplace_listing_title": "No URL", "amount": "1500.00"},
        ])
        result = reader.parse_listings(p)
        assert len(result) == 1
        assert result[0].listing_id == "100000000000002"


# ---------------------------------------------------------------------------
# Fixture file integration
# ---------------------------------------------------------------------------

class TestFixtureFile:
    def test_sample_fixture_parses(self) -> None:
        result = reader.parse_listings(SAMPLE_FIXTURE)
        # fixture has 6 records: 4 valid, 1 missing URL, 1 non-Facebook URL
        assert len(result) == 4

    def test_fixture_first_listing_fields(self) -> None:
        result = reader.parse_listings(SAMPLE_FIXTURE)
        first = result[0]
        assert first.listing_id == "111222333444555"
        assert first.title == "Bright 1BR near Leslieville"
        assert first.price_cents == 180000
        assert first.price_raw == "CA$1,800"
        assert first.location_raw == "Toronto, Ontario"
        assert first.bedrooms == "1"
        assert first.bathrooms == "1"

    def test_fixture_placeholder_price_is_none(self) -> None:
        result = reader.parse_listings(SAMPLE_FIXTURE)
        # The CA$1 placeholder-price listing should be included but price_cents=None
        placeholder = next(r for r in result if r.listing_id == "123000000000001")
        assert placeholder.price_cents is None

    def test_fixture_no_custom_title_has_none_beds(self) -> None:
        result = reader.parse_listings(SAMPLE_FIXTURE)
        studio = next(r for r in result if r.listing_id == "999888777666555")
        assert studio.bedrooms is None
        assert studio.bathrooms is None


# ---------------------------------------------------------------------------
# RawListing model
# ---------------------------------------------------------------------------

class TestRawListingModel:
    def test_frozen_dataclass_immutable(self) -> None:
        listing = RawListing(
            listing_id="ABC",
            url="https://www.facebook.com/marketplace/item/ABC/",
            title="Test",
        )
        with pytest.raises((AttributeError, TypeError)):
            listing.title = "Changed"  # type: ignore[misc]

    def test_str_representation_with_price(self) -> None:
        listing = RawListing(
            listing_id="123",
            url="https://www.facebook.com/marketplace/item/123/",
            title="Nice flat",
            price_cents=180000,
        )
        s = str(listing)
        assert "123" in s
        assert "Nice flat" in s
        assert "$1,800" in s

    def test_str_representation_no_price(self) -> None:
        listing = RawListing(
            listing_id="456",
            url="https://www.facebook.com/marketplace/item/456/",
            title="Unknown price",
        )
        s = str(listing)
        assert "?" in s


# ---------------------------------------------------------------------------
# EnrichedListing model
# ---------------------------------------------------------------------------

class TestEnrichedListingModel:
    def _raw(self) -> RawListing:
        return RawListing(
            listing_id="ENR001",
            url="https://www.facebook.com/marketplace/item/ENR001/",
            title="Enriched Test",
            price_cents=160000,
            location_raw="Leslieville",
        )

    def test_from_raw_with_description(self) -> None:
        raw = self._raw()
        enriched = EnrichedListing.from_raw(raw, "Bright south-facing unit.", "primary")
        assert enriched.listing_id == "ENR001"
        assert enriched.description == "Bright south-facing unit."
        assert enriched.description_source == "primary"
        assert enriched.price_cents == 160000

    def test_from_raw_with_none_description(self) -> None:
        raw = self._raw()
        enriched = EnrichedListing.from_raw(raw, None, "none")
        assert enriched.description is None
        assert enriched.description_source == "none"

    def test_from_raw_preserves_all_raw_fields(self) -> None:
        raw = self._raw()
        enriched = EnrichedListing.from_raw(raw, "desc", "secondary")
        assert enriched.url == raw.url
        assert enriched.title == raw.title
        assert enriched.location_raw == raw.location_raw
        assert enriched.extra_fields == raw.extra_fields
