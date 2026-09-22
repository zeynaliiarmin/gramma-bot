"""Gramma — Vercel single serverless function (root `api/index.py`).

This is the ONLY entrypoint the Vercel project `miniapp` needs for the whole
backend. It reuses the existing FastAPI app unchanged (``app.webapp.main``)
and mounts two extra routers on top:

  * ``POST /api/telegram/webhook`` — Telegram Bot API webhook. Each update is
    parsed to an aiogram :class:`Update` and run through a fresh Dispatcher
    (the same routers + middlewares the long-polling runtime uses) via
    ``feed_update``, then answered with HTTP 200 so Telegram stops retrying.
  * ``GET/POST /webhook/instagram`` + ``/webhook/instagram/callback`` — the
    existing Meta OAuth + webhook receiver (already FastAPI).

Security:
  * The Telegram route validates the optional ``X-Telegram-Bot-Api-Secret-Token``
    header (constant-time) whenever ``TELEGRAM_WEBHOOK_SECRET`` is set. An
    update's ``token`` field is never trusted and never chooses a bot.
  * Everything else (initData HMAC, session tickets, ownership scoping) is
    already enforced inside ``app.webapp.main`` / ``app.webapp.auth``.
"""

from __future__ import annotations

import asyncio
import hmac
import logging
import os
import time

os.environ.setdefault("RUN_MODE", "webhook")

logger = logging.getLogger("gramma.vercel")

from fastapi import Request, Response  # noqa: E402

from app.core.config import get_settings  # noqa: E402
from app.webapp.main import app as miniapp_app  # noqa: E402 (asgi base)

settings = get_settings()

# Import shared state so test/health code can observe that an update arrived.
import app.core.shared_state as shared  # noqa: E402

# ── Telegram webhook router ───────────────────────────────────
from fastapi import APIRouter  # noqa: E402

_tg_router = APIRouter(tags=["telegram"])


def _authorized(request: Request) -> bool:
    """Constant-time check of the optional webhook secret header.

    Reads settings at call time (not import time) so tests/ops can flip the
    env without restarting — and a mis-set secret is always enforced when
    configured.
    """
    secret = get_settings().telegram_webhook_secret
    if not secret:
        return True
    provided = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
    return hmac.compare_digest(provided.encode(), secret.encode())


def _note_seen(update_id: int | None) -> None:
    """Best-effort observability: count + timestamp the last update."""
    try:
        shared.telegram_updates_seen += 1
        shared.telegram_last_processed_at = time.time()
        if update_id is not None:
            shared.telegram_last_update_id = update_id
    except Exception:  # noqa: BLE001
        pass


@_tg_router.post(settings.telegram_webhook_path)
async def telegram_webhook(request: Request):
    """Receive one Telegram update, run it through aiogram, answer 200."""
    if not _authorized(request):
        return Response(status_code=403, content="forbidden")

    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001
        return Response(status_code=400, content="bad json")

    # Telegram always stamps webhook deliveries with OUR bot token; reject
    # anything addressed to a different bot (a mis-set webhook or a replay).
    if isinstance(payload, dict):
        arrived_token = payload.get("token")
        if arrived_token and not hmac.compare_digest(
            str(arrived_token).encode(), settings.telegram_bot_token.encode()
        ):
            return Response(status_code=403, content="wrong bot token")

    update = _parse_update(payload)
    update_id = int(payload.get("update_id") or 0) if payload else None

    # Per-process dedupe: Telegram retries the same update_id while we are
    # still answering; skip genuine duplicates (best-effort, bounded LRU).
    if update_id and update_id in shared.seen_update_ids:
        return Response(status_code=200, content='{"ok":true,"duplicate":true}')
    if update_id:
        shared.seen_update_ids.add(update_id)
    _note_seen(update_id)

    if update is not None:
        try:
            await _dispatch(update)
        except Exception:  # noqa: BLE001
            logger.exception("telegram update handling failed")

    # Always acknowledge, so Telegram never storms us with re-deliveries.
    return Response(status_code=200, content='{"ok":true}')


def _parse_update(payload: dict):
    """Parse to an aiogram Update; tolerate the Telegram delivery shape."""
    from aiogram.types import Update

    allowed = {
        "update_id", "message", "edited_message", "channel_post",
        "edited_channel_post", "inline_query", "chosen_inline_result",
        "callback_query", "shipping_query", "pre_checkout_query",
        "web_app_data", "poll", "poll_answer", "my_chat_member",
        "chat_member", "chat_join_request", "chat_boost", "removed_chat_boost",
    }
    try:
        cleaned = {k: v for k, v in (payload or {}).items() if k in allowed}
        return Update.model_validate(cleaned)
    except Exception:  # noqa: BLE001
        return None


async def _dispatch(update) -> None:
    """Feed one update to a fresh aiogram Dispatcher + Bot.

    aiogram is designed around one long-lived event loop; on serverless the
    safest pattern is to recreate the whole stack per request (cheap for the
    small updates the bot handles). FSM context lives in a per-request
    MemoryStorage — single-shot flows (menus, buttons, webapp, /start) are
    stateless and fully reliable; multi-message dialogs re-prompt gracefully
    in webhook mode.
    """
    from aiogram import Bot, Dispatcher
    from aiogram.client.default import DefaultBotProperties
    from aiogram.enums import ParseMode
    from aiogram.fsm.strategy import FSMStrategy

    bot = Bot(
        token=settings.telegram_bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )

    dp = Dispatcher(storage=None, fsm_strategy=FSMStrategy.CHAT)
    dp["settings"] = settings

    from app.bot.main import DatabaseMiddleware, ErrorHandlerMiddleware, _fallback_router

    dp.message.middleware(DatabaseMiddleware())
    dp.callback_query.middleware(DatabaseMiddleware())
    dp.errors.middleware(ErrorHandlerMiddleware())

    # ── Registry: fresh (deep-copied) routers per request ──────
    # Module-level routers have a single `parent_router` slot, so re-using
    # them across serverless invocations raises "Router is already attached".
    # aiogram routers deep-copy cleanly (including their handler stacks), so
    # every request gets its own copy — this is the whole trick that makes
    # the long-polling router set safe for one-shot webhook dispatch.
    import copy as _copy

    from app.bot.handlers import (
        account, ai, calendar, collab, community, direct,
        insights, navigation, publish, schedule, search, studio,
    )

    for module in (
        account, ai, calendar, collab, community, direct,
        insights, navigation, publish, schedule, search, studio,
    ):
        try:
            dp.include_router(_copy.deepcopy(module.router))
        except Exception as exc:  # noqa: BLE001  (never break dispatch)
            logger.exception("include_router failed for %s: %s", module.__name__, exc)
    try:
        dp.include_router(_copy.deepcopy(_fallback_router()))
    except Exception:  # noqa: BLE001
        pass

    try:
        await dp.feed_update(bot, update, routing_key=_routing_key(update))
    finally:
        try:
            session = getattr(bot, "session", None)
            if session is not None:
                await session.close()
        except Exception:  # noqa: BLE001
            pass


def _routing_key(update) -> int:
    """Stable routing key = chat id of the contained event (FSMStrategy.CHAT)."""
    obj = (
        update.message
        or update.edited_message
        or update.callback_query
        or update.inline_query
        or update.chat_member
        or update.my_chat_member
        or update.chat_join_request
    )
    chat = None
    if obj is not None:
        chat = getattr(obj, "chat", None)
        if chat is None and update.callback_query is not None:
            chat = getattr(update.callback_query.message, "chat", None)
    if chat is not None:
        try:
            return int(chat.id)
        except (TypeError, ValueError):
            return 0
    return 0


# ── Cron trigger (pg_cron or Vercel Cron → background jobs) ──
@_tg_router.api_route("/api/cron/{job}", methods=["GET", "POST"], include_in_schema=False)
async def cron_trigger(job: str, request: Request):
    """Endpoint pg_cron (or Vercel Cron) calls for scheduled jobs:

      * publish                   — publish due posts (every 1 min)
      * process-comment-queue     — drain the comment-reply queue (every 5 min)
      * check-instagram-health    — probe connected pages (every 10 min)
      * reset-daily-counters      — no-op (reset is implicit via Tehran date
                                    keying); returns today's counters (00:00)
      * daily-report              — 23:59 Tehran admin report + alerts

    Guarded by the shared CRON_SECRET (sent as ``X-Cron-Secret``) so a random
    visitor cannot trigger jobs. Empty CRON_SECRET keeps it open for local
    testing only.
    """
    cron_secret = get_settings().cron_secret
    if cron_secret:
        provided = request.headers.get("X-Cron-Secret", "")
        if not hmac.compare_digest(provided.encode(), cron_secret.encode()):
            return Response(status_code=403, content="forbidden")

    if job == "publish":
        from app.services.publisher import publish_due_posts

        try:
            summary = await publish_due_posts()
            return {"ok": True, **summary}
        except Exception as exc:  # noqa: BLE001
            logger.exception("cron publish failed")
            return Response(status_code=500, content='{"ok":false}')

    if job == "process-comment-queue":
        from app.services.reply_queue import run_queue_pass

        try:
            totals = await run_queue_pass()
            return {"ok": True, **totals}
        except Exception as exc:  # noqa: BLE001
            logger.exception("queue pass failed")
            return Response(status_code=500, content='{"ok":false,"error":"%s"}' % str(exc)[:200])

    if job == "check-instagram-health":
        from app.services.monitoring import check_all_accounts_health

        try:
            reports = await check_all_accounts_health()
            return {"ok": True, "accounts_checked": len(reports),
                    "unhealthy": [r for r in reports if not r["ok"]]}
        except Exception as exc:  # noqa: BLE001
            logger.exception("health check failed")
            return Response(status_code=500, content='{"ok":false}')

    if job == "reset-daily-counters":
        # Reset is implicit: counters are keyed by Tehran date, so a new day
        # simply creates fresh rows. This endpoint reports the state (and is
        # kept as the explicit 00:00 Tehran boundary tick).
        from app.core.database import SessionLocal
        from app.services.monitoring import _today_stats

        try:
            async with SessionLocal() as s:
                stats = await _today_stats(s)
            return {"ok": True, "date": stats["date"],
                    "replies_sent": stats["replies_sent"],
                    "queued": stats["queued"]}
        except Exception as exc:  # noqa: BLE001
            logger.exception("reset-daily-counters failed")
            return Response(status_code=500, content='{"ok":false}')

    if job == "daily-report":
        from app.services.monitoring import build_and_send_daily_report

        try:
            report = await build_and_send_daily_report()
            return {"ok": report.get("ok", False), **report}
        except Exception as exc:  # noqa: BLE001
            logger.exception("daily report failed")
            return Response(status_code=500, content='{"ok":false}')

    return Response(status_code=404, content="unknown job")


# ── Mount the Instagram webhook/OAuth router too ─────────────
from app.webhook.webhook import router as instagram_router  # noqa: E402

miniapp_app.include_router(_tg_router)
miniapp_app.include_router(instagram_router)

# NOTE: we do NOT mount the FastAPI StaticFiles SPA here — Vercel serves the
# built Mini-App (dist/) itself. Everything /api/* and the webhook paths are
# answered by this one function.


async def _serverless_handler(scope, receive, send):
    """ASGI entrypoint Vercel calls; warm the DB on first event."""
    from app.core.database import _warmup

    try:
        await _warmup()
    except Exception:  # noqa: BLE001
        logger.warning("db warmup failed (continuing)")
    await miniapp_app(scope, receive, send)


# Vercel loads this `app` as the ASGI callable.
app = _serverless_handler
