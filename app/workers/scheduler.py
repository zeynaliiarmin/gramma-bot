"""Gramma — in-process job scheduler (used when Redis/Celery is absent).

In development, this APScheduler instance runs the exact same background
functions as the Celery workers, so the behavior is identical between
`python run.py` (with --web) and the full docker-compose stack.
"""

from __future__ import annotations

import asyncio
import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app.core.config import get_settings

settings = get_settings()
logger = logging.getLogger("gramma.scheduler")


def build_scheduler() -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler(timezone=settings.timezone)

    def _run_async(coro_factory):
        async def _job():
            try:
                result = await coro_factory()
                logger.info("job done: %s", result)
            except Exception:  # noqa: BLE001
                logger.exception("background job failed")

        return _job

    from app.services.backup import run_backup
    from app.services.publisher import publish_due_posts
    from app.services.reports import cleanup_old_data, send_all_daily_reports
    from app.services.token_refresh import refresh_all_due_tokens

    async def _safe_backup():
        try:
            return await run_backup()
        except Exception as exc:  # noqa: BLE001
            logger.exception("backup failed")
            return {"status": "failed", "reason": str(exc)}

    scheduler.add_job(
        _run_async(publish_due_posts),
        "interval",
        seconds=settings.post_scheduler_interval_seconds,
        id="publish_scheduled_posts",
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        _run_async(refresh_all_due_tokens),
        "cron",
        hour=2,
        minute=30,
        id="refresh_tokens",
    )
    scheduler.add_job(
        _run_async(send_all_daily_reports),
        "cron",
        hour=settings.daily_report_hour,
        minute=0,
        id="daily_health_report",
    )
    scheduler.add_job(
        _run_async(cleanup_old_data),
        "cron",
        hour=3,
        minute=0,
        id="cleanup_old_data",
    )
    if settings.auto_backup_enabled:
        scheduler.add_job(
            _run_async(_safe_backup),
            "cron",
            hour=settings.auto_backup_hour,
            minute=0,
            id="auto_backup",
        )
    return scheduler
