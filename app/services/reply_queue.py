"""Gramma — FIFO comment-reply queue processor (runs every ~5 minutes).

Pulls the oldest pending items per account and replies while the day's
budget lasts. Two classes of queue items are handled:

  * plain comment reply (``dm_followup_required`` = false), and
  * comment reply + DM follow-up (comment first, DM second — both queued).

Anti-Block behaviour (delays, rest breaks, error backoff, response
diversity) is delegated to :mod:`app.services.anti_block`.

The processor is idempotent: crashed/mid-flight items are requeued and only
deleted from ``pending``/``processing`` lookups once marked ``done``.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.services import anti_block, limits
from app.services.reply_engine import RouteDecision  # noqa: F401 (re-export)
from app.services.reply_limits import (
    CommentReplyQueue,
    DailyReplyCounter,
    QUEUE_DONE,
    QUEUE_FAILED,
    QUEUE_PENDING,
    QUEUE_PROCESSING,
    SafetyAlert,
    get_counter,
    record_reply,
    record_reply_error,
)

logger = logging.getLogger("gramma.reply_queue")
settings = get_settings()


async def _pending_items(session: AsyncSession, account_id: int, limit_: int = 100):
    res = await session.execute(
        select(CommentReplyQueue)
        .where(CommentReplyQueue.account_id == account_id)
        .where(CommentReplyQueue.status.in_([QUEUE_PENDING, QUEUE_FAILED]))
        .order_by(CommentReplyQueue.id.asc())
        .limit(limit_)
    )
    return list(res.scalars())


def _reset_hourly(counter: DailyReplyCounter, now: datetime) -> None:
    if counter.hourly_window_start is None:
        counter.hourly_window_start = now
        counter.hourly_count = 0
        return
    if counter.hourly_window_start.tzinfo is None:
        counter.hourly_window_start = counter.hourly_window_start.replace(tzinfo=timezone.utc)
    if (now - counter.hourly_window_start).total_seconds() >= 3600:
        counter.hourly_window_start = now
        counter.hourly_count = 0


def _available_daily(counter: DailyReplyCounter, account, now: datetime) -> int:
    cap = limits.reply_limit_for_account(account.created_at, now_utc=now)
    return max(0, cap - (counter.replies_count or 0))


def _available_hourly(counter: DailyReplyCounter, now: datetime) -> int:
    _reset_hourly(counter, now)
    return max(0, limits.HOURLY_REPLY_LIMIT - (counter.hourly_count or 0))


async def process_queue_for_account(
    session: AsyncSession, account, *, now_utc: datetime | None = None
) -> dict:
    """Process one account's queued replies while its budget lasts.

    Returns a summary dict {sent, queued_failed, dm_sent, remaining_cap}.
    ``now_utc`` is injectable so the midnight-rollover simulation can drive
    the processor deterministically.
    """
    from app.services.meta.service import InstagramService

    now = now_utc or datetime.now(timezone.utc)
    counter = await get_counter(session, account.id, now_utc=now)
    profile = counter.rate_profile or anti_block.draw_rate_profile()
    counter.rate_profile = profile

    summary = {"sent": 0, "dm_sent": 0, "failed": 0, "skipped": 0,
               "remaining_cap": 0}

    if anti_block.in_backoff(counter):
        summary["reason"] = "in_backoff"
        return summary

    items = await _pending_items(session, account.id)
    svc = InstagramService()
    processed_ids: list[int] = []

    for item in items:
        # Re-check the budget before every action.
        daily_left = _available_daily(counter, account, now)
        hourly_left = _available_hourly(counter, now)
        summary["remaining_cap"] = daily_left
        if daily_left <= 0:
            summary["reason"] = "daily_cap"
            break
        if hourly_left <= 0:
            summary["reason"] = "hourly_cap"
            break
        if anti_block.in_backoff(counter):
            summary["reason"] = "in_backoff"
            break

        item.status = QUEUE_PROCESSING
        await session.flush()

        # Human-like delay between two queued actions.
        if summary["sent"] > 0 or processed_ids:
            await anti_block.human_delay_wait(profile)

        try:
            # Re-derive a fresh, varied reply via the stored rule.
            reply_text = await _reply_text_for_item(session, item)
            if not reply_text:
                reply_text = "پاسخ شما ثبت شد؟ برای اطلاعات بیشتر در خدمتیم 🙏"
            await svc.reply_comment(account, item.comment_id, reply_text)
            await record_reply(session, account.id, now_utc=now)
            await anti_block.mark_success(counter)

            if item.dm_followup_required and item.dm_message:
                await anti_block.human_delay_wait(profile)
                try:
                    # DM as a follow-up to *this comment* (Instagram's
                    # `/{comment_id}/private_replies` endpoint) — after the
                    # public comment reply succeeded.
                    await svc.private_reply(account, item.comment_id, item.dm_message)
                    summary["dm_sent"] += 1
                except Exception as exc:  # noqa: BLE001
                    await record_reply_error(session, account.id)
                    await anti_block.mark_error(counter)
                    logger.warning("queued dm followup failed: %s", exc)
            item.status = QUEUE_DONE
            item.processed_at = now
            summary["sent"] += 1
        except Exception as exc:  # noqa: BLE001
            item.retry_count = (item.retry_count or 0) + 1
            item.error_message = (str(exc) or "reply failed")[:2000]
            await record_reply_error(session, account.id)
            kind = anti_block.classify_instagram_error(exc)
            await anti_block.mark_error(counter)
            if kind:
                await _raise_alert(session, account.id, kind, str(exc), item)
                # Blocking error → stop this account's processing this round.
                item.status = QUEUE_FAILED
                summary["failed"] += 1
                break
            item.status = QUEUE_FAILED
            summary["failed"] += 1
        processed_ids.append(item.id)

    await session.commit()
    return summary


async def _reply_text_for_item(session: AsyncSession, item: CommentReplyQueue) -> str | None:
    from app.services.autoreply import get_rule_by_id_cached

    if item.auto_reply_id:
        rule = await get_rule_by_id_cached(session, item.account_id, item.auto_reply_id)
        if rule is not None and rule.reply:
            return anti_block.vary_reply(rule.reply)
    return None


async def _raise_alert(
    session: AsyncSession, account_id: int | None, kind: str, raw: str,
    item: CommentReplyQueue | None,
) -> None:
    alert = SafetyAlert(
        account_id=account_id,
        alert_type=kind,
        message=(
            f"خطای اینستاگرام هنگام پاسخ کامنت (کد {kind}). "
            "پاسخ‌دهی متوقف و backoff فعال شد."
        ),
        raw_error={"error": raw[:2000], "queue_id": item.id if item else None},
    )
    session.add(alert)
    await session.flush()


async def run_queue_pass() -> dict:
    """Process the queue for every eligible account (5-minute tick)."""
    from sqlalchemy import select as _select

    from app.core.database import SessionLocal
    from app.models import InstagramAccount

    totals = {"accounts": 0, "sent": 0, "dm_sent": 0, "failed": 0}
    async with SessionLocal() as session:
        accounts = list((
            await session.execute(
                _select(InstagramAccount).where(
                    InstagramAccount.status == "connected"
                )
            )
        ).scalars())
        for acc in accounts:
            summary = await process_queue_for_account(session, acc)
            totals["accounts"] += 1
            totals["sent"] += summary.get("sent", 0)
            totals["dm_sent"] += summary.get("dm_sent", 0)
            totals["failed"] += summary.get("failed", 0)
    return totals
