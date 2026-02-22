"""
Async rate limiter for Playwright page loads.

Inserts a random uniform delay between min_s and max_s seconds before
each page navigation to avoid triggering Facebook's bot detection.
"""

from __future__ import annotations

import asyncio
import random

from rent_finder.utils.logging_config import get_logger

log = get_logger(__name__)


class RateLimiter:
    """Simple async token-bucket rate limiter using random uniform delay."""

    async def acquire(self, min_s: float, max_s: float) -> None:
        """Sleep for a random duration in [min_s, max_s] seconds."""
        delay = random.uniform(min_s, max_s)
        log.debug("rate_limit_delay", seconds=round(delay, 2))
        await asyncio.sleep(delay)
