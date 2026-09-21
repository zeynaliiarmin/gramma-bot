"""Gramma — platform & tenant limits (hard constraints).

The product rules for the *development-mode* deployment:

* Each Telegram user may connect **at most 3** Instagram pages.
* The whole bot may manage **up to 5** pages simultaneously (dev mode).

These are enforced here in one place so both the bot handlers and the
Mini-App API apply identical rules. Because this is the single choke point,
no code path can accidentally exceed the limits.
"""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.models import InstagramAccount

settings = get_settings()

MAX_ACCOUNTS_PER_USER = settings.max_accounts_per_user      # hard: هر کاربر ۳ پیج
MAX_TOTAL_ACCOUNTS_DEV = settings.max_total_accounts_dev    # hard: ۲۴ پیج کل ربات (dev)

REASON_USER_LIMIT = "user_limit"
REASON_TOTAL_LIMIT = "total_limit"


async def count_accounts_for_user(session: AsyncSession, user_id: int) -> int:
    result = await session.execute(
        select(func.count())
        .select_from(InstagramAccount)
        .where(
            InstagramAccount.owner_id == user_id,
            InstagramAccount.status != "disconnected",
        )
    )
    return int(result.scalar() or 0)


async def count_total_accounts(session: AsyncSession) -> int:
    result = await session.execute(
        select(func.count())
        .select_from(InstagramAccount)
        .where(InstagramAccount.status != "disconnected")
    )
    return int(result.scalar() or 0)


async def can_connect_account(session: AsyncSession, user_id: int) -> tuple[bool, str | None]:
    """Return (allowed, reason) for creating one more page for a user.

    Enforcement order:
      1) per-user cap (hard: 3)
      2) global dev cap (hard: 5 pages in development mode)
    """
    user_count = await count_accounts_for_user(session, user_id)
    if user_count >= MAX_ACCOUNTS_PER_USER:
        return False, REASON_USER_LIMIT

    total = await count_total_accounts(session)
    if total >= settings.max_total_accounts_dev:
        return False, REASON_TOTAL_LIMIT

    return True, None
