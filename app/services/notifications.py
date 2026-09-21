"""Gramma — outbound Telegram notifications (used by background jobs).

Background jobs (celery beat / worker) run without a request context, so they
cannot use handler-reply helpers; this module sends raw bot-API messages via
the shared aiogram Bot instance.
"""

from __future__ import annotations

import logging

from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger("gramma.notifications")


async def notify_telegram(
    session: AsyncSession, telegram_id: int, text: str, kind: str = "info"
) -> bool:
    """Send a plain-text message to one telegram user (smart-gated). Returns success."""
    from app.bot.main import get_bot
    from app.services.smart_notifications import should_notify

    if not should_notify(kind):
        logger.info("notification suppressed kind=%s uid=%s", kind, telegram_id)
        return False

    bot = get_bot()
    try:
        await bot.send_message(chat_id=telegram_id, text=text)
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("notify failed uid=%s err=%s", telegram_id, exc)
        return False
