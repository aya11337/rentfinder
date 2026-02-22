"""
Generic retry decorator for rent-finder.

Wraps tenacity to provide a clean, consistent retry interface used by:
- openai_client.py  (RateLimitError, APIConnectionError)
- telegram.py       (httpx.HTTPError, httpx.ConnectError)

Usage:
    from rent_finder.utils.retry import retry_on

    @retry_on(exceptions=(SomeError,), max_attempts=3, base_delay=2.0)
    def call_external_api() -> dict:
        ...
"""

from __future__ import annotations

import functools
from collections.abc import Callable
from typing import Any, TypeVar

from tenacity import (
    RetryError,
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
    wait_random_exponential,
)

from rent_finder.utils.logging_config import get_logger

log = get_logger(__name__)

F = TypeVar("F", bound=Callable[..., Any])


def retry_on(
    exceptions: tuple[type[Exception], ...],
    max_attempts: int = 3,
    base_delay: float = 2.0,
    max_delay: float = 30.0,
    jitter: bool = True,
) -> Callable[[F], F]:
    """
    Decorator: retry the wrapped function on specified exception types.

    Args:
        exceptions:   Tuple of exception classes that trigger a retry.
        max_attempts: Maximum total attempts (including the first call).
        base_delay:   Base wait time in seconds between retries.
        max_delay:    Maximum wait time in seconds (exponential backoff cap).
        jitter:       Add randomisation to wait times to avoid thundering herd.

    On final failure after all attempts, the last exception is re-raised.
    Each retry attempt is logged at WARNING level.
    """
    wait = (
        wait_random_exponential(min=base_delay, max=max_delay)
        if jitter
        else wait_exponential(multiplier=base_delay, max=max_delay)
    )

    def decorator(func: F) -> F:
        @retry(
            retry=retry_if_exception_type(exceptions),
            stop=stop_after_attempt(max_attempts),
            wait=wait,
            reraise=True,
            before_sleep=_log_retry_attempt,
        )
        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            return func(*args, **kwargs)

        return wrapper  # type: ignore[return-value]

    return decorator


def _log_retry_attempt(retry_state: Any) -> None:
    """Log each retry attempt before sleeping."""
    attempt = retry_state.attempt_number
    exc = retry_state.outcome.exception() if retry_state.outcome else None
    exc_name = type(exc).__name__ if exc else "Unknown"
    next_wait = getattr(retry_state.next_action, "sleep", None)
    log.warning(
        "retry_attempt",
        attempt=attempt,
        exception=exc_name,
        next_wait_seconds=round(next_wait, 2) if next_wait else None,
    )


__all__ = ["retry_on", "RetryError"]
