"""Gramma — content calendar handler (/calendar)."""

from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

from app.bot.handlers.common import tr
from app.bot.keyboards import back
from app.core.database import SessionLocal
from app.db.repositories import get_accounts_for_user, get_or_create_user
from app.services.calendar import build_calendar, format_calendar

router = Router(name="calendar")


@router.message(Command("calendar"))
async def cmd_calendar(message: Message):
    text = await _calendar_text(message.from_user.id)
    await message.answer(text)


@router.callback_query(F.data == "calendar:menu")
async def cb_calendar_menu(callback: CallbackQuery):
    text = await _calendar_text(callback.from_user.id)
    await callback.message.edit_text(text)
    await callback.answer()


async def _calendar_text(telegram_id: int) -> str:
    async with SessionLocal() as session:
        user = await get_or_create_user(session, telegram_id)
        accounts = await get_accounts_for_user(session, telegram_id)
        ids = [a.id for a in accounts]
        if not ids:
            return "📅 هنوز پیجی متصل نیست.\nاز /connect شروع کنید."
        days = await build_calendar(session, telegram_id, ids)
    return format_calendar(days)
