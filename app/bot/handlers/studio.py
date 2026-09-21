"""Gramma v3 — content-studio handlers: web search, templates, PDF report."""

from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from aiogram.types import BufferedInputFile

from app.bot.states import TextWait
from app.core.database import SessionLocal
from app.db.repositories import get_accounts_for_user, get_or_create_user
from app.services.insights import build_health_report, format_health_report
from app.services.pdf_report import build_health_pdf_bytes
from app.services.templates import list_templates, render_template
from app.services.web_search import format_web_results, web_search

router = Router(name="studio")


# ── Web search (Tavily) ──────────────────────────────────────
@router.message(Command("websearch"))
async def cmd_websearch(message: Message, state: FSMContext):
    await state.set_state(TextWait.waiting)
    await state.update_data(topic_target="websearch")
    await message.answer("🌐 موضوعی که می‌خواهید درباره‌اش جستجوی وب کنم را بنویسید:")


# ── Post templates ───────────────────────────────────────────
@router.message(Command("templates"))
async def cmd_templates(message: Message):
    await _show_templates(message, page=0)


async def _show_templates(message: Message, page: int = 0):
    async with SessionLocal() as session:
        await get_or_create_user(
            session, message.from_user.id,
            username=message.from_user.username, full_name=message.from_user.full_name,
        )
        accounts = await get_accounts_for_user(session, message.from_user.id)
        if not accounts:
            await message.answer("ابتدا یک پیج متصل کنید (/connect).")
            return
        templates = await list_templates(session, accounts[0].id)

    per_page = 5
    chunk = templates[page * per_page : (page + 1) * per_page]
    if not chunk:
        await message.answer("🧩 الگوی دیگری نیست.")
        return
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=f"🧩 {t['title']}", callback_data=f"tpl:pick:{-t['id'] if t['id'] <= 0 else t['id']}")]
            for t in chunk
        ]
        + ([[InlineKeyboardButton(text="بعدی ➡️", callback_data=f"tpl:page:{page + 1}")]] if (page + 1) * per_page < len(templates) else [])
    )
    await message.answer("🧩 <b>الگوهای پست</b> — یکی را انتخاب کنید تا کپشن آماده شود:", reply_markup=kb)


@router.callback_query(F.data.startswith("tpl:page:"))
async def cb_tpl_page(callback: CallbackQuery):
    page = int(callback.data.split(":")[2])
    await _show_templates(callback.message, page)
    await callback.answer()


@router.callback_query(F.data.startswith("tpl:pick:"))
async def cb_tpl_pick(callback: CallbackQuery, state: FSMContext):
    tpl_id = int(callback.data.split(":")[2])
    async with SessionLocal() as session:
        accounts = await get_accounts_for_user(session, callback.from_user.id)
        if not accounts:
            await callback.message.edit_text("ابتدا یک پیج متصل کنید.")
            await callback.answer()
            return
        templates = await list_templates(session, accounts[0].id)
    if tpl_id < 0:
        # built-in starter (we stored them with negative ids)
        template = templates[-tpl_id - 1] if -tpl_id - 1 < len(templates) else None
    else:
        template = next((t for t in templates if t["id"] == tpl_id), None)
    if template is None:
        await callback.message.edit_text("الگو یافت نشد.")
        await callback.answer()
        return

    await state.set_state(TextWait.waiting)
    await state.update_data(topic_target="template_title", template_caption=template["caption"])
    await callback.message.edit_text(
        f"🧩 الگو: <b>{template['title']}</b>\n\n"
        "یک عنوان کوتاه بنویسید تا در {title} الگو جایگذاری شود (یا «-» برای خالی):"
    )
    await callback.answer()


# ── PDF report ───────────────────────────────────────────────
@router.message(Command("report"))
async def cmd_report(message: Message):
    async with SessionLocal() as session:
        accounts = await get_accounts_for_user(session, message.from_user.id)
        if not accounts:
            await message.answer("ابتدا یک پیج متصل کنید (/connect).")
            return
        account = accounts[0]
        report = await build_health_report(account)

    text = format_health_report(report)
    pdf = build_health_pdf_bytes(text)
    await message.answer_document(
        BufferedInputFile(pdf, filename=f"gramma_health_{account.username}.pdf"),
        caption="📄 گزارش سلامت پیج (PDF)",
    )
