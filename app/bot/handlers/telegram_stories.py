"""Private Telegram Story FSM: count → slides → explicit «پایان و صف».

This router runs BEFORE direct Post/Reel media. It never calls Zernio and
cannot enqueue except on the End button. State lives in PostgreSQL, not the
serverless instance's in-memory FSM. Telegram albums are separate stories.
"""

from __future__ import annotations

import html
import logging

from aiogram import F, Router
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from app.core.config import get_settings
from app.services.media_storage import save_telegram_post_media_to_supabase
from app.services.telegram_story_sequences import (
    StoryError, active_story, cancel_story, complete_story_slide,
    describe_story_media, finish_story, invalidate_story_album,
    reserve_story_slide, set_story_count, start_story, story_media_context,
    suppress_late_story_album,
)

router = Router(name="telegram_stories")
logger = logging.getLogger("gramma.story_bot")
settings = get_settings()


def _private(user_id: int | None, chat) -> bool:
    return bool(user_id and user_id in settings.admin_ids and chat
                and chat.type == "private" and chat.id == user_id)


def _buttons(sequence_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🏁 پایان و صف", callback_data=f"ts:{sequence_id}:end")],
        [InlineKeyboardButton(text="🗑 لغو استوری", callback_data=f"ts:{sequence_id}:cancel")],
    ])


def _count_buttons(sequence_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🗑 لغو استوری", callback_data=f"ts:{sequence_id}:cancel")],
    ])


def _progress_text(sequence: dict) -> str:
    ready = sequence["ready_count"]
    announced = sequence["declared_count"]
    prefix = f"📸 دنبالهٔ استوری {sequence['id']} · پیج @{html.escape(sequence['page'])}\n"
    if sequence["status"] == "awaiting_count":
        return (prefix + "چند اسلاید استوری می‌خواهید بگذارید؟ عدد را بفرستید.\n"
                "این عدد برآورد اولیه است؛ با «پایان و صف» می‌توانید زودتر تمام کنید "
                "یا اسلایدهای بیشتری تا سقف ایمنی بفرستید.")
    note = (f"اسلاید {ready} دریافت شد. " if ready else "منتظر اسلاید اول هستم. ")
    return (prefix + f"برآورد شما: {announced} · اسلایدهای ذخیره‌شده: {ready}\n"
            + note + "اسلاید بعدی را بفرستید یا اگر تمام است دکمهٔ «پایان و صف» را بزنید.\n"
            "آلبوم تلگرام به ترتیب اسلایدها، به استوری‌های جداگانه تبدیل می‌شود؛ "
            "برای آلبوم پس از آخرین قطعه کمی صبر کنید.\n"
            "تا قبل از «پایان و صف» هیچ اسلایدی وارد صف انتشار نمی‌شود.")


@router.callback_query(F.data == "publish:new:story")
async def story_button(callback: CallbackQuery) -> None:
    if (not callback.from_user or not isinstance(callback.message, Message)
            or not _private(callback.from_user.id, callback.message.chat)):
        await callback.answer("فقط در چت خصوصی مدیر فعال است.")
        return
    await callback.answer()
    try:
        sequence = await start_story(callback.from_user.id, callback.message.chat.id)
        await callback.message.edit_text(
            _progress_text(sequence),
            reply_markup=(_count_buttons(sequence["id"]) if sequence["status"] == "awaiting_count"
                          else _buttons(sequence["id"])),
        )
    except StoryError as exc:
        await callback.message.answer("⚠️ " + html.escape(str(exc)))
    except Exception as exc:  # noqa: BLE001
        logger.warning("story selection uncertain error=%s", type(exc).__name__)
        await callback.message.answer("⚠️ ثبت حالت انتظار استوری قطعی نشد؛ روی دکمهٔ استوری دوباره بزنید.")


@router.message(F.chat.type == "private", F.text)
async def story_count_message(message: Message) -> None:
    if not message.from_user or not _private(message.from_user.id, message.chat):
        raise SkipHandler()
    sequence = await active_story(message.from_user.id, message.chat.id)
    if not sequence or sequence["status"] != "awaiting_count":
        raise SkipHandler()
    raw = (message.text or "").strip()
    if message.reply_to_message:
        raise SkipHandler()  # a caption-edit reply belongs to its own prompt
    # Navigation/help and future bot commands must not cancel this FSM.
    if raw.startswith("/") or raw in ("📋 منوی کامل", "منوی کامل"):
        raise SkipHandler()
    try:
        updated = await set_story_count(message.from_user.id, message.chat.id, raw)
        await message.answer(_progress_text(updated), reply_markup=_buttons(updated["id"]))
    except StoryError as exc:
        await message.answer("⚠️ " + html.escape(str(exc)) + "\nلطفاً فقط عدد اسلایدها را بفرستید.")


@router.message(F.chat.type == "private", F.document | F.video_note | F.animation | F.audio)
async def reject_story_attachment(message: Message) -> None:
    if not message.from_user or not _private(message.from_user.id, message.chat):
        raise SkipHandler()
    if message.media_group_id:
        context = await story_media_context(message.from_user.id, message.chat.id,
                                            message.message_id, message.media_group_id)
        if context in ("invalid_album", "duplicate"):
            await message.answer("این قطعه متعلق به آلبوم استوریِ ردشده است؛ آن را دوباره به صورت یک آلبوم سالم بفرستید.")
            return
        if context == "late_album":
            await suppress_late_story_album(message.from_user.id, message.chat.id,
                                            message.media_group_id)
            await message.answer("⚠️ فایل دیررسِ آلبوم استوری پیدا شد؛ وضعیت صف/Zernio را بررسی کنید.")
            return
    sequence = await active_story(message.from_user.id, message.chat.id)
    if not sequence:
        raise SkipHandler()
    if message.media_group_id and sequence["status"] == "collecting":
        await invalidate_story_album(message.from_user.id, message.chat.id,
                                     message.media_group_id, "گروه حاوی فایل غیرعکس/ویدیو")
        await message.answer("⚠️ این آلبوم فایل پشتیبانی‌نشده دارد؛ دنبالهٔ فعلی برای جلوگیری از صف‌گذاری ناقص بسته شد. با دکمهٔ استوری دوباره شروع کنید.")
    else:
        await message.answer("⚠️ برای استوری عکس یا ویدیوی عمودی MP4/MOV بفرستید. حالت انتظار حفظ شد.")


@router.message(F.chat.type == "private", F.photo | F.video)
async def receive_story_slide(message: Message) -> None:
    if not message.from_user or not _private(message.from_user.id, message.chat):
        raise SkipHandler()
    owner_id, chat_id = message.from_user.id, message.chat.id
    try:
        context = await story_media_context(owner_id, chat_id, message.message_id,
                                            message.media_group_id)
        if context is None:
            raise SkipHandler()  # direct media belongs to the Post/Reel router
        if context == "duplicate":
            return  # repeated webhook update cannot produce a new draft/post
        if context == "invalid_album":
            await message.answer("⚠️ این قطعه متعلق به آلبوم استوری ردشده است و به پست/ریلز تبدیل نمی‌شود؛ آلبوم سالم را دوباره بفرستید.")
            return
        if context == "late_album":
            cancelled = await suppress_late_story_album(owner_id, chat_id, message.media_group_id)
            await message.answer(
                f"⚠️ قطعهٔ دیررسِ آلبوم استوری پیدا شد؛ {cancelled} اسلایدِ هنوز در انتظار لغو شد. "
                "اگر بخشی از دنباله قبلاً پردازش شده، وضعیت Zernio را دستی بررسی کنید؛ ارسال خودکارِ دوباره نداریم."
            )
            return
        if context == "awaiting_count":
            await message.answer("اول تعداد تقریبی اسلایدهای استوری را به صورت عدد بفرستید؛ سپس رسانه‌ها را ارسال کنید.")
            return
        info = describe_story_media(message)
        reserved = await reserve_story_slide(owner_id, chat_id, message.message_id,
                                             message.media_group_id, info["kind"])
        if reserved["action"] == "duplicate":
            return
        try:
            url = await save_telegram_post_media_to_supabase(message.bot, message, info)
        except Exception as exc:  # noqa: BLE001
            logger.warning("story storage failed seq=%s error=%s", reserved["sequence_id"], type(exc).__name__)
            await complete_story_slide(reserved["slide_id"], owner_id, chat_id,
                                       error="ذخیرهٔ این اسلاید ناموفق بود؛ آن را دوباره ارسال کنید.")
            await message.answer("⚠️ ذخیرهٔ این اسلاید کامل نشد؛ به صف نمی‌رود. همان رسانه را با یک پیام تازه بفرستید.")
            return
        sequence = await complete_story_slide(reserved["slide_id"], owner_id, chat_id, url=url)
        if sequence["status"] == "collecting":
            await message.answer(_progress_text(sequence), reply_markup=_buttons(sequence["id"]))
    except SkipHandler:
        raise
    except StoryError as exc:
        if message.media_group_id:
            await invalidate_story_album(owner_id, chat_id, message.media_group_id, str(exc))
        await message.answer("⚠️ " + html.escape(str(exc)) + "\nاین قطعه در صف ثبت نشد؛ حالت دنباله را بررسی کنید.")
    except Exception as exc:  # noqa: BLE001
        logger.warning("story receipt uncertain msg=%s error=%s", message.message_id, type(exc).__name__)
        await message.answer("⚠️ وضعیت دریافت اسلاید قطعی نشد؛ پیش از ارسال دوباره دکمهٔ «پایان و صف» و وضعیت پیام‌ها را بررسی کنید.")


@router.callback_query(F.data.startswith("ts:"))
async def story_actions(callback: CallbackQuery) -> None:
    if (not callback.from_user or not isinstance(callback.message, Message)
            or not _private(callback.from_user.id, callback.message.chat)):
        await callback.answer("دسترسی ندارید.")
        return
    await callback.answer()
    bits = (callback.data or "").split(":")
    if len(bits) != 3 or not bits[1].isdigit() or bits[2] not in ("end", "cancel"):
        await callback.message.answer("این دکمه معتبر نیست.")
        return
    sequence_id, action = int(bits[1]), bits[2]
    try:
        if action == "cancel":
            await cancel_story(sequence_id, callback.from_user.id, callback.message.chat.id)
            await callback.message.edit_text("🗑 حالت استوری با دکمهٔ لغو بسته شد؛ هیچ اسلایدی به صف نرفت.")
            return
        queued = await finish_story(sequence_id, callback.from_user.id, callback.message.chat.id)
        queue_ids = queued["queue_ids"]
        if not queue_ids:
            raise StoryError("صف‌گذاری دنباله تأیید نشد؛ وضعیت را بررسی کنید.")
        await callback.message.edit_text(
            f"✅ {len(queue_ids)} اسلاید به ترتیب پیام‌های تلگرام در صف مستقل استوری ثبت شد.\n"
            f"اسلاید اول: شناسه {queue_ids[0]}، در نوبت بعدی Worker استوری.\n"
            "هر اسلاید بعدی فقط پس از تأیید انتشار قبلی و با فاصلهٔ تصادفی "
            "5 تا 12 دقیقه آزاد می‌شود؛ زمان واقعی ممکن است با تأخیر ارائه‌دهنده تغییر کند.\n"
            "فشردن دوبارهٔ «پایان و صف» درخواست تازه‌ای ایجاد نمی‌کند."
        )
    except StoryError as exc:
        await callback.message.answer("⚠️ " + html.escape(str(exc)))
    except Exception as exc:  # noqa: BLE001
        logger.warning("story action uncertain seq=%s error=%s", sequence_id, type(exc).__name__)
        await callback.message.answer("⚠️ نتیجهٔ این دکمه نامشخص است؛ پیش از تکرار، پیام‌ها و صف را بررسی کنید. هیچ ارسال خودکاری دوباره انجام نمی‌شود.")
