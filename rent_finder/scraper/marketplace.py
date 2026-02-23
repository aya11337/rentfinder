"""
Live Facebook Marketplace browse-page scraper for rent-finder.

Navigates to a filtered Marketplace search URL, scrolls through all
listing cards via infinite scroll, and extracts RawListing objects from
each card's href, aria-label, span text, and first image.

Uses the same create_context() from browser.py and RateLimiter from
rate_limiter.py as the per-listing Playwright scraper.

Age filtering:
    When max_age_hours > 0, cards whose relative timestamp indicates they
    were posted more than max_age_hours ago are skipped. Cards with no
    detectable timestamp are always included (conservative). Since the browse
    URL uses sortBy=creation_time_descend, once we see _AGE_STOP_THRESHOLD
    consecutive cards that are all too old we stop scrolling entirely.

Public API:
    scrape_marketplace(...) -> list[RawListing]
"""

from __future__ import annotations

import re
from typing import Any

from playwright.async_api import ElementHandle, async_playwright

from rent_finder.ingestion.models import RawListing
from rent_finder.scraper.browser import CookieExpiredError, create_context
from rent_finder.scraper.rate_limiter import RateLimiter
from rent_finder.utils.logging_config import get_logger

log = get_logger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_LISTING_ID_RE = re.compile(r"/marketplace/item/(\d+)/")
_PRICE_RE = re.compile(r"CA\$[\d,]+|\$[\d,]+", re.IGNORECASE)
_CARD_SELECTOR = 'div[role="main"] a[href*="/marketplace/item/"]'

# Stop scrolling after this many consecutive cards all exceeding max_age_hours
_AGE_STOP_THRESHOLD = 5

# Relative-time patterns Facebook shows on browse cards (checked in order)
_TIME_RE_PATTERNS: list[tuple[re.Pattern[str], float]] = [
    # "just now", "a moment ago" → 0 hours
    (re.compile(r"\bjust now\b|\bmoment ago\b", re.IGNORECASE), 0.0),
    # "X minute(s) ago" → X/60 hours
    (re.compile(r"\b(\d+)\s*min(?:ute)?s?\s+ago\b", re.IGNORECASE), -1.0),
    # "X hour(s) ago" or "Xh" short form
    (re.compile(r"\b(\d+)\s*h(?:ou?r?)?s?\s+ago\b", re.IGNORECASE), -2.0),
    (re.compile(r"\b(\d+)\s*h\b", re.IGNORECASE), -2.0),
    # "yesterday" → 24 hours (conservative)
    (re.compile(r"\byesterday\b", re.IGNORECASE), 24.0),
    # "X day(s) ago"
    (re.compile(r"\b(\d+)\s*days?\s+ago\b", re.IGNORECASE), -3.0),
]

# Sentinel values (negative) signal which capture group to use
_SENTINEL_MINUTES = -1.0
_SENTINEL_HOURS = -2.0
_SENTINEL_DAYS = -3.0


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _parse_hours_ago(text: str) -> float | None:
    """
    Parse a relative-time string and return elapsed hours as a float.

    Returns None if no recognisable pattern is found.

    Examples:
        "just now"       → 0.0
        "5 minutes ago"  → 0.083
        "3 hours ago"    → 3.0
        "2h"             → 2.0
        "yesterday"      → 24.0
        "random text"    → None
    """
    for pattern, sentinel in _TIME_RE_PATTERNS:
        m = pattern.search(text)
        if m is None:
            continue
        if sentinel == 0.0:
            return 0.0
        if sentinel == _SENTINEL_MINUTES:
            return int(m.group(1)) / 60.0
        if sentinel == _SENTINEL_HOURS:
            return float(m.group(1))
        if sentinel == _SENTINEL_DAYS:
            return float(m.group(1)) * 24.0
        return sentinel  # fixed value like 24.0 for "yesterday"
    return None


def _extract_listing_id(href: str) -> str | None:
    """Extract the numeric listing ID from a Facebook Marketplace item URL."""
    m = re.search(_LISTING_ID_RE, href)
    return m.group(1) if m else None


def _parse_price_raw(text: str) -> tuple[str | None, int | None]:
    """
    Parse a price string from card text.

    Returns:
        (price_raw, price_cents)
        price_cents is None for placeholder prices ($0, $1) or no match.
    """
    m = re.search(_PRICE_RE, text)
    if not m:
        return None, None

    raw = m.group(0)
    # Strip currency symbol and commas, convert to cents
    digits = re.sub(r"[^\d]", "", raw)
    if not digits:
        return raw, None

    cents = int(digits) * 100
    # Treat $0 or $1 as placeholder prices (Facebook sometimes shows these)
    if cents <= 100:
        return raw, None

    return raw, cents


async def _extract_card_data(card: ElementHandle) -> dict[str, Any] | None:
    """
    Extract listing data from a single card element.

    Returns a dict with listing_id, url, title, price_raw, price_cents,
    location_raw, image_url, and hours_ago (float | None).
    Returns None if listing_id cannot be extracted (invalid or missing href).
    """
    try:
        href: str = await card.get_attribute("href") or ""
        listing_id = _extract_listing_id(href)
        if not listing_id:
            return None

        # Canonical Facebook Marketplace URL
        url = f"https://www.facebook.com/marketplace/item/{listing_id}/"

        title: str | None = None
        price_raw: str | None = None
        price_cents: int | None = None
        location_raw: str | None = None
        image_url: str | None = None
        hours_ago: float | None = None

        # Fast path: aria-label often contains "TITLE, CA$PRICE" or similar
        aria = await card.get_attribute("aria-label") or ""
        if aria:
            price_raw, price_cents = _parse_price_raw(aria)
            # Title is the part before the first comma (if price found)
            if price_raw and "," in aria:
                title = aria.split(",")[0].strip() or None
            elif aria:
                title = aria.strip() or None

        # Span heuristics: more reliable for individual fields
        spans = await card.query_selector_all("span")
        span_texts: list[str] = []
        for span in spans:
            t = (await span.inner_text()).strip()
            if t:
                span_texts.append(t)

        # Find price span by regex
        price_span_idx: int | None = None
        for i, t in enumerate(span_texts):
            if re.search(_PRICE_RE, t):
                raw, cents = _parse_price_raw(t)
                if raw:
                    price_raw = raw
                    price_cents = cents
                    price_span_idx = i
                    break

        # Title heuristic: first non-price, non-short span
        if not title:
            for i, t in enumerate(span_texts):
                if i == price_span_idx:
                    continue
                if len(t) > 3 and not re.search(_PRICE_RE, t):
                    title = t
                    break

        # Location heuristic: span immediately after the price span
        if price_span_idx is not None and price_span_idx + 1 < len(span_texts):
            candidate = span_texts[price_span_idx + 1]
            # Location spans are typically short city/neighbourhood names
            if len(candidate) < 60 and not re.search(_PRICE_RE, candidate):
                location_raw = candidate

        # Timestamp heuristic: look for relative time in any span
        for t in span_texts:
            parsed = _parse_hours_ago(t)
            if parsed is not None:
                hours_ago = parsed
                break

        # First <img src> in the card
        img = await card.query_selector("img")
        if img:
            src = await img.get_attribute("src")
            if src and src.startswith("http"):
                image_url = src

        return {
            "listing_id": listing_id,
            "url": url,
            "title": title or "Unknown",
            "price_raw": price_raw,
            "price_cents": price_cents,
            "location_raw": location_raw,
            "image_url": image_url,
            "hours_ago": hours_ago,
        }

    except Exception as exc:
        log.debug("card_extract_error", error=str(exc))
        return None


async def _scroll_and_collect(
    page: Any,
    *,
    max_listings: int,
    max_scroll_pages: int,
    max_stale_scrolls: int,
    max_age_hours: float,
    rate_limiter: RateLimiter,
    min_delay_s: float,
    max_delay_s: float,
) -> list[RawListing]:
    """
    Scroll through the Marketplace browse page and collect listing cards.

    Stops when any of these conditions are met:
    - max_listings reached (if > 0)
    - max_stale_scrolls consecutive rounds with no height change AND no new cards
    - max_scroll_pages total scroll iterations reached
    - max_age_hours > 0 and _AGE_STOP_THRESHOLD consecutive cards all exceed the age limit
      (safe to stop early because the browse page is sorted newest-first)
    """
    collected: dict[str, RawListing] = {}  # listing_id → RawListing, dedup
    stale_count = 0
    scroll_count = 0
    prev_height = 0
    consecutive_old = 0  # cards exceeding max_age_hours in a row

    while scroll_count < max_scroll_pages:
        # Stop if we've hit the listing cap
        if max_listings > 0 and len(collected) >= max_listings:
            log.info(
                "marketplace_scroll_cap_hit",
                listings=len(collected),
                max=max_listings,
            )
            break

        # Extract all visible cards
        cards = await page.query_selector_all(_CARD_SELECTOR)
        new_this_round = 0

        for card in cards:
            data = await _extract_card_data(card)
            if data is None:
                continue

            # Age filter: skip cards posted too long ago
            if max_age_hours > 0 and data["hours_ago"] is not None:
                if data["hours_ago"] > max_age_hours:
                    consecutive_old += 1
                    log.debug(
                        "marketplace_card_too_old",
                        listing_id=data["listing_id"],
                        hours_ago=round(data["hours_ago"], 1),
                        max_age_hours=max_age_hours,
                    )
                    if consecutive_old >= _AGE_STOP_THRESHOLD:
                        log.info(
                            "marketplace_age_stop",
                            consecutive_old=consecutive_old,
                            total=len(collected),
                        )
                        return list(collected.values())
                    continue
                else:
                    consecutive_old = 0  # Reset on a fresh card

            lid = data["listing_id"]
            if lid not in collected:
                collected[lid] = RawListing(
                    listing_id=lid,
                    url=data["url"],
                    title=data["title"],
                    price_raw=data["price_raw"],
                    price_cents=data["price_cents"],
                    location_raw=data["location_raw"],
                    bedrooms=None,
                    bathrooms=None,
                    image_url=data["image_url"],
                    scraped_at=None,
                    extra_fields={},
                )
                new_this_round += 1
                if max_listings > 0 and len(collected) >= max_listings:
                    break

        log.debug(
            "marketplace_scroll_round",
            scroll=scroll_count,
            new_this_round=new_this_round,
            total=len(collected),
        )

        # Delay before scrolling (mimics human pacing)
        await rate_limiter.acquire(min_delay_s, max_delay_s)

        # Scroll to bottom
        current_height: int = await page.evaluate(
            "() => document.body.scrollHeight"
        )
        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")

        # Small pause for new content to load after scroll
        await rate_limiter.acquire(min_delay_s / 2, max_delay_s / 2)

        new_height: int = await page.evaluate(
            "() => document.body.scrollHeight"
        )

        scroll_count += 1

        # Stale detection: no height change AND no new cards → likely end of page
        if new_height == prev_height and new_this_round == 0:
            stale_count += 1
            log.debug("marketplace_stale_scroll", stale_count=stale_count)
            if stale_count >= max_stale_scrolls:
                log.info(
                    "marketplace_scroll_stale_stop",
                    stale_scrolls=stale_count,
                    total=len(collected),
                )
                break
        else:
            stale_count = 0  # Reset on any progress

        prev_height = new_height

    log.info(
        "marketplace_scroll_complete",
        scroll_rounds=scroll_count,
        listings_collected=len(collected),
    )
    return list(collected.values())


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def scrape_marketplace(
    *,
    browse_url: str,
    cookies_path: str,
    headless: bool,
    page_timeout_ms: int,
    max_listings: int,
    max_scroll_pages: int,
    max_stale_scrolls: int,
    max_age_hours: float,
    min_delay_s: float,
    max_delay_s: float,
) -> list[RawListing]:
    """
    Open a Facebook Marketplace browse URL and collect listing cards via scroll.

    Uses create_context() for cookie injection and session validation.
    Raises CookieExpiredError if the session has expired.

    Args:
        max_age_hours: Only include cards posted within this many hours.
                       0 disables the age filter (collect all cards).

    Returns a list of RawListing objects (no description — those are scraped
    in the subsequent per-listing Playwright pass).
    """
    rate_limiter = RateLimiter()

    async with async_playwright() as pw:
        browser, context = await create_context(
            pw,
            cookies_path,
            headless=headless,
        )
        try:
            page = await context.new_page()
            page.set_default_timeout(page_timeout_ms)

            log.info("marketplace_navigate", url=browse_url)
            await page.goto(browse_url, wait_until="domcontentloaded")

            # Detect cookie expiry on the browse page
            if "/login" in page.url or "/checkpoint" in page.url:
                raise CookieExpiredError(
                    "Facebook cookies rejected on Marketplace browse page. "
                    "Please export fresh cookies."
                )

            # Wait for at least one listing card to appear
            try:
                await page.wait_for_selector(_CARD_SELECTOR, timeout=15000)
            except Exception:
                log.warning(
                    "marketplace_no_cards_found",
                    url=browse_url,
                )
                return []

            listings = await _scroll_and_collect(
                page,
                max_listings=max_listings,
                max_scroll_pages=max_scroll_pages,
                max_stale_scrolls=max_stale_scrolls,
                max_age_hours=max_age_hours,
                rate_limiter=rate_limiter,
                min_delay_s=min_delay_s,
                max_delay_s=max_delay_s,
            )

            await page.close()
            return listings

        finally:
            await context.close()
            await browser.close()
