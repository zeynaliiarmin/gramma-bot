"""Gramma — models/repository tests."""

from __future__ import annotations

import os
import sys

# Ensure the project root is importable when run directly.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "000:test")
os.environ.setdefault("ENCRYPTION_KEY", "9f6Bd2S19LFy/SQ/9Om4559N1Lgi5upd2zZGwvITWOA=")
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///./tests.db")


@pytest.mark.asyncio
async def test_tenant_isolation():
    from app.core.database import SessionLocal, init_db
    from app.core.security.crypto import get_cipher
    from app.db.repositories import get_accounts_for_user, get_or_create_user
    from app.models import InstagramAccount

    await init_db()
    cipher = get_cipher()
    async with SessionLocal() as session:
        u = await get_or_create_user(session, 111, full_name="A")
        session.add(
            InstagramAccount(
                owner_id=u.id,
                username="a.page",
                long_lived_token_enc=cipher.encrypt("token-of-A"),
            )
        )
        await session.commit()

    async with SessionLocal() as session:
        a_accounts = await get_accounts_for_user(session, 111)
        assert len(a_accounts) == 1
        b_accounts = await get_accounts_for_user(session, 222)
        assert b_accounts == []
