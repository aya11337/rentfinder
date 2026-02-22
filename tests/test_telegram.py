"""
Unit tests for rent_finder.notifications.telegram

All HTTP calls are mocked — no real Telegram API requests.
Tests verify: dry-run behaviour, retry logic, rate-limit handling,
truncation on long messages, and summary always-sends semantics.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import httpx

from rent_finder.filtering.openai_client import FilterResult
from rent_finder.ingestion.models import EnrichedListing
from rent_finder.notifications.telegram import (
    _send_text,
    send_listing,
    send_summary,
    send_text_alert,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

BOT_TOKEN = "123456789:FAKE_TOKEN_FOR_TESTS"
CHAT_ID = "999999999"

_BREAKDOWN = {
    "neighbourhood": 3, "laundry": 2, "transit": 2, "natural_light": 3,
    "condition": 2, "parking": 3, "furnished": 2, "move_in_timing": 3,
}


def _result(score: int = 20) -> FilterResult:
    return FilterResult(
        decision="PASS",
        rejection_reasons=[],
        scam_flag=False,
        total_score=score,
        score_breakdown=_BREAKDOWN,
        reasoning="Good listing in North York with parking.",
    )


def _listing(listing_id: str = "NOTIF001") -> EnrichedListing:
    return EnrichedListing(
        listing_id=listing_id,
        url=f"https://www.facebook.com/marketplace/item/{listing_id}/",
        title="Bright 1BR North York",
        price_raw="CA$1,400",
        price_cents=140000,
        location_raw="North York, Ontario",
        bedrooms="1",
        bathrooms="1",
        image_url=None,
        scraped_at=None,
        extra_fields={},
        description="Walkout basement, parking included.",
        description_source="primary",
    )


def _ok_response() -> MagicMock:
    resp = MagicMock()
    resp.status_code = 200
    return resp


def _error_response(status: int, body: dict | None = None) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status
    resp.text = str(body or {})
    resp.json.return_value = body or {}
    return resp


# ---------------------------------------------------------------------------
# _send_text — core send function
# ---------------------------------------------------------------------------

class TestSendText:
    def test_success_returns_true(self) -> None:
        with patch("rent_finder.notifications.telegram.httpx.Client") as mock_cls:
            mock_client = MagicMock()
            mock_cls.return_value.__enter__ = MagicMock(return_value=mock_client)
            mock_cls.return_value.__exit__ = MagicMock(return_value=False)
            mock_client.post.return_value = _ok_response()

            result = _send_text(BOT_TOKEN, CHAT_ID, "Hello world")
            assert result is True

    def test_http_error_returns_false(self) -> None:
        with patch("rent_finder.notifications.telegram.httpx.Client") as mock_cls:
            mock_client = MagicMock()
            mock_cls.return_value.__enter__ = MagicMock(return_value=mock_client)
            mock_cls.return_value.__exit__ = MagicMock(return_value=False)
            mock_client.post.return_value = _error_response(500)

            result = _send_text(BOT_TOKEN, CHAT_ID, "Hello")
            assert result is False

    def test_rate_limit_sleeps_and_retries(self) -> None:
        rate_limit_resp = _error_response(429, {"parameters": {"retry_after": 2}})
        with patch("rent_finder.notifications.telegram.httpx.Client") as mock_cls:
            with patch("rent_finder.notifications.telegram.time.sleep") as mock_sleep:
                mock_client = MagicMock()
                mock_cls.return_value.__enter__ = MagicMock(return_value=mock_client)
                mock_cls.return_value.__exit__ = MagicMock(return_value=False)
                mock_client.post.side_effect = [rate_limit_resp, _ok_response()]

                result = _send_text(BOT_TOKEN, CHAT_ID, "Hello")

        mock_sleep.assert_called_with(2)
        assert result is True

    def test_network_error_retried(self) -> None:
        with patch("rent_finder.notifications.telegram.httpx.Client") as mock_cls:
            with patch("rent_finder.notifications.telegram.time.sleep"):
                mock_client = MagicMock()
                mock_cls.return_value.__enter__ = MagicMock(return_value=mock_client)
                mock_cls.return_value.__exit__ = MagicMock(return_value=False)
                mock_client.post.side_effect = [
                    httpx.ConnectError("connection refused"),
                    _ok_response(),
                ]

                result = _send_text(BOT_TOKEN, CHAT_ID, "Hello")
        assert result is True

    def test_three_network_errors_returns_false(self) -> None:
        with patch("rent_finder.notifications.telegram.httpx.Client") as mock_cls:
            with patch("rent_finder.notifications.telegram.time.sleep"):
                mock_client = MagicMock()
                mock_cls.return_value.__enter__ = MagicMock(return_value=mock_client)
                mock_cls.return_value.__exit__ = MagicMock(return_value=False)
                mock_client.post.side_effect = httpx.ConnectError("refused")

                result = _send_text(BOT_TOKEN, CHAT_ID, "Hello")
        assert result is False

    def test_400_returns_false_no_retry(self) -> None:
        with patch("rent_finder.notifications.telegram.httpx.Client") as mock_cls:
            mock_client = MagicMock()
            mock_cls.return_value.__enter__ = MagicMock(return_value=mock_client)
            mock_cls.return_value.__exit__ = MagicMock(return_value=False)
            mock_client.post.return_value = _error_response(400, {"description": "too long"})

            result = _send_text(BOT_TOKEN, CHAT_ID, "Hello")
        assert result is False
        # Should not retry — only 1 call
        assert mock_client.post.call_count == 1


# ---------------------------------------------------------------------------
# send_listing
# ---------------------------------------------------------------------------

class TestSendListing:
    def test_dry_run_no_http_call(self) -> None:
        with patch("rent_finder.notifications.telegram._send_text") as mock_send:
            result = send_listing(
                _listing(), _result(),
                bot_token=BOT_TOKEN, chat_id=CHAT_ID, dry_run=True,
            )
        assert result is True
        mock_send.assert_not_called()

    def test_dry_run_returns_true_always(self) -> None:
        result = send_listing(
            _listing(), _result(),
            bot_token=BOT_TOKEN, chat_id=CHAT_ID, dry_run=True,
        )
        assert result is True

    def test_successful_send_returns_true(self) -> None:
        with patch("rent_finder.notifications.telegram._send_text", return_value=True):
            result = send_listing(
                _listing(), _result(),
                bot_token=BOT_TOKEN, chat_id=CHAT_ID,
            )
        assert result is True

    def test_failed_send_returns_false(self) -> None:
        with patch("rent_finder.notifications.telegram._send_text", return_value=False):
            result = send_listing(
                _listing(), _result(),
                bot_token=BOT_TOKEN, chat_id=CHAT_ID,
            )
        assert result is False

    def test_long_message_truncated_on_retry(self) -> None:
        """If first send fails and message > _MAX_TRUNCATED, a truncated retry is attempted."""
        import rent_finder.notifications.telegram as tg_module

        long_listing = _listing()
        long_result = _result()

        call_count = 0

        def mock_send(token, chat, text, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1 and len(text) > tg_module._MAX_TRUNCATED:
                return False
            return True

        with patch("rent_finder.notifications.telegram.format_listing_message") as mock_fmt:
            mock_fmt.return_value = "X" * (tg_module._MAX_TRUNCATED + 100)
            with patch("rent_finder.notifications.telegram._send_text", side_effect=mock_send):
                result = send_listing(
                    long_listing, long_result,
                    bot_token=BOT_TOKEN, chat_id=CHAT_ID,
                )

        assert result is True
        assert call_count == 2


# ---------------------------------------------------------------------------
# send_summary
# ---------------------------------------------------------------------------

class TestSendSummary:
    def _call_summary(self, dry_run: bool = False, **kwargs) -> bool:
        defaults = dict(
            bot_token=BOT_TOKEN, chat_id=CHAT_ID,
            total_rows=50, new_listings=10,
            scraped_ok=9, scrape_failed=1,
            filter_passed=2, filter_rejected=7,
            notified=2, notify_failed=0, errors=1,
            duration_str="1m 15s", dry_run=dry_run,
        )
        defaults.update(kwargs)
        return send_summary(**defaults)

    def test_summary_sends_in_normal_mode(self) -> None:
        with patch("rent_finder.notifications.telegram._send_text", return_value=True) as mock_s:
            result = self._call_summary(dry_run=False)
        assert result is True
        mock_s.assert_called_once()

    def test_summary_sends_even_in_dry_run(self) -> None:
        """send_summary ALWAYS sends — even in dry-run (operational visibility)."""
        with patch("rent_finder.notifications.telegram._send_text", return_value=True) as mock_s:
            result = self._call_summary(dry_run=True)
        assert result is True
        mock_s.assert_called_once()

    def test_summary_message_includes_dry_run_badge(self) -> None:
        captured: list[str] = []

        def capture(token, chat, text, **kwargs):
            captured.append(text)
            return True

        with patch("rent_finder.notifications.telegram._send_text", side_effect=capture):
            self._call_summary(dry_run=True)

        assert captured
        assert "DRY RUN" in captured[0]

    def test_summary_failure_returns_false(self) -> None:
        with patch("rent_finder.notifications.telegram._send_text", return_value=False):
            result = self._call_summary()
        assert result is False


# ---------------------------------------------------------------------------
# send_text_alert
# ---------------------------------------------------------------------------

class TestSendTextAlert:
    def test_alert_sent_returns_true(self) -> None:
        with patch("rent_finder.notifications.telegram.httpx.Client") as mock_cls:
            mock_client = MagicMock()
            mock_cls.return_value.__enter__ = MagicMock(return_value=mock_client)
            mock_cls.return_value.__exit__ = MagicMock(return_value=False)
            mock_client.post.return_value = _ok_response()

            result = send_text_alert(
                "⚠️ Cookies expired",
                bot_token=BOT_TOKEN, chat_id=CHAT_ID,
            )
        assert result is True

    def test_alert_network_error_returns_false(self) -> None:
        with patch("rent_finder.notifications.telegram.httpx.Client") as mock_cls:
            mock_client = MagicMock()
            mock_cls.return_value.__enter__ = MagicMock(return_value=mock_client)
            mock_cls.return_value.__exit__ = MagicMock(return_value=False)
            mock_client.post.side_effect = httpx.ConnectError("no network")

            result = send_text_alert(
                "Alert", bot_token=BOT_TOKEN, chat_id=CHAT_ID,
            )
        assert result is False
