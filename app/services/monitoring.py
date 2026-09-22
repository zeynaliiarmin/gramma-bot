"""Gramma — safety monitoring: health checks, daily report, admin alerts.

Covers the production-hardening monitoring requirements:

  * ``check_account_health`` — probes the Instagram connection of each
    connected page (every ~10 min) and raises a ``safety_alerts`` row if a
    blocking error is seen;
  * ``build_daily_report`` — builds the 23:59 Tehran admin report with
    successful replies, errors by type, queued comments and page health,
    and fires an immediate alert when the error rate exceeds 5%;
  * ``send_safety_alert`` — notifies the configured admin ids right away.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.services import anti_block, limits
from app.services.anti_block import classify_instagram_error
from app.services.reply_limits import (
    CommentReplyQueue,
    DailyReplyCounter,
    QUEUE_DONE,
    QUEUE_FAILED,
    QUEUE_PENDING,
    QUEUE_PROCESSING,
    SafetyAlert,
    get_counter,
    record_reply_error,
    tehran_date,
)

logger = logging.getLogger("gramma.monitoring")
settings = get_settings()


async def check_account_health(account) -> dict:
    """Probe one account's IG connection; return a small health dict."""
    from app.services.meta.service import InstagramService

    result = {"account_id": account.id, "ok": True, "error": None, "kind": None}
    try:
        info = await InstagramService().get_account_info(account)
        if isinstance(info, dict) and info.get("error"):
            result["ok"] = False
            result["kind"] = classify_instagram_error(info.get("error"))
            result["error"] = str(info.get("error"))[:500]
    except Exception as exc:  # noqa: BLE001
        result["ok"] = False
        result["kind"] = classify_instagram_error(exc)
        result["error"] = str(exc)[:500]
    return result


async def check_all_accounts_health() -> list[dict]:
    """Health pass over every connected account (10-minute tick)."""
    from app.core.database import SessionLocal
    from app.models import InstagramAccount

    reports: list[dict] = []
    async with SessionLocal() as session:
        accounts = list((
            await session.execute(
                select(InstagramAccount).where(InstagramAccount.status == "connected")
            )
        ).scalars())
        for acc in accounts:
            rep = await check_account_health(acc)
            reports.append(rep)
            if not rep["ok"] and rep["kind"]:
                await create_alert(
                    session,
                    account_id=acc.id,
                    alert_type=rep["kind"],
                    message=f"بررسی سلامت پیج: خطای {rep['kind']} شناسایی شد.",
                    raw_error={"error": rep["error"]},
                    notify=True,
                )
                await record_reply_error(session, acc.id)
        await session.commit()
    return reports


async def create_alert(
    session: AsyncSession,
    *,
    account_id: int | None,
    alert_type: str,
    message: str,
    raw_error: dict | None = None,
    notify: bool = True,
) -> SafetyAlert:
    alert = SafetyAlert(
        account_id=account_id,
        alert_type=alert_type,
        message=message,
        raw_error=raw_error,
    )
    session.add(alert)
    await session.flush()
    if notify:
        await send_safety_alert(account_id, alert_type, message)
    return alert


async def send_safety_alert(
    account_id: int | None, alert_type: str, message: str
) -> bool:
    """Push an immediate Persian alert to the configured admin ids."""
    from app.services.broadcast import notify_admin

    text = (
        "🚨 <b>هشدار ایمنی Gramma</b>\n\n"
        f"نوع: <code>{alert_type}</code>\n"
        f"پیج: <code>{account_id or '—'}</code>\n"
        f"پیام: {message}\n\n"
        "پاسخ‌دهی خودکار برای این پیج متوقف شد. جزئیات در پنل → هشدارها."
    )
    try:
        return await notify_admin(text)
    except Exception as exc:  # noqa: BLE001
        logger.warning("safety alert notify failed: %s", exc)
        return False


def _error_rate(counter: DailyReplyCounter | None) -> float:
    if counter is None:
        return 0.0
    total = (counter.replies_count or 0) + (counter.error_count or 0)
    if total == 0:
        return 0.0
    return (counter.error_count or 0) / total


async def _today_stats(session: AsyncSession) -> dict:
    today = tehran_date()
    res = await session.execute(
        select(DailyReplyCounter).where(DailyReplyCounter.date == today)
    )
    counters = list(res.scalars())
    sent = sum(c.replies_count or 0 for c in counters)
    errors = sum(c.error_count or 0 for c in counters)
    queued = list((
        await session.execute(
            select(CommentReplyQueue).where(
                CommentReplyQueue.status.in_([QUEUE_PENDING, QUEUE_PROCESSING])
            )
        )
    ).scalars())
    done = list((
        await session.execute(
            select(CommentReplyQueue).where(CommentReplyQueue.status == QUEUE_DONE)
        )
    ).scalars())
    failed = list((
        await session.execute(
            select(CommentReplyQueue).where(CommentReplyQueue.status == QUEUE_FAILED)
        )
    ).scalars())
    unresolved_alerts = list((await session.execute(
        select(SafetyAlert).where(SafetyAlert.resolved.is_(False))
    )).scalars())
    overall = 0.0
    if sent + errors:
        overall = round(errors / (sent + errors), 4)
    return {
        "date": today,
        "replies_sent": sent,
        "errors": errors,
        "error_rate": overall,
        "queued": len(queued),
        "processed": len(done),
        "failed": len(failed),
        "unresolved_alerts": len(unresolved_alerts),
        "counters": counters,
    }


async def build_and_send_daily_report() -> dict:
    """The 23:59 Tehran report, pushed to the admin ids."""
    from app.core.database import SessionLocal
    from app.services.broadcast import notify_admin

    async with SessionLocal() as session:
        stats = await _today_stats(session)
        counters = stats["counters"]

        # Immediate alert when error rate > 5%.
        for c in counters:
            if _error_rate(c) > limits.ALERT_ERROR_RATE:
                await create_alert(
                    session,
                    account_id=c.account_id,
                    alert_type="high_error_rate",
                    message=(
                        f"نرخ خطای امروز {_error_rate(c):.1%} از آستانه "
                        f"{limits.ALERT_ERROR_RATE:.0%} عبور کرد."
                    ),
                    raw_error={"date": stats["date"], "rate": _error_rate(c)},
                    notify=True,
                )

        # Account health line.
        from app.models import InstagramAccount

        accounts = list((
            await session.execute(
                select(InstagramAccount).where(InstagramAccount.status == "connected")
            )
        ).scalars())
        health_lines = []
        for a in accounts:
            healthy = "✅" if a.status == "connected" else "⚠️"
            health_lines.append(f"{healthy} {a.username or a.name or a.id}")

        lines = [
            "📊 <b>گزارش روزانه Gramma</b>",
            f"تاریخ: <code>{stats['date']}</code>",
            "",
            f"✅ پاسخ‌های موفق: <b>{stats['replies_sent']}</b>",
            f"❌ خطاها: <b>{stats['errors']}</b>",
            f"⏳ در صف (اجرانشده): <b>{stats['queued']}</b>",
            f"🛠 پردازش‌شده: <b>{stats['processed']}</b> · ناموفق: <b>{stats['failed']}</b>",
            f"🚨 هشدارهای باز: <b>{stats['unresolved_alerts']}</b>",
            "",
            "<b>سلامت پیج‌ها:</b>",
            *health_lines,
        ]
        await session.commit()

    from app.services.broadcast import notify_admin

    text = "\n".join(lines)
    try:
        await notify_admin(text)
        return {"ok": True, **{k: v for k, v in stats.items() if k != "counters"}}
    except Exception as exc:  # noqa: BLE001
        logger.warning("daily report send failed: %s", exc)
        return {"ok": False, "error": str(exc)[:200]}
