"""Jalali content calendar and guarded pending Telegram/Zernio queue editing."""

from __future__ import annotations

import html
import logging
import re
from urllib.parse import urlencode

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, ForceReply, InlineKeyboardButton, InlineKeyboardMarkup, Message, WebAppInfo

from app.bot.keyboards import back
from app.core.config import get_settings
from app.core.database import SessionLocal
from app.db.repositories import get_accounts_for_user, get_or_create_user
from app.services.calendar import build_calendar
from app.services.telegram_scheduling import (
    ScheduleError, attach_queue_caption_prompt, get_owned_scheduled_post,
    list_scheduled_posts, quick_choice, set_queue_schedule, update_queue_caption,
)
from app.utils import jalali
from app.webapp.auth import make_miniapp_ticket

router = Router(name="calendar")
settings = get_settings()
logger = logging.getLogger("gramma.schedule_calendar")
_REPLY = re.compile(r"^✏️ صف ([0-9]+) — (کپشن اصلی کامنت|کپشن روی پست|کپشن)\n")


def _private(callback: CallbackQuery) -> bool:
    return bool(callback.from_user and callback.from_user.id in settings.admin_ids
                and isinstance(callback.message, Message)
                and callback.message.chat.type == "private"
                and callback.message.chat.id == callback.from_user.id)


def _btn(text_value: str, action: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text_value, callback_data=action)


def _queue_details(job: dict) -> str:
    payload = job["payload"] or {}
    kind = {"post": "عکس / پست فید", "reel": "ویدیو / ریلز با نمایش در فید",
            "carousel": "آلبوم / کاروسل"}.get(payload.get("media_type"), "رسانه")
    visible = payload.get("caption") or ""
    moved = payload.get("caption_moved_to_comment") is True
    parts = [
        f"📅 <b>پست زمان‌بندی‌شده · صف {job['id']}</b>",
        "🕰 زمان تهران (شمسی): " + html.escape(jalali.to_jalali_str(job["scheduled_for"])),
        "🎯 پیج: @" + html.escape(payload.get("expected_username") or "—"),
        "🖼 نوع رسانه: " + kind,
        "📝 کپشن روی پست: " + (html.escape(visible[:350]) if visible else "خالی"),
    ]
    if moved:
        original = (payload.get("instagram_options") or {}).get("firstComment") or ""
        parts.append("💬 کپشن اصلی در اولین کامنت: " + html.escape(original[:350]))
        parts.append("⚠️ درج اولین کامنت به مجوز comments اتصال Zernio نیاز دارد.")
    parts.append("وضعیت صف: " + html.escape(job["status"]))
    if job["status"] != "pending":
        parts.append("این درخواست دیگر قابل ویرایش نیست؛ وضعیت واقعی را در صف/Zernio بررسی کنید.")
    else:
        parts.append("تا پیش از برداشته‌شدن توسط Worker، زمان یا کپشن قابل ویرایش است.")
    return "\n".join(parts)


def _detail_keyboard(job: dict) -> InlineKeyboardMarkup:
    rows = []
    if job["status"] == "pending":
        rows.append([_btn("✏️ ویرایش", f"calendar:edit:{job['id']}")])
    rows.append([_btn("◀️ تقویم", "calendar:menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def show_queue_detail(callback: CallbackQuery, queue_id: int) -> None:
    if not _private(callback):
        raise ScheduleError("جزئیات فقط در چت خصوصی مدیر است.")
    async with SessionLocal() as session:
        job = await get_owned_scheduled_post(session, queue_id, callback.from_user.id)
    if job is None:
        raise ScheduleError("پست زمان‌بندی‌شدهٔ متعلق به شما پیدا نشد.")
    await callback.message.edit_text(_queue_details(job), reply_markup=_detail_keyboard(job))


async def _calendar_view(user_id: int, page: int) -> tuple[str, InlineKeyboardMarkup]:
    async with SessionLocal() as session:
        await get_or_create_user(session, user_id)
        accounts = await get_accounts_for_user(session, user_id)
        legacy = await build_calendar(session, user_id, [account.id for account in accounts]) if accounts else []
    queue = await list_scheduled_posts(user_id, limit=8, offset=page * 8) if user_id in settings.admin_ids else []
    lines = ["📅 <b>تقویم محتوا · تهران / شمسی</b>",
             "پست‌های زمان‌بندی‌شدهٔ صف Zernio:"]
    if not queue:
        lines.append("برای این صفحه پستی در صفِ آینده نیست.")
    rows = []
    for job in queue:
        payload = job["payload"]
        kind = {"post": "📸", "reel": "🎬", "carousel": "🌀"}.get(payload.get("media_type"), "🖼")
        when = jalali.to_jalali_str(job["scheduled_for"])
        label = (payload.get("caption") or (payload.get("instagram_options") or {}).get("firstComment") or "بدون کپشن")[:18]
        rows.append([_btn(f"{kind} {when} · {label}", f"calendar:post:{job['id']}")])
    nav = []
    if page > 0:
        nav.append(_btn("◀️ قبلی", f"calendar:page:{page - 1}"))
    if len(queue) == 8:
        nav.append(_btn("بعدی ▶", f"calendar:page:{page + 1}"))
    if nav:
        rows.append(nav)
    if legacy:
        lines.append("\nپست‌های سامانهٔ قدیمی نیز محفوظ‌اند:")
        for day in legacy[:3]:
            lines.append(html.escape(day["label"]) + f" · {len(day['posts'])} مورد")
        rows.append([_btn("🕰 فهرست قدیمی", "schedule:list")])
    rows.append(back())
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


@router.message(Command("calendar"))
async def cmd_calendar(message: Message):
    text_value, keyboard = await _calendar_view(message.from_user.id, 0)
    await message.answer(text_value, reply_markup=keyboard)


@router.callback_query(F.data == "calendar:menu")
async def cb_calendar_menu(callback: CallbackQuery):
    await callback.answer()
    text_value, keyboard = await _calendar_view(callback.from_user.id, 0)
    await callback.message.edit_text(text_value, reply_markup=keyboard)


@router.callback_query(F.data.startswith("calendar:page:"))
async def cb_calendar_page(callback: CallbackQuery):
    await callback.answer()
    try:
        page = int((callback.data or "").split(":")[2])
        if not 0 <= page <= 100:
            raise ValueError("invalid page")
        text_value, keyboard = await _calendar_view(callback.from_user.id, page)
        await callback.message.edit_text(text_value, reply_markup=keyboard)
    except ValueError:
        await callback.message.answer("این صفحهٔ تقویم معتبر نیست.")


@router.callback_query(F.data.startswith("calendar:post:"))
async def cb_calendar_post(callback: CallbackQuery):
    await callback.answer()
    try:
        await show_queue_detail(callback, int((callback.data or "").split(":")[2]))
    except (ValueError, ScheduleError) as exc:
        await callback.message.answer("⚠️ " + html.escape(str(exc)))


@router.callback_query(F.data.startswith("calendar:edit:"))
async def cb_calendar_edit(callback: CallbackQuery):
    if not _private(callback):
        await callback.answer("دسترسی ندارید.")
        return
    await callback.answer()
    try:
        queue_id = int((callback.data or "").split(":")[2])
        async with SessionLocal() as session:
            job = await get_owned_scheduled_post(session, queue_id, callback.from_user.id)
        if not job or job["status"] != "pending":
            raise ScheduleError("این درخواست دیگر قابل ویرایش نیست.")
        moved = job["payload"].get("caption_moved_to_comment") is True
        rows = [[_btn("⏰ تغییر زمان", f"calendar:time:{queue_id}:menu")]]
        if moved:
            rows.append([_btn("✏️ کپشن روی پست", f"calendar:caption:{queue_id}:visible")])
            rows.append([_btn("✏️ کپشن اصلی در کامنت", f"calendar:caption:{queue_id}:comment")])
            rows.append([_btn("🚫 بدون کپشن روی پست", f"calendar:caption:{queue_id}:empty")])
        else:
            rows.append([_btn("✏️ ویرایش کپشن", f"calendar:caption:{queue_id}:caption")])
        rows.append([_btn("◀️ جزئیات", f"calendar:post:{queue_id}")])
        await callback.message.edit_text(
            _queue_details(job) + "\n\n✏️ تغییر فقط تا وقتی صف در حالت انتظار است مجاز است.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
        )
    except (ValueError, ScheduleError) as exc:
        await callback.message.answer("⚠️ " + html.escape(str(exc)))


@router.callback_query(F.data.startswith("calendar:time:"))
async def cb_calendar_time(callback: CallbackQuery):
    if not _private(callback):
        await callback.answer("دسترسی ندارید.")
        return
    await callback.answer()
    parts = (callback.data or "").split(":")
    try:
        if len(parts) != 4 or not parts[2].isdigit():
            raise ScheduleError("دکمهٔ زمان نامعتبر است.")
        queue_id, choice = int(parts[2]), parts[3]
        async with SessionLocal() as session:
            job = await get_owned_scheduled_post(session, queue_id, callback.from_user.id)
        if not job or job["status"] != "pending":
            raise ScheduleError("این پست دیگر در صفِ قابل ویرایش نیست.")
        if choice in ("1h", "3h", "tomorrow"):
            await set_queue_schedule(queue_id, callback.from_user.id, quick_choice(choice))
            await show_queue_detail(callback, queue_id)
        elif choice in ("menu", "custom"):
            if choice == "menu":
                rows = [
                    [_btn("1 ساعت بعد", f"calendar:time:{queue_id}:1h")],
                    [_btn("3 ساعت بعد", f"calendar:time:{queue_id}:3h")],
                    [_btn("فردا صبح (ساعت 9)", f"calendar:time:{queue_id}:tomorrow")],
                    [_btn("زمان دلخواه (Custom)", f"calendar:time:{queue_id}:custom")],
                ]
            else:
                rows = []
                if settings.miniapp_url:
                    ticket = make_miniapp_ticket(callback.from_user.id, ttl_seconds=15 * 60)
                    url = settings.miniapp_url.rstrip("/") + "/?" + urlencode({
                        "_token": ticket, "schedule": "queue", "item": queue_id,
                    })
                    rows.append([InlineKeyboardButton(text="🗓 تقویم شمسی گرافیکی (مینی‌اپ)",
                                                      web_app=WebAppInfo(url=url))])
                rows.append([_btn("🔘 تقویم دکمه‌ای تلگرام", f"tcal:q:{queue_id}:m")])
            rows.append([_btn("◀️ جزئیات و بازخوانی زمان", f"calendar:post:{queue_id}")])
            await callback.message.edit_text(
                "⏰ " + ("گزینهٔ تغییر زمان را انتخاب کنید." if choice == "menu" else
                           "زمان دلخواه را در ساعت تهران و تقویم شمسی انتخاب کنید. پس از ذخیره در مینی‌اپ، جزئیات را بازخوانی کنید."),
                reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
            )
        else:
            raise ScheduleError("گزینهٔ زمان معتبر نیست.")
    except (ValueError, ScheduleError) as exc:
        await callback.message.answer("⚠️ " + html.escape(str(exc)))
    except Exception as exc:  # noqa: BLE001
        logger.warning("calendar time uncertain error=%s", type(exc).__name__)
        await callback.message.answer("⚠️ تغییر زمان قطعی نشد؛ جزئیات صف را بررسی کنید.")


@router.callback_query(F.data.startswith("calendar:caption:"))
async def cb_calendar_caption(callback: CallbackQuery):
    if not _private(callback):
        await callback.answer("دسترسی ندارید.")
        return
    await callback.answer()
    parts = (callback.data or "").split(":")
    try:
        if len(parts) != 4 or not parts[2].isdigit():
            raise ScheduleError("دکمهٔ کپشن معتبر نیست.")
        queue_id, kind = int(parts[2]), parts[3]
        if kind == "empty":
            await update_queue_caption(queue_id, callback.from_user.id,
                                       kind="visible", value="")
            await show_queue_detail(callback, queue_id)
            return
        labels = {"caption": "کپشن", "visible": "کپشن روی پست",
                  "comment": "کپشن اصلی کامنت"}
        if kind not in labels:
            raise ScheduleError("نوع کپشن معتبر نیست.")
        prompt = await callback.message.answer(
            f"✏️ صف {queue_id} — {labels[kind]}\n"
            "کپشن جدید را فقط در پاسخ به همین پیام بفرستید. "
            + ("حداکثر 150 نویسه؛ برای خالی‌کردن از دکمهٔ بدون کپشن استفاده کنید."
               if kind == "visible" else "حداکثر 2200 نویسه."),
            reply_markup=ForceReply(force_reply=True),
        )
        await attach_queue_caption_prompt(queue_id, callback.from_user.id,
                                          prompt.message_id, kind)
    except (ValueError, ScheduleError) as exc:
        await callback.message.answer("⚠️ " + html.escape(str(exc)))
    except Exception as exc:  # noqa: BLE001
        logger.warning("calendar caption prompt uncertain error=%s", type(exc).__name__)
        await callback.message.answer("⚠️ شروع ویرایش کپشن قطعی نشد؛ دوباره از جزئیات اقدام کنید.")


@router.message(F.chat.type == "private", F.reply_to_message, F.text)
async def queued_caption_reply(message: Message):
    if (not message.from_user or message.from_user.id not in settings.admin_ids
            or message.chat.id != message.from_user.id):
        return
    replied = message.reply_to_message
    if not replied.from_user or replied.from_user.id != message.bot.id:
        return
    match = _REPLY.match(replied.text or "")
    if not match:
        from aiogram.dispatcher.event.bases import SkipHandler

        raise SkipHandler()
    queue_id = int(match.group(1))
    kind = {"کپشن": "caption", "کپشن روی پست": "visible",
            "کپشن اصلی کامنت": "comment"}[match.group(2)]
    try:
        result = await update_queue_caption(
            queue_id, message.from_user.id, kind=kind, value=message.text or "",
            prompt_message_id=replied.message_id,
        )
        await message.answer("✅ کپشن صف ویرایش شد. برای دیدن جزئیات یا تغییر زمان از تقویم استفاده کنید.\n"
                             "🕰 زمان تهران (شمسی): " + html.escape(result["jalali"]),
                             reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                                 [_btn("📅 جزئیات پست", f"calendar:post:{queue_id}")],
                             ]))
    except ScheduleError as exc:
        await message.answer("⚠️ " + html.escape(str(exc)))
    except Exception as exc:  # noqa: BLE001
        logger.warning("calendar caption edit uncertain id=%s error=%s", queue_id, type(exc).__name__)
        await message.answer("⚠️ نتیجهٔ ویرایش مشخص نیست؛ از تقویم جزئیات صف را بازخوانی کنید.")
