"""
Telegram Bot API sender for rent-finder.

Uses raw httpx HTTP calls to the Telegram Bot API — no python-telegram-bot library.
All sends use MarkdownV2 parse mode.

Retry behaviour:
  Network errors   → 2 retries: 3s, 6s
  HTTP 429         → honour retry_after header, then 1 retry
  HTTP 400 too long → truncate message to 4000 chars, 1 retry
  Other HTTP errors → log ERROR, return False (no retry)

Dry-run:
  send_listing() → no HTTP call, returns True, logs at INFO
  send_summary() → ALWAYS sends (operational visibility), includes DRY RUN badge
"""

from __future__ import annotations

import time

import httpx

from rent_finder.filtering.openai_client import FilterResult
from rent_finder.ingestion.models import EnrichedListing
from rent_finder.notifications.formatter import (
    format_listing_message,
    format_rejected_message,
    format_summary_message,
)
from rent_finder.utils.logging_config import get_logger

log = get_logger(__name__)

_TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"
_MAX_TRUNCATED = 4000  # Leave headroom below the 4096 limit on truncation retry


# ---------------------------------------------------------------------------
# Low-level send
# ---------------------------------------------------------------------------

def _send_text(
    bot_token: str,
    chat_id: str,
    text: str,
    *,
    timeout_s: int = 15,
) -> bool:
    """
    POST a single message to the Telegram Bot API.

    Returns True on success, False on permanent failure.
    Handles retry for network errors and rate limits.
    Does NOT truncate — the caller must ensure the message fits.
    """
    url = _TELEGRAM_API.format(token=bot_token)
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "MarkdownV2",
        "disable_web_page_preview": False,
    }

    for attempt in range(1, 4):  # 3 total attempts
        try:
            with httpx.Client(timeout=timeout_s) as client:
                resp = client.post(url, json=payload)

            if resp.status_code == 200:
                log.debug("telegram_send_ok", attempt=attempt)
                return True

            if resp.status_code == 429:
                retry_after = int(resp.json().get("parameters", {}).get("retry_after", 5))
                log.warning("telegram_rate_limit", retry_after=retry_after, attempt=attempt)
                time.sleep(retry_after)
                continue  # Retry after waiting

            if resp.status_code == 400:
                error_description = resp.json().get("description", "")
                log.warning(
                    "telegram_bad_request",
                    status=resp.status_code,
                    description=error_description,
                )
                return False  # Caller handles truncation

            log.error(
                "telegram_http_error",
                status=resp.status_code,
                body=resp.text[:200],
            )
            return False

        except httpx.HTTPError as exc:
            wait = attempt * 3  # 3s, 6s
            log.warning("telegram_network_error", attempt=attempt, error=str(exc), retry_in=wait)
            if attempt < 3:
                time.sleep(wait)
            else:
                log.error("telegram_send_exhausted", error=str(exc))
                return False

    return False


# ---------------------------------------------------------------------------
# Public send functions
# ---------------------------------------------------------------------------

def send_listing(
    listing: EnrichedListing,
    result: FilterResult,
    *,
    bot_token: str,
    chat_id: str,
    dry_run: bool = False,
    timeout_s: int = 15,
) -> bool:
    """
    Send a matched listing notification to Telegram.

    In dry-run mode: logs the would-be message at INFO and returns True
    without making any HTTP request.

    Returns True if the message was sent (or dry-run), False on delivery failure.
    """
    if dry_run:
        log.info(
            "dry_run_notify_skipped",
            listing_id=listing.listing_id,
            score=result.total_score,
            title=listing.title,
        )
        return True

    message = format_listing_message(listing, result)

    # Attempt 1: full message
    success = _send_text(bot_token, chat_id, message, timeout_s=timeout_s)
    if success:
        log.info(
            "notification_sent",
            listing_id=listing.listing_id,
            score=result.total_score,
            chars=len(message),
        )
        return True

    # Attempt 2: truncate if the message might be too long
    if len(message) > _MAX_TRUNCATED:
        truncated = message[:_MAX_TRUNCATED] + "\n_\\[message truncated\\]_"
        log.warning("telegram_truncating", original_chars=len(message))
        success = _send_text(bot_token, chat_id, truncated, timeout_s=timeout_s)
        if success:
            log.info("notification_sent_truncated", listing_id=listing.listing_id)
            return True

    log.error("notification_failed", listing_id=listing.listing_id)
    return False


def send_rejected_listing(
    listing: EnrichedListing,
    result: FilterResult,
    *,
    bot_token: str,
    chat_id: str,
    dry_run: bool = False,
    timeout_s: int = 15,
) -> bool:
    """
    Send an AI-rejected listing to Telegram so the user can review the decision.

    Shows rejection reasons, GPT reasoning, and a preview of the scraped
    description. In dry-run mode: logs at INFO without making an HTTP call.

    Returns True if sent (or dry-run), False on delivery failure.
    """
    if dry_run:
        log.info(
            "dry_run_rejected_notify_skipped",
            listing_id=listing.listing_id,
            score=result.total_score,
        )
        return True

    message = format_rejected_message(listing, result)

    success = _send_text(bot_token, chat_id, message, timeout_s=timeout_s)
    if success:
        log.info(
            "rejected_notification_sent",
            listing_id=listing.listing_id,
            score=result.total_score,
            chars=len(message),
        )
        return True

    if len(message) > _MAX_TRUNCATED:
        truncated = message[:_MAX_TRUNCATED] + "\n_\\[message truncated\\]_"
        log.warning("telegram_truncating_rejected", original_chars=len(message))
        success = _send_text(bot_token, chat_id, truncated, timeout_s=timeout_s)
        if success:
            log.info("rejected_notification_sent_truncated", listing_id=listing.listing_id)
            return True

    log.error("rejected_notification_failed", listing_id=listing.listing_id)
    return False


def send_summary(
    *,
    bot_token: str,
    chat_id: str,
    total_rows: int,
    new_listings: int,
    scraped_ok: int,
    scrape_failed: int,
    filter_passed: int,
    filter_rejected: int,
    notified: int,
    notify_failed: int = 0,
    errors: int = 0,
    duration_str: str = "",
    dry_run: bool = False,
    timeout_s: int = 15,
) -> bool:
    """
    Send the end-of-run pipeline summary to Telegram.

    Always sends — even in dry-run mode (operational visibility).
    The summary includes a DRY RUN badge when dry_run=True.
    """
    message = format_summary_message(
        total_rows=total_rows,
        new_listings=new_listings,
        scraped_ok=scraped_ok,
        scrape_failed=scrape_failed,
        filter_passed=filter_passed,
        filter_rejected=filter_rejected,
        notified=notified,
        notify_failed=notify_failed,
        errors=errors,
        duration_str=duration_str,
        dry_run=dry_run,
    )
    success = _send_text(bot_token, chat_id, message, timeout_s=timeout_s)
    if success:
        log.info("summary_sent", dry_run=dry_run)
    else:
        log.error("summary_send_failed")
    return success


def send_text_alert(
    text: str,
    *,
    bot_token: str,
    chat_id: str,
    timeout_s: int = 15,
) -> bool:
    """
    Send a plain text operational alert (cookie expiry, auth error, etc.).

    No parse_mode — text is sent as plain text to avoid escape issues.
    Always sends regardless of dry_run (it's an operational alert).
    """
    url = _TELEGRAM_API.format(token=bot_token)
    try:
        with httpx.Client(timeout=timeout_s) as client:
            resp = client.post(url, json={"chat_id": chat_id, "text": text})
        success = resp.status_code == 200
        if not success:
            log.error("alert_send_failed", status=resp.status_code)
        return success
    except httpx.HTTPError as exc:
        log.error("alert_send_error", error=str(exc))
        return False
