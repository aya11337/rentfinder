"""
Unit tests for rent_finder.config.Settings.

Tests cover field validators, helper methods, and the masked_summary output.
Settings are instantiated directly with keyword arguments (no .env file needed).
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from rent_finder.config import Settings


def _base_settings(**overrides) -> Settings:
    """Return a Settings instance with safe test defaults."""
    defaults = dict(
        database_path="data/test.db",
        json_input_path="input/test.json",
        facebook_cookies_path="data/cookies.json",
        scraper_min_delay_seconds=4.0,
        scraper_max_delay_seconds=8.0,
        playwright_headless=True,
        playwright_page_timeout_ms=30000,
        openai_api_key="sk-test-fake-key-1234567890",
        openai_model="gpt-4o-mini",
        openai_max_tokens=600,
        criteria_max_rent_cad=1600,
        criteria_require_pet_friendly=False,
        criteria_min_score=12,
        criteria_move_in_date="2026-04-01",
        telegram_bot_token="",
        telegram_chat_id="",
        telegram_send_summary=True,
        telegram_request_timeout_seconds=15,
        schedule_cron="0 8,18 * * *",
        schedule_timezone="America/Toronto",
        log_dir="logs",
        log_level_file="DEBUG",
        log_level_console="INFO",
    )
    defaults.update(overrides)
    return Settings(**defaults)


# ---------------------------------------------------------------------------
# max_delay_must_exceed_min validator
# ---------------------------------------------------------------------------

class TestMaxDelayValidator:
    def test_valid_min_less_than_max(self) -> None:
        s = _base_settings(scraper_min_delay_seconds=4.0, scraper_max_delay_seconds=8.0)
        assert s.scraper_max_delay_seconds == 8.0

    def test_equal_min_and_max_is_valid(self) -> None:
        s = _base_settings(scraper_min_delay_seconds=4.0, scraper_max_delay_seconds=4.0)
        assert s.scraper_max_delay_seconds == 4.0

    def test_max_less_than_min_raises(self) -> None:
        with pytest.raises(ValidationError, match="SCRAPER_MAX_DELAY_SECONDS"):
            _base_settings(scraper_min_delay_seconds=6.0, scraper_max_delay_seconds=3.0)


# ---------------------------------------------------------------------------
# valid_log_level validator
# ---------------------------------------------------------------------------

class TestLogLevelValidator:
    @pytest.mark.parametrize("level", ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"])
    def test_valid_levels_accepted(self, level: str) -> None:
        s = _base_settings(log_level_file=level, log_level_console=level)
        assert s.log_level_file == level
        assert s.log_level_console == level

    def test_level_is_uppercased(self) -> None:
        s = _base_settings(log_level_file="debug", log_level_console="warning")
        assert s.log_level_file == "DEBUG"
        assert s.log_level_console == "WARNING"

    def test_invalid_level_raises(self) -> None:
        with pytest.raises(ValidationError):
            _base_settings(log_level_file="VERBOSE")

    def test_invalid_console_level_raises(self) -> None:
        with pytest.raises(ValidationError):
            _base_settings(log_level_console="VERBOSE")


# ---------------------------------------------------------------------------
# telegram_configured helper
# ---------------------------------------------------------------------------

class TestTelegramConfigured:
    def test_empty_token_returns_false(self) -> None:
        s = _base_settings(telegram_bot_token="", telegram_chat_id="12345")
        assert s.telegram_configured() is False

    def test_empty_chat_id_returns_false(self) -> None:
        s = _base_settings(telegram_bot_token="123:REAL_TOKEN", telegram_chat_id="")
        assert s.telegram_configured() is False

    def test_placeholder_token_returns_false(self) -> None:
        s = _base_settings(
            telegram_bot_token="123456789:AABBccDD...",
            telegram_chat_id="12345",
        )
        assert s.telegram_configured() is False

    def test_real_credentials_return_true(self) -> None:
        s = _base_settings(
            telegram_bot_token="987654321:REAL_TOKEN_HERE",
            telegram_chat_id="123456789",
        )
        assert s.telegram_configured() is True


# ---------------------------------------------------------------------------
# masked_summary helper
# ---------------------------------------------------------------------------

class TestMaskedSummary:
    def test_api_key_is_masked(self) -> None:
        s = _base_settings(openai_api_key="sk-secretsecretkey12345")
        summary = s.masked_summary()
        assert "sk-secretse" in summary["openai_api_key"]  # type: ignore[operator]
        assert "secretkey12345" not in summary["openai_api_key"]  # type: ignore[operator]
        assert "..." in summary["openai_api_key"]  # type: ignore[operator]

    def test_summary_contains_expected_keys(self) -> None:
        s = _base_settings()
        summary = s.masked_summary()
        for key in [
            "database_path", "openai_model", "openai_api_key",
            "playwright_headless", "criteria_max_rent_cad",
            "telegram_configured", "log_level_file",
        ]:
            assert key in summary, f"Expected key '{key}' in masked_summary"

    def test_telegram_configured_reflects_settings(self) -> None:
        s_no_tg = _base_settings()
        assert s_no_tg.masked_summary()["telegram_configured"] is False

        s_with_tg = _base_settings(
            telegram_bot_token="987654321:REAL", telegram_chat_id="123"
        )
        assert s_with_tg.masked_summary()["telegram_configured"] is True
