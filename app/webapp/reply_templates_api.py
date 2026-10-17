"""Authenticated, owner-scoped Mini-App CRUD for private keyword templates."""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select, text

from app.core.config import get_settings
from app.core.database import SessionLocal
from app.models import InstagramAccount, ReplyTemplate
from app.webapp.auth import MiniAppIdentity, resolve_miniapp_user

router = APIRouter(prefix="/api/reply-templates", tags=["reply-templates"])


def _account_id(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise HTTPException(400, "شناسهٔ پیج معتبر نیست.")
    return value


async def _owned_account(session, account_id: int, user_id: int) -> InstagramAccount:
    account = (await session.execute(select(InstagramAccount).where(
        InstagramAccount.id == account_id, InstagramAccount.owner_id == user_id,
    ))).scalar_one_or_none()
    if not account:
        raise HTTPException(404, "پیج پیدا نشد یا متعلق به شما نیست.")
    return account


def _validated_text(value: object, field: str, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > maximum:
        raise HTTPException(400, f"{field} باید بین 1 تا {maximum} نویسه باشد.")
    return value.strip()


def _serialize(item: ReplyTemplate) -> dict:
    return {
        "id": item.id, "account_id": item.account_id,
        "keyword": item.keyword, "reply": item.reply,
        "enabled": item.enabled, "priority": item.priority,
        "created_at": item.created_at.isoformat() if item.created_at else None,
    }


@router.get("")
async def list_templates(
    account_id: int,
    ident: MiniAppIdentity = Depends(resolve_miniapp_user),
):
    async with SessionLocal() as session:
        await _owned_account(session, _account_id(account_id), ident.user_id)
        rows = (await session.execute(
            select(ReplyTemplate).where(ReplyTemplate.account_id == account_id)
            .order_by(ReplyTemplate.priority.desc(), ReplyTemplate.id.asc())
        )).scalars().all()
    return {"templates": [_serialize(item) for item in rows], "worker_enabled": get_settings().auto_reply_worker_enabled}


@router.get("/budget")
async def get_auto_reply_budget(
    account_id: int,
    ident: MiniAppIdentity = Depends(resolve_miniapp_user),
):
    """Read-only per-page actual+conservative automatic send usage."""
    from app.services.limits import AUTO_REPLY_DAILY_LIMIT, AUTO_REPLY_ROLLING_HOURLY_LIMIT

    async with SessionLocal() as session:
        await _owned_account(session, _account_id(account_id), ident.user_id)
        counts = (await session.execute(text("""
            SELECT
              (SELECT count(*) FROM public.auto_reply_slots
               WHERE account_id = :aid AND status IN ('reserved','sent','unknown')
                 AND reserved_at >= ((now() AT TIME ZONE 'Asia/Tehran')::date AT TIME ZONE 'Asia/Tehran'))
              + (SELECT count(*) FROM public.activity_logs
                 WHERE account_id = :aid AND action = 'chatbotx_outbound'
                   AND created_at >= ((now() AT TIME ZONE 'Asia/Tehran')::date AT TIME ZONE 'Asia/Tehran')) AS today,
              (SELECT count(*) FROM public.auto_reply_slots
               WHERE account_id = :aid AND status IN ('reserved','sent','unknown')
                 AND reserved_at > now() - interval '1 hour')
              + (SELECT count(*) FROM public.activity_logs
                 WHERE account_id = :aid AND action = 'chatbotx_outbound'
                   AND created_at > now() - interval '1 hour') AS hour
        """), {"aid": account_id})).one()
    return {
        "account_id": account_id, "used_today": counts.today, "used_hour": counts.hour,
        "daily_limit": AUTO_REPLY_DAILY_LIMIT,
        "hourly_limit": AUTO_REPLY_ROLLING_HOURLY_LIMIT,
        "worker_enabled": get_settings().auto_reply_worker_enabled,
    }


@router.post("")
async def create_template(
    body: dict,
    ident: MiniAppIdentity = Depends(resolve_miniapp_user),
):
    body = body or {}
    account_id = _account_id(body.get("account_id"))
    keyword = _validated_text(body.get("keyword"), "کلمهٔ کلیدی", 160)
    reply = _validated_text(body.get("reply"), "پاسخ", 2000)
    async with SessionLocal() as session:
        await _owned_account(session, account_id, ident.user_id)
        count = (await session.execute(select(func.count()).select_from(ReplyTemplate).where(
            ReplyTemplate.account_id == account_id
        ))).scalar_one()
        if count >= 100:
            raise HTTPException(409, "سقف 100 قالب برای این پیج پر شده است.")
        item = ReplyTemplate(account_id=account_id, keyword=keyword, reply=reply)
        session.add(item)
        await session.commit()
        await session.refresh(item)
        return {"ok": True, "template": _serialize(item)}


@router.patch("/{template_id}")
async def update_template(
    template_id: int, body: dict,
    ident: MiniAppIdentity = Depends(resolve_miniapp_user),
):
    body = body or {}
    async with SessionLocal() as session:
        item = await session.get(ReplyTemplate, template_id)
        if not item:
            raise HTTPException(404, "قالب پیدا نشد.")
        await _owned_account(session, item.account_id, ident.user_id)
        # Account binding is immutable, including on partial updates.
        if "account_id" in body and body["account_id"] != item.account_id:
            raise HTTPException(400, "پیج قالب قابل تغییر نیست.")
        if "keyword" in body:
            item.keyword = _validated_text(body["keyword"], "کلمهٔ کلیدی", 160)
        if "reply" in body:
            item.reply = _validated_text(body["reply"], "پاسخ", 2000)
        if "enabled" in body:
            if type(body["enabled"]) is not bool:
                raise HTTPException(400, "وضعیت قالب نامعتبر است.")
            item.enabled = body["enabled"]
        if "priority" in body:
            priority = body["priority"]
            if isinstance(priority, bool) or not isinstance(priority, int) or not -100 <= priority <= 100:
                raise HTTPException(400, "اولویت باید بین -100 تا 100 باشد.")
            item.priority = priority
        item.updated_at = datetime.now(timezone.utc)
        await session.commit()
        return {"ok": True, "template": _serialize(item)}


@router.delete("/{template_id}")
async def delete_template(
    template_id: int,
    ident: MiniAppIdentity = Depends(resolve_miniapp_user),
):
    async with SessionLocal() as session:
        item = await session.get(ReplyTemplate, template_id)
        if not item:
            raise HTTPException(404, "قالب پیدا نشد.")
        await _owned_account(session, item.account_id, ident.user_id)
        await session.delete(item)
        await session.commit()
    return {"ok": True}
