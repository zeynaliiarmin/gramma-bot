"""Gramma — navigation + settings + security handlers."""

from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message
from aiogram.fsm.context import FSMContext

from app.bot.handlers.common import account_options, log_activity, tr
from app.bot.keyboards import (
    community_menu,
    direct_menu,
    full_menu,
    insights_menu,
    main_menu,
    publish_menu,
    settings_menu,
)
from app.core.config import get_settings
from app.core.database import SessionLocal
from app.db.repositories import (
    get_accounts_for_user,
    get_active_account,
    get_or_create_user,
    set_active_account,
)
from app.models import InstagramAccount
from app.services.meta.service import InstagramService

settings = get_settings()
router = Router(name="navigation")


def _menu_by_target(target: str):
    return {
        "publish": publish_menu,
        "community": community_menu,
        "direct": direct_menu,
        "insights": insights_menu,
        "settings": lambda: settings_menu(None),
    }.get(target, main_menu)


@router.callback_query(F.data.startswith("nav:"))
async def cb_nav(callback: CallbackQuery):
    target = callback.data.split(":", 1)[1]
    kb = _menu_by_target(target)()
    labels = {
        "home": "🏠 منوی اصلی",
        "publish": "📤 انتشار محتوا",
        "community": "💬 کامیونیتی",
        "direct": "📥 دایرکت",
        "insights": "📊 آمار و امنیت",
        "settings": "🛠 تنظیمات",
    }
    await callback.message.edit_text(
        labels.get(target, "🏠 منوی اصلی"), reply_markup=kb
    )
    await callback.answer()


# ── Persistent «📋 منوی کامل» button ─────────────────────────
@router.message(F.text.in_(["📋 منوی کامل", "منوی کامل"]))
async def msg_full_menu(message: Message):
    """The persistent reply-keyboard button opens the complete inline menu."""
    await message.answer(
        "📋 منوی کامل — همه امکانات ربات:",
        reply_markup=full_menu(),
    )


# ── Section commands ─────────────────────────────────────────
async def _section(message: Message, title: str, kb):
    await message.answer(title, reply_markup=kb)


@router.message(Command("publish"))
async def cmd_publish(message: Message):
    await _section(message, "📤 انتشار محتوا", publish_menu())


@router.message(Command("community"))
async def cmd_community(message: Message):
    await _section(message, "💬 کامیونیتی", community_menu())


@router.message(Command("direct"))
async def cmd_direct(message: Message):
    await _section(message, "📥 دایرکت", direct_menu())


@router.message(Command("insights"))
async def cmd_insights(message: Message):
    await _section(message, "📊 آمار و امنیت", insights_menu())


@router.message(Command("settings"))
async def cmd_settings(message: Message):
    async with SessionLocal() as session:
        user = await get_or_create_user(
            session,
            message.from_user.id,
            username=message.from_user.username,
            full_name=message.from_user.full_name,
            locale=message.from_user.language_code,
        )
        accounts = await get_accounts_for_user(session, user.id)
        await message.answer("🛠 تنظیمات", reply_markup=settings_menu(accounts[0] if accounts else None))


# ── Settings section ─────────────────────────────────────────
@router.callback_query(F.data == "settings:menu")
async def cb_settings_menu(callback: CallbackQuery):
    async with SessionLocal() as session:
        user = await get_or_create_user(
            session,
            callback.from_user.id,
            username=callback.from_user.username,
            full_name=callback.from_user.full_name,
            locale=callback.from_user.language_code,
        )
        accounts = await get_accounts_for_user(session, user.id)
        active = await get_active_account(session, user.id)

        from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

        rows = []
        for a in accounts:
            mark = " ✅" if active and a.id == active.id else ""
            rows.append([
                InlineKeyboardButton(
                    text=f"📄 {a.username}{mark}",
                    callback_data=f"settings:active:{a.id}",
                )
            ])
        rows.append([InlineKeyboardButton(text="🔗 اتصال پیج جدید", callback_data="connect:start")])
        rows.append([InlineKeyboardButton(text="🔌 قطع اتصال پیج فعال", callback_data="connect:disconnect")])
        rows.append([InlineKeyboardButton(text="🔐 بررسی امنیت", callback_data="security:check")])
        rows.append([InlineKeyboardButton(text="◀️ بازگشت", callback_data="nav:home")])

        kb = InlineKeyboardMarkup(inline_keyboard=rows)
        text = (
            "🛠 <b>تنظیمات</b>\n\n"
            f"👤 پیج‌های شما: {len(accounts)} از {settings.max_accounts_per_user}\n"
            f"🎯 پیج فعال: {active.username if active else '—'}\n\n"
            "برای تغییر پیج فعال، روی آن بزنید."
        )
        await callback.message.edit_text(text, reply_markup=kb)
        await callback.answer()


@router.callback_query(F.data.startswith("settings:active:"))
async def cb_settings_active(callback: CallbackQuery):
    account_id = int(callback.data.split(":")[2])
    async with SessionLocal() as session:
        user = await get_or_create_user(
            session,
            callback.from_user.id,
            username=callback.from_user.username,
            full_name=callback.from_user.full_name,
        )
        ok = await set_active_account(session, user.id, account_id)
        await session.commit()
    if ok:
        await callback.answer("✅ پیج فعال تغییر کرد.")
    else:
        await callback.answer("⚠️ پیج متعلق به شما نیست.")
    await cb_settings_menu(callback)


@router.callback_query(F.data == "settings:lang")
async def cb_settings_lang(callback: CallbackQuery):
    await callback.message.edit_text(
        "🌐 زبان فعلی: فارسی (fa)\n"
        "برای تغییر زبان، کافی است زبان تلگرام یا تنظیمات را تغییر دهید (نسخه آینده)."
    )
    await callback.answer()


@router.callback_query(F.data == "connect:disconnect")
@router.message(Command("disconnect"))
async def cmd_disconnect(obj):
    """Handles both a /disconnect command and the settings button."""
    if isinstance(obj, CallbackQuery):
        message = obj.message
        tg_id = obj.from_user.id
        edit = True
    else:
        message = obj
        tg_id = obj.from_user.id
        edit = False

    async with SessionLocal() as session:
        accounts = await get_accounts_for_user(session, tg_id)
        if not accounts:
            text = "هیچ پیجی برای قطع اتصال وجود ندارد."
            await message.answer(text) if not edit else await message.edit_text(text)
            return
        for acc in accounts:
            acc.status = "disconnected"
            acc.long_lived_token_enc = None  # purge the token
            acc.token_expires_at = None
            await log_activity(
                session, account_id=acc.id, user_id=tg_id,
                action="disconnect", detail="user disconnected page",
            )
        await session.commit()
        text = "🔌 اتصال پیج(ها) قطع و توکن‌ها از دیتابیس پاک شدند."
        if edit:
            await message.edit_text(text, reply_markup=main_menu())
        else:
            await message.answer(text, reply_markup=main_menu())


# ── Security check ───────────────────────────────────────────
@router.callback_query(F.data == "security:check")
async def cb_security_check(callback: CallbackQuery):
    async with SessionLocal() as session:
        accounts = await get_accounts_for_user(session, callback.from_user.id)
        if not accounts:
            await callback.message.edit_text(tr(None, "no_accounts"))
            await callback.answer()
            return
        acc = accounts[0]
        svc = InstagramService()
        try:
            info = await svc.get_account_info(acc)
        except Exception as exc:  # noqa: BLE001
            info = {"error": str(exc)}
        token_state = "🔒 رمزنگاری‌شده" if acc.long_lived_token_enc else "⚠️ بدون توکن"
        lines = [
            "🔐 <b>گزارش امنیتی</b>",
            f"👤 پیج: {info.get('username', acc.username)}",
            f"🔑 وضعیت توکن: {token_state}",
            f"📅 انقضا: {acc.token_expires_at.isoformat()[:10] if acc.token_expires_at else 'نامشخص'}",
            f"🟢 وضعیت اتصال: {acc.status}",
            "",
            "اطلاعات حساس فقط به صورت رمزنگاری‌شده ذخیره می‌شوند. ✅",
        ]
        await callback.message.edit_text("\n".join(lines))
        await callback.answer()
