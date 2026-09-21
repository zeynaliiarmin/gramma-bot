"""Gramma — service context helpers.

Think of `AccountContext` as the "request envelope" used by every celery task
and handler callback: it carries exactly one tenant (Telegram user) and
exactly one Instagram account, already resolved and ownership-checked, so
downstream code can never accidentally operate cross-tenant.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.repositories import get_account_for_user, get_or_create_user

if TYPE_CHECKING:
    from app.models import InstagramAccount, User


@dataclass
class AccountContext:
    user: "User"
    account: "InstagramAccount"


async def build_context(
    session: AsyncSession,
    telegram_id: int,
    account_id: int,
    username: str | None = None,
    full_name: str = "",
) -> AccountContext:
    """Resolve tenant + account, raising if the account is not theirs.

    This is the single choke point for data isolation on the account axis.
    """
    user = await get_or_create_user(
        session, telegram_id, username=username, full_name=full_name
    )
    account = await get_account_for_user(session, telegram_id, account_id)
    if account is None:
        raise PermissionError("account_not_found_or_not_owned")
    return AccountContext(user=user, account=account)
