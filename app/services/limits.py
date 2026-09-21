"""Gramma — platform & tenant limits (hard constraints).

The product rules for the *development-mode* deployment:

* MAX_TOTAL_USERS: at most 24 Telegram users may register with the bot.
* MAX_TOTAL_INSTAGRAM_ACCOUNTS: at most 24 Instagram pages in total.
* MAX_ACCOUNTS_PER_USER: each user may connect at most 3 pages.

These three caps live in `app.core.config` as environment variables
(`MAX_TOTAL_USERS`, `MAX_TOTAL_INSTAGRAM_ACCOUNTS`, `MAX_ACCOUNTS_PER_USER`)
and are enforced HERE in one place, so both the Telegram bot handlers and
the Mini-App FastAPI server apply identical rules. Because this module is the
single choke point, no code path can accidentally exceed the limits.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.models import InstagramAccount, User

settings = get_settings()

# ── The three hard caps (single source of truth) ─────────────
MAX_TOTAL_USERS = settings.max_total_users                      # 24 کاربر ثبت‌شده
MAX_TOTAL_INSTAGRAM_ACCOUNTS = settings.max_total_instagram_accounts  # 24 پیج کل
MAX_ACCOUNTS_PER_USER = settings.max_accounts_per_user          # 3 پیج به ازای هر کاربر

REASON_USER_LIMIT = "user_limit"
REASON_USER_TOTAL_LIMIT = "user_total_limit"
REASON_TOTAL_LIMIT = "total_limit"

_ACTIVE_ACCOUNT_FILTER = InstagramAccount.status != "disconnected"


@dataclass
class Limits:
    """Snapshot of the three caps + current usage (shown in bot & Mini-App)."""

    max_users: int
    max_instagram_accounts: int
    max_accounts_per_user: int

    total_users: int
    total_accounts: int

    @property
    def as_dict(self) -> dict:
        return {
            "max_total_users": self.max_users,
            "max_total_instagram_accounts": self.max_instagram_accounts,
            "max_accounts_per_user": self.max_accounts_per_user,
            "total_users": self.total_users,
            "total_instagram_accounts": self.total_accounts,
        }


# ── Counters ──────────────────────────────────────────────────
async def count_registered_users(session: AsyncSession) -> int:
    """Total number of registered Telegram users."""
    result = await session.execute(select(func.count()).select_from(User))
    return int(result.scalar() or 0)


async def count_accounts_for_user(session: AsyncSession, user_id: int) -> int:
    """Active (non-disconnected) Instagram pages owned by one user."""
    result = await session.execute(
        select(func.count())
        .select_from(InstagramAccount)
        .where(
            InstagramAccount.owner_id == user_id,
            _ACTIVE_ACCOUNT_FILTER,
        )
    )
    return int(result.scalar() or 0)


async def count_total_accounts(session: AsyncSession) -> int:
    """Active Instagram pages across the whole bot."""
    result = await session.execute(
        select(func.count()).select_from(InstagramAccount).where(_ACTIVE_ACCOUNT_FILTER)
    )
    return int(result.scalar() or 0)


async def get_limits(session: AsyncSession) -> Limits:
    """Return the caps together with current usage (for /api/me etc.)."""
    return Limits(
        max_users=MAX_TOTAL_USERS,
        max_instagram_accounts=MAX_TOTAL_INSTAGRAM_ACCOUNTS,
        max_accounts_per_user=MAX_ACCOUNTS_PER_USER,
        total_users=await count_registered_users(session),
        total_accounts=await count_total_accounts(session),
    )


# ── Registration gate ─────────────────────────────────────────
async def can_register_user(
    session: AsyncSession, telegram_id: int, *, allow_existing: bool = True
) -> tuple[bool, str | None]:
    """Can this Telegram user register with the bot?

    * allow_existing=True  → existing users always pass (idempotent /start).
    * allow_existing=False → strict check: even existing users blocked once
      MAX_TOTAL_USERS is reached (used by tests).

    Returns (allowed, reason|None).
    """
    existing = await session.get(User, telegram_id)
    if existing is not None and allow_existing:
        return True, None

    if await count_registered_users(session) >= MAX_TOTAL_USERS:
        return False, REASON_USER_TOTAL_LIMIT
    return True, None


# ── Account-connect gate ──────────────────────────────────────
async def can_connect_account(session: AsyncSession, user_id: int) -> tuple[bool, str | None]:
    """Return (allowed, reason) for creating one more page for a user.

    Enforcement order:
      1) per-user cap (hard: MAX_ACCOUNTS_PER_USER = 3)
      2) global Instagram-account cap (hard: MAX_TOTAL_INSTAGRAM_ACCOUNTS = 24)
    """
    user_count = await count_accounts_for_user(session, user_id)
    if user_count >= MAX_ACCOUNTS_PER_USER:
        return False, REASON_USER_LIMIT

    total = await count_total_accounts(session)
    if total >= MAX_TOTAL_INSTAGRAM_ACCOUNTS:
        return False, REASON_TOTAL_LIMIT

    # (Belt-and-braces) a brand-new user must also fit under the user cap.
    if await session.get(User, user_id) is None:
        allowed, reason = await can_register_user(session, user_id)
        if not allowed:
            return False, reason

    return True, None
