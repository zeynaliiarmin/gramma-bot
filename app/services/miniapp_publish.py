"""Admin-only Mini-App composer: private drafts, signed uploads, final queue.

No function here calls a Zernio publisher or publication Worker. Uploads use
short-lived per-object Supabase signatures, never a service key in the browser.
All final queue writes are in the same transaction as the draft status change.
"""

from __future__ import annotations

import json
import logging
import math
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

import httpx
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError

from app.core.config import get_settings
from app.core.database import SessionLocal
from app.models import InstagramAccount, MiniappPublishDraft, MiniappPublishMedia
from app.services.instagram_options import InstagramOptionError, validate_instagram_options
from app.services.limits import (
    MINIAPP_IMAGE_MAX_BYTES, MINIAPP_POST_MAX_ITEMS, MINIAPP_STORY_MAX_ITEMS,
    MINIAPP_VIDEO_MAX_BYTES, TELEGRAM_SHORT_ON_POST_CAPTION_LIMIT,
)
from app.services.media_storage import owned_supabase_storage_path
from app.services.outbound_queue import OutboundQueueError, enqueue_zernio_post
from app.services.telegram_scheduling import ScheduleError, jalali_choice
from app.services.zernio_publishing import ZernioPublishError, _media_items, normalize_username

settings = get_settings()
logger = logging.getLogger("gramma.miniapp_publish")
_MIMES = {"image/jpeg": ("image", "jpg"), "image/png": ("image", "png"),
          "video/mp4": ("video", "mp4"), "video/quicktime": ("video", "mov")}


class ComposerError(ValueError):
    """A safe, non-secret explanation to show to the administrator."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _admin(owner_id: int) -> None:
    if owner_id not in settings.admin_ids:
        raise ComposerError("ساخت و صف‌گذاری محتوا فقط برای مدیر ربات فعال است.")


def _storage_base() -> tuple[str, str]:
    if not settings.supabase_url or not settings.supabase_service_role_key:
        raise ComposerError("فضای ذخیره‌سازی پروژه هنوز برای مینی‌اپ تنظیم نشده است.")
    bucket = settings.supabase_storage_bucket
    if not bucket or not bucket.replace("-", "").replace("_", "").isalnum():
        raise ComposerError("نام فضای ذخیره‌سازی معتبر نیست.")
    return settings.supabase_url.rstrip("/") + "/storage/v1", bucket


def _media_dict(row: MiniappPublishMedia) -> dict:
    return {"id": row.id, "position": row.position, "kind": row.kind,
            "status": row.status, "url": row.url if row.status == "ready" else None,
            "size_bytes": row.size_bytes, "width": row.width, "height": row.height,
            "duration_seconds": row.duration_seconds}


async def _snapshot(session, draft: MiniappPublishDraft) -> dict:
    media = (await session.execute(select(MiniappPublishMedia).where(
        MiniappPublishMedia.draft_id == draft.id,
    ).order_by(MiniappPublishMedia.position))).scalars().all()
    return {"id": draft.id, "kind": draft.kind, "status": draft.status,
            "owner_id": draft.owner_id, "account_id": draft.local_account_id,
            "username": draft.expected_username, "media": [_media_dict(m) for m in media],
            "queue_ids": draft.queue_ids or [], "settings": draft.settings or {}}


async def _owned(session, draft_id: int, owner_id: int, *, lock: bool = False) -> MiniappPublishDraft:
    _admin(owner_id)
    if type(draft_id) is not int or draft_id < 1:
        raise ComposerError("شناسهٔ پیش‌نویس معتبر نیست.")
    query = select(MiniappPublishDraft).where(
        MiniappPublishDraft.id == draft_id, MiniappPublishDraft.owner_id == owner_id,
    )
    if lock:
        query = query.with_for_update()
    draft = (await session.execute(query)).scalar_one_or_none()
    if draft is None:
        raise ComposerError("این پیش‌نویس متعلق به شما نیست یا پیدا نشد.")
    return draft


def _collecting(draft: MiniappPublishDraft) -> None:
    if draft.status != "collecting" or draft.expires_at <= _now():
        raise ComposerError("این پیش‌نویس دیگر فعال نیست؛ از صفحهٔ ساخت محتوا دوباره شروع کنید.")


def _account(account: InstagramAccount | None, owner_id: int, username: str | None = None) -> None:
    if (account is None or account.owner_id != owner_id or account.status != "connected"
            or not normalize_username(account.username)
            or (username is not None and normalize_username(account.username) != username)):
        raise ComposerError("پیج مقصد متصل یا متعلق به شما نیست؛ چیزی به صف نرفت.")


async def active_draft(owner_id: int, kind: str) -> dict | None:
    _admin(owner_id)
    if kind not in ("post", "story"):
        raise ComposerError("نوع پیش‌نویس نامعتبر است.")
    async with SessionLocal() as session:
        draft = (await session.execute(select(MiniappPublishDraft).where(
            MiniappPublishDraft.owner_id == owner_id, MiniappPublishDraft.kind == kind,
            MiniappPublishDraft.status == "collecting",
        ).order_by(MiniappPublishDraft.id.desc()).limit(1).with_for_update())).scalar_one_or_none()
        if draft is None:
            return None
        if draft.expires_at <= _now():
            draft.status = "expired"
            draft.updated_at = _now()
            await session.commit()
            return None
        return await _snapshot(session, draft)


async def get_draft(owner_id: int, draft_id: int) -> dict:
    async with SessionLocal() as session:
        return await _snapshot(session, await _owned(session, draft_id, owner_id))


async def create_draft(owner_id: int, account_id: int, kind: str) -> dict:
    _admin(owner_id)
    if kind not in ("post", "story") or type(account_id) is not int or account_id < 1:
        raise ComposerError("نوع محتوا یا پیج انتخاب‌شده معتبر نیست.")
    # One active draft per user and kind. A reload cannot lose the upload
    # order; another tab cannot silently redirect it to a different page.
    existing = await active_draft(owner_id, kind)
    if existing is not None:
        return existing
    async with SessionLocal() as session:
        account = await session.get(InstagramAccount, account_id)
        _account(account, owner_id)
        draft = MiniappPublishDraft(owner_id=owner_id, local_account_id=account.id,
                                    expected_username=normalize_username(account.username),
                                    kind=kind, status="collecting", expires_at=_now() + timedelta(days=7))
        session.add(draft)
        try:
            await session.commit()
        except IntegrityError:
            # Unique active-kind index arbitrates concurrent browser tabs.
            await session.rollback()
            existing = await active_draft(owner_id, kind)
            if existing is None:
                raise ComposerError("ساخت پیش‌نویس قطعی نشد؛ صفحه را بازخوانی کنید.")
            return existing
        return await _snapshot(session, draft)


async def save_settings(owner_id: int, draft_id: int, value: dict) -> dict:
    """Durably save UI choices; never enqueue or call a provider."""
    allowed = {"caption", "visible_caption", "caption_moved_to_comment", "collaborators",
               "userTags", "scheduled"}
    if (not isinstance(value, dict) or set(value) - allowed
            or len(json.dumps(value, ensure_ascii=False)) > 12000):
        raise ComposerError("تنظیمات پیش‌نویس بیش از حد بزرگ یا نامعتبر است.")
    if (not isinstance(value.get("caption", ""), str) or len(value.get("caption", "")) > 2200
            or not isinstance(value.get("visible_caption", ""), str)
            or len(value.get("visible_caption", "")) > TELEGRAM_SHORT_ON_POST_CAPTION_LIMIT
            or type(value.get("caption_moved_to_comment", False)) is not bool):
        raise ComposerError("کپشن یا انتقال آن معتبر نیست.")
    for name, maximum in (("collaborators", 3), ("userTags", 5)):
        items = value.get(name, [])
        if not isinstance(items, list) or len(items) > maximum:
            raise ComposerError("تعداد تگ‌ها یا همکارها از سقف مجاز بیشتر است.")
    when = value.get("scheduled")
    if when is not None and (not isinstance(when, dict)
                             or set(when) != {"year", "month", "day", "hour", "minute"}
                             or any(type(v) is not int for v in when.values())):
        raise ComposerError("برای زمان‌بندی فقط انتخاب تقویم شمسی و ساعت تهران مجاز است.")
    async with SessionLocal() as session:
        draft = await _owned(session, draft_id, owner_id, lock=True)
        _collecting(draft)
        if draft.kind != "post" and any(v for v in value.values()):
            raise ComposerError("استوری در مینی‌اپ تنظیمات پست ندارد.")
        draft.settings = value
        draft.updated_at = _now()
        await session.commit()
        return {"saved": True}


async def cancel_draft(owner_id: int, draft_id: int) -> dict:
    async with SessionLocal() as session:
        draft = await _owned(session, draft_id, owner_id, lock=True)
        _collecting(draft)
        draft.status = "cancelled"
        draft.updated_at = _now()
        await session.commit()
        return {"cancelled": True}


def _validate_upload(draft_kind: str, value: dict) -> dict:
    if not isinstance(value, dict) or set(value) - {"position", "mime_type", "size_bytes",
                                                  "width", "height", "duration_seconds"}:
        raise ComposerError("مشخصات رسانه نامعتبر است.")
    position, mime, size = value.get("position"), value.get("mime_type"), value.get("size_bytes")
    dims = (value.get("width"), value.get("height"))
    if (type(position) is not int or position < 0 or position >= (
            MINIAPP_POST_MAX_ITEMS if draft_kind == "post" else MINIAPP_STORY_MAX_ITEMS)):
        raise ComposerError("شمارهٔ اسلاید خارج از سقف مجاز است.")
    if not isinstance(mime, str) or mime not in _MIMES:
        raise ComposerError("فقط عکس JPEG/PNG یا ویدیوی MP4/MOV قابل ارسال است.")
    kind, ext = _MIMES[mime]
    max_bytes = MINIAPP_IMAGE_MAX_BYTES if kind == "image" else MINIAPP_VIDEO_MAX_BYTES
    if type(size) is not int or not 0 < size <= max_bytes:
        raise ComposerError("حجم عکس باید حداکثر 8 MB و ویدیو حداکثر 50 MB باشد.")
    if any(type(v) is not int or not 1 <= v <= 10000 for v in dims):
        raise ComposerError("ابعاد عکس یا ویدیو معتبر نیست.")
    width, height = dims
    ratio = width / height
    if draft_kind == "story" and not 0.52 <= ratio <= 0.61:
        raise ComposerError("اسلاید استوری باید ویدیوی/عکس عمودی نزدیک 9:16 باشد.")
    if draft_kind == "post" and kind == "image" and not 0.55 <= ratio <= 1.93:
        raise ComposerError("نسبت عکس پست باید در محدودهٔ مجاز اینستاگرام باشد.")
    duration = value.get("duration_seconds")
    if kind == "video":
        end = 60 if draft_kind == "story" else 90
        if (type(duration) not in (int, float) or not math.isfinite(duration)
                or not 3 <= duration <= end):
            raise ComposerError(f"مدت ویدیو باید بین 3 و {end} ثانیه باشد.")
        duration = float(duration)
    elif duration is not None:
        raise ComposerError("عکس مدت ویدیو ندارد.")
    return {"position": position, "mime_type": mime, "kind": kind, "ext": ext,
            "size_bytes": size, "width": width, "height": height,
            "duration_seconds": duration}


async def _signed_upload(path: str) -> dict:
    base, bucket = _storage_base()
    headers = {"Authorization": f"Bearer {settings.supabase_service_role_key}",
               "apikey": settings.supabase_service_role_key}
    async with httpx.AsyncClient(timeout=12.0, follow_redirects=False) as client:
        response = await client.post(f"{base}/object/upload/sign/{bucket}/{path}",
                                     headers=headers, json={})
    if response.status_code not in (200, 201):
        logger.warning("Storage signing deferred status=%d", response.status_code)
        raise ComposerError("امضای آپلود صادر نشد؛ دوباره تلاش کنید. چیزی به صف انتشار نرفت.")
    data = response.json()
    token = data.get("token")
    if not isinstance(token, str) or not token or len(token) > 4096:
        raise ComposerError("توکن موقت آپلود معتبر نیست؛ دوباره تلاش کنید.")
    return {"upload_url": f"{base}/object/upload/sign/{bucket}/{path}?token={quote(token, safe='')}",
            "upload_token": token, "tus_endpoint": f"{base}/upload/resumable/sign",
            "bucket": bucket, "storage_path": path}


async def prepare_upload(owner_id: int, draft_id: int, value: dict) -> dict:
    _storage_base()
    async with SessionLocal() as session:
        draft = await _owned(session, draft_id, owner_id, lock=True)
        _collecting(draft)
        details = _validate_upload(draft.kind, value)
        media = (await session.execute(select(MiniappPublishMedia).where(
            MiniappPublishMedia.draft_id == draft.id,
        ).order_by(MiniappPublishMedia.position))).scalars().all()
        position = details["position"]
        if position > len(media):
            raise ComposerError("اسلایدها باید به ترتیب انتخاب و ذخیره شوند.")
        if position < len(media):
            row = media[position]
            if (row.position != position or any(getattr(row, k) != v for k, v in details.items()
                                                 if k != "position")):
                raise ComposerError("این اسلاید قبلاً برای فایل دیگری رزرو شده است؛ پیش‌نویس تازه بسازید.")
        else:
            base, bucket = _storage_base()
            path = f"gramma/{uuid.uuid4().hex}.{details['ext']}"
            url = f"{base}/object/public/{bucket}/{path}"
            row = MiniappPublishMedia(draft_id=draft.id, **details,
                                      storage_path=path, url=url, status="uploading")
            session.add(row)
            await session.flush()
        snapshot = _media_dict(row)
        await session.commit()  # reservation durable before token leaves server
    if snapshot["status"] == "ready":
        return {"media": snapshot, "ready": True}
    signed = await _signed_upload(row.storage_path)
    return {"media": snapshot, "ready": False, **signed}


async def _verify_storage(row: MiniappPublishMedia) -> None:
    if owned_supabase_storage_path(row.url) != row.storage_path:
        raise ComposerError("رسانه متعلق به Storage این پروژه نیست.")
    # Only an authenticated admin can prepare a signed URL. Storage metadata
    # is read through a HEAD (no file downloaded through Vercel) and compared
    # to the original reservation, so a token alone cannot authorize a job.
    base, bucket = _storage_base()
    headers = {"Authorization": f"Bearer {settings.supabase_service_role_key}",
               "apikey": settings.supabase_service_role_key}
    async with httpx.AsyncClient(timeout=12.0, follow_redirects=False) as client:
        response = await client.head(row.url)
        if response.status_code != 200:
            raise ComposerError("فایل هنوز در فضای ذخیره‌سازی تأیید نشده است؛ کمی بعد دوباره تلاش کنید.")
        try:
            size = int(response.headers.get("content-length", ""))
        except ValueError:
            size = -1
        mime = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        if size == row.size_bytes and mime == row.mime_type:
            return
        # Some Storage CDNs omit the HEAD length; the object-info API is a
        # read-only authoritative fallback. Never read the file body here.
        info = await client.get(f"{base}/object/info/public/{bucket}/{row.storage_path}",
                                headers=headers)
    if info.status_code == 200:
        try:
            data = info.json()
            metadata = (data.get("metadata") or {}) if isinstance(data, dict) else {}
            if not isinstance(metadata, dict):
                metadata = {}
        except ValueError:
            metadata = {}
        try:
            size = int(metadata.get("size", -1))
        except (ValueError, TypeError):
            size = -1
        mime = str(metadata.get("mimetype", "")).split(";", 1)[0].strip().lower()
        if size == row.size_bytes and mime == row.mime_type:
            return
    raise ComposerError("حجم یا نوع فایل آپلودشده با فایل انتخابی یکسان نیست؛ به صف نمی‌رود.")


async def complete_upload(owner_id: int, draft_id: int, media_id: int) -> dict:
    async with SessionLocal() as session:
        draft = await _owned(session, draft_id, owner_id)
        _collecting(draft)
        row = (await session.execute(select(MiniappPublishMedia).where(
            MiniappPublishMedia.id == media_id, MiniappPublishMedia.draft_id == draft_id,
        ))).scalar_one_or_none()
        if row is None:
            raise ComposerError("اسلاید برای این پیش‌نویس پیدا نشد.")
        if row.status == "ready":
            return _media_dict(row)
    await _verify_storage(row)  # DB session/connection released during HTTP
    async with SessionLocal() as session:
        draft = await _owned(session, draft_id, owner_id, lock=True)
        _collecting(draft)
        row = (await session.execute(select(MiniappPublishMedia).where(
            MiniappPublishMedia.id == media_id, MiniappPublishMedia.draft_id == draft_id,
        ).with_for_update())).scalar_one_or_none()
        if row is None:
            raise ComposerError("اسلاید دیگر متعلق به پیش‌نویس نیست.")
        row.status = "ready"
        row.updated_at = _now()
        draft.updated_at = _now()
        result = _media_dict(row)
        await session.commit()
        return result


def _post_options(value: dict, media_type: str, media: list[MiniappPublishMedia]) -> tuple[str, dict, bool]:
    original = value.get("caption")
    moved = value.get("caption_moved_to_comment", False)
    short = value.get("visible_caption", "")
    if not isinstance(original, str) or not 0 < len(original.strip()) <= 2200:
        raise ComposerError("کپشن اصلی باید بین 1 تا 2200 نویسه باشد.")
    if type(moved) is not bool or not isinstance(short, str):
        raise ComposerError("تنظیمات کپشن معتبر نیست.")
    options: dict = {}
    if value.get("collaborators"):
        options["collaborators"] = value["collaborators"]
    if value.get("userTags"):
        options["userTags"] = value["userTags"]
    if moved:
        if media_type not in ("post", "carousel") or len(short) > TELEGRAM_SHORT_ON_POST_CAPTION_LIMIT:
            raise ComposerError("انتقال کپشن به اولین کامنت فقط برای عکس/کاروسل است؛ کپشن روی پست حداکثر 150 نویسه.")
        options["firstComment"] = original.strip()
    elif short.strip():
        raise ComposerError("کپشن کوتاه فقط هنگام انتقال کپشن اصلی به اولین کامنت کاربرد دارد.")
    try:
        clean = validate_instagram_options(options, media_type=media_type,
                                           media_kinds=[m.kind for m in media])
    except InstagramOptionError as exc:
        raise ComposerError(str(exc)) from exc
    return (short.strip() if moved else original.strip()), clean, moved


async def submit_draft(owner_id: int, draft_id: int, value: dict) -> dict:
    """Only final click: atomically enqueue exactly one post OR Story sequence."""
    _admin(owner_id)
    if not isinstance(value, dict) or set(value) - {
        "caption", "visible_caption", "caption_moved_to_comment", "collaborators", "userTags", "scheduled",
    }:
        raise ComposerError("تنظیمات نهایی ناشناخته یا نامعتبر است.")
    async with SessionLocal() as session:
        draft = await _owned(session, draft_id, owner_id, lock=True)
        if draft.status == "queued" and draft.queue_ids:
            return {"queue_ids": draft.queue_ids, "created": False, "kind": draft.kind}
        _collecting(draft)
        media = (await session.execute(select(MiniappPublishMedia).where(
            MiniappPublishMedia.draft_id == draft.id,
        ).order_by(MiniappPublishMedia.position).with_for_update())).scalars().all()
        maximum = MINIAPP_POST_MAX_ITEMS if draft.kind == "post" else MINIAPP_STORY_MAX_ITEMS
        if (not 1 <= len(media) <= maximum or [m.position for m in media] != list(range(len(media)))
                or any(m.status != "ready" or owned_supabase_storage_path(m.url) != m.storage_path
                       for m in media)):
            raise ComposerError("همهٔ اسلایدها باید به‌ترتیب و کامل در Storage ثبت شوند؛ چیزی به صف نرفت.")
        account = await session.get(InstagramAccount, draft.local_account_id)
        _account(account, owner_id, draft.expected_username)
        if not settings.zernio_api_key:
            raise ComposerError("اتصال Zernio تنظیم نشده است؛ چیزی به صف نرفت.")
        now = _now()
        if draft.kind == "post":
            media_type = ("carousel" if len(media) > 1 else
                          "reel" if media[0].kind == "video" else "post")
            if media_type == "reel" and not 0.52 <= media[0].width / media[0].height <= 0.61:
                raise ComposerError("ویدیوی تکی باید عمودی نزدیک 9:16 باشد تا به صورت ریلز ثبت شود.")
            if media_type == "carousel":
                for row in media:
                    if row.kind == "video" and (row.duration_seconds or 0) > 60:
                        raise ComposerError("مدت هر ویدیوی کاروسل باید حداکثر 60 ثانیه باشد.")
            try:
                _media_items([m.url for m in media], media_type)
            except ZernioPublishError as exc:
                raise ComposerError(str(exc)) from exc
            caption, options, moved = _post_options(value, media_type, media)
            when = value.get("scheduled")
            if when is None:
                scheduled = None
            elif isinstance(when, dict) and set(when) == {"year", "month", "day", "hour", "minute"}:
                try:
                    scheduled = jalali_choice(when["year"], when["month"], when["day"],
                                              when["hour"], when["minute"])
                except ScheduleError as exc:
                    raise ComposerError(str(exc)) from exc
            else:
                raise ComposerError("زمان انتخاب‌شده باید از تقویم شمسی و ساعت تهران باشد.")
            try:
                job = await enqueue_zernio_post(
                    session, account_id=draft.local_account_id, owner_id=owner_id,
                    media_urls=[m.url for m in media], caption=caption,
                    idempotency_key=f"ma:post:{owner_id}:{draft.id}", origin="miniapp",
                    media_type=media_type, instagram_options=options,
                    caption_moved_to_comment=moved, scheduled_for=scheduled,
                )
            except OutboundQueueError as exc:
                raise ComposerError(str(exc)) from exc
            ids = [job["id"]]
        else:
            if (value.get("scheduled") is not None or value.get("caption")
                    or value.get("visible_caption") or value.get("caption_moved_to_comment")
                    or value.get("collaborators") or value.get("userTags")):
                raise ComposerError("برای استوری فقط رسانه و دکمهٔ «پایان و صف» فعال است.")
            # Preallocate row IDs in one DB round trip so payload predecessor
            # links are known before a batched INSERT. All rows and the draft
            # state commit together; no Worker can observe half a sequence.
            ids = (await session.execute(text("""
                SELECT nextval('public.outbound_queue_id_seq')
                  FROM generate_series(1, :count)
            """), {"count": len(media)})).scalars().all()
            due = now
            params = []
            for index, row in enumerate(media):
                try:
                    _media_items([row.url], "story")
                except ZernioPublishError as exc:
                    raise ComposerError(str(exc)) from exc
                delay = 0 if index == 0 else 300 + secrets.randbelow(421)
                due += timedelta(seconds=delay)
                payload = {
                    "owner_id": owner_id, "local_account_id": draft.local_account_id,
                    "expected_username": draft.expected_username,
                    "caption": "", "media_urls": [row.url], "media_type": "story",
                    "origin": "miniapp_story", "miniapp_draft_id": draft.id,
                    "story_sequence_id": f"ma-{draft.id}", "story_index": index + 1,
                    "story_total": len(media), "story_previous_queue_id": ids[index-1] if index else None,
                    "story_delay_seconds": delay, "story_released": index == 0,
                    "storage_paths": [row.storage_path],
                }
                params.append({"id": ids[index], "key": f"ma:story:{owner_id}:{draft.id}:{index}",
                               "payload": json.dumps(payload, ensure_ascii=False), "due": due})
            await session.execute(text("""
                INSERT INTO public.outbound_queue
                    (id, idempotency_key, action_type, provider, payload, status,
                     scheduled_for, max_attempts)
                VALUES (:id, :key, 'story', 'zernio', CAST(:payload AS jsonb),
                        'pending', :due, 1)
                ON CONFLICT (idempotency_key) DO NOTHING
            """), params)
            count = (await session.execute(text("""
                SELECT COUNT(*) FROM public.outbound_queue
                 WHERE id = ANY(CAST(:ids AS BIGINT[]))
                   AND provider = 'zernio' AND action_type = 'story'
                   AND payload->>'owner_id' = :owner
                   AND payload->>'miniapp_draft_id' = :draft
            """), {"ids": ids, "owner": str(owner_id), "draft": str(draft.id)})).scalar_one()
            if count != len(ids):
                raise ComposerError("شناسهٔ صف قبلاً مصرف شده است؛ ورود ناقصِ استوری ممنوع است.")
        draft.queue_ids = ids
        draft.status = "queued"
        draft.updated_at = _now()
        await session.commit()
        return {"queue_ids": ids, "created": True, "kind": draft.kind,
                "scheduled": value.get("scheduled") if draft.kind == "post" else None}
