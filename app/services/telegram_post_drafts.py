"""Durable, admin-private Telegram media drafts; no provider POST in this module.

Album fragments arrive in separate webhook invocations. Row locks and a unique
source key provide cross-instance dedupe; only an explicit inline confirmation
can create ONE ordinary Zernio post in public.outbound_queue.
"""

from __future__ import annotations

import hashlib
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import and_, or_, select, text

from app.core.config import get_settings
from app.core.database import SessionLocal
from app.db.repositories import get_active_account
from app.models import InstagramAccount, TelegramMediaIntent, TelegramPostDraft
from app.services.instagram_options import (
    InstagramOptionError,
    instagram_username,
    validate_instagram_options,
)
from app.services.limits import (
    TELEGRAM_ALBUM_MAX_ITEMS,
    TELEGRAM_ALBUM_MIN_ITEMS,
    TELEGRAM_ALBUM_QUIET_SECONDS,
    TELEGRAM_ALBUM_DRAFTS_PER_TICK,
    TELEGRAM_DRAFT_EXPIRY_DAYS,
    TELEGRAM_MEDIA_INTENT_EXPIRY_DAYS,
    TELEGRAM_POST_COLLABORATOR_LIMIT,
    TELEGRAM_POST_USER_TAG_LIMIT,
    TELEGRAM_SHORT_ON_POST_CAPTION_LIMIT,
)
from app.services.outbound_queue import (
    OutboundQueueError,
    enqueue_zernio_post,
    get_owned_post_by_key,
    telegram_post_key,
)
from app.services.zernio_publishing import normalize_username

logger = logging.getLogger("gramma.telegram_post_drafts")
settings = get_settings()


class TelegramDraftError(ValueError):
    """Safe, user-facing draft error; never includes upstream response bodies."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def album_key(chat_id: int, media_group_id: str) -> str:
    if not isinstance(media_group_id, str) or not 0 < len(media_group_id) <= 255:
        raise TelegramDraftError("شناسهٔ آلبوم تلگرام معتبر نیست.")
    # The original ID remains in the private draft; the bounded hash is a
    # stable key in the publishing queue, not a provider retry token.
    digest = hashlib.sha256(media_group_id.encode("utf-8")).hexdigest()
    return f"tg:album:{chat_id}:{digest}"


def source_key(chat_id: int, message_id: int, kind: str, media_group_id: str | None) -> str:
    if media_group_id:
        return album_key(chat_id, media_group_id)
    if kind == "image":
        return telegram_post_key(chat_id, message_id)  # preserve Phase 1 dedupe
    return f"tg:video:{chat_id}:{message_id}"


def ordered_media(draft: TelegramPostDraft) -> list[dict]:
    return sorted(draft.media or [], key=lambda value: value["message_id"])


def draft_media_type(draft: TelegramPostDraft) -> str:
    media = ordered_media(draft)
    if draft.media_intent not in ("auto", "post", "reel"):
        raise TelegramDraftError("حالت انتخاب رسانه معتبر نیست.")
    if draft.media_group_id is not None:
        if draft.media_intent == "reel":
            raise TelegramDraftError("ریلز فقط یک ویدیوی تکی می‌پذیرد.")
        if not TELEGRAM_ALBUM_MIN_ITEMS <= len(media) <= TELEGRAM_ALBUM_MAX_ITEMS:
            raise TelegramDraftError("آلبوم باید بین 2 تا 10 اسلاید داشته باشد.")
        return "carousel"
    if len(media) != 1 or media[0].get("kind") not in ("image", "video"):
        raise TelegramDraftError("رسانهٔ تکی باید دقیقاً یک عکس یا ویدیو داشته باشد.")
    if draft.media_intent == "reel" and media[0]["kind"] != "video":
        raise TelegramDraftError("ریلز فقط یک ویدیوی تکی می‌پذیرد.")
    return "reel" if media[0]["kind"] == "video" else "post"


def _media_kinds(draft: TelegramPostDraft) -> list[str]:
    return [part["kind"] for part in ordered_media(draft)]


def _owned(draft: TelegramPostDraft | None, owner_id: int, chat_id: int) -> TelegramPostDraft:
    if (draft is None or draft.owner_id != owner_id or draft.chat_id != chat_id
            or owner_id not in settings.admin_ids or owner_id != chat_id):
        raise TelegramDraftError("این پیش‌نویس متعلق به چت خصوصی مدیر نیست.")
    return draft


def _live(draft: TelegramPostDraft) -> None:
    if _aware(draft.expires_at) <= _now():
        raise TelegramDraftError("مهلت این پیش‌نویس تمام شده است؛ رسانه را دوباره بفرستید.")


async def get_owned_draft(draft_id: int, owner_id: int, chat_id: int) -> TelegramPostDraft:
    async with SessionLocal() as session:
        draft = await session.get(TelegramPostDraft, draft_id)
        return _owned(draft, owner_id, chat_id)


async def set_pending_media_intent(owner_id: int, chat_id: int, mode: str) -> str:
    """Store a private Post/Reel selection across webhook invocations."""
    if owner_id != chat_id or owner_id not in settings.admin_ids or mode not in ("post", "reel"):
        raise TelegramDraftError("انتخاب رسانه فقط برای مدیر در چت خصوصی مجاز است.")
    async with SessionLocal() as session:
        account = await get_active_account(session, owner_id)
        if (account is None or account.owner_id != owner_id or account.status != "connected"
                or not normalize_username(account.username)):
            raise TelegramDraftError("پیج فعال و متصلِ متعلق به مدیر پیدا نشد.")
        await session.execute(text("""
            INSERT INTO public.telegram_media_intents
                (chat_id, owner_id, mode, selected_at, expires_at)
            VALUES (:chat, :owner, :mode, NOW(), :expiry)
            ON CONFLICT (chat_id) DO UPDATE
                SET mode = EXCLUDED.mode, owner_id = EXCLUDED.owner_id,
                    selected_at = EXCLUDED.selected_at,
                    expires_at = EXCLUDED.expires_at
        """), {"chat": chat_id, "owner": owner_id, "mode": mode,
               "expiry": _now() + timedelta(days=TELEGRAM_MEDIA_INTENT_EXPIRY_DAYS)})
        await session.commit()
        return normalize_username(account.username)


async def clear_pending_media_intent(owner_id: int, chat_id: int) -> None:
    """Only an explicit Cancel button may remove a private waiting choice."""
    if owner_id != chat_id or owner_id not in settings.admin_ids:
        return
    async with SessionLocal() as session:
        await session.execute(text("""
            DELETE FROM public.telegram_media_intents
             WHERE chat_id = :chat AND owner_id = :owner
        """), {"chat": chat_id, "owner": owner_id})
        await session.commit()


async def get_pending_media_intent(owner_id: int, chat_id: int) -> str | None:
    """Read-only mode lookup for a helpful rejection of unsupported files."""
    if owner_id != chat_id or owner_id not in settings.admin_ids:
        return None
    async with SessionLocal() as session:
        row = await session.get(TelegramMediaIntent, chat_id)
        if (row is not None and row.owner_id == owner_id
                and _aware(row.expires_at) > _now() and row.mode in ("post", "reel")):
            return row.mode
    return None


async def _cancel_pending_album(session, draft: TelegramPostDraft) -> bool:
    """Only unclaimed post jobs may be cancelled when a late fragment arrives."""
    if draft.media_group_id is None or not draft.queue_id:
        return False
    cancelled = (await session.execute(text("""
        UPDATE public.outbound_queue
           SET status = 'cancelled', updated_at = NOW(),
               last_error = 'Telegram album fragment arrived after queue confirmation'
         WHERE id = :queue_id AND idempotency_key = :key
           AND provider = 'zernio' AND action_type = 'post'
           AND status = 'pending' AND payload->>'origin' = 'telegram_album'
           AND payload->>'owner_id' = :owner
        RETURNING id
    """), {"queue_id": draft.queue_id, "key": draft.source_key,
           "owner": str(draft.owner_id)})).scalar_one_or_none()
    return cancelled is not None


async def reserve_telegram_media(
    *, owner_id: int, chat_id: int, message_id: int,
    media_group_id: str | None, media_info: dict, caption: str,
) -> dict:
    """Reserve ONE Telegram message before downloading/uploading it.

    The placeholder also makes repeated webhook updates harmless. No media URL
    appears in a publishable queue job until an owner presses the inline button.
    """
    if owner_id not in settings.admin_ids or chat_id != owner_id:
        raise TelegramDraftError("فقط مدیر در چت خصوصی می‌تواند پست ثبت کند.")
    key = source_key(chat_id, message_id, media_info["kind"], media_group_id)
    async with SessionLocal() as session:
        if media_group_id is None:
            previous = await get_owned_post_by_key(session, key, owner_id)
            if previous:
                return {"action": "already_queued", "queue_id": previous["id"]}
        existing = (await session.execute(select(TelegramPostDraft.id).where(
            TelegramPostDraft.source_key == key
        ))).scalar_one_or_none()
        created = False
        if existing is None:
            # Lock the durable waiting choice before creating a draft. The
            # first accepted fragment consumes it in the same transaction;
            # subsequent album fragments use the stored draft intent, not a
            # newer button selection meant for a different upload.
            intent = (await session.execute(select(TelegramMediaIntent).where(
                TelegramMediaIntent.chat_id == chat_id,
                TelegramMediaIntent.owner_id == owner_id,
            ).with_for_update())).scalar_one_or_none()
            mode = (intent.mode if intent and _aware(intent.expires_at) > _now() else "auto")
            if media_info["kind"] not in ("image", "video"):
                raise TelegramDraftError("برای پیش‌نویس فقط عکس یا ویدیو معتبر است.")
            if mode == "reel" and media_group_id is not None:
                raise TelegramDraftError("ریلز فقط یک ویدیوی تکی می‌پذیرد؛ آلبوم را در حالت پست بفرستید.")
            if mode == "reel" and media_info["kind"] != "video":
                raise TelegramDraftError("ریلز فقط ویدیوی عمودی تکی می‌پذیرد؛ لطفاً ویدیو بفرستید.")
            account = await get_active_account(session, owner_id)
            if account is None or account.owner_id != owner_id or account.status != "connected":
                raise TelegramDraftError("پیج فعال و متصلِ متعلق به مدیر پیدا نشد.")
            if not normalize_username(account.username):
                raise TelegramDraftError("نام کاربری پیج متصل معتبر نیست.")
            created = (await session.execute(text("""
                INSERT INTO public.telegram_post_drafts
                    (source_key, owner_id, chat_id, local_account_id,
                     expected_username, media_group_id, media_intent, expires_at)
                VALUES (:key, :owner, :chat, :account, :username, :group_id, :mode, :expiry)
                ON CONFLICT (source_key) DO NOTHING RETURNING id
            """), {
                "key": key, "owner": owner_id, "chat": chat_id, "account": account.id,
                "username": normalize_username(account.username), "group_id": media_group_id,
                "mode": mode, "expiry": _now() + timedelta(days=TELEGRAM_DRAFT_EXPIRY_DAYS),
            })).scalar_one_or_none() is not None
            if created and intent is not None:
                await session.delete(intent)
        draft = (await session.execute(select(TelegramPostDraft).where(
            TelegramPostDraft.source_key == key
        ).with_for_update())).scalar_one()
        _owned(draft, owner_id, chat_id)
        if draft.media_group_id != media_group_id:
            raise TelegramDraftError("شناسهٔ آلبوم با پیش‌نویس ثبت‌شده یکسان نیست.")
        parts = ordered_media(draft)
        if any(part["message_id"] == message_id for part in parts):
            return {"action": "duplicate", "draft_id": draft.id}
        if draft.status == "queued":
            if media_group_id and await _cancel_pending_album(session, draft):
                draft.status = "invalid"
                draft.error_text = "اسلاید دیررسید؛ درخواستِ هنوز در صف برای جلوگیری از انتشار ناقص لغو شد."
                draft.panel_message_id = None
                draft.prompt_message_id = None
                draft.prompt_kind = None
                draft.notify_attempts = 0
                draft.last_prompt_at = None
                draft.updated_at = _now()
                await session.commit()
                return {"action": "cancelled_late", "draft_id": draft.id, "queue_id": draft.queue_id}
            return {"action": "late_after_claim", "queue_id": draft.queue_id}
        if draft.status in ("invalid", "cancelled", "expired") or _aware(draft.expires_at) <= _now():
            return {"action": "closed", "draft_id": draft.id}
        if len(parts) >= TELEGRAM_ALBUM_MAX_ITEMS:
            draft.status = "invalid"
            draft.error_text = "آلبوم بیش از 10 اسلاید دارد؛ هیچ پستی در صف ثبت نشد."
            draft.panel_message_id = None
            draft.prompt_message_id = None
            draft.updated_at = _now()
            await session.commit()
            return {"action": "too_many", "draft_id": draft.id}
        if media_group_id is None and parts:
            raise TelegramDraftError("برای رسانهٔ تکی فقط یک فایل مجاز است.")
        late = draft.status == "ready"
        if late:
            # The old panel remains visible but every callback checks status
            # and message ID, so late slides cannot publish a partial album.
            # A newly arrived lower message_id shifts slide numbers: discard
            # tags rather than silently tagging the wrong person/media.
            draft.status = "collecting"
            discarded = {"userTags"}
            if draft.caption_moved_to_comment:
                # A new slide can change the final album caption. Do not keep
                # an old comment that no longer matches the original caption.
                discarded.add("firstComment")
                draft.caption_moved_to_comment = False
                draft.visible_caption = None
            draft.options = {k: v for k, v in (draft.options or {}).items() if k not in discarded}
            draft.panel_message_id = None
            draft.prompt_message_id = None
            draft.prompt_kind = None
            draft.prompt_slide = None
            draft.notify_attempts = 0
            draft.last_prompt_at = None
        draft.media = [*parts, {
            "message_id": message_id, "kind": media_info["kind"],
            "ext": media_info["ext"], "mime": media_info["mime"],
            "width": media_info["width"], "height": media_info["height"],
            "duration": media_info["duration"], "file_size": media_info["file_size"],
            "file_id": media_info["file_id"], "caption": (caption or "").strip(),
            "url": None,
        }]
        draft.updated_at = _now()
        draft_id = draft.id
        await session.commit()
        return {"action": "late_upload" if late else "upload", "draft_id": draft_id,
                "created": created, "page": draft.expected_username}


async def invalidate_telegram_album(
    owner_id: int, chat_id: int, media_group_id: str, reason: str,
) -> int | None:
    """Durable tombstone for an unsupported/invalid fragment in an album.

    Even if the bad fragment arrives before every valid one, the remaining
    slides cannot masquerade as a complete album and reach the queue.
    """
    if owner_id != chat_id or owner_id not in settings.admin_ids:
        raise TelegramDraftError("فقط مدیر چت خصوصی مجاز است.")
    key = album_key(chat_id, media_group_id)
    async with SessionLocal() as session:
        await session.execute(text("""
            INSERT INTO public.telegram_post_drafts
                (source_key, owner_id, chat_id, local_account_id,
                 expected_username, media_group_id, status, expires_at)
            VALUES (:key, :owner, :chat, 0, 'invalid', :group_id, 'invalid', :expiry)
            ON CONFLICT (source_key) DO NOTHING
        """), {"key": key, "owner": owner_id, "chat": chat_id,
                "group_id": media_group_id,
                "expiry": _now() + timedelta(days=TELEGRAM_DRAFT_EXPIRY_DAYS)})
        draft = (await session.execute(select(TelegramPostDraft).where(
            TelegramPostDraft.source_key == key
        ).with_for_update())).scalar_one()
        _owned(draft, owner_id, chat_id)
        if draft.status == "queued" and await _cancel_pending_album(session, draft):
            draft.status = "invalid"
        if draft.status != "queued":
            draft.status = "invalid"
            draft.error_text = reason[:300]
            draft.panel_message_id = None
            draft.prompt_message_id = None
            draft.prompt_kind = None
            draft.notify_attempts = 0
            draft.last_prompt_at = None
            draft.updated_at = _now()
        draft_id = draft.id
        await session.commit()
        return draft_id


async def complete_telegram_media(
    draft_id: int, message_id: int, *, url: str | None = None,
    error: str | None = None,
) -> dict:
    """Record the durable URL, or permanently disable an incomplete draft."""
    async with SessionLocal() as session:
        draft = (await session.execute(select(TelegramPostDraft).where(
            TelegramPostDraft.id == draft_id
        ).with_for_update())).scalar_one_or_none()
        if draft is None:
            raise TelegramDraftError("پیش‌نویس پیدا نشد؛ هیچ پستی در صف ثبت نشد.")
        parts = ordered_media(draft)
        found = False
        updated = []
        for part in parts:
            item = dict(part)
            if item["message_id"] == message_id:
                found = True
                if error:
                    item["error"] = error[:300]
                elif url:
                    item["url"] = url
                    item.pop("file_id", None)  # reduce retention of Telegram identifiers
            updated.append(item)
        if not found:
            raise TelegramDraftError("رسانهٔ این پیش‌نویس پیدا نشد.")
        draft.media = updated
        draft.updated_at = _now()
        if error:
            draft.status = "invalid"
            draft.error_text = error[:300]
            draft.panel_message_id = None
            draft.prompt_message_id = None
            draft.prompt_kind = None
            draft.notify_attempts = 0
            draft.last_prompt_at = None
        elif draft.media_group_id is None and draft.status == "collecting":
            caption = (updated[0].get("caption") or "").strip()
            draft.caption = caption if len(caption) <= 2200 else ""
            draft.status = "ready"
            draft.notify_attempts = 0
            draft.last_prompt_at = None
        status = draft.status
        await session.commit()
        return {"id": draft_id, "status": status, "caption": draft.caption}


async def prepare_one_due_album() -> str | None:
    """Finalize one quiet group in SQL; only the minute job calls this."""
    async with SessionLocal() as session:
        candidates = (await session.execute(select(TelegramPostDraft).where(
            TelegramPostDraft.status == "collecting",
            TelegramPostDraft.media_group_id.is_not(None),
            TelegramPostDraft.updated_at < _now() - timedelta(seconds=TELEGRAM_ALBUM_QUIET_SECONDS),
            TelegramPostDraft.expires_at > _now(),
        ).order_by(TelegramPostDraft.updated_at, TelegramPostDraft.id)
         .limit(TELEGRAM_ALBUM_DRAFTS_PER_TICK + 10)
         .with_for_update(skip_locked=True))).scalars().all()
        if not candidates:
            return None
        for draft in candidates:
            parts = ordered_media(draft)
            age = _now() - _aware(draft.updated_at)
            if len(parts) < TELEGRAM_ALBUM_MIN_ITEMS or any(not item.get("url") for item in parts):
                if age < timedelta(minutes=2):
                    # Wait for missing updates without blocking other complete albums.
                    continue
                draft.status = "invalid"
                draft.error_text = "آلبوم ناقص است یا یکی از رسانه‌ها ذخیره نشد؛ هیچ پستی در صف ثبت نشد."
                draft.updated_at = _now()
                await session.commit()
                return "invalid"
            if len(parts) > TELEGRAM_ALBUM_MAX_ITEMS:
                draft.status = "invalid"
                draft.error_text = "تعداد اسلایدهای آلبوم مجاز نیست."
                draft.updated_at = _now()
                await session.commit()
                return "invalid"
            captions = {(item.get("caption") or "").strip() for item in parts
                        if (item.get("caption") or "").strip()}
            if not draft.caption_from_reply:
                caption = next(iter(captions)) if len(captions) == 1 else ""
                draft.caption = caption if len(caption) <= 2200 else ""
                if len(captions) > 1:
                    draft.error_text = "آلبوم چند کپشن متفاوت دارد؛ لطفاً کپشن نهایی را با ریپلای بنویسید."
                elif captions and not draft.caption:
                    draft.error_text = "کپشن آلبوم طولانی است؛ لطفاً کپشن کوتاه‌تری ریپلای کنید."
            draft.status = "ready"
            draft.updated_at = _now()
            draft.notify_attempts = 0
            draft.last_prompt_at = None
            await session.commit()
            return "ready"
        return "waiting"


async def claim_one_draft_notice(draft_id: int | None = None) -> dict | None:
    """Claim a pending inline panel or ForceReply without touching bot FSM."""
    async with SessionLocal() as session:
        conditions = [
            TelegramPostDraft.status.in_(("ready", "invalid")),
            TelegramPostDraft.panel_message_id.is_(None),
            TelegramPostDraft.expires_at > _now(),
            TelegramPostDraft.notify_attempts < 5,
            or_(TelegramPostDraft.last_prompt_at.is_(None),
                TelegramPostDraft.last_prompt_at < _now() - timedelta(minutes=2)),
            or_(TelegramPostDraft.status == "invalid",
                and_(TelegramPostDraft.status == "ready",
                     or_(TelegramPostDraft.caption != "", TelegramPostDraft.prompt_message_id.is_(None)))),
        ]
        if draft_id is not None:
            conditions.append(TelegramPostDraft.id == draft_id)
        draft = (await session.execute(select(TelegramPostDraft).where(*conditions)
                 .order_by(TelegramPostDraft.updated_at, TelegramPostDraft.id)
                 .limit(1).with_for_update(skip_locked=True))).scalar_one_or_none()
        if not draft:
            return None
        kind = "error" if draft.status == "invalid" else ("caption" if not draft.caption else "panel")
        draft.notify_attempts += 1
        draft.last_prompt_at = _now()
        snapshot = {
            "id": draft.id, "chat_id": draft.chat_id, "owner_id": draft.owner_id,
            "kind": kind, "error_text": draft.error_text,
            "attempts": draft.notify_attempts,
        }
        await session.commit()
        return snapshot


async def record_draft_notice(draft_id: int, kind: str, message_id: int) -> bool:
    async with SessionLocal() as session:
        draft = (await session.execute(select(TelegramPostDraft).where(
            TelegramPostDraft.id == draft_id
        ).with_for_update())).scalar_one_or_none()
        if draft is None or _aware(draft.expires_at) <= _now():
            return False
        if kind == "error" and draft.status == "invalid":
            draft.panel_message_id = message_id
        elif kind == "caption" and draft.status == "ready" and not draft.caption:
            draft.prompt_message_id = message_id
            draft.prompt_kind = "caption"
        elif kind == "panel" and draft.status == "ready" and draft.caption:
            draft.panel_message_id = message_id
        else:
            return False
        await session.commit()
        return True


async def attach_prompt(
    draft_id: int, owner_id: int, chat_id: int, panel_message_id: int,
    *, kind: str, slide: int | None, prompt_message_id: int,
) -> bool:
    if kind not in ("caption", "location", "tag", "collaborators", "short_caption"):
        return False
    async with SessionLocal() as session:
        draft = (await session.execute(select(TelegramPostDraft).where(
            TelegramPostDraft.id == draft_id
        ).with_for_update())).scalar_one_or_none()
        _owned(draft, owner_id, chat_id)
        if draft.status != "ready" or draft.panel_message_id != panel_message_id:
            return False
        _live(draft)
        if kind == "short_caption" and not draft.caption_moved_to_comment:
            return False
        draft.prompt_message_id = prompt_message_id
        draft.prompt_kind = kind
        draft.prompt_slide = slide if kind == "tag" else None
        await session.commit()
        return True


async def apply_draft_reply(
    draft_id: int, owner_id: int, chat_id: int, prompt_message_id: int, value: str,
) -> TelegramPostDraft:
    """Consume only a reply to THIS bot prompt; no FSM reads or writes."""
    async with SessionLocal() as session:
        draft = (await session.execute(select(TelegramPostDraft).where(
            TelegramPostDraft.id == draft_id
        ).with_for_update())).scalar_one_or_none()
        _owned(draft, owner_id, chat_id)
        _live(draft)
        if draft.status != "ready" or draft.prompt_message_id != prompt_message_id:
            raise TelegramDraftError("این پیام راهنما قدیمی است؛ از دکمه‌های پیش‌نویس تازه استفاده کنید.")
        kind = draft.prompt_kind
        raw = (value or "").strip()
        if not raw:
            raise TelegramDraftError("متن نمی‌تواند خالی باشد؛ به همین پیام ریپلای کنید.")
        options = dict(draft.options or {})
        media_type = draft_media_type(draft)
        kinds = _media_kinds(draft)
        if kind == "caption":
            if len(raw) > 2200:
                raise TelegramDraftError("کپشن باید حداکثر 2200 نویسه باشد.")
            draft.caption = raw
            if draft.caption_moved_to_comment:
                # Editing the original must also edit the future first comment.
                options["firstComment"] = raw
            draft.caption_from_reply = True
            draft.error_text = None
            draft.notify_attempts = 0
            draft.last_prompt_at = None
        elif kind == "short_caption":
            if not draft.caption_moved_to_comment or media_type not in ("post", "carousel"):
                raise TelegramDraftError("کپشن کوتاه فقط پس از انتقال کپشن اصلی به اولین کامنت مجاز است.")
            if len(raw) > TELEGRAM_SHORT_ON_POST_CAPTION_LIMIT:
                raise TelegramDraftError(f"کپشن کوتاه روی پست باید حداکثر {TELEGRAM_SHORT_ON_POST_CAPTION_LIMIT} نویسه باشد.")
            draft.visible_caption = raw
        elif kind == "location":
            options["locationId"] = raw
        elif kind == "collaborators":
            names = [part for part in re.split(r"[,\s]+", raw) if part]
            if not 1 <= len(names) <= TELEGRAM_POST_COLLABORATOR_LIMIT:
                raise TelegramDraftError("بین 1 تا 3 نام کاربری همکار وارد کنید.")
            options["collaborators"] = [instagram_username(name) for name in names]
        elif kind == "tag":
            name = instagram_username(raw)
            index = draft.prompt_slide
            if type(index) is not int or not 0 <= index < len(kinds):
                raise TelegramDraftError("شمارهٔ اسلاید تگ مشخص نیست.")
            tag: dict[str, Any] = {"username": name}
            if media_type == "carousel":
                tag["mediaIndex"] = index
            if kinds[index] == "image":
                tag.update({"x": 0.5, "y": 0.5})
            tags = list(options.get("userTags") or [])
            if len(tags) >= TELEGRAM_POST_USER_TAG_LIMIT:
                raise TelegramDraftError("تعداد تگ‌های این پست به سقف 5 رسیده است.")
            options["userTags"] = [*tags, tag]
        else:
            raise TelegramDraftError("نوع تنظیمات این درخواست معتبر نیست.")
        try:
            draft.options = validate_instagram_options(options, media_type=media_type, media_kinds=kinds)
        except InstagramOptionError as exc:
            raise TelegramDraftError(str(exc)) from exc
        draft.prompt_message_id = None
        draft.prompt_kind = None
        draft.prompt_slide = None
        draft.updated_at = _now()
        await session.commit()
        return draft


async def invalidate_stale_panel(
    draft_id: int, owner_id: int, chat_id: int, panel_message_id: int,
) -> bool:
    """If Telegram cannot edit a panel, require a new one before queueing."""
    async with SessionLocal() as session:
        draft = (await session.execute(select(TelegramPostDraft).where(
            TelegramPostDraft.id == draft_id
        ).with_for_update())).scalar_one_or_none()
        _owned(draft, owner_id, chat_id)
        if draft.status != "ready" or draft.panel_message_id != panel_message_id:
            return False
        draft.panel_message_id = None
        draft.notify_attempts = 0
        draft.last_prompt_at = None
        draft.updated_at = _now()
        await session.commit()
        return True


async def change_caption_move(
    draft_id: int, owner_id: int, chat_id: int, panel_message_id: int,
    *, action: str,
) -> TelegramPostDraft:
    """Move the ORIGINAL caption to a comment, restore it, or blank the post.

    The source caption is retained on the private draft. Only feed/carousel can
    use firstComment; a Reel never exposes this control. No queue insert here.
    """
    if action not in ("move", "restore", "empty"):
        raise TelegramDraftError("دکمهٔ انتقال کپشن معتبر نیست.")
    async with SessionLocal() as session:
        draft = (await session.execute(select(TelegramPostDraft).where(
            TelegramPostDraft.id == draft_id
        ).with_for_update())).scalar_one_or_none()
        _owned(draft, owner_id, chat_id)
        _live(draft)
        if draft.status != "ready" or draft.panel_message_id != panel_message_id:
            raise TelegramDraftError("این پنل قدیمی است؛ چیزی تغییر نکرد.")
        media_type = draft_media_type(draft)
        if media_type not in ("post", "carousel"):
            raise TelegramDraftError("انتقال کپشن به اولین کامنت فقط برای پست فید/کاروسل است.")
        options = dict(draft.options or {})
        if action == "move":
            original = draft.caption.strip()
            if not 0 < len(original) <= 2200:
                raise TelegramDraftError("ابتدا کپشن اصلی را ثبت کنید؛ کپشن خالی قابل انتقال نیست.")
            options["firstComment"] = original
            draft.caption_moved_to_comment = True
            draft.visible_caption = ""  # empty is a deliberate, safe setting
        else:
            if not draft.caption_moved_to_comment:
                raise TelegramDraftError("کپشنی به کامنت منتقل نشده است.")
            if action == "restore":
                draft.caption_moved_to_comment = False
                draft.visible_caption = None
                options.pop("firstComment", None)
            else:
                draft.visible_caption = ""
        draft.options = validate_instagram_options(
            options, media_type=media_type, media_kinds=_media_kinds(draft),
        )
        draft.prompt_message_id = None
        draft.prompt_kind = None
        draft.prompt_slide = None
        draft.updated_at = _now()
        await session.commit()
        return draft


async def clear_draft_option(
    draft_id: int, owner_id: int, chat_id: int, panel_message_id: int, key: str,
) -> TelegramPostDraft:
    field = {"loc": "locationId", "tag": "userTags", "collab": "collaborators"}.get(key)
    if field is None:
        raise TelegramDraftError("تنظیمات انتخاب‌شده معتبر نیست.")
    async with SessionLocal() as session:
        draft = (await session.execute(select(TelegramPostDraft).where(
            TelegramPostDraft.id == draft_id
        ).with_for_update())).scalar_one_or_none()
        _owned(draft, owner_id, chat_id)
        _live(draft)
        if draft.status != "ready" or draft.panel_message_id != panel_message_id:
            raise TelegramDraftError("این صفحه قدیمی است؛ پیش‌نویس تازه را باز کنید.")
        options = dict(draft.options or {})
        options.pop(field, None)
        draft.options = validate_instagram_options(
            options, media_type=draft_media_type(draft), media_kinds=_media_kinds(draft),
        )
        draft.updated_at = _now()
        await session.commit()
        return draft


async def cancel_draft(draft_id: int, owner_id: int, chat_id: int, panel_message_id: int) -> None:
    async with SessionLocal() as session:
        draft = (await session.execute(select(TelegramPostDraft).where(
            TelegramPostDraft.id == draft_id
        ).with_for_update())).scalar_one_or_none()
        _owned(draft, owner_id, chat_id)
        if draft.status != "ready" or draft.panel_message_id != panel_message_id:
            raise TelegramDraftError("این صفحه قدیمی است؛ لغو انجام نشد.")
        draft.status = "cancelled"
        draft.panel_message_id = None
        draft.prompt_message_id = None
        draft.updated_at = _now()
        await session.commit()


async def enqueue_confirmed_draft(
    draft_id: int, owner_id: int, chat_id: int, panel_message_id: int,
) -> dict:
    """Only explicit admin click; draft + queue commit atomically once."""
    async with SessionLocal() as session:
        draft = (await session.execute(select(TelegramPostDraft).where(
            TelegramPostDraft.id == draft_id
        ).with_for_update())).scalar_one_or_none()
        _owned(draft, owner_id, chat_id)
        if draft.status == "queued" and draft.queue_id:
            return {"id": draft.queue_id, "created": False}
        _live(draft)
        if draft.status != "ready" or draft.panel_message_id != panel_message_id:
            raise TelegramDraftError("این صفحه قدیمی یا آلبوم هنوز در حال تکمیل است؛ چیزی در صف ثبت نشد.")
        media = ordered_media(draft)
        media_type = draft_media_type(draft)
        if (not 0 < len(draft.caption.strip()) <= 2200
                or any(not isinstance(item.get("url"), str) for item in media)):
            raise TelegramDraftError("تا رسانه‌ها و کپشن اصلی کامل نباشند، ورود به صف ممکن نیست.")
        options = dict(draft.options or {})
        if not draft.caption_moved_to_comment and "firstComment" in options:
            # An old separate-comment button may still be visible in Telegram;
            # it must never leak a retired setting into a new queue request.
            options.pop("firstComment")
            draft.options = options
        caption_for_post = draft.caption
        if draft.caption_moved_to_comment:
            if (media_type not in ("post", "carousel")
                    or options.get("firstComment") != draft.caption.strip()
                    or not isinstance(draft.visible_caption, str)
                    or len(draft.visible_caption) > TELEGRAM_SHORT_ON_POST_CAPTION_LIMIT):
                raise TelegramDraftError("کپشن منتقل‌شده یا متن کوتاهِ روی پست معتبر نیست؛ چیزی در صف ثبت نشد.")
            caption_for_post = draft.visible_caption
        account = await session.get(InstagramAccount, draft.local_account_id)
        if (account is None or account.owner_id != owner_id or
                normalize_username(account.username) != draft.expected_username):
            raise TelegramDraftError("پیج مقصد از زمان ساخت پیش‌نویس تغییر کرده است؛ رسانه‌ها را دوباره برای پیج درست بفرستید.")
        try:
            job = await enqueue_zernio_post(
                session,
                account_id=draft.local_account_id, owner_id=owner_id,
                media_urls=[item["url"] for item in media], caption=caption_for_post,
                media_type=media_type, idempotency_key=draft.source_key,
                caption_moved_to_comment=draft.caption_moved_to_comment,
                origin=("telegram_album" if draft.media_group_id is not None else
                        "telegram_video" if media_type == "reel" else "telegram_photo"),
                telegram_chat_id=chat_id,
                telegram_message_id=media[0]["message_id"],
                instagram_options=options,
                scheduled_for=draft.scheduled_for,
            )
        except OutboundQueueError as exc:
            raise TelegramDraftError(str(exc)) from exc
        draft.queue_id = job["id"]
        draft.status = "queued"
        draft.prompt_message_id = None
        draft.updated_at = _now()
        await session.commit()
        return {"id": job["id"], "created": job["created"]}
