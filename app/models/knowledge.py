"""Private, page-scoped knowledge candidates, bot audit and LLM model metadata.

Only service-role/Postgres server-side code touches these RLS-protected tables.
A missing bot log is NOT proof an outgoing message was authored by a human:
only explicit owner approval promotes a candidate into retrieval context.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import BigInteger, Boolean, CHAR, DateTime, ForeignKey, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base, BIGINT_PK


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


JSONB_PORTABLE = JSONB().with_variant(JSON, "sqlite")


class BotSentLog(Base):
    __tablename__ = "bot_sent_logs"

    id: Mapped[int] = mapped_column(BIGINT_PK, primary_key=True, autoincrement=True)
    account_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("instagram_accounts.id", ondelete="CASCADE"), nullable=False)
    inbox_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("inbox_messages.id", ondelete="SET NULL"), unique=True)
    provider: Mapped[str] = mapped_column(String(16), nullable=False)
    channel: Mapped[str] = mapped_column(String(16), nullable=False)
    delivery_status: Mapped[str] = mapped_column(String(12), nullable=False)
    provider_message_id: Mapped[str | None] = mapped_column(String(256))
    platform_message_id: Mapped[str | None] = mapped_column(String(256))
    content_hash: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    peer_id_hash: Mapped[str | None] = mapped_column(CHAR(64))
    sent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


class ManualReplyCandidate(Base):
    __tablename__ = "manual_reply_candidates"
    __table_args__ = (
        UniqueConstraint("account_id", "source", "channel", "source_message_id", name="manual_reply_candidates_account_id_source_channel_source_message_id_key"),
    )

    id: Mapped[int] = mapped_column(BIGINT_PK, primary_key=True, autoincrement=True)
    account_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("instagram_accounts.id", ondelete="CASCADE"), nullable=False)
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    source_message_id: Mapped[str] = mapped_column(String(256), nullable=False)
    source_platform_id: Mapped[str | None] = mapped_column(String(256))
    channel: Mapped[str] = mapped_column(String(16), nullable=False)
    origin: Mapped[str] = mapped_column(String(16), nullable=False)
    question_text: Mapped[str] = mapped_column(Text, nullable=False)
    answer_text: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    style_status: Mapped[str] = mapped_column(String(16), nullable=False, default="not_started")
    fact_approved: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    fact_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    source_created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class PageStyleGuide(Base):
    __tablename__ = "page_style_guides"

    account_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("instagram_accounts.id", ondelete="CASCADE"), primary_key=True)
    guide: Mapped[dict] = mapped_column(JSONB_PORTABLE, nullable=False, default=dict)
    sample_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


class LLMModelCatalogue(Base):
    __tablename__ = "llm_model_catalogues"

    provider: Mapped[str] = mapped_column(String(16), primary_key=True)
    models: Mapped[list] = mapped_column(JSONB_PORTABLE, nullable=False, default=list)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="unknown")
    checked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


class LLMModelBlacklist(Base):
    __tablename__ = "llm_model_blacklist"

    provider: Mapped[str] = mapped_column(String(16), primary_key=True)
    model_id: Mapped[str] = mapped_column(String(180), primary_key=True)
    blocked_until: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    reason: Mapped[str] = mapped_column(String(24), nullable=False)


class KnowledgeSyncState(Base):
    __tablename__ = "knowledge_sync_state"

    account_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("instagram_accounts.id", ondelete="CASCADE"), primary_key=True)
    dm_cursor: Mapped[str | None] = mapped_column(Text)
    comments_cursor: Mapped[str | None] = mapped_column(Text)
    last_sync_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_status: Mapped[str] = mapped_column(String(32), nullable=False, default="not_started")
    last_error_code: Mapped[str | None] = mapped_column(String(64))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
