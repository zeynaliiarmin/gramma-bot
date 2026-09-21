"""Gramma — collab post workflow handler.

Flow:
  1) The owner picks one of their pages and their draft post, then chooses a
     partner page (must be another page connected in the bot).
  2) A CollabRequest row is created (pending) and the partner is notified on
     Telegram with Accept / Decline buttons.
  3) On accept, the post is cloned to the partner's page for joint campaign
     publishing (policy-safe twin publishing, see app/services/collab.py).
"""

from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import select

from app.bot.handlers.common import account_options, log_activity
from app.bot.keyboards import back
from app.core.database import SessionLocal
from app.db.repositories import get_accounts_for_user, get_or_create_user
from app.models import InstagramAccount, Post
from app.utils import jalali
from app.services.collab import (
    CollabRequest,
    CollabStatus,
    accept_collab,
    create_collab_request,
    get_collab_for_user,
    pending_for_user,
)
from app.services.notifications import notify_telegram

router = Router(name="collab")


def _menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🤝 درخواست کلبریشن جدید", callback_data="collab:new")],
            [InlineKeyboardButton(text="📨 درخواست‌های رسیده", callback_data="collab:inbox")],
            [InlineKeyboardButton(text="📋 تاریخچه", callback_data="collab:history")],
            back("home"),
        ]
    )


@router.message(Command("collab"))
async def cmd_collab(message: Message):
    await message.answer("🤝 <b>پست کلبریشن (Collab)</b>", reply_markup=_menu())


@router.callback_query(F.data == "collab:menu")
async def cb_collab_menu(callback: CallbackQuery):
    await callback.message.edit_text("🤝 <b>پست کلبریشن (Collab)</b>", reply_markup=_menu())
    await callback.answer()


@router.callback_query(F.data == "collab:new")
async def cb_collab_new(callback: CallbackQuery):
    async with SessionLocal() as session:
        user = await get_or_create_user(
            session, callback.from_user.id,
            username=callback.from_user.username, full_name=callback.from_user.full_name,
        )
        accounts = await get_accounts_for_user(session, user.id)
        if not accounts:
            await callback.message.edit_text("ابتدا یک پیج متصل کنید (/connect).")
            await callback.answer()
            return

        # Pick MY page first
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text=f"📄 پیج من: {a.username}", callback_data=f"collab:pick_me:{a.id}")]
                for a in accounts
            ] + [back("collab")]
        )
        await callback.message.edit_text("۱) کدام پیج خودتان را می‌خواهید شریک کنید؟", reply_markup=kb)
        await callback.answer()


@router.callback_query(F.data.startswith("collab:pick_me:"))
async def cb_collab_pick_me(callback: CallbackQuery):
    my_account_id = int(callback.data.split(":")[2])
    async with SessionLocal() as session:
        # Partner candidates: every connected page in the bot EXCEPT mine
        result = await session.execute(
            select(InstagramAccount).where(
                InstagramAccount.status == "connected",
                InstagramAccount.id != my_account_id,
            )
        )
        candidates = result.scalars().all()
        if not candidates:
            await callback.message.edit_text(
                "🤷 هنوز پیج دیگری در ربات متصل نیست.\nبرای کلبریشن، مالک پیج دیگر هم باید ربات را نصب کند.",
                reply_markup=_menu(),
            )
            await callback.answer()
            return

        # Ownership gate for my_account_id
        mine = (await session.execute(select(InstagramAccount).where(
            InstagramAccount.id == my_account_id,
            InstagramAccount.owner_id == callback.from_user.id,
        ))).scalars().first()
        if mine is None:
            await callback.message.edit_text("پیج موردنظر متعلق به شما نیست.")
            await callback.answer()
            return

        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text=f"🤝 با پیج: {c.username}", callback_data=f"collab:pick_partner:{my_account_id}:{c.id}")]
                for c in candidates
            ] + [back("collab")]
        )
        await callback.message.edit_text("۲) پیج شریک را انتخاب کنید:", reply_markup=kb)
        await callback.answer()


@router.callback_query(F.data.startswith("collab:pick_partner:"))
async def cb_collab_pick_partner(callback: CallbackQuery):
    _, _, _, me_id, partner_id = callback.data.split(":")
    me_id, partner_id = int(me_id), int(partner_id)

    async with SessionLocal() as session:
        user = await get_or_create_user(
            session, callback.from_user.id,
            username=callback.from_user.username, full_name=callback.from_user.full_name,
        )
        partner = await session.get(InstagramAccount, partner_id)
        draft = (
            await session.execute(
                select(Post).where(
                    Post.account_id == me_id,
                    Post.status == "draft",
                ).order_by(Post.updated_at.desc())
            )
        ).scalars().first()

        if draft is None:
            await callback.message.edit_text(
                "هنوز پیش‌نویس پستی ندارید.\nاول یک پست بسازید (بخش انتشار) و بعد کلبریشن بدهید.",
                reply_markup=_menu(),
            )
            await callback.answer()
            return

        req = await create_collab_request(
            session,
            owner_id=user.id,
            partner_id=partner.owner_id,
            source_account_id=me_id,
            target_account_id=partner_id,
            post_id=draft.id,
            message=f"پیشنهاد کلبریشن از {user.full_name or user.telegram_username}",
        )
        caption = (draft.caption or "بدون کپشن")[:50]
        await log_activity(
            session, account_id=me_id, user_id=user.id, action="collab_request",
            detail=f"to account {partner_id}",
        )
        await session.commit()

        # Notify partner
        await notify_telegram(
            session,
            partner.owner_id,
            f"🤝 <b>درخواست کلبریشن جدید</b>\n"
            f"از: {user.full_name or user.telegram_username}\n"
            f"محتوای پست: «{caption}»\n"
            f"برای تایید، از منوی /collab → درخواست‌های رسیده اقدام کنید.",
        )

    from app.bot.keyboards import main_menu

    await callback.message.edit_text(
        "✅ درخواست کلبریشن برای پیج شریک ارسال شد.\n"
        "به‌محض تایید شدن، پست مشترک کپی و آماده انتشار می‌شود.",
        reply_markup=main_menu(),
    )
    await callback.answer()


# ── Inbox (partner side) ─────────────────────────────────────
@router.callback_query(F.data == "collab:inbox")
async def cb_collab_inbox(callback: CallbackQuery):
    async with SessionLocal() as session:
        pending = await pending_for_user(session, callback.from_user.id)

    if not pending:
        await callback.message.edit_text("📭 درخواست کلبریشن در انتظاری ندارید.", reply_markup=_menu())
        await callback.answer()
        return

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ تایید", callback_data=f"collab:accept:{r.id}"),
                InlineKeyboardButton(text="❌ رد", callback_data=f"collab:decline:{r.id}"),
            ]
            for r in pending
        ] + [back("collab")]
    )
    await callback.message.edit_text(
        f"📨 {len(pending)} درخواست کلبریشن در انتظار تایید شماست:", reply_markup=kb
    )
    await callback.answer()


@router.callback_query(F.data.startswith("collab:accept:"))
async def cb_collab_accept(callback: CallbackQuery):
    req_id = int(callback.data.split(":")[2])
    async with SessionLocal() as session:
        req = await get_collab_for_user(session, req_id, callback.from_user.id)
        if req is None or req.partner_id != callback.from_user.id:
            await callback.message.edit_text("درخواست یافت نشد یا متعلق به شما نیست.")
            await callback.answer()
            return
        err = await accept_collab(session, req)
        if err:
            await callback.message.edit_text(f"خطا: {err}")
            await callback.answer()
            return
        await session.commit()

    await callback.message.edit_text(
        "✅ کلبریشن تایید شد! پست مشترک برای پیج شما کپی شد.\n"
        "از بخش «انتشار» یا تقویم، آن را زمان‌بندی و منتشر کنید.",
        reply_markup=_menu(),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("collab:decline:"))
async def cb_collab_decline(callback: CallbackQuery):
    req_id = int(callback.data.split(":")[2])
    async with SessionLocal() as session:
        req = await get_collab_for_user(session, req_id, callback.from_user.id)
        if req is None:
            await callback.message.edit_text("درخواست یافت نشد.")
            await callback.answer()
            return
        req.status = CollabStatus.declined.value
        from datetime import datetime as _dt, timezone as _tz

        req.responded_at = _dt.now(_tz.utc)
        await session.commit()

    await callback.message.edit_text("درخواست رد شد.", reply_markup=_menu())
    await callback.answer()


@router.callback_query(F.data == "collab:history")
async def cb_collab_history(callback: CallbackQuery):
    async with SessionLocal() as session:
        result = await session.execute(
            select(CollabRequest).where(
                (CollabRequest.owner_id == callback.from_user.id)
                | (CollabRequest.partner_id == callback.from_user.id)
            ).order_by(CollabRequest.created_at.desc()).limit(10)
        )
        rows = result.scalars().all()

    if not rows:
        await callback.message.edit_text("هنوز کلبریشنی نداشته‌اید.", reply_markup=_menu())
        await callback.answer()
        return

    emoji = {"pending": "⏳", "accepted": "✅", "declined": "❌", "published": "🚀", "canceled": "🗑"}
    lines = ["📋 تاریخچه کلبریشن‌ها:\n"]
    for r in rows:
        s = emoji.get(r.status, "•")
        lines.append(f"{s} {r.status} — {jalali.to_jalali_str(r.created_at)} — «{r.message[:30]}»")
    await callback.message.edit_text("\n".join(lines), reply_markup=_menu())
    await callback.answer()
