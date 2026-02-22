"""
APScheduler daemon for rent-finder.

Invoked when --daemon is passed to the CLI. Runs the pipeline on a cron
schedule defined by SCHEDULE_CRON and SCHEDULE_TIMEZONE in .env.
"""

from __future__ import annotations

import signal
import sys
import uuid

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

from rent_finder.config import Settings
from rent_finder.utils.logging_config import get_logger

log = get_logger(__name__)


def start_scheduler(
    *,
    settings: Settings,
    json_path: str,
    dry_run: bool,
    headed: bool,
) -> None:
    """
    Start the APScheduler blocking scheduler.

    Blocks until SIGTERM or KeyboardInterrupt. Each cron tick invokes
    run_pipeline() with a fresh run_id.
    """
    # Import here to avoid circular imports (scheduler ← main ← scheduler)
    from rent_finder.main import run_pipeline

    scheduler = BlockingScheduler()
    trigger = CronTrigger.from_crontab(
        settings.schedule_cron,
        timezone=settings.schedule_timezone,
    )

    def _run_job() -> None:
        run_id = str(uuid.uuid4())[:8]
        log.info(
            "scheduled_run_start",
            run_id=run_id,
            cron=settings.schedule_cron,
        )
        exit_code = run_pipeline(
            settings=settings,
            json_path=json_path,
            dry_run=dry_run,
            headed=headed,
            run_id=run_id,
        )
        log.info("scheduled_run_complete", run_id=run_id, exit_code=exit_code)

    scheduler.add_job(
        _run_job,
        trigger,
        id="rent_finder_pipeline",
        max_instances=1,  # Prevent overlapping runs
    )

    # Graceful shutdown on SIGTERM (Unix / WSL only; no-op on Windows)
    if hasattr(signal, "SIGTERM"):
        def _handle_sigterm(signum: int, frame: object) -> None:
            log.info("sigterm_received_shutting_down")
            scheduler.shutdown(wait=True)
            sys.exit(0)

        signal.signal(signal.SIGTERM, _handle_sigterm)

    log.info(
        "scheduler_started",
        cron=settings.schedule_cron,
        timezone=settings.schedule_timezone,
        dry_run=dry_run,
    )

    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        log.info("scheduler_stopped")
