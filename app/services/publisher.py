"""Gramma — scheduled-publishing job.

Executed periodically (Celery beat). For each due scheduled post:
  1) lock it (status publishing) so two workers never publish twice;
  2) resolve the owner's account & decrypt token;
  3) publish via the Graph API (single media / carousel);
  4) update status + notify the owner on Telegram on success/failure.

Simulation mode makes the whole pipeline testable without Meta credentials.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from sqlalchemy import select

from app.core.config import get_settings
from app.core.database import SessionLocal
from app.models import ActivityLog, InstagramAccount, Media, Post
from app.services.meta.service import InstagramService

settings = get_settings()
logger = logging.getLogger("gramma.publisher")


async def publish_due_posts() -> dict:
    """Publish every due scheduled post. Returns a summary dict."""
    svc = InstagramService()
    published = 0
    failed = 0

    async with SessionLocal() as session:
        now = datetime.now(timezone.utc)
        result = await session.execute(
            select(Post).where(
                Post.status == "scheduled",
                Post.scheduled_at <= now,
            )
        )
        posts = result.scalars().all()

        for post in posts:
            post.status = "publishing"
            post.publish_started_at = now
            await session.commit()

            account = await session.get(InstagramAccount, post.account_id)
            if account is None:
                _mark_failed(post, "owner account missing")
                await session.commit()
                failed += 1
                continue

            try:
                media_rows = (
                    (
                        await session.execute(
                            select(Media)
                            .where(Media.post_id == post.id)
                            .order_by(Media.order_index)
                        )
                    )
                    .scalars()
                    .all()
                )

                if not media_rows:
                    raise ValueError("no media attached to post")

                media_urls = [m.url for m in media_rows]
                caption = post.caption or ""

                if post.kind == "carousel" and len(media_urls) > 1:
                    children = []
                    for m in media_rows:
                        child = await svc.create_child_container(
                            account, m.url, is_video=(m.kind == "video")
                        )
                        children.append(child)
                    result_pub = await svc.publish_carousel(account, children, caption)
                else:
                    single = media_rows[0]
                    result_pub = await svc.publish_media(
                        account,
                        single.url,
                        caption,
                        is_video=(single.kind == "video"),
                        is_reel=(post.kind == "reel"),
                        is_story=(post.kind == "story"),
                    )

                post.ig_media_id = str(result_pub.get("id", ""))
                post.ig_permalink = result_pub.get("permalink", "")
                post.status = "published"
                post.published_at = datetime.now(timezone.utc)
                try:  # live-refresh the Mini-App dashboard
                    from app.webapp.main import broadcast_refresh

                    await broadcast_refresh(
                        "posts", {"type": "post_published", "post_id": post.id}
                    )
                except Exception:  # noqa: BLE001
                    pass
                session.add(
                    ActivityLog(
                        account_id=account.id,
                        user_id=account.owner_id,
                        action="publish",
                        detail=f"published {post.kind} (media_id={post.ig_media_id})",
                        level="info",
                    )
                )
                await _notify_owner(session, account, post, success=True)
                await session.commit()
                published += 1
            except Exception as exc:  # noqa: BLE001
                logger.exception("publish failed post_id=%s", post.id)
                _mark_failed(post, str(exc))
                session.add(
                    ActivityLog(
                        account_id=account.id,
                        user_id=account.owner_id,
                        action="publish",
                        detail=f"FAILED: {exc}",
                        level="error",
                    )
                )
                await _notify_owner(session, account, post, success=False, error=str(exc))
                await session.commit()
                failed += 1

    return {"published": published, "failed": failed}


def _mark_failed(post: Post, message: str) -> None:
    post.status = "failed"
    post.error_message = message[:500]
    post.updated_at = datetime.now(timezone.utc)


async def _notify_owner(session, account, post, success: bool, error: str = "") -> None:
    """Send the owner a Telegram message about the publish result."""
    from app.services.notifications import notify_telegram

    if success:
        text = (
            f"✅ پست شما منتشر شد\n"
            f"📌 نوع: {post.kind}\n"
            f"🔗 لینک: {post.ig_permalink or '—'}"
        )
        kind = "publish_success"
    else:
        text = (
            f"❌ انتشار پست ناموفق بود\n"
            f"📌 نوع: {post.kind}\n"
            f"⚠️ خطا: {error[:300]}"
        )
        kind = "publish_failed"
    try:
        await notify_telegram(session, account.owner_id, text, kind=kind)
    except Exception:  # noqa: BLE001
        logger.warning("could not notify owner id=%s", account.owner_id)
