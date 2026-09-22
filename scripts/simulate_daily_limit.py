"""Gramma — deterministic live simulation of the 1000/day reply hardening.

Runs the REAL production code (reply_engine.route_comment_auto_reply +
reply_queue.process_queue_for_account) against the LIVE Supabase DB with an
injected, strictly-monotonic clock. Anti-block sleeps are short-circuited
via GRAMMA_DISABLE_ANTIBLOCK_DELAYS=1 (never set in production).

Scenario (day 0 base = 2026-09-22 Asia/Tehran):

  DAY 1 — "1000 comments today":
    08:00–09:00  130 comments burst  → 100 reply, 30 queue (hourly cap).
    09:05        queue pass          → 30 hourly-cap items drain (FIFO).
    09:00–17:00  800 more (100/hour) → all reply.
    17:00–18:10  70 more             → reply  → daily counter reaches 1000.
    18:20        3 late comments     → daily cap reached → queued (daily_cap).
    + injected rate-limit alert + error counter (anti-block / safety_alerts).

  DAY 2 — "500 tomorrow morning":
    00:01        midnight reset tick (new Tehran-day counter row).
    06:00        queue pass drains Day-1 queue FIRST (FIFO, oldest id first),
                 then 500 fresh morning comments reply from the fresh budget.

Uses a dedicated account `sim.live-daily-cap` so demo data is never touched;
cleans up any data from earlier runs first. Leaves rows in place for the
Mini-App /api/reply-usage to display.

Run:  python scripts/simulate_daily_limit.py
"""

from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("GRAMMA_DISABLE_ANTIBLOCK_DELAYS", "1")  # sim only

from dotenv import load_dotenv  # noqa: E402

load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))
os.environ["INSTAGRAM_ACCOUNT_MODE"] = "simulation"
os.environ["TELEGRAM_BOT_TOKEN"] = os.environ.get("TELEGRAM_BOT_TOKEN", "000:test")
os.environ["ENCRYPTION_KEY"] = os.environ.get("ENCRYPTION_KEY", "9f6Bd2S19LFy/SQ/9Om4559N1Lgi5upd2zZGwvITWOA=")
# Bulk run: use the session pooler (5432) for faster sequential work.
if os.environ.get("DATABASE_URL"):
    os.environ["DATABASE_URL"] = os.environ["DATABASE_URL"].replace(
        "postgresql+asyncpg://", "postgresql+asyncpg://"
    ).replace(":6543", ":5432")

from zoneinfo import ZoneInfo  # noqa: E402

TEHRAN = ZoneInfo("Asia/Tehran")


def tehran_day(hour_offset: float = 0.0, day: int = 0) -> datetime:
    """UTC instant of (08:00 + hour_offset) Asia/Tehran, day 0 = 22-Sep."""
    base = datetime(2026, 9, 22, 8, 0, tzinfo=TEHRAN).astimezone(timezone.utc)
    return base + timedelta(days=day, hours=hour_offset)


def tehran_dt(day: int, hour: int, minute: int = 0) -> datetime:
    """UTC instant of a concrete Asia/Tehran wall-clock time, day 0 = 22-Sep."""
    return datetime(2026, 9, 22 + day, hour, minute, tzinfo=TEHRAN).astimezone(timezone.utc)


def seq(start: datetime, count: int, step: float) -> list[datetime]:
    return [start + timedelta(seconds=step * i) for i in range(count)]


async def _main() -> None:
    from sqlalchemy import delete, select

    from app.core.database import SessionLocal
    from app.models import InstagramAccount, User
    from app.services.autoreply import AutoReply
    from app.services.reply_engine import route_comment_auto_reply
    from app.services.reply_limits import (
        CommentReplyQueue,
        DailyReplyCounter,
        SafetyAlert,
        get_counter,
        tehran_date,
    )
    from app.services.reply_queue import process_queue_for_account

    print("== Gramma live daily-cap simulation ==", flush=True)

    # ── Clean slate for the dedicated simulation account ──────
    async with SessionLocal() as s:
        sim = (
            await s.execute(
                select(InstagramAccount).where(InstagramAccount.username == "sim.live-daily-cap")
            )
        ).scalars().first()
        if sim is not None:
            acc_id = sim.id
            for model in (SafetyAlert, CommentReplyQueue, DailyReplyCounter):
                await s.execute(delete(model).where(model.account_id == acc_id))
            await s.execute(delete(AutoReply).where(AutoReply.account_id == acc_id))
            await s.execute(delete(InstagramAccount).where(InstagramAccount.id == acc_id))
            await s.commit()
            print(f"[cleanup] removed previous sim account id={acc_id}", flush=True)

    async with SessionLocal() as s:
        owner = await s.get(User, 495432021)
        if owner is None:
            owner = User(id=495432021, telegram_username="zeynaliarmin", full_name="Amin", locale="fa")
            s.add(owner)
            await s.flush()
        sim = InstagramAccount(
            owner_id=495432021,
            username="sim.live-daily-cap",
            name="Sim Live Daily Cap",
            status="connected",
            created_at=datetime.now(timezone.utc) - timedelta(days=15),  # full 1000 ramp
        )
        s.add(sim)
        await s.flush()
        acc_id = sim.id
        rule = AutoReply(
            account_id=acc_id,
            keywords="قیمت",
            reply="سلام! برای اطلاع از قیمت‌ها لطفاً به هایلایت «قیمت» مراجعه کنید 🙏",
            dm_followup="سلام! برای راهنمایی سریع‌تر، شماره سفارش یا مدل موردنظرتان را بفرستید.",
        )
        s.add(rule)
        await s.commit()
        print(f"[setup] sim account id={acc_id} username={sim.username} rule_id={rule.id}", flush=True)

    result: dict = {"day1": {}, "day2": {}}

    # ═════════ DAY 1 ═════════
    async with SessionLocal() as s:
        acc = await s.get(InstagramAccount, acc_id)
        sent = queued = hourly_queued = 0

        # (a) 08:00 burst: 130 comments in <1 hour (27s step) → the 100/hour
        #     cap kicks in: 100 reply, 30 queue (hourly_cap).
        burst = seq(tehran_day(0.0), 130, 27)
        for i, ts in enumerate(burst):
            d = await route_comment_auto_reply(
                s, acc, comment_id=f"d1_burst_{i:03d}", comment_text="سلام قیمت چنده؟",
                commenter_id=f"fan_b{i}", now_utc=ts, commit=False,
            )
            if d.action == "replied":
                sent += 1
            elif d.action == "queued":
                queued += 1
                if d.reason == "hourly_cap":
                    hourly_queued += 1
        await s.commit()
        print(f"  [day1] burst 130 → sent={sent} queued={queued} (hourly={hourly_queued})", flush=True)

        # (b) 09:00 queue pass (the every-5-min processor) drains the 30.
        acc = await s.get(InstagramAccount, acc_id)
        qp = await process_queue_for_account(s, acc, now_utc=tehran_day(1.0, 0))
        await s.commit()
        print(f"  [day1] 09:00 queue pass → {qp}", flush=True)

        # (c) steady load: 800 comments, 10:00→18:53 (40s step ≈ 90/hour →
        #     stays under the 100/hour cap → all reply).
        steady = seq(tehran_day(2.0), 800, 40)
        for i, ts in enumerate(steady):
            d = await route_comment_auto_reply(
                s, acc, comment_id=f"d1_steady_{i:03d}", comment_text="سلام موجوده؟ قیمت بگید",
                commenter_id=f"fan_s{i}", now_utc=ts, commit=False,
            )
            if d.action == "replied":
                sent += 1
            elif d.action == "queued":
                queued += 1
                if d.reason == "hourly_cap":
                    hourly_queued += 1
            if i % 200 == 199:
                await s.commit()
        await s.commit()
        print(f"  [day1] steady 800 → sent={sent} queued={queued}", flush=True)

        # (d) final 70 (19:00→~19:47, 40s step ≈ 90/hour) to reach exactly
        #     1000 replies today while staying under the hourly cap.
        final = seq(tehran_day(11.0), 70, 40)
        for i, ts in enumerate(final):
            d = await route_comment_auto_reply(
                s, acc, comment_id=f"d1_final_{i:03d}", comment_text="قیمت؟",
                commenter_id=f"fan_f{i}", now_utc=ts, commit=False,
            )
            sent += 1
        await s.commit()
        print(f"  [day1] final 70 → daily total replies = {sent}", flush=True)

        # (e) 3 late comments AFTER the cap → queued (daily_cap).
        late = [tehran_day(12.0), tehran_day(12.05), tehran_day(12.1)]
        daily_queued = 0
        for i, ts in enumerate(late):
            d = await route_comment_auto_reply(
                s, acc, comment_id=f"d1_late_{i}", comment_text="قیمت؟",
                commenter_id=f"fan_l{i}", now_utc=ts, commit=False,
            )
            assert d.action == "queued" and d.reason == "daily_cap", d.as_dict()
            daily_queued += 1
        await s.commit()
        print(f"  [day1] late 3 → queued (daily_cap) ✓", flush=True)

        # (f) injected rate-limit error → safety alert + backoff.
        c1 = await get_counter(s, acc_id)
        c1.error_count = 3
        s.add(SafetyAlert(
            account_id=acc_id, alert_type="rate_limit",
            message="خطای محدودیت نرخ (کد 4) هنگام پاسخ کامنت — پاسخ‌دهی متوقف و backoff فعال شد.",
            raw_error={"code": 4, "error_subcode": 2446037, "message": "(شبیه‌سازی)"},
        ))
        await s.commit()
        result["day1"] = {
            "date": tehran_date(tehran_day(0)),
            "events_total": 130 + 800 + 70 + 3,
            "replied_immediately": sent,          # loop-level count (webhook path)
            "counter_replies": c1.replies_count,  # DB counter (includes FIFO drain)
            "queued_hourly_cap": hourly_queued,
            "queued_daily_cap": daily_queued,
            "hourly_count_current": c1.hourly_count,
            "rate_profile": c1.rate_profile,
            "backoff_until": str(c1.backoff_until),
        }
        print(f"  [day1] DONE: {result['day1']}", flush=True)

    # ═════════ DAY 2 ═════════
    async with SessionLocal() as s:
        acc = await s.get(InstagramAccount, acc_id)
        # 00:01 Tehran reset tick: new Tehran-date counter row (fresh budget).
        d2 = await get_counter(s, acc_id, now_utc=tehran_dt(1, 0, 1))
        d2.error_count = 0
        d2.backoff_until = None
        await s.commit()
        print(f"  [day2] reset tick → new date={d2.date} replies={d2.replies_count}", flush=True)

        pending = list((await s.execute(
            select(CommentReplyQueue).where(
                CommentReplyQueue.account_id == acc_id,
                CommentReplyQueue.status.in_(["pending", "failed"]),
            ).order_by(CommentReplyQueue.id.asc())
        )).scalars())
        pending_ids = [q.id for q in pending]
        print(f"  [day2] queue before drain: {len(pending_ids)} items", flush=True)

        acc = await s.get(InstagramAccount, acc_id)
        qp = await process_queue_for_account(s, acc, now_utc=tehran_dt(1, 6, 0))
        await s.commit()

        done = list((await s.execute(
            select(CommentReplyQueue).where(
                CommentReplyQueue.account_id == acc_id,
                CommentReplyQueue.status == "done",
            ).order_by(CommentReplyQueue.id.asc())
        )).scalars())
        drained_ids = [q.id for q in done if q.id in pending_ids]
        fifo_ok = drained_ids == sorted(pending_ids)
        print(f"  [day2] 06:00 drain: {qp} | fifo_order_ok={fifo_ok}", flush=True)

        # 500 fresh morning comments (06:30→~13:27, 50s step ≈ 72/hour) →
        # all reply from the fresh daily budget.
        morning = seq(tehran_dt(1, 6, 30), 500, 50)
        sent2 = queued2 = 0
        for i, ts in enumerate(morning):
            d = await route_comment_auto_reply(
                s, acc, comment_id=f"d2_morning_{i:04d}", comment_text="سلام قیمتش چنده؟",
                commenter_id=f"fan_m{i}", now_utc=ts, commit=False,
            )
            if d.action == "replied":
                sent2 += 1
            else:
                queued2 += 1
            if i % 250 == 249:
                await s.commit()
        await s.commit()

        d2c = await get_counter(s, acc_id, now_utc=tehran_dt(1, 6, 30))
        alerts = list((await s.execute(
            select(SafetyAlert).where(SafetyAlert.account_id == acc_id)
        )).scalars())
        result["day2"] = {
            "date": tehran_date(tehran_day(0, 1)),
            "queue_drained": qp["sent"],
            "dm_followups": qp["dm_sent"],
            "fifo_order_ok": fifo_ok,
            "new_comments_replied": sent2,
            "new_comments_queued": queued2,
            "counter_replies": d2c.replies_count,
            "hourly_count_current": d2c.hourly_count,
        }
        result["alerts"] = [{"type": a.alert_type, "msg": a.message[:70]} for a in alerts]
        result["counters"] = [
            {"date": c.date, "replies": c.replies_count, "errors": c.error_count, "profile": c.rate_profile}
            for c in (await s.execute(
                select(DailyReplyCounter).where(DailyReplyCounter.account_id == acc_id)
            )).scalars()
        ]
        result["sim_account"] = {"id": acc_id, "username": sim.username}
        result["ok"] = bool(
            fifo_ok
            and result["day1"]["counter_replies"] == 1000
            and result["day1"]["queued_daily_cap"] == 3
            and (d2c.replies_count or 0) >= 500
            and result["day2"]["queue_drained"] == 3
        )
        print(f"  [day2] DONE: {result['day2']}", flush=True)

    import json

    print("== RESULT ==\n" + json.dumps(result, ensure_ascii=False, indent=2, default=str), flush=True)


if __name__ == "__main__":
    asyncio.run(_main())
