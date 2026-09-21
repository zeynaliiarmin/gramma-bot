"""Celery task definitions.

Each task is thin: it just imports and runs the same async service function
used by the in-process scheduler. This keeps the two schedulers in parity.
"""

from __future__ import annotations

import asyncio

from app.tasks.celery_app import celery_app


def _run(coro):
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    return loop.run_until_complete(coro)


@celery_app.task(name="app.tasks.publish_scheduled_posts")
def publish_scheduled_posts():
    from app.services.publisher import publish_due_posts

    return _run(publish_due_posts())


@celery_app.task(name="app.tasks.refresh_tokens")
def refresh_tokens():
    from app.services.token_refresh import refresh_all_due_tokens

    return _run(refresh_all_due_tokens())


@celery_app.task(name="app.tasks.send_daily_health_reports")
def send_daily_health_reports():
    from app.services.reports import send_all_daily_reports

    return _run(send_all_daily_reports())


@celery_app.task(name="app.tasks.cleanup_old_data")
def cleanup_old_data():
    from app.services.reports import cleanup_old_data

    return _run(cleanup_old_data())


@celery_app.task(name="app.tasks.auto_backup")
def auto_backup():
    from app.services.backup import run_backup

    return _run(run_backup())


@celery_app.task(name="app.tasks.publish_one_post")
def publish_one_post(post_id: int):
    """Publish a single scheduled post on demand (retry button)."""
    from app.services.publisher import publish_post_by_id

    return _run(publish_post_by_id(post_id))
