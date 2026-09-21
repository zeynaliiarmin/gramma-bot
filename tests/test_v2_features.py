"""Gramma v2 — unit tests for limits, search, calendar, collab, miniapp auth."""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import datetime, timezone

import pytest

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "000:test")
os.environ.setdefault("ENCRYPTION_KEY", "9f6Bd2S19LFy/SQ/9Om4559N1Lgi5upd2zZGwvITWOA=")
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///./tests_v2.db")
os.environ.setdefault("INSTAGRAM_ACCOUNT_MODE", "simulation")


def test_to_jalali():
    from app.services.calendar import to_jalali

    assert to_jalali(datetime(2026, 3, 21, 12, 0, tzinfo=timezone.utc)) == (1405, 1, 1)
    assert to_jalali(datetime(2026, 9, 21, 12, 0, tzinfo=timezone.utc)) == (1405, 6, 30)


def test_fa_weekday():
    from app.services.calendar import fa_weekday

    # 2026-09-21 is a Monday
    assert fa_weekday(datetime(2026, 9, 21, 12, 0, tzinfo=timezone.utc)) == "دوشنبه"


def test_tokenizer_persian():
    from app.services.search import tokenize

    assert tokenize("قیمت محصول") == tokenize("قیمتِ محصولات")
    assert tokenize("اینستاگرام") == ["اینستاگرام"]
    assert "قیمت" in tokenize("قیمت چنده؟")


def test_normalize_arabic_to_persian():
    from app.services.search import normalize_text

    assert normalize_text("عربي") == "عربی"
    assert normalize_text("كتاب") == "کتاب"


def test_miniapp_auth():
    from app.webapp.auth import make_miniapp_ticket, verify_ticket

    ticket = make_miniapp_ticket(123)
    ident = verify_ticket(ticket)
    assert ident is not None and ident.user_id == 123
    assert verify_ticket("garbage") is None
    # tamper user id
    parts = ticket.split(":")
    forged = f"999:{parts[1]}:{parts[2]}"
    assert verify_ticket(forged) is None


@pytest.mark.asyncio
async def test_limits_enforced():
    """v3 limits: 3 pages/user AND (globally) 24 pages total in dev mode."""
    import sqlalchemy as sa

    from app.core.database import SessionLocal, init_db
    from app.core.security.crypto import get_cipher
    from app.db.repositories import get_or_create_user
    from app.models import InstagramAccount
    from app.services.limits import can_connect_account

    await init_db()
    cipher = get_cipher()

    async with SessionLocal() as session:
        from app.models import InstagramAccount as IA

        existing = (await session.execute(sa.select(IA))).scalars().all()
        for r in existing:
            await session.delete(r)
        await session.commit()

        # 1) per-user cap: user A gets 3, 4th blocked
        u1 = await get_or_create_user(session, 4001, full_name="A")
        for i in range(3):
            session.add(InstagramAccount(owner_id=u1.id, username=f"a{i}", long_lived_token_enc=cipher.encrypt("t")))
        await session.flush()
        ok, reason = await can_connect_account(session, u1.id)
        assert ok is False and reason == "user_limit"

        # 2) global dev cap = 24 pages: 8 users x 3 pages = 24, 25th blocked
        for uid in range(4100, 4107):  # 7 more users (u1 already has 3)
            u = await get_or_create_user(session, uid, full_name=f"U{uid}")
            for i in range(3):
                session.add(InstagramAccount(owner_id=u.id, username=f"u{uid}_{i}", long_lived_token_enc=cipher.encrypt("t")))
        await session.flush()  # total = 3 + 7*3 = 24

        # a new user wants their 1st page → must be blocked (total cap)
        ux = await get_or_create_user(session, 4999, full_name="NewUser")
        ok2, reason2 = await can_connect_account(session, ux.id)
        assert ok2 is False and reason2 == "total_limit", (ok2, reason2)
        await session.rollback()

    # cleanup for repeatability
    async with SessionLocal() as session:
        from app.models import InstagramAccount as IA

        rows = (await session.execute(sa.select(IA))).scalars().all()
        for r in rows:
            await session.delete(r)
        await session.commit()
