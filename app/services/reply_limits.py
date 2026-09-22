"""Gramma — daily comment-reply counting, FIFO queue, and safety alerts.

Implements the production hardening requirements:

* every comment reply is counted against the *per-account, per-Tehran-day*
  budget (``DAILY_REPLY_LIMIT``) and the rolling hourly budget
  (``HOURLY_REPLY_LIMIT``);
* when the budget allows, the reply is sent immediately; otherwise it is
  enqueued in ``comment_reply_queue`` (FIFO) and processed later;
* an optional DM follow-up rides along in the same queue item and is sent
  strictly *after* the comment reply;
* a per-account rate profile is drawn once per Tehran day from the
  running ``daily_reply_counters`` row (it also seeds the occasional
  5–15 min rest break, tracked via the hourly window);
* Instagram error codes are classified into ``safety_alerts`` rows.

Everything that must be capped lives in :mod:`app.services.limits` — this
module only *enforces* those single-source caps.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
    select,
)
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.dialects.postgresql import JSONB

from app.core.database import Base, BIGINT_PK

logger = logging.getLogger("gramma.reply_limits")

# JSONB on Postgres; plain JSON on SQLite so tests still create the table.
JSONB_PORTABLE = JSONB().with_variant(JSON, "sqlite")

# ── Row statuses (portable strings) ──────────────────────────
QUEUE_PENDING = "pending"
QUEUE_PROCESSING = "processing"
QUEUE_DONE = "done"
QUEUE_FAILED = "failed"


class CommentReplyQueue(Base):
    """A comment reply (optionally + DM follow-up) waiting for the cap."""

    __tablename__ = "comment_reply_queue"

    id: Mapped[int] = mapped_column(BIGINT_PK, primary_key=True, autoincrement=True)
    account_id: Mapped[int] = mapped_column(BigInteger, index=True)
    comment_id: Mapped[str] = mapped_column(String(64), default="")
    comment_text: Mapped[str] = mapped_column(Text, default="")
    auto_reply_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    dm_followup_required: Mapped[bool] = mapped_column(Boolean, default=False)
    dm_message: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(16), default=QUEUE_PENDING, index=True)
    retry_count: Mapped[int] = mapped_column(Integer, default=0)
    error_message: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    scheduled_for: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class DailyReplyCounter(Base):
    """Per-account, per-Tehran-day reply usage + rate profile + hourly window."""

    __tablename__ = "daily_reply_counters"
    __table_args__ = (UniqueConstraint("account_id", "date", name="uq_daily_reply_account_date"),)

    id: Mapped[int] = mapped_column(BIGINT_PK, primary_key=True, autoincrement=True)
    account_id: Mapped[int] = mapped_column(BigInteger, index=True)
    date: Mapped[str] = mapped_column(String(10), default="")  # YYYY-MM-DD (Tehran)
    replies_count: Mapped[int] = mapped_column(Integer, default=0)
    hourly_count: Mapped[int] = mapped_column(Integer, default=0)
    hourly_window_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    rate_profile: Mapped[str] = mapped_column(String(16), default="medium")
    rest_at_action: Mapped[int] = mapped_column(Integer, default=0)  # next action idx that triggers a rest
    last_action_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error_count: Mapped[int] = mapped_column(Integer, default=0)
    backoff_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    backoff_runs: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


class SafetyAlert(Base):
    """A safety incident (Instagram error, spike, etc.) shown to the admin."""

    __tablename__ = "safety_alerts"

    id: Mapped[int] = mapped_column(BIGINT_PK, primary_key=True, autoincrement=True)
    account_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, index=True)
    alert_type: Mapped[str] = mapped_column(String(48), default="info")
    message: Mapped[str] = mapped_column(Text, default="")
    raw_error: Mapped[dict | None] = mapped_column(JSONB_PORTABLE, nullable=True)
    resolved: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


# ── Helpers ───────────────────────────────────────────────────

def tehran_date(now_utc: datetime | None = None) -> str:
    """Return today's YYYY-MM-DD in the Asia/Tehran timezone."""
    from zoneinfo import ZoneInfo

    from app.core.config import get_settings

    tz = ZoneInfo(get_settings().timezone)
    return (now_utc or datetime.now(timezone.utc)).astimezone(tz).strftime("%Y-%m-%d")


async def get_counter(
    session: AsyncSession, account_id: int, *, now_utc: datetime | None = None, create: bool = True
) -> DailyReplyCounter | None:
    """Load (and optionally create) today's counter row for one account.

    Uses a session-scoped cache so consecutive calls inside one request (or
    one queue-pass) reuse the same instance instead of re-SELECTing every
    time — an important saving on the Supabase transaction pooler where each
    round-trip costs ~tens of ms.
    """
    key = (account_id, tehran_date(now_utc))
    cache: dict = session.info.get("reply_counter_cache") or {}
    if key in cache:
        return cache[key]
    res = await session.execute(
        select(DailyReplyCounter).where(
            DailyReplyCounter.account_id == account_id,
            DailyReplyCounter.date == key[1],
        )
    )
    counter = res.scalars().first()
    if counter is None and create:
        counter = DailyReplyCounter(account_id=account_id, date=key[1])
        session.add(counter)
        await session.flush()
    cache[key] = counter
    session.info["reply_counter_cache"] = cache
    return counter


async def record_reply(
    session: AsyncSession, account_id: int, *, now_utc: datetime | None = None
) -> DailyReplyCounter:
    """Bump today's counter + the rolling hourly window, resetting as needed."""
    now = now_utc or datetime.now(timezone.utc)
    counter = await get_counter(session, account_id, now_utc=now)
    counter.replies_count = (counter.replies_count or 0) + 1

    # Rolling 60-minute window.
    if counter.hourly_window_start is None or (
        now - counter.hourly_window_start).total_seconds() >= 3600:
        counter.hourly_window_start = now
        counter.hourly_count = 0
    counter.hourly_count = (counter.hourly_count or 0) + 1
    counter.last_action_at = now
    await session.flush()
    return counter


async def record_reply_error(session: AsyncSession, account_id: int | None) -> None:
    """Track an error for the daily >5% alert threshold."""
    if account_id is None:
        return
    counter = await get_counter(session, account_id)
    counter.error_count = (counter.error_count or 0) + 1
    await session.flush()
