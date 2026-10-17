"""Durable, admin-private Telegram media drafts; never a publishing queue.

An album's separate Telegram updates share one row and ordered JSON media.
Only an explicit owner confirmation can create an outbound_queue post job.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import BigInteger, Boolean, DateTime, Index, Integer, JSON, String, Text, UniqueConstraint, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import BIGINT_PK, Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _expires() -> datetime:
    # Lazy import avoids a models <-> limits initialization cycle.
    from app.services.limits import TELEGRAM_DRAFT_EXPIRY_DAYS

    return _utcnow() + timedelta(days=TELEGRAM_DRAFT_EXPIRY_DAYS)


class TelegramPostDraft(Base):
    __tablename__ = "telegram_post_drafts"

    id: Mapped[int] = mapped_column(BIGINT_PK, primary_key=True, autoincrement=True)
    source_key: Mapped[str] = mapped_column(String(180), nullable=False, unique=True)
    owner_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    local_account_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    expected_username: Mapped[str] = mapped_column(String(64), nullable=False)
    media_group_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    media: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON().with_variant(JSONB, "postgresql"), nullable=False,
        default=list, server_default=text("'[]'"),
    )
    caption: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default=text("''"))
    caption_from_reply: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false"),
    )
    options: Mapped[dict[str, Any]] = mapped_column(
        JSON().with_variant(JSONB, "postgresql"), nullable=False,
        default=dict, server_default=text("'{}'"),
    )
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="collecting", server_default=text("'collecting'"),
    )
    queue_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    panel_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    prompt_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    prompt_kind: Mapped[str | None] = mapped_column(String(24), nullable=True)
    prompt_slide: Mapped[int | None] = mapped_column(Integer, nullable=True)
    notify_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default=text("0"))
    last_prompt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, server_default=func.now(), nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, server_default=func.now(), nullable=False,
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_expires, nullable=False)

    __table_args__ = (
        UniqueConstraint("chat_id", "media_group_id", name="uq_telegram_post_drafts_album"),
        Index("ix_telegram_post_drafts_due", "status", "updated_at"),
        Index("ix_telegram_post_drafts_owner", "owner_id", "created_at"),
    )
