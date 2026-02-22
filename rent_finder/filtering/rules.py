"""
Deterministic pre-filter rules for rent-finder.

Runs on RawListing data BEFORE Playwright scraping to avoid wasting browser
time on listings that are obviously wrong.  Every check here uses only fields
available in the Apify JSON dataset (price, location, title, extra_fields).

Rules applied (in order):
  1. SOLD / HIDDEN    — extra_fields["is_sold"] or extra_fields["is_hidden"] is True
  2. PRICE_CAP        — price_cents > settings.criteria_max_rent_cad * 100
  3. LOCATION_EXCL    — location_raw contains a known out-of-area city name
  4. SHARED_UNIT      — title clearly indicates shared accommodation

Criteria for the target searcher (all configurable in .env except location sets):
  Target areas : North York, Markham, Downtown Toronto (Yonge corridor)
  Rent cap     : $1,600 / month including utilities (CRITERIA_MAX_RENT_CAD)
  Unit type    : Entire self-contained unit (no shared bathroom / kitchen)
  Move-in      : April 1 2026 (evaluated by OpenAI on the scraped description)
  Parking      : 1 car spot required (evaluated by OpenAI on the scraped description)
  Furnished    : Priority but not mandatory (evaluated by OpenAI)
  Basement     : OK if well-lit or walkout (evaluated by OpenAI)

Returns (passes: bool, reasons: list[str]).
  passes=True  → listing should proceed to Playwright scraping
  passes=False → listing should be marked pre_filter_rejected and skipped
"""

from __future__ import annotations

import re

from rent_finder.ingestion.models import RawListing
from rent_finder.utils.logging_config import get_logger

log = get_logger(__name__)


# ---------------------------------------------------------------------------
# Location data — cities clearly outside the target search area
# ---------------------------------------------------------------------------

# These city names appear in location_display_name / location_raw.
# All matches are case-insensitive substring checks.
# Conservative: only cities we're sure are out-of-scope are listed here.
# Ambiguous locations (plain "Toronto", "Ontario") are passed through to LLM.
_EXCLUDED_LOCATION_SUBSTRINGS: frozenset[str] = frozenset({
    "brampton",
    "mississauga",
    "hamilton",
    "oshawa",
    "ajax",
    "whitby",
    "pickering",
    "oakville",
    "burlington",
    "vaughan",
    "barrie",
    "kingston",
    "guelph",
    "cambridge",
    "kitchener",
    "waterloo",
})

# ---------------------------------------------------------------------------
# Shared-accommodation title patterns
# ---------------------------------------------------------------------------

# Matches titles that clearly indicate private-room or shared-unit listings.
# These imply a shared bathroom or kitchen even when the price looks fine.
_SHARED_UNIT_RE = re.compile(
    r"\b("
    r"private\s+room"
    r"|shared\s+(room|bathroom|kitchen|house|unit|accommodation)"
    r"|room(mate)?s?\s+(wanted|needed|for\s+rent)"
    r"|accommodation\s+(available\s+)?for\s+(couple|male|female|\d+)"
    r"|room\s+for\s+rent"
    r"|rent\s+a\s+room"
    r")\b",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def apply_pre_filters(
    listing: RawListing,
    *,
    max_rent_cad: int,
) -> tuple[bool, list[str]]:
    """
    Apply all deterministic pre-filter rules to a single listing.

    Args:
        listing: The RawListing to evaluate.
        max_rent_cad: Hard rent ceiling in CAD (from settings.criteria_max_rent_cad).

    Returns:
        (passes, rejection_reasons) — passes=True means proceed to scraping.
    """
    reasons: list[str] = []

    # ── Rule 1: Sold / hidden listing ─────────────────────────────────────────
    if listing.extra_fields.get("is_sold"):
        reasons.append("listing_already_sold")
    if listing.extra_fields.get("is_hidden"):
        reasons.append("listing_hidden_or_removed")

    # ── Rule 2: Price cap ─────────────────────────────────────────────────────
    # Only reject if we have a non-None price. Unknown price → pass to LLM.
    if listing.price_cents is not None:
        cap_cents = max_rent_cad * 100
        if listing.price_cents > cap_cents:
            reasons.append(
                f"price_exceeds_cap:{listing.price_cents // 100}_vs_{max_rent_cad}"
            )

    # ── Rule 3: Excluded location ─────────────────────────────────────────────
    # Only reject if location_raw is present and matches a known-excluded city.
    # Absent or ambiguous locations pass through.
    if listing.location_raw:
        loc_lower = listing.location_raw.lower()
        for excluded in _EXCLUDED_LOCATION_SUBSTRINGS:
            if excluded in loc_lower:
                reasons.append(f"location_excluded:{excluded}")
                break  # One location reason is enough

    # ── Rule 4: Shared accommodation ──────────────────────────────────────────
    if _SHARED_UNIT_RE.search(listing.title):
        reasons.append("shared_accommodation_title")

    passes = len(reasons) == 0
    if not passes:
        log.info(
            "pre_filter_rejected",
            listing_id=listing.listing_id,
            title=listing.title,
            reasons=reasons,
        )
    else:
        log.debug("pre_filter_passed", listing_id=listing.listing_id)

    return passes, reasons
