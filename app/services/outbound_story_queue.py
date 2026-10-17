"""Story-only protected minute Worker; at-most-once provider POST per slide.

The existing five-minute post Worker and album/settings cron are not reused.
Each child is gated until a Zernio GET confirms BOTH the parent post and its
Instagram target as published. A timeout/crash never causes a second POST.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import text

from app.core.config import get_settings
from app.core.database import SessionLocal
from app.models import InstagramAccount
from app.services.limits import ZERNIO_STORY_DAILY_LIMIT, ZERNIO_STORY_MIN_SPACING_SECONDS
from app.services.media_storage import owned_supabase_storage_path
from app.services.outbound_queue import OutboundQueueError, _notify
from app.services.zernio_publishing import (
    ZernioPublishError, _media_items, get_zernio_publisher, normalize_username,
)

settings = get_settings()
logger = logging.getLogger("gramma.outbound_stories")


async def _story_job(queue_id: int | None = None) -> dict | None:
    async with SessionLocal() as session:
        conditions = "id = :id" if queue_id is not None else (
            "status = 'sent' AND provider_job_id IS NOT NULL "
            "AND created_at > NOW() - INTERVAL '14 days' AND ( "
            "(payload->>'provider_status' = 'publishing' "
            "AND updated_at < NOW() - INTERVAL '1 minute') OR "
            "(payload->>'provider_status' = 'published' AND EXISTS ( "
            "SELECT 1 FROM public.outbound_queue child "
            "WHERE child.provider = 'zernio' AND child.action_type = 'story' "
            "AND child.status = 'pending' "
            "AND child.payload->>'story_previous_queue_id' = outbound_queue.id::text "
            "AND child.payload->>'story_released' = 'false')) )"
        )
        row = (await session.execute(text(f"""
            SELECT id, idempotency_key, status, scheduled_for, attempts,
                   last_error, provider_job_id, created_at, updated_at, payload
              FROM public.outbound_queue
             WHERE provider = 'zernio' AND action_type = 'story' AND {conditions}
             ORDER BY updated_at ASC LIMIT 1
        """), {"id": queue_id} if queue_id is not None else {})).mappings().first()
        return dict(row) if row else None


async def _safe_update(sql: str, params: dict) -> dict | None:
    """A DB-only retry is safe. No caller of this helper retries a POST."""
    for attempt in range(2):
        try:
            async with SessionLocal() as session:
                row = (await session.execute(text(sql), params)).mappings().first()
                await session.commit()
                return dict(row) if row else None
        except Exception as exc:  # noqa: BLE001
            logger.warning("story DB finalization attempt=%s error=%s", attempt + 1, type(exc).__name__)
            if attempt:
                raise
            await asyncio.sleep(0.2)
    return None


async def _mark_failed(queue_id: int, reason: str) -> None:
    row = await _safe_update("""
        UPDATE public.outbound_queue
           SET status = 'failed', last_error = :reason,
               payload = payload || CAST(:patch AS jsonb), updated_at = NOW()
         WHERE id = :id AND provider = 'zernio' AND action_type = 'story'
           AND status IN ('processing', 'sent')
        RETURNING payload
    """, {"id": queue_id, "reason": reason[:450],
           "patch": json.dumps({"manual_review": True, "story_blocked": True})})
    if row:
        owner = row["payload"].get("owner_id")
        if isinstance(owner, int) and owner in settings.admin_ids:
            await _notify(owner, f"⚠️ اسلاید استوریِ صف {queue_id} نیاز به بررسی دستی دارد؛ اسلاید بعدی آزاد نمی‌شود. بدون بررسی Zernio دوباره ارسال نکنید.", success=False)


async def _mark_sent(queue_id: int, result: dict, account_id: str) -> None:
    if result.get("status") not in ("publishing", "published") or not result.get("post_id"):
        raise OutboundQueueError("نتیجهٔ استوری از Zernio قطعی نیست؛ ارسال دوباره ممنوع است.")
    patch = json.dumps({
        "provider_status": "publishing",
        "provider_response_status": result["status"],
        "provider_account_id": account_id,
        "accepted_at": datetime.now(timezone.utc).isoformat(),
        "manual_review": False,
    }, ensure_ascii=False)
    await _safe_update("""
        UPDATE public.outbound_queue
           SET status = 'sent', provider_job_id = :post_id,
               payload = payload || CAST(:patch AS jsonb), updated_at = NOW(),
               last_error = NULL
         WHERE id = :id AND provider = 'zernio' AND action_type = 'story'
           AND status = 'processing'
        RETURNING id
    """, {"id": queue_id, "post_id": str(result["post_id"]), "patch": patch})


async def _release_child(parent_id: int, parent_payload: dict, published_at: datetime) -> int | None:
    """The next slide is pending but unclaimable until this CAS releases it."""
    row = await _safe_update("""
        UPDATE public.outbound_queue
           SET scheduled_for = GREATEST(
                   scheduled_for, CAST(:published_at AS TIMESTAMPTZ) +
                   make_interval(secs => (payload->>'story_delay_seconds')::INTEGER), NOW()
               ),
               payload = payload || '{"story_released":true}'::jsonb,
               updated_at = NOW()
         WHERE provider = 'zernio' AND action_type = 'story' AND status = 'pending'
           AND payload->>'story_released' = 'false'
           AND payload->>'story_previous_queue_id' = :parent
           AND payload->>'story_sequence_id' = :sequence
           AND payload->>'owner_id' = :owner
           AND (payload->>'story_delay_seconds')::INTEGER BETWEEN 300 AND 720
           AND EXISTS (
               SELECT 1 FROM public.outbound_queue p
                WHERE p.id = :parent_id AND p.provider = 'zernio'
                  AND p.action_type = 'story' AND p.status = 'sent'
                  AND p.payload->>'provider_status' = 'published'
           )
        RETURNING id
    """, {"parent_id": parent_id, "parent": str(parent_id),
           "sequence": str(parent_payload.get("story_sequence_id")),
           "owner": str(parent_payload.get("owner_id")), "published_at": published_at})
    return row["id"] if row else None


async def notify_one_confirmed_story(queue_id: int | None = None) -> bool:
    """CAS before non-idempotent Telegram send; never touches chat FSM."""
    if queue_id is None:
        async with SessionLocal() as session:
            queue_id = (await session.execute(text("""
                SELECT id FROM public.outbound_queue
                 WHERE provider = 'zernio' AND action_type = 'story'
                   AND status = 'sent' AND payload->>'provider_status' = 'published'
                   AND payload->>'success_notification_pending' = 'true'
                   AND NOT (payload ? 'success_notification_attempted_at')
                 ORDER BY updated_at LIMIT 1
            """))).scalar_one_or_none()
    if queue_id is None:
        return False
    row = await _safe_update("""
        UPDATE public.outbound_queue
           SET payload = payload || jsonb_build_object('success_notification_attempted_at', NOW()),
               updated_at = NOW()
         WHERE id = :id AND provider = 'zernio' AND action_type = 'story'
           AND status = 'sent' AND payload->>'provider_status' = 'published'
           AND payload->>'success_notification_pending' = 'true'
           AND NOT (payload ? 'success_notification_attempted_at')
        RETURNING payload
    """, {"id": queue_id})
    if not row:
        return False
    payload = row["payload"]
    owner = payload.get("owner_id")
    if not isinstance(owner, int) or owner not in settings.admin_ids:
        return False
    index, total = payload.get("story_index"), payload.get("story_total")
    delivered = await _notify(owner, f"✅ اسلاید {index} از {total} دنبالهٔ استوری منتشر شد.", success=True)
    if delivered:
        await _safe_update("""
            UPDATE public.outbound_queue
               SET payload = payload || jsonb_build_object('success_notification_sent_at', NOW()),
                   updated_at = NOW()
             WHERE id = :id AND action_type = 'story' AND provider = 'zernio'
             RETURNING id
        """, {"id": queue_id})
    return delivered


async def refresh_one_accepted_story(queue_id: int | None = None) -> dict | None:
    """GET only; require both Zernio post and Instagram target published."""
    job = await _story_job(queue_id)
    if not job or job["status"] != "sent" or not job["provider_job_id"]:
        return job
    payload = job["payload"]
    if payload.get("provider_status") == "published":
        # If confirmation persisted but a release was interrupted, retry the
        # DB-only release. It is idempotent and cannot publish a slide.
        stamp = payload.get("provider_published_at") or payload.get("confirmed_at")
        try:
            when = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
            if when.tzinfo is None:
                raise ValueError("naive timestamp")
        except (ValueError, TypeError):
            when = datetime.now(timezone.utc)
        await _release_child(job["id"], payload, when)
        return job
    if payload.get("provider_status") in ("failed", "partial"):
        return job
    account_id = payload.get("provider_account_id")
    if not account_id:
        return job
    try:
        remote = await get_zernio_publisher().get_post_status(
            job["provider_job_id"], expected_account_id=account_id
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("story status GET deferred id=%s error=%s", job["id"], type(exc).__name__)
        return job
    status = remote.get("status")
    if status not in ("published", "publishing", "failed", "partial"):
        return job
    if status == "publishing":
        return job  # leave timestamp so idle ticks can read again
    failed = status in ("failed", "partial")
    stamp = remote.get("published_at")
    try:
        published_at = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
        if published_at.tzinfo is None:
            raise ValueError("naive timestamp")
        published_at = published_at.astimezone(timezone.utc)
    except (ValueError, TypeError):
        published_at = datetime.now(timezone.utc)
    patch = {
        "provider_status": status,
        "last_checked_at": datetime.now(timezone.utc).isoformat(),
        "manual_review": failed,
    }
    if not failed:
        patch.update({
            "provider_published_at": published_at.isoformat(),
            "confirmed_at": datetime.now(timezone.utc).isoformat(),
            "success_notification_pending": True,
        })
    row = await _safe_update("""
        UPDATE public.outbound_queue
           SET status = CASE WHEN CAST(:failed AS boolean) THEN 'failed' ELSE 'sent' END,
               last_error = CASE WHEN CAST(:failed AS boolean) THEN CAST(:reason AS text)
                                 ELSE NULL END,
               payload = payload || CAST(:patch AS jsonb), updated_at = NOW()
         WHERE id = :id AND provider = 'zernio' AND action_type = 'story'
           AND status = 'sent' AND provider_job_id = :post_id
           AND payload->>'provider_status' = 'publishing'
        RETURNING id, payload
    """, {"id": job["id"], "post_id": job["provider_job_id"],
           "failed": failed, "reason": "Zernio انتشار استوری را ناموفق گزارش کرد؛ بررسی دستی لازم است.",
           "patch": json.dumps(patch, ensure_ascii=False)})
    if row and not failed:
        await _release_child(job["id"], row["payload"], published_at)
        await notify_one_confirmed_story(job["id"])
    elif row and failed:
        owner = row["payload"].get("owner_id")
        if isinstance(owner, int) and owner in settings.admin_ids:
            await _notify(owner, f"⚠️ اسلاید استوری {job['id']} در Zernio ناموفق بود؛ بقیهٔ دنباله آزاد نمی‌شود.", success=False)
    return await _story_job(job["id"])


async def reconcile_one_stalled_story() -> None:
    """A killed invocation gets only bounded GET/list, never another POST."""
    async with SessionLocal() as session:
        row = (await session.execute(text("""
            SELECT id, idempotency_key, status, provider_job_id, updated_at,
                   created_at, payload FROM public.outbound_queue
             WHERE provider = 'zernio' AND action_type = 'story'
               AND status = 'processing' AND provider_job_id IS NULL
               AND attempts = 1 AND max_attempts = 1
               AND updated_at < NOW() - INTERVAL '2 minutes'
               AND created_at > NOW() - INTERVAL '14 days'
             ORDER BY updated_at LIMIT 1
        """))).mappings().first()
        job = dict(row) if row else None
    if not job:
        return
    payload = job["payload"]
    username = payload.get("expected_username")
    owner = payload.get("owner_id")
    found = None
    try:
        if not isinstance(owner, int) or owner not in settings.admin_ids or not normalize_username(username):
            raise OutboundQueueError("مالکیت صف معتبر نیست.")
        publisher = get_zernio_publisher()
        accounts = await publisher.list_instagram_accounts()
        match = [a for a in accounts if a["active"] and normalize_username(a["username"]) == username]
        if len(match) != 1:
            raise OutboundQueueError("پیج در Zernio متصل یا یکتا نیست.")
        found = await publisher.find_queued_post(
            account_id=match[0]["id"], idempotency_key=job["idempotency_key"],
            content="استوری", queued_at=job["created_at"],
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("stalled story GET deferred id=%s error=%s", job["id"], type(exc).__name__)
    if found:
        await _mark_sent(job["id"], {"post_id": found, "status": "publishing"}, match[0]["id"])
        await refresh_one_accepted_story(job["id"])
    elif datetime.now(timezone.utc) - job["updated_at"] >= timedelta(minutes=15):
        await _mark_failed(job["id"], "نتیجهٔ استوری نامشخص است؛ Zernio را دستی بررسی کنید. ارسال دوباره خودکار نیست.")


async def process_one_zernio_story() -> dict:
    """The Story-only minute tick. Never claims action_type='post'."""
    # Prior accepted stories are GET-checked every minute so a published
    # parent can release its child. Reconciliation itself never issues POST.
    try:
        await refresh_one_accepted_story()
        await reconcile_one_stalled_story()
        await notify_one_confirmed_story()
    except Exception as exc:  # noqa: BLE001
        logger.warning("story read-only reconciliation deferred error=%s", type(exc).__name__)
    try:
        from app.services.storage_cleanup import cleanup_one_confirmed_media

        await cleanup_one_confirmed_media("story")
    except Exception as exc:  # noqa: BLE001
        logger.warning("story storage cleanup deferred error=%s", type(exc).__name__)

    async with SessionLocal() as session:
        queue_id = (await session.execute(text("SELECT id FROM public.claim_next_zernio_story_job()"))).scalar_one_or_none()
        await session.commit()  # durable at-most-once claim before any POST
    if queue_id is None:
        return {"claimed": 0, "published_requests": 0}

    async with SessionLocal() as session:
        row = (await session.execute(text("""
            SELECT id, idempotency_key, payload FROM public.outbound_queue
             WHERE id = :id AND status = 'processing'
               AND provider = 'zernio' AND action_type = 'story'
        """), {"id": queue_id})).mappings().first()
        if row is None:
            return {"claimed": 1, "published_requests": 0, "state": "manual_review"}
        payload = row["payload"]
        key = row["idempotency_key"]
        try:
            owner = payload["owner_id"]
            account_id = payload["local_account_id"]
            username = payload["expected_username"]
            urls = payload["media_urls"]
            path = payload.get("storage_paths")
            if (not isinstance(owner, int) or owner not in settings.admin_ids
                    or not isinstance(account_id, int) or payload.get("media_type") != "story"
                    or payload.get("origin") not in ("telegram_story", "miniapp_story")
                    or payload.get("story_released") is not True
                    or not isinstance(urls, list) or len(urls) != 1
                    or path != [owned_supabase_storage_path(urls[0])]
                    or not path[0]):
                raise OutboundQueueError("دادهٔ استوریِ صف معتبر نیست؛ انتشار متوقف شد.")
            _media_items(urls, "story")
            account = await session.get(InstagramAccount, account_id)
            if (account is None or account.owner_id != owner or account.status != "connected"
                    or normalize_username(account.username) != username):
                raise OutboundQueueError("مالکیت یا اتصال پیج استوری تغییر کرده است؛ انتشار متوقف شد.")
            publisher = get_zernio_publisher()
            if not publisher.configured:
                raise OutboundQueueError("اتصال Zernio تنظیم نشده است؛ انتشار استوری متوقف شد.")
        except (KeyError, ValueError, TypeError, OutboundQueueError, ZernioPublishError) as exc:
            reason = str(exc) if isinstance(exc, (OutboundQueueError, ZernioPublishError)) else "دادهٔ استوری ناقص است؛ بررسی دستی لازم است."
            await _mark_failed(queue_id, reason)
            return {"claimed": 1, "published_requests": 0, "state": "failed"}

    try:
        providers = await publisher.list_instagram_accounts()  # GET only
        match = [a for a in providers if a["active"] and normalize_username(a["username"]) == username]
        if len(match) != 1:
            raise OutboundQueueError("پیج در Zernio متصل و فعال نیست.")
    except Exception as exc:  # noqa: BLE001
        await _mark_failed(queue_id, str(exc) if isinstance(exc, (OutboundQueueError, ZernioPublishError))
                           else "بررسی اتصال Zernio ممکن نشد.")
        return {"claimed": 1, "published_requests": 0, "state": "failed"}

    async with SessionLocal() as session:
        rate = (await session.execute(text("""
            SELECT allowed, retry_after
              FROM public.reserve_zernio_story_slot(:page, :daily, :spacing)
        """), {"page": username, "daily": ZERNIO_STORY_DAILY_LIMIT,
               "spacing": ZERNIO_STORY_MIN_SPACING_SECONDS})).mappings().one()
        if not rate["allowed"]:
            await session.execute(text("""
                UPDATE public.outbound_queue
                   SET status = 'pending', attempts = GREATEST(attempts - 1, 0),
                       scheduled_for = GREATEST(:after, NOW() + INTERVAL '1 minute'),
                       updated_at = NOW(), last_error = NULL
                 WHERE id = :id AND provider = 'zernio' AND action_type = 'story'
                   AND status = 'processing'
            """), {"id": queue_id, "after": rate["retry_after"]})
            await session.commit()
            return {"claimed": 1, "published_requests": 0, "state": "deferred"}
        await session.commit()

    result: dict | None = None
    reason = "نتیجهٔ انتشار استوری نامشخص است؛ پیش از تلاش دیگر Zernio را بررسی کنید."
    try:
        result = await publisher.publish_post(
            zernio_account_id=match[0]["id"], expected_username=username,
            caption="", media_urls=urls, media_type="story",
            idempotency_key=key, platform_specific_data=None,
        )  # ONE real POST, only in the protected Story Worker
    except Exception as exc:  # noqa: BLE001
        if isinstance(exc, ZernioPublishError):
            reason = str(exc)
        logger.warning("story outcome uncertain queue_id=%s error=%s", queue_id, type(exc).__name__)
    finally:
        try:
            if result is None:
                await _mark_failed(queue_id, reason)
            else:
                await _mark_sent(queue_id, result, match[0]["id"])
        except Exception:  # noqa: BLE001
            logger.exception("story outcome finalization failed queue_id=%s; NEVER resubmit", queue_id)
            raise
    if result is None:
        return {"claimed": 1, "published_requests": 1, "state": "failed_manual_review"}
    try:
        checked = await refresh_one_accepted_story(queue_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("story confirmation deferred queue_id=%s error=%s", queue_id, type(exc).__name__)
        checked = None
    confirmed = bool(checked and checked["payload"].get("provider_status") == "published")
    return {"claimed": 1, "published_requests": 1,
            "state": "published" if confirmed else "accepted", "queue_id": queue_id}
