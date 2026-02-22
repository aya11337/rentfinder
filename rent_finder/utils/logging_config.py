"""
Structured logging configuration for rent-finder.

Sets up structlog with:
- JSON output to a daily-rotating file (machine-readable for future tooling)
- Pretty-printed output to console (human-readable during development)

Usage:
    from rent_finder.utils.logging_config import configure_logging, get_logger
    configure_logging(log_dir="logs", file_level="DEBUG", console_level="INFO")
    log = get_logger(__name__)
    log.info("event_name", key="value")
"""

from __future__ import annotations

import logging
import logging.handlers
import sys
from pathlib import Path

import structlog


def configure_logging(
    log_dir: str = "logs",
    file_level: str = "DEBUG",
    console_level: str = "INFO",
) -> None:
    """
    Configure structlog for the application.

    Must be called once at pipeline startup before any log.info() calls.
    Subsequent calls are idempotent (handlers are not duplicated).
    """
    log_path = Path(log_dir)
    log_path.mkdir(parents=True, exist_ok=True)

    # Map level names to logging constants
    file_level_int = getattr(logging, file_level.upper(), logging.DEBUG)
    console_level_int = getattr(logging, console_level.upper(), logging.INFO)

    # Root logger — structlog routes through this
    root_logger = logging.getLogger()

    # Avoid adding duplicate handlers on repeated calls
    if not root_logger.handlers:
        root_logger.setLevel(logging.DEBUG)  # Let handlers filter individually

        # ── File handler: JSON, daily rotation, 14-day retention ──────────────
        file_handler = logging.handlers.TimedRotatingFileHandler(
            filename=log_path / "rent_finder.jsonl",
            when="midnight",
            backupCount=14,
            encoding="utf-8",
            utc=True,
        )
        file_handler.setLevel(file_level_int)

        # ── Console handler: pretty-printed ───────────────────────────────────
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setLevel(console_level_int)

        root_logger.addHandler(file_handler)
        root_logger.addHandler(console_handler)

    # Shared processors for both outputs
    shared_processors: list[structlog.types.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]

    structlog.configure(
        processors=shared_processors
        + [
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.DEBUG),
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

    # File formatter: JSON (one object per line)
    file_formatter = structlog.stdlib.ProcessorFormatter(
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.processors.JSONRenderer(),
        ],
    )

    # Console formatter: human-readable with colours if terminal supports it
    console_formatter = structlog.stdlib.ProcessorFormatter(
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.dev.ConsoleRenderer(
                colors=sys.stdout.isatty(),
                exception_formatter=structlog.dev.plain_traceback,
            ),
        ],
    )

    # Apply formatters to their respective handlers
    for handler in root_logger.handlers:
        if isinstance(handler, logging.handlers.TimedRotatingFileHandler):
            handler.setFormatter(file_formatter)
        elif isinstance(handler, logging.StreamHandler):
            handler.setFormatter(console_formatter)


def get_logger(name: str = __name__) -> structlog.stdlib.BoundLogger:
    """Return a bound structlog logger for the given module name."""
    return structlog.get_logger(name)
