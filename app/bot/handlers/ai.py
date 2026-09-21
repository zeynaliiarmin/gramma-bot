"""Gramma — AI assistant handlers (caption generation + DM labeling)."""

from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message
from aiogram.fsm.context import FSMContext

from app.bot.handlers.common import account_options, tr
from app.bot.keyboards import ai_menu, caption_actions, main_menu
from app.bot.states import TextWait
from app.core.database import SessionLocal
from app.db.repositories import get_accounts_for_user, get_or_create_user
from app.services.ai import generate_caption, classify_dm_async

router = Router(name="ai")


@router.message(Command("ai"))
async def cmd_ai(message: Message):
    await message.answer("🤖 <b>دستیار هوش مصنوعی Gramma</b>", reply_markup=ai_menu())


@router.callback_query(F.data == "ai:menu")
async def cb_ai_menu(callback: CallbackQuery):
    await callback.message.edit_text("🤖 <b>دستیار هوش مصنوعی Gramma</b>", reply_markup=ai_menu())
    await callback.answer()


@router.callback_query(F.data == "ai:pick")
async def cb_ai_pick(callback: CallbackQuery, state: FSMContext):
    await state.set_state(TextWait.waiting)
    await state.update_data(topic_target="caption_fa")
    await callback.message.edit_text(tr(None, "send_topic"))
    await callback.answer()


@router.callback_query(F.data.startswith("ai:caption:"))
async def cb_ai_caption(callback: CallbackQuery, state: FSMContext):
    account_id = int(callback.data.split(":")[2])
    await state.set_state(TextWait.waiting)
    await state.update_data(topic_target="caption_fa", account_id=account_id)
    await callback.message.edit_text(tr(None, "send_topic"))
    await callback.answer()


@router.callback_query(F.data.startswith("ai:caption_en:"))
async def cb_ai_caption_en(callback: CallbackQuery, state: FSMContext):
    account_id = int(callback.data.split(":")[2])
    await state.set_state(TextWait.waiting)
    await state.update_data(topic_target="caption_en", account_id=account_id)
    await callback.message.edit_text(tr("en", "send_topic"))
    await callback.answer()


@router.message(TextWait.waiting)
async def text_entered(message: Message, state: FSMContext):
    data = await state.get_data()
    target = data.get("topic_target", "caption_fa")

    if target in ("caption_fa", "caption_en"):
        language = "fa" if target == "caption_fa" else "en"
        prompt = message.text or ""
        await message.answer("✨ در حال تولید کپشن…")
        caption = await generate_caption(prompt, tone="friendly", language=language)
        await message.answer(caption)
        account_id = data.get("account_id") or await _first_account(message)
        if account_id:
            await message.answer("می‌خواهید با این کپشن پست بسازید؟", reply_markup=caption_actions(account_id))
        else:
            await message.answer("از منوی اصلی ادامه دهید.", reply_markup=main_menu())
    elif target in ("comment_reply", "comment_private_reply"):
        # Send the reply to Instagram (failures surface as a friendly message).
        from app.services.meta.service import InstagramService

        comment_id = data.get("comment_id")
        account = await _account_for(message)
        if account is None:
            await message.answer(tr(None, "no_accounts"))
        else:
            try:
                svc = InstagramService()
                if target == "comment_private_reply":
                    await svc.private_reply(account, comment_id, message.text or "")
                    await message.answer("✉️ پاسخ خصوصی به کامنت ارسال شد ✅")
                else:
                    await svc.reply_comment(account, comment_id, message.text or "")
                    await message.answer("✅ پاسخ به کامنت ارسال شد.")
            except Exception:  # noqa: BLE001
                await message.answer(tr(None, "something_wrong"))
    elif target == "dm_reply":
        from app.services.meta.service import InstagramService

        thread_id = data.get("thread_id")
        account = await _account_for(message)
        if account is None:
            await message.answer(tr(None, "no_accounts"))
        else:
            try:
                await InstagramService().send_dm(account, thread_id, message.text or "")
                await message.answer("✅ پاسخ دایرکت ارسال شد.")
            except Exception:  # noqa: BLE001
                await message.answer(tr(None, "something_wrong"))
    elif target == "search":
        # Semantic search over this tenant's comments + DMs.
        from app.services.search import format_hits, search_history

        async with SessionLocal() as session:
            hits = await search_history(
                session, message.from_user.id, message.text or "", limit=8
            )
        await message.answer(format_hits(hits))
    elif target == "websearch":
        # Web research (Tavily), used for content inspiration.
        from app.services.web_search import format_web_results, web_search

        await message.answer("🔎 در حال جستجوی وب…")
        results = await web_search(message.text or "", max_results=5)
        await message.answer(format_web_results(message.text or "", results))
    elif target == "template_title":
        # Fill a chosen post template.
        from app.services.templates import render_template

        caption = data.get("template_caption", "")
        title = (message.text or "").strip()
        if title == "-":
            title = ""
        await message.answer(render_template(caption, title))
    await state.clear()


async def _account_for(message: Message):
    """First connected account of the user (ownership-scoped)."""
    async with SessionLocal() as session:
        await get_or_create_user(
            session, message.from_user.id,
            username=message.from_user.username, full_name=message.from_user.full_name,
        )
        accounts = await get_accounts_for_user(session, message.from_user.id)
        return accounts[0] if accounts else None


async def _first_account(message: Message) -> int | None:
    async with SessionLocal() as session:
        await get_or_create_user(
            session, message.from_user.id,
            username=message.from_user.username, full_name=message.from_user.full_name,
        )
        accounts = await get_accounts_for_user(session, message.from_user.id)
        return accounts[0].id if accounts else None


@router.callback_query(F.data.startswith("ai:classify:"))
async def cb_ai_classify(callback: CallbackQuery):
    account_id = int(callback.data.split(":")[2])
    await callback.message.edit_text("🤖 در حال دسته‌بندی دایرکت‌ها…")
    # Demo classification over account DMs
    from app.bot.handlers.direct import demo_subjects

    async with SessionLocal() as session:
        accounts = await get_accounts_for_user(session, callback.from_user.id)
        acc = accounts[0] if accounts else None

    rows = []
    for i, subject in enumerate(demo_subjects):
        label = await classify_dm_async(subject, locale="fa")
        emoji = {"spam": "🗑", "needs_reply": "📌", "replied": "✅"}.get(label, "•")
        rows.append(f"{emoji} {subject[:40]} — {label}")
    await callback.message.edit_text("🤖 دسته‌بندی خودکار:\n\n" + "\n".join(rows))
    await callback.answer()
