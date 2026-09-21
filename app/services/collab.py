"""Gramma — collaboration (collab) posts.

Workflow:
  1) Page A's owner picks a draft post and requests a collab with page B
     (both pages must belong to owners within the bot).
  2) Page B's owner receives a Telegram notification and can accept/decline.
  3) On acceptance we *copy* the media into a new post owned by page B and
     schedule it (co-author tag is described via the caption). Publishing then
     goes through the normal scheduled-publisher pipeline.

Note on platform reality: the Instagram Graph API forbids auto-inviting
co-authors from third-party apps in Development mode ("branded content" and
"co-author invite" endpoints require special access). Gramma therefore
implements a **creative, policy-safe approach**: coordinated twin publishing
with a pre-agreed branded caption so both pages post the campaign together,
while the bot tracks the partnership link clearly in the UI.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from enum import Enum

from sqlalchemy import BigInteger, DateTime, String, Text, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.models.core import BIGINT_PK

logger = logging.getLogger("gramma.collab")


class CollabStatus(str, Enum):
    pending = "pending"
    accepted = "accepted"
    declined = "declined"
    published = "published"
    canceled = "canceled"


class CollabRequest(Base):
    """A partnership request between two pages in the bot."""

    __tablename__ = "collab_requests"

    id: Mapped[int] = mapped_column(BIGINT_PK, primary_key=True, autoincrement=True)
    owner_id: Mapped[int] = mapped_column(BigInteger, index=True)     # Telegram user A
    partner_id: Mapped[int] = mapped_column(BigInteger, index=True)    # Telegram user B
    source_account_id: Mapped[int] = mapped_column(BigInteger, index=True)
    target_account_id: Mapped[int] = mapped_column(BigInteger, index=True)
    post_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    message: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(16), default=CollabStatus.pending.value, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    responded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


async def create_collab_request(
    session: AsyncSession,
    owner_id: int,
    partner_id: int,
    source_account_id: int,
    target_account_id: int,
    message: str = "",
    post_id: int | None = None,
) -> CollabRequest:
    req = CollabRequest(
        owner_id=owner_id,
        partner_id=partner_id,
        source_account_id=source_account_id,
        target_account_id=target_account_id,
        post_id=post_id,
        message=message,
        status=CollabStatus.pending.value,
    )
    session.add(req)
    await session.flush()
    return req


async def get_collab_for_user(session: AsyncSession, req_id: int, user_id: int) -> CollabRequest | None:
    """Fetch a collab request, scoped to either its owner or partner."""
    result = await session.execute(
        select(CollabRequest).where(
            CollabRequest.id == req_id,
            (CollabRequest.owner_id == user_id) | (CollabRequest.partner_id == user_id),
        )
    )
    return result.scalars().first()


async def pending_for_user(session: AsyncSession, user_id: int) -> list[CollabRequest]:
    result = await session.execute(
        select(CollabRequest)
        .where(CollabRequest.partner_id == user_id, CollabRequest.status == CollabStatus.pending.value)
        .order_by(CollabRequest.created_at.desc())
    )
    return list(result.scalars())


async def accept_collab(session: AsyncSession, req: CollabRequest) -> str | None:
    """Accept → clone the source post media to the target page for twin
    publishing. Returns None on success, or an error string."""
    from app.models import Media, Post

    source_post = await session.get(Post, req.post_id) if req.post_id else None
    if source_post is None:
        return "post_not_found"

    # Clone the post to the target account (negotiated caption).
    caption = source_post.caption or ""
    joint = (
        f"{caption}\n\n"
        f"🤝 پست کلبریشن (Collab) — منتشرشده مشترک با پیج طرف قرارداد"
    )
    new_post = Post(
        account_id=req.target_account_id,
        kind=source_post.kind,
        caption=joint,
        status="draft",
    )
    session.add(new_post)
    await session.flush()

    media_rows = (
        await session.execute(select(Media).where(Media.post_id == source_post.id).order_by(Media.order_index))
    ).scalars().all()
    for m in media_rows:
        session.add(
            Media(
                post_id=new_post.id,
                url=m.url,
                kind=m.kind,
                order_index=m.order_index,
            )
        )

    req.status = CollabStatus.accepted.value
    req.responded_at = datetime.now(timezone.utc)
    return None
