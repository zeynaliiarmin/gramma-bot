"""Gramma — direct-message handlers (inbox, categorization, quick replies)."""

from __future__ import annotations

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from app.bot.handlers.common import tr
from app.bot.keyboards import direct_menu, main_menu
from app.bot.states import TextWait
from app.core.database import SessionLocal
from app.db.repositories import get_accounts_for_user
from app.services.ai import classify_dm_async
from app.services.meta.service import InstagramService

router = Router(name="direct")

demo_subjects = [
    "سلام، قیمت محصول رو می‌فرستید؟",
    "کارت هدیه گرفتی؟ روی لینک بزن!",
    "پست دیروز عالی بود، ممنون 🙏",
    "کلیک کن برنده شوید!",
    "لطفاً راهنمایی کنید چطور سفارش بدم؟",
]

CATEGORY_EMOJI = {"needs_reply": "📌", "spam": "🗑", "replied": "✅", "archived": "📦"}


@router.callback_query(F.data == "direct:menu")
async def cb_direct_menu(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text("📥 <b>دایرکت</b>", reply_markup=direct_menu())
    await callback.answer()


@router.callback_query(F.data == "direct:inbox")
async def cb_inbox(callback: CallbackQuery):
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
        conversations = await svc.list_conversations(account)
    except Exception:  # noqa: BLE001
        conversations = [
            {"id": f"sim_t{i}", "name": f"کاربر {i}", "last_message": s}
            for i, s in enumerate(demo_subjects, start=1)
        ]

    kb_buttons = []
    for conv in conversations[:8]:
        name = conv.get("name") or f"thread {conv['id']}"
        kb_buttons.append([
            InlineKeyboardButton(text=f"💬 {name}", callback_data=f"dm:reply:{conv['id']}"),
            InlineKeyboardButton(text="✨", callback_data=f"dm:suggest:{conv['id']}"),
        ])
    kb_buttons.append([InlineKeyboardButton(text="◀️ بازگشت", callback_data="nav:direct")])
    await callback.message.edit_text(
        "📥 گفتگوهای اخیر:\n(💬 = پاسخ دادن، ✨ = پیشنهاد پاسخ هوشمند)",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_buttons),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("dm:suggest:"))
async def cb_dm_suggest(callback: CallbackQuery):
    """AI-suggested reply to a DM — routed through OpenClaw (fallback)."""
    from app.services.openclaw import OpenClawError, suggest_dm_reply

    thread_id = callback.data.split(":", 2)[2]
    await callback.answer("در حال تولید پیشنهاد…")

    last_message = ""
    sender = ""
    async with SessionLocal() as session:
        accounts = await get_accounts_for_user(session, callback.from_user.id)
        account = accounts[0] if accounts else None
    if account is not None:
        try:
            conversations = await InstagramService().list_conversations(account)
        except Exception:  # noqa: BLE001
            conversations = [
                {"id": f"sim_t{i}", "name": f"کاربر {i}", "last_message": s}
                for i, s in enumerate(demo_subjects, start=1)
            ]
        hit = next((c for c in conversations if str(c.get("id")) == thread_id), None)
        if hit:
            last_message = hit.get("last_message", "")
            sender = hit.get("name", "")

    if not last_message:
        await callback.message.edit_text(
            tr(None, "something_wrong"), reply_markup=direct_menu()
        )
        return

    try:
        suggestion = await suggest_dm_reply(
            last_message,
            sender_name=sender,
            language="fa",
            user_id=callback.from_user.id,
            account_id=account.id if account else None,
        )
    except OpenClawError:
        suggestion = "سلام! ممنون از پیام شما 🙏 به‌زودی پاسخ کامل را ارسال می‌کنیم."

    await callback.message.edit_text(
        f"🎯 <b>پیشنهاد پاسخ هوشمند:</b>\n\n{suggestion}\n\n"
        f"برای ارسال، روی گفتگو بزنید و متن را بنویسید.",
        reply_markup=direct_menu(),
    )


@router.callback_query(F.data.startswith("dm:reply:"))
async def cb_dm_reply(callback: CallbackQuery, state: FSMContext):
    thread_id = callback.data.split(":", 2)[2]
    await state.set_state(TextWait.waiting)
    await state.update_data(topic_target="dm_reply", thread_id=thread_id)
    await callback.message.edit_text(tr(None, "send_reply"))
    await callback.answer()


@router.callback_query(F.data == "direct:categorize")
async def cb_categorize(callback: CallbackQuery):
    lines = ["🗂 دسته‌بندی خودکار دایرکت‌ها:\n"]
    for subject in demo_subjects:
        label = await classify_dm_async(subject, locale="fa")
        lines.append(f"{CATEGORY_EMOJI.get(label, '•')} {subject[:42]} → {label}")
    await callback.message.edit_text("\n".join(lines))
    await callback.answer()


@router.callback_query(F.data == "direct:quick")
async def cb_quick(callback: CallbackQuery):
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✅ ممنون از پیام شما!", callback_data="dm:quicksend:thanks")],
            [InlineKeyboardButton(text="📦 سفارش شما ثبت شد", callback_data="dm:quicksend:order")],
            [InlineKeyboardButton(text="⏰ به‌زودی پاسخ می‌دهیم", callback_data="dm:quicksend:later")],
            [InlineKeyboardButton(text="◀️ بازگشت", callback_data="nav:direct")],
        ]
    )
    await callback.message.edit_text("✍️ پاسخ‌های سریع:", reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data.startswith("dm:quicksend:"))
async def cb_quick_send(callback: CallbackQuery):
    key = callback.data.split(":", 2)[2]
    texts = {
        "thanks": "ممنون از پیام شما! 🙏",
        "order": "سفارش شما با موفقیت ثبت شد ✅",
        "later": "به‌زودی پاسخ شما را می‌دهیم ⏰",
    }
    async with SessionLocal() as session:
        accounts = await get_accounts_for_user(session, callback.from_user.id)
        account = accounts[0] if accounts else None
    svc = InstagramService()
    if account:
        await svc.send_dm(account, "demo_thread", texts[key])
    await callback.message.edit_text(
        f"✅ ارسال شد:\n{texts[key]}\n(در حالت دمو شبیه‌سازی شد)", reply_markup=direct_menu()
    )
    await callback.answer()
