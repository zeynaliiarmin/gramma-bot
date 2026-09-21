"""Gramma — semantic / full-text search over comments & DMs.

Tenant-scoped search across everything the user has ever seen in the bot:
comments and DM conversations. Tokenizes Persian + English text, normalizes
(Arabic ي/ك → فارسی ی/ک, strip diacritics), and scores matches by term
overlap + recency so the most relevant items surface first.

Separate cable paths:
  * `search_history(session, user_id, query)` — async, returns ranked results.
  * `tokenize(text)` — the pure-Python tokenizer used for scoring.
"""

from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.models import Comment, Conversation, InstagramAccount
from app.utils import jalali

logger = logging.getLogger("gramma.search")
settings = get_settings()

_STOPWORDS = {
    "the", "and", "for", "with", "this", "that", "you", "من", "که", "این", "از",
    "به", "با", "در", "برای", "است", "هست", "چه", "چطور", "کدام", "و",
}


_DIACRITICS = {
    "\u064B", "\u064C", "\u064D", "\u064E", "\u064F", "\u0650", "\u0651", "\u0652",
    "\u0670", "\u0640",  # harakat, shadda, sukun, superscript alef, tatweel
    "\u200C",  # ZWNJ
    "\u061F", "\u060C", "\u061B",  # ؟ ، ؛ punctuation
}


def _normalize_char(ch: str) -> str:
    ch = unicodedata.normalize("NFKC", ch)
    # Drop Arabic/Persian diacritics (harakat) — irrelevant to search.
    if ch in _DIACRITICS:
        return ""
    # Unify Arabic ي / ى → Persian ی and Arabic ك → Persian ک.
    if ch in {"ي", "ى"}:
        return "ی"
    if ch == "ك":
        return "ک"
    return ch


def normalize_text(text: str) -> str:
    """Normalize Persian/Arabic text for reliable matching."""
    return "".join(_normalize_char(c) for c in text or "")


def tokenize(text: str) -> list[str]:
    """Split into normalized, stopword-free lowercase tokens.

    Persian letters are preserved (asserted by tests); Latin letters are
    lowercased. Diacritics are dropped through NFKC + explicit sets.
    """
    text = normalize_text((text or "").lower())
    # Keep Persian letters + latin letters + digits; split on the rest.
    words = re.findall(r"[\u0600-\u06FFa-z0-9]{2,}", text)
    out: list[str] = []
    for word in words:
        if word in _STOPWORDS or len(word) < 2:
            continue
        # soft-stem common Persian suffixes/prefixes for recall
        stem = re.sub(
            r"(هایم|های|ها|ترین|تر|ات|ان|ی|می)$", "", word
        ) or word
        out.append(stem)
    return out


@dataclass
class SearchHit:
    kind: str            # comment | dm
    score: int
    account_id: int
    username: str
    text: str
    created_at: str
    meta: dict = field(default_factory=dict)


def _score(query_tokens: list[str], text: str, days_old: float) -> float:
    text_tokens = tokenize(text)
    if not text_tokens:
        return 0.0
    matched = [t for t in query_tokens if any(t in tt for tt in text_tokens)]
    term_score = len(matched) / max(len(query_tokens), 1)
    recency = max(0.0, 1.0 - days_old / 365.0)  # older → slightly lower
    return round((term_score * 100.0) * (0.7 + 0.3 * recency), 1)


async def search_history(
    session: AsyncSession,
    user_id: int,
    query: str,
    limit: int = 10,
) -> list[SearchHit]:
    """Semantic-ish ranked search over this tenant's comments + DMs."""
    if not settings.semantic_search_enabled:
        return []

    query_tokens = tokenize(query)
    if not query_tokens:
        return []

    hits: list[SearchHit] = []

    # 1) Comments (media comments the user has acted on / seen)
    acc_result = await session.execute(
        select(InstagramAccount.id).where(InstagramAccount.owner_id == user_id)
    )
    account_ids = list(acc_result.scalars())

    if account_ids:
        comments_result = await session.execute(
            select(Comment)
            .where(Comment.account_id.in_(account_ids))
            .order_by(Comment.created_at.desc())
            .limit(300)
        )
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc)
        for c in comments_result.scalars():
            days = (now - c.created_at.replace(tzinfo=timezone.utc)).days
            s = _score(query_tokens, c.text, days)
            if s > 0:
                hits.append(
                    SearchHit(
                        kind="comment",
                        score=s,
                        account_id=c.account_id,
                        username=c.username,
                        text=c.text,
                        created_at=jalali.jalali_date_str(c.created_at),
                        meta={"replied": c.replied},
                    )
                )

    # 2) DMs
    if account_ids:
        dm_result = await session.execute(
            select(Conversation)
            .where(Conversation.account_id.in_(account_ids))
            .order_by(Conversation.updated_at.desc())
            .limit(300)
        )
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc)
        for conv in dm_result.scalars():
            days = (now - conv.updated_at.replace(tzinfo=timezone.utc)).days
            text = f"{conv.counterpart_name} {conv.last_message_preview}"
            s = _score(query_tokens, text, days)
            if s > 0:
                hits.append(
                    SearchHit(
                        kind="dm",
                        score=s,
                        account_id=conv.account_id,
                        username=conv.counterpart_name,
                        text=conv.last_message_preview,
                        created_at=jalali.jalali_date_str(conv.updated_at),
                        meta={"category": conv.category},
                    )
                )

    hits.sort(key=lambda h: (h.score, h.created_at), reverse=True)
    return hits[:limit]


def format_hits(hits: list[SearchHit]) -> str:
    """Render search results for a Telegram message."""
    if not hits:
        return "🔍 چیزی پیدا نشد. عبارت دیگری را امتحان کنید."
    lines = ["🔍 نتایج جستجو:\n"]
    for i, h in enumerate(hits, 1):
        kind = "💬 کامنت" if h.kind == "comment" else "📥 دایرکت"
        snippet = (h.text or "…")[:60]
        lines.append(f"{i}. {kind} | {h.username} | امتیاز {h.score:.0f}%\n    «{snippet}»")
    return "\n".join(lines)
