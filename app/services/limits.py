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

# ─────────────────────────────────────────────────────────────
# Anti-Block + daily reply caps (single source of truth).
#
# Required by production hardening:
#   * at most DAILY_REPLY_LIMIT comment replies per page per Tehran day;
#   * at most HOURLY_REPLY_LIMIT comment replies per page per rolling hour;
#   * human-like pauses between queued actions;
#   * a random 5–15 min rest break every few hours;
#   * rate profiles (slow/medium/fast) drawn per account per day;
#   * warm-up ramp for pages younger than WARMUP_FULL_DAYS days;
#   * smart backoff ladder after Meta errors (5m → 15m → 1h → 24h);
#   * a daily admin report at 23:59 Tehran + an alert when the error
#     rate exceeds ALERT_ERROR_RATE within a day.
# ─────────────────────────────────────────────────────────────

DAILY_REPLY_LIMIT = 1000          # پاسخ کامنت در روز (هر پیج)
HOURLY_REPLY_LIMIT = 100          # سقف ساعتی پاسخ کامنت (علاوه بر سقف روزانه)
REPLY_MIN_DELAY_S = 1.5           # حداقل مکث شبه‌انسانی بین عملیات
REPLY_MAX_DELAY_S = 4.5           # حداکثر مکث شبه‌انسانی بین عملیات
QUEUE_HUMAN_DELAY_MIN_S = 2.0     # مکث بین آیتم‌های صف (کف تصادفی)
QUEUE_HUMAN_DELAY_MAX_S = 5.0     # مکث بین آیتم‌های صف (سقف تصادفی)

# Per-account rate profile averages (randomised each Tehran day):
RATE_PROFILE_SLOW_AVG_S = 5.0     # کند — حدود ۵ ثانیه
RATE_PROFILE_MEDIUM_AVG_S = 3.0   # متوسط — حدود ۳ ثانیه
RATE_PROFILE_FAST_AVG_S = 1.5     # سریع — حدود ۱.۵ ثانیه
RATE_PROFILES = ("slow", "medium", "fast")

REST_BREAK_MIN_S = 5 * 60         # استراحت تصادفی ۵ تا ۱۵ دقیقه
REST_BREAK_MAX_S = 15 * 60
REST_BREAK_EVERY_MIN_ACTIONS = 80  # هر ~۸۰ عملیات یک استراحت

# Warm-up ramp for freshly connected pages (share of DAILY_REPLY_LIMIT):
WARMUP_PHASE_1_DAYS = 3           # روز ۱..۳
WARMUP_PHASE_2_DAYS = 7           # روز ۴..۱۰
WARMUP_PHASE_1_RATIO = 0.10       # ۱۰٪ → ۱۰۰ در روز
WARMUP_PHASE_2_RATIO = 0.50       # ۵۰٪ → ۵۰۰ در روز
WARMUP_FULL_DAYS = 10             # پس از ۱۰ روز → ۱۰۰۰ کامل

# Smart backoff ladder (seconds) after Instagram errors — 5m → 15m → 1h → 24h.
BACKOFF_STEPS_S = (5 * 60, 15 * 60, 60 * 60, 24 * 60 * 60)

# Detection of Instagram error payloads → stop replies + alert admin.
BLOCK_ERROR_CODES = {
    "190": "oauth_exception",      # توکن منقضی/نامعتبر
    "4": "rate_limit",             # محدودیت نرخ (استفاده API)
    "17": "rate_limit",            # محدودیت نرخ (کاربر)
    "368": "temporarily_blocked",  # مسدودی موقت
    "1200": "suspicious_activity", # فعالیت مشکوک
}

# Monitoring / reporting
ALERT_ERROR_RATE = 0.05            # نرخ خطای > ۵٪ → هشدار فوری
DAILY_REPORT_TEHRAN_HOUR = 23
DAILY_REPORT_TEHRAN_MINUTE = 59
QUEUE_PROCESS_INTERVAL_MIN = 5     # صف هر ۵ دقیقه
HEALTH_CHECK_INTERVAL_MIN = 10     # سلامت هر ۱۰ دقیقه
BUSINESS_HOURS_ACTIVE_START_H = 8  # توزیع پاسخ‌ها عمدتاً ۸ تا ۲۳
BUSINESS_HOURS_ACTIVE_END_H = 23
BUSINESS_HOURS_PEAK_START_H = 10   # تمرکز ۱۰ تا ۲۲
BUSINESS_HOURS_PEAK_END_H = 22

MIN_REPLY_VARIANTS = 5             # حداقل ۵ نسخه از پیش‌نوشته‌شده (تنوع پاسخ)


def reply_limit_for_account(account_created_at, *, now_utc=None) -> int:
    """Warm-up ramp: 100 (≤3d) → 500 (≤10d) → 1000 (>10d)."""

    from datetime import datetime, timedelta, timezone

    now = now_utc or datetime.now(timezone.utc)
    if account_created_at is None:
        return DAILY_REPLY_LIMIT
    if account_created_at.tzinfo is None:
        account_created_at = account_created_at.replace(tzinfo=timezone.utc)
    age_days = (now - account_created_at) / timedelta(days=1)
    if age_days <= WARMUP_PHASE_1_DAYS:
        return int(DAILY_REPLY_LIMIT * WARMUP_PHASE_1_RATIO)
    if age_days <= WARMUP_PHASE_1_DAYS + WARMUP_PHASE_2_DAYS:
        return int(DAILY_REPLY_LIMIT * WARMUP_PHASE_2_RATIO)
    return DAILY_REPLY_LIMIT

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
