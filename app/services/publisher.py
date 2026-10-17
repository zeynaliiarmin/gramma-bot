"""Move due scheduled Post rows into the durable Zernio outbound post queue.

The existing minute scheduler and Celery task still call these functions.
Neither function contacts Zernio or publishes: the separate authenticated
five-minute outbound worker is the ONLY consumer of post queue rows.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import select

from app.core.database import SessionLocal
from app.models import ActivityLog, InstagramAccount, Media, Post
from app.services.outbound_queue import OutboundQueueError, enqueue_zernio_post

logger = logging.getLogger("gramma.publisher")


async def publish_due_posts() -> dict:
    """Enqueue up to 10 due legacy posts without issuing a provider POST."""
    async with SessionLocal() as session:
        rows = (await session.execute(
            select(Post.id)
            .where(Post.status == "scheduled", Post.scheduled_at <= datetime.now(timezone.utc))
            .order_by(Post.scheduled_at.asc(), Post.id.asc())
            .limit(10)
        )).scalars().all()

    queued = 0
    failed = 0
    for post_id in rows:
        result = await publish_post_by_id(post_id)
        if result.get("status") == "queued":
            queued += 1
        elif not result.get("skipped"):
            failed += 1
    # Keep previous response keys for callers while accurately reporting that
    # the due-post tick itself made ZERO publication requests.
    return {"queued": queued, "published": 0, "accepted": 0, "failed": failed}


async def publish_post_by_id(post_id: int) -> dict:
    """Atomically claim a due Post, insert its idempotent queue row, commit.

    The stable key forbids another send if two scheduler instances overlap.
    Publish result/failure is later reflected back onto this legacy Post by
    the queue worker, so the existing Mini-App view stays meaningful.
    """
    async with SessionLocal() as session:
        post = (await session.execute(
            select(Post).where(
                Post.id == post_id, Post.status == "scheduled",
                Post.scheduled_at <= datetime.now(timezone.utc),
            ).with_for_update(skip_locked=True)
        )).scalars().first()
        if post is None:
            return {"ok": False, "skipped": True}

        account = await session.get(InstagramAccount, post.account_id)
        media = (await session.execute(
            select(Media).where(Media.post_id == post.id).order_by(Media.order_index)
        )).scalars().all()
        kind = {"photo": "post", "video": "reel"}.get(post.kind, post.kind)
        try:
            if account is None:
                raise OutboundQueueError("پیج این پست یافت نشد؛ انتشار متوقف شد.")
            queue_row = await enqueue_zernio_post(
                session,
                account_id=account.id,
                owner_id=account.owner_id,
                caption=post.caption or "",
                media_urls=[m.url for m in media],
                media_type=kind,
                idempotency_key=f"post:{post.id}",
                origin="scheduled_post",
                legacy_post_id=post.id,
            )
        except OutboundQueueError as exc:
            post.status = "failed"
            post.error_message = str(exc)[:500]
            session.add(ActivityLog(
                account_id=post.account_id, user_id=account.owner_id if account else None,
                action="zernio_post_not_queued", detail=f"post_id={post_id} invalid_input",
                level="warning",
            ))
            await session.commit()
            logger.warning("scheduled post not queued post_id=%s reason=%s", post_id, type(exc).__name__)
            return {"ok": False, "post_id": post_id, "status": "failed"}

        # A manually reset Post must not be sent again: ON CONFLICT above
        # reports the original queue row instead of creating another one.
        if queue_row["status"] == "failed":
            post.status = "failed"
            post.error_message = "این شناسه قبلاً مصرف شده؛ وضعیت Zernio را دستی بررسی کنید."
            await session.commit()
            return {"ok": False, "post_id": post_id, "status": "failed"}
        post.status = "publishing"
        post.publish_started_at = datetime.now(timezone.utc)
        session.add(ActivityLog(
            account_id=post.account_id, user_id=account.owner_id,
            action="zernio_post_queued",
            detail=f"post_id={post_id} outbound_id={queue_row['id']}", level="info",
        ))
        await session.commit()
        return {"ok": True, "post_id": post_id, "queue_id": queue_row["id"], "status": "queued"}
