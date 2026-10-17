"""Phase 4: private per-page reply templates, durable ChatbotX inbox and quota slots.

No table in this module sends messages. A slot is reserved transactionally before
an automatic outbound attempt and remains charged on uncertain delivery.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base, BIGINT_PK


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


JSONB_PORTABLE = JSONB().with_variant(JSON, "sqlite")


class ReplyTemplate(Base):
    """Owner-created keyword/answer pair; never automatically seeded."""

    __tablename__ = "reply_templates"

    id: Mapped[int] = mapped_column(BIGINT_PK, primary_key=True, autoincrement=True)
    account_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("instagram_accounts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    keyword: Mapped[str] = mapped_column(String(160), nullable=False)
    reply: Mapped[str] = mapped_column(Text, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow)


class InboxMessage(Base):
    """One authenticated inbound event. Legacy decisions are terminal, never replayed."""

    __tablename__ = "inbox_messages"
    __table_args__ = (UniqueConstraint("source", "event_key", name="uq_inbox_source_event"),)

    id: Mapped[int] = mapped_column(BIGINT_PK, primary_key=True, autoincrement=True)
    # Required pre-Phase-4 columns of the existing Zernio inbox schema. New
    # ChatbotX rows set the old status to 'ignored' to prevent any old worker
    # from processing them; the Phase-4 state below is authoritative instead.
    message_type: Mapped[str] = mapped_column(Text, nullable=False, default="dm")
    external_id: Mapped[str] = mapped_column(Text, nullable=False)
    sender_id: Mapped[str] = mapped_column(Text, nullable=False, default="unknown")
    status: Mapped[str] = mapped_column(Text, nullable=False, default="ignored")
    thread_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    parent_post_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    sender_username: Mapped[str | None] = mapped_column(Text, nullable=True)
    raw: Mapped[dict] = mapped_column(JSONB_PORTABLE, nullable=False, default=dict)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    account_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("instagram_accounts.id", ondelete="CASCADE"), nullable=True, index=True
    )
    source: Mapped[str] = mapped_column(String(24), nullable=False, default="chatbotx")
    event_key: Mapped[str] = mapped_column(String(64), nullable=False)
    channel: Mapped[str] = mapped_column(String(16), nullable=False, default="dm")
    conversation_id: Mapped[str] = mapped_column(String(256), nullable=False, default="")
    contact_id: Mapped[str] = mapped_column(String(256), nullable=False, default="")
    text: Mapped[str | None] = mapped_column(Text, nullable=True, default="")
    # Whitelisted IDs only; never dump raw provider payloads or access tokens.
    context: Mapped[dict] = mapped_column(JSONB_PORTABLE, nullable=False, default=dict)
    state: Mapped[str] = mapped_column(String(28), nullable=False, default="legacy_processing", index=True)
    reply_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    reply_source: Mapped[str | None] = mapped_column(String(32), nullable=True)
    sent_provider: Mapped[str | None] = mapped_column(String(16), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    ready_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow)


class AutoReplySlot(Base):
    """A reserved DM/comment outbound quota slot, including ambiguous attempts."""

    __tablename__ = "auto_reply_slots"

    id: Mapped[int] = mapped_column(BIGINT_PK, primary_key=True, autoincrement=True)
    account_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("instagram_accounts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    inbox_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("inbox_messages.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    reserved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    status: Mapped[str] = mapped_column(String(12), nullable=False, default="reserved")
    provider: Mapped[str | None] = mapped_column(String(16), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow)
