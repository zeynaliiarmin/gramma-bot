"""Tehran/Jalali selection and guarded pending-queue editing.

Buttons and the existing Mini-App share these checks. Selecting a time for a
DRAFT never enqueues it; only its original final inline confirmation does.
Queue edits use a row lock and are rejected once a Worker may claim the row.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import select, text

from app.core.config import get_settings
from app.core.database import SessionLocal
from app.models import InstagramAccount, TelegramPostDraft
from app.services.instagram_options import (
    InstagramOptionError, validate_instagram_caption, validate_instagram_options,
)
from app.services.limits import (
    TELEGRAM_SCHEDULE_MAX_DAYS,
    TELEGRAM_SCHEDULE_MIN_LEAD_SECONDS,
    TELEGRAM_SHORT_ON_POST_CAPTION_LIMIT,
)
from app.services.telegram_post_drafts import TelegramDraftError, draft_media_type, ordered_media
from app.services.zernio_publishing import ZernioPublishError, _media_items
from app.utils import jalali

settings = get_settings()
TEHRAN = ZoneInfo("Asia/Tehran")
_ORIGINS = ("telegram_photo", "telegram_video", "telegram_album")


class ScheduleError(ValueError):
    """User-facing validation failure; not a reason to retry publication."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _admin(owner_id: int, chat_id: int | None = None) -> None:
    if owner_id not in settings.admin_ids or (chat_id is not None and owner_id != chat_id):
        raise ScheduleError("تقویم انتشار فقط در چت خصوصی مدیر فعال است.")


def _valid_target(when: datetime) -> datetime:
    if not isinstance(when, datetime) or when.tzinfo is None:
        raise ScheduleError("زمان انتشار باید دارای منطقهٔ زمانی تهران باشد.")
    target = when.astimezone(timezone.utc)
    now = _now()
    if target < now + timedelta(seconds=TELEGRAM_SCHEDULE_MIN_LEAD_SECONDS):
        raise ScheduleError("زمان انتشار باید حداقل 5 دقیقه در آینده باشد.")
    if target > now + timedelta(days=TELEGRAM_SCHEDULE_MAX_DAYS):
        raise ScheduleError("بیش از 365 روز در آینده را نمی‌توان زمان‌بندی کرد.")
    return target


def jalali_choice(jy: int, jm: int, jd: int, hour: int, minute: int) -> datetime:
    """Canonical Jalali round-trip rejects fake Esfand 30 and rollover dates."""
    if (any(type(v) is not int for v in (jy, jm, jd, hour, minute))
            or not 1300 <= jy <= 1500 or not 1 <= jm <= 12 or not 1 <= jd <= 31
            or not 0 <= hour <= 23 or not 0 <= minute <= 59):
        raise ScheduleError("روز یا ساعت شمسی نامعتبر است.")
    try:
        local = jalali.from_jalali(jy, jm, jd, hour, minute).astimezone(TEHRAN)
        if jalali.to_jalali(local) != (jy, jm, jd) or (local.hour, local.minute) != (hour, minute):
            raise ValueError("invalid Jalali rollover")
    except (ValueError, OverflowError) as exc:
        raise ScheduleError("تاریخ شمسی واقعی نیست؛ روز دیگری انتخاب کنید.") from exc
    return _valid_target(local)


def quick_choice(key: str) -> datetime:
    now = datetime.now(TEHRAN)
    if key == "1h":
        return _valid_target(now + timedelta(hours=1))
    if key == "3h":
        return _valid_target(now + timedelta(hours=3))
    if key == "tomorrow":
        tomorrow = now.date() + timedelta(days=1)
        return _valid_target(datetime(tomorrow.year, tomorrow.month, tomorrow.day,
                                      9, 0, tzinfo=TEHRAN))
    raise ScheduleError("گزینهٔ زمان‌بندی معتبر نیست.")


async def set_draft_schedule(draft_id: int, owner_id: int, when: datetime,
                             *, panel_message_id: int | None = None) -> dict:
    _admin(owner_id, owner_id)
    when = _valid_target(when)
    async with SessionLocal() as session:
        draft = (await session.execute(select(TelegramPostDraft).where(
            TelegramPostDraft.id == draft_id,
            TelegramPostDraft.owner_id == owner_id,
            TelegramPostDraft.chat_id == owner_id,
        ).with_for_update())).scalar_one_or_none()
        if not draft or draft.status != "ready" or draft.expires_at <= _now():
            raise ScheduleError("این پیش‌نویس دیگر قابل زمان‌بندی نیست.")
        if panel_message_id is not None and draft.panel_message_id != panel_message_id:
            raise ScheduleError("این پنل قدیمی است؛ زمان‌بندی ثبت نشد.")
        try:
            draft_media_type(draft)
        except TelegramDraftError as exc:
            raise ScheduleError(str(exc)) from exc
        draft.scheduled_for = when
        draft.updated_at = _now()
        await session.commit()
        return {"id": draft.id, "scheduled_for": when,
                "jalali": jalali.to_jalali_str(when)}


async def get_owned_scheduled_post(session, queue_id: int, owner_id: int,
                                   *, lock: bool = False) -> dict | None:
    _admin(owner_id)
    statement = text("""
        SELECT id, idempotency_key, status, scheduled_for, payload,
               created_at, updated_at, provider_job_id
          FROM public.outbound_queue
         WHERE id = :id AND provider = 'zernio' AND action_type = 'post'
           AND payload->>'owner_id' = :owner
           AND payload->>'origin' IN ('telegram_photo','telegram_video','telegram_album')
    """ + (" FOR UPDATE" if lock else ""))
    row = (await session.execute(statement, {"id": queue_id,
                                            "owner": str(owner_id)})).mappings().first()
    return dict(row) if row else None


async def list_scheduled_posts(owner_id: int, *, limit: int = 12, offset: int = 0) -> list[dict]:
    _admin(owner_id)
    async with SessionLocal() as session:
        rows = (await session.execute(text("""
            SELECT id, idempotency_key, status, scheduled_for, payload,
                   created_at, updated_at, provider_job_id
              FROM public.outbound_queue
             WHERE provider = 'zernio' AND action_type = 'post'
               AND status = 'pending' AND scheduled_for > NOW()
               AND payload->>'owner_id' = :owner
               AND payload->>'origin' IN ('telegram_photo','telegram_video','telegram_album')
             ORDER BY scheduled_for ASC, id ASC LIMIT :limit OFFSET :offset
        """), {"owner": str(owner_id), "limit": min(max(limit, 1), 40),
               "offset": max(offset, 0)})).mappings().all()
        return [dict(row) for row in rows]


async def set_queue_schedule(queue_id: int, owner_id: int, when: datetime) -> dict:
    _admin(owner_id)
    when = _valid_target(when)
    async with SessionLocal() as session:
        row = await get_owned_scheduled_post(session, queue_id, owner_id, lock=True)
        if not row or row["status"] != "pending" or row["scheduled_for"] <= _now() + timedelta(seconds=TELEGRAM_SCHEDULE_MIN_LEAD_SECONDS):
            raise ScheduleError("این پست دیگر در صفِ قابل ویرایش نیست؛ شاید Worker آن را برداشته است.")
        await session.execute(text("""
            UPDATE public.outbound_queue
               SET scheduled_for = :when, updated_at = NOW()
             WHERE id = :id AND provider = 'zernio' AND action_type = 'post'
               AND status = 'pending' AND payload->>'owner_id' = :owner
        """), {"when": when, "id": queue_id, "owner": str(owner_id)})
        await session.commit()
        return {"id": queue_id, "scheduled_for": when,
                "jalali": jalali.to_jalali_str(when)}


async def attach_queue_caption_prompt(queue_id: int, owner_id: int,
                                      prompt_message_id: int, kind: str) -> None:
    _admin(owner_id)
    if kind not in ("caption", "visible", "comment"):
        raise ScheduleError("نوع ویرایش کپشن معتبر نیست.")
    async with SessionLocal() as session:
        row = await get_owned_scheduled_post(session, queue_id, owner_id, lock=True)
        if not row or row["status"] != "pending" or row["scheduled_for"] <= _now() + timedelta(seconds=TELEGRAM_SCHEDULE_MIN_LEAD_SECONDS):
            raise ScheduleError("این پست دیگر برای ویرایش کپشن در دسترس نیست.")
        moved = row["payload"].get("caption_moved_to_comment") is True
        if (kind == "caption" and moved) or (kind in ("visible", "comment") and not moved):
            raise ScheduleError("نوع کپشن این پست با دکمهٔ انتخاب‌شده سازگار نیست.")
        await session.execute(text("""
            UPDATE public.outbound_queue
               SET payload = payload || CAST(:patch AS jsonb), updated_at = NOW()
             WHERE id = :id AND provider = 'zernio' AND action_type = 'post'
               AND status = 'pending' AND payload->>'owner_id' = :owner
        """), {"id": queue_id, "owner": str(owner_id),
               "patch": json.dumps({"edit_prompt_id": prompt_message_id,
                                     "edit_prompt_kind": kind})})
        await session.commit()


async def update_queue_caption(queue_id: int, owner_id: int, *, kind: str,
                               value: str, prompt_message_id: int | None = None) -> dict:
    """Edit only an unclaimed Telegram job, then keep its private draft in sync."""
    _admin(owner_id)
    async with SessionLocal() as session:
        row = await get_owned_scheduled_post(session, queue_id, owner_id, lock=True)
        if not row or row["status"] != "pending" or row["scheduled_for"] <= _now() + timedelta(seconds=TELEGRAM_SCHEDULE_MIN_LEAD_SECONDS):
            raise ScheduleError("صف این پست دیگر قابل ویرایش نیست؛ پیش از اقدام وضعیت انتشار را بررسی کنید.")
        payload = dict(row["payload"] or {})
        if prompt_message_id is not None and (
            payload.get("edit_prompt_id") != prompt_message_id
            or payload.get("edit_prompt_kind") != kind
        ):
            raise ScheduleError("این پیام ویرایش قدیمی است؛ از دکمهٔ ویرایش فعلی استفاده کنید.")
        moved = payload.get("caption_moved_to_comment") is True
        value = (value or "").strip()
        if kind == "caption" and not moved:
            if not 0 < len(value) <= 2200:
                raise ScheduleError("کپشن باید بین 1 تا 2200 نویسه باشد.")
            payload["caption"] = value
        elif kind == "visible" and moved:
            if len(value) > TELEGRAM_SHORT_ON_POST_CAPTION_LIMIT:
                raise ScheduleError("کپشن روی پست باید حداکثر 150 نویسه باشد.")
            payload["caption"] = value  # blank is deliberately allowed after a move
        elif kind == "comment" and moved:
            if not 0 < len(value) <= 2200:
                raise ScheduleError("کپشن اصلیِ کامنت باید بین 1 تا 2200 نویسه باشد.")
            options = dict(payload.get("instagram_options") or {})
            options["firstComment"] = value
            payload["instagram_options"] = options
        else:
            raise ScheduleError("نوع ویرایش با وضعیت این کپشن سازگار نیست.")
        try:
            items = _media_items(payload.get("media_urls"), payload.get("media_type"))
            options = validate_instagram_options(
                payload.get("instagram_options"), media_type=payload.get("media_type"),
                media_kinds=[item["type"] for item in items],
            )
            validate_instagram_caption(payload["caption"], media_type=payload.get("media_type"),
                                       options=options, caption_moved_to_comment=moved)
        except (InstagramOptionError, ZernioPublishError) as exc:
            raise ScheduleError(str(exc)) from exc
        payload.pop("edit_prompt_id", None)
        payload.pop("edit_prompt_kind", None)
        await session.execute(text("""
            UPDATE public.outbound_queue
               SET payload = CAST(:payload AS jsonb), updated_at = NOW()
             WHERE id = :id AND provider = 'zernio' AND action_type = 'post'
               AND status = 'pending' AND payload->>'owner_id' = :owner
        """), {"id": queue_id, "owner": str(owner_id),
               "payload": json.dumps(payload, ensure_ascii=False)})
        draft = (await session.execute(select(TelegramPostDraft).where(
            TelegramPostDraft.queue_id == queue_id,
            TelegramPostDraft.owner_id == owner_id,
            TelegramPostDraft.status == "queued",
        ).with_for_update())).scalar_one_or_none()
        if draft:
            if kind == "comment":
                draft.caption = value
                draft.options = {**(draft.options or {}), "firstComment": value}
            elif kind == "visible":
                draft.visible_caption = value
            else:
                draft.caption = value
            draft.updated_at = _now()
        await session.commit()
        return {"id": queue_id, "payload": payload,
                "jalali": jalali.to_jalali_str(row["scheduled_for"])}
