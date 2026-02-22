"""
Facebook Marketplace listing scraper.

Navigates to each listing URL using an authenticated Playwright browser context
(cookies injected by browser.py) and extracts the full listing description via
a 6-level selector fallback chain.

Description selector levels (tried in order, first match wins):
  Level 1 — PRIMARY:    data-testid structured element
  Level 2 — SECONDARY:  aria-label region paragraphs
  Level 3 — TERTIARY:   longest span inside role="main"
  Level 4 — QUATERNARY: Open Graph og:description meta tag (often truncated)
  Level 5 — QUINARY:    inner_text of role="main" block (raw visible text)
  Level 6 — FAILURE:    description=None, source="none"

Per-listing behaviour:
  - Login wall detected   → raises CookieExpiredError (aborts entire run)
  - Listing unavailable   → returns (None, "unavailable"), no selectors tried
  - TimeoutError on goto  → retried once with domcontentloaded; on second fail
                            returns (None, "none") and continues
  - Selector chain fails  → returns (None, "none") and continues
"""

from __future__ import annotations

import asyncio

from playwright.async_api import Page, async_playwright
from playwright.async_api import TimeoutError as PWTimeoutError

from rent_finder.ingestion.models import EnrichedListing, RawListing
from rent_finder.scraper.browser import CookieExpiredError, create_context
from rent_finder.scraper.rate_limiter import RateLimiter
from rent_finder.utils.logging_config import get_logger

log = get_logger(__name__)

# Minimum description length to consider a selector result valid
_MIN_DESC_CHARS = 30


# ---------------------------------------------------------------------------
# Internal page helpers
# ---------------------------------------------------------------------------

def _is_login_wall(url: str) -> bool:
    """Return True if the URL indicates a Facebook login or checkpoint page."""
    return "/login" in url or "/checkpoint" in url


async def _detect_unavailable(page: Page) -> bool:
    """Return True if the listing has been removed from Facebook."""
    try:
        el = await page.query_selector('span:text("This listing is no longer available")')
        if el:
            return True
        el = await page.query_selector('span:text("no longer available")')
        return el is not None
    except Exception:
        return False


async def _dismiss_modal(page: Page) -> None:
    """Silently dismiss any popup/modal that may obscure the description."""
    try:
        await page.click('div[aria-label="Close"]', timeout=2000)
    except Exception:
        pass


async def _scroll_to_trigger_lazy_load(page: Page) -> None:
    """Scroll halfway down to trigger lazy-loaded content."""
    try:
        await page.evaluate("window.scrollTo(0, document.body.scrollHeight / 2)")
        await asyncio.sleep(1.5)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Six-level selector fallback chain
# ---------------------------------------------------------------------------

async def _try_primary(page: Page) -> str | None:
    """Level 1: Structured data-testid element."""
    try:
        el = await page.wait_for_selector(
            'div[data-testid="marketplace-listing-item-description"]',
            timeout=8000,
        )
        if el:
            text = await el.inner_text()
            if text and len(text.strip()) >= _MIN_DESC_CHARS:
                return text.strip()
    except Exception:
        pass
    return None


async def _try_secondary(page: Page) -> str | None:
    """Level 2: aria-label='Listing details' paragraphs."""
    try:
        el = await page.wait_for_selector(
            'div[aria-label="Listing details"]',
            timeout=5000,
        )
        if el:
            paragraphs = await el.query_selector_all("p")
            texts = [await p.inner_text() for p in paragraphs]
            combined = "\n".join(t.strip() for t in texts if t.strip())
            if len(combined) >= _MIN_DESC_CHARS:
                return combined
    except Exception:
        pass
    return None


async def _try_tertiary(page: Page) -> str | None:
    """Level 3: Longest span inside role='main'."""
    try:
        spans = await page.query_selector_all('div[role="main"] span')
        best = ""
        for span in spans:
            text = await span.inner_text()
            if text and len(text.strip()) > len(best):
                best = text.strip()
        if len(best) >= _MIN_DESC_CHARS:
            return best
    except Exception:
        pass
    return None


async def _try_quaternary(page: Page) -> str | None:
    """Level 4: Open Graph og:description meta tag (often truncated ~200 chars)."""
    try:
        content = await page.evaluate(
            "() => document.querySelector('meta[property=\"og:description\"]')?.content"
        )
        if content and len(str(content).strip()) >= _MIN_DESC_CHARS:
            return str(content).strip()
    except Exception:
        pass
    return None


async def _try_quinary(page: Page) -> str | None:
    """Level 5: Full visible inner_text of the main region."""
    try:
        text = await page.inner_text('div[role="main"]')
        if text:
            # Take the longest contiguous block (split on blank lines)
            blocks = [b.strip() for b in text.split("\n\n") if b.strip()]
            best = max(blocks, key=len, default="")
            if len(best) >= _MIN_DESC_CHARS:
                return best
    except Exception:
        pass
    return None


async def _run_selector_chain(page: Page) -> tuple[str | None, str]:
    """
    Run selector levels 1-5 in order and return (description, source_name).
    Returns (None, "none") if all levels fail.
    """
    for level_fn, name in [
        (_try_primary, "primary"),
        (_try_secondary, "secondary"),
        (_try_tertiary, "tertiary"),
        (_try_quaternary, "og_meta"),
        (_try_quinary, "full_text"),
    ]:
        result = await level_fn(page)
        if result:
            log.debug("selector_matched", level=name, chars=len(result))
            return result, name

    log.warning("selector_chain_exhausted")
    return None, "none"


# ---------------------------------------------------------------------------
# Per-listing scrape
# ---------------------------------------------------------------------------

async def scrape_listing(
    page: Page,
    url: str,
    timeout_ms: int = 30000,
) -> tuple[str | None, str]:
    """
    Navigate to a single listing URL and extract the description.

    Args:
        page: An active Playwright page (shared across listings in a run).
        url: The Facebook Marketplace listing URL.
        timeout_ms: Page load timeout in milliseconds.

    Returns:
        (description, source) where source is one of:
        "primary", "secondary", "tertiary", "og_meta", "full_text",
        "none", or "unavailable".

    Raises:
        CookieExpiredError: If a login wall is detected mid-run.
    """
    # Attempt 1: networkidle (richer content)
    try:
        await page.goto(url, wait_until="networkidle", timeout=timeout_ms)
    except PWTimeoutError:
        log.warning("page_load_timeout_retry", url=url)
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
        except PWTimeoutError:
            log.warning("page_load_failed", url=url)
            return None, "none"
    except Exception as exc:
        log.warning("page_load_error", url=url, error=str(exc))
        return None, "none"

    # Check for login wall
    if _is_login_wall(page.url):
        raise CookieExpiredError(
            f"Login wall detected after navigating to {url}. "
            "Facebook session cookies have expired."
        )

    # Check for removed listing
    if await _detect_unavailable(page):
        log.info("listing_unavailable", url=url)
        return None, "unavailable"

    # Dismiss any modal that might block the description
    await _dismiss_modal(page)

    # Trigger lazy-loaded content
    await _scroll_to_trigger_lazy_load(page)

    # Run the 6-level selector chain
    description, source = await _run_selector_chain(page)

    if description:
        log.info(
            "scrape_success",
            url=url,
            selector=source,
            chars=len(description),
        )
    else:
        log.warning("scrape_no_description", url=url, selector=source)

    return description, source


# ---------------------------------------------------------------------------
# Batch scraper (called from pipeline orchestrator via asyncio.run)
# ---------------------------------------------------------------------------

async def scrape_all(
    listings: list[RawListing],
    *,
    cookies_path: str,
    headless: bool = True,
    page_timeout_ms: int = 30000,
    min_delay_s: float = 4.0,
    max_delay_s: float = 8.0,
    max_listings: int = 0,
) -> list[EnrichedListing]:
    """
    Scrape descriptions for all listings and return EnrichedListing objects.

    Runs inside a single Playwright browser context (one context per pipeline run).
    The rate limiter fires before every page load.

    Args:
        listings: New listings to scrape (already deduplicated and pre-filtered).
        cookies_path: Path to the Facebook session cookies JSON file.
        headless: Whether to run Chromium headless.
        page_timeout_ms: Per-page navigation timeout.
        min_delay_s / max_delay_s: Delay range between page loads.
        max_listings: Cap on listings scraped per run (0 = unlimited).

    Returns:
        List of EnrichedListing objects (one per input listing).

    Raises:
        CookieExpiredError: If the session expires mid-run (bubble up to orchestrator).
    """
    if max_listings > 0:
        listings = listings[:max_listings]
        log.info("scrape_cap_applied", cap=max_listings, total=len(listings))

    rate_limiter = RateLimiter()
    results: list[EnrichedListing] = []

    async with async_playwright() as pw:
        browser, context = await create_context(
            pw,
            cookies_path,
            headless=headless,
            timeout_ms=page_timeout_ms,
        )
        page = await context.new_page()

        try:
            for i, listing in enumerate(listings):
                log.info(
                    "scraping_listing",
                    index=i + 1,
                    total=len(listings),
                    listing_id=listing.listing_id,
                )
                # Rate limit before every navigation
                await rate_limiter.acquire(min_delay_s, max_delay_s)

                try:
                    description, source = await scrape_listing(
                        page, listing.url, timeout_ms=page_timeout_ms
                    )
                except CookieExpiredError:
                    # Session expired mid-run — bubble up to orchestrator
                    raise

                results.append(
                    EnrichedListing.from_raw(listing, description, source)
                )

        finally:
            await page.close()
            await context.close()
            await browser.close()
            log.info("browser_closed", scraped=len(results))

    return results
