"""Gramma — scheduling handlers + scheduled posts list.

Scheduling paths:
  * "publish now" → set `scheduled_at = now` so the next sweep publishes it;
  * "schedule for later" → ask for `YYYY-MM-DD HH:MM` in local tz.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from app.bot.handlers.common import parse_local_datetime, tr
from app.bot.keyboards import main_menu, paginated, schedule_kind_pick
from app.bot.keyboards_time import time_picker
from app.bot.states import TimeWait
from app.core.config import get_settings
from app.core.database import SessionLocal
from app.db.repositories import get_accounts_for_user, get_or_create_user
from app.models import Media, Post

settings = get_settings()
router = Router(name="schedule")


@router.callback_query(F.data == "schedule:now")
async def cb_schedule_now(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    post_id = data.get("pending_post_id")
    if not post_id:
        await callback.message.edit_text(tr(None, "something_wrong"), reply_markup=main_menu())
        await callback.answer()
        return
    async with SessionLocal() as session:
        post = await session.get(Post, post_id)
        if post is None:
            await callback.message.edit_text(tr(None, "something_wrong"))
            await callback.answer()
            return
        # Ownership re-check: post must belong to this user's account.
        from app.models import InstagramAccount

        acc = await session.get(InstagramAccount, post.account_id)
        if acc is None or acc.owner_id != callback.from_user.id:
            await callback.message.edit_text(tr(None, "account_not_found"))
            await callback.answer()
            return

        post.status = "scheduled"
        post.scheduled_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        await session.commit()

    await state.clear()
    await callback.message.edit_text(
        "🚀 در صف انتشار قرار گرفت. به‌محض انتشار، خبرش را می‌فرستم ✅"
    )
    await callback.answer()


@router.callback_query(F.data == "schedule:time")
async def cb_schedule_time(callback: CallbackQuery, state: FSMContext):
    await state.set_state(TimeWait.waiting)
    now_local = datetime.now().strftime("%Y-%m-%d %H:%M")
    await callback.message.edit_text(
        f"🕰 زمان انتشار را وارد کنید (فرمت: YYYY-MM-DD HH:MM)\n"
        f"مثلاً: {now_local}\n"
        f"منطقه زمانی: {settings.timezone}",
        reply_markup=time_picker(),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("schedule:quick:"))
async def cb_schedule_quick(callback: CallbackQuery, state: FSMContext):
    """Quick presets: +1h / +6h / tomorrow 9am."""
    key = callback.data.split(":", 2)[2]
    data = await state.get_data()
    post_id = data.get("pending_post_id")
    now = datetime.now(timezone.utc)
    delta = {"1h": timedelta(hours=1), "6h": timedelta(hours=6), "tomorrow": timedelta(days=1)}[key]
    target = now + delta
    if key == "tomorrow":
        target = target.replace(hour=6, minute=0, second=0, microsecond=0)
    await _finalize_schedule(callback, state, post_id, target)


@router.message(TimeWait.waiting)
async def time_received(message: Message, state: FSMContext):
    dt = parse_local_datetime(message.text or "")
    if dt is None:
        await message.answer(
            "⚠️ قالب نامعتبر است. مثال: 2026-09-21 18:30"
        )
        return
    data = await state.get_data()
    post_id = data.get("pending_post_id")
    await _finalize_schedule(message, state, post_id, dt)


async def _finalize_schedule(update_obj, state: FSMContext, post_id, dt_utc: datetime):
    """Shared finalizer for both callback & text scheduling paths."""
    if dt_utc.tzinfo is None:
        dt_utc = dt_utc.replace(tzinfo=timezone.utc)

    if isinstance(update_obj, CallbackQuery):
        reply = update_obj.message.edit_text
    else:
        reply = update_obj.answer

    if not post_id:
        await reply(tr(None, "something_wrong"))
        return

    async with SessionLocal() as session:
        post = await session.get(Post, post_id)
        if post is None:
            await reply(tr(None, "something_wrong"))
            await state.clear()
            return
        post.status = "scheduled"
        post.scheduled_at = dt_utc
        await session.commit()

    await state.clear()
    await reply(
        tr(None, "scheduled_ok", when=dt_utc.strftime("%Y-%m-%d %H:%M UTC"))
    )
    if isinstance(update_obj, CallbackQuery):
        await update_obj.answer()


@router.callback_query(F.data == "schedule:cancel")
async def cb_schedule_cancel(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    post_id = data.get("pending_post_id") or data.get("carousel_post_id")
    if post_id:
        async with SessionLocal() as session:
            post = await session.get(Post, post_id)
            if post is not None and post.status == "draft":
                post.status = "canceled"
                await session.commit()
    await state.clear()
    await callback.message.edit_text("🗑 لغو شد.", reply_markup=main_menu())
    await callback.answer()


@router.callback_query(F.data == "schedule:list")
async def cb_schedule_list(callback: CallbackQuery):
    await _show_schedule(callback, page=0)


@router.callback_query(F.data.startswith("schedlist:page:"))
async def cb_schedule_page(callback: CallbackQuery):
    page = int(callback.data.split(":")[2])
    await _show_schedule(callback, page=page)


async def _show_schedule(callback: CallbackQuery, page: int):
    from sqlalchemy import select

    async with SessionLocal() as session:
        user = await get_or_create_user(
            session,
            callback.from_user.id,
            username=callback.from_user.username,
            full_name=callback.from_user.full_name,
        )
        accounts = await get_accounts_for_user(session, user.id)
        ids = [a.id for a in accounts]
        if not ids:
            await callback.message.edit_text(tr(None, "no_accounts"), reply_markup=main_menu())
            await callback.answer()
            return
        result = await session.execute(
            select(Post)
            .where(Post.account_id.in_(ids), Post.status.in_(["scheduled", "failed"]))
            .order_by(Post.scheduled_at)
        )
        posts = result.scalars().all()

    items = []
    for p in posts:
        when = p.scheduled_at.strftime("%m-%d %H:%M") if p.scheduled_at else "—"
        mark = "⏳" if p.status == "scheduled" else "❌"
        items.append((f"{mark} {p.kind} — {when} — {p.caption[:24]}", f"schedule:view:{p.id}"))

    if not items:
        await callback.message.edit_text("هیچ پست زمان‌بندی‌شده‌ای ندارید.", reply_markup=main_menu())
        await callback.answer()
        return

    await callback.message.edit_text(
        f"🕰 <b>پست‌های زمان‌بندی‌شده</b> ({len(items)})",
        reply_markup=paginated(items, "schedlist", page=page),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("schedule:view:"))
async def cb_schedule_view(callback: CallbackQuery):
    post_id = int(callback.data.split(":")[2])
    async with SessionLocal() as session:
        post = await session.get(Post, post_id)
        if post is None:
            await callback.message.edit_text(tr(None, "something_wrong"))
            await callback.answer()
            return
        # ownership
        from app.models import InstagramAccount

        acc = await session.get(InstagramAccount, post.account_id)
        if acc.owner_id != callback.from_user.id:
            await callback.message.edit_text(tr(None, "account_not_found"))
            await callback.answer()
            return
        from sqlalchemy import func, select as _select

        media_count = (
            await session.execute(
                _select(func.count()).where(Media.post_id == post.id)
            )
        ).scalar() or 0
        status_icon = {"scheduled": "⏳", "failed": "❌", "published": "✅"}.get(post.status, "•")
        text = (
            f"{status_icon} <b>جزئیات پست</b>\n"
            f"نوع: {post.kind}\n"
            f"کپشن: {post.caption or '—'}\n"
            f"زمان: {post.scheduled_at.strftime('%Y-%m-%d %H:%M UTC') if post.scheduled_at else '—'}\n"
            f"تعداد رسانه: {media_count}\n"
            f"لینک: {post.ig_permalink or '—'}\n"
        )
        if post.error_message:
            text += f"\n⚠️ خطا: {post.error_message[:200]}"
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🗑 حذف", callback_data=f"schedlist:del:{post_id}")],
            [InlineKeyboardButton(text="◀️ بازگشت", callback_data="schedule:list")],
        ]
    )
    await callback.message.edit_text(text, reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data.startswith("schedlist:del:"))
async def cb_schedule_delete(callback: CallbackQuery):
    post_id = int(callback.data.split(":")[2])
    async with SessionLocal() as session:
        post = await session.get(Post, post_id)
        if post is not None:
            from app.models import InstagramAccount

            acc = await session.get(InstagramAccount, post.account_id)
            if acc.owner_id == callback.from_user.id:
                post.status = "canceled"
                await session.commit()
    await _show_schedule(callback, page=0)
    await callback.answer()
