"""Gramma — aiogram bot factory + middleware.

The dispatcher is assembled here so both `run.py --web` (bot + webhook server)
and future decoupled deployments can reuse it.
"""

from __future__ import annotations

import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import TelegramObject

from app.bot.handlers import (
    account,
    ai,
    calendar,
    collab,
    community,
    direct,
    insights,
    navigation,
    publish,
    schedule,
    search,
    studio,
)
from app.core.config import get_settings

settings = get_settings()
logger = logging.getLogger("gramma.bot")

_bot: Bot | None = None
_dp: Dispatcher | None = None


def get_bot() -> Bot:
    """Return the shared bot instance (used by background notifications)."""
    global _bot
    if _bot is None:
        _bot = Bot(
            token=settings.telegram_bot_token,
            default=DefaultBotProperties(parse_mode=ParseMode.HTML),
        )
    return _bot


def build_dispatcher() -> Dispatcher:
    """Create + wire the dispatcher with middleware and routers."""
    from aiogram.fsm.strategy import FSMStrategy

    dp = Dispatcher(storage=MemoryStorage(), fsm_strategy=FSMStrategy.USER_IN_CHAT)
    dp["settings"] = settings

    # Middleware: ensure every callback/message opens a clean scoped DB
    # session (so access is always tracked) + friendly error guard.
    dp.callback_query.middleware(DatabaseMiddleware())
    dp.message.middleware(DatabaseMiddleware())
    dp.errors.middleware(ErrorHandlerMiddleware())

    for module in (
        account, ai, calendar, collab, community, direct,
        insights, navigation, publish, schedule, search, studio,
    ):
        dp.include_router(module.router)

    # Catch-all router LAST: logs unhandled callbacks/timers and answers the
    # user with a friendly message, so a button never silently does nothing.
    dp.include_router(_fallback_router())

    return dp


def _fallback_router() -> "Router":
    """Late-bound router that catches any callback_query no handler claimed."""
    from aiogram import F, Router
    from aiogram.types import CallbackQuery, Message, TelegramObject

    router = Router(name="fallback")

    @router.callback_query()
    async def unhandled_callback(callback: CallbackQuery):
        data = getattr(callback, "data", None)
        logger.warning(
            "unhandled callback %r from user=%s", data, callback.from_user.id
        )
        from app.bot.keyboards import main_menu

        try:
            await callback.answer("این دکمه در دسترس نیست.", show_alert=False)
        except Exception:  # noqa: BLE001
            pass
        try:
            await callback.message.edit_text(
                "🏠 منوی اصلی:", reply_markup=main_menu()
            )
        except Exception:  # noqa: BLE001
            try:
                await callback.message.answer(
                    "🏠 منوی اصلی:", reply_markup=main_menu()
                )
            except Exception:  # noqa: BLE001
                pass

    @router.message()
    async def unhandled_text(message: Message):
        # Only fires when NO state filter matched (e.g. user typed something
        # outside a dialog). Keep it silent — do not spam every message.
        logger.info(
            "unhandled plain message from user=%s: %r",
            message.from_user.id,
            (message.text or "")[:60],
        )

    return router



def get_dispatcher() -> Dispatcher:
    global _dp
    if _dp is None:
        _dp = build_dispatcher()
    return _dp


class DatabaseMiddleware:
    """Attach a fresh DB session to every update (data-isolation ready)."""

    async def __call__(self, handler, event: TelegramObject, data: dict):
        from app.core.database import SessionLocal

        async with SessionLocal() as session:
            data["db"] = session
            try:
                return await handler(event, data)
            finally:
                await session.close()


class ErrorHandlerMiddleware:
    """Never let an unexpected exception kill the bot — reply gracefully.

    aiogram errors middleware receives the raw :class:`Update`, so we unpack
    the underlying Message / CallbackQuery before trying to answer the user.
    """

    async def __call__(self, handler, event, data: dict):
        try:
            return await handler(event, data)
        except Exception as exc:  # noqa: BLE001
            logger.exception("handler error: %s", exc)
            from aiogram.types import CallbackQuery, Message, Update

            update = event if isinstance(event, Update) else None
            target = None
            if update is not None:
                target = update.message or update.callback_query
            else:
                target = event

            fallback = "⚠️ مشکلی پیش آمد. لطفاً دوباره تلاش کنید."
            try:
                if isinstance(target, Message):
                    await target.answer(fallback)
                elif isinstance(target, CallbackQuery):
                    try:
                        await target.message.answer(fallback)
                    except Exception:  # noqa: BLE001
                        pass
                    await target.answer("خطایی رخ داد.")
            except Exception:  # noqa: BLE001
                pass
            return None
