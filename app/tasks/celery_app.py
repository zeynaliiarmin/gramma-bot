"""Celery app + beat schedule.

Gramma uses Celery + Redis as its task queue in production, and a built-in
in-process scheduler (APScheduler) when Redis is not available — both share
the exact same task functions so there is a single code path.
"""

from __future__ import annotations

from celery import Celery
from celery.schedules import crontab

from app.core.config import get_settings

settings = get_settings()

celery_app = Celery(
    "gramma",
    broker=settings.redis_url,
    backend=settings.redis_url,
    include=["app.tasks"],
)

celery_app.conf.update(
    timezone=settings.timezone,
    enable_utc=True,
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    task_track_started=True,
    broker_connection_retry_on_startup=True,
)

celery_app.conf.beat_schedule = {
    # Publish scheduled posts every N seconds
    "publish-scheduled-posts": {
        "task": "app.tasks.publish_scheduled_posts",
        "schedule": settings.post_scheduler_interval_seconds,
    },
    # Refresh expiring tokens once a day
    "refresh-tokens": {
        "task": "app.tasks.refresh_tokens",
        "schedule": crontab(hour=2, minute=30),
    },
    # Daily page-health report at the configured hour
    "daily-health-report": {
        "task": "app.tasks.send_daily_health_reports",
        "schedule": crontab(hour=settings.daily_report_hour, minute=0),
    },
    # Housekeeping: purge old logs / user data
    "cleanup-old-data": {
        "task": "app.tasks.cleanup_old_data",
        "schedule": crontab(hour=3, minute=0),
    },
    # Nightly automatic database backup
    "auto-backup": {
        "task": "app.tasks.auto_backup",
        "schedule": crontab(hour=settings.auto_backup_hour, minute=0),
    },
}
