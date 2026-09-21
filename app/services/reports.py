"""Gramma — daily reports + housekeeping (shared by celery & in-process)."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, select

from app.core.config import get_settings
from app.core.database import SessionLocal
from app.models import ActivityLog, InstagramAccount, Post
from app.services.insights import build_health_report, format_health_report
from app.services.notifications import notify_telegram

settings = get_settings()
logger = logging.getLogger("gramma.reports")


async def send_all_daily_reports() -> dict:
    """Build + send the daily page-health report to every connected user."""
    sent = 0
    async with SessionLocal() as session:
        result = await session.execute(select(InstagramAccount))
        accounts = result.scalars().all()
        notify_cache: dict[int, None] = {}
        for account in accounts:
            if account.owner_id in notify_cache:
                continue  # one report per user (even with multiple pages later)
            try:
                report = await build_health_report(account)
                ok = await notify_telegram(
                    session, account.owner_id, format_health_report(report)
                )
                if ok:
                    sent += 1
                    notify_cache[account.owner_id] = None
            except Exception as exc:  # noqa: BLE001
                logger.exception("report failed account=%s", account.id)
    logger.info("daily reports sent=%s", sent)
    return {"sent": sent}


async def cleanup_old_data() -> dict:
    """Delete old activity logs & failed posts beyond retention window."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=settings.data_retention_days)
    deleted_logs = 0
    deleted_posts = 0
    async with SessionLocal() as session:
        r1 = await session.execute(delete(ActivityLog).where(ActivityLog.created_at < cutoff))
        deleted_logs = r1.rowcount or 0
        r2 = await session.execute(
            delete(Post).where(
                Post.created_at < cutoff,
                Post.status.in_(["failed", "canceled", "draft"]),
            )
        )
        deleted_posts = r2.rowcount or 0
        await session.commit()
    return {"deleted_logs": deleted_logs, "deleted_posts": deleted_posts}
