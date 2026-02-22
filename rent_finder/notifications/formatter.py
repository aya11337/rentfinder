"""
Telegram MarkdownV2 message formatter for rent-finder.

Provides:
  escape_md(text)           — Escape all 18 MarkdownV2 special characters.
  format_listing_message()  — Full listing notification with score breakdown.
  format_summary_message()  — End-of-run pipeline statistics summary.

Telegram MarkdownV2 special characters (must all be escaped outside link syntax):
  _ * [ ] ( ) ~ ` > # + - = | { } . !

URLs inside [text](URL) link syntax are NOT escaped — they must be raw valid URLs.
Telegram message hard limit: 4096 characters. Messages are truncated at the
reasoning field if necessary to stay within this limit.
"""

from __future__ import annotations

import re

from rent_finder.filtering.openai_client import FilterResult
from rent_finder.ingestion.models import EnrichedListing

# Telegram's hard character limit for a single text message
_TELEGRAM_MAX_CHARS = 4096

# Characters that must be escaped in MarkdownV2 mode
_MD_SPECIAL = re.compile(r"([_*\[\]()~`>#+\-=|{}.!\\])")


def escape_md(text: str | None) -> str:
    """
    Escape all MarkdownV2 special characters in user-supplied text.

    None input returns an empty string.
    URLs used inside [text](URL) syntax must NOT pass through this function.
    """
    if text is None:
        return ""
    return _MD_SPECIAL.sub(r"\\\1", text)


# ---------------------------------------------------------------------------
# Listing notification message
# ---------------------------------------------------------------------------

def format_listing_message(listing: EnrichedListing, result: FilterResult) -> str:
    """
    Build the full MarkdownV2 Telegram message for a matched listing.

    Truncates the reasoning field if the total message exceeds 4096 characters.
    """
    breakdown = result.score_breakdown

    title = escape_md(listing.title or "Untitled Listing")
    price = escape_md(listing.price_raw or "Price not specified")
    location = escape_md(listing.location_raw or "Location not specified")
    reasoning = escape_md(result.reasoning)

    def _score(key: str) -> int:
        return breakdown.get(key, 0)

    lines = [
        f"🏠 *New Rental Match* \\- Score: {result.total_score}/24",
        "",
        f"*{title}*",
        "",
        f"💰 *Price:* {price}",
        f"📍 *Location:* {location}",
    ]

    if listing.bedrooms:
        lines.append(f"🛏 *Bedrooms:* {escape_md(listing.bedrooms)}")
    if listing.bathrooms:
        lines.append(f"🚿 *Bathrooms:* {escape_md(listing.bathrooms)}")

    lines += [
        "",
        "━━━━━━━━━━━━━━━━",
        "📊 *Score Breakdown*",
        "━━━━━━━━━━━━━━━━",
        f"• Neighbourhood: {_score('neighbourhood')}/3",
        f"• Laundry: {_score('laundry')}/3",
        f"• Transit: {_score('transit')}/3",
        f"• Natural Light: {_score('natural_light')}/3",
        f"• Condition: {_score('condition')}/3",
        f"• Parking: {_score('parking')}/3",
        f"• Furnished: {_score('furnished')}/3",
        f"• Move\\-in: {_score('move_in_timing')}/3",
    ]

    if result.scam_flag:
        lines += ["", "⚠️ *SCAM FLAG RAISED*"]

    lines += [
        "",
        "📝 *Why it matched:*",
        f"_{reasoning}_",
        "",
        f"🔗 [View on Facebook]({listing.url})",
    ]

    if not listing.description:
        lines += [
            "",
            "⚠️ _Description unavailable \\— filtered on title and price only_",
        ]

    message = "\n".join(lines)

    # Truncate reasoning if message is too long
    if len(message) > _TELEGRAM_MAX_CHARS:
        max_reasoning_chars = max(20, len(reasoning) - (len(message) - _TELEGRAM_MAX_CHARS) - 10)
        truncated = reasoning[:max_reasoning_chars] + escape_md("…")
        message = message.replace(f"_{reasoning}_", f"_{truncated}_")

    return message[:_TELEGRAM_MAX_CHARS]


# ---------------------------------------------------------------------------
# End-of-run summary message
# ---------------------------------------------------------------------------

def format_summary_message(
    *,
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
) -> str:
    """Build the end-of-run pipeline statistics summary message."""
    dry_run_badge = "\n🧪 _DRY RUN \\— no notifications sent_" if dry_run else ""
    notify_failed_line = (
        f"\n📭 _Retry pending: {notify_failed}_" if notify_failed > 0 else ""
    )
    duration_line = f"\n⏱ _Duration: {escape_md(duration_str)}_" if duration_str else ""

    lines = [
        f"📊 *rent\\-finder Run Summary*{dry_run_badge}",
        "",
        f"📥 CSV rows: {total_rows}",
        f"🆕 New listings: {new_listings}",
        f"🔍 Scraped OK: {scraped_ok} \\({scrape_failed} failed\\)",
        f"🤖 Filter: {filter_passed} passed / {filter_rejected} rejected",
        f"📨 Notified: {notified}{notify_failed_line}",
        f"❌ Errors: {errors}",
    ]

    if duration_line:
        lines.append(duration_line)

    return "\n".join(lines)
