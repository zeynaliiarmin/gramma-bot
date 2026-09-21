"""Gramma — semantic search handler (/search).

The actual search execution lives in the shared TextWait consumer
(handlers/ai.py) so all free-text dialogs resolve in one place.
"""

from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from app.bot.states import TextWait

router = Router(name="search")


@router.message(Command("search"))
async def cmd_search(message: Message, state: FSMContext):
    await state.set_state(TextWait.waiting)
    await state.update_data(topic_target="search")
    await message.answer("🔍 چه چیزی را در کامنت‌ها و دایرکت‌هایتان جستجو می‌کنید؟")


@router.callback_query(F.data == "search:menu")
async def cb_search_menu(callback: CallbackQuery, state: FSMContext):
    await state.set_state(TextWait.waiting)
    await state.update_data(topic_target="search")
    await callback.message.edit_text("🔍 عبارت جستجو را بنویسید:")
    await callback.answer()
