"""
Shared pytest fixtures for all test modules.

Fixtures provided:
- tmp_db_conn  : In-memory SQLite connection with schema applied
- mock_settings: Settings instance with safe test values (no real API keys)
- sample_csv_path: Path to the fixture CSV file
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from rent_finder.storage.database import get_connection, init_db


@pytest.fixture
def tmp_db_conn() -> sqlite3.Connection:
    """
    Return an in-memory SQLite connection with the full schema initialised.

    Each test that uses this fixture gets a fresh, isolated database.
    The connection is closed automatically after the test.
    """
    conn = get_connection(":memory:")
    init_db(conn)
    yield conn
    conn.close()


@pytest.fixture
def sample_json_path() -> Path:
    """Path to the sample Apify JSON fixture file."""
    return Path(__file__).parent / "fixtures" / "sample_listings.json"


@pytest.fixture
def mock_settings():
    """
    A Settings-like MagicMock with safe defaults for unit tests.

    Tests that need specific values should override individual attributes:
        mock_settings.criteria_max_rent_cad = 1500
    """
    s = MagicMock()
    s.database_path = ":memory:"
    s.json_input_path = "input/marketplace_export.json"
    s.facebook_cookies_path = "tests/fixtures/sample_cookies.json"
    s.scraper_min_delay_seconds = 0.01
    s.scraper_max_delay_seconds = 0.02
    s.scraper_max_listings_per_run = 0
    s.playwright_headless = True
    s.playwright_page_timeout_ms = 5000
    s.openai_api_key = "sk-test-fake-key-for-unit-tests"
    s.openai_model = "gpt-4o-mini"
    s.openai_max_tokens = 600
    s.criteria_max_rent_cad = 1600
    s.criteria_require_pet_friendly = False
    s.criteria_min_score = 12
    s.criteria_move_in_date = "2026-04-01"
    s.telegram_bot_token = "123456789:FAKE_TOKEN_FOR_TESTS"
    s.telegram_chat_id = "999999999"
    s.telegram_send_summary = True
    s.telegram_request_timeout_seconds = 5
    s.log_dir = "logs"
    s.log_level_file = "DEBUG"
    s.log_level_console = "WARNING"
    s.telegram_configured.return_value = True
    return s
