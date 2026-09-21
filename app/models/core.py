"""
Gramma — database models (schema).

Design notes:
  * Multi-tenant isolation is enforced by `telegram_id` columns on the
    User-owned rows; every repository query filters by the acting user.
  * Access tokens are stored **encrypted** (AES-256 / Fernet) in
    `InstagramAccount.long_lived_token_enc`.
  * `key_version` tracks which encryption key encrypted the value, so keys
    can be rotated later without a data migration.
  * Decimal counters use ints (Graph API counters are ints / epochs are
    unix timestamps) to keep the schema portable across SQLite & PostgreSQL.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base

# Auto-incrementing primary key that works on BOTH backends:
#   * PostgreSQL → BIGINT (IDENTITY / BIGSERIAL)
#   * SQLite     → INTEGER PRIMARY KEY (the only auto-incrementing type there)
BIGINT_PK = BigInteger().with_variant(Integer, "sqlite")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class User(Base):
    """A Telegram user who uses the bot (the 'tenant')."""

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)  # telegram uid
    telegram_username: Mapped[str | None] = mapped_column(String(64), nullable=True)
    full_name: Mapped[str] = mapped_column(String(255), default="")
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    locale: Mapped[str] = mapped_column(String(8), default="en")
    # The page the user considers "active" (multi-page support).
    active_account_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("instagram_accounts.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    accounts: Mapped[list["InstagramAccount"]] = relationship(
        back_populates="owner",
        cascade="all, delete-orphan",
        foreign_keys="InstagramAccount.owner_id",
    )


class InstagramAccount(Base):
    """One connected Instagram Business/Creator account."""

    __tablename__ = "instagram_accounts"

    id: Mapped[int] = mapped_column(BIGINT_PK, primary_key=True, autoincrement=True)
    owner_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), index=True
    )

    instagram_user_id: Mapped[str | None] = mapped_column(
        String(64), nullable=True, index=True
    )
    username: Mapped[str] = mapped_column(String(64), default="")
    name: Mapped[str] = mapped_column(String(255), default="")
    profile_pic_url: Mapped[str] = mapped_column(Text, default="")

    # ── Tokens (ENCRYPTED at rest) ────────────────────────────
    long_lived_token_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    token_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    key_version: Mapped[int] = mapped_column(Integer, default=1)

    status: Mapped[str] = mapped_column(String(16), default="connected")
    last_sync_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    owner: Mapped["User"] = relationship(back_populates="accounts", foreign_keys="InstagramAccount.owner_id")
    posts: Mapped[list["Post"]] = relationship(
        back_populates="account", cascade="all, delete-orphan"
    )
    comments: Mapped[list["Comment"]] = relationship(
        back_populates="account", cascade="all, delete-orphan"
    )
    conversations: Mapped[list["Conversation"]] = relationship(
        back_populates="account", cascade="all, delete-orphan"
    )
    logs: Mapped[list["ActivityLog"]] = relationship(
        back_populates="account", cascade="all, delete-orphan"
    )


class Media(Base):
    """Media items attached to a scheduled post (before Graph upload)."""

    __tablename__ = "media"

    id: Mapped[int] = mapped_column(BIGINT_PK, primary_key=True, autoincrement=True)
    post_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("posts.id", ondelete="CASCADE"), nullable=True, index=True
    )
    # Path to local file, or public URL once uploaded to CDN
    url: Mapped[str] = mapped_column(Text, default="")
    kind: Mapped[str] = mapped_column(String(16), default="photo")  # photo | video
    order_index: Mapped[int] = mapped_column(Integer, default=0)
    container_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    post: Mapped["Post"] = relationship(back_populates="media")


class Post(Base):
    """A (scheduled or published) content unit — post / reel / carousel /
    story."""

    __tablename__ = "posts"

    id: Mapped[int] = mapped_column(BIGINT_PK, primary_key=True, autoincrement=True)
    account_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("instagram_accounts.id", ondelete="CASCADE"), index=True
    )

    kind: Mapped[str] = mapped_column(String(16), default="photo")  # photo|video|reel|carousel|story
    caption: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(16), default="draft")  # draft|scheduled|publishing|published|failed|canceled
    scheduled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    publish_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ig_media_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    ig_permalink: Mapped[str] = mapped_column(Text, default="")
    error_message: Mapped[str] = mapped_column(Text, default="")

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    account: Mapped["InstagramAccount"] = relationship(back_populates="posts")
    media: Mapped[list["Media"]] = relationship(
        back_populates="post", cascade="all, delete-orphan", order_by="Media.order_index"
    )


class Comment(Base):
    """A comment that the user is acting on (e.g. replied / hidden)."""

    __tablename__ = "comments"

    id: Mapped[int] = mapped_column(BIGINT_PK, primary_key=True, autoincrement=True)
    account_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("instagram_accounts.id", ondelete="CASCADE"), index=True
    )
    ig_comment_id: Mapped[str] = mapped_column(String(64), index=True)
    ig_media_id: Mapped[str] = mapped_column(String(64), default="")
    username: Mapped[str] = mapped_column(String(64), default="")
    text: Mapped[str] = mapped_column(Text, default="")
    replied: Mapped[bool] = mapped_column(Boolean, default=False)
    hidden: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    account: Mapped["InstagramAccount"] = relationship(back_populates="comments")


class Conversation(Base):
    """A DM thread, categorized by AI / rules."""

    __tablename__ = "conversations"

    id: Mapped[int] = mapped_column(BIGINT_PK, primary_key=True, autoincrement=True)
    account_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("instagram_accounts.id", ondelete="CASCADE"), index=True
    )
    ig_thread_id: Mapped[str] = mapped_column(String(64), index=True)
    counterpart_name: Mapped[str] = mapped_column(String(255), default="")
    last_message_preview: Mapped[str] = mapped_column(Text, default="")
    category: Mapped[str] = mapped_column(String(24), default="needs_reply")  # needs_reply|replied|spam|archived
    unread_count: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    account: Mapped["InstagramAccount"] = relationship(back_populates="conversations")


class ActivityLog(Base):
    """Audit / security log — every sensitive action is recorded."""

    __tablename__ = "activity_logs"

    id: Mapped[int] = mapped_column(BIGINT_PK, primary_key=True, autoincrement=True)
    account_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("instagram_accounts.id", ondelete="CASCADE"),
        nullable=True, index=True,
    )
    user_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, index=True)
    action: Mapped[str] = mapped_column(String(64), index=True)  # login|connect|publish|comment_reply|dm_send|token_refresh|security_alert…
    detail: Mapped[str] = mapped_column(Text, default="")
    level: Mapped[str] = mapped_column(String(8), default="info")  # info|warning|error
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    account: Mapped["InstagramAccount"] = relationship(back_populates="logs")


class OAuthFlow(Base):
    """Persisted OAuth authorization-code record + secure random verifier."""

    __tablename__ = "oauth_flows"

    id: Mapped[int] = mapped_column(BIGINT_PK, primary_key=True, autoincrement=True)
    account_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("instagram_accounts.id", ondelete="CASCADE"), index=True
    )
    code_enc: Mapped[str] = mapped_column(Text, default="")  # encrypted auth code
    code_verifier: Mapped[str] = mapped_column(Text, default="")
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (UniqueConstraint("account_id", name="uq_oauth_flow_account"),)
