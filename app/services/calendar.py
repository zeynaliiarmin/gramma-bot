"""Gramma — content calendar.

Groups a user's scheduled posts by Persian calendar day so both the bot's
"تقویم محتوا" and the Mini-App calendar can render a clean month view.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

if TYPE_CHECKING:
    from app.models import InstagramAccount

try:
    from babel.dates import format_date
    from babel.dates import get_day_names as _day_names
    from babel import Locale

    _FA = Locale.parse("fa")
    _HAS_BABEL = True
except Exception:  # pragma: no cover
    _HAS_BABEL = False


def _fa_day_names(width="abbreviated"):
    if _HAS_BABEL:
        try:
            return list(_day_names(width, locale=_FA))
        except Exception:
            pass
    return ["دوشنبه", "سه‌شنبه", "چهارشنبه", "پنجشنبه", "جمعه", "شنبه", "یکشنبه"]


def _fa_month_name(month: int) -> str:
    names = [
        "", "فروردین", "اردیبهشت", "خرداد", "تیر", "مرداد", "شهریور",
        "مهر", "آبان", "آذر", "دی", "بهمن", "اسفند",
    ]
    return names[month] if 1 <= month <= 12 else str(month)


def to_jalali(dt: datetime) -> tuple[int, int, int]:
    """Convert a datetime (UTC) to (jyear, jmonth, jday) using the standard
    33-year arithmetic algorithm (jalali-js compatible, no dependencies)."""
    d = dt.astimezone(timezone.utc)
    gy, gm, gd = d.year, d.month, d.day
    g_d_m = [0, 31, 59, 90, 120, 151, 181, 212, 243, 273, 304, 334]
    gy2 = gy + 1 if gm > 2 else gy
    days = (
        355666
        + 365 * gy
        + (gy2 + 3) // 4
        - (gy2 + 99) // 100
        + (gy2 + 399) // 400
        + gd
        + g_d_m[gm - 1]
    )
    jy = -1595 + 33 * (days // 12053)
    days %= 12053
    jy += 4 * (days // 1461)
    days %= 1461
    if days > 365:
        jy += (days - 1) // 365
        days = (days - 1) % 365
    if days < 186:
        jm = 1 + days // 31
        jd = 1 + days % 31
    else:
        jm = 7 + (days - 186) // 30
        jd = 1 + (days - 186) % 30
    return jy, jm, jd


def fa_weekday(dt: datetime) -> str:
    """Persian weekday name. Python weekday(): Mon=0..Sun=6. Map to FA order."""
    wd = dt.astimezone(timezone.utc).weekday()  # 0=Mon
    # FA calendar list starts at Saturday → index mapping
    fa_index = {"sat": 0, "sun": 1, "mon": 2, "tue": 3, "wed": 4, "thu": 5, "fri": 6}
    py = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"][wd]
    names = ["شنبه", "یکشنبه", "دوشنبه", "سه‌شنبه", "چهارشنبه", "پنجشنبه", "جمعه"]
    return names[fa_index[py]]


async def build_calendar(session: AsyncSession, user_id: int, account_ids: list[int]) -> list[dict]:
    """Return scheduled posts grouped by Persian date (ascending)."""
    from app.models import Post

    result = await session.execute(
        select(Post)
        .where(Post.account_id.in_(account_ids), Post.status.in_(["scheduled", "draft"]))
        .order_by(Post.scheduled_at)
    )
    posts = result.scalars().all()

    buckets: dict[tuple[int, int, int], list[dict]] = {}
    for p in posts:
        if p.scheduled_at is None:
            continue
        key = to_jalali(p.scheduled_at)
        buckets.setdefault(key, []).append(
            {
                "id": p.id,
                "kind": p.kind,
                "caption": (p.caption or "")[:60],
                "at": p.scheduled_at.strftime("%H:%M"),
                "status": p.status,
            }
        )

    out = []
    for key in sorted(buckets):
        jy, jm, jd = key
        label = f"{(jd)} {_fa_month_name(jm)} {jy}"
        items = buckets[key]
        out.append({"date": f"{jy}-{jm:02d}-{jd:02d}", "label": label, "posts": items})
    return out


def format_calendar(entries: list[dict]) -> str:
    if not entries:
        return "📅 هنوز پستی زمان‌بندی نشده است.\nاز بخش انتشار، پست بسازید و زمان‌بندی کنید."
    lines = ["📅 <b>تقویم محتوا</b>\n"]
    for entry in entries[:14]:
        lines.append(f"▫️ <b>{entry['label']}</b>")
        for p in entry["posts"]:
            kind_icon = {"photo": "🖼", "video": "🎬", "reel": "🛰", "carousel": "🌀", "story": "📸"}.get(p["kind"], "•")
            lines.append(f"    {kind_icon} {p['at']} — {p['caption'][:32]}")
    return "\n".join(lines)
