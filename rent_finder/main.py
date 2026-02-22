"""
rent-finder pipeline orchestrator and CLI.

Entry point: python -m rent_finder.main [OPTIONS]

Pipeline stages (in order):
  1. Load settings and init DB
  2. Parse JSON input file
  3. Dedup: filter out listings already in DB
  4. Pre-filter: reject by price / location / shared-unit rules
  5. Re-notify: deliver any notify_failed listings from previous runs
  6. Scrape: Playwright description extraction for new listings
  7. Filter + Notify: OpenAI evaluation → Telegram delivery for PASS listings
  8. Summary: send end-of-run statistics to Telegram
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import click

from rent_finder.config import Settings
from rent_finder.filtering.openai_client import (
    FilterResult,
    OpenAIAuthError,
    filter_listing,
)
from rent_finder.filtering.rules import apply_pre_filters
from rent_finder.ingestion.json_reader import parse_listings
from rent_finder.ingestion.models import EnrichedListing, RawListing
from rent_finder.notifications.telegram import (
    send_listing,
    send_summary,
    send_text_alert,
)
from rent_finder.scraper.browser import CookieExpiredError
from rent_finder.scraper.facebook import scrape_all
from rent_finder.storage import repository as repo
from rent_finder.storage.database import get_connection, init_db
from rent_finder.utils.logging_config import configure_logging, get_logger

log = get_logger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _format_duration(seconds: float) -> str:
    """Format elapsed seconds as a human-readable string."""
    total = int(seconds)
    h, remainder = divmod(total, 3600)
    m, s = divmod(remainder, 60)
    if h:
        return f"{h}h {m}m {s}s"
    if m:
        return f"{m}m {s}s"
    return f"{s}s"


def _rebuild_enriched(row: dict[str, Any]) -> EnrichedListing:
    """
    Reconstruct an EnrichedListing from a get_unnotified_passes() DB row.

    Used when re-notifying previously failed Telegram sends.
    """
    return EnrichedListing(
        listing_id=row["listing_id"],
        url=row["url"],
        title=row.get("title") or "",
        price_raw=row.get("price_raw"),
        price_cents=None,
        location_raw=row.get("location_raw"),
        bedrooms=row.get("bedrooms"),
        bathrooms=row.get("bathrooms"),
        image_url=None,
        scraped_at=None,
        extra_fields={},
        description=row.get("description"),
        description_source="db",
    )


def _rebuild_filter_result(row: dict[str, Any]) -> FilterResult:
    """
    Reconstruct a FilterResult from a get_unnotified_passes() DB row.

    Uses model_construct() to skip re-validation (data was already validated
    when originally stored).
    """
    breakdown: dict[str, int] = {}
    if row.get("filter_score_breakdown"):
        try:
            breakdown = json.loads(row["filter_score_breakdown"])
        except Exception:
            pass
    return FilterResult.model_construct(
        decision="PASS",
        rejection_reasons=[],
        scam_flag=False,
        total_score=row.get("filter_score") or 0,
        score_breakdown=breakdown,
        reasoning=row.get("filter_reasoning") or "Previously matched listing.",
    )


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------

def run_pipeline(
    *,
    settings: Settings,
    json_path: str,
    dry_run: bool,
    headed: bool,
    run_id: str,
) -> int:
    """
    Execute the full rent-finder pipeline.

    Returns:
        0 — success (possibly with non-critical per-listing errors)
        1 — unrecoverable error (bad config, DB init failure, OpenAI auth)
        2 — Facebook cookie expiry detected
    """
    start_time = time.monotonic()
    errors: list[str] = []

    # ── 1. Init DB ────────────────────────────────────────────────────────────
    db_path = Path(settings.database_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        conn = get_connection(str(db_path))
        init_db(conn)
    except Exception as exc:
        log.critical("db_init_failed", error=str(exc))
        return 1

    if not dry_run:
        try:
            repo.insert_run_log(conn, run_id, json_path, dry_run)
        except Exception as exc:
            log.warning("run_log_insert_failed", error=str(exc))

    # ── 2. Parse JSON ─────────────────────────────────────────────────────────
    try:
        all_listings: list[RawListing] = parse_listings(json_path)
    except FileNotFoundError:
        log.critical("json_file_not_found", path=json_path)
        return 1
    except Exception as exc:
        log.critical("json_parse_failed", error=str(exc))
        return 1

    total_rows = len(all_listings)
    log.info("json_parsed", total_rows=total_rows)

    # ── 3. Dedup ──────────────────────────────────────────────────────────────
    seen_ids = repo.get_seen_listing_ids(conn)
    new_listings = [lst for lst in all_listings if lst.listing_id not in seen_ids]
    log.info(
        "dedup_complete",
        total=total_rows,
        new=len(new_listings),
        seen=len(seen_ids),
    )

    # ── 4. Pre-filter + insert raw listings ───────────────────────────────────
    pre_filter_rejected = 0
    passes_pre_filter: list[RawListing] = []

    for listing in new_listings:
        passed, reasons = apply_pre_filters(
            listing,
            max_rent_cad=settings.criteria_max_rent_cad,
        )

        if not dry_run:
            try:
                repo.insert_listing(
                    conn,
                    listing_id=listing.listing_id,
                    url=listing.url,
                    title=listing.title,
                    price_raw=listing.price_raw,
                    price_cents=listing.price_cents,
                    location_raw=listing.location_raw,
                    bedrooms=listing.bedrooms,
                    bathrooms=listing.bathrooms,
                    image_url=listing.image_url,
                    scraped_at=listing.scraped_at,
                    extra_fields=listing.extra_fields,
                    run_id=run_id,
                )
            except Exception as exc:
                log.error(
                    "insert_listing_failed",
                    listing_id=listing.listing_id,
                    error=str(exc),
                )

        if not passed:
            pre_filter_rejected += 1
            log.info(
                "pre_filter_rejected",
                listing_id=listing.listing_id,
                reasons=reasons,
            )
            if not dry_run:
                try:
                    repo.update_status(conn, listing.listing_id, "pre_filter_rejected")
                except Exception:
                    pass
        else:
            passes_pre_filter.append(listing)

    log.info(
        "pre_filter_complete",
        new=len(new_listings),
        rejected=pre_filter_rejected,
        proceeding=len(passes_pre_filter),
    )

    # ── 5. Re-notify listings that failed on a previous run ───────────────────
    if not dry_run and settings.telegram_configured():
        try:
            unnotified = repo.get_unnotified_passes(conn)
            for row in unnotified:
                try:
                    enriched = _rebuild_enriched(row)
                    result = _rebuild_filter_result(row)
                    ok = send_listing(
                        enriched,
                        result,
                        bot_token=settings.telegram_bot_token,
                        chat_id=settings.telegram_chat_id,
                        timeout_s=settings.telegram_request_timeout_seconds,
                    )
                    if ok:
                        repo.mark_notified(conn, row["listing_id"])
                        log.info("re_notification_sent", listing_id=row["listing_id"])
                    else:
                        log.warning(
                            "re_notification_failed",
                            listing_id=row["listing_id"],
                        )
                except Exception as exc:
                    log.error(
                        "re_notification_error",
                        listing_id=row.get("listing_id"),
                        error=str(exc),
                    )
        except Exception as exc:
            log.error("unnotified_passes_check_failed", error=str(exc))

    # ── 6. Scrape descriptions via Playwright ─────────────────────────────────
    scraped_ok = 0
    scrape_failed_count = 0
    enriched_listings: list[EnrichedListing] = []

    if passes_pre_filter:
        cap = settings.scraper_max_listings_per_run
        to_scrape = passes_pre_filter[:cap] if cap > 0 else passes_pre_filter

        try:
            headless = not headed and settings.playwright_headless
            enriched_listings = asyncio.run(
                scrape_all(
                    to_scrape,
                    cookies_path=settings.facebook_cookies_path,
                    headless=headless,
                    min_delay_s=settings.scraper_min_delay_seconds,
                    max_delay_s=settings.scraper_max_delay_seconds,
                    page_timeout_ms=settings.playwright_page_timeout_ms,
                )
            )
        except CookieExpiredError as exc:
            log.error("cookie_expired_abort", error=str(exc))
            if settings.telegram_configured():
                send_text_alert(
                    "⚠️ rent-finder: Facebook cookies have expired. "
                    "Please export fresh cookies and replace data/cookies.json.",
                    bot_token=settings.telegram_bot_token,
                    chat_id=settings.telegram_chat_id,
                )
            if not dry_run:
                try:
                    repo.insert_cookie_health(
                        conn,
                        is_valid=False,
                        failure_reason="login_redirect",
                        run_id=run_id,
                    )
                    repo.update_run_log(conn, run_id, exit_status="cookie_expired")
                except Exception:
                    pass
            conn.close()
            return 2

        except Exception as exc:
            log.error("scrape_all_failed", error=str(exc))
            errors.append(f"scrape_all: {exc}")
            enriched_listings = []

        # Persist scrape results
        for enriched in enriched_listings:
            src = enriched.description_source
            if src == "unavailable":
                scrape_failed_count += 1
                if not dry_run:
                    try:
                        repo.update_status(conn, enriched.listing_id, "unavailable")
                    except Exception:
                        pass
            else:
                scraped_ok += 1
                if not dry_run:
                    try:
                        repo.update_description(
                            conn,
                            enriched.listing_id,
                            enriched.description,
                            src,
                            scrape_attempts=1,
                        )
                    except Exception:
                        pass

    log.info(
        "scrape_complete",
        scraped_ok=scraped_ok,
        scrape_failed=scrape_failed_count,
    )

    # ── 7. Filter via OpenAI + 8. Notify via Telegram ─────────────────────────
    filter_passed = 0
    filter_rejected = 0
    notified = 0
    notify_failed_count = 0

    for enriched in enriched_listings:
        if enriched.description_source == "unavailable":
            continue

        # AI filter
        try:
            result = filter_listing(
                enriched,
                api_key=settings.openai_api_key,
                model=settings.openai_model,
                max_tokens=settings.openai_max_tokens,
            )
        except OpenAIAuthError as exc:
            log.critical("openai_auth_failed_abort", error=str(exc))
            if settings.telegram_configured():
                send_text_alert(
                    "⚠️ rent-finder: OpenAI API key is invalid. Pipeline aborted.",
                    bot_token=settings.telegram_bot_token,
                    chat_id=settings.telegram_chat_id,
                )
            if not dry_run:
                try:
                    repo.update_run_log(
                        conn,
                        run_id,
                        exit_status="crash",
                        error_summary=[str(exc)],
                    )
                except Exception:
                    pass
            conn.close()
            return 1

        except Exception as exc:
            log.error(
                "filter_listing_failed",
                listing_id=enriched.listing_id,
                error=str(exc),
            )
            errors.append(f"filter:{enriched.listing_id}: {exc}")
            continue

        is_pass = (
            result.decision == "PASS"
            and result.total_score >= settings.criteria_min_score
        )

        if is_pass:
            filter_passed += 1
            if not dry_run:
                try:
                    repo.update_filter_result(
                        conn,
                        enriched.listing_id,
                        result.decision,
                        result.total_score,
                        result.reasoning,
                        result.score_breakdown,
                        "filter_passed",
                    )
                except Exception:
                    pass

            log.info(
                "filter_passed",
                listing_id=enriched.listing_id,
                score=result.total_score,
                title=enriched.title,
            )

            # Telegram notification
            if settings.telegram_configured():
                ok = send_listing(
                    enriched,
                    result,
                    bot_token=settings.telegram_bot_token,
                    chat_id=settings.telegram_chat_id,
                    dry_run=dry_run,
                    timeout_s=settings.telegram_request_timeout_seconds,
                )
                if ok:
                    notified += 1
                    if not dry_run:
                        try:
                            repo.mark_notified(conn, enriched.listing_id)
                        except Exception:
                            pass
                else:
                    notify_failed_count += 1
                    if not dry_run:
                        try:
                            repo.mark_notify_failed(conn, enriched.listing_id)
                        except Exception:
                            pass
            else:
                # Telegram not configured — count as notified for stats
                notified += 1
                log.warning(
                    "telegram_not_configured",
                    listing_id=enriched.listing_id,
                )

        else:
            filter_rejected += 1
            log.info(
                "filter_rejected",
                listing_id=enriched.listing_id,
                score=result.total_score,
                decision=result.decision,
                reasons=result.rejection_reasons,
            )
            if not dry_run:
                try:
                    repo.update_filter_result(
                        conn,
                        enriched.listing_id,
                        result.decision,
                        result.total_score,
                        result.reasoning,
                        result.score_breakdown,
                        "filter_rejected",
                    )
                except Exception:
                    pass

    # ── 9. End-of-run summary ─────────────────────────────────────────────────
    duration_str = _format_duration(time.monotonic() - start_time)

    log.info(
        "run_complete",
        run_id=run_id,
        total_rows=total_rows,
        new_listings=len(new_listings),
        pre_filter_rejected=pre_filter_rejected,
        scraped_ok=scraped_ok,
        scrape_failed=scrape_failed_count,
        filter_passed=filter_passed,
        filter_rejected=filter_rejected,
        notified=notified,
        notify_failed=notify_failed_count,
        errors=len(errors),
        duration_str=duration_str,
    )

    if settings.telegram_configured() and settings.telegram_send_summary:
        try:
            send_summary(
                bot_token=settings.telegram_bot_token,
                chat_id=settings.telegram_chat_id,
                total_rows=total_rows,
                new_listings=len(new_listings),
                scraped_ok=scraped_ok,
                scrape_failed=scrape_failed_count,
                filter_passed=filter_passed,
                filter_rejected=filter_rejected,
                notified=notified,
                notify_failed=notify_failed_count,
                errors=len(errors),
                duration_str=duration_str,
                dry_run=dry_run,
            )
        except Exception as exc:
            log.error("summary_send_failed", error=str(exc))

    if not dry_run:
        try:
            repo.update_run_log(
                conn,
                run_id,
                finished_at=datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
                rows_in_csv=total_rows,
                new_listings=len(new_listings),
                pre_filter_rejected=pre_filter_rejected,
                scraped_ok=scraped_ok,
                scrape_failed=scrape_failed_count,
                filter_passed=filter_passed,
                filter_rejected=filter_rejected,
                notified=notified,
                notify_failed=notify_failed_count,
                exit_status="success" if not errors else "partial",
                error_summary=errors if errors else None,
            )
        except Exception as exc:
            log.warning("run_log_update_failed", error=str(exc))

    conn.close()
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

@click.command()
@click.option(
    "--json", "json_path",
    default=None,
    help="Path to the Apify JSON listing export. Overrides JSON_INPUT_PATH in .env.",
)
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help=(
        "Parse, scrape, and filter without writing to DB or sending notifications. "
        "The end-of-run summary IS still sent."
    ),
)
@click.option(
    "--headed",
    is_flag=True,
    default=False,
    help="Launch Playwright in visible (headed) mode for debugging selector issues.",
)
@click.option(
    "--once",
    is_flag=True,
    default=False,
    help="Run the pipeline once and exit (overrides --daemon).",
)
@click.option(
    "--daemon",
    is_flag=True,
    default=False,
    help="Start in scheduled daemon mode using the SCHEDULE_CRON setting.",
)
def main(
    json_path: str | None,
    dry_run: bool,
    headed: bool,
    once: bool,
    daemon: bool,
) -> None:
    """
    rent-finder: Automated Toronto rental listing filter and notifier.

    Reads a pre-scraped Facebook Marketplace JSON export, scrapes full
    descriptions via Playwright, filters with OpenAI GPT-4o-mini, and
    delivers matching listings to Telegram.
    """
    try:
        settings = Settings()  # type: ignore[call-arg]
    except Exception as exc:
        click.echo(f"ERROR: Configuration invalid — {exc}", err=True)
        sys.exit(1)

    configure_logging(
        log_dir=settings.log_dir,
        file_level=settings.log_level_file,
        console_level=settings.log_level_console,
    )

    log.info("rent_finder_start", dry_run=dry_run, headed=headed, daemon=daemon)
    log.debug("config_loaded", **settings.masked_summary())

    resolved_path = json_path or settings.json_input_path

    if daemon and not once:
        from rent_finder.scheduler import start_scheduler
        start_scheduler(
            settings=settings,
            json_path=resolved_path,
            dry_run=dry_run,
            headed=headed,
        )
        return

    run_id = str(uuid.uuid4())[:8]
    exit_code = run_pipeline(
        settings=settings,
        json_path=resolved_path,
        dry_run=dry_run,
        headed=headed,
        run_id=run_id,
    )
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
