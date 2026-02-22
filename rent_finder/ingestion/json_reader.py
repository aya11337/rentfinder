"""
JSON dataset reader for rent-finder.

Reads the Apify Facebook Marketplace Scraper output format and converts each
record into a RawListing dataclass.  Invalid or unparseable records are skipped
with a WARNING log — they never cause the pipeline to abort.

Field mapping from Apify JSON → RawListing:
    URl                        → url  (note: typo in source field name)
    marketplace_listing_title  → title
    formatted_amount           → price_raw
    amount                     → price_cents  (parsed: "1350.00" → 135000)
    location_display_name      → location_raw
    image                      → image_url
    custom_title               → bedrooms / bathrooms  (parsed: "1 bed · 1 bath")
    Everything else            → extra_fields  (stored as JSON blob)

Listing ID extraction:
    Extracted from the URL via regex: /item/(\\d+)/
    Records where no numeric ID is found are skipped.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from typing import Any

from rent_finder.ingestion.models import RawListing
from rent_finder.utils.logging_config import get_logger

log = get_logger(__name__)

# Regex to extract the numeric listing ID from a Facebook Marketplace URL.
# Matches: https://www.facebook.com/marketplace/item/123456789/?...
_LISTING_ID_RE = re.compile(r"/item/(\d+)/")

# Fields consumed explicitly from each JSON record.
# Everything else is stored in extra_fields.
_MAPPED_KEYS = {
    "URl",
    "marketplace_listing_title",
    "formatted_amount",
    "amount",
    "location_display_name",
    "image",
    "custom_title",
}

# Regex to parse "1 bed · 1 bath", "5 beds · 2 baths", "Studio" etc.
# The middle dot may be U+00B7 (·) or U+2022 (•) or a plain ASCII dot.
_BED_RE = re.compile(r"(\d+)\s+bed", re.IGNORECASE)
_BATH_RE = re.compile(r"(\d+)\s+bath", re.IGNORECASE)


def _extract_listing_id(url: str) -> str | None:
    """Return the numeric listing ID from a Facebook Marketplace URL, or None."""
    match = _LISTING_ID_RE.search(url)
    return match.group(1) if match else None


def _parse_price_cents(amount_str: str | None) -> int | None:
    """
    Convert the Apify ``amount`` field to integer cents.

    "1350.00" → 135000.  Returns None on None input, blank strings, or parse errors.
    Prices of $0 or $1 are treated as placeholder/contact-for-price and returned as None.
    """
    if not amount_str:
        return None
    try:
        dollars = float(amount_str)
        if dollars <= 1:
            # "$1" is a common Apify placeholder meaning "contact for price"
            return None
        return round(dollars * 100)
    except ValueError:
        return None


def _parse_bed_bath(custom_title: str | None) -> tuple[str | None, str | None]:
    """
    Extract bedroom and bathroom counts from the Apify ``custom_title`` field.

    Examples:
        "1 bed · 1 bath"  → ("1", "1")
        "5 beds · 2 baths" → ("5", "2")
        "Private Room"     → (None, None)
        None               → (None, None)
    """
    if not custom_title:
        return None, None
    bed_match = _BED_RE.search(custom_title)
    bath_match = _BATH_RE.search(custom_title)
    bedrooms = bed_match.group(1) if bed_match else None
    bathrooms = bath_match.group(1) if bath_match else None
    return bedrooms, bathrooms


def _build_extra_fields(record: dict[str, Any]) -> dict[str, Any]:
    """Return a dict of all fields not explicitly mapped to RawListing attributes."""
    return {k: v for k, v in record.items() if k not in _MAPPED_KEYS}


def parse_listings(path: str | Path) -> list[RawListing]:
    """
    Parse an Apify Facebook Marketplace JSON dataset file into RawListing objects.

    Args:
        path: Path to the JSON file (list of objects at top level).

    Returns:
        List of valid RawListing objects.  Skipped records are logged at WARNING.

    Raises:
        FileNotFoundError: If the file does not exist.
        ValueError: If the file is not valid JSON or is not a JSON array.
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"Dataset file not found: {path}")

    raw_text = path.read_text(encoding="utf-8")
    try:
        records: list[dict[str, Any]] = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON in dataset file {path}: {exc}") from exc

    if not isinstance(records, list):
        raise ValueError(
            f"Expected a JSON array at the top level of {path}, "
            f"got {type(records).__name__}"
        )

    log.info("dataset_loaded", path=str(path), total_records=len(records))

    listings: list[RawListing] = []
    skipped = 0

    for index, record in enumerate(records):
        # ── URL ────────────────────────────────────────────────────────────────
        url = record.get("URl") or ""
        if not url:
            log.warning("record_skipped_missing_url", index=index)
            skipped += 1
            continue

        # ── Listing ID ─────────────────────────────────────────────────────────
        listing_id = _extract_listing_id(url)
        if not listing_id:
            log.warning("record_skipped_no_listing_id", index=index, url=url)
            skipped += 1
            continue

        # ── Title ──────────────────────────────────────────────────────────────
        title: str = record.get("marketplace_listing_title") or ""
        if not title:
            log.warning("record_skipped_missing_title", index=index, listing_id=listing_id)
            skipped += 1
            continue

        # ── Price ──────────────────────────────────────────────────────────────
        price_raw: str | None = record.get("formatted_amount") or None
        price_cents = _parse_price_cents(record.get("amount"))

        # ── Location ───────────────────────────────────────────────────────────
        location_raw: str | None = record.get("location_display_name") or None

        # ── Image ──────────────────────────────────────────────────────────────
        image_url: str | None = record.get("image") or None

        # ── Bed / Bath ─────────────────────────────────────────────────────────
        bedrooms, bathrooms = _parse_bed_bath(record.get("custom_title"))

        # ── Extra fields ───────────────────────────────────────────────────────
        extra_fields = _build_extra_fields(record)

        listings.append(
            RawListing(
                listing_id=listing_id,
                url=url,
                title=title,
                price_raw=price_raw,
                price_cents=price_cents,
                location_raw=location_raw,
                bedrooms=bedrooms,
                bathrooms=bathrooms,
                image_url=image_url,
                scraped_at=None,  # Not present in Apify output
                extra_fields=extra_fields,
            )
        )
        log.debug("listing_parsed", listing_id=listing_id, title=title)

    log.info(
        "dataset_parsed",
        valid=len(listings),
        skipped=skipped,
        total=len(records),
    )
    return listings
