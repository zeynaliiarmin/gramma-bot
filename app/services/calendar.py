"""Gramma — content calendar (Jalali-first).

Groups a user's scheduled posts by Persian calendar day so both the bot's
"تقویم محتوا" and the Mini-App calendar render a clean Jalali view, with
Gregorian↔Jalali conversions centralized in app/utils/jalali.py.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.utils.jalali import FA_MONTHS, from_jalali, to_jalali, weekday_fa

settings = get_settings()


def fa_weekday(dt: datetime) -> str:
    """Back-compat alias → Persian day-of-week name (شنبه، …)."""
    return weekday_fa(dt)


def fa_month_name(jm: int) -> str:
    return FA_MONTHS[jm - 1] if 1 <= jm <= 12 else str(jm)


async def build_calendar(session: AsyncSession, user_id: int, account_ids: list[int]) -> list[dict]:
    """Return scheduled posts grouped by Jalali date (ascending)."""
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
                "at": p.scheduled_at.astimezone(ZoneInfo(settings.timezone)).strftime("%H:%M"),
                "status": p.status,
            }
        )

    out = []
    for key in sorted(buckets):
        jy, jm, jd = key
        out.append(
            {
                "date": f"{jy}-{jm:02d}-{jd:02d}",
                "label": f"{jd} {fa_month_name(jm)} {jy}",
                "weekday": _weekday_of_jalali(key),
                "posts": buckets[key],
            }
        )
    return out


def _weekday_of_jalali(key: tuple[int, int, int]) -> str:
    from app.utils.jalali import weekday_fa

    return weekday_fa(from_jalali(key[0], key[1], key[2]))


def format_calendar(entries: list[dict]) -> str:
    if not entries:
        return "📅 هنوز پستی زمان‌بندی نشده است.\nاز بخش انتشار، پست بسازید و زمان‌بندی کنید."
    lines = ["📅 <b>تقویم محتوا</b> (شمسی)\n"]
    for entry in entries[:14]:
        lines.append(f"▫️ <b>{entry['label']}</b> · {entry['weekday']}")
        for p in entry["posts"]:
            kind_icon = {
                "photo": "🖼", "video": "🎬", "reel": "🛰",
                "carousel": "🌀", "story": "📸",
            }.get(p["kind"], "•")
            lines.append(f"    {kind_icon} {p['at']} — {p['caption'][:32]}")
    return "\n".join(lines)


def build_month_grid(jy: int, jm: int) -> list[dict]:
    """A Jalali month grid (weeks starting Saturday) for the visual calendar.

    Each cell: {day, jdate, is_current_month, weekday}.
    """
    days_in_month = 31 if jm <= 6 else (30 if jm <= 11 else 29)  # Esfand 29/30
    if jm == 12:
        # leap-Jalali check (approx): year % 33 in {1,5,9,13,17,22,26,30}
        if (jy % 33) in {1, 5, 9, 13, 17, 22, 26, 30}:
            days_in_month = 30
    first = from_jalali(jy, jm, 1)
    # Jalali week: Saturday=0 … Friday=6
    weekday_index = (first.weekday() + 1) % 7  # Mon..Sun(0..6) → Sat..Fri(0..6)
    cells = []
    for i in range(weekday_index):
        cells.append({"day": None, "jdate": None, "is_current_month": False, "weekday": None})
    for d in range(1, days_in_month + 1):
        cells.append(
            {
                "day": d,
                "jdate": f"{jy}-{jm:02d}-{d:02d}",
                "is_current_month": True,
                "weekday": (weekday_index + d - 1) % 7,
            }
        )
    while len(cells) % 7:
        cells.append({"day": None, "jdate": None, "is_current_month": False, "weekday": None})
    return cells


def month_meta(jy: int, jm: int) -> dict:
    """Month header + weekday names (Sat-first) + a small occasion hint."""
    prev = (jy, jm - 1) if jm > 1 else (jy - 1, 12)
    nxt = (jy, jm + 1) if jm < 12 else (jy + 1, 1)
    occasions = _occasions_for_month(jm)
    return {
        "year": jy,
        "month": jm,
        "label": f"{fa_month_name(jm)} {jy}",
        "prev": f"{prev[0]}-{prev[1]:02d}",
        "next": f"{nxt[0]}-{nxt[1]:02d}",
        "weeknames": ["ش", "ی", "د", "س", "چ", "پ", "ج"],
        "occasions": occasions,
    }


# ── Iranian occasions (for scheduling suggestions) ────────────
_OCCASIONS = [
    (1, 1, "🎉 نوروز"),
    (1, 2, "🌱 روز طبیعت سیزده‌به‌در"),
    (1, 12, "🏛 روز جمهوری اسلامی"),
    (1, 13, "🌿 سیزده‌به‌در"),
    (6, 31, "🍉 شب یلدا (آستانه)"),
    (7, 1, "🎒 بازگشایی مدارس"),
    (7, 8, "📚 روز بزرگداشت مولوی"),
    (9, 1, "🎄 آستانه کریسمس ارامنه"),
    (9, 30, "❄️ شب یلدا"),
    (10, 25, "🌙 (ماه رمضان may vary — تقویم قمری)"),
    (11, 22, "🔥 جشن سده"),
    (12, 29, "🎊 روز ملی‌شدن نفت"),
]


def _occasions_for_month(jm: int) -> list[str]:
    return [f"{d} {fa_month_name(m)} — {label}" for (m, d, label) in _OCCASIONS if m == jm]
