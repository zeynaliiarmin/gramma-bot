"""Gramma — automatic token refresh.

Long-lived Instagram tokens last ~60 days. This service proactively refreshes
tokens *before* they expire (configurable safety margin) so users never have
to re-login.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.core.config import get_settings
from app.core.database import SessionLocal
from app.core.security.crypto import get_cipher
from app.models import ActivityLog, InstagramAccount
from app.services.meta.oauth import exchange_long_lived_token

settings = get_settings()
logger = logging.getLogger("gramma.token_refresh")


async def refresh_token_if_needed(account: InstagramAccount, session=None) -> bool:
    """Refresh one account's token if it is within the safety margin.

    Returns True when a refresh happened.
    """
    if not account.long_lived_token_enc:
        return False

    expires_at = account.token_expires_at
    if expires_at and expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)

    margin = timedelta(minutes=settings.auto_refresh_margin_minutes)
    if expires_at and datetime.now(timezone.utc) < (expires_at - margin):
        return False  # still far from expiry

    token = get_cipher().decrypt(account.long_lived_token_enc)
    try:
        data = await exchange_long_lived_token(token)
    except Exception as exc:
        logger.warning("token refresh failed account=%s err=%s", account.id, exc)
        return False

    new_token = data.get("access_token")
    expires_in = int(data.get("expires_in", 0) or 0)  # seconds
    if not new_token:
        return False

    account.long_lived_token_enc = get_cipher().encrypt(new_token)
    account.token_expires_at = (
        datetime.now(timezone.utc) + timedelta(seconds=expires_in)
        if expires_in
        else datetime.now(timezone.utc) + timedelta(days=60)
    )
    account.updated_at = datetime.now(timezone.utc)
    return True


async def refresh_all_due_tokens() -> dict:
    """Scheduled job: scan every account, refresh anything near expiry."""
    refreshed = 0
    failed = 0
    async with SessionLocal() as session:
        result = await session.execute(select(InstagramAccount))
        accounts = result.scalars().all()
        for account in accounts:
            try:
                if await refresh_token_if_needed(account, session):
                    session.add(
                        ActivityLog(
                            account_id=account.id,
                            user_id=account.owner_id,
                            action="token_refresh",
                            detail="automatic long-lived token refresh",
                            level="info",
                        )
                    )
                    refreshed += 1
            except Exception as exc:  # noqa: BLE001
                logger.exception("refresh error account=%s", account.id)
                failed += 1
        await session.commit()
    logger.info("token refresh sweep done refreshed=%s failed=%s", refreshed, failed)
    return {"refreshed": refreshed, "failed": failed}
