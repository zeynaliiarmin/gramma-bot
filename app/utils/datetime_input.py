"""Gramma — natural-language schedule parsing (Persian).

Understands inputs like «فردا صبح», «شنبه ساعت ۱۰», «۲ ساعت بعد», «الان»
and delegates exact Jalali dates to app.utils.jalali.parse_jalali_input.
Always returns a timezone-aware datetime (Asia/Tehran).
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from app.core.config import get_settings
from app.utils import jalali

settings = get_settings()

_WEEKDAY_INDEX = {
    "شنبه": 0, "یکشنبه": 1, "دوشنبه": 2, "سه‌شنبه": 3, "سه شنبه": 3,
    "چهارشنبه": 4, "پنجشنبه": 5, "جمعه": 6,
}


def _now() -> datetime:
    return datetime.now(ZoneInfo(settings.timezone))


def _today_9() -> datetime:
    n = _now()
    return n.replace(hour=9, minute=0, second=0, microsecond=0)


_NUM_FA = str.maketrans("۰۱۲۳۴۵۶۷۸۹", "0123456789")


def _en(s: str) -> str:
    return s.translate(_NUM_FA)


def parse_natural_time(text: str) -> datetime | None:
    """Parse Persian natural-language time into a tz-aware datetime."""
    s = _en((text or "").strip())
    if not s:
        return None
    low = s.lower()

    # exact Jalali date (also handles «30 شهریور ۱۴:۳۰»)
    jd = jalali.parse_jalali_input(s)
    if jd is not None:
        return jd

    # tomorrow morning / فردا صبح / فردا
    if "فردا" in low:
        t = _today_9() + timedelta(days=1)
        if "ظهر" in low:
            t = t.replace(hour=12)
        elif "عصر" in low:
            t = t.replace(hour=18)
        elif "شب" in low:
            t = t.replace(hour=21)
        m = re.search(r"ساعت\s*(\d{1,2})", low)
        if m:
            t = t.replace(hour=int(m.group(1)))
        return t

    if "پس‌فردا" in low or "پس فردا" in low:
        return _today_9() + timedelta(days=2)

    # +Nh (یک ساعت دیگه / ۲ ساعت بعد / تا N ساعت دیگر)
    m = re.search(r"(\d{1,2})\s*ساعت", low)
    if m and ("دیگه" in low or "بعد" in low or "دیگر" in low):
        return _now() + timedelta(hours=int(m.group(1)))
    if "نیم ساعت" in low and ("دیگه" in low or "بعد" in low):
        return _now() + timedelta(minutes=30)

    # "الان" / "همین الان"
    if "الان" in low or "همین حالا" in low:
        return _now() + timedelta(minutes=1)

    # weekday + ساعت («شنبه ساعت ۱۰»)
    for name, idx in _WEEKDAY_INDEX.items():
        if name in low:
            n = _now()
            current = (n.weekday() + 1) % 7  # Sat=0
            delta = (idx - current) % 7 or 7
            t = n.replace(hour=9, minute=0, second=0, microsecond=0) + timedelta(days=delta)
            m = re.search(r"ساعت\s*(\d{1,2})", low)
            if m:
                t = t.replace(hour=int(m.group(1)))
            return t

    # HH:MM alone → next occurrence today (or tomorrow if passed)
    m = re.search(r"^(\d{1,2})[.:](\d{2})$", s)
    if m:
        hh, mm = int(m.group(1)), int(m.group(2))
        if 0 <= hh <= 23 and 0 <= mm <= 59:
            t = _now().replace(hour=hh, minute=mm, second=0, microsecond=0)
            return t if t > _now() else t + timedelta(days=1)

    return None
