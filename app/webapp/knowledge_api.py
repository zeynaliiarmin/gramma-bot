"""Owner-only Mini-App review for private learned examples, style, and held replies.

GETs are owner-scoped; approvals never send a social/Telegram message. Only
explicit owner approval permits a prior reply to influence future LLM output.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select

from app.core.database import SessionLocal
from app.models import (InboxMessage, KnowledgeSyncState, LLMModelBlacklist,
                        LLMModelCatalogue, ManualReplyCandidate, PageStyleGuide)
from app.services.knowledge import analyze_approved_style, matches_bot_log
from app.utils import jalali
from app.webapp.auth import MiniAppIdentity, resolve_miniapp_user
from app.webapp.reply_templates_api import _account_id, _owned_account

router = APIRouter(prefix="/api/knowledge", tags=["owner-knowledge"])


def _date(value: datetime | None) -> str | None:
    return jalali.to_jalali_str(value) if value else None


def _candidate(item: ManualReplyCandidate) -> dict:
    return {"id": item.id, "channel": item.channel, "origin": item.origin,
            "source": item.source, "question": item.question_text,
            "answer": item.answer_text, "status": item.status,
            "style_status": item.style_status, "fact_approved": item.fact_approved,
            "created_jalali": _date(item.source_created_at),
            "fact_expires_jalali": _date(item.fact_expires_at)}


@router.get("/review")
async def review_dashboard(account_id: int,
                           ident: MiniAppIdentity = Depends(resolve_miniapp_user)):
    account_id = _account_id(account_id)
    async with SessionLocal() as session:
        await _owned_account(session, account_id, ident.user_id)
        candidates = (await session.execute(select(ManualReplyCandidate).where(
            ManualReplyCandidate.account_id == account_id,
            ManualReplyCandidate.status == "pending",
        ).order_by(ManualReplyCandidate.created_at.desc()).limit(40))).scalars().all()
        approved = (await session.execute(select(ManualReplyCandidate).where(
            ManualReplyCandidate.account_id == account_id,
            ManualReplyCandidate.status == "approved",
        ).order_by(ManualReplyCandidate.approved_at.desc()).limit(25))).scalars().all()
        pending_count = (await session.execute(select(func.count()).select_from(ManualReplyCandidate).where(
            ManualReplyCandidate.account_id == account_id,
            ManualReplyCandidate.status == "pending",
        ))).scalar_one()
        pending_style = (await session.execute(select(func.count()).select_from(ManualReplyCandidate).where(
            ManualReplyCandidate.account_id == account_id,
            ManualReplyCandidate.status == "approved",
            ManualReplyCandidate.style_status != "ready",
        ))).scalar_one()
        review = (await session.execute(select(InboxMessage).where(
            InboxMessage.account_id == account_id,
            InboxMessage.source == "chatbotx",
            InboxMessage.state.in_(("manual_review", "quota_expired")),
            InboxMessage.manual_reviewed_at.is_(None),
        ).order_by(InboxMessage.received_at.desc(), InboxMessage.id.desc()).limit(30))).scalars().all()
        held = (await session.execute(select(InboxMessage).where(
            InboxMessage.account_id == account_id,
            InboxMessage.source == "chatbotx", InboxMessage.state == "hold",
        ).order_by(InboxMessage.scheduled_for.asc()).limit(25))).scalars().all()
        style = await session.get(PageStyleGuide, account_id)
        sync = await session.get(KnowledgeSyncState, account_id)
        catalogues = (await session.execute(select(LLMModelCatalogue))).scalars().all()
        blocked = (await session.execute(select(LLMModelBlacklist).where(
            LLMModelBlacklist.blocked_until > datetime.now(timezone.utc)
        ))).scalars().all()
    return {
        "candidates": [_candidate(row) for row in candidates],
        "approved": [_candidate(row) for row in approved],
        "pending_total": pending_count, "style_pending": pending_style,
        "manual_review": [{"id": row.id, "channel": row.channel,
                           "text": (row.text or "")[:800], "reason": row.error_code,
                           "received_jalali": _date(row.received_at)} for row in review],
        "held": [{"id": row.id, "channel": row.channel,
                  "text": (row.text or "")[:240], "reason": row.error_code,
                  "scheduled_jalali": _date(row.scheduled_for)} for row in held],
        "style": style.guide if style else {},
        "style_samples": style.sample_count if style else 0,
        "sync": {"status": sync.last_status if sync else "not_started",
                 "last_jalali": _date(sync.last_sync_at) if sync else None,
                 "error_code": sync.last_error_code if sync else None},
        "models": {row.provider: {"status": row.status,
                                  "eligible": len(row.models) if isinstance(row.models, list) else 0,
                                  "checked_jalali": _date(row.checked_at)}
                   for row in catalogues},
        "blacklisted_model_count": len(blocked),
    }


@router.post("/candidates/{candidate_id}/decision")
async def decide_candidate(candidate_id: int, body: dict,
                           ident: MiniAppIdentity = Depends(resolve_miniapp_user)):
    body = body or {}
    account_id = _account_id(body.get("account_id"))
    decision = body.get("decision")
    if decision not in ("approve", "reject"):
        raise HTTPException(400, "تصمیم باید تأیید یا رد باشد.")
    fact = body.get("fact_approved", False)
    if type(fact) is not bool:
        raise HTTPException(400, "تأیید اطلاعات کسب‌وکار نامعتبر است.")
    async with SessionLocal() as session:
        await _owned_account(session, account_id, ident.user_id)
        row = await session.get(ManualReplyCandidate, candidate_id, with_for_update=True)
        if row is None or row.account_id != account_id:
            raise HTTPException(404, "نمونهٔ موردنظر پیدا نشد.")
        if row.status != "pending":
            raise HTTPException(409, "این نمونه پیش‌تر بررسی شده است.")
        if decision == "reject":
            row.status = "rejected"
            row.question_text, row.answer_text = "", ""  # forget private content
            await session.commit()
            return {"ok": True, "status": "rejected", "message": "نمونه حذف شد؛ هیچ پیامی ارسال نشد."}
        if await matches_bot_log(
            session, account_id=account_id, source=row.source, channel=row.channel,
            source_message_id=row.source_message_id,
            platform_id=row.source_platform_id, answer=row.answer_text,
            created_at=row.source_created_at,
        ):
            row.status = "bot_excluded"
            row.question_text, row.answer_text = "", ""
            await session.commit()
            raise HTTPException(409, "این پاسخ با لاگ ربات منطبق بود و وارد دانش نشد.")
        row.status = "approved"
        row.approved_at = datetime.now(timezone.utc)
        row.fact_approved = fact
        row.fact_expires_at = row.approved_at + timedelta(days=7) if fact else None
        row.style_status = "pending"
        await session.commit()
    # The user clicked approval. This analysis cannot send an Instagram/Telegram
    # message. If no verified free LLM exists, keep the style job pending.
    enriched = await analyze_approved_style(candidate_id)
    return {"ok": True, "status": "approved", "style_status": "ready" if enriched else "pending",
            "message": "دانش ذخیره شد؛ تحلیل لحن در صورت دسترسی مدل رایگان انجام می‌شود."}


@router.delete("/examples/{candidate_id}")
async def remove_approved(candidate_id: int, account_id: int,
                          ident: MiniAppIdentity = Depends(resolve_miniapp_user)):
    account_id = _account_id(account_id)
    async with SessionLocal() as session:
        await _owned_account(session, account_id, ident.user_id)
        row = await session.get(ManualReplyCandidate, candidate_id, with_for_update=True)
        if row is None or row.account_id != account_id or row.status != "approved":
            raise HTTPException(404, "نمونهٔ تأییدشده پیدا نشد.")
        row.status, row.question_text, row.answer_text = "rejected", "", ""
        row.style_status = "not_started"
        style = await session.get(PageStyleGuide, account_id, with_for_update=True)
        if style:
            style.guide, style.sample_count = {}, 0
        # A removed example's style must not linger; rebuild incrementally
        # from remaining owner-approved samples on subsequent nightly runs.
        remaining = (await session.execute(select(ManualReplyCandidate).where(
            ManualReplyCandidate.account_id == account_id,
            ManualReplyCandidate.status == "approved",
        ))).scalars().all()
        for item in remaining:
            item.style_status = "pending"
        await session.commit()
    return {"ok": True, "message": "نمونه و اثر آن بر راهنمای لحن حذف شد؛ هیچ پیامی ارسال نشد."}


@router.post("/manual-review/{inbox_id}/ack")
async def acknowledge_manual_review(inbox_id: int, body: dict,
                                    ident: MiniAppIdentity = Depends(resolve_miniapp_user)):
    account_id = _account_id((body or {}).get("account_id"))
    async with SessionLocal() as session:
        await _owned_account(session, account_id, ident.user_id)
        row = await session.get(InboxMessage, inbox_id, with_for_update=True)
        if (row is None or row.account_id != account_id
                or row.state not in ("manual_review", "quota_expired")):
            raise HTTPException(404, "پیامِ نیازمند بررسی پیدا نشد.")
        row.manual_reviewed_at = datetime.now(timezone.utc)
        await session.commit()
    return {"ok": True, "message": "بررسی ثبت شد. این عمل هیچ پیامی ارسال نمی‌کند."}
