"""Gramma — comment-reply engine (daily cap + FIFO queue + Anti-Block).

This is the single decision point for an incoming Instagram comment when
Auto-Reply is wired up:

``route_comment_auto_reply`` decides between

  * immediate reply (+ optional DM follow-up) when the day's cap allows, or
  * enqueueing in ``comment_reply_queue`` (FIFO) when the cap is reached.

Rules (per page, per Tehran day):

  * max 1000 comment replies/day and 100/hour — DMs and story replies are
    NEVER limited;
  * reset happens at 00:00 Tehran automatically because the counter row is
    keyed by Tehran date;
  * a queued item = the comment reply, optionally followed by one DM
    follow-up (comment reply first, DM second);
  * standalone DMs are never limited.

All numbers live in ``app.services.limits``; behavior helpers live in
``app.services.anti_block``.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped

from app.core.config import get_settings
from app.services import limits
from app.services.anti_block import vary_reply
from app.services.autoreply import AutoReply, find_rule
from app.services.reply_limits import (
    CommentReplyQueue,
    DailyReplyCounter,
    QUEUE_DONE,
    QUEUE_FAILED,
    QUEUE_PENDING,
    QUEUE_PROCESSING,
    get_counter,
    record_reply,
    record_reply_error,
    tehran_date,
)

logger = logging.getLogger("gramma.reply_engine")
settings = get_settings()


async def daily_remaining(
    session: AsyncSession, account, *, now_utc: datetime | None = None
) -> int:
    """How many comment replies are still allowed today (after warm-up ramp)."""
    counter = await get_counter(session, account.id, now_utc=now_utc, create=False)
    used = counter.replies_count if counter is not None else 0
    cap = limits.reply_limit_for_account(account.created_at, now_utc=now_utc)
    return max(0, cap - used)


def _daily_left_from_counter(counter: DailyReplyCounter, account, now: datetime) -> int:
    """Cap-aware remaining budget straight from an already-fetched counter."""
    cap = limits.reply_limit_for_account(account.created_at, now_utc=now)
    return max(0, cap - (counter.replies_count or 0))


async def hourly_remaining(
    session: AsyncSession, account, *, now_utc: datetime | None = None
) -> int:
    """Remaining budget in the current rolling 60-minute window."""
    now = now_utc or datetime.now(timezone.utc)
    counter = await get_counter(session, account.id, now_utc=now, create=False)
    if counter is None:
        return limits.HOURLY_REPLY_LIMIT
    return _hourly_left_from_counter(counter, now)


def _hourly_left_from_counter(counter: DailyReplyCounter, now: datetime) -> int:
    """Remaining budget straight from an already-fetched counter."""
    if counter is None or counter.hourly_window_start is None:
        return limits.HOURLY_REPLY_LIMIT
    ws = counter.hourly_window_start
    if ws.tzinfo is None:
        ws = ws.replace(tzinfo=timezone.utc)
    if (now - ws).total_seconds() >= 3600:
        return limits.HOURLY_REPLY_LIMIT
    return max(0, limits.HOURLY_REPLY_LIMIT - (counter.hourly_count or 0))


async def _compose_reply(
    session: AsyncSession, account_id: int, comment_text: str
) -> tuple[str | None, AutoReply | None, str]:
    """Return (comment_reply_text, matched_rule, dm_followup_text)."""
    rule = await find_rule(session, account_id, comment_text)
    reply_text = rule.reply if rule else None
    dm = ""
    if rule is not None and (rule.dm_followup or "").strip():
        dm = rule.dm_followup.strip()
    return reply_text, rule, dm


async def route_comment_auto_reply(
    session: AsyncSession,
    account,
    *,
    comment_id: str,
    comment_text: str,
    commenter_id: str | None,
    now_utc: datetime | None = None,
    commit: bool = True,
) -> "RouteDecision":
    """Entry point for a comment webhook event that triggers auto-reply.

    Returns a :class:`RouteDecision` describing what was done. Queued items
    are marked ``done`` immediately when the reply succeeds synchronously;
    otherwise they stay ``pending`` for the queue processor.

    ``commit=False`` keeps the transaction open (used by bulk simulations to
    batch one round-trip per N events); production always commits.
    """
    from app.services.meta.service import InstagramService

    now = now_utc or datetime.now(timezone.utc)
    reply_text, rule, dm = await _compose_reply(session, account.id, comment_text)

    decision = RouteDecision(
        account_id=account.id,
        comment_id=comment_id,
        reply_text=reply_text,
        dm_followup=dm,
        auto_reply_id=rule.id if rule else None,
    )

    if not reply_text:
        decision.action = "no_reply"
        decision.reason = "no_matching_rule"
        return decision

    counter = await get_counter(session, account.id, now_utc=now)

    daily_left = _daily_left_from_counter(counter, account, now)
    hourly_left = _hourly_left_from_counter(counter, now)

    if daily_left <= 0 or hourly_left <= 0:
        # Cap reached → queue instead of replying right now.
        q = CommentReplyQueue(
            account_id=account.id,
            comment_id=comment_id,
            comment_text=comment_text,
            auto_reply_id=rule.id if rule else None,
            dm_followup_required=bool(dm),
            dm_message=dm,
            status=QUEUE_PENDING,
        )
        session.add(q)
        await session.flush()
        decision.action = "queued"
        decision.queued = True
        decision.reason = (
            "daily_cap" if daily_left <= 0 else "hourly_cap"
        )
        decision.queue_id = q.id
        if commit:
            await session.commit()
        return decision

    # Send now (comment reply first, then optional DM).
    svc = InstagramService()
    human = await _send_comment_and_dm(
        session, account, svc, comment_id, comment_text, reply_text, dm, counter, commenter_id,
        now_utc=now,
    )

    if human["ok"]:
        decision.action = "replied"
        decision.reply_text = human["varied_reply"]
        decision.dm_sent = human["dm_sent"]
        if commit:
            await session.commit()
    else:
        # Immediate failure → keep it in the queue for retry.
        q = CommentReplyQueue(
            account_id=account.id,
            comment_id=comment_id,
            comment_text=comment_text,
            auto_reply_id=rule.id if rule else None,
            dm_followup_required=bool(dm),
            dm_message=dm,
            status=QUEUE_FAILED,
            retry_count=1,
            error_message=(human.get("error") or "")[:2000],
        )
        session.add(q)
        await session.flush()
        decision.action = "queued"
        decision.queued = True
        decision.reason = "send_error_queued"
        decision.queue_id = q.id
        if commit:
            await session.commit()
    return decision


async def _send_comment_and_dm(
    session: AsyncSession,
    account,
    svc,
    comment_id: str,
    comment_text: str,
    reply_text: str,
    dm: str,
    counter: DailyReplyCounter,
    commenter_id: str | None,
    *,
    now_utc: datetime | None = None,
) -> dict:
    """Send the comment reply (+ optional DM) with anti-block safeguards."""
    from app.services import anti_block

    now = now_utc or datetime.now(timezone.utc)

    result: dict = {"ok": False, "varied_reply": "", "dm_sent": False, "error": ""}
    profile = counter.rate_profile or anti_block.draw_rate_profile()
    counter.rate_profile = profile

    # Don't act while in backoff (after an Instagram error).
    if anti_block.in_backoff(counter, now_utc=now):
        result["error"] = "in_backoff"
        return result

    await anti_block.await_rest_if_needed(counter, profile)

    varied = anti_block.vary_reply(reply_text)
    try:
        await svc.reply_comment(account, comment_id, varied)
    except Exception as exc:  # noqa: BLE001
        kind = anti_block.classify_instagram_error(exc)
        await record_reply_error(session, counter.account_id)
        await anti_block.mark_error(counter)
        result["error"] = str(exc) or "reply failed"
        result["block_kind"] = kind
        return result

    await record_reply(session, account.id, now_utc=now)
    await anti_block.mark_success(counter)
    result["ok"] = True
    result["varied_reply"] = varied

    # Optional DM follow-up (only after the comment reply succeeded).
    if dm:
        await anti_block.human_delay_wait(profile)
        try:
            # Private reply attached to the comment (Instagram endpoint).
            await svc.private_reply(account, comment_id, dm)
            result["dm_sent"] = True
        except Exception as exc:  # noqa: BLE001
            # DM failure does NOT fail the comment reply; keep for retry via queue.
            kind = anti_block.classify_instagram_error(exc)
            await record_reply_error(session, counter.account_id)
            await anti_block.mark_error(counter)
            result["error"] = f"dm_followup_failed: {exc}"
            result["block_kind"] = kind
    return result


class RouteDecision:
    """What the engine did with one comment-reply event."""

    def __init__(self, account_id: int, comment_id: str, reply_text: str | None,
                 dm_followup: str, auto_reply_id: int | None):
        self.account_id = account_id
        self.comment_id = comment_id
        self.reply_text = reply_text
        self.dm_followup = dm_followup
        self.auto_reply_id = auto_reply_id
        self.action = "no_reply"      # no_reply | replied | queued
        self.reason = ""
        self.queued = False
        self.queue_id = None
        self.dm_sent = False

    def as_dict(self) -> dict:
        return {
            "account_id": self.account_id,
            "comment_id": self.comment_id,
            "action": self.action,
            "reason": self.reason,
            "queued": self.queued,
            "queue_id": self.queue_id,
            "dm_sent": self.dm_sent,
        }


# ── New: generic incoming message router for ChatbotX API Channel ──
async def route_incoming_message(
    session: AsyncSession,
    account,
    *,
    message_text: str,
    message_type: str = "dm",  # dm | comment | story_reply
    contact_id: str | None = None,
    conversation_id: str | None = None,
    now_utc: datetime | None = None,
) -> "IncomingDecision":
    """Route an incoming message from ChatbotX through auto_replies + AI.

    This is the brain for the new architecture:
      Instagram -> ChatbotX API Channel -> Gramma webhook -> this function
        -> check auto_replies -> if match, return reply
        -> else if AI enabled, generate via AvalAI
        -> else no reply

    Returns IncomingDecision with reply_text, source, etc.
    """
    now = now_utc or datetime.now(timezone.utc)
    text = (message_text or "").strip()

    # 1. Try auto_replies rule
    rule = await find_rule(session, account.id, text)
    if rule:
        rule.matches = (rule.matches or 0) + 1
        # Don't commit here, caller will commit
        return IncomingDecision(
            account_id=account.id,
            incoming_text=text,
            reply_text=rule.reply,
            dm_followup=rule.dm_followup or "",
            auto_reply_id=rule.id,
            source="rule",
            matched=True,
            message_type=message_type,
            conversation_id=conversation_id,
            contact_id=contact_id,
        )

    # 2. Try AI if enabled
    ai_enabled = False
    # AvalAI is the only AI usable on the serverless webhook runtime
    # (OpenClaw lives on the user's phone and is unreachable from Vercel).
    if getattr(settings, "avalai_api_key", None):
        ai_enabled = True

    if ai_enabled and text:
        try:
            from app.services.ai import draft_reply

            # Webhook path gets a hard 5s budget so an incoming Instagram
            # message can never stall the function; local polling keeps 20s.
            ai_timeout = 5.0 if getattr(settings, "run_mode", "webhook") == "webhook" else 20.0
            ai_reply = await draft_reply(text, locale="fa", timeout=ai_timeout)
            if ai_reply:
                return IncomingDecision(
                    account_id=account.id,
                    incoming_text=text,
                    reply_text=ai_reply,
                    dm_followup="",
                    auto_reply_id=None,
                    source="ai",
                    matched=False,
                    ai_used=True,
                    message_type=message_type,
                    conversation_id=conversation_id,
                    contact_id=contact_id,
                )
        except Exception as exc:
            logger.warning("AI generation failed in route_incoming_message: %s", exc)

    # 3. No match
    return IncomingDecision(
        account_id=account.id,
        incoming_text=text,
        reply_text=None,
        dm_followup="",
        auto_reply_id=None,
        source="none",
        matched=False,
        message_type=message_type,
        conversation_id=conversation_id,
        contact_id=contact_id,
    )


class IncomingDecision:
    """Decision for a generic incoming message (DM/comment/story) via ChatbotX."""

    def __init__(
        self,
        account_id: int,
        incoming_text: str,
        reply_text: str | None,
        dm_followup: str,
        auto_reply_id: int | None,
        source: str,  # rule | ai | none
        matched: bool = False,
        ai_used: bool = False,
        message_type: str = "dm",
        conversation_id: str | None = None,
        contact_id: str | None = None,
    ):
        self.account_id = account_id
        self.incoming_text = incoming_text
        self.reply_text = reply_text
        self.dm_followup = dm_followup
        self.auto_reply_id = auto_reply_id
        self.source = source
        self.matched = matched
        self.ai_used = ai_used
        self.message_type = message_type
        self.conversation_id = conversation_id
        self.contact_id = contact_id

    @property
    def should_reply(self) -> bool:
        return bool(self.reply_text)

    def as_dict(self) -> dict:
        return {
            "account_id": self.account_id,
            "incoming_text": self.incoming_text,
            "reply_text": self.reply_text,
            "source": self.source,
            "matched": self.matched,
            "ai_used": self.ai_used,
            "message_type": self.message_type,
            "conversation_id": self.conversation_id,
            "contact_id": self.contact_id,
            "auto_reply_id": self.auto_reply_id,
        }
