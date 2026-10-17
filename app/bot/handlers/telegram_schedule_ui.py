"""No-typing, inline Jalali calendar fallback for the existing Mini-App picker."""

from __future__ import annotations

import html
import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from aiogram import F, Router
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from app.core.config import get_settings
from app.core.database import SessionLocal
from app.services.telegram_post_drafts import get_owned_draft
from app.services.telegram_scheduling import (
    ScheduleError, get_owned_scheduled_post, jalali_choice,
    set_draft_schedule, set_queue_schedule,
)
from app.utils import jalali

router = Router(name="telegram_schedule_picker")
logger = logging.getLogger("gramma.inline_jalali")
settings = get_settings()
_LOCAL = ZoneInfo("Asia/Tehran")
_WEEK = ["ش", "ی", "د", "س", "چ", "پ", "ج"]


def _button(title: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=title, callback_data=data)


def _key(target: str, item_id: int, stage: str, *values: int) -> str:
    return ":".join(["tcal", target, str(item_id), stage,
                     *[str(value) for value in values]])


def _month_after(year: int, month: int, offset: int) -> tuple[int, int]:
    slot = year * 12 + month - 1 + offset
    return slot // 12, slot % 12 + 1


def _days(year: int, month: int) -> int:
    for day in range(31, 27, -1):
        try:
            local = jalali.from_jalali(year, month, day)
            if jalali.to_jalali(local) == (year, month, day):
                return day
        except (ValueError, OverflowError):
            pass
    raise ScheduleError("ماه شمسی نامعتبر است.")


def _month_keyboard(target: str, item_id: int, year: int, month: int) -> InlineKeyboardMarkup:
    today = datetime.now(_LOCAL)
    current = jalali.to_jalali(today)
    if not (current[0] <= year <= current[0] + 1 and 1 <= month <= 12):
        raise ScheduleError("ماه انتخاب‌شده در بازهٔ مجاز نیست.")
    count = _days(year, month)
    first = jalali.from_jalali(year, month, 1).weekday()  # Mon=0; Sat=5
    offset = (first + 2) % 7
    rows: list[list[InlineKeyboardButton]] = [
        [_button("◀ ماه قبل", _key(target, item_id, "m", *_month_after(year, month, -1))),
         _button(f"{jalali.FA_MONTHS[month - 1]} {year}", "tcal:noop"),
         _button("ماه بعد ▶", _key(target, item_id, "m", *_month_after(year, month, 1)))],
        [_button(weekday, "tcal:noop") for weekday in _WEEK],
    ]
    for start in range(0, offset + count, 7):
        week = []
        for spot in range(start, start + 7):
            day = spot - offset + 1
            if 1 <= day <= count:
                selected = jalali.from_jalali(year, month, day).astimezone(_LOCAL)
                out_of_range = (selected.date() < today.date()
                                or selected > today + timedelta(days=365))
                week.append(_button(f"{day}" if not out_of_range else f"·{day}",
                                    "tcal:noop" if out_of_range else
                                    _key(target, item_id, "d", year, month, day)))
            else:
                week.append(_button(" ", "tcal:noop"))
        rows.append(week)
    rows.append([_button("◀ بازگشت به تنظیمات", f"td:{item_id}:back" if target == "d"
                         else f"calendar:post:{item_id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _hour_keyboard(target: str, item_id: int, year: int, month: int,
                   day: int) -> InlineKeyboardMarkup:
    rows = [[_button(f"{hour:02d}", _key(target, item_id, "h", year, month, day, hour))
             for hour in range(start, start + 6)] for start in range(0, 24, 6)]
    rows.append([_button("◀ تقویم", _key(target, item_id, "m", year, month))])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _minute_keyboard(target: str, item_id: int, year: int, month: int,
                     day: int, hour: int) -> InlineKeyboardMarkup:
    rows = [[_button(f"{minute:02d}", _key(target, item_id, "s", year, month, day, hour, minute))
             for minute in range(start, start + 20, 5)] for start in range(0, 60, 20)]
    rows.append([_button("◀ ساعت", _key(target, item_id, "d", year, month, day))])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _guard(target: str, item_id: int, owner_id: int, panel_id: int) -> None:
    if owner_id not in settings.admin_ids:
        raise ScheduleError("تقویم فقط برای مدیر است.")
    if target == "d":
        draft = await get_owned_draft(item_id, owner_id, owner_id)
        if draft.status != "ready" or draft.panel_message_id != panel_id:
            raise ScheduleError("این پنل پیش‌نویس قدیمی یا صف‌شده است.")
    elif target == "q":
        async with SessionLocal() as session:
            job = await get_owned_scheduled_post(session, item_id, owner_id)
        if not job or job["status"] != "pending" or job["scheduled_for"] <= datetime.now(timezone.utc) + timedelta(minutes=5):
            raise ScheduleError("زمان این پست دیگر قابل ویرایش نیست.")
    else:
        raise ScheduleError("نوع تقویم معتبر نیست.")


@router.callback_query(F.data == "tcal:noop")
async def noop(callback: CallbackQuery) -> None:
    await callback.answer()


@router.callback_query(F.data.startswith("tcal:"))
async def inline_calendar(callback: CallbackQuery) -> None:
    if (not callback.from_user or not isinstance(callback.message, Message)
            or callback.message.chat.type != "private"
            or callback.message.chat.id != callback.from_user.id):
        await callback.answer("فقط در چت خصوصی مدیر فعال است.")
        return
    await callback.answer()
    parts = (callback.data or "").split(":")
    try:
        if (len(parts) < 4 or parts[1] not in ("d", "q") or not parts[2].isdigit()
                or parts[3] not in ("m", "d", "h", "s")
                or any(not value.isdigit() for value in parts[4:])):
            raise ScheduleError("دکمهٔ تقویم معتبر نیست.")
        target, item_id, stage = parts[1], int(parts[2]), parts[3]
        await _guard(target, item_id, callback.from_user.id, callback.message.message_id)
        args = [int(value) for value in parts[4:]]
        if stage == "m":
            if len(args) == 0:
                year, month, _ = jalali.to_jalali(datetime.now(_LOCAL))
            elif len(args) == 2:
                year, month = args
            else:
                raise ScheduleError("ماه شمسی نامعتبر است.")
            kb = _month_keyboard(target, item_id, year, month)
            label = "روز شمسی موردنظر را انتخاب کنید (اعداد کم‌رنگ گذشته‌اند)."
        elif stage == "d" and len(args) == 3:
            year, month, day = args
            if not 1 <= month <= 12 or not 1 <= day <= _days(year, month):
                raise ScheduleError("روز شمسی معتبر نیست.")
            kb = _hour_keyboard(target, item_id, year, month, day)
            label = f"{year}/{month:02d}/{day:02d} · ساعت تهران را انتخاب کنید:"
        elif stage == "h" and len(args) == 4:
            year, month, day, hour = args
            if not 0 <= hour <= 23 or not 1 <= month <= 12 or not 1 <= day <= _days(year, month):
                raise ScheduleError("ساعت یا روز معتبر نیست.")
            kb = _minute_keyboard(target, item_id, year, month, day, hour)
            label = f"{year}/{month:02d}/{day:02d} ساعت {hour:02d} · دقیقه را انتخاب کنید:"
        elif stage == "s" and len(args) == 5:
            year, month, day, hour, minute = args
            if minute % 5:
                raise ScheduleError("دقیقه باید یکی از گزینه‌های دکمه‌ای باشد.")
            when = jalali_choice(year, month, day, hour, minute)
            if target == "d":
                await set_draft_schedule(item_id, callback.from_user.id, when,
                                         panel_message_id=callback.message.message_id)
                from app.bot.handlers.telegram_drafts import _panel_keyboard, _panel_text

                draft = await get_owned_draft(item_id, callback.from_user.id, callback.from_user.id)
                await callback.message.edit_text(_panel_text(draft), reply_markup=_panel_keyboard(draft))
            else:
                await set_queue_schedule(item_id, callback.from_user.id, when)
                from app.bot.handlers.calendar import show_queue_detail

                await show_queue_detail(callback, item_id)
            return
        else:
            raise ScheduleError("دکمهٔ تقویم معتبر نیست.")
        await callback.message.edit_text("🗓 " + label + "\nتاریخ و ساعت برای تهران است؛ ثبت زمان به‌تنهایی انتشار یا صف‌گذاری نیست.",
                                         reply_markup=kb)
    except (ScheduleError, ValueError) as exc:
        await callback.message.answer("⚠️ " + html.escape(str(exc)))
    except Exception as exc:  # noqa: BLE001
        logger.warning("Jalali button failed error=%s", type(exc).__name__)
        await callback.message.answer("⚠️ انتخاب تقویم قطعی نشد؛ زمان ذخیره نشده است یا وضعیت پنل را بررسی کنید.")
