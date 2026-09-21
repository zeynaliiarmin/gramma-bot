"""Gramma — End-to-End tests for the three platform caps.

Covers the exact three limits requested, each enforced through the real
code paths (not mocked servers):

  1. MAX_TOTAL_USERS              = 24 → 25th user blocked at /start (bot)
                                            AND 403 at /api/me (Mini-App).
  2. MAX_TOTAL_INSTAGRAM_ACCOUNTS = 24 → 25th page blocked (reason total_limit).
  3. MAX_ACCOUNTS_PER_USER        =  3 → 4th page blocked  (reason user_limit).

The limits module's constants are swapped to the small values below, so the
tests run fast while exercising the exact same enforcement functions used by
the bot handlers and the Mini-App server.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "000:test")
os.environ.setdefault("ENCRYPTION_KEY", "9f6Bd2S19LFy/SQ/9Om4559N1Lgi5upd2zZGwvITWOA=")
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///./tests_limits_e2e.db")
os.environ.setdefault("INSTAGRAM_ACCOUNT_MODE", "simulation")

import pytest

# ── small values → fast tests, same code paths ──────────────────
LOW_MAX_USERS = 24
LOW_MAX_ACCOUNTS = 24
LOW_PER_USER = 3


# ── helpers ──────────────────────────────────────────────────────
def _settings_off():
    """Disable AI side effects for handler runs; returns a restorer."""
    from app.core.config import get_settings

    s = get_settings()
    saved = (s.openclaw_base_url, s.avalai_api_key, s.openai_api_key)
    s.openclaw_base_url = ""
    s.avalai_api_key = ""
    s.openai_api_key = ""

    def restore():
        s.openclaw_base_url, s.avalai_api_key, s.openai_api_key = saved

    return restore


async def _reset_db():
    import sqlalchemy as sa

    from app.core.database import SessionLocal, init_db
    from app import models  # noqa: F401  (register all models)
    from app.models import (
        ActivityLog,
        Comment,
        Conversation,
        InstagramAccount,
        Media,
        OAuthFlow,
        Post,
        User,
    )

    await init_db()
    async with SessionLocal() as session:
        # child-first order (avoids the users↔instagram_accounts FK cycle warning)
        for model in (
            ActivityLog,
            OAuthFlow,
            Media,
            Post,
            Comment,
            Conversation,
            InstagramAccount,
            User,
        ):
            await session.execute(sa.delete(model))
        await session.commit()


async def _patch_limits():
    """Swap the module constants to the test values (same code paths)."""
    from app.services import limits as lm

    lm.__limits_backup__ = (
        lm.MAX_TOTAL_USERS,
        lm.MAX_TOTAL_INSTAGRAM_ACCOUNTS,
        lm.MAX_ACCOUNTS_PER_USER,
    )
    lm.MAX_TOTAL_USERS = LOW_MAX_USERS
    lm.MAX_TOTAL_INSTAGRAM_ACCOUNTS = LOW_MAX_ACCOUNTS
    lm.MAX_ACCOUNTS_PER_USER = LOW_PER_USER


async def _restore_limits():
    from app.services import limits as lm

    if getattr(lm, "__limits_backup__", None):
        lm.MAX_TOTAL_USERS, lm.MAX_TOTAL_INSTAGRAM_ACCOUNTS, lm.MAX_ACCOUNTS_PER_USER = (
            lm.__limits_backup__
        )


class _FakeUser:
    def __init__(self, uid: int):
        self.id = uid
        self.username = None
        self.full_name = f"User{uid}"
        self.first_name = f"User{uid}"
        self.language_code = "fa"


class _FakeMessage:
    """Minimal stand-in carrying the fields the bot handlers actually touch."""

    def __init__(self, uid: int):
        self.from_user = _FakeUser(uid)
        self.sent: list[str] = []

    async def answer(self, text: str, **kwargs):
        self.sent.append(text)
        return self


class _DummyState:
    async def clear(self):  # FSMStateContext has no awaitable body in cmd_start
        pass

    async def set_state(self, *_a, **_k):
        pass

    async def update_data(self, *_a, **_k):
        pass

    async def get_data(self, *_a, **_k):
        return {}


def _assert_limits():
    """The three env-driven caps must be exposed with the exact names."""
    from app.core.config import get_settings

    s = get_settings()
    assert s.max_total_users == 24
    assert s.max_total_instagram_accounts == 24
    assert s.max_accounts_per_user == 3


async def _seed_users(start: int, count: int):
    from app.core.database import SessionLocal
    from app.models import User

    async with SessionLocal() as session:
        for uid in range(start, start + count):
            session.add(User(id=uid, full_name=f"U{uid}"))
        await session.commit()


# ── 1) MAX_TOTAL_USERS: user #25 blocked (bot + miniapp) ─────────
@pytest.mark.asyncio
async def test_total_users_cap_bot_blocks_25th_user():
    from app.bot.handlers.account import cmd_start
    from app.core.database import SessionLocal
    from app.models import User

    restore = _settings_off()
    await _reset_db()
    await _patch_limits()
    try:
        await _seed_users(1001, LOW_MAX_USERS)

        m = _FakeMessage(9999)
        await cmd_start(m, state=_DummyState())
        replies = " || ".join(m.sent)
        assert ("ظرفیت" in replies) or ("تکمیل" in replies), replies

        # the 25th user was never persisted
        async with SessionLocal() as session:
            assert await session.get(User, 9999) is None
    finally:
        await _restore_limits()
        restore()


@pytest.mark.asyncio
async def test_total_users_cap_miniapp_returns_403():
    import json

    from app.webapp.main import api_me

    restore = _settings_off()
    await _reset_db()
    await _patch_limits()
    try:
        await _seed_users(2001, LOW_MAX_USERS)

        class _Ident:
            user_id = 9999
            expires_at = 2**40

        resp = await api_me(_Ident())
        assert resp.status_code == 403
        body = resp.body if isinstance(resp.body, dict) else json.loads(resp.body)
        assert body.get("limits", {}).get("max_total_users") == LOW_MAX_USERS
    finally:
        await _restore_limits()
        restore()


# ── 2) MAX_TOTAL_INSTAGRAM_ACCOUNTS: 25th page blocked ───────────
@pytest.mark.asyncio
async def test_total_accounts_cap_blocks_25th_page():
    from app.core.database import SessionLocal
    from app.core.security.crypto import get_cipher
    from app.db.repositories import get_or_create_user
    from app.models import InstagramAccount
    from app.services.limits import can_connect_account

    await _reset_db()
    await _patch_limits()
    try:
        cipher = get_cipher()
        async with SessionLocal() as session:
            n_users = LOW_MAX_ACCOUNTS // LOW_PER_USER  # 8 users
            for uid in range(3001, 3001 + n_users):
                u = await get_or_create_user(session, uid, full_name=f"U{uid}")
                for i in range(LOW_PER_USER):
                    session.add(
                        InstagramAccount(
                            owner_id=u.id,
                            username=f"u{uid}_{i}",
                            long_lived_token_enc=cipher.encrypt("t"),
                        )
                    )
            await session.flush()  # total = 24 pages

            # a brand-new user's 1st page → refused (global cap)
            ux = await get_or_create_user(session, 3999, full_name="NewUser")
            ok, reason = await can_connect_account(session, ux.id)
            assert ok is False and reason == "total_limit", (ok, reason)
            await session.rollback()
    finally:
        await _restore_limits()


# ── 3) MAX_ACCOUNTS_PER_USER: 4th page blocked ───────────────────
@pytest.mark.asyncio
async def test_per_user_cap_blocks_4th_page():
    from app.core.database import SessionLocal
    from app.core.security.crypto import get_cipher
    from app.db.repositories import get_or_create_user
    from app.models import InstagramAccount
    from app.services.limits import can_connect_account

    await _reset_db()
    await _patch_limits()
    try:
        cipher = get_cipher()
        async with SessionLocal() as session:
            u1 = await get_or_create_user(session, 4001, full_name="A")
            for i in range(LOW_PER_USER):
                session.add(
                    InstagramAccount(
                        owner_id=u1.id,
                        username=f"a{i}",
                        long_lived_token_enc=cipher.encrypt("t"),
                    )
                )
            await session.flush()

            ok, reason = await can_connect_account(session, u1.id)
            assert ok is False and reason == "user_limit", (ok, reason)
            await session.rollback()
    finally:
        await _restore_limits()


# ── config wiring: the exact env var names must exist ────────────
def test_env_var_names_are_exact():
    _assert_limits()
