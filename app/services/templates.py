"""Gramma v3 — post templates.

Reusable caption/media skeletons so users can publish faster. Templates are
tenant-scoped (per account) and rendered with simple `{title}` placeholders.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import BigInteger, Boolean, DateTime, String, Text, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.models.core import BIGINT_PK

# Built-in starter library (fa): shown when a user has no custom template.
DEFAULT_TEMPLATES = [
    {
        "title": "اعلان محصول جدید",
        "caption": "✨ محصول جدید رسید!\n\n{title}\n\nهمین حالا سفارش دهید 👇\n#جدید #محصول",
    },
    {
        "title": "تخفیف ویژه",
        "caption": "🔥 تخفیف محدود!\n\n{title}\nفقط تا پایان هفته ⏰\n#تخفیف #ویژه",
    },
    {
        "title": "پرسش از مخاطب",
        "caption": "💬 نظر شما چیه؟\n\n{title}\nکامنت بگذارید! 👇",
    },
    {
        "title": "پشت صحنه",
        "caption": "🎬 پشت صحنه امروز:\n{title}\n\nما اینجاییم، همیشه!",
    },
    {
        "title": "قدردانی از فالوورها",
        "caption": "💜 ممنون که هستید!\nما حالا {title} فالوور داریم 🎉\n#فالوور #ممنون",
    },
]


def render_template(caption: str, title: str = "") -> str:
    """Fill a template placeholder."""
    try:
        return caption.format(title=title or "")
    except Exception:  # noqa: BLE001
        return caption.replace("{title}", title or "")


class PostTemplate(Base):
    """A user/account-scoped post template."""

    __tablename__ = "post_templates"

    id: Mapped[int] = mapped_column(BIGINT_PK, primary_key=True, autoincrement=True)
    account_id: Mapped[int] = mapped_column(BigInteger, index=True)
    title: Mapped[str] = mapped_column(String(120), default="")
    caption: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


async def list_templates(session: AsyncSession, account_id: int) -> list[dict]:
    result = await session.execute(
        select(PostTemplate).where(PostTemplate.account_id == account_id)
    )
    custom = [
        {"id": t.id, "title": t.title, "caption": t.caption}
        for t in result.scalars()
    ]
    # Always append the starter library so new users see something useful.
    return custom + [{"id": -i, **t} for i, t in enumerate(DEFAULT_TEMPLATES, 1)]


async def add_template(session: AsyncSession, account_id: int, title: str, caption: str) -> PostTemplate:
    t = PostTemplate(account_id=account_id, title=title, caption=caption)
    session.add(t)
    await session.flush()
    return t
