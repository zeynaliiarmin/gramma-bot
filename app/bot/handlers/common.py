"""Gramma — shared handler utilities (i18n, account resolving, audit logs).

Keeping these in one place guarantees that *every* handler resolves the
acting account through `resolve_account`, which scopes by owner id — the
data-isolation invariant is enforced by construction.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.database import SessionLocal
from app.db.repositories import get_accounts_for_user, get_or_create_user

if TYPE_CHECKING:
    from app.models import InstagramAccount, User

settings = get_settings()

# ── Minimal i18n (fa / en) ───────────────────────────────────
T = {
    "require_connect": {
        "fa": "هنوز پیجی متصل نکرده‌اید.\nاز /connect استفاده کنید یا دکمه زیر را بزنید.",
        "en": "You haven't connected a page yet.\nUse /connect or the button below.",
    },
    "no_accounts": {
        "fa": "هیچ پیجی به این حساب متصل نیست.",
        "en": "No Instagram page is connected.",
    },
    "choose_account": {
        "fa": "کدام پیج؟",
        "en": "Which page?",
    },
    "account_not_found": {
        "fa": "پیج موردنظر یافت نشد یا متعلق به شما نیست.",
        "en": "Page not found or not yours.",
    },
    "something_wrong": {
        "fa": "مشکلی پیش آمد. لطفاً دوباره تلاش کنید.",
        "en": "Something went wrong. Please try again.",
    },
    "send_media": {
        "fa": "رسانه (عکس/ویدیو) را بفرستید؛ کپشن را می‌توانید در توضیح فایل بنویسید.",
        "en": "Send your photo/video; write the caption in the file's caption.",
    },
    "send_topic": {
        "fa": "موضوع کپشن را بنویسید (محصول، خبر، رویداد...):",
        "en": "Write the caption topic (product, news, event...):",
    },
    "send_reply": {
        "fa": "متن پاسخ را بنویسید:",
        "en": "Write your reply text:",
    },
    "scheduled_ok": {
        "fa": "پست برای {when} زمان‌بندی شد ✅",
        "en": "Post scheduled for {when} ✅",
    },
    "local_media_note": {
        "fa": "رسانه ذخیره شد. (در حالت دمو، انتشار شبیه‌سازی می‌شود.)",
        "en": "Media saved. (Demo mode simulates publishing.)",
    },
}


def tr(locale: str | None, key: str, **fmt) -> str:
    lang = "fa" if (locale or "").lower().startswith("fa") else "en"
    text = T.get(key, {}).get(lang, key)
    return text.format(**fmt) if fmt else text


async def resolve_account(
    session: AsyncSession, telegram_id: int, account_id: int
) -> "InstagramAccount | None":
    """Ownership-scoped account lookup (tenant isolation)."""
    from app.models import InstagramAccount

    result = await session.execute(
        select(InstagramAccount).where(
            InstagramAccount.id == account_id,
            InstagramAccount.owner_id == telegram_id,
        )
    )
    return result.scalars().first()


async def account_options(session: AsyncSession, telegram_id: int) -> list[tuple[int, str]]:
    """[(account_id, label)] for this tenant."""
    accounts = await get_accounts_for_user(session, telegram_id)
    return [(a.id, a.username or f"page#{a.id}") for a in accounts]


async def log_activity(
    session: AsyncSession,
    *,
    account_id: int | None,
    user_id: int | None,
    action: str,
    detail: str = "",
    level: str = "info",
) -> None:
    from app.models import ActivityLog

    session.add(
        ActivityLog(
            account_id=account_id,
            user_id=user_id,
            action=action,
            detail=detail[:2000],
            level=level,
        )
    )


def parse_local_datetime(text: str) -> datetime | None:
    """Parse 'YYYY-MM-DD HH:MM' in the configured timezone → UTC dt."""
    text = text.strip().replace("T", " ")
    try:
        naive = datetime.strptime(text, "%Y-%m-%d %H:%M")
    except ValueError:
        return None
    local_tz = ZoneInfo(settings.timezone)
    return naive.replace(tzinfo=local_tz).astimezone()
