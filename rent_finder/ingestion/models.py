"""
Data models for rent-finder ingestion and filtering pipeline.

RawListing:     Parsed directly from the JSON dataset. Immutable after construction.
EnrichedListing: RawListing + description scraped by Playwright. Passed to OpenAI filter.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class RawListing:
    """
    A single listing as parsed from the Apify Facebook Marketplace JSON dataset.

    All fields that might be absent in the source data are Optional.
    ``listing_id`` is the canonical deduplication key — extracted from the URL.
    ``price_cents`` is the parsed integer price (e.g. 135000 for CA$1,350).
    ``extra_fields`` holds any source columns not explicitly mapped to a field above.
    """

    listing_id: str
    url: str
    title: str
    price_raw: str | None = None
    price_cents: int | None = None
    location_raw: str | None = None
    bedrooms: str | None = None
    bathrooms: str | None = None
    image_url: str | None = None
    scraped_at: str | None = None
    extra_fields: dict = field(default_factory=dict)

    def __str__(self) -> str:
        price = f"${self.price_cents // 100:,}" if self.price_cents else self.price_raw or "?"
        return f"RawListing({self.listing_id!r}, {self.title!r}, {price})"


@dataclass(frozen=True)
class EnrichedListing:
    """
    A RawListing extended with the full description scraped by Playwright.

    Constructed in the scraper stage; passed to the OpenAI filter.
    ``description`` may be None if all selectors failed but the page loaded.
    ``description_source`` records which selector level (or "none"/"unavailable")
    produced the description.
    """

    listing_id: str
    url: str
    title: str
    price_raw: str | None
    price_cents: int | None
    location_raw: str | None
    bedrooms: str | None
    bathrooms: str | None
    image_url: str | None
    scraped_at: str | None
    extra_fields: dict
    description: str | None
    description_source: str

    @classmethod
    def from_raw(
        cls,
        raw: RawListing,
        description: str | None,
        description_source: str,
    ) -> EnrichedListing:
        """Construct an EnrichedListing from a RawListing and scrape results."""
        return cls(
            listing_id=raw.listing_id,
            url=raw.url,
            title=raw.title,
            price_raw=raw.price_raw,
            price_cents=raw.price_cents,
            location_raw=raw.location_raw,
            bedrooms=raw.bedrooms,
            bathrooms=raw.bathrooms,
            image_url=raw.image_url,
            scraped_at=raw.scraped_at,
            extra_fields=raw.extra_fields,
            description=description,
            description_source=description_source,
        )
