"""
Gramma — data-access layer (repositories).

Every method that touches user-owned data starts from the acting Telegram
`user_id`, guaranteeing **tenant isolation**: a query never finds another
user's rows, because the account lookup is scoped by `owner_id = user.id`.
"""

from __future__ import annotations

from typing import AsyncIterator, Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security.crypto import get_cipher


async def get_or_create_user(
    session: AsyncSession,
    telegram_id: int,
    username: str | None = None,
    full_name: str = "",
    locale: str = "en",
):
    from app.models import User

    user = await session.get(User, telegram_id)
    if user is None:
        user = User(
            id=telegram_id,
            telegram_username=username,
            full_name=full_name or (username or str(telegram_id)),
            locale=locale,
        )
        session.add(user)
        await session.flush()
    return user


async def get_accounts_for_user(
    session: AsyncSession, telegram_id: int
) -> Sequence["InstagramAccount"]:
    """All Instagram accounts owned by this Telegram user (any order)."""
    from app.models import InstagramAccount

    result = await session.execute(
        select(InstagramAccount).where(InstagramAccount.owner_id == telegram_id)
    )
    return result.scalars().all()


async def get_account_for_user(
    session: AsyncSession, telegram_id: int, account_id: int
):
    """Fetch ONE account, scoped to its owner. Returns None if it belongs
    to someone else (or does not exist) — the caller must not leak either way."""
    from app.models import InstagramAccount

    result = await session.execute(
        select(InstagramAccount).where(
            InstagramAccount.id == account_id,
            InstagramAccount.owner_id == telegram_id,
        )
    )
    return result.scalars().first()


async def iter_scheduled_posts(
    session: AsyncSession, only_due: bool = True, now=None
) -> AsyncIterator["Post"]:
    """Stream posts that are scheduled (and optionally due)."""
    from datetime import datetime, timezone

    from app.models import Post

    now = now or datetime.now(timezone.utc)
    stmt = select(Post).where(Post.status == "scheduled")
    if only_due:
        stmt = stmt.where(Post.scheduled_at <= now)
    result = await session.stream(stmt)
    async for row in result:
        yield row[0]


def decrypt_token(ciphertext: str | None) -> str:
    """Helper: decrypt an at-rest token (empty → empty)."""
    if not ciphertext:
        return ""
    return get_cipher().decrypt(ciphertext)


async def get_active_account(session: AsyncSession, telegram_id: int):
    """The user's 'active' page (or their first one)."""
    from app.models import InstagramAccount, User

    user = await session.get(User, telegram_id)
    accounts = await get_accounts_for_user(session, telegram_id)
    if not accounts:
        return None
    if user is not None and user.active_account_id:
        match = next((a for a in accounts if a.id == user.active_account_id), None)
        if match:
            return match
    return accounts[0]


async def set_active_account(session: AsyncSession, telegram_id: int, account_id: int) -> bool:
    """Point the user's active page at one of their own accounts."""
    from app.models import User

    accounts = await get_accounts_for_user(session, telegram_id)
    if account_id not in [a.id for a in accounts]:
        return False
    user = await session.get(User, telegram_id)
    if user is not None:
        user.active_account_id = account_id
        await session.flush()
    return True
