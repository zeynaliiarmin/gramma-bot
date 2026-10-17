"""Durable, private Telegram Story FSM; only «پایان و صف» can enqueue.

Each slide becomes its own story queue job. No function in this module calls a
publisher or Worker. Telegram album fragments are ordered by message_id and
cannot be finalized before the quiet window or while an upload is incomplete.
"""

from __future__ import annotations

import json
import logging
import secrets
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, text

from app.core.config import get_settings
from app.core.database import SessionLocal
from app.db.repositories import get_active_account
from app.models import InstagramAccount, TelegramMediaIntent, TelegramStorySequence, TelegramStorySlide
from app.services.limits import (
    TELEGRAM_DRAFT_EXPIRY_DAYS,
    TELEGRAM_STORY_ALBUM_QUIET_SECONDS,
    TELEGRAM_STORY_SEQUENCE_MAX_ITEMS,
    TELEGRAM_STORY_VIDEO_MIN_SECONDS,
    TELEGRAM_STORY_VIDEO_MAX_SECONDS,
    TELEGRAM_STORY_IMAGE_MAX_BYTES,
    TELEGRAM_BOT_DOWNLOAD_MAX_BYTES,
)
from app.services.media_storage import TelegramPostMediaError, owned_supabase_storage_path
from app.services.zernio_publishing import ZernioPublishError, _media_items, normalize_username

settings = get_settings()
logger = logging.getLogger("gramma.story_fsm")


class StoryError(ValueError):
    """Safe message suitable for display to the private administrator."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _private(owner_id: int, chat_id: int) -> None:
    if owner_id != chat_id or owner_id not in settings.admin_ids:
        raise StoryError("استوری فقط در چت خصوصی مدیر قابل ثبت است.")


async def _active(session, owner_id: int, chat_id: int, *, lock: bool = False):
    stmt = select(TelegramStorySequence).where(
        TelegramStorySequence.chat_id == chat_id,
        TelegramStorySequence.owner_id == owner_id,
        TelegramStorySequence.status.in_(("awaiting_count", "collecting")),
    )
    if lock:
        stmt = stmt.with_for_update()
    sequence = (await session.execute(stmt)).scalar_one_or_none()
    if sequence and _aware(sequence.expires_at) <= _now():
        sequence.status = "expired"
        sequence.updated_at = _now()
        await session.flush()
        return None
    return sequence


async def _snapshot(session, sequence: TelegramStorySequence) -> dict:
    slides = (await session.execute(select(TelegramStorySlide).where(
        TelegramStorySlide.sequence_id == sequence.id,
    ).order_by(TelegramStorySlide.message_id))).scalars().all()
    return {
        "id": sequence.id, "status": sequence.status,
        "declared_count": sequence.declared_count,
        "page": sequence.expected_username,
        "ready_count": sum(slide.status == "ready" for slide in slides),
        "uploading_count": sum(slide.status == "uploading" for slide in slides),
        "failed_count": sum(slide.status == "failed" for slide in slides),
        "last_album_at": sequence.last_album_at,
    }


async def active_story(owner_id: int, chat_id: int) -> dict | None:
    if owner_id != chat_id or owner_id not in settings.admin_ids:
        return None
    async with SessionLocal() as session:
        sequence = await _active(session, owner_id, chat_id, lock=True)
        result = await _snapshot(session, sequence) if sequence else None
        await session.commit()  # expire stale FSM rows without touching drafts
        return result


async def start_story(owner_id: int, chat_id: int) -> dict:
    _private(owner_id, chat_id)
    async with SessionLocal() as session:
        # Do not silently cancel an explicitly selected Post/Reel waiting mode.
        intent = (await session.execute(select(TelegramMediaIntent).where(
            TelegramMediaIntent.chat_id == chat_id,
            TelegramMediaIntent.owner_id == owner_id,
        ).with_for_update())).scalar_one_or_none()
        if intent and _aware(intent.expires_at) > _now():
            raise StoryError("ابتدا حالت انتظار پست/ریلز را با دکمهٔ «لغو» همان پنل ببندید.")
        sequence = await _active(session, owner_id, chat_id, lock=True)
        if not sequence:
            account = await get_active_account(session, owner_id)
            if (account is None or account.owner_id != owner_id or account.status != "connected"
                    or not normalize_username(account.username)):
                raise StoryError("پیج فعال و متصلِ مدیر پیدا نشد.")
            sequence = TelegramStorySequence(
                owner_id=owner_id, chat_id=chat_id, local_account_id=account.id,
                expected_username=normalize_username(account.username),
                status="awaiting_count",
                expires_at=_now() + timedelta(days=TELEGRAM_DRAFT_EXPIRY_DAYS),
            )
            session.add(sequence)
            await session.flush()
        result = await _snapshot(session, sequence)
        await session.commit()
        return result


async def set_story_count(owner_id: int, chat_id: int, raw: str) -> dict:
    _private(owner_id, chat_id)
    digits = (raw or "").strip().translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789"))
    if not digits.isascii() or not digits.isdecimal() or len(digits) > 2:
        raise StoryError("تعداد اسلاید را به صورت یک عدد معتبر بفرستید.")
    count = int(digits)
    if not 1 <= count <= TELEGRAM_STORY_SEQUENCE_MAX_ITEMS:
        raise StoryError(f"تعداد اولیه باید بین 1 تا {TELEGRAM_STORY_SEQUENCE_MAX_ITEMS} باشد؛ این فقط یک برآورد است.")
    async with SessionLocal() as session:
        sequence = await _active(session, owner_id, chat_id, lock=True)
        if sequence is None or sequence.status != "awaiting_count":
            raise StoryError("پیام تعداد مربوط به حالت انتظار فعلی استوری نیست؛ از دکمهٔ استوری استفاده کنید.")
        sequence.declared_count = count
        sequence.status = "collecting"
        sequence.updated_at = _now()
        result = await _snapshot(session, sequence)
        await session.commit()
        return result


async def story_media_context(owner_id: int, chat_id: int, message_id: int,
                              media_group_id: str | None) -> str | None:
    """Route private media before the Post router, including redeliveries."""
    if owner_id != chat_id or owner_id not in settings.admin_ids:
        return None
    async with SessionLocal() as session:
        existing = (await session.execute(select(TelegramStorySlide.id).where(
            TelegramStorySlide.chat_id == chat_id, TelegramStorySlide.message_id == message_id,
        ))).scalar_one_or_none()
        if existing is not None:
            return "duplicate"
        if media_group_id:
            tombstone = (await session.execute(select(TelegramStorySequence.id).where(
                TelegramStorySequence.chat_id == chat_id,
                TelegramStorySequence.invalid_media_group_id == media_group_id,
                TelegramStorySequence.status == "invalid",
            ).limit(1))).scalar_one_or_none()
            if tombstone is not None:
                return "invalid_album"
            # Check older queued groups BEFORE a new active sequence: a late
            # fragment cannot sneak into a later Story or become a feed post.
            late = (await session.execute(select(TelegramStorySequence.status).join(
                TelegramStorySlide,
                TelegramStorySlide.sequence_id == TelegramStorySequence.id,
            ).where(
                TelegramStorySlide.chat_id == chat_id,
                TelegramStorySlide.media_group_id == media_group_id,
                TelegramStorySequence.status.in_(("queued", "invalid")),
            ).order_by(TelegramStorySequence.id.desc()).limit(1))).scalar_one_or_none()
            if late:
                return "late_album" if late == "queued" else "invalid_album"
        sequence = await _active(session, owner_id, chat_id, lock=True)
        result = sequence.status if sequence else None
        await session.commit()
        return result


async def reserve_story_slide(owner_id: int, chat_id: int, message_id: int,
                              media_group_id: str | None, kind: str) -> dict:
    _private(owner_id, chat_id)
    if kind not in ("image", "video"):
        raise StoryError("استوری فقط عکس یا ویدیو می‌پذیرد.")
    async with SessionLocal() as session:
        sequence = await _active(session, owner_id, chat_id, lock=True)
        if not sequence or sequence.status != "collecting":
            raise StoryError("حالت دریافت استوری فعال نیست؛ ابتدا دکمهٔ استوری و تعداد را ثبت کنید.")
        previous = (await session.execute(select(TelegramStorySlide).where(
            TelegramStorySlide.chat_id == chat_id, TelegramStorySlide.message_id == message_id,
        ))).scalar_one_or_none()
        if previous:
            if previous.sequence_id != sequence.id or previous.status != "uploading":
                return {"action": "duplicate", "sequence_id": previous.sequence_id}
            if _now() - _aware(previous.updated_at) < timedelta(minutes=3):
                return {"action": "duplicate", "sequence_id": sequence.id}
            # A killed webhook before the URL was committed may safely retry
            # the storage upload, but NEVER creates a second slide/queue job.
            previous.updated_at = _now()
            slide = previous
        else:
            total = (await session.execute(select(TelegramStorySlide.id).where(
                TelegramStorySlide.sequence_id == sequence.id,
                TelegramStorySlide.status.in_(("ready", "uploading")),
            ))).scalars().all()
            if len(total) >= TELEGRAM_STORY_SEQUENCE_MAX_ITEMS:
                raise StoryError(f"حداکثر {TELEGRAM_STORY_SEQUENCE_MAX_ITEMS} اسلاید برای یک دنباله مجاز است.")
            slide = TelegramStorySlide(
                sequence_id=sequence.id, owner_id=owner_id, chat_id=chat_id,
                message_id=message_id, media_group_id=media_group_id,
                kind=kind, status="uploading",
            )
            session.add(slide)
        sequence.updated_at = _now()
        if media_group_id:
            sequence.last_album_at = _now()
        await session.flush()
        slide_id = slide.id
        sequence_id = sequence.id
        await session.commit()
        return {"action": "upload", "slide_id": slide_id, "sequence_id": sequence_id}


async def complete_story_slide(slide_id: int, owner_id: int, chat_id: int,
                               *, url: str | None = None, error: str | None = None) -> dict:
    _private(owner_id, chat_id)
    async with SessionLocal() as session:
        existing = await session.get(TelegramStorySlide, slide_id)
        if not existing or existing.owner_id != owner_id or existing.chat_id != chat_id:
            raise StoryError("اسلاید متعلق به این چت نیست.")
        # Same lock order as End/reserve: sequence first, then slide. A late
        # callback can never deadlock a simultaneous media upload/finalization.
        sequence = (await session.execute(select(TelegramStorySequence).where(
            TelegramStorySequence.id == existing.sequence_id,
        ).with_for_update())).scalar_one()
        slide = (await session.execute(select(TelegramStorySlide).where(
            TelegramStorySlide.id == slide_id,
        ).with_for_update())).scalar_one()
        if slide.status == "ready":
            result = await _snapshot(session, sequence)
            await session.commit()
            return result
        if sequence.status != "collecting" or slide.status != "uploading":
            raise StoryError("این دنباله دیگر در حالت دریافت رسانه نیست.")
        if error:
            slide.status = "failed"
            slide.error_text = error[:300]
        elif url and owned_supabase_storage_path(url):
            slide.status = "ready"
            slide.url = url
        else:
            slide.status = "failed"
            slide.error_text = "نشانی ذخیرهٔ رسانه معتبر نیست."
        slide.updated_at = _now()
        sequence.updated_at = _now()
        if slide.media_group_id:
            sequence.last_album_at = _now()
        await session.flush()
        result = await _snapshot(session, sequence)
        await session.commit()
        return result


async def invalidate_story_album(owner_id: int, chat_id: int, group_id: str, reason: str) -> None:
    """Tombstone a mixed/unsupported album, including yet-unseen fragments."""
    _private(owner_id, chat_id)
    if not group_id or len(group_id) > 255:
        return
    async with SessionLocal() as session:
        sequence = await _active(session, owner_id, chat_id, lock=True)
        if sequence and sequence.status == "collecting":
            sequence.status = "invalid"
            sequence.invalid_media_group_id = group_id
            sequence.updated_at = _now()
            logger.warning("story album invalid seq=%s reason=%s", sequence.id, reason[:80])
            await session.commit()


async def suppress_late_story_album(owner_id: int, chat_id: int, group_id: str) -> int:
    """Never treat late fragments as feed posts; cancel only unclaimed stories."""
    _private(owner_id, chat_id)
    async with SessionLocal() as session:
        sequence = (await session.execute(select(TelegramStorySequence).join(
            TelegramStorySlide,
            TelegramStorySlide.sequence_id == TelegramStorySequence.id,
        ).where(
            TelegramStorySequence.owner_id == owner_id,
            TelegramStorySequence.chat_id == chat_id,
            TelegramStorySequence.status == "queued",
            TelegramStorySlide.media_group_id == group_id,
        ).order_by(TelegramStorySequence.id.desc()).limit(1)
          .with_for_update(of=TelegramStorySequence))).scalar_one_or_none()
        if sequence is None:
            return 0
        cancelled = (await session.execute(text("""
            UPDATE public.outbound_queue SET status = 'cancelled', updated_at = NOW(),
                   last_error = 'Late Telegram Story album fragment; verify before requeue'
             WHERE provider = 'zernio' AND action_type = 'story'
               AND status = 'pending' AND payload->>'story_sequence_id' = :sequence
               AND payload->>'owner_id' = :owner
            RETURNING id
        """), {"sequence": str(sequence.id), "owner": str(owner_id)})).scalars().all()
        sequence.status = "invalid"
        sequence.updated_at = _now()
        await session.commit()
        return len(cancelled)


async def cancel_story(sequence_id: int, owner_id: int, chat_id: int) -> None:
    _private(owner_id, chat_id)
    async with SessionLocal() as session:
        sequence = (await session.execute(select(TelegramStorySequence).where(
            TelegramStorySequence.id == sequence_id,
        ).with_for_update())).scalar_one_or_none()
        if not sequence or sequence.owner_id != owner_id or sequence.chat_id != chat_id:
            raise StoryError("این دنباله متعلق به شما نیست.")
        if sequence.status not in ("awaiting_count", "collecting"):
            raise StoryError("استوری قبلاً صف شده یا حالت انتظار آن پایان یافته است؛ لغو ممکن نیست.")
        sequence.status = "cancelled"
        sequence.updated_at = _now()
        await session.commit()


async def finish_story(sequence_id: int, owner_id: int, chat_id: int) -> dict:
    """THE sole Story queue insertion, atomically gated by the End button."""
    _private(owner_id, chat_id)
    if not settings.zernio_api_key:
        raise StoryError("اتصال Zernio تنظیم نشده است؛ استوری وارد صف نشد.")
    async with SessionLocal() as session:
        sequence = (await session.execute(select(TelegramStorySequence).where(
            TelegramStorySequence.id == sequence_id,
        ).with_for_update())).scalar_one_or_none()
        if not sequence or sequence.owner_id != owner_id or sequence.chat_id != chat_id:
            raise StoryError("این دنباله متعلق به شما نیست.")
        if sequence.status == "queued":
            rows = (await session.execute(select(TelegramStorySlide.queue_id).where(
                TelegramStorySlide.sequence_id == sequence_id,
                TelegramStorySlide.queue_id.is_not(None),
            ).order_by(TelegramStorySlide.message_id))).scalars().all()
            return {"queue_ids": rows, "created": False}
        if sequence.status != "collecting" or _aware(sequence.expires_at) <= _now():
            raise StoryError("این حالت دریافت استوری دیگر فعال نیست.")
        if sequence.last_album_at and _aware(sequence.last_album_at) > _now() - timedelta(seconds=TELEGRAM_STORY_ALBUM_QUIET_SECONDS):
            raise StoryError("آلبوم هنوز در حال دریافت است؛ کمی صبر کنید و دوباره «پایان و صف» را بزنید.")
        slides = (await session.execute(select(TelegramStorySlide).where(
            TelegramStorySlide.sequence_id == sequence.id,
        ).order_by(TelegramStorySlide.message_id).with_for_update())).scalars().all()
        if any(slide.status == "uploading" for slide in slides):
            raise StoryError("ذخیرهٔ یکی از اسلایدها هنوز کامل نشده است؛ کمی بعد دوباره پایان را بزنید.")
        ready = [slide for slide in slides if slide.status == "ready"]
        if not ready:
            raise StoryError("حداقل یک اسلایدِ سالم لازم است؛ عکس/ویدیو بفرستید.")
        if len(ready) > TELEGRAM_STORY_SEQUENCE_MAX_ITEMS:
            raise StoryError("تعداد اسلایدها بیش از سقف ایمنی است.")
        account = await session.get(InstagramAccount, sequence.local_account_id)
        if (account is None or account.owner_id != owner_id or account.status != "connected"
                or normalize_username(account.username) != sequence.expected_username):
            raise StoryError("پیج مقصدِ استوری دیگر متصل یا متعلق به شما نیست؛ چیزی به صف نرفت.")
        now = _now()
        due = now
        queue_ids: list[int] = []
        previous_id: int | None = None
        for index, slide in enumerate(ready):
            try:
                _media_items([slide.url], "story")
            except ZernioPublishError as exc:
                raise StoryError(str(exc)) from exc
            storage_path = owned_supabase_storage_path(slide.url)
            if not storage_path:
                raise StoryError("فایل استوری متعلق به فضای امن پروژه نیست؛ چیزی به صف نرفت.")
            delay = 0 if index == 0 else 300 + secrets.randbelow(421)
            due += timedelta(seconds=delay)
            key = f"tg:story:{chat_id}:{sequence.id}:{slide.message_id}"
            payload = {
                "owner_id": owner_id,
                "local_account_id": sequence.local_account_id,
                "expected_username": sequence.expected_username,
                "caption": "",
                "media_urls": [slide.url],
                "media_type": "story", "origin": "telegram_story",
                "telegram_chat_id": chat_id, "telegram_message_id": slide.message_id,
                "story_sequence_id": sequence.id, "story_index": index + 1,
                "story_total": len(ready), "story_previous_queue_id": previous_id,
                "story_delay_seconds": delay, "story_released": index == 0,
                "storage_paths": [storage_path],
            }
            queue_id = (await session.execute(text("""
                INSERT INTO public.outbound_queue
                    (idempotency_key, action_type, provider, payload, status,
                     scheduled_for, max_attempts)
                VALUES (:key, 'story', 'zernio', CAST(:payload AS jsonb),
                        'pending', :due, 1)
                ON CONFLICT (idempotency_key) DO NOTHING RETURNING id
            """), {"key": key, "payload": json.dumps(payload, ensure_ascii=False),
                   "due": due})).scalar_one_or_none()
            if queue_id is None:
                row = (await session.execute(text("""
                    SELECT id FROM public.outbound_queue
                     WHERE idempotency_key = :key AND provider = 'zernio'
                       AND action_type = 'story' AND payload->>'owner_id' = :owner
                       AND payload->>'story_sequence_id' = :sequence
                """), {"key": key, "owner": str(owner_id),
                       "sequence": str(sequence.id)})).scalar_one_or_none()
                if row is None:
                    raise StoryError("شناسهٔ اسلاید قبلاً برای درخواست دیگری مصرف شده است.")
                queue_id = row
            slide.queue_id = queue_id
            previous_id = queue_id
            queue_ids.append(queue_id)
        sequence.status = "queued"
        sequence.updated_at = _now()
        await session.commit()
        return {"queue_ids": queue_ids, "created": True}


def describe_story_media(message) -> dict:
    """Stories use the provider's documented 9:16 and <=60s contract."""
    from app.services.media_storage import describe_telegram_post_media

    # Story albums can contain each slide independently; call the shared
    # Telegram MIME/file-size checks with a group ID only to avoid Reel rules.
    # For single Story videos the shared Reel duration limit is still >=60s.
    try:
        info = describe_telegram_post_media(message)
    except TelegramPostMediaError as exc:
        raise StoryError(str(exc).replace("پست", "استوری").replace("ریلز", "استوری")) from exc
    if not 0.52 <= info["width"] / info["height"] <= 0.61:
        raise StoryError("اسلاید استوری باید عمودی نزدیک به نسبت 9:16 باشد.")
    if info["kind"] == "video" and not (
        isinstance(info["duration"], int)
        and TELEGRAM_STORY_VIDEO_MIN_SECONDS <= info["duration"] <= TELEGRAM_STORY_VIDEO_MAX_SECONDS
    ):
        raise StoryError("مدت ویدیوی استوری باید بین 3 و 60 ثانیه باشد.")
    if info["kind"] == "image" and isinstance(info["file_size"], int) and info["file_size"] > TELEGRAM_STORY_IMAGE_MAX_BYTES:
        raise StoryError("اندازهٔ عکس استوری نباید از 8 MB بیشتر باشد.")
    if isinstance(info["file_size"], int) and info["file_size"] > TELEGRAM_BOT_DOWNLOAD_MAX_BYTES:
        raise StoryError("فایل بزرگ‌تر از سقف دریافت 20 MB بات است.")
    return info
