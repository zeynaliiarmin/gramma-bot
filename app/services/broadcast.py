"""Gramma — admin/ops broadcast helpers.

`notify_admin` is used by the webhook consumer to warn the bot admin about
developer-mode platform restrictions, and `notify_all_users` powers the
admin panel's "announce to everyone" button. Both go through the shared bot
instance; failures are logged, never fatal.
"""

from __future__ import annotations

import logging

from app.core.config import get_settings

settings = get_settings()
logger = logging.getLogger("gramma.broadcast")


async def notify_admin(text: str) -> bool:
    """Send a message to every configured admin id."""
    from app.bot.main import get_bot

    if not settings.admin_ids:
        return False
    bot = get_bot()
    ok = 0
    for admin_id in settings.admin_ids:
        try:
            await bot.send_message(chat_id=admin_id, text=text)
            ok += 1
        except Exception as exc:  # noqa: BLE001
            logger.warning("admin notify failed id=%s: %s", admin_id, exc)
    return ok > 0


async def notify_all_users(result: dict, text: str) -> int:
    """Broadcast a message to every registered user. Returns delivered count."""
    from sqlalchemy import select

    from app.bot.main import get_bot
    from app.core.database import SessionLocal
    from app.models import User

    bot = get_bot()
    delivered = 0
    async with SessionLocal() as session:
        users = (await session.execute(select(User))).scalars().all()
        for user in users:
            try:
                await bot.send_message(chat_id=user.id, text=text)
                delivered += 1
            except Exception as exc:  # noqa: BLE001
                logger.warning("broadcast failed uid=%s: %s", user.id, exc)
    result["delivered"] = delivered
    return delivered
