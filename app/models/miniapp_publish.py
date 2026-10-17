"""Durable Mini-App composer. These drafts never publish by themselves."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import BigInteger, DateTime, Float, ForeignKey, Index, Integer, JSON, String, Text, UniqueConstraint, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import BIGINT_PK, Base


def _expiry() -> datetime:
    return datetime.now(timezone.utc) + timedelta(days=7)


class MiniappPublishDraft(Base):
    __tablename__ = "miniapp_publish_drafts"

    id: Mapped[int] = mapped_column(BIGINT_PK, primary_key=True, autoincrement=True)
    owner_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    local_account_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    expected_username: Mapped[str] = mapped_column(String(64), nullable=False)
    kind: Mapped[str] = mapped_column(String(8), nullable=False)  # post | story
    status: Mapped[str] = mapped_column(String(12), nullable=False, default="collecting",
                                        server_default=text("'collecting'"))
    queue_ids: Mapped[list[int]] = mapped_column(
        JSON().with_variant(JSONB, "postgresql"), nullable=False,
        default=list, server_default=text("'[]'"),
    )
    settings: Mapped[dict] = mapped_column(
        JSON().with_variant(JSONB, "postgresql"), nullable=False,
        default=dict, server_default=text("'{}'"),
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False,
                                                 server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False,
                                                 server_default=func.now())
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_expiry)

    __table_args__ = (
        Index("ix_miniapp_drafts_owner", "owner_id", "status"),
        Index("uq_miniapp_active_kind", "owner_id", "kind", unique=True,
              postgresql_where=text("status = 'collecting'"),
              sqlite_where=text("status = 'collecting'")),
    )


class MiniappPublishMedia(Base):
    __tablename__ = "miniapp_publish_media"

    id: Mapped[int] = mapped_column(BIGINT_PK, primary_key=True, autoincrement=True)
    draft_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("miniapp_publish_drafts.id"), nullable=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    kind: Mapped[str] = mapped_column(String(8), nullable=False)  # image | video
    ext: Mapped[str] = mapped_column(String(5), nullable=False)
    mime_type: Mapped[str] = mapped_column(String(24), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    width: Mapped[int] = mapped_column(Integer, nullable=False)
    height: Mapped[int] = mapped_column(Integer, nullable=False)
    duration_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
    storage_path: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    url: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(12), nullable=False, default="uploading",
                                        server_default=text("'uploading'"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False,
                                                 server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False,
                                                 server_default=func.now())

    __table_args__ = (
        UniqueConstraint("draft_id", "position", name="uq_miniapp_media_position"),
        Index("ix_miniapp_media_draft", "draft_id", "position"),
    )
