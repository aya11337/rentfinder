"""
Configuration loader for rent-finder.

All settings are read from the .env file (or environment variables).
Validation happens at import time — missing required fields raise a clear error
before any pipeline code runs.
"""

from __future__ import annotations

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",  # Ignore unknown env vars
    )

    # ── Database ─────────────────────────────────────────────────────────────
    database_path: str = Field(default="data/rent_finder.db")

    # ── Input ─────────────────────────────────────────────────────────────────
    json_input_path: str = Field(default="input/marketplace_export.json")

    # ── Facebook / Playwright ─────────────────────────────────────────────────
    facebook_cookies_path: str = Field(default="data/cookies.json")
    scraper_min_delay_seconds: float = Field(default=4.0, ge=2.0)
    scraper_max_delay_seconds: float = Field(default=8.0, ge=2.0)
    scraper_max_listings_per_run: int = Field(default=0, ge=0)
    playwright_headless: bool = Field(default=True)
    playwright_page_timeout_ms: int = Field(default=30000, ge=5000)

    # ── OpenAI ────────────────────────────────────────────────────────────────
    openai_api_key: str = Field(..., min_length=10)  # Required
    openai_model: str = Field(default="gpt-4o-mini")
    openai_max_tokens: int = Field(default=600, ge=100, le=4096)

    # ── Rental Criteria ───────────────────────────────────────────────────────
    criteria_max_rent_cad: int = Field(default=1600, ge=500, le=10000)
    criteria_require_pet_friendly: bool = Field(default=False)
    criteria_min_score: int = Field(default=12, ge=0, le=24)
    criteria_move_in_date: str = Field(default="2026-04-01")

    # ── Telegram ──────────────────────────────────────────────────────────────
    telegram_bot_token: str = Field(default="")
    telegram_chat_id: str = Field(default="")
    telegram_send_summary: bool = Field(default=True)
    telegram_request_timeout_seconds: int = Field(default=15, ge=5, le=60)

    # ── Scheduling ────────────────────────────────────────────────────────────
    schedule_cron: str = Field(default="0 8,18 * * *")
    schedule_timezone: str = Field(default="America/Toronto")

    # ── Logging ───────────────────────────────────────────────────────────────
    log_dir: str = Field(default="logs")
    log_level_file: str = Field(default="DEBUG")
    log_level_console: str = Field(default="INFO")

    @field_validator("scraper_max_delay_seconds")
    @classmethod
    def max_delay_must_exceed_min(cls, v: float, info: object) -> float:
        # Access already-validated field via info.data
        data = getattr(info, "data", {})
        min_delay = data.get("scraper_min_delay_seconds", 4.0)
        if v < min_delay:
            raise ValueError(
                f"SCRAPER_MAX_DELAY_SECONDS ({v}) must be >= "
                f"SCRAPER_MIN_DELAY_SECONDS ({min_delay})"
            )
        return v

    @field_validator("log_level_file", "log_level_console")
    @classmethod
    def valid_log_level(cls, v: str) -> str:
        valid = {"TRACE", "DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        upper = v.upper()
        if upper not in valid:
            raise ValueError(f"Log level must be one of {valid}, got {v!r}")
        return upper

    def telegram_configured(self) -> bool:
        """Return True if Telegram credentials are present and non-placeholder."""
        return bool(self.telegram_bot_token) and bool(self.telegram_chat_id) and \
               self.telegram_bot_token != "123456789:AABBccDD..."

    def masked_summary(self) -> dict[str, object]:
        """Return config dict safe for logging (secrets masked)."""
        return {
            "database_path": self.database_path,
            "json_input_path": self.json_input_path,
            "openai_model": self.openai_model,
            "openai_api_key": self.openai_api_key[:12] + "...",
            "playwright_headless": self.playwright_headless,
            "criteria_max_rent_cad": self.criteria_max_rent_cad,
            "criteria_min_score": self.criteria_min_score,
            "telegram_configured": self.telegram_configured(),
            "log_level_file": self.log_level_file,
            "log_level_console": self.log_level_console,
        }
