"""Admin-only Telegram photo → durable post queue (never direct publish)."""

from __future__ import annotations

import html
import logging
from datetime import datetime, timezone

from aiogram import F, Router
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Chat, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import select

from app.core.config import get_settings
from app.core.database import SessionLocal
from app.db.repositories import get_active_account
from app.models import InstagramAccount
from app.services.media_storage import save_telegram_photo_to_supabase
from app.services.outbound_queue import (
    OutboundQueueError,
    enqueue_zernio_post,
    get_owned_post,
    get_owned_post_by_key,
    list_owned_posts,
    reconcile_one_stalled_processing_post,
    refresh_one_accepted_post,
    telegram_post_key,
)
from app.services.zernio_publishing import normalize_username
from app.utils import jalali

logger = logging.getLogger("gramma.telegram_outbound")
settings = get_settings()
router = Router(name="outbound_posts")

_SAMPLE_URL = "https://picsum.photos/1080/1080"
_SAMPLE_CAPTION = "تست صف انتشار…"
_SAMPLE_PAGE = "zeynalikids"


def _admin_private(user_id: int | None, chat: Chat | None) -> bool:
    return bool(user_id is not None and user_id in settings.admin_ids and chat and chat.type == "private")


def _keyboard(queue_id: int | None = None) -> InlineKeyboardMarkup:
    buttons = []
    if queue_id is not None:
        buttons.append([InlineKeyboardButton(text="🔄 وضعیت این پست", callback_data=f"oq:refresh:{queue_id}")])
    buttons.append([InlineKeyboardButton(text="📋 وضعیت صف پست‌ها", callback_data="oq:list")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


async def _ack_queue(message: Message, job: dict, *, username: str) -> None:
    if job["created"]:
        text_value = (
            "✅ درخواست شما در صف قرار گرفت\n"
            f"🎯 پیج: @{html.escape(username)}\n"
            f"🆔 شناسه صف: {job['id']}\n"
            "انتشار فقط توسط پردازشگر زمان‌بندی‌شده انجام می‌شود."
        )
    else:
        text_value = (
            "این پیام قبلاً در صف ثبت شده است؛ پست تازه‌ای ساخته نشد.\n"
            f"🆔 شناسه صف: {job['id']}"
        )
    await message.answer(text_value, reply_markup=_keyboard(job["id"]))


@router.message(Command("test_post"))
async def cmd_test_post(message: Message, state: FSMContext) -> None:
    """Explicit user-requested sample: Picsum + exact caption; enqueue only."""
    if not message.from_user or not _admin_private(message.from_user.id, message.chat):
        return
    key = telegram_post_key(message.chat.id, message.message_id, sample=True)
    try:
        async with SessionLocal() as session:
            # Fixed original test page, even when the admin has switched their
            # active page. Never take a destination or media URL from text.
            accounts = (await session.execute(select(InstagramAccount).where(
                InstagramAccount.owner_id == message.from_user.id,
                InstagramAccount.status == "connected",
            ))).scalars().all()
            matches = [a for a in accounts if normalize_username(a.username) == _SAMPLE_PAGE]
            if len(matches) != 1:
                raise OutboundQueueError("پیج تستِ متصل و متعلق به مدیر یافت نشد.")
            job = await enqueue_zernio_post(
                session, account_id=matches[0].id, owner_id=message.from_user.id,
                media_urls=[_SAMPLE_URL], caption=_SAMPLE_CAPTION,
                idempotency_key=key, origin="telegram_test",
                telegram_chat_id=message.chat.id, telegram_message_id=message.message_id,
            )
            await session.commit()
        await state.clear()
        await _ack_queue(message, job, username=matches[0].username)
    except OutboundQueueError as exc:
        await message.answer(f"⚠️ {html.escape(str(exc))}")
    except Exception as exc:  # noqa: BLE001
        logger.warning("test post queue insertion uncertain error=%s", type(exc).__name__)
        await message.answer("⚠️ وضعیت ثبت درخواست نامشخص است؛ پیش از ارسال دوباره، وضعیت صف را بررسی کنید.", reply_markup=_keyboard())


@router.message(F.photo, F.chat.type == "private", StateFilter(None))
async def admin_photo_post(message: Message) -> None:
    """One spontaneous private-chat photo with its own caption = one post."""
    if not message.from_user or message.from_user.id not in settings.admin_ids:
        return
    if message.media_group_id:
        if message.caption:
            await message.answer("برای این مسیر، یک عکس تکی همراه کپشن بفرستید؛ آلبوم به‌طور خودکار منتشر نمی‌شود.")
        return
    caption = (message.caption or "").strip()
    if not caption:
        await message.answer("برای صف انتشار، عکس را همراه کپشنِ غیرخالی در یک پیام بفرستید؛ این عکس منتشر نشد.")
        return
    if len(caption) > 2200:
        await message.answer("کپشن باید حداکثر 2200 نویسه باشد؛ این عکس منتشر نشد.")
        return

    key = telegram_post_key(message.chat.id, message.message_id)
    try:
        # Short-circuit redeliveries BEFORE downloading/uploading a second time.
        async with SessionLocal() as session:
            prior = await get_owned_post_by_key(session, key, message.from_user.id)
            if prior:
                await _ack_queue(message, {"id": prior["id"], "created": False}, username="")
                return
            account = await get_active_account(session, message.from_user.id)
            if account is None or account.status != "connected":
                raise OutboundQueueError("پیج فعال و متصلِ متعلق به مدیر پیدا نشد.")
            target_id, target_username = account.id, account.username
        if not settings.supabase_url or not settings.supabase_service_role_key:
            raise OutboundQueueError("فضای ذخیره‌سازی تنظیم نشده است؛ پستی در صف ثبت نشد.")
        public_url = await save_telegram_photo_to_supabase(message.bot, message)
        async with SessionLocal() as session:
            job = await enqueue_zernio_post(
                session, account_id=target_id, owner_id=message.from_user.id,
                media_urls=[public_url], caption=caption,
                idempotency_key=key, origin="telegram_photo",
                telegram_chat_id=message.chat.id, telegram_message_id=message.message_id,
            )
            await session.commit()
        await _ack_queue(message, job, username=target_username)
    except OutboundQueueError as exc:
        await message.answer(f"⚠️ {html.escape(str(exc))}")
    except Exception as exc:  # noqa: BLE001
        logger.warning("photo queue insertion uncertain error=%s", type(exc).__name__)
        await message.answer("⚠️ ذخیره یا ثبت درخواست قطعی نشد؛ پیش از ارسال دوباره وضعیت صف را بررسی کنید.", reply_markup=_keyboard())


def _when(value: object) -> str:
    if not value:
        return "—"
    try:
        if isinstance(value, str):
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if isinstance(value, datetime):
            return jalali.to_jalali_str(value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value)
    except (ValueError, TypeError, OverflowError):
        pass
    return "—"


def _label(job: dict) -> str:
    payload = job["payload"] or {}
    status = job["status"]
    if status == "pending":
        if job["scheduled_for"] and job["scheduled_for"] > datetime.now(timezone.utc):
            return "در انتظار نوبت / محدودیت ارسال"
        return "در صف، در انتظار پردازش"
    if status == "processing":
        return "در حال پردازش؛ اگر طولانی شد نیازمند بررسی دستی است"
    if status == "sent":
        if payload.get("provider_status") == "published":
            return "انتشار نهایی توسط Zernio تأیید شد"
        return "Zernio پذیرفت؛ تأیید نهایی هنوز ثبت نشده"
    if status == "failed":
        return "ناموفق یا نامشخص؛ فقط بررسی دستی، بدون ارسال دوباره"
    return "لغوشده"


def _detail(job: dict) -> str:
    payload = job["payload"] or {}
    lines = [
        f"🆔 پست صف: {job['id']}",
        f"🎯 پیج: @{html.escape(str(payload.get('expected_username') or '—'))}",
        f"📌 وضعیت: {_label(job)}",
        f"🕒 زمان ثبت در صف: {_when(job['created_at'])}",
    ]
    if job["status"] == "pending" and job["scheduled_for"] > datetime.now(timezone.utc):
        lines.append(f"⏳ زودترین نوبت: {_when(job['scheduled_for'])}")
    if payload.get("accepted_at"):
        lines.append(f"📨 پذیرش توسط Zernio: {_when(payload['accepted_at'])}")
    if payload.get("confirmed_at"):
        lines.append(f"✅ زمان مشاهدهٔ تأیید: {_when(payload['confirmed_at'])}")
    if payload.get("provider_published_at"):
        lines.append(f"📆 زمان انتشار طبق Zernio: {_when(payload['provider_published_at'])}")
    if payload.get("failed_at"):
        lines.append(f"⚠️ زمان ثبت نیاز به بررسی: {_when(payload['failed_at'])}")
    if payload.get("last_checked_at"):
        lines.append(f"🔄 آخرین بررسی وضعیت: {_when(payload['last_checked_at'])}")
    if job.get("provider_job_id"):
        lines.append(f"شناسه Zernio: {html.escape(str(job['provider_job_id']))}")
    if job["status"] == "failed" or job["status"] == "processing":
        lines.append("قبل از هر اقدام تازه، وضعیت واقعی را در Zernio/Instagram بررسی کنید.")
    return "\n".join(lines)


async def _display(callback: CallbackQuery, value: str, markup: InlineKeyboardMarkup) -> None:
    try:
        await callback.message.edit_text(value, reply_markup=markup)
    except Exception:  # noqa: BLE001
        # Telegram rejects editMessageText when text is unchanged.
        await callback.message.answer(value, reply_markup=markup)


@router.callback_query(F.data == "oq:list")
async def cb_queue_list(callback: CallbackQuery) -> None:
    if not callback.from_user or not _admin_private(callback.from_user.id, callback.message.chat if callback.message else None):
        return
    try:
        async with SessionLocal() as session:
            jobs = await list_owned_posts(session, callback.from_user.id)
        if not jobs:
            await _display(callback, "هنوز پستی در صف ثبت نشده است.", _keyboard())
            return
        lines = ["📋 آخرین پست‌های صف (زمان‌ها شمسی، ساعت تهران):"]
        buttons = []
        for job in jobs:
            lines.append(f"{job['id']} · {_label(job)} · {_when(job['created_at'])}")
            buttons.append([InlineKeyboardButton(text=f"جزئیات پست {job['id']}", callback_data=f"oq:detail:{job['id']}")])
        buttons.append([InlineKeyboardButton(text="🔄 بروزرسانی فهرست", callback_data="oq:list")])
        await _display(callback, "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=buttons))
    except Exception as exc:  # noqa: BLE001
        logger.warning("queue list unavailable error=%s", type(exc).__name__)
        await callback.message.answer("وضعیت صف فعلاً در دسترس نیست؛ کمی بعد دوباره تلاش کنید.")


@router.callback_query(F.data.startswith("oq:detail:"))
@router.callback_query(F.data.startswith("oq:refresh:"))
async def cb_queue_detail(callback: CallbackQuery) -> None:
    if not callback.from_user or not _admin_private(callback.from_user.id, callback.message.chat if callback.message else None):
        return
    try:
        action, raw_id = callback.data.split(":")[1:]
        queue_id = int(raw_id)
        if queue_id < 1:
            return
        async with SessionLocal() as session:
            job = await get_owned_post(session, queue_id, callback.from_user.id)
        if job is None:
            await callback.message.answer("این درخواست در صف شما یافت نشد.")
            return
        if action == "refresh" and job["status"] == "sent":
            job = await refresh_one_accepted_post(queue_id, callback.from_user.id) or job
        elif action == "refresh" and job["status"] == "processing":
            job = await reconcile_one_stalled_processing_post(queue_id, callback.from_user.id) or job
        await _display(callback, _detail(job), _keyboard(queue_id))
    except Exception as exc:  # noqa: BLE001
        logger.warning("queue detail unavailable error=%s", type(exc).__name__)
        await callback.message.answer("بروزرسانی وضعیت موقتاً ممکن نیست؛ ثبت یا انتشار تازه‌ای انجام نشد.", reply_markup=_keyboard())
