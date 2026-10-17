"""Admin-private Telegram photo/video/album drafting and inline queue consent.

These handlers never set, clear, or depend on the bot's FSM for optional
settings. They do not invoke Zernio or the five-minute publication Worker.
"""

from __future__ import annotations

import html
import logging
import re
from urllib.parse import urlencode

from aiogram import F, Router
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    CallbackQuery, ForceReply, InlineKeyboardButton, InlineKeyboardMarkup, Message, WebAppInfo,
)

from app.bot.states import MediaWait
from app.core.config import get_settings
from app.services.media_storage import (
    TelegramPostMediaError,
    describe_telegram_post_media,
    save_telegram_post_media_to_supabase,
)
from app.services.telegram_post_drafts import (
    TelegramDraftError,
    apply_draft_reply,
    attach_prompt,
    cancel_draft,
    change_caption_move,
    claim_one_draft_notice,
    clear_draft_option,
    clear_pending_media_intent,
    complete_telegram_media,
    draft_media_type,
    enqueue_confirmed_draft,
    get_owned_draft,
    get_pending_media_intent,
    invalidate_stale_panel,
    invalidate_telegram_album,
    ordered_media,
    prepare_one_due_album,
    record_draft_notice,
    reserve_telegram_media,
    set_pending_media_intent,
)
from app.services.limits import TELEGRAM_ALBUM_DRAFTS_PER_TICK
from app.services.telegram_scheduling import ScheduleError, quick_choice, set_draft_schedule
from app.utils import jalali
from app.webapp.auth import make_miniapp_ticket

router = Router(name="telegram_media_drafts")
logger = logging.getLogger("gramma.telegram_drafts")
settings = get_settings()
_PROMPT_ID = re.compile(r"^📝 پیش‌نویس ([0-9]+)\b")


def _admin_private(user_id: int | None, chat) -> bool:
    return bool(user_id is not None and user_id in settings.admin_ids
                and chat and chat.type == "private" and chat.id == user_id)


def _button(text_value: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text_value, callback_data=data)


@router.callback_query(F.data.in_({
    "publish:wait:post", "publish:wait:reel",
    # Old inline buttons may still be visible in Telegram after deployment.
    "publish:new:post", "publish:new:photo", "publish:new:carousel",
    "publish:new:reel", "publish:new:video",
}))
async def wait_for_admin_media(callback: CallbackQuery, state: FSMContext) -> None:
    if (not callback.from_user or not isinstance(callback.message, Message)
            or not _admin_private(callback.from_user.id, callback.message.chat)):
        await callback.answer("این گزینه فقط در چت خصوصی مدیر فعال است.")
        return
    await callback.answer()
    from app.services.telegram_story_sequences import active_story

    if await active_story(callback.from_user.id, callback.message.chat.id):
        await callback.message.answer("⚠️ یک دنبالهٔ استوری در جریان است؛ ابتدا «پایان و صف» یا «لغو استوری» را بزنید. /start آن را لغو نمی‌کند.")
        return
    mode = "post" if (callback.data or "") in {
        "publish:wait:post", "publish:new:post", "publish:new:photo", "publish:new:carousel",
    } else "reel"
    try:
        page = await set_pending_media_intent(
            callback.from_user.id, callback.message.chat.id, mode,
        )
        # The explicit waiting choice is durable; neither this selection nor
        # /start/navigation silently cancels another unfinished draft/FSM.
        prompt = ("لطفاً عکس یا ویدیوی خود را ارسال کنید" if mode == "post"
                  else "لطفاً ویدیوی عمودی خود را ارسال کنید")
        text_value = f"{prompt}\n🎯 پیج: @{html.escape(page)}\nرسانه ابتدا به تنظیمات می‌رود؛ فقط تأیید نهایی آن را به صف می‌افزاید."
        if mode == "post":
            text_value += ("\nویدیوی تکی در مسیر فعلی Zernio به شکل ریلز با نمایش در فید "
                           "ارسال می‌شود؛ نوع دقیق در تنظیمات نشان داده می‌شود.")
        try:
            await callback.message.edit_text(text_value, reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [_button("🗑 لغو", "publish:wait:cancel")],
            ]))
        except Exception:  # noqa: BLE001
            await callback.message.answer(text_value, reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [_button("🗑 لغو", "publish:wait:cancel")],
            ]))
    except TelegramDraftError as exc:
        await callback.message.answer("⚠️ " + html.escape(str(exc)))
    except Exception as exc:  # noqa: BLE001
        logger.warning("media intent selection uncertain error=%s", type(exc).__name__)
        await callback.message.answer("⚠️ ثبت حالت انتظار قطعی نشد؛ دوباره روی دکمهٔ پست یا ریلز بزنید.")


@router.callback_query(F.data == "publish:wait:cancel")
async def cancel_waiting_media(callback: CallbackQuery) -> None:
    if (not callback.from_user or not isinstance(callback.message, Message)
            or not _admin_private(callback.from_user.id, callback.message.chat)):
        await callback.answer("دسترسی ندارید.")
        return
    await callback.answer()
    mode = await get_pending_media_intent(callback.from_user.id, callback.message.chat.id)
    if mode:
        await clear_pending_media_intent(callback.from_user.id, callback.message.chat.id)
    await callback.message.edit_text("🗑 حالت انتظار رسانه با دکمهٔ لغو بسته شد؛ پیش‌نویس‌های دیگر محفوظ‌اند.")


def _panel_text(draft) -> str:
    parts = ordered_media(draft)
    media_type = draft_media_type(draft)
    kind = {"post": "عکس تکی / پست فید", "reel": "ویدیوی تکی / ریلز",
            "carousel": "آلبوم / کاروسل"}[media_type]
    choices = draft.options or {}
    original = draft.caption or ""
    rows = [
        f"🖼 پیش‌نویس {draft.id} — {kind} ({len(parts)} رسانه)",
        f"🎯 پیج: @{html.escape(draft.expected_username)}",
        f"📝 کپشن اصلی: {html.escape(original[:280])}{'…' if len(original) > 280 else ''}",
    ]
    if media_type == "reel":
        rows.append("✅ نمایش ریلز در فید: روشن (shareToFeed)")
        if draft.media_intent == "post":
            rows.append("⚠️ با وجود انتخاب «پست»، ویدیوی تکی در مسیر فعلی Zernio ریلزِ قابل‌نمایش در فید است؛ گزینهٔ مستندی برای ویدیوی فیدِ غیرریلز نداریم.")
    if draft.caption_moved_to_comment:
        visible = draft.visible_caption or ""
        rows.extend((
            "📩 کپشن اصلی به اولین کامنت منتقل شد.",
            "✏️ کپشن روی خود پست: "
            + (html.escape(visible) if visible else "خالی (بدون کپشن)"),
            "⚠️ برای درج اولین کامنت، اتصال Zernio باید مجوز comments داشته باشد؛ "
            "بدون این مجوز ممکن است پست منتشر شود ولی متنِ منتقل‌شده در کامنت نیاید.",
        ))
    rows.extend((
        "",
        "تنظیمات اختیاری:",
        "🤝 دعوت همکار (Collab): " + (
            ", ".join("@" + html.escape(name) for name in choices.get("collaborators", []))
            if choices.get("collaborators") else "—"),
    ))
    tags = choices.get("userTags") or []
    rows.append("👤 تگ کاربران روی رسانه: " + (
        ", ".join((f"اسلاید {tag['mediaIndex'] + 1}: " if "mediaIndex" in tag else "")
                  + "@" + html.escape(tag["username"]) for tag in tags)
        if tags else "—"))
    if draft.caption_moved_to_comment:
        comment = choices.get("firstComment") or "—"
        rows.append(f"💬 اولین کامنت (از کپشن اصلی): {html.escape(comment[:150])}{'…' if len(comment) > 150 else ''}")
    if draft.scheduled_for:
        rows.append("⏰ انتشار زمان‌بندی‌شده (تهران، شمسی): "
                    + html.escape(jalali.to_jalali_str(draft.scheduled_for)))
        rows.append("پردازشگر پست هر 5 دقیقه صف را بررسی می‌کند؛ ساعت دقیق انتشار ممکن است کمی دیرتر باشد.")
    else:
        rows.append("⏰ زمان‌بندی: انتخاب نشده؛ پس از تأیید، نوبت بعدی صف.")
    rows.extend(("", "فقط دکمهٔ «تأیید نهایی؛ افزودن به صف» درخواست را ثبت می‌کند؛ انتشار واقعی بعداً و طبق محدودیت‌ها انجام می‌شود."))
    return "\n".join(rows)


def _panel_keyboard(draft, *, slides: bool = False) -> InlineKeyboardMarkup:
    root = f"td:{draft.id}:"
    if slides:
        rows = [[_button(f"اسلاید {index + 1} ({'ویدیو' if media['kind'] == 'video' else 'عکس'})",
                         root + f"tag:{index}")]
                for index, media in enumerate(ordered_media(draft))]
        rows.append([_button("◀️ تنظیمات", root + "back")])
        return InlineKeyboardMarkup(inline_keyboard=rows)
    rows = [
        [_button("🤝 دعوت همکار", root + "set:collaborators")],
        [_button("👤 تگ کاربر روی رسانه", root + ("slides" if draft.media_group_id is not None else "tag:0")),
         _button("✏️ ویرایش کپشن اصلی", root + "set:caption")],
    ]
    if draft_media_type(draft) in ("post", "carousel"):
        if draft.caption_moved_to_comment:
            rows.append([_button("✏️ کپشن کوتاه روی پست", root + "set:short_caption")])
            if draft.visible_caption:
                rows.append([_button("🚫 بدون کپشن روی پست", root + "clear:short")])
            rows.append([_button("↩️ بازگرداندن کپشن به پست", root + "clear:move")])
        else:
            rows.append([_button("📩 انتقال کپشن به اولین کامنت", root + "move")])
    choices = draft.options or {}
    deletions = [("tag", "userTags", "تگ‌ها"),
                 ("collab", "collaborators", "همکارها")]
    for code, field, label in deletions:
        if choices.get(field):
            rows.append([_button(f"❌ حذف {label}", root + "clear:" + code)])
    rows.append([_button("🚀 تأیید نهایی؛ افزودن به صف", root + "send")])
    rows.append([_button("⏰ زمان‌بندی انتشار", root + "schedule")])
    rows.append([_button("🗑 لغو پیش‌نویس", root + "cancel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _prompt_text(draft, kind: str, slide: int | None = None) -> str:
    header = f"📝 پیش‌نویس {draft.id} — "
    if kind == "caption":
        hint = (f"\n⚠️ {html.escape(draft.error_text)}" if draft.error_text else "")
        moved_hint = (" اگر انتقال فعال است، همین متن به اولین کامنت می‌رود."
                      if draft.caption_moved_to_comment else "")
        return (header + "کپشن اصلی\n"
                "به همین پیام ریپلای کنید و کپشن اصلی را بنویسید (حداکثر 2200 نویسه)."
                " تا کپشن اصلی ثبت نشود، امکان افزودن به صف وجود ندارد."
                + moved_hint + hint)
    if kind == "short_caption":
        return (header + "کپشن کوتاه روی خود پست\n"
                "متن کوتاه را به همین پیام ریپلای کنید (حداکثر 150 نویسه). "
                "پست در ابتدا بدون کپشن است؛ برای حذف متن کوتاهِ ثبت‌شده، "
                "دکمهٔ «بدون کپشن روی پست» در تنظیمات را بزنید.")
    if kind == "collaborators":
        return (header + "همکارها\nتا 3 نام کاربریِ عمومی Business/Creator را با فاصله یا ویرگول "
                "به همین پیام ریپلای کنید؛ پذیرفتن دعوت به عهدهٔ صاحبان حساب است.")
    if kind == "tag" and slide is not None:
        media = ordered_media(draft)[slide]
        suffix = ("مختصات تگ روی این عکس به‌صورت مرکزی (0.5, 0.5) ثبت می‌شود."
                  if media["kind"] == "image" else "تگ ویدیو مختصات ندارد.")
        return (header + f"تگ کاربر روی اسلاید {slide + 1}\nنام کاربری Instagram را به همین پیام "
                f"ریپلای کنید. {suffix}")
    raise TelegramDraftError("نوع پیام تنظیمات معتبر نیست.")


async def send_draft_notice(bot, draft_id: int | None = None) -> str | None:
    """Send one already-ready panel, missing-caption ForceReply, or error."""
    claim = await claim_one_draft_notice(draft_id)
    if claim is None:
        return None
    if (claim["owner_id"] != claim["chat_id"]
            or claim["owner_id"] not in settings.admin_ids):
        logger.warning("draft notice rejected owner/chat mismatch id=%s", claim["id"])
        return "rejected"
    draft = await get_owned_draft(claim["id"], claim["owner_id"], claim["chat_id"])
    try:
        if claim["kind"] == "caption":
            sent = await bot.send_message(draft.chat_id, _prompt_text(draft, "caption"),
                                          reply_markup=ForceReply(force_reply=True))
        elif claim["kind"] == "panel":
            sent = await bot.send_message(draft.chat_id, _panel_text(draft),
                                          reply_markup=_panel_keyboard(draft))
        else:
            sent = await bot.send_message(
                draft.chat_id, "⚠️ پیش‌نویس آلبوم آماده نشد: "
                + html.escape(claim["error_text"] or "رسانه‌ها ناقص یا نامعتبرند.")
                + "\nهیچ درخواستی به صف انتشار نرفت. لطفاً رسانه‌ها را دوباره بفرستید.",
            )
        saved = await record_draft_notice(claim["id"], claim["kind"], sent.message_id)
        if not saved:
            try:
                await bot.edit_message_text("این پیش‌نویس دیگر فعال نیست.",
                                            chat_id=draft.chat_id, message_id=sent.message_id)
            except Exception:  # noqa: BLE001
                pass
        return claim["kind"]
    except Exception as exc:  # noqa: BLE001
        logger.warning("draft notice delivery uncertain id=%s error=%s", claim["id"], type(exc).__name__)
        return "delivery_uncertain"


@router.message(F.chat.type == "private", F.reply_to_message, F.text)
async def draft_prompt_reply(message: Message) -> None:
    if not message.from_user or not _admin_private(message.from_user.id, message.chat):
        raise SkipHandler()
    target = message.reply_to_message
    if not target.from_user or target.from_user.id != message.bot.id:
        raise SkipHandler()
    match = _PROMPT_ID.match(target.text or "")
    if not match:
        raise SkipHandler()  # replies to every other bot conversation retain their handlers
    draft_id = int(match.group(1))
    try:
        draft = await apply_draft_reply(
            draft_id, message.from_user.id, message.chat.id,
            target.message_id, message.text or "",
        )
        if draft.panel_message_id:
            try:
                await message.bot.edit_message_text(
                    _panel_text(draft), chat_id=draft.chat_id,
                    message_id=draft.panel_message_id, reply_markup=_panel_keyboard(draft),
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("draft panel edit unavailable id=%s error=%s", draft_id, type(exc).__name__)
                invalidated = await invalidate_stale_panel(
                    draft_id, message.from_user.id, message.chat.id, draft.panel_message_id,
                )
                if invalidated:
                    await message.answer("✅ تنظیم ذخیره شد؛ پنل قبلی باطل شد و پنل تازه می‌فرستم.")
                    await send_draft_notice(message.bot, draft_id)
                else:
                    await message.answer("وضعیت این پنل تغییر کرده است؛ قبل از اقدام، وضعیت صف را بررسی کنید.")
            else:
                await message.answer("✅ تنظیم این پیش‌نویس ذخیره شد. برای ادامه به دکمه‌های پنل برگردید.")
        else:
            await message.answer("✅ کپشن ثبت شد؛ تنظیمات و دکمهٔ افزودن به صف را می‌فرستم.")
            await send_draft_notice(message.bot, draft_id)
    except TelegramDraftError as exc:
        await message.answer("⚠️ " + html.escape(str(exc)) + "\nدر صورت معتبر بودن، به همان پیام راهنما ریپلای کنید.")
    except Exception as exc:  # noqa: BLE001
        logger.warning("draft reply uncertain id=%s error=%s", draft_id, type(exc).__name__)
        await message.answer("⚠️ ثبت پاسخ قطعی نشد؛ پیش از تکرار، پنل پیش‌نویس را بررسی کنید.")


async def _reject_album_fragment(message: Message, reason: str) -> None:
    """Persist an invalid-album tombstone, even when its first part was bad."""
    try:
        draft_id = await invalidate_telegram_album(
            message.from_user.id, message.chat.id, message.media_group_id, reason,
        )
        draft = await get_owned_draft(draft_id, message.from_user.id, message.chat.id)
        if draft.status == "queued":
            await message.answer(
                f"⚠️ {html.escape(reason)}\nدرخواست {draft.queue_id} پیش‌تر از صف انتظار خارج شده است. "
                "لطفاً وضعیت آن را در صف/Zernio بررسی کنید؛ ارسال دوبارهٔ خودکار انجام نمی‌شود."
            )
        else:
            sent = await message.answer("⚠️ " + html.escape(reason) + "\nاین آلبوم در صف ثبت نمی‌شود؛ همهٔ اسلایدها را دوباره بفرستید.")
            await record_draft_notice(draft_id, "error", sent.message_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("album rejection uncertain message=%s error=%s", message.message_id, type(exc).__name__)
        await message.answer("⚠️ آلبوم نامعتبر است؛ وضعیت رد آن نامشخص شد. از تأیید هیچ پنل مربوط به آن استفاده نکنید.")


async def _legacy_story_dialog(state: FSMContext) -> bool:
    return (await state.get_state() == MediaWait.waiting.state
            and (await state.get_data()).get("publish_kind") == "story")


@router.message(F.chat.type == "private", F.media_group_id, F.document | F.audio,
                StateFilter(None, MediaWait.waiting))
async def reject_non_photo_album(message: Message, state: FSMContext) -> None:
    if not message.from_user or not _admin_private(message.from_user.id, message.chat):
        raise SkipHandler()
    if await _legacy_story_dialog(state):
        raise SkipHandler()
    await _reject_album_fragment(message, "آلبوم برای انتشار فقط می‌تواند شامل عکس و ویدیوی قابل‌دریافت باشد.")


@router.message(F.chat.type == "private", F.document | F.video_note | F.animation | F.audio,
                StateFilter(None))
async def reject_unsupported_waiting_media(message: Message) -> None:
    if not message.from_user or not _admin_private(message.from_user.id, message.chat):
        raise SkipHandler()
    mode = await get_pending_media_intent(message.from_user.id, message.chat.id)
    if mode is None:
        raise SkipHandler()  # not a post dialog; leave other bot features alone
    prompt = ("ریلز فقط یک ویدیوی عمودی MP4/MOV به‌صورت ویدیوی تلگرام می‌پذیرد."
              if mode == "reel" else "برای پست، عکس یا ویدیوی تلگرام بفرستید؛ فایلِ سند/صوت پذیرفته نیست.")
    await message.answer("⚠️ " + prompt + "\nحالت انتظار شما حفظ شد؛ رسانهٔ درست را بفرستید.")


@router.message(F.chat.type == "private", F.photo | F.video,
                StateFilter(None, MediaWait.waiting))
async def collect_telegram_post_media(message: Message, state: FSMContext) -> None:
    if not message.from_user or not _admin_private(message.from_user.id, message.chat):
        raise SkipHandler()
    if await _legacy_story_dialog(state):
        raise SkipHandler()  # unchanged Story dialog still belongs to publish.py
    try:
        info = describe_telegram_post_media(message)
        reserved = await reserve_telegram_media(
            owner_id=message.from_user.id, chat_id=message.chat.id,
            message_id=message.message_id, media_group_id=message.media_group_id,
            media_info=info, caption=message.caption or "",
        )
        action = reserved["action"]
        if action == "already_queued":
            await message.answer(f"این رسانه قبلاً با شناسهٔ {reserved['queue_id']} به صف افزوده شد؛ درخواست تازه‌ای ساخته نشد.")
            return
        if action == "cancelled_late":
            sent = await message.answer(f"⚠️ اسلاید دیر رسید؛ درخواستِ در انتظار با شناسهٔ {reserved['queue_id']} برای جلوگیری از انتشار ناقص لغو شد. آلبوم را دوباره کامل بفرستید.")
            await record_draft_notice(reserved["draft_id"], "error", sent.message_id)
            return
        if action == "late_after_claim":
            await message.answer(f"⚠️ اسلاید دیر رسید اما درخواست {reserved['queue_id']} دیگر در حالت انتظار نیست. وضعیت واقعی آن را در صف/Zernio بررسی کنید؛ ارسال دوبارهٔ خودکار نداریم.")
            return
        if action in ("duplicate", "closed"):
            return
        if action == "too_many":
            sent = await message.answer("⚠️ این آلبوم بیش از 10 اسلاید دارد؛ پیش‌نویس رد شد و چیزی در صف ثبت نشد.")
            await record_draft_notice(reserved["draft_id"], "error", sent.message_id)
            return
        draft_id = reserved["draft_id"]
        try:
            url = await save_telegram_post_media_to_supabase(message.bot, message, info)
        except TelegramPostMediaError as exc:
            await complete_telegram_media(draft_id, message.message_id, error=str(exc))
            sent = await message.answer("⚠️ " + html.escape(str(exc)) + "\nاین پیش‌نویس وارد صف نمی‌شود.")
            await record_draft_notice(draft_id, "error", sent.message_id)
            return
        except Exception as exc:  # noqa: BLE001
            logger.warning("Telegram media storage uncertain draft=%s error=%s", draft_id, type(exc).__name__)
            await complete_telegram_media(draft_id, message.message_id,
                                          error="ذخیرهٔ فایل قطعی نشد؛ پیش‌نویس رد شد.")
            sent = await message.answer("⚠️ ذخیرهٔ رسانه قطعی نشد؛ هیچ پستی در صف ثبت نشد.")
            await record_draft_notice(draft_id, "error", sent.message_id)
            return
        ready = await complete_telegram_media(draft_id, message.message_id, url=url)
        if message.media_group_id:
            if action == "late_upload":
                await message.answer("اسلاید تازه رسید؛ دکمه‌های قبلی آلبوم قدیمی شدند. بعد از تکمیل آلبوم، تنظیمات تازه فرستاده می‌شود.")
            elif reserved["created"]:
                await message.answer("🌀 آلبوم در حال گردآوری است. پس از آخرین اسلاید و یک فاصلهٔ کوتاه، تنظیمات ارسال می‌شود؛ هیچ پستی خودکار وارد صف نمی‌شود.")
        elif ready["status"] == "ready":
            await send_draft_notice(message.bot, draft_id)
    except (TelegramDraftError, TelegramPostMediaError) as exc:
        if message.media_group_id:
            await _reject_album_fragment(message, str(exc))
        else:
            await message.answer("⚠️ " + html.escape(str(exc)) + "\nهیچ پستی در صف ثبت نشد.")
    except Exception as exc:  # noqa: BLE001
        logger.warning("Telegram media draft uncertain message=%s error=%s", message.message_id, type(exc).__name__)
        if message.media_group_id:
            await _reject_album_fragment(message, "آماده‌سازی این قطعه از آلبوم قطعی نشد؛ انتشار ناقص ممنوع است.")
        else:
            await message.answer("⚠️ وضعیت پیش‌نویس نامشخص است؛ پیش از ارسال مجدد، پیام‌های ربات را بررسی کنید. انتشار خودکاری انجام نمی‌شود.")


@router.callback_query(F.data.startswith("td:"))
async def draft_button(callback: CallbackQuery) -> None:
    if (not callback.from_user or not isinstance(callback.message, Message)
            or not _admin_private(callback.from_user.id, callback.message.chat)):
        await callback.answer("دسترسی ندارید.")
        return
    bits = (callback.data or "").split(":")
    if len(bits) < 3 or not bits[1].isdigit():
        await callback.answer("دکمه معتبر نیست.")
        return
    await callback.answer()
    draft_id = int(bits[1])
    action = bits[2]
    try:
        draft = await get_owned_draft(draft_id, callback.from_user.id, callback.message.chat.id)
        if draft.status != "ready" or draft.panel_message_id != callback.message.message_id:
            raise TelegramDraftError("این پنل قدیمی است؛ آلبوم هنوز در حال تکمیل یا قبلاً به صف افزوده شده است.")
        if action == "send" and len(bits) == 3:
            job = await enqueue_confirmed_draft(
                draft_id, callback.from_user.id, callback.message.chat.id,
                callback.message.message_id,
            )
            await callback.message.edit_text(
                "✅ درخواست با تنظیماتِ تأییدشده در صف ثبت شد."
                + f"\n🆔 شناسه صف: {job['id']}\n"
                "انتشار واقعی تنها بعداً توسط پردازشگر زمان‌بندی‌شده انجام می‌شود.",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [_button("🔄 وضعیت این پست", f"oq:refresh:{job['id']}")],
                ]),
            )
        elif action == "cancel" and len(bits) == 3:
            await cancel_draft(draft_id, callback.from_user.id, callback.message.chat.id,
                               callback.message.message_id)
            await callback.message.edit_text("🗑 پیش‌نویس لغو شد؛ هیچ پستی به صف نرفت.")
        elif action == "schedule" and len(bits) == 3:
            await callback.message.edit_text(
                _panel_text(draft) + "\n\n⏰ یکی از 4 گزینهٔ زمان‌بندی را انتخاب کنید؛ انتخاب زمان هنوز چیزی را صف نمی‌کند.",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [_button("1 ساعت بعد", f"td:{draft_id}:when:1h")],
                    [_button("3 ساعت بعد", f"td:{draft_id}:when:3h")],
                    [_button("فردا صبح (ساعت 9)", f"td:{draft_id}:when:tomorrow")],
                    [_button("زمان دلخواه (Custom)", f"td:{draft_id}:when:custom")],
                    [_button("◀️ تنظیمات", f"td:{draft_id}:back")],
                ]),
            )
        elif action == "when" and len(bits) == 4 and bits[3] in ("1h", "3h", "tomorrow"):
            await set_draft_schedule(
                draft_id, callback.from_user.id, quick_choice(bits[3]),
                panel_message_id=callback.message.message_id,
            )
            updated = await get_owned_draft(draft_id, callback.from_user.id, callback.message.chat.id)
            await callback.message.edit_text(_panel_text(updated), reply_markup=_panel_keyboard(updated))
        elif action == "when" and len(bits) == 4 and bits[3] == "custom":
            buttons = []
            if settings.miniapp_url:
                ticket = make_miniapp_ticket(callback.from_user.id, ttl_seconds=15 * 60)
                url = settings.miniapp_url.rstrip("/") + "/?" + urlencode({
                    "_token": ticket, "schedule": "draft", "item": draft_id,
                })
                buttons.append([InlineKeyboardButton(text="🗓 تقویم شمسی گرافیکی (مینی‌اپ)",
                                                     web_app=WebAppInfo(url=url))])
            buttons.extend([
                [_button("🔘 تقویم دکمه‌ای تلگرام", f"tcal:d:{draft_id}:m")],
                [_button("◀️ تنظیمات (بازخوانی زمان)", f"td:{draft_id}:back")],
            ])
            await callback.message.edit_text(
                "🗓 زمان دلخواه را با تاریخ شمسی و ساعت تهران انتخاب کنید. "
                "پس از ثبت در مینی‌اپ، به این چت برگردید و «بازخوانی زمان» را بزنید. "
                "ورود به صف فقط با «تأیید نهایی» در تنظیمات انجام می‌شود.",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
            )
        elif action == "slides" and len(bits) == 3 and draft.media_group_id is not None:
            await callback.message.edit_text(_panel_text(draft) + "\n\nاسلاید تگ را انتخاب کنید:",
                                             reply_markup=_panel_keyboard(draft, slides=True))
        elif action == "back" and len(bits) == 3:
            await callback.message.edit_text(_panel_text(draft), reply_markup=_panel_keyboard(draft))
        elif (action == "move" and len(bits) == 3) or (
            action == "clear" and len(bits) == 4 and bits[3] in ("move", "short")
        ):
            move_action = ("move" if action == "move" else
                           "restore" if bits[3] == "move" else "empty")
            draft = await change_caption_move(
                draft_id, callback.from_user.id, callback.message.chat.id,
                callback.message.message_id, action=move_action,
            )
            await callback.message.edit_text(_panel_text(draft), reply_markup=_panel_keyboard(draft))
        elif action == "clear" and len(bits) == 4:
            draft = await clear_draft_option(draft_id, callback.from_user.id, callback.message.chat.id,
                                             callback.message.message_id, bits[3])
            await callback.message.edit_text(_panel_text(draft), reply_markup=_panel_keyboard(draft))
        elif ((action == "set" and len(bits) == 4 and bits[3] in
               ("caption", "short_caption", "collaborators"))
              or (action == "tag" and len(bits) == 4 and bits[3].isdigit())):
            if action == "set":
                kind, slide = bits[3], None
                if kind == "short_caption" and not draft.caption_moved_to_comment:
                    raise TelegramDraftError("ابتدا کپشن اصلی را به اولین کامنت منتقل کنید.")
            else:
                kind, slide = "tag", int(bits[3])
                if not 0 <= slide < len(ordered_media(draft)):
                    raise TelegramDraftError("اسلاید تگ پیدا نشد.")
            prompt = await callback.message.answer(_prompt_text(draft, kind, slide),
                                                   reply_markup=ForceReply(force_reply=True))
            saved = await attach_prompt(
                draft_id, callback.from_user.id, callback.message.chat.id,
                callback.message.message_id, kind=kind, slide=slide,
                prompt_message_id=prompt.message_id,
            )
            if not saved:
                await callback.message.answer("این صفحه در همین فاصله قدیمی شد؛ به پیام راهنمای تازه پاسخ ندهید.")
        else:
            raise TelegramDraftError("دکمه معتبر یا مربوط به این رسانه نیست.")
    except (TelegramDraftError, ScheduleError) as exc:
        await callback.message.answer("⚠️ " + html.escape(str(exc)))
    except Exception as exc:  # noqa: BLE001
        logger.warning("draft callback uncertain id=%s error=%s", draft_id, type(exc).__name__)
        if action in ("clear", "move"):
            try:
                if await invalidate_stale_panel(
                    draft_id, callback.from_user.id, callback.message.chat.id,
                    callback.message.message_id,
                ):
                    await callback.message.answer("⚠️ ویرایش پنل ممکن نشد؛ پنل قبلی باطل شد و پنل تازه می‌فرستم.")
                    await send_draft_notice(callback.message.bot, draft_id)
                    return
            except Exception as inner:  # noqa: BLE001
                logger.warning("draft panel recovery uncertain id=%s error=%s", draft_id, type(inner).__name__)
        await callback.message.answer("⚠️ وضعیت این دکمه نامشخص است؛ قبل از تلاش دوباره وضعیت صف را بررسی کنید.")


async def assemble_album_drafts_once(bot) -> dict[str, int]:
    """Album/settings-only minute job; NEVER calls a publication Worker."""
    summary = {"albums_ready": 0, "albums_invalid": 0, "notices": 0}
    for _ in range(TELEGRAM_ALBUM_DRAFTS_PER_TICK):
        outcome = await prepare_one_due_album()
        if outcome in (None, "waiting"):
            break
        summary["albums_ready" if outcome == "ready" else "albums_invalid"] += 1
    for _ in range(TELEGRAM_ALBUM_DRAFTS_PER_TICK):
        outcome = await send_draft_notice(bot)
        if outcome is None:
            break
        if outcome not in ("rejected", "delivery_uncertain"):
            summary["notices"] += 1
    return summary
