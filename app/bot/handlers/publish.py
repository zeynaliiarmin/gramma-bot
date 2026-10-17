"""Legacy Story dialog and historical scheduling callbacks.

Private admin Post/Reel/carousel media now uses the serverless-durable
telegram_drafts router, not this in-memory FSM or a direct queue insert.
All real publication remains delegated to the existing Zernio worker.
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
from app.services.media_storage import save_downloaded_media, save_downloaded_media_serverless

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
    await callback.message.edit_text("📤 <b>انتشار محتوا</b>", reply_markup=publish_menu())
    await callback.answer()


# Durable Story/album reception now lives in telegram_stories (earlier router).
# No path here starts an in-memory Story FSM or queues Story media.


@router.message(MediaWait.waiting, F.content_type.in_({ContentType.PHOTO, ContentType.VIDEO, ContentType.DOCUMENT, ContentType.VIDEO_NOTE}))
async def media_received(message: Message, state: FSMContext):
    data = await state.get_data()
    kind = data.get("publish_kind", "photo")
    if kind == "story":
        # A pre-deploy in-memory dialog must not resurrect the obsolete Story
        # path. The private database-backed Story FSM is the only Story entry.
        await message.answer("این حالت قدیمی استوری منقضی شده است؛ از دکمهٔ «📸 استوری» دوباره شروع کنید.")
        return
    caption = (message.caption or "").strip()
    is_video = message.content_type in (ContentType.VIDEO, ContentType.VIDEO_NOTE)

    # Zernio needs a public URL: serverless uploads to Supabase Storage,
    # while local/Docker deployments may use a configured S3-compatible CDN.
    if settings.run_mode == "webhook" and not settings.s3_bucket:
        path = await save_downloaded_media_serverless(message.bot, message, is_video=is_video)
    else:
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
    """Used by the AI-caption flow to open the publish menu."""
    await callback.message.edit_text(
        "رسانه را بفرستید:", reply_markup=publish_menu()
    )
    await callback.answer()
