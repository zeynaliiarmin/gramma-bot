"""Gramma — smart auto-replies for DMs.

* Each account can hold a set of keyword → reply rules (`AutoReply` rows).
* FAQ replies are offered as quick buttons ("Smart Replies") using a simple
  FAQ bank, so unsupported questions still get a warm canned answer.
* The webhook event consumer can trigger these automatically for incoming
  messages (see webhook.py).

Rules are always evaluated in-process — no external AI required (LLM is
optional for caption generation, never a hard dependency for replies).
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import BigInteger, Boolean, DateTime, String, Text, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base, BIGINT_PK


class AutoReply(Base):
    """A keyword-triggered DM auto-reply rule for one account."""

    __tablename__ = "auto_replies"

    id: Mapped[int] = mapped_column(BIGINT_PK, primary_key=True, autoincrement=True)
    account_id: Mapped[int] = mapped_column(BigInteger, index=True)
    keywords: Mapped[str] = mapped_column(Text, default="")   # comma separated
    reply: Mapped[str] = mapped_column(Text, default="")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    matches: Mapped[int] = mapped_column(BigInteger, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


async def find_reply(session: AsyncSession, account_id: int, text: str) -> str | None:
    """Return the first enabled rule whose keywords match `text` (or None)."""
    result = await session.execute(
        select(AutoReply).where(
            AutoReply.account_id == account_id, AutoReply.enabled.is_(True)
        )
    )
    low = (text or "").lower()
    for rule in result.scalars():
        kws = [k.strip() for k in (rule.keywords or "").split(",") if k.strip()]
        if any(k.lower() in low for k in kws):
            rule.matches = (rule.matches or 0) + 1
            await session.commit()
            return rule.reply
    return None


async def add_rule(session: AsyncSession, account_id: int, keywords: str, reply: str) -> AutoReply:
    rule = AutoReply(account_id=account_id, keywords=keywords, reply=reply)
    session.add(rule)
    await session.flush()
    return rule


FAQ_BANK = [
    ("قیمت", "برای اطلاع از قیمت‌ها، لطفاً به هایلایت «قیمت» مراجعه کنید یا بفرمایید کدام محصول؟"),
    ("سفارش", "سفارش شما ثبت می‌شود؛ لطفاً مشخصات و آدرس را بفرستید 🙏"),
    ("ارسال", "ارسال سفارش‌ها ۲ تا ۴ روز کاری طول می‌کشد 📦"),
    ("ساعت کاری", "ساعت پاسخ‌گویی ما: ۹ صبح تا ۹ شب، همه‌روزه."),
    ("محصول", "به‌روزترین محصولات را در پست‌های اخیر پیج می‌بینید ✨"),
]
