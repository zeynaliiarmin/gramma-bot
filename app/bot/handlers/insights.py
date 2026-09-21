"""Gramma — insights & security-scan handlers."""

from __future__ import annotations

from datetime import datetime, timezone

from aiogram import F, Router
from aiogram.types import CallbackQuery, Message

from app.bot.handlers.common import account_options, tr
from app.bot.keyboards import insights_menu, main_menu, paginated
from app.core.database import SessionLocal
from app.db.repositories import get_accounts_for_user, get_or_create_user
from app.services.insights import build_health_report
from app.services.insights import format_health_report
from app.services.meta.service import InstagramService

router = Router(name="insights")


@router.callback_query(F.data == "insights:menu")
async def cb_insights_menu(callback: CallbackQuery):
    await callback.message.edit_text("📊 <b>آمار و امنیت</b>", reply_markup=insights_menu())
    await callback.answer()


@router.callback_query(F.data == "insights:dashboard")
async def cb_dashboard(callback: CallbackQuery):
    async with SessionLocal() as session:
        user = await get_or_create_user(
            session,
            callback.from_user.id,
            username=callback.from_user.username,
            full_name=callback.from_user.full_name,
        )
        accounts = await get_accounts_for_user(session, user.id)
        if not accounts:
            await callback.message.edit_text(tr(None, "no_accounts"), reply_markup=main_menu())
            await callback.answer()
            return
        account = accounts[0]

    svc = InstagramService()
    try:
        info = await svc.get_account_info(account)
    except Exception:  # noqa: BLE001
        info = {}

    followers = info.get("followers_count", "—")
    text = (
        "📈 <b>داشبورد پیج شما</b>\n\n"
        f"👤 نام: {info.get('name') or account.name or '—'}\n"
        f"🔖 یوزرنیم: {info.get('username') or account.username}\n"
        f"👥 دنبال‌کننده: {followers}\n"
        f"🖼 تعداد پست: {info.get('media_count', '—')}\n"
        f"🟢 وضعیت اتصال: {account.status}\n"
        f"🕒 آخرین همگام‌سازی: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')} UTC"
    )
    await callback.message.edit_text(text, reply_markup=insights_menu())
    await callback.answer()


@router.callback_query(F.data == "insights:health")
async def cb_health(callback: CallbackQuery):
    async with SessionLocal() as session:
        accounts = await get_accounts_for_user(session, callback.from_user.id)
        if not accounts:
            await callback.message.edit_text(tr(None, "no_accounts"), reply_markup=main_menu())
            await callback.answer()
            return
        account = accounts[0]

    await callback.message.answer("🩺 در حال ساخت گزارش سلامت…")
    report = await build_health_report(account)
    # build_health_report may open its own sessions; it only reads.
    await callback.message.answer(format_health_report(report), reply_markup=insights_menu())
    await callback.answer()


@router.callback_query(F.data == "insights:security")
async def cb_security(callback: CallbackQuery):
    from sqlalchemy import select

    from app.models import ActivityLog

    async with SessionLocal() as session:
        user = await get_or_create_user(
            session,
            callback.from_user.id,
            username=callback.from_user.username,
            full_name=callback.from_user.full_name,
        )
        accounts = await get_accounts_for_user(session, user.id)
        if not accounts:
            await callback.message.edit_text(tr(None, "no_accounts"), reply_markup=main_menu())
            await callback.answer()
            return
        account = accounts[0]
        result = await session.execute(
            select(ActivityLog)
            .where(ActivityLog.user_id == user.id)
            .order_by(ActivityLog.created_at.desc())
            .limit(12)
        )
        logs = result.scalars().all()

    lines = ["🛡 <b>آخرین رویدادهای امنیتی</b>\n"]
    if not logs:
        lines.append("رویدادی ثبت نشده است.")
    for lg in logs:
        icon = {"error": "🔴", "warning": "🟠"}.get(lg.level, "🟢")
        lines.append(f"{icon} {lg.created_at.strftime('%m-%d %H:%M')} — {lg.action}")
        if lg.detail:
            lines.append(f"    {lg.detail[:90]}")
    await callback.message.edit_text("\n".join(lines), reply_markup=insights_menu())
    await callback.answer()
