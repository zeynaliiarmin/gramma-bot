"""Gramma's scheduled publication worker — Zernio is the ONLY publisher.

A scheduled row is durably claimed before the provider POST. If the provider
returns an ambiguous error/timeout, the row stays failed with an explicit
warning; it is never blindly retried (a public post may already exist).
This worker never calls Meta media publishing or a private/mock publisher.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import select

from app.core.config import get_settings
from app.core.database import SessionLocal
from app.models import ActivityLog, InstagramAccount, Media, Post
from app.services.zernio_publishing import (
    ZernioPublishError,
    get_zernio_publisher,
    normalize_username,
)

logger = logging.getLogger("gramma.publisher")
settings = get_settings()


async def publish_due_posts() -> dict:
    """Claim and send each due post once, exclusively through Zernio."""
    async with SessionLocal() as session:
        rows = (await session.execute(
            select(Post.id)
            .where(Post.status == "scheduled", Post.scheduled_at <= datetime.now(timezone.utc))
            .order_by(Post.scheduled_at.asc(), Post.id.asc())
            .limit(10)
        )).scalars().all()

    published = 0
    accepted = 0
    failed = 0
    for post_id in rows:
        result = await publish_post_by_id(post_id)
        if result.get("status") == "published":
            published += 1
        elif result.get("ok"):
            accepted += 1
        elif not result.get("skipped"):
            failed += 1
    return {"published": published, "accepted": accepted, "failed": failed}


async def publish_post_by_id(post_id: int) -> dict:
    """Atomically claim a scheduled post, then issue at most one Zernio POST.

    Can be invoked by Celery or the Vercel cron. Account ownership is checked
    against the configured admin's Telegram ID; one shared Zernio key must not
    become a cross-tenant publishing credential.
    """
    async with SessionLocal() as session:
        post = (await session.execute(
            select(Post)
            .where(
                Post.id == post_id,
                Post.status == "scheduled",
                Post.scheduled_at <= datetime.now(timezone.utc),
            )
            .with_for_update(skip_locked=True)
        )).scalars().first()
        if post is None:
            return {"ok": False, "skipped": True}
        post.status = "publishing"
        post.publish_started_at = datetime.now(timezone.utc)
        await session.commit()

        account = await session.get(InstagramAccount, post.account_id)
        media = (await session.execute(
            select(Media).where(Media.post_id == post.id).order_by(Media.order_index)
        )).scalars().all()
        account_id = post.account_id
        kind = post.kind
        caption = post.caption or ""
        urls = [m.url for m in media]
        owner_id = account.owner_id if account else None
        username = account.username if account else ""
        connected = account.status == "connected" if account else False

    try:
        if not connected or owner_id not in settings.admin_ids:
            raise ZernioPublishError("انتشار فقط برای پیج متصلِ متعلق به مدیر مجاز است.", 403)
        if not urls or any(not url.startswith("https://") for url in urls):
            raise ZernioPublishError("رسانه باید URL عمومی HTTPS داشته باشد؛ فایل محلی قابل انتشار نیست.")
        media_type = "post" if kind == "photo" else ("reel" if kind == "video" else kind)
        publisher = get_zernio_publisher()
        providers = await publisher.list_instagram_accounts()
        matches = [a for a in providers if a["active"] and normalize_username(a["username"]) == normalize_username(username)]
        if len(matches) != 1:
            raise ZernioPublishError("پیج شما در Zernio متصل و فعال نیست.", 409)
        result = await publisher.publish_post(
            zernio_account_id=matches[0]["id"],
            expected_username=username,
            caption=caption,
            media_urls=urls,
            media_type=media_type,
            idempotency_key=f"gramma_post_{post_id}",
        )
    except Exception as exc:  # noqa: BLE001
        # Do not leak provider response bodies, keys or account tokens to users.
        reason = str(exc) if isinstance(exc, ZernioPublishError) else "نتیجه نامشخص است؛ پیش از تلاش دوباره داشبورد Zernio را بررسی کنید."
        logger.warning("Zernio worker post_id=%s result=%s", post_id, type(exc).__name__)
        async with SessionLocal() as session:
            post = await session.get(Post, post_id)
            if post is not None:
                post.status = "failed"
                post.error_message = reason[:500]
                session.add(ActivityLog(
                    account_id=account_id, user_id=owner_id, action="zernio_publish_failed",
                    detail=f"post_id={post_id} {reason[:200]}", level="error",
                ))
                if account is not None:
                    await _notify_owner(session, account, post, success=False, error=reason)
                await session.commit()
        return {"ok": False, "error": reason, "post_id": post_id}

    # Zernio may initially say processing: never claim an unconfirmed publish.
    confirmed = result["status"] in ("published", "posted")
    async with SessionLocal() as session:
        post = await session.get(Post, post_id)
        if post is not None:
            post.ig_media_id = str(result["post_id"])
            post.status = "published" if confirmed else "publishing"
            if confirmed:
                post.published_at = datetime.now(timezone.utc)
            session.add(ActivityLog(
                account_id=account_id, user_id=owner_id,
                action="zernio_published" if confirmed else "zernio_publish_accepted",
                detail=f"post_id={post_id} zernio_id={result['post_id']} status={result['status']}",
                level="info",
            ))
            if account is not None:
                await _notify_owner(session, account, post, success=True, provider_status=result["status"])
            await session.commit()
    if confirmed:
        try:
            from app.webapp.main import broadcast_refresh

            await broadcast_refresh("posts", {"type": "post_published", "post_id": post_id})
        except Exception:  # noqa: BLE001
            pass
    return {"ok": True, "post_id": post_id, "provider_post_id": result["post_id"], "status": result["status"]}


async def _notify_owner(
    session, account: InstagramAccount, post: Post, *,
    success: bool, error: str = "", provider_status: str = "",
) -> None:
    """Best-effort Telegram notification; it must never retry publication."""
    from app.services.notifications import notify_telegram

    if success:
        text = (
            "✅ انتشار از مسیر Zernio ثبت شد\n" if post.status == "published"
            else "⏳ درخواست انتشار در Zernio پذیرفته شد؛ وضعیت نهایی را آنجا بررسی کنید.\n"
        ) + f"📌 نوع: {post.kind}\n🆔 شناسه: {post.ig_media_id or '—'}\nوضعیت: {provider_status}"
        notification_kind = "publish_success"
    else:
        text = f"❌ انتشار پست ناموفق یا نامشخص است\n📌 نوع: {post.kind}\n⚠️ {error[:300]}"
        notification_kind = "publish_failed"
    try:
        await notify_telegram(session, account.owner_id, text, kind=notification_kind)
    except Exception:  # noqa: BLE001
        logger.warning("could not notify owner id=%s", account.owner_id)
