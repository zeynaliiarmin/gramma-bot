"""Gramma — community handlers (comments, moderation, private replies)."""

from __future__ import annotations

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from app.bot.handlers.common import tr
from app.bot.keyboards import community_menu, main_menu
from app.bot.states import TextWait
from app.core.database import SessionLocal
from app.db.repositories import get_accounts_for_user, get_or_create_user
from app.services.meta.service import InstagramService

router = Router(name="community")

demo_comments = [
    {"id": "c1", "username": "sara_fans", "text": "عالی بود! 😍"},
    {"id": "c2", "username": "majid", "text": "قیمت رو می‌فرستید؟"},
    {"id": "c3", "username": "spammer99", "text": "فالوور رایگان کلیک کن!"},
    {"id": "c4", "username": "niloo", "text": "پست بعدی کی میاد؟"},
]


@router.callback_query(F.data == "community:menu")
async def cb_community_menu(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text("💬 <b>کامیونیتی</b>", reply_markup=community_menu())
    await callback.answer()


@router.callback_query(F.data == "community:comments")
async def cb_comments(callback: CallbackQuery):
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    svc = InstagramService()
    async with SessionLocal() as session:
        accounts = await get_accounts_for_user(session, callback.from_user.id)
        account = accounts[0] if accounts else None

    if account is None:
        await callback.message.edit_text(tr(None, "no_accounts"), reply_markup=main_menu())
        await callback.answer()
        return

    try:
        comments = await svc.list_comments(account, "demo_media")
    except Exception:  # noqa: BLE001
        comments = demo_comments

    kb_buttons = []
    for c in comments[:8]:
        label = f"💬 {c['username']}: {c['text'][:26]}"
        kb_buttons.append([InlineKeyboardButton(text=label, callback_data=f"comment:reply:{c['id']}")])
    kb_buttons.append([InlineKeyboardButton(text="◀️ بازگشت", callback_data="nav:community")])
    await callback.message.edit_text(
        "💬 آخرین کامنت‌ها:\n(برای پاسخ، روی کامنت بزنید)",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_buttons),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("comment:reply:"))
async def cb_comment_reply(callback: CallbackQuery, state: FSMContext):
    comment_id = callback.data.split(":", 2)[2]
    await state.set_state(TextWait.waiting)
    await state.update_data(topic_target="comment_reply", comment_id=comment_id)
    await callback.message.edit_text(tr(None, "send_reply"))
    await callback.answer()


@router.callback_query(F.data == "community:moderate")
async def cb_moderate(callback: CallbackQuery):
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔇 مخفی کردن کامنت اسپم", callback_data="mod:hide:c3")],
            [InlineKeyboardButton(text="🗑 حذف کامنت", callback_data="mod:delete:c3")],
            [InlineKeyboardButton(text="✉️ پاسخ خصوصی به کامنت", callback_data="mod:private:c1")],
            [InlineKeyboardButton(text="◀️ بازگشت", callback_data="nav:community")],
        ]
    )
    await callback.message.edit_text("🛡 ابزارهای مدیریت کامنت:", reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data.startswith("mod:"))
async def cb_mod_action(callback: CallbackQuery, state: FSMContext):
    parts = callback.data.split(":")
    action, comment_id = parts[1], parts[2]

    async with SessionLocal() as session:
        accounts = await get_accounts_for_user(session, callback.from_user.id)
        account = accounts[0] if accounts else None

    svc = InstagramService()
    if action == "hide":
        await svc.hide_comment(account, comment_id, True)
        result_text = "🔇 کامنت مخفی شد."
    elif action == "delete":
        await svc.delete_comment(account, comment_id)
        result_text = "🗑 کامنت حذف شد."
    elif action == "private":
        await state.set_state(TextWait.waiting)
        await state.update_data(topic_target="comment_private_reply", comment_id=comment_id)
        await callback.message.edit_text("✉️ متن پاسخ خصوصی را بنویسید:")
        await callback.answer()
        return
    else:
        result_text = tr(None, "something_wrong")

    await callback.message.edit_text(result_text, reply_markup=community_menu())
    await callback.answer()


@router.callback_query(F.data == "community:private")
async def cb_private(callback: CallbackQuery):
    await callback.message.edit_text(
        "✉️ پاسخ خصوصی: روی یک کامنت، دکمه «پاسخ خصوصی» را بزنید (از بخش ابزارها).",
        reply_markup=community_menu(),
    )
    await callback.answer()
