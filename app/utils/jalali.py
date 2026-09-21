"""Gramma — Jalali (Persian/Solar Hijri) date utilities.

Central place for every Gregorian↔Jalali conversion in the project. The DB
keeps Gregorian `datetime` (timezone-aware) for compatibility; **display and
user input are always Jalali**.

The pure-Python 33-year arithmetic conversion is dependency-free and matches
the jdatetime/Intl.NumberFormat('fa-IR') results.
"""

from __future__ import annotations

import re
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from app.core.config import get_settings

settings = get_settings()

# ── Persian month/day names ───────────────────────────────────
FA_MONTHS = [
    "فروردین", "اردیبهشت", "خرداد", "تیر", "مرداد", "شهریور",
    "مهر", "آبان", "آذر", "دی", "بهمن", "اسفند",
]
_FA_WEEKDAYS = ["شنبه", "یکشنبه", "دوشنبه", "سه‌شنبه", "چهارشنبه", "پنجشنبه", "جمعه"]

# The user requires LATIN (English) digits everywhere in the bot & Mini-App,
# so dates render as `1405/06/30 14:30` — month/weekday names stay Persian.


def _local(dt: datetime) -> datetime:
    """Normalize to the configured local timezone (Asia/Tehran)."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ZoneInfo(settings.timezone))
    return dt.astimezone(ZoneInfo(settings.timezone))


def to_jalali(dt: datetime) -> tuple[int, int, int]:
    """Convert a datetime to (jyear, jmonth, jday) — standard 33-year algo."""
    d = _local(dt)
    gy, gm, gd = d.year, d.month, d.day
    g_d_m = [0, 31, 59, 90, 120, 151, 181, 212, 243, 273, 304, 334]
    gy2 = gy + 1 if gm > 2 else gy
    days = (
        355666 + 365 * gy + (gy2 + 3) // 4 - (gy2 + 99) // 100
        + (gy2 + 399) // 400 + gd + g_d_m[gm - 1]
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


def from_jalali(jy: int, jm: int, jd: int, hour: int = 0, minute: int = 0) -> datetime:
    """Convert a Jalali date (+ optional time) to a timezone-aware datetime."""
    gy, gm, gd = _jalali_to_gregorian(jy, jm, jd)
    return datetime(gy, gm, gd, hour, minute, tzinfo=ZoneInfo(settings.timezone)).astimezone(
        ZoneInfo(settings.timezone)
    )


def _jalali_to_gregorian(jy: int, jm: int, jd: int) -> tuple[int, int, int]:
    """Inverse of to_jalali (canonical jdf algorithm)."""
    jy += 1595
    days = -355668 + 365 * jy + (jy // 33) * 8 + ((jy % 33 + 3) // 4) + jd
    if jm < 7:
        days += (jm - 1) * 31
    else:
        days += (jm - 7) * 30 + 186

    gy = 400 * (days // 146097)
    days %= 146097
    if days > 36524:
        days -= 1
        gy += 100 * (days // 36524)
        days %= 36524
        if days >= 365:
            days += 1
    gy += 4 * (days // 1461)
    days %= 1461
    if days > 365:
        gy += (days - 1) // 365
        days = (days - 1) % 365

    gd = days + 1
    # Month lengths indexed by month number (0..12): index 2 = February.
    feb = 29 if (gy % 4 == 0 and gy % 100 != 0) or (gy % 400 == 0) else 28
    sal_a = [0, 31, feb, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]
    gm = 0
    while gm < 13 and gd > sal_a[gm]:
        gd -= sal_a[gm]
        gm += 1
    return gy, gm, gd


def to_jalali_str(dt: datetime, with_time: bool = True, seconds: bool = False) -> str:
    """Format as `1405/06/30 14:30` (Latin digits; date-only / seconds variants)."""
    jy, jm, jd = to_jalali(dt)
    d = _local(dt)
    out = f"{jy}/{jm:02d}/{jd:02d}"
    if with_time:
        pattern = "%H:%M:%S" if seconds else "%H:%M"
        out += " " + d.strftime(pattern)
    return out


def jalali_date_str(dt: datetime) -> str:
    """Jalali date only, Latin digits: `1405/06/30`."""
    return to_jalali_str(dt, with_time=False)


def format_jalali_date(dt: datetime, fmt: str = "full") -> str:
    """fmt: 'full' → 1405/06/30 14:30؛ 'date' → 1405/06/30؛ 'monthday' → 30 شهریور."""
    jy, jm, jd = to_jalali(dt)
    if fmt == "date":
        return f"{jy}/{jm:02d}/{jd:02d}"
    if fmt == "monthday":
        return f"{jd} {FA_MONTHS[jm - 1]}"
    if fmt == "label":
        return f"{jd} {FA_MONTHS[jm - 1]} {jy}"
    return to_jalali_str(dt)


def fa_month_name(jm: int) -> str:
    return FA_MONTHS[jm - 1] if 1 <= jm <= 12 else str(jm)


def weekday_fa(dt: datetime) -> str:
    """Persian day-of-week name. Jalali weeks start on Saturday."""
    wd = _local(dt).weekday()  # Mon=0
    py = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"][wd]
    index = {"sat": 0, "sun": 1, "mon": 2, "tue": 3, "wed": 4, "thu": 5, "fri": 6}[py]
    return _FA_WEEKDAYS[index]


def parse_jalali_input(text: str, now: datetime | None = None) -> datetime | None:
    """Parse user text (Persian or English digits) into a local datetime.

    Accepts, among others:
      * `۱۴۰۵/۰۶/۳۰ ۱۴:۳۰`  (Jalali date + time)
      * `1405/06/30T14:30`
      * `1405/06/30`        (defaults to 09:00)
      * `⏱۳۰ شهریور ۱۴:۳۰` / `30 شهریور`
      * `1405-06-30 14:30`
    Returns a timezone-aware datetime (Asia/Tehran). None when unparseable.
    """
    s = _en(text.replace("،", " ")).strip()
    if not s:
        return None
    base = now or datetime.now(ZoneInfo(settings.timezone))

    # "روز ماه [سال] [ساعت]"
    m = re.search(
        r"(\d{1,2})\s*(فروردین|اردیبهشت|خرداد|تیر|مرداد|شهریور|مهر|آبان|آذر|دی|بهمن|اسفند)"
        r"(?:\s+(\d{4}))?(?:\s+(\d{1,2})[.:](\d{2}))?",
        s,
    )
    if m:
        jd = int(m.group(1))
        jm = FA_MONTHS.index(m.group(2)) + 1
        jy = int(m.group(3)) if m.group(3) else to_jalali(base)[0]
        hh = int(m.group(4)) if m.group(4) else 9
        mm = int(m.group(5)) if m.group(5) else 0
        if _valid(jy, jm, jd, hh, mm):
            return from_jalali(jy, jm, jd, hh, mm)

    # numeric 1405/06/30 [14:30]
    m = re.search(r"(\d{4})[/-](\d{1,2})[/-](\d{1,2})(?:[T\s]+(\d{1,2})[.:](\d{2}))?", s)
    if m:
        jy, jm, jd = int(m.group(1)), int(m.group(2)), int(m.group(3))
        hh = int(m.group(4)) if m.group(4) else 9
        mm = int(m.group(5)) if m.group(5) else 0
        if _valid(jy, jm, jd, hh, mm):
            return from_jalali(jy, jm, jd, hh, mm)
    return None


def _valid(jy: int, jm: int, jd: int, hh: int, mm: int) -> bool:
    max_day = 31 if jm <= 6 else 30
    return (
        1300 <= jy <= 1500
        and 1 <= jm <= 12
        and 1 <= jd <= max_day
        and 0 <= hh <= 23
        and 0 <= mm <= 59
    )


def _en(s: str) -> str:
    return s.translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹", "0123456789"))


def _fa(value: object, pad: int = 0) -> str:
    """Compatibility alias — returns LATIN digits (user's requirement)."""
    s = str(value)
    if pad:
        s = s.zfill(pad)
    return s


def current_jalali_context() -> dict:
    """Jalali snapshot for AI prompts (OpenClaw) and reports."""
    now = datetime.now(ZoneInfo(settings.timezone))
    jy, jm, jd = to_jalali(now)
    return {
        "current_date_jalali": f"{jy:04d}/{jm:02d}/{jd:02d}",
        "current_date_jalali_fa": f"{jd} {FA_MONTHS[jm - 1]} {jy}",
        "current_time": now.strftime("%H:%M"),
        "day_of_week": weekday_fa(now),
        "timezone": settings.timezone,
        "user_timezone": settings.timezone,
        "current_datetime_jalali": to_jalali_str(now),
    }


def parse_jalali_datetime_or_now(text: str) -> datetime:
    """Parse user input; fall back to now+1h when unparseable (smart UI)."""
    return parse_jalali_input(text) or (
        datetime.now(ZoneInfo(settings.timezone)) + timedelta(hours=1)
    )
