"""
Unit tests for rent_finder/scraper/marketplace.py.

All tests use AsyncMock — no real browser is launched.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from rent_finder.scraper.marketplace import (
    _extract_listing_id,
    _parse_price_raw,
    _extract_card_data,
    _scroll_and_collect,
    scrape_marketplace,
)
from rent_finder.scraper.browser import CookieExpiredError
from rent_finder.scraper.rate_limiter import RateLimiter


# ---------------------------------------------------------------------------
# _extract_listing_id
# ---------------------------------------------------------------------------


class TestExtractListingId:
    def test_valid_marketplace_url(self):
        href = "https://www.facebook.com/marketplace/item/123456789012/"
        assert _extract_listing_id(href) == "123456789012"

    def test_valid_url_with_query_params(self):
        href = "/marketplace/item/987654321/?ref=search"
        assert _extract_listing_id(href) == "987654321"

    def test_non_item_url_returns_none(self):
        href = "https://www.facebook.com/marketplace/toronto/"
        assert _extract_listing_id(href) is None

    def test_empty_string_returns_none(self):
        assert _extract_listing_id("") is None

    def test_non_marketplace_item_url_returns_none(self):
        # CSS card selector already requires /marketplace/item/ so this never
        # appears in practice, but the regex is now explicit about it.
        assert _extract_listing_id("https://example.com/item/123/") is None


# ---------------------------------------------------------------------------
# _parse_price_raw
# ---------------------------------------------------------------------------


class TestParsePriceRaw:
    def test_canadian_dollar_price(self):
        raw, cents = _parse_price_raw("CA$1,350/month")
        assert raw == "CA$1,350"
        assert cents == 135000

    def test_plain_dollar_price(self):
        raw, cents = _parse_price_raw("$2,400 per month")
        assert raw == "$2,400"
        assert cents == 240000

    def test_placeholder_price_one_dollar(self):
        # $1 is a Facebook placeholder — should return None for cents
        raw, cents = _parse_price_raw("$1")
        assert raw == "$1"
        assert cents is None

    def test_placeholder_price_zero(self):
        raw, cents = _parse_price_raw("$0")
        assert raw is not None  # raw is returned
        assert cents is None

    def test_no_price_in_text(self):
        raw, cents = _parse_price_raw("Bright 1 bedroom in Leslieville")
        assert raw is None
        assert cents is None

    def test_price_without_comma(self):
        raw, cents = _parse_price_raw("CA$900/month")
        assert raw == "CA$900"
        assert cents == 90000


# ---------------------------------------------------------------------------
# _extract_card_data
# ---------------------------------------------------------------------------


class TestExtractCardData:
    def _make_card(
        self,
        href: str = "https://www.facebook.com/marketplace/item/111222333/",
        aria_label: str = "",
        spans: list[str] | None = None,
        img_src: str | None = None,
    ) -> MagicMock:
        card = AsyncMock()
        card.get_attribute = AsyncMock(
            side_effect=lambda attr: {
                "href": href,
                "aria-label": aria_label,
            }.get(attr, "")
        )

        span_mocks = []
        for text in (spans or []):
            s = AsyncMock()
            s.inner_text = AsyncMock(return_value=text)
            span_mocks.append(s)
        card.query_selector_all = AsyncMock(return_value=span_mocks)

        if img_src:
            img = AsyncMock()
            img.get_attribute = AsyncMock(return_value=img_src)
            card.query_selector = AsyncMock(return_value=img)
        else:
            card.query_selector = AsyncMock(return_value=None)

        return card

    @pytest.mark.asyncio
    async def test_full_card_with_aria_label(self):
        card = self._make_card(
            aria_label="Bright 1BR Leslieville, CA$1,800",
            spans=["Bright 1BR Leslieville", "CA$1,800", "Leslieville"],
            img_src="https://scontent.example.com/img.jpg",
        )
        result = await _extract_card_data(card)
        assert result is not None
        assert result["listing_id"] == "111222333"
        assert result["url"] == "https://www.facebook.com/marketplace/item/111222333/"

    @pytest.mark.asyncio
    async def test_no_href_returns_none(self):
        card = self._make_card(href="")
        result = await _extract_card_data(card)
        assert result is None

    @pytest.mark.asyncio
    async def test_non_item_href_returns_none(self):
        card = self._make_card(href="https://www.facebook.com/marketplace/toronto/")
        result = await _extract_card_data(card)
        assert result is None

    @pytest.mark.asyncio
    async def test_exception_in_card_returns_none(self):
        card = MagicMock()
        card.get_attribute = AsyncMock(side_effect=Exception("DOM error"))
        result = await _extract_card_data(card)
        assert result is None

    @pytest.mark.asyncio
    async def test_price_extracted_from_spans(self):
        card = self._make_card(
            spans=["Nice apartment", "CA$1,500/month", "Riverside"],
        )
        result = await _extract_card_data(card)
        assert result is not None
        assert result["price_raw"] == "CA$1,500"
        assert result["price_cents"] == 150000

    @pytest.mark.asyncio
    async def test_location_after_price_span(self):
        card = self._make_card(
            spans=["Cozy studio", "CA$1,200", "Corktown, Toronto"],
        )
        result = await _extract_card_data(card)
        assert result is not None
        assert result["location_raw"] == "Corktown, Toronto"

    @pytest.mark.asyncio
    async def test_image_url_extracted(self):
        card = self._make_card(
            img_src="https://scontent.fb.com/photo.jpg",
        )
        result = await _extract_card_data(card)
        assert result is not None
        assert result["image_url"] == "https://scontent.fb.com/photo.jpg"

    @pytest.mark.asyncio
    async def test_no_image_gives_none(self):
        card = self._make_card(img_src=None)
        result = await _extract_card_data(card)
        assert result is not None
        assert result["image_url"] is None


# ---------------------------------------------------------------------------
# _scroll_and_collect
# ---------------------------------------------------------------------------


def _make_rate_limiter() -> RateLimiter:
    rl = RateLimiter()
    rl.acquire = AsyncMock(return_value=None)
    return rl


def _make_card_result(listing_id: str) -> dict[str, Any]:
    """Build a fake _extract_card_data return value for a given listing_id."""
    return {
        "listing_id": listing_id,
        "url": f"https://www.facebook.com/marketplace/item/{listing_id}/",
        "title": f"Listing {listing_id}",
        "price_raw": "CA$1,500",
        "price_cents": 150000,
        "location_raw": "Toronto",
        "image_url": None,
    }


def _make_page_for_scroll(
    card_id_rounds: list[list[str]],
    heights: list[int] | None = None,
) -> AsyncMock:
    """
    Build a minimal async page mock for _scroll_and_collect tests.

    card_id_rounds: list of lists — each entry is the ACCUMULATED set of
    listing IDs visible on that scroll round (simulates new cards loading).
    heights: page heights returned by document.body.scrollHeight per scroll.
    """
    page = AsyncMock()

    # round_idx tracks how many times query_selector_all has been called
    state = {"round": 0}

    async def qsa(selector: str) -> list[SimpleNamespace]:
        idx = min(state["round"], len(card_id_rounds) - 1)
        state["round"] += 1
        # Return a SimpleNamespace per listing_id so card.listing_id is a string
        return [SimpleNamespace(listing_id=lid) for lid in card_id_rounds[idx]]

    page.query_selector_all = qsa

    if heights is None:
        heights = list(range(1000, 1000 + len(card_id_rounds) * 1000, 1000))

    # scrollTo also contains "scrollHeight", so provide enough values
    height_seq = heights + [heights[-1]] * 40
    height_state = {"idx": 0}

    async def evaluate(expr: str) -> object:
        if "scrollHeight" in expr:
            val = height_seq[min(height_state["idx"], len(height_seq) - 1)]
            height_state["idx"] += 1
            return val
        return None

    page.evaluate = evaluate
    return page


class TestScrollAndCollect:
    """
    Tests for _scroll_and_collect. Uses patch on _extract_card_data so we
    control exactly which listing IDs are returned per card without relying
    on complex multi-level async mock chains.
    """

    def _patched_extract(self) -> Any:
        """
        Build a patch-compatible async side_effect for _extract_card_data.

        Reads card.listing_id from the SimpleNamespace objects returned by
        the page mock's query_selector_all.
        """
        async def _extract(card: Any) -> dict[str, Any] | None:
            lid = card.listing_id
            return _make_card_result(lid)

        return _extract

    @pytest.mark.asyncio
    async def test_max_listings_stop(self):
        listing_ids = ["aaa", "bbb", "ccc", "ddd", "eee"]
        page = _make_page_for_scroll(
            [listing_ids] * 5,  # same 5 ids every round
            heights=[1000, 2000, 3000, 4000, 5000],
        )
        rl = _make_rate_limiter()

        with patch(
            "rent_finder.scraper.marketplace._extract_card_data",
            side_effect=self._patched_extract(),
        ):
            results = await _scroll_and_collect(
                page,
                max_listings=3,
                max_scroll_pages=10,
                max_stale_scrolls=3,
                rate_limiter=rl,
                min_delay_s=0,
                max_delay_s=0,
            )
        assert len(results) == 3

    @pytest.mark.asyncio
    async def test_stale_stop(self):
        # After first round, height stays the same and no new cards load
        listing_ids = ["aaa", "bbb"]
        page = _make_page_for_scroll(
            [listing_ids] * 5,  # same ids every round (no growth)
            heights=[1000, 1000, 1000, 1000, 1000],  # no height change
        )
        rl = _make_rate_limiter()

        with patch(
            "rent_finder.scraper.marketplace._extract_card_data",
            side_effect=self._patched_extract(),
        ):
            results = await _scroll_and_collect(
                page,
                max_listings=0,
                max_scroll_pages=10,
                max_stale_scrolls=2,
                rate_limiter=rl,
                min_delay_s=0,
                max_delay_s=0,
            )
        # Stops after 2 stale rounds; only the initial 2 listings collected
        assert len(results) == 2

    @pytest.mark.asyncio
    async def test_max_scroll_pages_ceiling(self):
        # Each round adds 5 new unique cards; page grows
        rounds = [[f"id{i:04d}" for i in range(j * 5, (j + 1) * 5)] for j in range(20)]
        # Accumulate: each round shows all cards up to that point
        accumulated = []
        acc_rounds: list[list[str]] = []
        for r in rounds:
            accumulated = list(dict.fromkeys(accumulated + r))
            acc_rounds.append(accumulated[:])

        page = _make_page_for_scroll(
            acc_rounds,
            heights=list(range(1000, 21000, 1000)),
        )
        rl = _make_rate_limiter()

        with patch(
            "rent_finder.scraper.marketplace._extract_card_data",
            side_effect=self._patched_extract(),
        ):
            results = await _scroll_and_collect(
                page,
                max_listings=0,
                max_scroll_pages=5,
                max_stale_scrolls=3,
                rate_limiter=rl,
                min_delay_s=0,
                max_delay_s=0,
            )
        # 5 scroll rounds × 5 new unique cards = 25 listings max
        assert len(results) <= 25

    @pytest.mark.asyncio
    async def test_cross_round_dedup(self):
        # Same listing IDs appear in both rounds — should deduplicate
        round1 = ["aaa", "bbb"]
        round2 = ["aaa", "bbb", "ccc"]  # aaa/bbb repeated

        page = _make_page_for_scroll(
            [round1, round2, round2],  # repeat round2 to trigger stale
            heights=[1000, 2000, 2000, 2000],
        )
        rl = _make_rate_limiter()

        with patch(
            "rent_finder.scraper.marketplace._extract_card_data",
            side_effect=self._patched_extract(),
        ):
            results = await _scroll_and_collect(
                page,
                max_listings=0,
                max_scroll_pages=5,
                max_stale_scrolls=2,
                rate_limiter=rl,
                min_delay_s=0,
                max_delay_s=0,
            )
        listing_ids = {r.listing_id for r in results}
        assert "aaa" in listing_ids
        assert "bbb" in listing_ids
        assert "ccc" in listing_ids
        assert len(results) == 3  # No duplicates


# ---------------------------------------------------------------------------
# scrape_marketplace (public API)
# ---------------------------------------------------------------------------


class TestScrapeMarketplace:
    @pytest.mark.asyncio
    async def test_happy_path_returns_listings(self):
        with (
            patch("rent_finder.scraper.marketplace.async_playwright") as mock_pw,
            patch("rent_finder.scraper.marketplace.create_context") as mock_ctx,
        ):
            # Set up fake browser/context
            mock_browser = AsyncMock()
            mock_context = AsyncMock()
            mock_page = AsyncMock()

            mock_pw.return_value.__aenter__ = AsyncMock(return_value=MagicMock())
            mock_pw.return_value.__aexit__ = AsyncMock(return_value=None)
            mock_ctx.return_value = (mock_browser, mock_context)
            mock_context.new_page = AsyncMock(return_value=mock_page)
            mock_page.url = "https://www.facebook.com/marketplace/toronto/search/"
            mock_page.set_default_timeout = MagicMock()

            # Card with valid listing_id
            card = AsyncMock()
            card.get_attribute = AsyncMock(
                side_effect=lambda attr: {
                    "href": "https://www.facebook.com/marketplace/item/999888777/",
                    "aria-label": "",
                }.get(attr, "")
            )
            span = AsyncMock()
            span.inner_text = AsyncMock(return_value="CA$1,500")
            card.query_selector_all = AsyncMock(return_value=[span])
            card.query_selector = AsyncMock(return_value=None)

            call_count = 0

            async def qsa(sel: str) -> list:
                nonlocal call_count
                call_count += 1
                return [card]

            mock_page.query_selector_all = qsa
            mock_page.wait_for_selector = AsyncMock(return_value=None)
            mock_page.goto = AsyncMock(return_value=None)

            height_calls = 0

            async def evaluate(expr: str) -> object:
                nonlocal height_calls
                if "scrollHeight" in expr:
                    height_calls += 1
                    return 1000  # Constant height → stale after max_stale_scrolls
                return None

            mock_page.evaluate = evaluate
            mock_page.close = AsyncMock(return_value=None)

            results = await scrape_marketplace(
                browse_url="https://www.facebook.com/marketplace/toronto/search/",
                cookies_path="data/cookies.json",
                headless=True,
                page_timeout_ms=30000,
                max_listings=0,
                max_scroll_pages=5,
                max_stale_scrolls=2,
                min_delay_s=0,
                max_delay_s=0,
            )

            assert isinstance(results, list)
            # At least one listing was collected
            assert len(results) >= 1
            assert results[0].listing_id == "999888777"

    @pytest.mark.asyncio
    async def test_login_wall_raises_cookie_expired_error(self):
        with (
            patch("rent_finder.scraper.marketplace.async_playwright") as mock_pw,
            patch("rent_finder.scraper.marketplace.create_context") as mock_ctx,
        ):
            mock_browser = AsyncMock()
            mock_context = AsyncMock()
            mock_page = AsyncMock()

            mock_pw.return_value.__aenter__ = AsyncMock(return_value=MagicMock())
            mock_pw.return_value.__aexit__ = AsyncMock(return_value=None)
            mock_ctx.return_value = (mock_browser, mock_context)
            mock_context.new_page = AsyncMock(return_value=mock_page)
            # Simulate redirect to login
            mock_page.url = "https://www.facebook.com/login/?next=marketplace"
            mock_page.set_default_timeout = MagicMock()
            mock_page.goto = AsyncMock(return_value=None)

            with pytest.raises(CookieExpiredError):
                await scrape_marketplace(
                    browse_url="https://www.facebook.com/marketplace/toronto/search/",
                    cookies_path="data/cookies.json",
                    headless=True,
                    page_timeout_ms=30000,
                    max_listings=0,
                    max_scroll_pages=5,
                    max_stale_scrolls=2,
                    min_delay_s=0,
                    max_delay_s=0,
                )

    @pytest.mark.asyncio
    async def test_no_cards_found_returns_empty_list(self):
        with (
            patch("rent_finder.scraper.marketplace.async_playwright") as mock_pw,
            patch("rent_finder.scraper.marketplace.create_context") as mock_ctx,
        ):
            mock_browser = AsyncMock()
            mock_context = AsyncMock()
            mock_page = AsyncMock()

            mock_pw.return_value.__aenter__ = AsyncMock(return_value=MagicMock())
            mock_pw.return_value.__aexit__ = AsyncMock(return_value=None)
            mock_ctx.return_value = (mock_browser, mock_context)
            mock_context.new_page = AsyncMock(return_value=mock_page)
            mock_page.url = "https://www.facebook.com/marketplace/toronto/search/"
            mock_page.set_default_timeout = MagicMock()
            mock_page.goto = AsyncMock(return_value=None)
            # Simulate no cards appearing (timeout)
            mock_page.wait_for_selector = AsyncMock(side_effect=Exception("Timeout"))

            results = await scrape_marketplace(
                browse_url="https://www.facebook.com/marketplace/toronto/search/",
                cookies_path="data/cookies.json",
                headless=True,
                page_timeout_ms=30000,
                max_listings=0,
                max_scroll_pages=5,
                max_stale_scrolls=2,
                min_delay_s=0,
                max_delay_s=0,
            )
            assert results == []
