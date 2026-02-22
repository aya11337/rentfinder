"""
Playwright browser context factory for rent-finder.

Responsibilities:
  - Load and normalise Facebook session cookies from JSON (supports both
    Playwright native format and Cookie-Editor extension export format).
  - Validate that the required session cookies (c_user, xs) are present.
  - Warn if any cookie expires within 7 days.
  - Create a Chromium browser context with cookies injected.
  - Perform a health-check navigation to confirm the session is still valid.

Raises CookieExpiredError if:
  - Required cookies are missing from the cookie file.
  - The health-check navigation lands on a login page.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from playwright.async_api import Browser, BrowserContext, Playwright

from rent_finder.utils.logging_config import get_logger

log = get_logger(__name__)


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class CookieExpiredError(Exception):
    """Raised when Facebook session cookies are expired or invalid."""


# ---------------------------------------------------------------------------
# Cookie utilities
# ---------------------------------------------------------------------------

def load_cookies(path: str | Path) -> list[dict]:
    """
    Load and normalise cookies from a JSON file.

    Accepts both:
    - Playwright native format (uses ``expires`` key)
    - Cookie-Editor extension export (uses ``expirationDate`` key)

    Returns a list of cookie dicts ready for context.add_cookies().

    Raises:
        FileNotFoundError: If the cookie file does not exist.
        ValueError: If the file is not valid JSON or not a list.
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(
            f"Cookie file not found: {path}\n"
            "Export your Facebook cookies using the Cookie-Editor browser extension "
            "while logged into Facebook, then save to this path."
        )

    try:
        raw: list[dict] = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"Cookie file is not valid JSON: {path}: {exc}") from exc

    if not isinstance(raw, list):
        raise ValueError(f"Cookie file must be a JSON array, got {type(raw).__name__}")

    normalised = []
    for c in raw:
        cookie: dict = {
            "name": c["name"],
            "value": c["value"],
            "domain": c.get("domain", ".facebook.com"),
            "path": c.get("path", "/"),
            "httpOnly": c.get("httpOnly", False),
            "secure": c.get("secure", True),
        }
        # Cookie-Editor uses "expirationDate"; Playwright uses "expires"
        expires = c.get("expires") or c.get("expirationDate")
        if expires is not None:
            try:
                expires_float = float(expires)
                if expires_float > 0:
                    cookie["expires"] = expires_float
            except (ValueError, TypeError):
                pass

        same_site = c.get("sameSite", "Lax") or "Lax"
        # Playwright expects title-case: "Strict", "Lax", "None"
        cookie["sameSite"] = same_site.capitalize()

        normalised.append(cookie)

    log.debug("cookies_loaded", count=len(normalised), path=str(path))
    return normalised


def validate_cookies(cookies: list[dict]) -> None:
    """
    Assert that the required Facebook session cookies are present.

    Raises CookieExpiredError if c_user or xs is missing.
    """
    names = {c["name"] for c in cookies}
    missing = {"c_user", "xs"} - names
    if missing:
        raise CookieExpiredError(
            f"Required Facebook session cookie(s) missing: {missing}. "
            "Please export fresh cookies from a logged-in browser session."
        )


def check_expiry_warnings(cookies: list[dict]) -> list[str]:
    """
    Return names of cookies that expire within 7 days.

    Does not raise — the caller logs this as a WARNING.
    """
    soon = time.time() + (7 * 86400)
    return [
        c["name"]
        for c in cookies
        if c.get("expires") is not None and float(c["expires"]) < soon
    ]


# ---------------------------------------------------------------------------
# Context factory
# ---------------------------------------------------------------------------

async def create_context(
    pw: Playwright,
    cookies_path: str | Path,
    *,
    headless: bool = True,
    timeout_ms: int = 20000,
) -> tuple[Browser, BrowserContext]:
    """
    Launch a Chromium browser, inject cookies, and verify the session is valid.

    Args:
        pw: Active Playwright instance (from async_playwright()).
        cookies_path: Path to the Facebook cookies JSON file.
        headless: Whether to run Chromium in headless mode.
        timeout_ms: Timeout for the health-check navigation in milliseconds.

    Returns:
        (browser, context) — caller is responsible for closing both.

    Raises:
        CookieExpiredError: If cookies are missing or session is expired.
        FileNotFoundError: If the cookie file does not exist.
    """
    cookies = load_cookies(cookies_path)
    validate_cookies(cookies)

    expiring = check_expiry_warnings(cookies)
    if expiring:
        log.warning("cookies_expiring_soon", names=expiring, days=7)

    browser = await pw.chromium.launch(
        headless=headless,
        args=[
            "--disable-blink-features=AutomationControlled",
            "--no-sandbox",
            "--disable-dev-shm-usage",
        ],
    )

    context = await browser.new_context(
        viewport={"width": 1280, "height": 800},
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/122.0.0.0 Safari/537.36"
        ),
        locale="en-CA",
        timezone_id="America/Toronto",
    )

    await context.add_cookies(cookies)
    log.debug("cookies_injected", count=len(cookies))

    # Health-check navigation: confirm the session is still active
    page = await context.new_page()
    try:
        await page.goto(
            "https://www.facebook.com/marketplace/",
            wait_until="domcontentloaded",
            timeout=timeout_ms,
        )
        current_url = page.url
        has_email_input = await page.query_selector('input[name="email"]') is not None

        if "/login" in current_url or "/checkpoint" in current_url or has_email_input:
            await browser.close()
            raise CookieExpiredError(
                "Facebook cookies rejected — session expired or account checkpointed. "
                "Please export fresh cookies from a logged-in browser session."
            )
    finally:
        await page.close()

    log.info("browser_context_ready", headless=headless)
    return browser, context
