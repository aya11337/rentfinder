"""
Unit tests for rent_finder.utils.retry and rent_finder.utils.logging_config.
"""

from __future__ import annotations

import logging
from pathlib import Path
from unittest.mock import patch

import pytest

from rent_finder.utils.logging_config import configure_logging, get_logger
from rent_finder.utils.retry import RetryError, retry_on


# ---------------------------------------------------------------------------
# retry_on decorator
# ---------------------------------------------------------------------------

class TestRetryOn:
    def test_success_on_first_try(self) -> None:
        call_count = 0

        @retry_on((ValueError,), max_attempts=3, base_delay=0.001, max_delay=0.001)
        def succeed() -> str:
            nonlocal call_count
            call_count += 1
            return "ok"

        result = succeed()
        assert result == "ok"
        assert call_count == 1

    def test_retries_on_specified_exception(self) -> None:
        call_count = 0

        @retry_on((ValueError,), max_attempts=3, base_delay=0.001, max_delay=0.001)
        def fail_once() -> str:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise ValueError("first attempt fails")
            return "recovered"

        result = fail_once()
        assert result == "recovered"
        assert call_count == 2

    def test_raises_after_max_attempts(self) -> None:
        call_count = 0

        @retry_on((ValueError,), max_attempts=3, base_delay=0.001, max_delay=0.001)
        def always_fail() -> str:
            nonlocal call_count
            call_count += 1
            raise ValueError("permanent failure")

        with pytest.raises(ValueError, match="permanent failure"):
            always_fail()
        assert call_count == 3

    def test_does_not_retry_on_unspecified_exception(self) -> None:
        call_count = 0

        @retry_on((ValueError,), max_attempts=3, base_delay=0.001, max_delay=0.001)
        def raise_type_error() -> None:
            nonlocal call_count
            call_count += 1
            raise TypeError("not retried")

        with pytest.raises(TypeError):
            raise_type_error()
        assert call_count == 1  # No retry for TypeError

    def test_jitter_false_uses_exponential_wait(self) -> None:
        """jitter=False should use wait_exponential; function still retries correctly."""
        call_count = 0

        @retry_on(
            (RuntimeError,),
            max_attempts=2,
            base_delay=0.001,
            max_delay=0.001,
            jitter=False,
        )
        def fail_once() -> str:
            nonlocal call_count
            call_count += 1
            if call_count < 2:
                raise RuntimeError("retry me")
            return "done"

        result = fail_once()
        assert result == "done"
        assert call_count == 2

    def test_return_value_propagated(self) -> None:
        @retry_on((OSError,), max_attempts=2, base_delay=0.001)
        def returns_dict() -> dict:
            return {"key": "value"}

        assert returns_dict() == {"key": "value"}

    def test_multiple_exception_types_retried(self) -> None:
        call_count = 0

        @retry_on(
            (ValueError, RuntimeError),
            max_attempts=3,
            base_delay=0.001,
            max_delay=0.001,
        )
        def fail_twice() -> str:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise ValueError("first")
            if call_count == 2:
                raise RuntimeError("second")
            return "third"

        result = fail_twice()
        assert result == "third"
        assert call_count == 3


# ---------------------------------------------------------------------------
# logging_config
# ---------------------------------------------------------------------------

class TestConfigureLogging:
    def test_creates_log_directory(self, tmp_path: Path) -> None:
        log_dir = tmp_path / "sublogs" / "nested"
        configure_logging(
            log_dir=str(log_dir),
            file_level="DEBUG",
            console_level="WARNING",
        )
        assert log_dir.exists()

    def test_log_file_created_in_directory(self, tmp_path: Path) -> None:
        # Reset root logger handlers so our call sets them up fresh
        root = logging.getLogger()
        original_handlers = root.handlers[:]
        root.handlers.clear()

        try:
            configure_logging(
                log_dir=str(tmp_path),
                file_level="DEBUG",
                console_level="WARNING",
            )
            log_file = tmp_path / "rent_finder.jsonl"
            assert log_file.exists()
        finally:
            # Restore handlers to avoid polluting other tests
            for h in root.handlers:
                h.close()
            root.handlers[:] = original_handlers

    def test_idempotent_does_not_add_duplicate_handlers(
        self, tmp_path: Path
    ) -> None:
        """Calling configure_logging twice must not double-add handlers."""
        root = logging.getLogger()
        original_handlers = root.handlers[:]
        root.handlers.clear()

        try:
            configure_logging(str(tmp_path), "DEBUG", "WARNING")
            count_after_first = len(root.handlers)
            configure_logging(str(tmp_path), "DEBUG", "WARNING")
            count_after_second = len(root.handlers)
            assert count_after_second == count_after_first
        finally:
            for h in root.handlers:
                h.close()
            root.handlers[:] = original_handlers


class TestGetLogger:
    def test_returns_logger(self) -> None:
        log = get_logger("test.module")
        assert log is not None

    def test_different_names_return_different_loggers(self) -> None:
        log_a = get_logger("module.a")
        log_b = get_logger("module.b")
        # Both are valid loggers; structlog bound loggers are distinct objects
        assert log_a is not log_b
