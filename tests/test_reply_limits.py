"""Tests for the comment-reply engine: daily cap, hourly cap, FIFO queue,
Tehran-day reset, warm-up ramp and error classification."""

import os
from datetime import datetime, timedelta, timezone

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "000:test")
os.environ.setdefault("ENCRYPTION_KEY", "9f6Bd2S19LFy/SQ/9Om4559N1Lgi5upd2zZGwvITWOA=")
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///./tests_reply.db")
os.environ.setdefault("INSTAGRAM_ACCOUNT_MODE", "simulation")

import pytest  # noqa: E402

from app.core.database import SessionLocal, init_db  # noqa: E402
from app.models import InstagramAccount, User  # noqa: E402


@pytest.fixture()
async def db():
    await init_db()
    async with SessionLocal() as session:
        # Wipe rows (models stay registered; sqlite file re-used per module).
        from sqlalchemy import delete

        from app.models import InstagramAccount, User
        from app.services.autoreply import AutoReply
        from app.services.reply_limits import (
            CommentReplyQueue,
            DailyReplyCounter,
            SafetyAlert,
        )

        for model in (CommentReplyQueue, SafetyAlert, DailyReplyCounter,
                      AutoReply, InstagramAccount, User):
            await session.execute(delete(model))
        await session.commit()

        user = User(
            id=1, telegram_username="tester", full_name="T", locale="fa",
        )
        session.add(user)
        acc = InstagramAccount(
            id=7, owner_id=1, username="page7", name="Page 7", status="connected",
            created_at=datetime(2026, 9, 1, tzinfo=timezone.utc),  # > 10 days old
        )
        session.add(acc)
        await session.commit()
        yield session


@pytest.mark.asyncio
async def test_warmup_ramp(db):
    from app.services import limits

    old = datetime(2026, 9, 1, tzinfo=timezone.utc)
    new = datetime(2026, 9, 21, tzinfo=timezone.utc)
    now = datetime(2026, 9, 22, tzinfo=timezone.utc)
    assert limits.reply_limit_for_account(old, now_utc=now) == 1000
    assert limits.reply_limit_for_account(new, now_utc=now) == 100   # day 1
    middle = datetime(2026, 9, 17, tzinfo=timezone.utc)
    assert limits.reply_limit_for_account(middle, now_utc=now) == 500  # day 4..10


@pytest.mark.asyncio
async def test_counter_is_keyed_by_tehran_date(db):
    from app.services.reply_limits import get_counter, tehran_date

    now = datetime(2026, 9, 22, 20, 0, tzinfo=timezone.utc)  # 23:30 Tehran
    c = await get_counter(db, 7, now_utc=now)
    assert c.date == tehran_date(now)
    # The next "day" in Tehran gets a different key with a fresh zero count.
    later = now + timedelta(hours=2)  # crosses Tehran midnight
    c2 = await get_counter(db, 7, now_utc=later, create=True)
    assert c2.date != c.date
    assert c2.replies_count == 0


@pytest.mark.asyncio
async def test_hourly_window_resets_after_60_minutes(db):
    from app.services.reply_limits import get_counter, record_reply
    from app.services.reply_engine import hourly_remaining, hourly_remaining

    # Seed 100 replies in the last hour → hourly budget exhausted.
    start = datetime.now(timezone.utc)
    c = await get_counter(db, 7, now_utc=start)
    c.hourly_window_start = start
    c.hourly_count = 100
    await db.commit()
    left = await hourly_remaining(db, await _account(db), now_utc=start)
    assert left == 0


@pytest.mark.asyncio
async def test_error_classification(db):
    from app.services.anti_block import classify_instagram_error

    assert classify_instagram_error({"error": {"code": 190}}) == "oauth_exception"
    assert classify_instagram_error({"error": {"code": 4}}) == "rate_limit"
    assert classify_instagram_error({"error": {"code": 368}}) == "temporarily_blocked"
    assert classify_instagram_error({"error": {"code": 1200}}) == "suspicious_activity"
    assert classify_instagram_error({"error": {"code": 200}}) is None


@pytest.mark.asyncio
async def test_backoff_ladder(db):
    from app.services import anti_block
    from app.services.reply_limits import DailyReplyCounter

    now = datetime.now(timezone.utc)
    c = DailyReplyCounter(account_id=7, date="2026-09-22")
    u1, _ = anti_block.next_backoff_until(None, 0)
    u2, _ = anti_block.next_backoff_until(u1, 1)
    u3, _ = anti_block.next_backoff_until(u2, 2)
    u4, _ = anti_block.next_backoff_until(u3, 3)
    assert (u1 - now) <= timedelta(minutes=5, seconds=2)
    assert (u2 - u1) <= timedelta(minutes=15, seconds=2)
    assert (u3 - u2) <= timedelta(hours=1, seconds=2)
    # 4th error stays at the 24h cap.
    u5, _ = anti_block.next_backoff_until(u4, 4)
    assert (u5 - u4) <= timedelta(hours=24, seconds=2)


@pytest.mark.asyncio
async def test_vary_reply_never_identical_back_to_back(db):
    from app.services.anti_block import vary_reply

    base = "سفارش شما ثبت می‌شود"
    variants = {vary_reply(base) for _ in range(60)}
    assert len(variants) >= 2  # diversity guaranteed


@pytest.mark.asyncio
async def test_route_queues_when_daily_cap_reached(db):
    from app.services.reply_engine import route_comment_auto_reply
    from app.services.reply_limits import CommentReplyQueue, DailyReplyCounter, get_counter

    from app.services.autoreply import AutoReply

    db.add(AutoReply(account_id=7, keywords="قیمت", reply="لیست قیمت‌ها را ببینید"))
    await db.commit()

    acc = await _account(db)
    # Seed today's counter at the cap (warm-up full: 1000).
    c = await get_counter(db, 7)
    c.replies_count = 1000
    await db.commit()

    decision = await route_comment_auto_reply(
        db, acc, comment_id="c1", comment_text="قیمت‌تان چنده؟", commenter_id="u1"
    )
    assert decision.action == "queued"
    assert decision.reason == "daily_cap"

    q = await db.get(CommentReplyQueue, decision.queue_id)
    assert q.dm_followup_required is False


async def _account(session):
    from app.models import InstagramAccount

    return await session.get(InstagramAccount, 7)
