"""Gramma — /start, /help, /connect + the OAuth entry point.

Hard limits (24 users · 24 pages total · 3 pages/user), enforced centrally
in app/services/limits.py, are applied here at /start (user cap) and at
connection time (per-user + total page caps).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    WebAppInfo,
)
from aiogram.fsm.context import FSMContext
from sqlalchemy import select

from app.bot.handlers.common import account_options, log_activity, tr
from app.bot.keyboards import full_menu, main_menu, persistent_menu
from app.core.config import get_settings
from app.core.database import SessionLocal
from app.core.security.oauth_states import oauth_states
from app.db.repositories import get_or_create_user
from app.models import InstagramAccount, OAuthFlow
from app.services.limits import (
    MAX_ACCOUNTS_PER_USER,
    MAX_TOTAL_INSTAGRAM_ACCOUNTS,
    MAX_TOTAL_USERS,
    can_connect_account,
    can_register_user,
    count_accounts_for_user,
)
from app.services.meta.oauth import build_authorization_url
from app.webapp.auth import make_miniapp_ticket

settings = get_settings()

router = Router(name="account")


# ── Callback UX helper ───────────────────────────────────────
async def _ack(callback: CallbackQuery, text: str = "") -> None:
    """Answer the callback query *immediately* so Telegram never shows a
    stuck/loading button; heavy DB/network work happens after the ack and
    updates the message content instead."""
    try:
        await callback.answer(text or None)
    except Exception:  # noqa: BLE001  (already answered / request expired)
        pass

def _error_edit(callback: CallbackQuery, text: str):
    """Show a precise Persian error in the message after an ack."""
    try:
        return callback.message.edit_text(text)
    except Exception:  # noqa: BLE001
        return callback.message.answer(text)


def _welcome_text(name: str, page_count: int) -> str:
    base = (
        f"👋 سلام <b>{name}</b>!\n\n"
        "من <b>Gramma v2</b> هستم؛ پنل مدیریت فوق‌حرفه‌ای پیج‌های اینستاگرام، داخل تلگرام 🌈\n\n"
        "📤 <b>انتشار</b> — عکس، ویدیو، ریلز، کاروسل، استوری + زمان‌بندی\n"
        "📅 <b>تقویم محتوا</b> — نمای ماهانه پست‌های زمان‌بندی‌شده\n"
        "🤝 <b>پست کلبریشن</b> — همکاری مشترک دو پیج متصل\n"
        "💬 <b>کامیونیتی</b> — کامنت‌ها و پاسخ خصوصی\n"
        "📥 <b>دایرکت</b> — پاسخ + دسته‌بندی هوشمند + پاسخ خودکار\n"
        "🔍 <b>جستجوی هوشمند</b> — جستجوی معنایی در کامنت‌ها و دایرکت‌ها\n"
        "📊 <b>آمار و امنیت</b> — گزارش روزانه سلامت پیج\n"
        "🤖 <b>دستیار هوش مصنوعی</b> — تولید کپشن\n"
        "🖥 <b>پنل مدیریت (Mini-App)</b> — داشبورد با نمودار و تقویم\n\n"
        f"🔗 پیج‌های متصل: <b>{page_count} از {MAX_ACCOUNTS_PER_USER}</b>"
    )
    return base


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    async with SessionLocal() as session:
        # Hard cap: at most MAX_TOTAL_USERS registered Telegram users.
        allowed, reason = await can_register_user(session, message.from_user.id)
        if not allowed:
            await session.rollback()
            await message.answer(
                "⛔ ظرفیت کاربران ربات تکمیل شده است.\n"
                f"حداکثر {MAX_TOTAL_USERS} کاربر می‌توانند به ربات وصل شوند.\n"
                "لطفاً بعداً دوباره تلاش کنید."
            )
            return
        await get_or_create_user(
            session,
            message.from_user.id,
            username=message.from_user.username,
            full_name=message.from_user.full_name,
            locale=message.from_user.language_code,
        )
        await session.commit()
        page_count = await count_accounts_for_user(session, message.from_user.id)

    keyboard = full_menu()
    await message.answer(
        _welcome_text(message.from_user.first_name, page_count),
        reply_markup=keyboard,
    )
    # Persistent «📋 منوی کامل» reply button — always visible in the chat.
    await message.answer(
        "👇 همیشه می‌توانید با دکمه «📋 منوی کامل» به منوی اصلی برگردید.",
        reply_markup=persistent_menu(),
    )


@router.message(Command("help"))
async def cmd_help(message: Message):
    text = (
        "🧭 <b>راهنمای Gramma v2</b>\n\n"
        "/start — منوی اصلی\n"
        "/connect — اتصال پیج اینستاگرام (حداکثر 3 پیج)\n"
        "/publish — انتشار محتوا\n"
        "/calendar — تقویم محتوا 📅\n"
        "/collab — پست کلبریشن 🤝\n"
        "/community — مدیریت کامنت‌ها\n"
        "/direct — دایرکت‌ها\n"
        "/search — جستجوی هوشمند 🔍\n"
        "/insights — آمار و امنیت\n"
        "/ai — دستیار هوش مصنوعی\n"
        "/webapp — پنل مدیریت (Mini-App)\n"
        "/settings — تنظیمات و قطع اتصال\n"
        "/disconnect — قطع اتصال پیج\n"
        "/help — همین راهنما\n\n"
        "هر کاربر فقط به پیج(های) <b>خودش</b> دسترسی دارد."
    )
    await message.answer(text)


@router.message(Command("connect"))
async def cmd_connect(message: Message, state: FSMContext):
    await state.clear()
    await _start_connect(message)


@router.callback_query(F.data == "connect:start")
async def cb_connect_start(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    # Ack FIRST, so the button never appears stuck while we hit the DB.
    await _ack(callback)
    await _start_connect(callback.message)


@router.callback_query(F.data.startswith("connect:page:"))
async def cb_connect_page(callback: CallbackQuery):
    """Re-issue the OAuth link for an already-created (pending) page."""
    account_id = int(callback.data.split(":")[2])
    # Ack FIRST — DB + OAuth URL building happens afterwards.
    await _ack(callback)
    async with SessionLocal() as session:
        user = await get_or_create_user(
            session,
            callback.from_user.id,
            username=callback.from_user.username,
            full_name=callback.from_user.full_name,
            locale=callback.from_user.language_code,
        )
        acc = await session.get(InstagramAccount, account_id)
        if acc is None or acc.owner_id != user.id:
            await _error_edit(
                callback,
                "❌ این پیج پیدا نشد یا متعلق به شما نیست.\n"
                "لطفاً از طریق /connect دوباره تلاش کنید.",
            )
            return
        await _build_oauth_message(session, acc, callback.message, user)


async def _start_connect(message: Message):
    """Enforce limits, then create a pending page + produce the OAuth link."""
    async with SessionLocal() as session:
        # ── Hard cap: at most MAX_TOTAL_USERS registered users ──
        allowed_user, u_reason = await can_register_user(session, message.from_user.id)
        if not allowed_user:
            await session.rollback()
            await message.answer(
                "⛔ ظرفیت کاربران ربات تکمیل شده است.\n"
                f"حداکثر {MAX_TOTAL_USERS} کاربر می‌توانند به ربات وصل شوند.\n"
                "لطفاً بعداً دوباره تلاش کنید."
            )
            return

        user = await get_or_create_user(
            session,
            message.from_user.id,
            username=message.from_user.username,
            full_name=message.from_user.full_name,
            locale=message.from_user.language_code,
        )

        # ── Hard tenant limits ───────────────────────────────
        allowed, reason = await can_connect_account(session, user.id)
        if not allowed:
            if reason == "user_limit":
                await message.answer(
                    f"⚠️ شما هم‌اکنون {MAX_ACCOUNTS_PER_USER} پیج متصل دارید "
                    f"(حداکثر مجاز هر کاربر).\n"
                    "برای افزودن پیج جدید، ابتدا یک پیج را قطع کنید (/disconnect)."
                )
            elif reason == "user_total_limit":
                await message.answer(
                    "⛔ ظرفیت کاربران ربات تکمیل شده است.\n"
                    f"حداکثر {MAX_TOTAL_USERS} کاربر می‌توانند به ربات وصل شوند."
                )
            else:
                await message.answer(
                    f"⛔ ظرفیت کل ربات تکمیل شده است "
                    f"({MAX_TOTAL_INSTAGRAM_ACCOUNTS} پیج در حالت توسعه).\n"
                    "به‌زودی ظرفیت افزایش می‌یابد؛ لطفاً بعداً تلاش کنید."
                )
            return

        # Reuse a still-pending draft (so we don't leak rows on re-click)
        existing = (
            await session.execute(
                select(InstagramAccount)
                .where(InstagramAccount.owner_id == user.id, InstagramAccount.status == "pending")
                .order_by(InstagramAccount.created_at)
            )
        ).scalars().all()

        acc = existing[0] if existing else InstagramAccount(
            owner_id=user.id, username="(pending)", name="Page", status="pending"
        )
        if not existing:
            session.add(acc)
        await session.commit()
        await _build_oauth_message(session, acc, message, user)


def _oauth_preflight() -> str | None:
    """Return a precise Persian error for the first mis-configuration, or None."""
    if not settings.meta_app_id:
        return (
            "شناسه اپ متا (META_APP_ID) در سرور تنظیم نشده است. "
            "تا زمانی که ادمین این مقدار را در Vercel ست نکند، اتصال واقعی انجام نمی‌شود."
        )
    if not settings.meta_app_secret:
        return (
            "کلید مخفی اپ متا (META_APP_SECRET) در سرور تنظیم نشده است. "
            "بدون آن، تبادل کد OAuth امکان‌پذیر نیست."
        )
    if not settings.webhook_base_url:
        return (
            "آدرس بازگشت (WEBHOOK_BASE_URL) در سرور تنظیم نشده است. "
            "متا باید بداند کد تأیید را به کدام آدرس برگرداند."
        )
    return None


async def _build_oauth_message(session, acc: InstagramAccount, message: Message, user):
    # ── Priority 1: ChatbotX is the new primary path (bypasses Meta sanctions) ──
    chatbotx_enabled = bool(getattr(settings, "chatbotx_enabled", False) or getattr(settings, "chatbotx_workspace_token", ""))
    chatbotx_url = "https://app.chatbotx.io"
    # If ChatbotX is configured OR Meta is not configured, show ChatbotX guide
    if chatbotx_enabled or (not settings.meta_app_id and not settings.is_simulation):
        # Show ChatbotX connection guide
        text = (
            "🔗 <b>اتصال پیج اینستاگرام از طریق ChatbotX</b>\n\n"
            "✅ پیج <b>@zeynalikids</b> هم‌اکنون از طریق ChatbotX متصل است.\n"
            "ChatbotX از Login with Instagram استفاده می‌کند و نیازی به اپ متا ندارد — تحریم را دور می‌زند.\n\n"
            "📌 برای اتصال پیج جدید:\n"
            "۱. وارد پنل ChatbotX شوید: https://app.chatbotx.io\n"
            "۲. بخش Instagram → Connect Account → Login with Instagram\n"
            "۳. پیج خود را انتخاب و مجوزها را تایید کنید\n"
            "۴. سپس در ربات Gramma دکمه «بررسی اتصال» را بزنید\n\n"
            "💬 قابلیت‌ها از طریق ChatbotX:\n"
            "• پاسخ خودکار کامنت‌ها (1000/روز)\n"
            "• پاسخ دایرکت (نامحدود)\n"
            "• ریپلای استوری\n"
            "• اتصال به AvalAI/DeepSeek برای هوش مصنوعی\n\n"
            "📤 برای انتشار پست/استوری/ریلز از OpenClaw (private-enabled) استفاده می‌شود."
        )
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="🌐 باز کردن پنل ChatbotX", url=chatbotx_url)],
                [InlineKeyboardButton(text="🔍 بررسی اتصال ChatbotX", callback_data=f"chatbotx:check:{acc.id}")],
                [InlineKeyboardButton(text="🧪 اتصال دمو (تست)", callback_data=f"connect:demo:{acc.id}")],
            ]
        )
        await message.answer(text, reply_markup=kb, disable_web_page_preview=True)
        return

    if settings.is_simulation:
        # Dev/demo: instantly link a simulated account (unique name per page).
        await _simulate_connect(session, acc, user, message)
        return

    # Production: Meta OAuth path (if app exists)
    problem = _oauth_preflight()
    if problem:
        # Fallback to ChatbotX guide instead of hard error
        text = (
            "⚠️ <b>اپ متا هنوز ساخته نشده</b> (به دلیل تحریم)\n\n"
            f"• {problem}\n\n"
            "✅ راه‌حل جایگزین: از ChatbotX استفاده کنید — بدون نیاز به اپ متا.\n"
            "پیج @zeynalikids هم‌اکنون از این طریق متصل است."
        )
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="🌐 پنل ChatbotX", url=chatbotx_url)],
                [InlineKeyboardButton(text="🔍 بررسی اتصال", callback_data=f"chatbotx:check:{acc.id}")],
                [InlineKeyboardButton(text="🧪 اتصال دمو", callback_data=f"connect:demo:{acc.id}")],
            ]
        )
        await message.answer(text, reply_markup=kb)
        return

    redirect_uri = f"{settings.webhook_base_url}{settings.webhook_path_prefix}/callback"
    state = oauth_states.create(user.id, acc.id)
    auth_url, verifier = build_authorization_url(redirect_uri, state)

    db_flow = (
        await session.execute(select(OAuthFlow).where(OAuthFlow.account_id == acc.id))
    ).scalars().first()
    if db_flow is None:
        db_flow = OAuthFlow(account_id=acc.id)
        session.add(db_flow)
    db_flow.code_verifier = verifier
    db_flow.expires_at = datetime.now(timezone.utc) + timedelta(minutes=15)
    await session.commit()

    text = (
        "🔐 <b>اتصال امن به اینستاگرام</b>\n\n"
        "۱. روی دکمه زیر بزنید.\n"
        "۲. با اکانتی که Tester اپ متاست وارد شوید.\n"
        "۳. پیج Business/Creator را انتخاب و مجوزها را تایید کنید.\n\n"
        "🔒 لینک یکبارمصرف است و فقط برای شما اعتبار دارد.\n"
        "توکن شما رمزنگاری‌شده (AES-256) ذخیره خواهد شد.\n\n"
        "💡 همچنین می‌توانید از ChatbotX (بدون اپ متا) استفاده کنید."
    )
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="💳 ورود با اینستاگرام (Meta)", url=auth_url)],
            [InlineKeyboardButton(text="🌐 اتصال از طریق ChatbotX", url=chatbotx_url)],
            [InlineKeyboardButton(text="✅ انجام شد", callback_data=f"connect:done:{acc.id}")],
        ]
    )
    await message.answer(text, reply_markup=kb, disable_web_page_preview=True)


async def _simulate_connect(session, acc: InstagramAccount, user, message: Message):
    """Demo mode: mark the pending page connected with a unique username."""
    acc.status = "connected"
    base = (user.telegram_username or f"user{user.id}")
    acc.username = f"{base}_{acc.id}"
    acc.name = f"Demo Page #{acc.id}"
    acc.status = "connected"
    if settings.is_simulation:
        acc.long_lived_token_enc = get_cipher().encrypt(f"sim-token-for-{acc.id}")
        acc.token_expires_at = datetime.now(timezone.utc) + timedelta(days=60)
    await log_activity(
        session, account_id=acc.id, user_id=user.id, action="connect",
        detail="simulated connect (no real Meta app configured)",
    )
    await session.commit()
    page_count = await count_accounts_for_user(session, user.id)
    await message.answer(
        f"✅ در حالت دمو، پیج شبیه‌سازی‌شده متصل شد.\n"
        f"👤 نام پیج: {acc.username}\n"
        f"📊 پیج‌های شما: {page_count} از {MAX_ACCOUNTS_PER_USER}\n\n"
        "برای اتصال واقعی، INSTAGRAM_ACCOUNT_MODE=production را تنظیم کنید.",
        reply_markup=main_menu(miniapp_ticket=make_miniapp_ticket(user.id)),
    )


from app.core.security.crypto import get_cipher  # noqa: E402  (used above)


@router.callback_query(F.data.startswith("connect:demo:"))
async def cb_connect_demo(callback: CallbackQuery):
    account_id = int(callback.data.split(":")[2])
    await _ack(callback, "در حال اتصال دمو...")
    async with SessionLocal() as session:
        user = await get_or_create_user(
            session,
            callback.from_user.id,
            username=callback.from_user.username,
            full_name=callback.from_user.full_name,
            locale=callback.from_user.language_code,
        )
        acc = await session.get(InstagramAccount, account_id)
        if acc is None or acc.owner_id != user.id:
            await _error_edit(callback, "❌ پیج پیدا نشد.")
            return
        await _simulate_connect(session, acc, user, callback.message)


@router.callback_query(F.data.startswith("chatbotx:check:"))
async def cb_chatbotx_check(callback: CallbackQuery):
    account_id = int(callback.data.split(":")[2])
    await _ack(callback, "در حال بررسی اتصال ChatbotX...")
    try:
        from app.services.chatbotx_service import get_chatbotx_client

        client = get_chatbotx_client()
        if not client.enabled:
            await callback.message.edit_text(
                "⚠️ ChatbotX هنوز پیکربندی نشده.\n\n"
                "توکن را در .env و Vercel Env بگذارید:\n"
                "`CHATBOTX_WORKSPACE_TOKEN=YOUR_CHATBOTX_TOKEN_HERE`\n"
                "`CHATBOTX_BASE_URL=https://app.chatbotx.io/api`\n"
                "`CHATBOTX_WORKSPACE_ID=11706290428788736`\n"
                "`CHATBOTX_ENABLED=true`\n\n"
                "سپس دوباره بررسی کنید. پیج @zeynalikids در پنل ChatbotX متصل است."
            )
            return

        status = await client.get_instagram_connection()
        connected = status.get("connected", False)
        username = status.get("username") or "@zeynalikids"
        if connected:
            # Also mark our local account as connected via ChatbotX
            async with SessionLocal() as session:
                acc = await session.get(InstagramAccount, account_id)
                if acc and acc.owner_id == callback.from_user.id:
                    acc.status = "connected"
                    acc.username = username.replace("@", "")
                    acc.name = f"{username} via ChatbotX"
                    await log_activity(
                        session, account_id=acc.id, user_id=callback.from_user.id,
                        action="connect_chatbotx",
                        detail=f"ChatbotX connected {username}",
                    )
                    await session.commit()
            await callback.message.edit_text(
                f"✅ <b>ChatbotX متصل است!</b>\n\n"
                f"📱 پیج: {username}\n"
                f"🔗 Workspace: {settings.chatbotx_workspace_id or '11706290428788736'}\n"
                f"💬 کامنت/دایرکت/استوری: فعال\n"
                f"🤖 AI: AvalAI DeepSeek متصل\n\n"
                f"اکنون می‌توانید از تمام قابلیت‌های ربات استفاده کنید."
            )
        else:
            await callback.message.edit_text(
                "❌ اتصال ChatbotX یافت نشد.\n\n"
                "لطفاً در پنل https://app.chatbotx.io وارد شوید و اینستاگرام را متصل کنید، "
                "سپس دوباره بررسی کنید."
            )
    except Exception as exc:
        await callback.message.edit_text(
            f"⚠️ خطا در بررسی ChatbotX:\n{str(exc)[:400]}\n\n"
            "لطفاً بعداً دوباره تلاش کنید یا به ادمین اطلاع دهید."
        )


@router.callback_query(F.data.startswith("connect:done:"))
async def cb_connect_done(callback: CallbackQuery):
    account_id = int(callback.data.split(":")[2])
    # Ack FIRST — the DB lookup happens after the spinner is dismissed.
    await _ack(callback)
    async with SessionLocal() as session:
        acc = await session.get(InstagramAccount, account_id)
        if acc is None or acc.owner_id != callback.from_user.id:
            await _error_edit(
                callback,
                "❌ این پیج پیدا نشد یا متعلق به شما نیست.\n"
                "لطفاً از طریق /connect دوباره تلاش کنید.",
            )
            return
        if acc.long_lived_token_enc and acc.status == "connected":
            await callback.message.edit_text(
                "✅ اتصال با موفقیت انجام شد!\nپیج شما آماده مدیریت است."
            )
        else:
            await callback.message.edit_text(
                "⏳ هنوز تایید دریافت نشده.\n"
                "✅ مسیر مهم است: دکمه «ورود با اینستاگرام» را در مرورگر باز کنید، "
                "پیج Business/Creator را انتخاب و مجوزها را تأیید کنید، سپس دوباره "
                "«انجام شد» را بزنید.\n\n"
                "⚠️ اگر خطایی دیدید: مطمئن شوید حسابتان Tester اپ متا در حالت Development است."
            )


# ── Mini-App entry ───────────────────────────────────────────
@router.callback_query(F.data == "webapp:open")
async def cb_webapp_open(callback: CallbackQuery):
    ticket = make_miniapp_ticket(callback.from_user.id)
    public = settings.miniapp_url
    url = f"{public}/?_token={ticket}"
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🖥 باز کردن پنل مدیریت", web_app=WebAppInfo(url=url))]
        ]
    )
    await callback.message.answer(
        "🖥 پنل مدیریت Gramma باز می‌شود — داشبورد آمار، تقویم محتوا، پیج‌ها و تنظیمات.",
        reply_markup=kb,
    )
    await callback.answer()


@router.message(Command("webapp"))
async def cmd_webapp(message: Message):
    ticket = make_miniapp_ticket(message.from_user.id)
    public = settings.miniapp_url
    url = f"{public}/?_token={ticket}"
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🖥 باز کردن پنل مدیریت", web_app=WebAppInfo(url=url))]
        ]
    )
    await message.answer("🖥 پنل مدیریت Gramma:", reply_markup=kb)
