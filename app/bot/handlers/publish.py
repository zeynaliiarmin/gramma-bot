"""Gramma — publishing handlers (photo / video / reel / carousel / story).

Flow:
  1. user picks content type;
  2. we ask for the media file (photo/video);
  3. caption comes from the file caption or from the AI caption topic;
  4. user picks "publish now" or "schedule";
  5. a `Post` row is created; the background publisher handles it.

In demo mode everything is persisted and "published" through a simulated
service so the full loop is testable end-to-end.
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, ContentType, Message

from app.bot.handlers.common import tr
from app.bot.keyboards import (
    main_menu,
    publish_menu,
    schedule_kind_pick,
)
from app.bot.states import MediaWait
from app.core.config import get_settings
from app.core.database import SessionLocal
from app.db.repositories import (
    get_accounts_for_user,
    get_active_account,
    get_or_create_user,
)
from app.models import Media, Post
from app.services.media_storage import save_downloaded_media

settings = get_settings()
router = Router(name="publish")

KIND_LABEL = {
    "photo": "عکس",
    "video": "ویدیو",
    "reel": "ریلز",
    "carousel": "کاروسل",
    "story": "استوری",
}


@router.callback_query(F.data == "publish:menu")
async def cb_publish_menu(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text("📤 <b>انتشار محتوا</b>", reply_markup=publish_menu())
    await callback.answer()


@router.callback_query(F.data.startswith("publish:new:"))
async def cb_publish_new(callback: CallbackQuery, state: FSMContext):
    kind = callback.data.split(":", 2)[2]
    await state.update_data(publish_kind=kind)

    # Multi-page support: pick the ACTIVE page (switchable in /settings).
    async with SessionLocal() as session:
        account = await get_active_account(session, callback.from_user.id)
        if not account:
            await callback.message.edit_text(tr(None, "require_connect"))
            await callback.answer()
            return
        await state.update_data(publish_account_id=account.id)

    await state.set_state(MediaWait.waiting)
    hint = {
        "photo": "یک عکس بفرستید (کپشن را در توضیح فایل بنویسید).",
        "video": "یک ویدیو بفرستید.",
        "reel": "یک ویدیو عمودی برای ریلز بفرستید.",
        "story": "یک عکس یا ویدیو کوتاه برای استوری بفرستید.",
        "carousel": "عکس‌ها را یکی‌یکی بفرستید؛ بعد از هر عکس «ادامه/پایان» را بزنید.",
    }[kind]
    page_hint = f"🎯 پیج مقصد: <b>{account.username}</b>\n(با /settings می‌توانید پیج فعال را تغییر دهید)"
    await callback.message.edit_text(f"🖼 <b>{KIND_LABEL[kind]}</b>\n{page_hint}\n{hint}")
    await callback.answer()


@router.message(MediaWait.waiting, F.content_type.in_({ContentType.PHOTO, ContentType.VIDEO, ContentType.DOCUMENT, ContentType.VIDEO_NOTE}))
async def media_received(message: Message, state: FSMContext):
    data = await state.get_data()
    kind = data.get("publish_kind", "photo")
    caption = (message.caption or "").strip()
    is_video = message.content_type in (ContentType.VIDEO, ContentType.VIDEO_NOTE)

    # Download + store the media locally (in production: upload to S3 CDN).
    path = await save_downloaded_media(message.bot, message, is_video=is_video)

    async with SessionLocal() as session:
        await get_or_create_user(
            session,
            message.from_user.id,
            username=message.from_user.username,
            full_name=message.from_user.full_name,
        )
        accounts = await get_accounts_for_user(session, message.from_user.id)
        if not accounts:
            await message.answer(tr(None, "no_accounts"), reply_markup=main_menu())
            await state.clear()
            return
        # Target the page chosen at publish:new (or the first one).
        chosen_id = data.get("publish_account_id") or data.get("carousel_account_id")
        account = next((a for a in accounts if a.id == chosen_id), accounts[0])

        # One Post row per published unit; carousels accumulate media rows.
        if kind == "carousel":
            from sqlalchemy import select

            post = (
                await session.execute(
                    select(Post).where(
                        Post.account_id == account.id, Post.status == "draft"
                    )
                )
            ).scalars().first()
            if post is None:
                post = Post(account_id=account.id, kind="carousel", caption=caption, status="draft")
                session.add(post)
                await session.flush()
        else:
            post = Post(account_id=account.id, kind=kind, caption=caption, status="draft")
            session.add(post)
            await session.flush()

        media = Media(
            post_id=post.id,
            url=path,
            kind="video" if is_video else "photo",
            order_index=(await _next_order(session, post.id)),
        )
        session.add(media)
        await session.commit()
        post_id = post.id
        account_id = account.id

    await state.clear()
    await message.answer(tr(None, "local_media_note"))

    if kind == "carousel":
        await message.answer(
            "✅ دریافت شد! عکس بعدی را بفرستید یا دکمه پایان را بزنید.",
            reply_markup=_carousel_kb(post_id),
        )
    else:
        await message.answer(
            f"📝 کپشن فعلی:\n«{caption or '—'}»\n\nانتشار را انتخاب کنید:",
            reply_markup=schedule_kind_pick(),
        )
        await state.update_data(pending_post_id=post_id)


async def _next_order(session, post_id: int) -> int:
    from sqlalchemy import func, select

    r = await session.execute(
        select(func.coalesce(func.max(Media.order_index), 0)).where(Media.post_id == post_id)
    )
    return int(r.scalar() or 0) + 1


def _carousel_kb(post_id: int):
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="➕ عکس بعدی", callback_data=f"publish:carousel_add:{post_id}"),
                InlineKeyboardButton(text="🏁 پایان و انتشار", callback_data=f"publish:carousel_done:{post_id}"),
            ],
            [InlineKeyboardButton(text="🗑 لغو", callback_data="schedule:cancel")],
        ]
    )


@router.callback_query(F.data.startswith("publish:carousel_add:"))
async def cb_carousel_add(callback: CallbackQuery, state: FSMContext):
    post_id = int(callback.data.split(":")[2])
    await state.set_state(MediaWait.waiting)
    await state.update_data(publish_kind="carousel", carousel_post_id=post_id)
    await callback.message.edit_text("عکس بعدی کاروسل را بفرستید.")
    await callback.answer()


@router.callback_query(F.data.startswith("publish:carousel_done:"))
async def cb_carousel_done(callback: CallbackQuery, state: FSMContext):
    post_id = int(callback.data.split(":")[2])
    await state.update_data(pending_post_id=post_id)
    await callback.message.edit_text("انتشار کاروسل:", reply_markup=schedule_kind_pick())
    await callback.answer()


@router.callback_query(F.data == "publish:confirm")
async def cb_publish_confirm(callback: CallbackQuery):
    """Used by the AI-caption flow to go publish immediately."""
    await callback.message.edit_text(
        "رسانه را بفرستید:", reply_markup=publish_menu()
    )
    await callback.answer()
