"""Delete only own Telegram media AFTER verified Zernio/Instagram published.

This is a separate, idempotent Storage API operation. A missing/delete-failed
object never alters queue publication or message history; the next appropriate
Worker tick may retry. External URLs and objects still needed by a live draft
or another unconfirmed job are never deleted.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from sqlalchemy import text

from app.core.config import get_settings
from app.core.database import SessionLocal
from app.services.media_storage import delete_owned_supabase_media, owned_supabase_storage_path

settings = get_settings()
logger = logging.getLogger("gramma.storage_cleanup")


async def _block_if_referenced(queue_id: int, url: str) -> bool:
    async with SessionLocal() as session:
        # The current/other CONFIRMED published rows are historical references,
        # not live consumers. Every other active/uncertain consumer blocks GC.
        used = (await session.execute(text("""
            SELECT EXISTS (
                SELECT 1 FROM public.outbound_queue q
                 WHERE q.id <> :id AND q.payload->'media_urls' ? :url
                   AND q.status <> 'cancelled'
                   AND NOT (q.status = 'sent' AND q.payload->>'provider_status' = 'published')
            ) OR EXISTS (
                SELECT 1 FROM public.telegram_post_drafts d,
                             jsonb_array_elements(d.media) AS part
                 WHERE d.status IN ('collecting', 'ready')
                   AND part->>'url' = :url
            ) OR EXISTS (
                SELECT 1 FROM public.telegram_story_slides s
                  JOIN public.telegram_story_sequences seq ON seq.id = s.sequence_id
                 WHERE seq.status IN ('awaiting_count', 'collecting')
                   AND s.url = :url
            ) OR EXISTS (
                SELECT 1 FROM public.miniapp_publish_media m
                  JOIN public.miniapp_publish_drafts d ON d.id = m.draft_id
                 WHERE m.url = :url AND d.status = 'collecting'
                   AND d.expires_at > NOW()
            ) OR EXISTS (
                SELECT 1 FROM public.media m
                  JOIN public.posts p ON p.id = m.post_id
                 WHERE m.url = :url
                   AND p.status IN ('draft', 'scheduled', 'publishing', 'failed')
            ) AS referenced
        """), {"id": queue_id, "url": url})).scalar_one()
        return bool(used)


async def _progress(queue_id: int, action_type: str, *, path: str | None = None,
                    done: bool = False, skipped: bool = False) -> None:
    async with SessionLocal() as session:
        row = (await session.execute(text("""
            SELECT payload FROM public.outbound_queue
             WHERE id = :id AND provider = 'zernio' AND action_type = :kind
               AND status = 'sent' AND payload->>'provider_status' = 'published'
             FOR UPDATE
        """), {"id": queue_id, "kind": action_type})).mappings().first()
        if not row:
            return
        payload = dict(row["payload"])
        deleted = list(payload.get("storage_deleted_paths") or [])
        if path and path not in deleted:
            deleted.append(path)
        payload["storage_deleted_paths"] = deleted
        payload.pop("storage_cleanup_claimed_at", None)
        if done:
            payload["storage_cleanup_done"] = True
        if skipped:
            payload["storage_cleanup_skipped"] = True
        await session.execute(text("""
            UPDATE public.outbound_queue
               SET payload = CAST(:payload AS jsonb), updated_at = NOW()
             WHERE id = :id AND provider = 'zernio' AND action_type = :kind
               AND status = 'sent' AND payload->>'provider_status' = 'published'
        """), {"id": queue_id, "kind": action_type,
               "payload": json.dumps(payload, ensure_ascii=False)})
        await session.commit()


async def cleanup_one_confirmed_media(action_type: str) -> dict:
    """At most one object per tick, and never a provider POST.

    Caller chooses 'post' on the original post job, or 'story' on the new
    story-only job. Neither changes the other's queue status or cron cadence.
    """
    if action_type not in ("post", "story"):
        raise ValueError("unsupported cleanup action")
    async with SessionLocal() as session:
        row = (await session.execute(text("""
            SELECT id, payload FROM public.outbound_queue
             WHERE provider = 'zernio' AND action_type = :kind AND status = 'sent'
               AND payload->>'provider_status' = 'published'
               AND payload->>'origin' IN
                   ('telegram_photo','telegram_video','telegram_album','telegram_story','scheduled_post',
                    'miniapp','miniapp_story')
               AND payload->>'storage_cleanup_done' IS DISTINCT FROM 'true'
               AND payload->>'storage_cleanup_skipped' IS DISTINCT FROM 'true'
               AND (NOT (payload ? 'storage_cleanup_claimed_at') OR
                    (payload->>'storage_cleanup_claimed_at')::timestamptz < NOW() - INTERVAL '2 minutes')
             ORDER BY updated_at ASC, id ASC LIMIT 1 FOR UPDATE SKIP LOCKED
        """), {"kind": action_type})).mappings().first()
        if not row:
            return {"selected": 0, "deleted": 0}
        payload = dict(row["payload"])
        owner = payload.get("owner_id")
        urls = payload.get("media_urls")
        paths = payload.get("storage_paths")
        if (not isinstance(owner, int) or owner not in settings.admin_ids
                or not isinstance(urls, list) or not 1 <= len(urls) <= 10
                or any(not isinstance(url, str) for url in urls)):
            invalid = True
            paths = []
        else:
            derived = [owned_supabase_storage_path(url) for url in urls]
            # Old Telegram queue rows lack the explicit path; deriving it only
            # from our exact HTTPS project/bucket/UUID format remains safe.
            invalid = any(item is None for item in derived) or (
                paths is not None and paths != derived
            )
            paths = derived if paths is None else paths
        payload["storage_cleanup_claimed_at"] = datetime.now(timezone.utc).isoformat()
        await session.execute(text("""
            UPDATE public.outbound_queue
               SET payload = CAST(:payload AS jsonb), updated_at = NOW()
             WHERE id = :id AND provider = 'zernio' AND action_type = :kind
               AND status = 'sent' AND payload->>'provider_status' = 'published'
        """), {"id": row["id"], "kind": action_type,
               "payload": json.dumps(payload, ensure_ascii=False)})
        await session.commit()
    if invalid:
        logger.warning("GC skipped unsafe/unowned media queue_id=%s", row["id"])
        await _progress(row["id"], action_type, skipped=True)
        return {"selected": 1, "deleted": 0, "skipped": True}
    already = set(payload.get("storage_deleted_paths") or [])
    for url, path in zip(urls, paths):
        if path in already:
            continue
        if await _block_if_referenced(row["id"], url):
            await _progress(row["id"], action_type)  # retry after consumers finish
            return {"selected": 1, "deleted": 0, "referenced": True}
        try:
            removed = await delete_owned_supabase_media(path)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Storage API deletion deferred queue_id=%s error=%s",
                           row["id"], type(exc).__name__)
            removed = False
        if not removed:
            await _progress(row["id"], action_type)
            return {"selected": 1, "deleted": 0, "deferred": True}
        await _progress(row["id"], action_type, path=path,
                        done=(len(already) + 1 == len(paths)))
        return {"selected": 1, "deleted": 1}
    await _progress(row["id"], action_type, done=True)
    return {"selected": 1, "deleted": 0, "done": True}
