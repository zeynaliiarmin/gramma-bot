"""Gramma — Vercel single serverless function (root `api/index.py`).

This is the ONLY entrypoint the Vercel project `miniapp` needs for the whole
backend. It reuses the existing FastAPI app unchanged (``app.webapp.main``)
and mounts two extra routers on top:

  * ``POST /api/telegram/webhook`` — Telegram Bot API webhook.
  * ``GET/POST /webhook/instagram`` + ``/webhook/instagram/callback`` — Meta.

Performance (2026-09-22 rework — "ack-first, build-once"):
  * The aiogram Dispatcher (+ all 12 routers + middlewares) is built ONCE at
    module load and reused by every request. No more per-request deepcopy of
    routers, no lazy imports inside the request path — this removes seconds
    of overhead per button press on warm instances.
  * Every ``callback_query`` is acknowledged IMMEDIATELY (``answer()``) in an
    outer middleware BEFORE any handler work starts, so the Telegram client
    stops showing the button spinner at once — true ack-first.
  * ``CallbackQuery.answer`` is made idempotent-safe (a second/late answer
    never raises), so existing handlers that call ``answer()`` again keep
    working after the middleware already acked.
  * Only the aiogram ``Bot`` object is created per request (cheap — the HTTP
    session is lazy) and closed at the end of the request, which keeps us
    safe against event-loop reuse issues on serverless.

Security:
  * The Telegram route validates the optional ``X-Telegram-Bot-Api-Secret-Token``
    header (constant-time) whenever ``TELEGRAM_WEBHOOK_SECRET`` is set.
  * Everything else (initData HMAC, session tickets, ownership scoping) is
    enforced inside ``app.webapp.main`` / ``app.webapp.auth``.
"""

from __future__ import annotations

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
    """Constant-time check of the optional webhook secret header."""
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


# ── Build the bot machinery ONCE per instance (module load) ───
# Handlers are imported eagerly so cold-start cost is paid once, and ONE
# Dispatcher (routers + middlewares + FSM storage) is shared by all requests
# served by this warm instance — no per-request rebuilds, no lazy imports.
# Routers are deep-copied exactly once at import time (aiogram routers copy
# cleanly) so the original module routers stay free for other dispatchers in
# the same process (local polling, tests).
import copy as _copy  # noqa: E402

from aiogram import Bot, Dispatcher  # noqa: E402
from aiogram.client.default import DefaultBotProperties  # noqa: E402
from aiogram.enums import ParseMode  # noqa: E402
from aiogram.fsm.storage.memory import MemoryStorage  # noqa: E402
from aiogram.fsm.strategy import FSMStrategy  # noqa: E402
from aiogram.types import CallbackQuery  # noqa: E402

from app.bot.handlers import (  # noqa: E402
    account, ai, calendar, collab, community, direct,
    insights, navigation, publish, schedule, search, studio,
)
from app.bot.main import (  # noqa: E402
    DatabaseMiddleware,
    ErrorHandlerMiddleware,
    _fallback_router,
)


def _detached_copy(router):
    """Deep-copy a router and drop the copied parent attachment.

    ``deepcopy`` carries ``_parent_router`` over; if the original router was
    already attached to another dispatcher in this process (local polling or
    tests), the copy would refuse to attach here. Only the copy is touched.
    """
    r = _copy.deepcopy(router)
    r._parent_router = None  # noqa: SLF001 — aiogram internal, safe on a copy
    return r


def _build_webhook_dispatcher() -> Dispatcher:
    dp = Dispatcher(storage=MemoryStorage(), fsm_strategy=FSMStrategy.USER_IN_CHAT)
    dp["settings"] = settings
    dp.callback_query.middleware(DatabaseMiddleware())
    dp.message.middleware(DatabaseMiddleware())
    dp.errors.middleware(ErrorHandlerMiddleware())
    for module in (
        account, ai, calendar, collab, community, direct,
        insights, navigation, publish, schedule, search, studio,
    ):
        dp.include_router(_detached_copy(module.router))
    # Catch-all LAST: answers any callback no handler claimed.
    dp.include_router(_detached_copy(_fallback_router()))
    return dp


_DISPATCHER = _build_webhook_dispatcher()

# ── Idempotent-safe CallbackQuery.answer (module-level, webhook mode) ──
# After the ack-first middleware answers a query, handlers may call
# ``callback.answer(...)`` again; Telegram then returns an error which aiogram
# would raise. Swallow that specific failure so no handler ever breaks.
_orig_answer = CallbackQuery.answer


async def _safe_answer(self, *args, **kwargs):  # noqa: ANN001
    try:
        return await _orig_answer(self, *args, **kwargs)
    except Exception as exc:  # noqa: BLE001
        logger.debug("callback.answer ignored (already acked/expired): %s", exc)
        return True


CallbackQuery.answer = _safe_answer  # type: ignore[method-assign]


class AckFirstMiddleware:
    """Acknowledge every callback_query BEFORE the handler runs.

    The Telegram client keeps the button spinner until ``answerCallbackQuery``
    arrives. Answering instantly (empty toast) makes the bot feel immediate
    even while DB/AI work continues in the handler.
    """

    async def __call__(self, handler, event, data):  # noqa: ANN001
        if isinstance(event, CallbackQuery):
            try:
                await event.answer()
            except Exception:  # noqa: BLE001
                pass
        return await handler(event, data)


_DISPATCHER.callback_query.outer_middleware(AckFirstMiddleware())


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
    """Feed one update to the process-wide cached Dispatcher.

    A fresh light-weight ``Bot`` is created per request (no network I/O at
    construction; the aiohttp session is created lazily on first API call and
    closed in ``finally``) so we never leak sessions across event loops.
    """
    bot = Bot(
        token=settings.telegram_bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    try:
        await _DISPATCHER.feed_update(bot, update)
    finally:
        try:
            session = getattr(bot, "session", None)
            if session is not None:
                await session.close()
        except Exception:  # noqa: BLE001
            pass


# ── Cron trigger (pg_cron or Vercel Cron → background jobs) ──
@_tg_router.api_route("/api/cron/{job}", methods=["GET", "POST"], include_in_schema=False)
async def cron_trigger(job: str, request: Request):
    """Endpoint pg_cron (or Vercel Cron) calls for scheduled jobs:

      * publish                   — publish due posts (every 1 min)
      * process-comment-queue     — drain the comment-reply queue (every 5 min)
      * check-instagram-health    — probe connected pages (every 10 min)
      * reset-daily-counters      — reports today's counters (00:00)
      * daily-report              — 23:59 Tehran admin report + alerts

    Guarded by the shared CRON_SECRET (sent as ``X-Cron-Secret``).
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
        except Exception:  # noqa: BLE001
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
        except Exception:  # noqa: BLE001
            logger.exception("health check failed")
            return Response(status_code=500, content='{"ok":false}')

    if job == "reset-daily-counters":
        # Reset is implicit: counters are keyed by Tehran date, so a new day
        # simply creates fresh rows. This endpoint reports the state.
        from app.core.database import SessionLocal
        from app.services.monitoring import _today_stats

        try:
            async with SessionLocal() as s:
                stats = await _today_stats(s)
            return {"ok": True, "date": stats["date"],
                    "replies_sent": stats["replies_sent"],
                    "queued": stats["queued"]}
        except Exception:  # noqa: BLE001
            logger.exception("reset-daily-counters failed")
            return Response(status_code=500, content='{"ok":false}')

    if job == "daily-report":
        from app.services.monitoring import build_and_send_daily_report

        try:
            report = await build_and_send_daily_report()
            return {"ok": report.get("ok", False), **report}
        except Exception:  # noqa: BLE001
            logger.exception("daily report failed")
            return Response(status_code=500, content='{"ok":false}')

    return Response(status_code=404, content="unknown job")


# ── Diagnostics (guarded by CRON_SECRET — never public) ──────
@_tg_router.get("/api/diag/db", include_in_schema=False)
async def diag_db(request: Request):
    """Live DB connectivity probe: SELECT 1 through the real engine.

    Returns the exact exception text (URL masked) so connectivity problems
    can be diagnosed without reading Vercel runtime logs.
    """
    cron_secret = get_settings().cron_secret
    if cron_secret:
        provided = request.headers.get("X-Cron-Secret", "")
        if not hmac.compare_digest(provided.encode(), cron_secret.encode()):
            return Response(status_code=403, content="forbidden")

    import re

    from sqlalchemy import text

    from app.core.database import SessionLocal

    url_txt = re.sub(r"://([^:@/]+):([^@/]+)@", "://\\1:****@", str(get_settings().database_url))
    out = {"database_url_masked": url_txt}
    try:
        async with SessionLocal() as s:
            val = (await s.execute(text("SELECT 1"))).scalar()
        out["ok"] = True
        out["select1"] = val
    except Exception as exc:  # noqa: BLE001
        out["ok"] = False
        out["error_type"] = type(exc).__name__
        out["error"] = re.sub(r"://([^:@/]+):([^@/]+)@", "://\\1:****@", str(exc))[:500]
    return out


@_tg_router.get("/api/diag/env", include_in_schema=False)
async def diag_env(request: Request):
    """Report which env vars are SET on the live function (names + lengths).

    Never returns values — only presence and length, so misconfiguration can
    be spotted without leaking secrets.
    """
    cron_secret = get_settings().cron_secret
    if cron_secret:
        provided = request.headers.get("X-Cron-Secret", "")
        if not hmac.compare_digest(provided.encode(), cron_secret.encode()):
            return Response(status_code=403, content="forbidden")

    keys = [
        "TELEGRAM_BOT_TOKEN", "TELEGRAM_WEBHOOK_SECRET", "ADMIN_TELEGRAM_IDS",
        "MINIAPP_PUBLIC_URL", "DATABASE_URL", "SUPABASE_URL",
        "SUPABASE_SERVICE_ROLE_KEY", "SUPABASE_ANON_KEY", "AVALAI_API_KEY",
        "AVALAI_BASE_URL", "AVALAI_MODEL", "CHATBOTX_ENABLED", "CHATBOTX_BASE_URL",
        "CHATBOTX_WORKSPACE_ID", "CHATBOTX_WORKSPACE_TOKEN",
        "CHATBOTX_API_CHANNEL_TOKEN", "RUN_MODE", "CRON_SECRET", "VERCEL",
        "INSTAGRAM_ACCOUNT_MODE", "ENCRYPTION_KEY",
    ]
    return {
        k: ({"set": bool(os.environ.get(k)), "len": len(os.environ.get(k, ""))})
        for k in keys
    }


# ── Mount the Instagram webhook/OAuth router too ─────────────
from app.webhook.webhook import router as instagram_router  # noqa: E402
from app.webhook.chatbotx import router as chatbotx_router  # noqa: E402

miniapp_app.include_router(_tg_router)
miniapp_app.include_router(instagram_router)
miniapp_app.include_router(chatbotx_router)

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
