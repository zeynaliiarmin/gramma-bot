"""Admin-bot fixes — E2E/security tests for the new requirements.

Covers:
  * Telegram initData HMAC-SHA256 validation (valid → accepted,
    forged/other-bot/wrong-token → rejected).
  * Jalali (Shamsi) conversions + parser, Latin-digit formatting.
  * «منوی کامل» persistent reply keyboard + full inline menu wiring.
  * Natural-language smart scheduling («فردا صبح»).
  * allowed_updates set in the polling runner.
  * Mini-App auth flow: ticket minted only after valid initData.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import sys
from datetime import datetime, timezone
from urllib.parse import urlencode

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ["TELEGRAM_BOT_TOKEN"] = "000:test"
os.environ["ENCRYPTION_KEY"] = "9f6Bd2S19LFy/SQ/9Om4559N1Lgi5upd2zZGwvITWOA="
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///./tests_v2.db")
os.environ.setdefault("INSTAGRAM_ACCOUNT_MODE", "simulation")

BOT_TOKEN = "000:test"


# ── Telegram initData fixture (canonical signer) ──────────────
def _secret_key(token: str) -> bytes:
    return hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()


def _sign_init_data(fields: dict, token: str) -> str:
    check = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    h = hmac.new(_secret_key(token), check.encode(), hashlib.sha256).hexdigest()
    fields = dict(fields)
    fields["hash"] = h
    return urlencode(fields)


def _valid_init_data(token: str = BOT_TOKEN, user_id: int = 456) -> str:
    user = json.dumps({"id": user_id, "first_name": "آرمین", "username": "armin"})
    return _sign_init_data(
        {
            "auth_date": str(int(datetime.now(timezone.utc).timestamp())),
            "query_id": "AAE123",
            "user": user,
        },
        token,
    )


# ── initData validation ──────────────────────────────────────
def test_init_data_valid_signature_accepted():
    from app.core.security.tg_initdata import validate_init_data

    fields = validate_init_data(_valid_init_data(), BOT_TOKEN)
    assert fields is not None
    assert fields["user"]["id"] == 456
    assert fields["user"]["first_name"] == "آرمین"


def test_init_data_forged_hash_rejected():
    from app.core.security.tg_initdata import validate_init_data

    raw = _valid_init_data()
    # corrupt the hash
    parts = raw.split("&")
    parts[-1] = "hash=" + "0" * 64
    forged = "&".join(parts)
    assert validate_init_data(forged, BOT_TOKEN) is None


def test_init_data_signed_by_other_bot_rejected():
    from app.core.security.tg_initdata import validate_init_data

    raw = _valid_init_data(token="OTHER:token")  # signed with a different bot
    assert validate_init_data(raw, BOT_TOKEN) is None


def test_init_data_user_id_never_trusted_without_hash():
    from app.core.security.tg_initdata import validate_init_data

    # No hash field at all → rejected even though user.id looks valid.
    fields_raw = urlencode(
        {
            "auth_date": str(int(datetime.now(timezone.utc).timestamp())),
            "user": json.dumps({"id": 456, "first_name": "x"}),
        }
    )
    assert validate_init_data(fields_raw, BOT_TOKEN) is None


def test_miniapp_verify_endpoint_rejects_bad_initdata():
    """app.webapp verify_telegram_init_data returns None on forged data."""
    from app.webapp.auth import verify_telegram_init_data

    assert verify_telegram_init_data(_valid_init_data()) == 456
    assert verify_telegram_init_data("garbage") is None
    assert verify_telegram_init_data("") is None


# ── Jalali calendar ──────────────────────────────────────────
def test_jalali_roundtrip_and_format():
    from app.utils import jalali

    # 2026-03-21 12:00 UTC → 1405/01/01 (Norooz), display 15:30 local.
    dt = datetime(2026, 3, 21, 12, 0, tzinfo=timezone.utc)
    assert jalali.to_jalali(dt) == (1405, 1, 1)
    assert jalali.to_jalali(datetime(2026, 9, 21, 12, 0, tzinfo=timezone.utc)) == (1405, 6, 30)

    # Latin digits (user requirement: no Persian digits anywhere).
    s = jalali.to_jalali_str(dt)
    assert s.startswith("1405/01/01")
    assert "۰" not in s and "۱" not in s

    # round-trip over a full year
    from datetime import timedelta

    base = datetime(2025, 3, 20, tzinfo=timezone.utc)
    for i in range(0, 370):
        d = base + timedelta(days=i)
        jy, jm, jd = jalali.to_jalali(d)
        back = jalali.from_jalali(jy, jm, jd)
        assert (back.year, back.month, back.day) == (d.year, d.month, d.day)


def test_jalali_parser_accepts_persian_input():
    from app.utils import jalali

    a = jalali.parse_jalali_input("1405/06/30 14:30")
    b = jalali.parse_jalali_input("۳۰ شهریور ۱۴:۳۰")
    assert a is not None and b is not None
    assert a.replace(tzinfo=None) == b.replace(tzinfo=None)
    # 1405/06/30 → 2026-09-21
    assert (a.year, a.month, a.day) == (2026, 9, 21)


def test_jalali_context_for_ai():
    from app.utils.jalali import current_jalali_context

    ctx = current_jalali_context()
    assert "current_date_jalali" in ctx
    assert "current_time" in ctx
    assert "day_of_week" in ctx
    assert ctx["timezone"] == "Asia/Tehran"


# ── persistent menu + full inline menu ───────────────────────
def test_persistent_menu_button_exists():
    from aiogram.types import KeyboardButton

    from app.bot.keyboards import persistent_menu

    kb = persistent_menu()
    texts = [b.text for row in kb.keyboard for b in row]
    assert "📋 منوی کامل" in texts


def test_full_menu_has_all_capabilities():
    from app.bot.keyboards import full_menu

    datas = [b.callback_data for row in full_menu().inline_keyboard for b in row]
    # Every capability is present, and every callback_data has a handler.
    expected = {
        "publish:menu", "publish:new:reel", "publish:new:story",
        "community:comments", "direct:menu", "calendar:menu",
        "collab:menu", "insights:menu", "settings:menu", "webapp:open",
    }
    assert expected <= set(datas)


def test_full_menu_callbacks_have_handlers():
    """build_dispatcher registers a handler for every full_menu callback."""
    from types import SimpleNamespace

    from app.bot.main import build_dispatcher
    from app.bot.keyboards import full_menu

    dp = build_dispatcher()

    def target_is_handled(cb_data: str) -> bool:
        event = SimpleNamespace(data=cb_data)
        for sub in dp.sub_routers:
            for h in sub.callback_query.handlers:
                for f in h.filters:
                    fn = getattr(f, "callback", None)
                    try:
                        if fn is not None and fn(event) is True:
                            return True
                    except Exception:  # noqa: BLE001
                        pass
        return False

    for row in full_menu().inline_keyboard:
        for b in row:
            assert target_is_handled(b.callback_data), f"no handler for {b.callback_data}"


# ── smart scheduling NLP ─────────────────────────────────────
def test_natural_time_tomorrow_morning():
    from app.utils.datetime_input import parse_natural_time

    t = parse_natural_time("فردا صبح")
    assert t is not None and t.hour == 9


def test_natural_time_weekday_hour():
    from app.utils.datetime_input import parse_natural_time

    t = parse_natural_time("شنبه ساعت 10")
    assert t is not None and t.hour == 10


def test_natural_time_falls_back_to_jalali_date():
    from app.utils.datetime_input import parse_natural_time

    t = parse_natural_time("1405/06/30 14:30")
    assert t is not None and (t.year, t.month, t.day) == (2026, 9, 21)


# ── allowlist updates in polling runner ──────────────────────
def test_run_py_allowed_updates_includes_callback_and_webapp():
    import run as run_module

    allowed = run_module.ALLOWED_UPDATES
    for required in ("message", "callback_query", "inline_query", "web_app_data"):
        assert required in allowed
