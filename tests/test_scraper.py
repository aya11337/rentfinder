"""
Unit tests for rent_finder.scraper (browser.py, facebook.py, rate_limiter.py).

No real Playwright browser is launched. All page/context/browser interactions
are mocked using AsyncMock so tests run instantly with zero network I/O.

asyncio_mode = "auto" in pyproject.toml means all async test functions
are automatically collected and run by pytest-asyncio.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from rent_finder.scraper.browser import (
    CookieExpiredError,
    check_expiry_warnings,
    load_cookies,
    validate_cookies,
)
from rent_finder.scraper.facebook import (
    _detect_unavailable,
    _is_login_wall,
    _run_selector_chain,
    scrape_listing,
)
from rent_finder.scraper.rate_limiter import RateLimiter

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

FIXTURE_COOKIES = Path(__file__).parent / "fixtures" / "sample_cookies.json"


def _make_page(
    url: str = "https://www.facebook.com/marketplace/item/123/",
    unavailable_el: object = None,
    primary_text: str | None = None,
    tertiary_spans: list[str] | None = None,
    og_meta: str | None = None,
    main_text: str | None = None,
) -> AsyncMock:
    """Build a mock Playwright Page with configurable return values."""
    page = AsyncMock()
    page.url = url

    # wait_for_selector: secondary level uses aria-label="Listing details"
    async def _wait_for_selector(selector: str, **kwargs):
        from playwright.async_api import TimeoutError as PWTimeout
        raise PWTimeout("selector not found")

    page.wait_for_selector = _wait_for_selector

    # query_selector: unavailability check, modal dismiss, and "See more" button
    async def _query_selector(selector: str, **kwargs):
        if "no longer available" in selector.lower() or "This listing" in selector:
            return unavailable_el
        return None

    page.query_selector = _query_selector

    # query_selector_all: tertiary spans
    async def _query_selector_all(selector: str, **kwargs):
        if "span" in selector and tertiary_spans is not None:
            mocks = []
            for text in tertiary_spans:
                el = AsyncMock()
                el.inner_text = AsyncMock(return_value=text)
                mocks.append(el)
            return mocks
        return []

    page.query_selector_all = _query_selector_all

    # evaluate: DOM-walk primary selector, og:description, and scroll helpers
    async def _evaluate(script: str, **kwargs):
        if "og:description" in script:
            return og_meta if og_meta else None
        if "scrollIntoView" in script or "scrollTo" in script:
            return None  # scroll helpers return no value
        if "Description" in script and primary_text:
            # DOM-walk primary selector — return the pre-cleaned description text
            return primary_text
        return None

    page.evaluate = _evaluate

    # inner_text: quinary fallback
    page.inner_text = AsyncMock(return_value=main_text or "")

    # goto: succeeds by default
    page.goto = AsyncMock(return_value=None)

    # click: modal dismiss (always fails silently)
    page.click = AsyncMock(side_effect=Exception("no modal"))

    return page


# ---------------------------------------------------------------------------
# RateLimiter
# ---------------------------------------------------------------------------

class TestRateLimiter:
    async def test_acquire_sleeps_within_range(self) -> None:
        limiter = RateLimiter()
        slept: list[float] = []

        with patch("rent_finder.scraper.rate_limiter.asyncio.sleep") as mock_sleep:
            mock_sleep.side_effect = lambda s: slept.append(s)
            await limiter.acquire(1.0, 2.0)

        assert len(slept) == 1
        assert 1.0 <= slept[0] <= 2.0

    async def test_acquire_always_sleeps(self) -> None:
        limiter = RateLimiter()
        calls: list[float] = []

        with patch("rent_finder.scraper.rate_limiter.asyncio.sleep") as mock_sleep:
            mock_sleep.side_effect = lambda s: calls.append(s)
            for _ in range(5):
                await limiter.acquire(0.1, 0.2)

        assert len(calls) == 5


# ---------------------------------------------------------------------------
# Cookie loading
# ---------------------------------------------------------------------------

class TestLoadCookies:
    def test_fixture_cookies_loaded(self) -> None:
        cookies = load_cookies(FIXTURE_COOKIES)
        assert len(cookies) == 4
        names = {c["name"] for c in cookies}
        assert {"c_user", "xs", "datr", "fr"}.issubset(names)

    def test_expirationdate_normalised_to_expires(self) -> None:
        """Cookie-Editor format uses expirationDate; we normalise to expires."""
        cookies = load_cookies(FIXTURE_COOKIES)
        for c in cookies:
            assert "expires" in c
            assert c["expires"] == 9999999999.0

    def test_playwright_format_expires_preserved(self, tmp_path: Path) -> None:
        data = [{
            "name": "test_cookie",
            "value": "val",
            "domain": ".facebook.com",
            "path": "/",
            "expires": 9999999999.0,
        }]
        p = tmp_path / "cookies.json"
        p.write_text(json.dumps(data))
        cookies = load_cookies(p)
        assert cookies[0]["expires"] == 9999999999.0

    def test_file_not_found_raises(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            load_cookies(tmp_path / "missing.json")

    def test_invalid_json_raises(self, tmp_path: Path) -> None:
        p = tmp_path / "bad.json"
        p.write_text("not json")
        with pytest.raises(ValueError, match="not valid JSON"):
            load_cookies(p)

    def test_non_list_json_raises(self, tmp_path: Path) -> None:
        p = tmp_path / "obj.json"
        p.write_text('{"name": "c_user"}')
        with pytest.raises(ValueError, match="JSON array"):
            load_cookies(p)

    def test_samesite_capitalised(self, tmp_path: Path) -> None:
        data = [{"name": "c_user", "value": "x", "sameSite": "none"}]
        p = tmp_path / "cookies.json"
        p.write_text(json.dumps(data))
        cookies = load_cookies(p)
        assert cookies[0]["sameSite"] == "None"

    def test_missing_samesite_defaults_to_lax(self, tmp_path: Path) -> None:
        data = [{"name": "c_user", "value": "x"}]
        p = tmp_path / "cookies.json"
        p.write_text(json.dumps(data))
        cookies = load_cookies(p)
        assert cookies[0]["sameSite"] == "Lax"


# ---------------------------------------------------------------------------
# Cookie validation
# ---------------------------------------------------------------------------

class TestValidateCookies:
    def _cookies(self, names: list[str]) -> list[dict]:
        return [{"name": n, "value": "x"} for n in names]

    def test_valid_cookies_no_error(self) -> None:
        validate_cookies(self._cookies(["c_user", "xs", "datr"]))

    def test_missing_c_user_raises(self) -> None:
        with pytest.raises(CookieExpiredError, match="c_user"):
            validate_cookies(self._cookies(["xs", "datr"]))

    def test_missing_xs_raises(self) -> None:
        with pytest.raises(CookieExpiredError, match="xs"):
            validate_cookies(self._cookies(["c_user", "datr"]))

    def test_both_missing_raises(self) -> None:
        with pytest.raises(CookieExpiredError):
            validate_cookies(self._cookies(["datr", "fr"]))

    def test_empty_list_raises(self) -> None:
        with pytest.raises(CookieExpiredError):
            validate_cookies([])


# ---------------------------------------------------------------------------
# Cookie expiry warnings
# ---------------------------------------------------------------------------

class TestExpiryWarnings:
    def test_far_future_cookie_no_warning(self) -> None:
        cookies = [{"name": "c_user", "expires": time.time() + 30 * 86400}]
        assert check_expiry_warnings(cookies) == []

    def test_expiring_within_7_days_warns(self) -> None:
        cookies = [{"name": "c_user", "expires": time.time() + 3 * 86400}]
        assert "c_user" in check_expiry_warnings(cookies)

    def test_no_expires_key_not_warned(self) -> None:
        cookies = [{"name": "c_user"}]
        assert check_expiry_warnings(cookies) == []

    def test_multiple_expiring_all_returned(self) -> None:
        soon = time.time() + 1 * 86400
        cookies = [
            {"name": "c_user", "expires": soon},
            {"name": "xs", "expires": soon},
            {"name": "datr", "expires": time.time() + 30 * 86400},
        ]
        expiring = check_expiry_warnings(cookies)
        assert "c_user" in expiring
        assert "xs" in expiring
        assert "datr" not in expiring


# ---------------------------------------------------------------------------
# Login wall detection
# ---------------------------------------------------------------------------

class TestLoginWallDetection:
    @pytest.mark.parametrize("url", [
        "https://www.facebook.com/login/",
        "https://www.facebook.com/login?next=/marketplace/",
        "https://www.facebook.com/checkpoint/",
        "https://www.facebook.com/checkpoint/block/?next",
    ])
    def test_login_urls_detected(self, url: str) -> None:
        assert _is_login_wall(url) is True

    @pytest.mark.parametrize("url", [
        "https://www.facebook.com/marketplace/item/123456/",
        "https://www.facebook.com/marketplace/",
        "https://www.facebook.com/marketplace/item/999/",
    ])
    def test_marketplace_urls_not_login(self, url: str) -> None:
        assert _is_login_wall(url) is False


# ---------------------------------------------------------------------------
# Unavailability detection
# ---------------------------------------------------------------------------

class TestUnavailableDetection:
    async def test_unavailable_element_found(self) -> None:
        page = _make_page(unavailable_el=MagicMock())
        result = await _detect_unavailable(page)
        assert result is True

    async def test_no_unavailable_element(self) -> None:
        page = _make_page(unavailable_el=None)
        result = await _detect_unavailable(page)
        assert result is False


# ---------------------------------------------------------------------------
# Selector chain
# ---------------------------------------------------------------------------

class TestSelectorChain:
    async def test_primary_selector_succeeds(self) -> None:
        desc = "A " + "x" * 50
        page = _make_page(primary_text=desc)
        description, source = await _run_selector_chain(page)
        assert description == desc
        assert source == "primary"

    async def test_primary_too_short_falls_to_next(self) -> None:
        page = _make_page(primary_text="Short")
        _, source = await _run_selector_chain(page)
        assert source != "primary"

    async def test_tertiary_longest_span_used(self) -> None:
        long_text = "B" * 60
        page = _make_page(tertiary_spans=["Hi", long_text])
        description, source = await _run_selector_chain(page)
        assert source == "tertiary"
        assert description == long_text

    async def test_og_meta_used_at_level_4(self) -> None:
        meta = "M" * 50
        page = _make_page(og_meta=meta)
        description, source = await _run_selector_chain(page)
        assert source == "og_meta"
        assert description == meta

    async def test_quinary_full_text_used_at_level_5(self) -> None:
        block_a = "A" * 80
        block_b = "B" * 40
        page = _make_page(main_text=f"{block_a}\n\n{block_b}")
        description, source = await _run_selector_chain(page)
        assert source == "full_text"
        assert block_a in description

    async def test_all_levels_fail_returns_none(self) -> None:
        page = _make_page()
        description, source = await _run_selector_chain(page)
        assert description is None
        assert source == "none"


# ---------------------------------------------------------------------------
# Per-listing scrape function
# ---------------------------------------------------------------------------

class TestScrapeListing:
    async def test_successful_scrape_returns_description(self) -> None:
        desc = "D" * 60
        page = _make_page(primary_text=desc)
        description, source = await scrape_listing(page, page.url, timeout_ms=5000)
        assert description == desc
        assert source == "primary"

    async def test_unavailable_listing_returns_unavailable(self) -> None:
        page = _make_page(unavailable_el=MagicMock())
        description, source = await scrape_listing(page, page.url, timeout_ms=5000)
        assert description is None
        assert source == "unavailable"

    async def test_login_wall_raises_cookie_error(self) -> None:
        page = _make_page(url="https://www.facebook.com/login/")
        with pytest.raises(CookieExpiredError):
            await scrape_listing(page, page.url, timeout_ms=5000)

    async def test_checkpoint_url_raises_cookie_error(self) -> None:
        page = _make_page(url="https://www.facebook.com/checkpoint/block/")
        with pytest.raises(CookieExpiredError):
            await scrape_listing(page, page.url, timeout_ms=5000)

    async def test_timeout_on_goto_retried_then_returns_none(self) -> None:
        from playwright.async_api import TimeoutError as PWTimeout

        page = AsyncMock()
        page.url = "https://www.facebook.com/marketplace/item/123/"
        page.goto = AsyncMock(side_effect=PWTimeout("timeout"))
        page.query_selector = AsyncMock(return_value=None)
        page.query_selector_all = AsyncMock(return_value=[])
        page.evaluate = AsyncMock(return_value=None)
        page.inner_text = AsyncMock(return_value="")

        async def _no_selector(*args, **kwargs):
            raise PWTimeout("timeout")

        page.wait_for_selector = _no_selector

        description, source = await scrape_listing(page, page.url, timeout_ms=100)
        assert description is None
        assert source == "none"
