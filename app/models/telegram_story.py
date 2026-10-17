"""Private durable Story FSM. No Telegram upload or story sequence publishes by itself."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import BigInteger, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint, func, text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import BIGINT_PK, Base


def _expiry() -> datetime:
    from app.services.limits import TELEGRAM_DRAFT_EXPIRY_DAYS

    return datetime.now(timezone.utc) + timedelta(days=TELEGRAM_DRAFT_EXPIRY_DAYS)


class TelegramStorySequence(Base):
    __tablename__ = "telegram_story_sequences"

    id: Mapped[int] = mapped_column(BIGINT_PK, primary_key=True, autoincrement=True)
    owner_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    local_account_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    expected_username: Mapped[str] = mapped_column(String(64), nullable=False)
    declared_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, server_default=text("'awaiting_count'"))
    last_album_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    invalid_media_group_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_expiry, nullable=False)

    __table_args__ = (Index("ix_story_sequence_owner", "owner_id", "status"),)


class TelegramStorySlide(Base):
    __tablename__ = "telegram_story_slides"

    id: Mapped[int] = mapped_column(BIGINT_PK, primary_key=True, autoincrement=True)
    sequence_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("telegram_story_sequences.id"), nullable=False)
    owner_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    message_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    media_group_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    kind: Mapped[str] = mapped_column(String(8), nullable=False)
    url: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(12), nullable=False, server_default=text("'uploading'"))
    error_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    queue_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    __table_args__ = (
        UniqueConstraint("chat_id", "message_id", name="uq_telegram_story_slide_message"),
        Index("ix_story_slide_sequence", "sequence_id", "message_id"),
    )
