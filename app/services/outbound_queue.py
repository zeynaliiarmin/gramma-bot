"""Durable, at-most-once Zernio publication from public.outbound_queue.

Only action_type='post' is handled here. ChatbotX replies and direct messages
have separate workers and are deliberately not touched. A provider request is
NEVER automatically retried: interrupted rows get a bounded read-only Zernio
lookup, then human review if the remote result remains ambiguous.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.database import SessionLocal
from app.models import ActivityLog, InstagramAccount, Post
from app.services.instagram_options import InstagramOptionError, validate_instagram_options
from app.services.limits import ZERNIO_POST_DAILY_LIMIT, ZERNIO_POST_MIN_SPACING_SECONDS
from app.services.zernio_publishing import (
    ZernioPublishError,
    _media_items,
    get_zernio_publisher,
    normalize_username,
)

settings = get_settings()
logger = logging.getLogger("gramma.outbound_posts")


class OutboundQueueError(Exception):
    """Safe validation error; no provider response or credential is exposed."""


def telegram_post_key(chat_id: int, message_id: int, *, sample: bool = False) -> str:
    """Stable across Telegram redeliveries; different for each new message."""
    return f"tg:{'test' if sample else 'photo'}:{chat_id}:{message_id}"


async def get_owned_post(session: AsyncSession, queue_id: int, owner_id: int) -> dict | None:
    row = (await session.execute(text("""
        SELECT id, idempotency_key, status, scheduled_for, attempts, last_error,
               provider_job_id, created_at, updated_at, payload
          FROM public.outbound_queue
         WHERE id = :id AND provider = 'zernio' AND action_type = 'post'
           AND payload->>'owner_id' = :owner
    """), {"id": queue_id, "owner": str(owner_id)})).mappings().first()
    return dict(row) if row else None


async def get_owned_post_by_key(session: AsyncSession, key: str, owner_id: int) -> dict | None:
    row = (await session.execute(text("""
        SELECT id, idempotency_key, status, scheduled_for, attempts, last_error,
               provider_job_id, created_at, updated_at, payload
          FROM public.outbound_queue
         WHERE idempotency_key = :key AND provider = 'zernio'
           AND action_type = 'post' AND payload->>'owner_id' = :owner
    """), {"key": key, "owner": str(owner_id)})).mappings().first()
    return dict(row) if row else None


async def list_owned_posts(session: AsyncSession, owner_id: int, limit: int = 6) -> list[dict]:
    rows = (await session.execute(text("""
        SELECT id, idempotency_key, status, scheduled_for, attempts, last_error,
               provider_job_id, created_at, updated_at, payload
          FROM public.outbound_queue
         WHERE provider = 'zernio' AND action_type = 'post'
           AND payload->>'owner_id' = :owner
         ORDER BY id DESC LIMIT :limit
    """), {"owner": str(owner_id), "limit": min(max(limit, 1), 10)})).mappings().all()
    return [dict(row) for row in rows]


async def enqueue_zernio_post(
    session: AsyncSession,
    *,
    account_id: int,
    owner_id: int,
    media_urls: list[str],
    caption: str,
    idempotency_key: str,
    origin: str,
    media_type: str = "post",
    telegram_chat_id: int | None = None,
    telegram_message_id: int | None = None,
    legacy_post_id: int | None = None,
    instagram_options: dict | None = None,
) -> dict:
    """Insert exactly one pending post; caller commits this DB transaction.

    This method only queues. No Zernio account request or publish can happen
    in the Telegram webhook. DB uniqueness is the cross-instance dedupe gate.
    """
    if owner_id not in settings.admin_ids:
        raise OutboundQueueError("ثبت پست فقط برای مدیر ربات مجاز است.")
    account = await session.get(InstagramAccount, account_id)
    if account is None or account.owner_id != owner_id or account.status != "connected":
        raise OutboundQueueError("پیج فعال و متصلِ متعلق به مدیر پیدا نشد.")
    if not normalize_username(account.username):
        raise OutboundQueueError("نام کاربری پیج متصل معتبر نیست.")
    if not settings.zernio_api_key:
        raise OutboundQueueError("اتصال Zernio تنظیم نشده است؛ پستی در صف ثبت نشد.")
    if not isinstance(caption, str) or len(caption) > 2200 or (not caption.strip() and media_type != "story"):
        raise OutboundQueueError("برای پست، کپشن غیرخالی و حداکثر 2200 نویسه لازم است.")
    if media_type not in ("post", "carousel", "reel", "story"):
        raise OutboundQueueError("نوع محتوای قابل انتشار معتبر نیست.")
    if origin not in ("telegram_photo", "telegram_video", "telegram_album", "telegram_test", "scheduled_post"):
        raise OutboundQueueError("منبع درخواست معتبر نیست.")
    if not isinstance(idempotency_key, str) or not 1 <= len(idempotency_key) <= 180:
        raise OutboundQueueError("شناسه یکتای درخواست نامعتبر است.")
    # Same media contract as the provider adapter, checked before insertion.
    try:
        items = _media_items(media_urls, media_type)
        safe_options = validate_instagram_options(
            instagram_options, media_type=media_type,
            media_kinds=[item["type"] for item in items],
        )
    except (ZernioPublishError, InstagramOptionError) as exc:
        raise OutboundQueueError(str(exc)) from exc

    payload = {
        "owner_id": owner_id,
        "local_account_id": account.id,
        "expected_username": normalize_username(account.username),
        "caption": caption.strip(),
        "media_urls": media_urls,
        "media_type": media_type,
        "origin": origin,
    }
    if telegram_chat_id is not None:
        payload["telegram_chat_id"] = telegram_chat_id
    if telegram_message_id is not None:
        payload["telegram_message_id"] = telegram_message_id
    if legacy_post_id is not None:
        payload["legacy_post_id"] = legacy_post_id
    if safe_options:
        payload["instagram_options"] = safe_options

    inserted = (await session.execute(text("""
        INSERT INTO public.outbound_queue
            (idempotency_key, action_type, provider, payload, status, max_attempts)
        VALUES (:key, 'post', 'zernio', CAST(:payload AS jsonb), 'pending', 1)
        ON CONFLICT (idempotency_key) DO NOTHING
        RETURNING id
    """), {"key": idempotency_key, "payload": json.dumps(payload, ensure_ascii=False)})).scalar_one_or_none()
    if inserted is not None:
        return {"id": inserted, "status": "pending", "created": True}

    existing = await get_owned_post_by_key(session, idempotency_key, owner_id)
    if existing is None:
        raise OutboundQueueError("این شناسه قبلاً برای درخواست دیگری استفاده شده است.")
    return {"id": existing["id"], "status": existing["status"], "created": False}


async def process_one_zernio_post() -> dict:
    """Claim AT MOST ONE post via SKIP LOCKED and issue AT MOST ONE POST.

    Claim is committed before the provider call. Processing/failed jobs are
    never selected by the claim function, including after a crash or timeout.
    A rate-limited job alone goes back to pending; its attempt is refunded
    inside the same transaction that records its future eligible timestamp.
    """
    async with SessionLocal() as session:
        queue_id = (await session.execute(text(
            "SELECT id FROM public.claim_next_zernio_post_job()"
        ))).scalar_one_or_none()
        await session.commit()  # durable claim BEFORE any provider request

    if queue_id is None:
        # These paths use Zernio GET/list only. They never claim or submit an
        # ambiguous post, even when an earlier invocation was killed outright.
        await deliver_one_confirmed_notification()
        await reconcile_one_stalled_processing_post()
        await refresh_one_accepted_post()
        return {"claimed": 0, "published_requests": 0}

    async with SessionLocal() as session:
        row = (await session.execute(text("""
            SELECT id, idempotency_key, payload FROM public.outbound_queue
             WHERE id = :id AND status = 'processing' AND provider = 'zernio'
               AND action_type = 'post'
        """), {"id": queue_id})).mappings().first()
        if row is None:
            return {"claimed": 1, "published_requests": 0, "state": "manual_review"}
        payload = row["payload"]
        key = row["idempotency_key"]
        try:
            owner = int(payload["owner_id"])
            account_id = int(payload["local_account_id"])
            caption = payload["caption"]
            urls = payload["media_urls"]
            media_type = payload["media_type"]
            username = payload["expected_username"]
            account = await session.get(InstagramAccount, account_id)
            if (owner not in settings.admin_ids or account is None
                    or account.owner_id != owner or account.status != "connected"
                    or normalize_username(account.username) != username):
                raise OutboundQueueError("مالکیت یا اتصال پیج تغییر کرده است؛ انتشار متوقف شد.")
            if not isinstance(caption, str) or (not caption.strip() and media_type != "story"):
                raise OutboundQueueError("کپشن لازم است؛ انتشار متوقف شد.")
            items = _media_items(urls, media_type)
            platform_options = validate_instagram_options(
                payload.get("instagram_options"), media_type=media_type,
                media_kinds=[item["type"] for item in items],
            )
            publisher = get_zernio_publisher()
            if not publisher.configured:
                raise OutboundQueueError("اتصال Zernio تنظیم نشده است؛ انتشار متوقف شد.")
        except (KeyError, ValueError, TypeError, OutboundQueueError, ZernioPublishError, InstagramOptionError) as exc:
            reason = str(exc) if isinstance(exc, (OutboundQueueError, ZernioPublishError)) else "دادهٔ صف ناقص است؛ بررسی دستی لازم است."
            await _mark_failed(queue_id, reason)
            return {"claimed": 1, "published_requests": 0, "state": "failed"}

    # Refresh provider accounts from Zernio: never accept a stale or arbitrary
    # remote account ID. This GET cannot publish. The adapter independently
    # verifies the exact same page once more before its single POST.
    try:
        providers = await publisher.list_instagram_accounts()
        matches = [a for a in providers if a["active"] and normalize_username(a["username"]) == username]
        if len(matches) != 1:
            raise OutboundQueueError("پیج در Zernio متصل و فعال نیست؛ انتشار متوقف شد.")
    except Exception as exc:  # noqa: BLE001
        reason = str(exc) if isinstance(exc, (OutboundQueueError, ZernioPublishError)) else "بررسی اتصال Zernio ممکن نشد."
        logger.warning("provider account check failed queue_id=%s error=%s", queue_id, type(exc).__name__)
        await _mark_failed(queue_id, reason)
        return {"claimed": 1, "published_requests": 0, "state": "failed"}

    # Rate reservation is atomic across overlapping Vercel invocations, and
    # committed BEFORE submission. A crash still spends the slot conservatively.
    async with SessionLocal() as session:
        rate = (await session.execute(text("""
            SELECT allowed, retry_after
              FROM public.reserve_zernio_post_slot(:page, :daily, :spacing)
        """), {
            "page": username,
            "daily": ZERNIO_POST_DAILY_LIMIT,
            "spacing": ZERNIO_POST_MIN_SPACING_SECONDS,
        })).mappings().one()
        if not rate["allowed"]:
            await session.execute(text("""
                UPDATE public.outbound_queue
                   SET status = 'pending', attempts = GREATEST(attempts - 1, 0),
                       scheduled_for = GREATEST(:after, NOW() + INTERVAL '1 minute'),
                       updated_at = NOW(), last_error = NULL
                 WHERE id = :id AND status = 'processing'
            """), {"id": queue_id, "after": rate["retry_after"]})
            await session.commit()
            return {"claimed": 1, "published_requests": 0, "state": "deferred"}
        await session.commit()

    # This is the ONLY provider publication call. Never retry it: even a 5xx
    # or timeout can mean that Instagram has already received the post.
    result: dict | None = None
    reason = "نتیجه انتشار نامشخص است؛ پیش از اقدام دوباره Zernio و Instagram را بررسی کنید."
    try:
        result = await publisher.publish_post(
            zernio_account_id=matches[0]["id"],
            expected_username=username,
            caption=caption,
            media_urls=urls,
            media_type=media_type,
            idempotency_key=key,
            platform_specific_data=platform_options,
        )
    except Exception as exc:  # noqa: BLE001
        if isinstance(exc, ZernioPublishError):
            reason = str(exc)
        logger.warning("publish result uncertain queue_id=%s error=%s", queue_id, type(exc).__name__)
    finally:
        # Finalize in a fresh DB transaction even on a Python exception or
        # cancellation. A second *DB-only* attempt is safe; never repeat POST.
        # A hard process kill cannot run finally; idle ticks perform bounded,
        # read-only Zernio reconciliation of such processing rows instead.
        try:
            if result is None:
                await _mark_failed(queue_id, reason)
            else:
                await _mark_sent(queue_id, result, matches[0]["id"])
        except Exception:  # noqa: BLE001
            logger.exception("post outcome finalization failed queue_id=%s; NEVER resubmit", queue_id)
            raise

    if result is None:
        return {"claimed": 1, "published_requests": 1, "state": "failed_manual_review"}
    # Record acceptance BEFORE this optional GET. A provider rollup saying
    # 'published' is not enough: verify the exact Instagram target as well.
    try:
        checked = await refresh_one_accepted_post(queue_id, owner)
    except Exception as exc:  # noqa: BLE001
        logger.warning("post confirmation deferred queue_id=%s error=%s", queue_id, type(exc).__name__)
        checked = None
    confirmed = bool(checked and checked["payload"].get("provider_status") == "published")
    return {"claimed": 1, "published_requests": 1,
            "state": "published" if confirmed else "accepted", "queue_id": queue_id}


async def _commit_queue_update(sql: str, params: dict) -> dict | None:
    """Retry a status UPDATE once in a fresh session; NEVER retry publication."""
    for attempt in range(2):
        try:
            async with SessionLocal() as session:
                row = (await session.execute(text(sql), params)).scalar_one_or_none()
                await session.commit()
                return row
        except Exception as exc:  # noqa: BLE001
            logger.warning("queue DB finalization failed attempt=%s error=%s",
                           attempt + 1, type(exc).__name__)
            if attempt == 1:
                raise
            await asyncio.sleep(0.25)
    return None


async def _record_legacy_status(
    queue_id: int, payload: dict, *, provider_status: str,
    provider_post_id: str | None = None, reason: str = "",
) -> None:
    """Legacy bookkeeping can fail without rolling back the durable queue."""
    try:
        async with SessionLocal() as session:
            await _update_legacy_post(session, payload, provider_status=provider_status,
                                      provider_post_id=provider_post_id, reason=reason)
            if provider_post_id:
                session.add(ActivityLog(
                    account_id=payload.get("local_account_id"), user_id=payload.get("owner_id"),
                    action="zernio_published" if provider_status == "published" else "zernio_publish_accepted",
                    detail=f"outbound_id={queue_id} zernio_post_id={provider_post_id} status={provider_status}",
                    level="info",
                ))
            await session.commit()
    except Exception as exc:  # noqa: BLE001
        logger.warning("legacy post audit unavailable queue_id=%s error=%s", queue_id, type(exc).__name__)


async def _mark_failed(queue_id: int, reason: str) -> None:
    """Fail closed. Neither a processing nor failed row can be republished."""
    safe_reason = reason[:500]
    row = await _commit_queue_update("""
        UPDATE public.outbound_queue
           SET status = 'failed', last_error = :reason,
               payload = payload || jsonb_build_object(
                   'manual_review', TRUE, 'failed_at', NOW()
               ), updated_at = NOW()
         WHERE id = :id AND provider = 'zernio' AND action_type = 'post'
           AND status = 'processing'
        RETURNING payload
    """, {"id": queue_id, "reason": safe_reason})
    if not row:
        return
    await _record_legacy_status(queue_id, row, provider_status="failed", reason=safe_reason)
    owner_id = row.get("owner_id")
    if isinstance(owner_id, int) and owner_id in settings.admin_ids:
        await _notify(owner_id, f"⚠️ پست صف {queue_id} نیازمند بررسی دستی است؛ بدون بررسی Zernio دوباره ارسال نکنید.", success=False)


async def _mark_sent(queue_id: int, result: dict, provider_account_id: str) -> None:
    status = str(result["status"])
    if status not in ("publishing", "published"):
        raise OutboundQueueError("وضعیت پاسخ Zernio نامشخص است؛ بررسی دستی لازم است.")
    provider_post_id = str(result["post_id"])
    # JSON is explicitly typed for asyncpg. jsonb_build_object(:untyped) was
    # the production error after Zernio returned 201; never reintroduce it.
    patch = json.dumps({
        "provider_status": "publishing",  # require GET of the exact Instagram target
        "provider_response_status": status,
        "provider_account_id": provider_account_id,
        "accepted_at": datetime.now(timezone.utc).isoformat(),
        "manual_review": False,
    }, ensure_ascii=False)
    row = await _commit_queue_update("""
        UPDATE public.outbound_queue
           SET status = 'sent', provider_job_id = :post_id,
               payload = payload || CAST(:patch AS jsonb),
               updated_at = NOW(), last_error = NULL
         WHERE id = :id AND provider = 'zernio' AND action_type = 'post'
           AND status = 'processing'
        RETURNING payload
    """, {"id": queue_id, "post_id": provider_post_id, "patch": patch})
    if row:
        await _record_legacy_status(queue_id, row, provider_status="publishing",
                                    provider_post_id=provider_post_id)


async def _update_legacy_post(
    session: AsyncSession, payload: dict,
    *, provider_status: str, provider_post_id: str | None = None, reason: str = "",
) -> None:
    post_id = payload.get("legacy_post_id")
    if not isinstance(post_id, int):
        return
    post = await session.get(Post, post_id)
    if post is None or post.account_id != payload.get("local_account_id"):
        return
    if provider_status == "failed":
        post.status = "failed"
        post.error_message = reason[:500]
    else:
        post.ig_media_id = provider_post_id
        post.status = "published" if provider_status == "published" else "publishing"
        if provider_status == "published":
            post.published_at = datetime.now(timezone.utc)


async def _broadcast_legacy_post(payload: dict) -> None:
    """Preserve the existing best-effort Mini-App refresh on confirmation."""
    post_id = payload.get("legacy_post_id")
    if not isinstance(post_id, int):
        return
    try:
        from app.webapp.main import broadcast_refresh

        await broadcast_refresh("posts", {"type": "post_published", "post_id": post_id})
    except Exception as exc:  # noqa: BLE001
        logger.warning("post refresh unavailable id=%s error=%s", post_id, type(exc).__name__)


async def _notify(owner_id: int, text_value: str, *, success: bool) -> bool:
    """Plain bot.send_message only: never touch Telegram FSM or queue status."""
    try:
        from app.services.notifications import notify_telegram

        async with SessionLocal() as session:
            return await notify_telegram(
                session, owner_id, text_value,
                kind="publish_success" if success else "publish_failed",
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning("outbound notification unavailable owner=%s error=%s", owner_id, type(exc).__name__)
        return False


async def deliver_one_confirmed_notification(queue_id: int | None = None) -> bool:
    """Claim a durable success notice BEFORE a plain Telegram send_message.

    A killed worker after confirmation leaves the pending flag for the next
    idle cron tick. Once claimed, an ambiguous Telegram timeout is not retried
    automatically; avoiding duplicate chat messages cannot be guaranteed
    simultaneously with guaranteed delivery by Telegram's non-idempotent API.
    """
    if queue_id is None:
        async with SessionLocal() as session:
            queue_id = (await session.execute(text("""
                SELECT id FROM public.outbound_queue
                 WHERE provider = 'zernio' AND action_type = 'post'
                   AND status = 'sent' AND payload->>'provider_status' = 'published'
                   AND payload->>'success_notification_pending' = 'true'
                   AND NOT (payload ? 'success_notification_attempted_at')
                 ORDER BY updated_at ASC LIMIT 1
            """))).scalar_one_or_none()
    if queue_id is None:
        return False
    row = await _commit_queue_update("""
        UPDATE public.outbound_queue
           SET payload = payload || jsonb_build_object(
                   'success_notification_attempted_at', NOW()
               ), updated_at = NOW()
         WHERE id = :id AND provider = 'zernio' AND action_type = 'post'
           AND status = 'sent' AND payload->>'provider_status' = 'published'
           AND payload->>'success_notification_pending' = 'true'
           AND NOT (payload ? 'success_notification_attempted_at')
        RETURNING payload
    """, {"id": queue_id})
    if not row:
        return False  # another invocation claimed it; never send twice
    owner = row.get("owner_id")
    if not isinstance(owner, int) or owner not in settings.admin_ids:
        logger.warning("post notification suppressed: owner not authorized queue_id=%s", queue_id)
        return False
    delivered = await _notify(owner, "پست شما با موفقیت منتشر شد", success=True)
    if delivered:
        try:
            await _commit_queue_update("""
                UPDATE public.outbound_queue
                   SET payload = payload || jsonb_build_object(
                           'success_notification_sent_at', NOW()
                       ), updated_at = NOW()
                 WHERE id = :id AND provider = 'zernio' AND action_type = 'post'
                   AND status = 'sent' AND payload ? 'success_notification_attempted_at'
                RETURNING payload
            """, {"id": queue_id})
        except Exception as exc:  # noqa: BLE001
            logger.warning("post notification delivered but audit failed queue_id=%s error=%s",
                           queue_id, type(exc).__name__)
    return delivered


async def refresh_one_accepted_post(queue_id: int | None = None, owner_id: int | None = None) -> dict | None:
    """Read-only provider GET for ONE accepted post; never retry publication.

    Called automatically only on an idle cron tick or explicitly by the admin
    status button. An ambiguous processing/failed row is never reconciled by
    sending a new POST. A GET error leaves the accepted status intact.
    """
    async with SessionLocal() as session:
        if queue_id is not None and owner_id is not None:
            job = await get_owned_post(session, queue_id, owner_id)
        elif queue_id is None and owner_id is None:
            row = (await session.execute(text("""
                SELECT id, idempotency_key, status, scheduled_for, attempts,
                       last_error, provider_job_id, created_at, updated_at, payload
                  FROM public.outbound_queue
                 WHERE provider = 'zernio' AND action_type = 'post'
                   AND status = 'sent' AND provider_job_id IS NOT NULL
                   AND payload->>'provider_status' = 'publishing'
                   AND updated_at < NOW() - INTERVAL '2 minutes'
                   AND created_at > NOW() - INTERVAL '14 days'
                 ORDER BY updated_at ASC LIMIT 1
            """))).mappings().first()
            job = dict(row) if row else None
        else:
            raise ValueError("specify both queue_id and owner_id, or neither")

    if not job or job["status"] != "sent" or not job["provider_job_id"]:
        return job
    payload = job["payload"]
    if payload.get("provider_status") in ("published", "failed", "partial"):
        return job
    account_id = payload.get("provider_account_id")
    if not account_id:
        return job

    try:
        remote = await get_zernio_publisher().get_post_status(
            job["provider_job_id"], expected_account_id=account_id
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("post status GET failed queue_id=%s error=%s", job["id"], type(exc).__name__)
        return job

    status = remote["status"]
    if status not in ("published", "publishing", "failed", "partial"):
        return job
    failed = status in ("failed", "partial")
    patch = {
        "provider_status": status,
        "last_checked_at": datetime.now(timezone.utc).isoformat(),
        "manual_review": failed,
    }
    if status == "published":
        patch.update({
            "confirmed_at": datetime.now(timezone.utc).isoformat(),
            "provider_published_at": remote.get("published_at"),
            "success_notification_pending": True,
        })
    # Explicit jsonb cast fixes the same asyncpg unknown-parameter failure in
    # the *second* status path; CAS prevents double Telegram notifications.
    changed = await _commit_queue_update("""
        UPDATE public.outbound_queue
           SET status = CASE WHEN CAST(:failed AS boolean) THEN 'failed' ELSE 'sent' END,
               last_error = CASE WHEN CAST(:failed AS boolean) THEN CAST(:error AS text)
                                 ELSE NULL END,
               payload = payload || CAST(:patch AS jsonb), updated_at = NOW()
         WHERE id = :id AND provider = 'zernio' AND action_type = 'post'
           AND status = 'sent' AND provider_job_id = :post_id
           AND payload->>'provider_status' = 'publishing'
        RETURNING payload
    """, {
        "id": job["id"], "post_id": job["provider_job_id"],
        "patch": json.dumps(patch, ensure_ascii=False), "failed": failed,
        "error": "Zernio انتشار نهایی را ناموفق گزارش کرد؛ قبل از هر تلاش دوباره بررسی کنید.",
    })
    if changed and status in ("published", "failed", "partial"):
        await _record_legacy_status(
            job["id"], changed, provider_status="published" if status == "published" else "failed",
            provider_post_id=job["provider_job_id"] if status == "published" else None,
            reason="انتشار در Zernio نهایی نشد؛ بررسی دستی لازم است.",
        )
        if status == "published":
            await _broadcast_legacy_post(changed)
            try:
                await deliver_one_confirmed_notification(job["id"])
            except Exception as exc:  # noqa: BLE001
                # Outcome was committed first; an idle tick can send pending.
                logger.warning("post notification deferred queue_id=%s error=%s",
                               job["id"], type(exc).__name__)
        else:
            who = changed.get("owner_id")
            if isinstance(who, int) and who in settings.admin_ids:
                await _notify(who, "⚠️ Zernio انتشار نهایی را ناموفق گزارش کرد؛ بررسی دستی لازم است.", success=False)
    async with SessionLocal() as session:
        return await get_owned_post(session, job["id"], int(payload["owner_id"]))


async def reconcile_one_stalled_processing_post(
    queue_id: int | None = None, owner_id: int | None = None,
) -> dict | None:
    """Recover ONE interrupted job using Zernio GET/list, never another POST.

    Called on an idle cron tick or a private admin status refresh. If there is
    no unique verified match after 15 minutes, mark the result *uncertain* for
    manual review. No processing/failed row ever returns to pending.
    """
    async with SessionLocal() as session:
        if queue_id is not None and owner_id is not None:
            job = await get_owned_post(session, queue_id, owner_id)
        elif queue_id is None and owner_id is None:
            row = (await session.execute(text("""
                SELECT id, idempotency_key, status, scheduled_for, attempts,
                       last_error, provider_job_id, created_at, updated_at, payload
                  FROM public.outbound_queue
                 WHERE provider = 'zernio' AND action_type = 'post'
                   AND status = 'processing' AND provider_job_id IS NULL
                   AND attempts = 1 AND max_attempts = 1
                   AND updated_at < NOW() - INTERVAL '2 minutes'
                   AND created_at > NOW() - INTERVAL '14 days'
                 ORDER BY updated_at ASC LIMIT 1
            """))).mappings().first()
            job = dict(row) if row else None
        else:
            raise ValueError("specify both queue_id and owner_id, or neither")
    if not job or job["status"] != "processing" or job["provider_job_id"]:
        return job
    now = datetime.now(timezone.utc)
    if (job["updated_at"] > now - timedelta(minutes=2)
            or job["created_at"] < now - timedelta(days=14)):
        return job  # the original worker might still be executing
    age = now - job["updated_at"]
    reason = "نتیجه انتشار نامشخص است؛ شناسهٔ یکتای Zernio پیدا یا تأیید نشد. بدون بررسی دستی دوباره ارسال نکنید."
    payload = job["payload"] or {}
    owner = payload.get("owner_id")
    account_id = payload.get("local_account_id")
    username = payload.get("expected_username")
    try:
        if (not isinstance(owner, int) or owner not in settings.admin_ids
                or (owner_id is not None and owner != owner_id)
                or not isinstance(account_id, int) or not normalize_username(username)):
            raise OutboundQueueError("مالکیت درخواست صف نامعتبر است.")
        async with SessionLocal() as session:
            account = await session.get(InstagramAccount, account_id)
            if (account is None or account.owner_id != owner
                    or normalize_username(account.username) != username):
                raise OutboundQueueError("پیج صف دیگر با مالکیت ثبت‌شده مطابق نیست.")
        publisher = get_zernio_publisher()
        providers = await publisher.list_instagram_accounts()  # GET only
        matches = [a for a in providers if a["active"]
                   and normalize_username(a["username"]) == username]
        if len(matches) != 1:
            raise OutboundQueueError("پیج مقصد در Zernio یکتا و فعال نیست.")
        content = payload.get("caption")
        if payload.get("media_type") == "story":
            content = content or "استوری"
        post_id = await publisher.find_queued_post(
            account_id=matches[0]["id"], idempotency_key=job["idempotency_key"],
            content=content, queued_at=job["created_at"],
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("stalled post GET unavailable queue_id=%s error=%s", job["id"], type(exc).__name__)
        post_id = None
    if post_id:
        # The list gives a uniquely matching provider ID. Commit acceptance
        # first; a separate GET verifies both post and Instagram target status.
        await _mark_sent(job["id"], {"post_id": post_id, "status": "publishing"}, matches[0]["id"])
        try:
            return await refresh_one_accepted_post(job["id"], owner)
        except Exception as exc:  # noqa: BLE001
            logger.warning("recovered post confirmation deferred queue_id=%s error=%s",
                           job["id"], type(exc).__name__)
            async with SessionLocal() as session:
                return await get_owned_post(session, job["id"], owner)
    if age >= timedelta(minutes=15):
        await _mark_failed(job["id"], reason)
        async with SessionLocal() as session:
            return await get_owned_post(session, job["id"], owner) if isinstance(owner, int) else None
    return job
